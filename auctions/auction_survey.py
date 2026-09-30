"""The post-auction survey: "How was <auction>?", two buttons, and a box for anything else.

``Auction.post_auction_survey`` asks it in the invoice email, in an email of its own once the auction is
``pretty_much_over``, or not at all. An online auction's invoices go out before pickup, so it always asks
in its own email, which for it waits for pickup (:func:`survey_mode`). The answer lives on the person's
``AuctionTOS``.

The emailed buttons are links (``?answer=great&uuid=...``) to a page that posts the answer from
JavaScript: mail scanners open every link in a message, both buttons included, and don't run scripts.
The uuid is the invoice's ``no_login_link``, which is why the survey is only emailed to people with an
invoice, and a click on it proves the address works, so it marks the address verified.
"""

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.sites.models import Site
from django.urls import reverse
from django.utils import timezone
from post_office import mail

from auctions.email_routing import email_routing_enabled
from auctions.models import Auction, AuctionTOS, ClubMember, Invoice

logger = logging.getLogger(__name__)

ANSWERS = dict(AuctionTOS.SURVEY_ANSWERS)
COMMENTS_MAX_LENGTH = 2000
#: A separate survey email goes out this long after the auction wound down at the latest. Switching an old
#: auction to "separate" must not email people about something that happened months ago.
SEND_WINDOW = timedelta(days=7)


def survey_url(auction, answer="", token=""):
    """The survey page, optionally answering on load and carrying the invoice token."""
    query = {key: value for key, value in (("answer", answer), ("uuid", token)) if value}
    url = reverse("auction_survey", kwargs={"slug": auction.slug})
    return f"{url}?{urlencode(query)}" if query else url


def participant(auction, user, token=""):
    """The ``AuctionTOS`` answering: the invoice token's owner, else the signed-in user's, else ``None``."""
    if token:
        invoice = (
            Invoice.objects.filter(no_login_link=token, auction=auction, auctiontos_user__isnull=False)
            .select_related("auctiontos_user")
            .first()
        )
        return invoice.auctiontos_user if invoice else None
    if user.is_authenticated:
        return AuctionTOS.objects.filter(auction=auction, user=user).first()
    return None


def survey_mode(auction):
    """How ``auction`` really asks: its rule, except that an online auction asks separately, after pickup."""
    if auction.is_online and auction.post_auction_survey == Auction.SURVEY_IN_INVOICE:
        return Auction.SURVEY_SEPARATE
    return auction.post_auction_survey


def wants_answer(tos):
    """Whether this person should still be asked."""
    return bool(tos and tos.auction.post_auction_survey != Auction.SURVEY_NONE and not tos.survey_answer)


def record_answer(tos, answer):
    """Save great/not_fun. The last click wins, so somebody can change their mind."""
    if answer not in ANSWERS:
        return False
    tos.survey_answer = answer
    tos.survey_answered_on = tos.survey_answered_on or timezone.now()
    # An update, not save(): AuctionTOS.save re-runs bidder numbers and club syncing.
    AuctionTOS.objects.filter(pk=tos.pk).update(survey_answer=answer, survey_answered_on=tos.survey_answered_on)
    return True


def record_comments(tos, comments):
    tos.survey_comments = (comments or "").strip()[:COMMENTS_MAX_LENGTH]
    tos.survey_answered_on = tos.survey_answered_on or timezone.now()
    AuctionTOS.objects.filter(pk=tos.pk).update(
        survey_comments=tos.survey_comments, survey_answered_on=tos.survey_answered_on
    )


def verify_email(tos):
    """A click from the email proves the address works, everywhere it's used. The opposite of a bounce
    (``signals.bounce_handler``), which marks it BAD by the same address.
    """
    if not tos.email:
        return
    AuctionTOS.objects.filter(email=tos.email).exclude(email_address_status="VALID").update(
        email_address_status="VALID"
    )
    ClubMember.objects.filter(email=tos.email, is_deleted=False).exclude(email_address_status="VALID").update(
        email_address_status="VALID"
    )


