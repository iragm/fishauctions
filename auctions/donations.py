"""Donation tracking: asking vendors for donations, and reading what they write back.

Three jobs live here, all of them wrapped around :mod:`auctions.llm`:

  * :func:`summarize_incoming` -- turn a vendor's reply into a 200-character summary and, when the
    reply is clear enough, a new vendor status.
  * :func:`draft_request` -- write the donation request itself, given whatever context the club
    admin typed plus what the club has already told us about itself.
  * :func:`send_request` / :func:`record_copied_request` -- commit an outgoing message: one
    :class:`~auctions.models.DonationEmail` row, a new follow-up date, and a club history line.

Everything the model returns is untrusted and is validated here before it reaches the database --
a status it invents is discarded, and a summary it pads out is truncated.  Both prompts truncate
their inputs hard: a vendor can put a megabyte of quoted history in a reply, and none of it is
worth paying for.

Rate limiting is per club per day rather than per user, because the thing being protected is the
API bill, and a club with ten admins is still one club.
"""

from __future__ import annotations

import logging
import re
from email.utils import parseaddr

from django.core.cache import cache
from django.utils import timezone

from .llm import LLMError, get_provider
from .models import ClubHistory, DonationEmail, DonationUnsubscribe, DonationVendor, LLMUsage

logger = logging.getLogger(__name__)

# --- limits ------------------------------------------------------------------

#: Incoming messages we'll pay to summarize, per club per day. Anything past this is still stored,
#: just without a summary or an automatic status change -- the record is the important part.
MAX_INCOMING_LLM_CALLS_PER_DAY = 30

#: Donation requests we'll draft per club per day. Separate budget from the incoming one so a club
#: being spammed can still write its own mail.
MAX_DRAFT_LLM_CALLS_PER_DAY = 30

#: How much of an incoming email to send for summarizing. Real replies say yes or no in the first
#: paragraph; past this it's quoted threads and signatures.
INCOMING_BODY_LIMIT = 4000

#: How much of the previous message to include when drafting. Same reasoning.
LAST_EMAIL_LIMIT = 2000

#: How much admin-typed context to pass through.
CONTEXT_LIMIT = 2000

SUMMARY_LENGTH = 200

# Roughly one day, but pinned to the clock so "30 per day" doesn't drift into "30 per rolling day"
# and let a caller double up across a boundary.
_RATE_LIMIT_WINDOW_SECONDS = 60 * 60 * 24


def _rate_limit_key(club, bucket):
    return f"donation_llm_{bucket}_{club.pk}_{timezone.now():%Y%m%d}"


def check_rate_limit(club, bucket="incoming", limit=MAX_INCOMING_LLM_CALLS_PER_DAY):
    """Consume one unit of *club*'s daily budget. Returns True when the call may proceed.

    ``cache.add`` then ``cache.incr`` is the same atomic pattern the command palette uses for its
    per-user budget: one round trip, no read-modify-write race between two workers.
    """
    key = _rate_limit_key(club, bucket)
    cache.add(key, 0, timeout=_RATE_LIMIT_WINDOW_SECONDS)
    try:
        used = cache.incr(key)
    except ValueError:
        # Expired between add and incr; treat as the first call of a new window.
        cache.set(key, 1, timeout=_RATE_LIMIT_WINDOW_SECONDS)
        used = 1
    return used <= limit


# --- text handling -----------------------------------------------------------

_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
_STYLE_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATA_URI_RE = re.compile(r"data:[^\s\"'>]{40,}", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")

#: Lines that start a quoted reply chain. Everything from here down is a copy of what we sent.
_QUOTE_MARKERS = (
    "-----original message-----",
    "________________________________",
)
_ON_WROTE_RE = re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.IGNORECASE | re.MULTILINE)


