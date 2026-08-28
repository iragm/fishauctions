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
API bill, and a club with ten admins is still one club.  Outgoing work -- drafting and sending --
shares one allowance, so the number the vendor page shows is the whole story; incoming replies are
budgeted separately, since a club should not be able to spend its way out of reading its own mail.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
from dataclasses import dataclass
from email.utils import parseaddr

from django.core.cache import cache
from django.utils import timezone

from .email_routing import sender_with_display_name
from .llm import LLMError, get_provider
from .models import ClubHistory, DonationEmail, DonationUnsubscribe, DonationVendor, LLMUsage

logger = logging.getLogger(__name__)

# --- limits ------------------------------------------------------------------

#: Incoming messages we'll pay to summarize, per club per day. Anything past this is still stored,
#: just without a summary or an automatic status change -- the record is the important part.
#: Kept separate from the outgoing allowance below so a club being written to can still write.
MAX_INCOMING_LLM_CALLS_PER_DAY = 30

#: Donation emails a club may send -- or record as copied, or ask the model to write -- in one day.
#: It is what keeps donation tracking from being usable as a mailing tool, and it is also what caps
#: the API bill: asking for a draft spends one whether or not the email is ever sent, because the
#: call was made and paid for either way. Thirty is more vendors than a volunteer-run club
#: approaches in a week.
MAX_DONATION_EMAILS_PER_DAY = 30

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
    # Local date, not UTC: this has to name the same day as _day_bounds below, or the counter a
    # club can see on the page and the counter behind it would roll over hours apart.
    return f"donation_llm_{bucket}_{club.pk}_{timezone.localtime():%Y%m%d}"


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


def calls_used_today(club, bucket):
    """How much of *club*'s daily budget for *bucket* is gone, without consuming any of it."""
    try:
        return int(cache.get(_rate_limit_key(club, bucket)) or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class DonationEmailQuota:
    """How much of a club's daily donation-email allowance is left, and when it comes back."""

    used: int
    limit: int
    resets_at: datetime.datetime

    @property
    def remaining(self):
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self):
        return self.used >= self.limit

    @property
    def percent_used(self):
        """0-100, for a progress bar. Capped so a bar can't run off the end of its track."""
        if self.limit <= 0:
            return 100
        return min(round(self.used * 100 / self.limit), 100)

    @property
    def resets_in_words(self):
        """How long until the allowance refills, as words: "in 3 hours", or "tomorrow"."""
        seconds = (self.resets_at - timezone.now()).total_seconds()
        if seconds <= 0:
            return "now"
        hours = math.ceil(seconds / 3600)
        if hours <= 1:
            return "in less than an hour"
        if hours < 24:
            return f"in {hours} hours"
        return "tomorrow"

    @property
    def exhausted_message(self):
        """Shown wherever an admin tries to contact a vendor with nothing left in the tank."""
        return f"You've hit your limit of {self.limit} donation emails for today. Try again {self.resets_in_words}."


def _day_bounds(now=None):
    """The start of today and the moment it rolls over, both in the site's own timezone.

    Local rather than UTC because the club reads "today" off a wall clock, and a limit that
    resets in the middle of their afternoon reads as a bug.
    """
    local = timezone.localtime(now or timezone.now())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(days=1)


def donation_email_quota(club, *, now=None):
    """How much of *club*'s daily donation-email allowance is gone.

    Two things spend it, and the larger of the two counts:

      * emails that actually went out, counted in the database -- this one gates real mail going to
        real businesses, so it has to survive a cache flush and be auditable afterwards, and
        copy/paste requests count too, because the club still asked a vendor for something;
      * drafts the model was asked to write, counted in the cache -- asking for one costs an API
        call whether or not the admin goes on to send it, so cancelling the dialog does not hand
        the allowance back.

    The larger rather than the sum: an ordinary send is one draft *and* one email, and charging
    twice for one message would halve a limit the page states plainly. Where the two disagree it is
    because drafts were thrown away (drafts lead) or because the cache was flushed under us (sends
    lead), and in both cases the bigger number is the honest one.
    """
    start, end = _day_bounds(now)
    sent = DonationEmail.objects.filter(
        vendor__club=club,
        direction=DonationEmail.DIRECTION_OUTGOING,
        date__gte=start,
    ).count()
    drafted = calls_used_today(club, "draft")
    return DonationEmailQuota(used=max(sent, drafted), limit=MAX_DONATION_EMAILS_PER_DAY, resets_at=end)