def email_links(invoice, mode, domain):
    """``{question, great, not_fun}`` for an emailed invoice when its auction asks this way, else ``None``."""
    auction = getattr(invoice, "auction", None)
    tos = getattr(invoice, "auctiontos_user", None)
    if not auction or not tos or survey_mode(auction) != mode or tos.survey_answer:
        return None
    base = f"https://{domain}"
    return {
        "question": f"How was {auction}?",
        AuctionTOS.SURVEY_GREAT: base + survey_url(auction, AuctionTOS.SURVEY_GREAT, invoice.no_login_link),
        AuctionTOS.SURVEY_NOT_FUN: base + survey_url(auction, AuctionTOS.SURVEY_NOT_FUN, invoice.no_login_link),
    }


def due_auctions(now=None):
    """Auctions asking by separate email whose emails haven't gone yet, between ``pretty_much_over`` (a day
    after the last pickup, online) and :data:`SEND_WINDOW` after they wound down.
    """
    now = now or timezone.now()
    candidates = Auction.objects.filter(
        is_deleted=False,
        post_auction_survey__in=[Auction.SURVEY_SEPARATE, Auction.SURVEY_IN_INVOICE],
        # An auction that emails nobody their invoice doesn't email them this either.
        email_users_when_invoices_ready=True,
        survey_emails_sent=False,
        date_start__lte=now,
        date_start__gte=now - timedelta(days=90),
    )
    return [
        auction
        for auction in candidates
        if survey_mode(auction) == Auction.SURVEY_SEPARATE
        and auction.pretty_much_over
        and now < auction.wind_down_time + SEND_WINDOW
    ]


def send_survey_emails():
    """Send the separate survey email for every due auction. Returns how many were queued."""
    sent = 0
    for auction in due_auctions():
        # Claimed with a conditional update, so two overlapping runs can't both send.
        if not Auction.objects.filter(pk=auction.pk, survey_emails_sent=False).update(survey_emails_sent=True):
            continue
        # The same gate as the invoice email: an untrusted creator's auction emails nobody.
        if not (auction.created_by and auction.created_by.userdata.is_trusted):
            continue
        for invoice in recipients(auction):
            try:
                send_one(invoice)
                sent += 1
            except Exception:
                logger.exception("Survey email failed for invoice %s", invoice.pk)
    return sent


def recipients(auction):
    """Invoices whose owner has a working address, hasn't answered yet, and hasn't unsubscribed."""
    return (
        Invoice.objects.filter(auction=auction, auctiontos_user__isnull=False, auctiontos_user__survey_answer="")
        .exclude(auctiontos_user__email__isnull=True)
        .exclude(auctiontos_user__email="")
        .exclude(auctiontos_user__email_address_status="BAD")
        .exclude(auctiontos_user__user__userdata__has_unsubscribed=True)
        .select_related("auction", "auctiontos_user__user__userdata")
    )


def send_one(invoice):
    auction = invoice.auction
    tos = invoice.auctiontos_user
    domain = Site.objects.get_current().domain
    kwargs = {
        "sender": auction.sender_email_with_name,
        "template": "auction_survey",
        "context": {
            "auction": auction,
            "invoice": invoice,
            "name": tos.name,
            "domain": domain,
            # The footer's "Stop promotional emails", for somebody with an account to unsubscribe.
            "unsubscribe": _unsubscribe_link(tos),
        },
    }
    if not email_routing_enabled():
        # With SES routing, replies go to the routed sender address, so no Reply-To.
        contact_email = auction.created_by.email
        kwargs["headers"] = {"Reply-to": contact_email}
        kwargs["context"]["reply_to_email"] = contact_email
    mail.send(tos.email, **kwargs)


def _unsubscribe_link(tos):
    userdata = getattr(tos.user, "userdata", None) if tos.user_id else None
    # str(): a fresh row holds its default as a UUID, which the queued email's JSON context can't store.
    return str(userdata.unsubscribe_link) if userdata else ""