def strip_email_html(raw):
    """Reduce an HTML (or plain) email body to readable plain text.

    Images go entirely -- both the tags and any inline ``data:`` payloads, which can be megabytes
    of base64 that would otherwise be stored and, worse, billed for as prompt tokens.
    """
    text = raw or ""
    text = _STYLE_SCRIPT_RE.sub(" ", text)
    text = _IMG_TAG_RE.sub(" ", text)
    text = _DATA_URI_RE.sub("[image removed]", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    # Unescape the handful of entities that survive tag stripping and actually show up in mail.
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(entity, char)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def strip_quoted_reply(text):
    """Drop the quoted copy of our own message from the bottom of a reply.

    Best-effort and deliberately conservative: this only saves tokens, so a missed marker costs
    nothing but a slightly longer prompt, while an over-eager cut would hide what the vendor said.
    """
    body = text or ""
    cut = len(body)
    lowered = body.lower()
    for marker in _QUOTE_MARKERS:
        found = lowered.find(marker)
        if found != -1:
            cut = min(cut, found)
    match = _ON_WROTE_RE.search(body)
    if match:
        cut = min(cut, match.start())
    # A reply that is *entirely* quoted text is more likely a bad match than a real cut.
    trimmed = body[:cut].strip()
    return trimmed or body.strip()


def truncate_for_model(text, limit):
    """Cut *text* to *limit* characters on a word boundary, marking where it was cut."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    clipped = body[:limit]
    space = clipped.rfind(" ")
    if space > limit * 0.8:
        clipped = clipped[:space]
    return clipped.rstrip() + "\n\n[truncated]"


def sender_address(raw):
    """Pull the bare address out of a ``From:`` header value."""
    return (parseaddr(raw or "")[1] or "").strip().lower()


# --- incoming mail -----------------------------------------------------------

_INCOMING_SYSTEM_PROMPT = """You read replies that businesses send to a small hobbyist club that \
has asked them to donate something to a raffle or charity auction.

Return a single JSON object with exactly these keys:
  "summary": a plain-English summary of the reply in at most 200 characters. No preamble, no \
quoting, just what they said and anything the club has to do next.
  "status": one of "interested", "promised", "not_interested", or "unclear".

Choose the status carefully:
  "promised"       - they have clearly committed to giving something specific.
  "interested"     - they are receptive, asking questions, or want more information, but have not \
committed yet.
  "not_interested" - they have declined, or say they do not donate.
  "unclear"        - anything else, including auto-replies, out-of-office messages, bounce \
notifications, and messages that are not really a reply at all.

Prefer "unclear" when you are unsure. Never infer a commitment from politeness."""


def _validate_incoming_reply(data):
    """Turn the model's JSON into ``(summary, status_or_None)``, discarding anything off-contract."""
    summary = ""
    status = None
    if isinstance(data, dict):
        raw_summary = data.get("summary")
        if isinstance(raw_summary, str):
            summary = " ".join(raw_summary.split())[:SUMMARY_LENGTH]
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            mapped = {
                "interested": DonationVendor.STATUS_INTERESTED,
                "promised": DonationVendor.STATUS_PROMISED,
                "not_interested": DonationVendor.STATUS_NOT_INTERESTED,
            }.get(raw_status.strip().lower())
            if mapped in DonationVendor.LLM_ASSIGNABLE_STATUSES:
                status = mapped
    return summary, status


def summarize_incoming(email_row, *, user=None):
    """Summarize an incoming :class:`DonationEmail` and update its vendor's status.

    Writes the summary onto *email_row* and returns it (possibly empty). Never raises: a model
    that is down, misconfigured, or over budget must not lose the club a vendor's reply.
    """
    vendor = email_row.vendor
    club = vendor.club
    if not check_rate_limit(club, "incoming", MAX_INCOMING_LLM_CALLS_PER_DAY):
        logger.info("Donation summary skipped for club %s: daily model budget used up", club.pk)
        return ""
    provider = get_provider()
    if not provider.is_configured():
        return ""

    body = truncate_for_model(strip_quoted_reply(email_row.body), INCOMING_BODY_LIMIT)
    prompt = f"Vendor: {vendor.name}\nSubject: {email_row.subject}\n\nTheir reply:\n{body}"
    result = None
    try:
        result = provider.complete_json(_INCOMING_SYSTEM_PROMPT, [{"role": "user", "content": prompt}])
        summary, status = _validate_incoming_reply(result.data)
    except LLMError:
        logger.exception("Could not summarize donation email %s", email_row.pk)
        _record_usage(user, result, f"donation reply from {vendor.name}", "donation_summary", success=False)
        return ""
    except Exception:
        logger.exception("Unexpected error summarizing donation email %s", email_row.pk)
        return ""

    _record_usage(user, result, f"donation reply from {vendor.name}", "donation_summary", success=True)
    if summary:
        email_row.summary = summary
        email_row.save(update_fields=["summary"])
    if status:
        apply_incoming_status(vendor, status, user=user)
    return summary


def apply_incoming_status(vendor, status, *, user=None):
    """Move *vendor* to *status* unless it is pinned. Returns True when the status changed.

    "Do not contact" is a floor, not a stage: once a club (or the vendor themselves) has said stop,
    a cheerful-sounding reply must not undo it. The same goes for a vendor who has unsubscribed.
    """
    if status not in DonationVendor.LLM_ASSIGNABLE_STATUSES:
        return False
    if vendor.status == DonationVendor.STATUS_DO_NOT_CONTACT or vendor.unsubscribed:
        return False
    # A received donation is a stronger fact than anything inferred from a later email.
    if vendor.status == DonationVendor.STATUS_RECEIVED:
        return False
    if vendor.status == status:
        return False
    previous = vendor.get_status_display()
    vendor.status = status
    vendor.save(update_fields=["status"])
    ClubHistory.objects.create(
        club=vendor.club,
        user=user,
        action=f"Donation status for {vendor.name} changed from {previous} to {vendor.get_status_display()} by a reply",
        applies_to="DONATIONS",
    )
    return True


def record_incoming(vendor, *, sender, recipients, subject, body, message_id="", date=None):
    """Store an inbound message against *vendor* and reset its follow-up clock.

    Returns ``(email_row, created)``. A repeat delivery of the same Message-ID is ignored -- SES
    retries, and a duplicate row would both double-count the rate limit and confuse the history.
    """
    if message_id:
        existing = DonationEmail.objects.filter(vendor=vendor, message_id=message_id).first()
        if existing:
            return existing, False
    now = timezone.now()
    email_row = DonationEmail.objects.create(
        vendor=vendor,
        direction=DonationEmail.DIRECTION_INCOMING,
        sender=(sender or "")[:255],
        recipients=(recipients or "")[:1000],
        subject=(subject or "")[:500],
        body=strip_email_html(body),
        message_id=(message_id or "")[:500],
        date=date or now,
    )
    # They wrote back, so there is nothing to chase: the ball is in the club's court now.
    vendor.last_contact = now
    vendor.followup_due = now
    vendor.save(update_fields=["last_contact", "followup_due"])
    ClubHistory.objects.create(
        club=vendor.club,
        user=None,
        action=f"Received a donation reply from {vendor.name}",
        applies_to="DONATIONS",
    )
    return email_row, True


# --- drafting ----------------------------------------------------------------

_DRAFT_SYSTEM_PROMPT = """You write short donation request emails on behalf of small, volunteer-run \
hobbyist clubs (aquarium societies and similar) asking local businesses to donate an item to a \
raffle or charity auction.

Return a single JSON object with exactly these keys:
  "subject": a short, specific subject line. No "Re:", no exclamation marks.
  "body": the email body as plain text.

Rules for the body:
  - Open by addressing the contact by name if you were given one, otherwise greet the business.
  - Say who the club is and what the event is, using only the facts you were given.
  - Make one clear, modest ask. Do not suggest a dollar value.
  - Say what the business gets: their name in front of local hobbyists who buy their products.
  - If a mailing address was supplied, include it as the place to send a donation.
  - If a tax or nonprofit identifier was supplied, mention it plainly as part of the club's details.
  - Never state or imply that a donation is tax deductible, and never describe the club as a \
registered charity or nonprofit, unless you were explicitly given a nonprofit or tax identifier. \
Whether a gift is deductible depends on the donor's own circumstances, so do not promise it either \
way.
  - Keep it under 250 words. Warm and direct, not effusive. No emoji, no marketing cliches.
  - End with a plain sign-off from the club. Do not invent a signer's name, phone number, website, \
or any fact you were not given.
  - Do not write a subject line, headers, or an unsubscribe line in the body. Those are added \
separately."""


def build_draft_prompt(vendor, *, context="", last_email=""):
    """Assemble the user-turn prompt for a donation request. Public so tests can read it."""
    club = vendor.club
    lines = [
        f"Club: {club.name}",
        f"Vendor: {vendor.name}",
    ]
    if vendor.contact_name:
        lines.append(f"Contact name: {vendor.contact_name}")
    if club.donation_context.strip():
        lines.append(f"About the club: {truncate_for_model(club.donation_context, CONTEXT_LIMIT)}")
    if club.donation_mailing_address.strip():
        lines.append(f"Send donations to: {club.donation_mailing_address.strip()}")
    next_event = _next_event_line(club)
    if next_event:
        lines.append(f"Next event: {next_event}")
    if context.strip():
        lines.append(f"About this vendor: {truncate_for_model(context, CONTEXT_LIMIT)}")
    if last_email.strip():
        lines.append(
            "Their last communication with the club (write a follow-up that acknowledges it):\n"
            + truncate_for_model(strip_quoted_reply(last_email), LAST_EMAIL_LIMIT)
        )
    else:
        lines.append("There has been no previous contact with this vendor. Write a first approach.")
    return "\n".join(lines)


def _next_event_line(club):
    """A one-line description of the club's next event, or "" when there isn't one.

    Gives the model something concrete to ask *for*, which is the difference between "we hold
    events" and "our spring auction is on the 14th of March".
    """
    try:
        event = club.events.filter(date_start__gte=timezone.now()).order_by("date_start").first()
    except Exception:
        logger.exception("Could not look up the next event for club %s", club.pk)
        return ""
    if not event:
        return ""
    when = timezone.localtime(event.date_start).strftime("%B %-d, %Y")
    where = f" at {event.location}" if event.location else ""
    return f"{event.title} on {when}{where}"


def draft_request(vendor, *, context="", last_email="", user=None):
    """Ask the model for a donation request. Returns ``(subject, body)``.

    Raises :class:`LLMError` when the model can't be reached or won't answer -- the caller shows
    that to the admin, who can still write the email themselves.
    """
    club = vendor.club
    if not check_rate_limit(club, "draft", MAX_DRAFT_LLM_CALLS_PER_DAY):
        msg = "This club has used up its donation email drafts for today. Try again tomorrow."
        raise LLMError(msg)
    provider = get_provider()
    if not provider.is_configured():
        msg = "Automatic email writing is not set up on this site."
        raise LLMError(msg)

    prompt = build_draft_prompt(vendor, context=context, last_email=last_email)
    result = None
    try:
        result = provider.complete_json(_DRAFT_SYSTEM_PROMPT, [{"role": "user", "content": prompt}], max_tokens=3000)
    except LLMError:
        _record_usage(user, result, f"donation request to {vendor.name}", "donation_draft", success=False)
        raise
    _record_usage(user, result, f"donation request to {vendor.name}", "donation_draft", success=True)

    data = result.data if isinstance(result.data, dict) else {}
    subject = data.get("subject")
    body = data.get("body")
    if not isinstance(subject, str) or not isinstance(body, str) or not body.strip():
        msg = "The language model didn't return an email. Try again, or write one yourself."
        raise LLMError(msg)
    return " ".join(subject.split())[:200], body.strip()


# --- outgoing mail -----------------------------------------------------------


class DonationSendError(Exception):
    """Sending was refused. The message is safe to show to the admin."""


def _check_contactable(vendor):
    if not vendor.email:
        msg = "This vendor has no email address."
        raise DonationSendError(msg)
    if not vendor.can_be_contacted:
        raise DonationSendError(vendor.cannot_contact_reason or "This vendor cannot be contacted.")


def unsubscribe_footer(vendor):
    """The physical address and opt-out line every donation email must carry.

    This is not decoration: US bulk commercial email has to name a physical mailing address for the
    sender and give a working, no-cost way to opt out. Both live here so no caller can send a
    donation request without them.
    """
    from django.contrib.sites.models import Site

    domain = Site.objects.get_current().domain
    club = vendor.club
    address = club.donation_mailing_address.strip()
    lines = ["", "---", f"This message is a donation request from {club.name}."]
    if address:
        lines.append(address)
    lines.append(
        f"To stop receiving donation requests from every club on this site: https://{domain}{vendor.unsubscribe_url}"
    )
    return "\n".join(lines)


def compose_email_text(vendor, body):
    """The exact text that goes out (or gets copied), footer included."""
    return f"{body.rstrip()}\n{unsubscribe_footer(vendor)}"


def _record_outgoing(vendor, *, subject, body, user, sender, recipients, message_id=""):
    """Shared bookkeeping for a request that went out, however it went out."""
    now = timezone.now()
    email_row = DonationEmail.objects.create(
        vendor=vendor,
        direction=DonationEmail.DIRECTION_OUTGOING,
        sender=(sender or "")[:255],
        recipients=(recipients or "")[:1000],
        subject=(subject or "")[:500],
        body=body,
        message_id=(message_id or "")[:500],
        date=now,
        sent_by=user,
    )
    vendor.last_contact = now
    vendor.schedule_followup(from_time=now)
    if vendor.status == DonationVendor.STATUS_NEW:
        vendor.status = DonationVendor.STATUS_EMAIL_SENT
    vendor.save(update_fields=["last_contact", "followup_due", "status"])
    return email_row


def send_request(vendor, *, subject, body, user):
    """Send a donation request through the site and record it.

    The From address is the club's per-vendor donation alias, so a reply comes back to
    ``resolve_donation_alias`` and lands on this vendor's row.
    """
    from post_office import mail

    _check_contactable(vendor)
    club = vendor.club
    if not club.sends_donation_email:
        msg = "This club is set up to copy/paste donation emails, not send them from this site."
        raise DonationSendError(msg)
    from_address = vendor.reply_to_address
    if not from_address:
        msg = "Email routing is not enabled on this site, so donation email can't be sent from here."
        raise DonationSendError(msg)
    # A postal address for the sender is not optional on a US solicitation sent in bulk, and when
    # the message leaves this site we are the one putting it on the wire. Refuse rather than send
    # something the club would have to answer for. Copy/paste mode is the admin's own message from
    # their own mail client, so it only warns (see the settings page).
    if not club.donation_mailing_address.strip():
        msg = (
            "Add a donation mailing address in donation settings first — a postal address for the "
            "club is required on donation emails sent from this site."
        )
        raise DonationSendError(msg)

    text = compose_email_text(vendor, body)
    try:
        mail.send(
            [vendor.email],
            # Display name as well as address: the From line has to identify who is actually
            # asking, and the bare relay address on this site's domain does not.
            f'"{club.name}" <{from_address}>',
            subject=subject,
            message=text,
            headers={"Reply-To": from_address},
        )
    except Exception as error:
        logger.exception("Could not queue donation email to vendor %s", vendor.pk)
        msg = "The email could not be queued for sending. Nothing was sent."
        raise DonationSendError(msg) from error

    email_row = _record_outgoing(
        vendor,
        subject=subject,
        body=text,
        user=user,
        sender=from_address,
        recipients=vendor.email,
    )
    ClubHistory.objects.create(
        club=club,
        user=user,
        action=f"Sent a donation request to {vendor.name} ({vendor.email})",
        applies_to="DONATIONS",
    )
    return email_row


def record_copied_request(vendor, *, subject, body, user):
    """Record a request the admin copied out to send from their own mail client.

    The site never sees whether it was really sent, so this is the admin asserting that it was --
    which is the whole trade-off of copy/paste mode, and why the settings page says replies to it
    can't be tracked.
    """
    _check_contactable(vendor)
    email_row = _record_outgoing(
        vendor,
        subject=subject,
        body=compose_email_text(vendor, body),
        user=user,
        sender=(user.email if user else ""),
        recipients=vendor.email,
    )
    ClubHistory.objects.create(
        club=vendor.club,
        user=user,
        action=f"Copied a donation request for {vendor.name} ({vendor.email}) to send by hand",
        applies_to="DONATIONS",
    )
    return email_row


# --- unsubscribing -----------------------------------------------------------


def unsubscribe_vendor(vendor):
    """Honour an unsubscribe: this vendor, and this address everywhere else on the site.

    One-way by design. There is no club-facing undo, because the person who clicked it doesn't
    read the club's admin pages and can't argue with what they find there.
    """
    # A vendor with no address can't have been mailed a link, but guard anyway: a blank row in the
    # unsubscribe table would match nothing and confuse anyone reading it.
    if vendor.email:
        DonationUnsubscribe.objects.get_or_create(
            email=vendor.email,
            defaults={"club": vendor.club},
        )
    affected = DonationVendor.objects.filter(email=vendor.email).exclude(pk=vendor.pk) if vendor.email else []
    vendor.unsubscribed = True
    vendor.status = DonationVendor.STATUS_DO_NOT_CONTACT
    vendor.followup_due = None
    vendor.save(update_fields=["unsubscribed", "status", "followup_due"])
    ClubHistory.objects.create(
        club=vendor.club,
        user=None,
        action=f"{vendor.name} ({vendor.email}) unsubscribed from donation requests",
        applies_to="DONATIONS",
    )
    for other in affected:
        other.unsubscribed = True
        other.status = DonationVendor.STATUS_DO_NOT_CONTACT
        other.followup_due = None
        other.save(update_fields=["unsubscribed", "status", "followup_due"])
        ClubHistory.objects.create(
            club=other.club,
            user=None,
            action=f"{other.name} ({other.email}) unsubscribed from donation requests on another club's email",
            applies_to="DONATIONS",
        )
    return vendor


# --- usage accounting --------------------------------------------------------


def _record_usage(user, result, query, kind, *, success=True):
    """Write one :class:`LLMUsage` row. Never allowed to break the caller."""
    try:
        LLMUsage.objects.create(
            user=user,
            model=(result.model if result else "")[:100],
            prompt_tokens=result.prompt_tokens if result else 0,
            cached_prompt_tokens=result.cached_prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            total_tokens=result.total_tokens if result else 0,
            query=(query or "")[:600],
            response_kind=kind[:30],
            success=success,
        )
    except Exception:
        logger.exception("Could not record donation LLM usage")