# --- text handling -----------------------------------------------------------

#
# Every pattern here runs over a body written by whoever emailed the vendor, so each one has to
# fail *fast* as well as match correctly. That is why the tag patterns exclude ``<`` as well as
# ``>``: with a plain ``[^>]*``, a body of "<img<img<img..." makes the engine scan from every
# ``<img`` to the end of the string looking for a ``>`` that isn't there, which is quadratic in the
# length of the body -- a few megabytes of that is a CPU burn triggered by sending an email. With
# ``<`` excluded, an unterminated tag stops at the next one and costs nothing. A tag whose
# attribute holds a raw unescaped ``<`` is malformed anyway, and the worst that happens to it is
# that it survives as text.
_IMG_TAG_RE = re.compile(r"<img[^<>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^<>]+>")
_SCRIPT_STYLE_OPEN_RE = re.compile(r"<(script|style)\b[^<>]*>", re.IGNORECASE)
_SCRIPT_STYLE_CLOSE_RE = {
    "script": re.compile(r"</script\s*>", re.IGNORECASE),
    "style": re.compile(r"</style\s*>", re.IGNORECASE),
}
_DATA_URI_RE = re.compile(r"data:[^\s\"'>]{40,}", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")

#: Lines that start a quoted reply chain. Everything from here down is a copy of what we sent.
_QUOTE_MARKERS = (
    "-----original message-----",
    "________________________________",
)
_ON_WROTE_RE = re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_script_and_style(text):
    """Drop ``<script>``/``<style>`` blocks, contents included.

    Walked by hand rather than matched with one ``<script.*?</script>`` pattern for the same reason
    the patterns above exclude ``<``: that regex re-scans from every opening tag to a closing tag
    that may not exist, so "<script>" repeated across a megabyte costs a megabyte of scanning per
    repeat. Here each region of the body is scanned once, and the first time a closing tag turns out
    to be missing, that tag name is written off -- there is no closer later in the body either, so
    every remaining opening tag of that name is ordinary text and needs no second search.

    An unclosed opening tag is left alone rather than swallowing the rest of the message: a vendor
    who writes about HTML in a reply should still be read, and :data:`_TAG_RE` removes the tag.
    """
    pieces = []
    position = 0
    exhausted: set[str] = set()
    while True:
        opening = _SCRIPT_STYLE_OPEN_RE.search(text, position)
        if not opening:
            break
        name = opening.group(1).lower()
        closing = None if name in exhausted else _SCRIPT_STYLE_CLOSE_RE[name].search(text, opening.end())
        if not closing:
            exhausted.add(name)
            pieces.append(text[position : opening.end()])
            position = opening.end()
            continue
        pieces.append(text[position : opening.start()])
        pieces.append(" ")
        position = closing.end()
    pieces.append(text[position:])
    return "".join(pieces)


def strip_email_html(raw):
    """Reduce an HTML (or plain) email body to readable plain text.

    Images go entirely -- both the tags and any inline ``data:`` payloads, which can be megabytes
    of base64 that would otherwise be stored and, worse, billed for as prompt tokens.
    """
    text = raw or ""
    text = _strip_script_and_style(text)
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


#: One reply or forward marker at the front of a subject line. Applied in a loop rather than with a
#: trailing ``+`` on the group: a repeated group whose parts can each match nothing is the classic
#: shape that backtracks exponentially, and subject lines arrive from strangers.
_REPLY_PREFIX_RE = re.compile(r"^\s*(?:re|fwd?)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE)


def strip_reply_prefix(subject):
    """Take "Re: Fwd: Re: Donations" down to "Donations"."""
    text = (subject or "").strip()
    while True:
        shorter = _REPLY_PREFIX_RE.sub("", text, count=1)
        if shorter == text:
            return text
        text = shorter


def followup_subject(previous_subject, fallback=""):
    """The subject for a second email in an existing conversation, or "" when there isn't one.

    Everything after the first email to a vendor is part of one thread -- a nudge about the request
    we sent, or an answer to what they wrote back -- so it goes out as ``RE:`` the subject the
    thread already has. Mail clients group on the subject as well as the message headers, and a
    fresh subject line every time reads to the vendor as a fresh cold approach.
    """
    base = strip_reply_prefix(previous_subject) or strip_reply_prefix(fallback)
    if not base:
        return ""
    return f"RE: {base}"[:200]


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

#: Which of three quite different emails is being written. Derived from the conversation so far by
#: :func:`draft_mode`, and read by both the system prompt and the user turn so the two cannot
#: disagree about what the model is doing.
DRAFT_MODE_FIRST = "first"
DRAFT_MODE_FOLLOWUP = "followup"
DRAFT_MODE_REPLY = "reply"


def draft_mode(last_email="", last_email_is_outgoing=False):
    """First approach, nudge, or reply -- decided by what came last in the conversation."""
    if not (last_email or "").strip():
        return DRAFT_MODE_FIRST
    return DRAFT_MODE_FOLLOWUP if last_email_is_outgoing else DRAFT_MODE_REPLY


_DRAFT_SYSTEM_BASE = """You write email for small, volunteer-run hobbyist clubs (aquarium societies \
and similar) that ask local businesses to donate an item to a raffle or charity auction.

Return a single JSON object with exactly these keys:
  "subject": a short, specific subject line. No "Re:", no exclamation marks.
  "body": the email body as plain text.

Rules that hold for every message:
  - Use only the facts you were given. Never invent a signer's name, a phone number, a website, a \
date, or a value.
  - Never state or imply that a donation is tax deductible, and never describe the club as a \
registered charity or nonprofit, unless you were explicitly given a nonprofit or tax identifier. \
Whether a gift is deductible depends on the donor's own circumstances, so do not promise it either \
way.
  - Warm and direct, not effusive. No emoji, no marketing cliches.
  - End with a plain sign-off from the club.
  - Do not write a subject line, headers, or an unsubscribe line in the body; those are added \
separately. Every message already carries the club's postal address in its footer."""

#: One of these is appended to the base. Keeping them apart is the whole point: the first-approach
#: rules -- introduce the club, state the event, make the ask, list what the business gets -- are
#: exactly what makes a *reply* read as though nobody at the club opened the vendor's message. A
#: heading in the user turn asking for a reply does not undo a system prompt that describes writing
#: a solicitation, so the system prompt has to change too.
_DRAFT_RULES = {
    DRAFT_MODE_FIRST: """
This is the first approach to this business. They have never heard from the club.
  - Open by addressing the contact by name if you were given one, otherwise greet the business.
  - Say who the club is and what the event is.
  - Make one clear, modest ask. Do not suggest a dollar value.
  - Say what the business gets: their name in front of local hobbyists who buy their products.
  - If a tax or nonprofit identifier was supplied, mention it plainly as part of the club's details.
  - Do not put the club's mailing address in the body. The footer already carries it, and \
repeating it only makes the email longer.
  - Under 250 words.""",
    DRAFT_MODE_FOLLOWUP: """
The club has already written to this business and has had no answer. This is a nudge, not a second \
pitch.
  - Refer back to the earlier email in the first sentence. Never greet them as though this were a \
first approach, and never re-introduce the club.
  - Do not repeat the pitch: no restating who the club is, what the event is, or what the business \
gets out of it. They were told all of that already.
  - Make it easy to say no, and say so plainly.
  - Do not put the club's mailing address in the body. The footer already carries it.
  - Shorter than the first email. Under 120 words.""",
    DRAFT_MODE_REPLY: """
The business has written back, and their message is below. You are writing the club's next message \
inside a conversation they are already part of. This is NOT a donation request -- they have read \
one already, and they answered it.
  - Answer what they actually asked, in the first sentence. Everything else is secondary.
  - Never re-introduce the club, restate the event, repeat the ask, or list what the business gets \
out of it. Any of those reads as though nobody at the club read their message, which is the worst \
thing this email can do.
  - Thank them once, briefly.
  - If they ask what happens next, how to get a donation to the club, where to send it, or when it \
is needed, answer concretely. When the answer involves posting or dropping something off, put the \
club's mailing address in the body where they will see it: the footer is fine print, and an answer \
to a direct question belongs in the body.
  - If they have said no, thank them, say the club will not write again about this, and stop there.
  - Under 150 words, and usually far less. A three-sentence reply is a good reply.""",
}


def draft_system_prompt(mode):
    """The system prompt for one kind of donation email. Public so tests can read it."""
    return _DRAFT_SYSTEM_BASE + "\n" + _DRAFT_RULES[mode]


def build_draft_prompt(vendor, *, context="", last_email="", last_email_is_outgoing=False):
    """Assemble the user-turn prompt for a donation request. Public so tests can read it.

    *last_email* is whatever came last in this conversation, and *last_email_is_outgoing* says who
    wrote it. The difference matters: answering a vendor who wrote back and nudging one who never
    did are different emails, and a nudge that re-introduces the club from scratch reads as though
    nobody at the club remembers sending the first one.
    """
    club = vendor.club
    mode = draft_mode(last_email, last_email_is_outgoing)
    lines = [
        f"Club: {club.name}",
        f"Vendor: {vendor.name}",
    ]
    if vendor.contact_name:
        lines.append(f"Contact name: {vendor.contact_name}")
    if club.donation_context.strip():
        lines.append(f"About the club: {truncate_for_model(club.donation_context, CONTEXT_LIMIT)}")
    if club.donation_mailing_address.strip():
        # Handed over with the rule attached: every email already carries this address in its
        # footer, so repeating it in the body is noise -- until the vendor asks how to get the
        # donation to the club, at which point the footer is the wrong place to answer from.
        instruction = (
            "Club mailing address. Put it in the body if their message asks where or how to send a "
            "donation, or what happens next; it is in the footer either way:"
            if mode == DRAFT_MODE_REPLY
            else "Club mailing address, already in the footer of every email. Do not repeat it in the body:"
        )
        lines.append(f"{instruction}\n{club.donation_mailing_address.strip()}")
    next_event = _next_event_line(club)
    if next_event:
        lines.append(f"Next event: {next_event}")
    if context.strip():
        lines.append(f"About this vendor: {truncate_for_model(context, CONTEXT_LIMIT)}")
    if mode == DRAFT_MODE_FIRST:
        lines.append("There has been no previous contact with this vendor. Write a first approach.")
    else:
        heading = (
            "The last email the club sent this vendor. They have not replied to it, so write a "
            "short follow-up: refer back to it, do not repeat the whole pitch, and make it easy "
            "for them to say no. Do not greet them as though this were a first approach:"
            if mode == DRAFT_MODE_FOLLOWUP
            else "Their last message to the club, which is what you are answering. Reply to what "
            "it says. Do not write another donation request:"
        )
        lines.append(f"{heading}\n{truncate_for_model(strip_quoted_reply(last_email), LAST_EMAIL_LIMIT)}")
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


def draft_request(vendor, *, context="", last_email="", last_email_is_outgoing=False, user=None):
    """Ask the model for a donation request. Returns ``(subject, body)``.

    Raises :class:`LLMError` when the model can't be reached or won't answer -- the caller shows
    that to the admin, who can still write the email themselves.
    """
    club = vendor.club
    quota = donation_email_quota(club)
    if quota.exhausted:
        raise LLMError(quota.exhausted_message)
    provider = get_provider()
    if not provider.is_configured():
        # Checked before charging: nothing was asked of anyone, so nothing is owed.
        msg = "Automatic email writing is not set up on this site."
        raise LLMError(msg)
    # Charged up front and never refunded. The call is made and paid for the moment it is asked
    # for, so an admin who reads the draft and hits Cancel has still spent one, and the number on
    # the vendor page has to say so -- otherwise a club can burn the whole day's API budget while
    # the page insists nothing has been used.
    check_rate_limit(club, "draft", MAX_DONATION_EMAILS_PER_DAY)

    prompt = build_draft_prompt(
        vendor, context=context, last_email=last_email, last_email_is_outgoing=last_email_is_outgoing
    )
    system = draft_system_prompt(draft_mode(last_email, last_email_is_outgoing))
    result = None
    try:
        result = provider.complete_json(system, [{"role": "user", "content": prompt}], max_tokens=3000)
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


def contact_blocked_reason(vendor, quota=None):
    """Why an admin can't write to *vendor* right now, or "" when they can.

    One place for both kinds of "no" -- something about the vendor, and the club's daily
    allowance -- so the table button, the vendor panel and the dialog all say the same thing.
    Pass *quota* when rendering a list, so a page of vendors doesn't count the same rows again
    for every row.
    """
    reason = vendor.cannot_contact_reason
    if reason:
        return reason
    quota = donation_email_quota(vendor.club) if quota is None else quota
    return quota.exhausted_message if quota.exhausted else ""


def _check_daily_quota(club):
    """Refuse a request that would take the club past its daily allowance.

    Checked here rather than only in the view so every path -- sent from the site, copied out by
    hand, or anything added later -- is held to the same number.
    """
    quota = donation_email_quota(club)
    if quota.exhausted:
        raise DonationSendError(quota.exhausted_message)


#: The line that opens the footer below. Named once so :func:`strip_donation_footer` can find it
#: again in a stored message without the two drifting apart.
FOOTER_MARKER = "This message is a donation request from:"

#: The separator drawn above the footer, plus any trailing whitespace, so cutting the footer off a
#: stored message doesn't leave a dangling rule behind.
_FOOTER_SEPARATOR_RE = re.compile(r"\n\s*-{2,}\s*$")


def strip_donation_footer(text):
    """Cut the footer this site appends off a message we stored.

    Used when an earlier email is fed back in as context for the next one: the address block and
    opt-out link are added again on the way out, so carrying them into the prompt only pays for
    tokens and invites the model to write its own version of them.
    """
    body = text or ""
    index = body.find(FOOTER_MARKER)
    if index == -1:
        return body.strip()
    return _FOOTER_SEPARATOR_RE.sub("", body[:index].rstrip()).strip()


def unsubscribe_footer(vendor):
    """The physical address and opt-out line every donation email must carry.

    This is not decoration: US bulk commercial email has to name a physical mailing address for the
    sender and give a working, no-cost way to opt out. Both live here so no caller can send a
    donation request without them.

    The club is named once, as the first line of the address block. Almost every club types its own
    name at the top of that address, and a separate "a donation request from <club>" sentence above
    it read as a stutter; when a club has left its name out, it goes in here instead, so the sender
    is always identified either way.
    """
    from django.contrib.sites.models import Site

    domain = Site.objects.get_current().domain
    club = vendor.club
    address = club.donation_mailing_address.strip()
    if not address:
        block = club.name
    elif club.name.strip().lower() in address.lower():
        block = address
    else:
        block = f"{club.name}\n{address}"
    lines = ["", "---", FOOTER_MARKER, block, ""]
    lines.append(f"If you don't want to be contacted again: https://{domain}{vendor.unsubscribe_url}")
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


def _thread_headers(vendor):
    """``In-Reply-To``/``References`` pointing at the vendor's last message, when there is one.

    A ``RE:`` subject alone leaves it to the mail client to guess; these headers are what actually
    file the email under the vendor's own message instead of starting a second thread beside it.
    Only their messages are referenced -- ours are handed to post_office without a Message-ID, so
    there is nothing of ours to point at.
    """
    previous = (
        vendor.emails.filter(direction=DonationEmail.DIRECTION_INCOMING).exclude(message_id="").first()
        if vendor.pk
        else None
    )
    if not previous:
        return {}
    message_id = previous.message_id.strip()
    if not message_id.startswith("<"):
        # Stored as it arrived, and not every relay brackets it. An unbracketed msg-id is not a
        # valid header value, and a mail client that can't parse it ignores the threading entirely.
        message_id = f"<{message_id.strip('<>')}>"
    return {"In-Reply-To": message_id, "References": message_id}


def send_request(vendor, *, subject, body, user):
    """Send a donation request through the site and record it.

    The From address is the club's per-vendor donation alias, so a reply comes back to
    ``resolve_donation_alias`` and lands on this vendor's row.
    """
    from post_office import mail

    _check_contactable(vendor)
    club = vendor.club
    _check_daily_quota(club)
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
            sender_with_display_name(club.name, from_address),
            subject=subject,
            message=text,
            headers={"Reply-To": from_address, **_thread_headers(vendor)},
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
    _check_daily_quota(vendor.club)
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
