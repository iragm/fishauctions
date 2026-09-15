"""Email → mobile-push routing.

App users can opt to receive push notifications (Firebase Cloud Messaging) instead of emails for
everything *except* account-related mail (verification, password reset, security warnings — always
email). :func:`notify_user` is the single choke point every send site funnels through: it either
sends the caller's email or enqueues a push, never both.

Push degrades gracefully — if FCM isn't configured, the user hasn't opted in, or they have no live
device token, the email is sent. This mirrors ``email_routing.email_routing_enabled()``.

This module owns the *decision* and the low-level FCM send; the actual fan-out to a user's devices
runs in the ``auctions.tasks.send_push_to_user`` Celery task (never send inline in a request).
"""

import json
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

# Notification categories. Account mail is never pushed — a signed-out or wrong phone must never
# receive password resets / security warnings.
CATEGORY_ACCOUNT = "account"
CATEGORY_INVOICE = "invoice"
CATEGORY_WATCHED = "watched"
CATEGORY_AUCTION_CONFIRM = "auction_confirm"
# "You looked at lots in this auction but never joined." Time-limited nudge, ideal as a push.
CATEGORY_AUCTION_REMINDER = "auction_reminder"
CATEGORY_CHAT = "chat"
CATEGORY_MEMBERSHIP = "membership"
CATEGORY_AUCTION_ADMIN = "auction_admin"
CATEGORY_PROMO = "promo"
# "Your Bluetooth printer is supported now." Push-only by nature: an ObservedPrinter row exists
# only because someone paired a printer in the app.
CATEGORY_PRINTER = "printer"
# "Someone needs help right now." Push-only by nature: an email arrives long after the job is done.
CATEGORY_VOLUNTEER = "volunteer"
# "A lot you're watching is being sold right now." Distinct from CATEGORY_WATCHED (the nightly
# "watched lots ending soon" mail): this one has no email form and is worthless minutes later.
CATEGORY_LOT_SELLING = "lot_selling"
# "You just won a lot, and here's what you've spent so far." One per auction, not one per lot: the
# collapse key is the auction, so each sale rewrites the same notification in place. Push-only by
# nature -- it is a running total, worthless once the auction is over and absurd as a per-lot email.
CATEGORY_RUNNING_TOTAL = "running_total"
# The one-time "this is a setting you can turn off" tip that follows a person's first running total.
# Separate category so it is never folded into the running total's own collapse key.
CATEGORY_RUNNING_TOTAL_TIP = "running_total_tip"
# "Tap to Pay on iPhone is here." Apple's marketing requirements ask for a launch email (6.1) AND an
# in-app push (6.3) with different, separately-specified copy, so the push must not fall back to
# emailing its own text -- that would be a third message that is neither of the two required ones,
# on top of the launch email the same command already sent.
CATEGORY_TAP_TO_PAY_LAUNCH = "tap_to_pay_launch"
# "Your club just said something." Push-only by nature: the club picks its channels one by one on
# the announcement form, and a member who didn't get the push is reached by the Discord post or the
# club's own website -- not by a surprise email nobody ticked a box for.
CATEGORY_CLUB_ANNOUNCEMENT = "club_announcement"

# Categories with no email equivalent -- either app-native, or so time-critical that a late email is
# worse than nothing. A push that can't be delivered in these categories is simply dropped; every
# other category falls back to email (see send_push_to_user).
PUSH_ONLY_CATEGORIES = frozenset(
    {
        CATEGORY_PROMO,
        CATEGORY_PRINTER,
        CATEGORY_VOLUNTEER,
        CATEGORY_LOT_SELLING,
        CATEGORY_RUNNING_TOTAL,
        CATEGORY_RUNNING_TOTAL_TIP,
        CATEGORY_TAP_TO_PAY_LAUNCH,
        CATEGORY_CLUB_ANNOUNCEMENT,
    }
)

# Categories that are always emailed, never pushed:
#   account     - a signed-out or wrong phone must never receive password resets / security warnings
#   membership  - effectively account correspondence for a club; a durable record in an inbox is the
#                 point, and members are often not site users at all
#   auction_admin - running an auction is desk work done from an inbox (invoices ready, follow-ups),
#                 and the emails carry detail a notification can't hold
PUSH_EXEMPT_CATEGORIES = frozenset({CATEGORY_ACCOUNT, CATEGORY_MEMBERSHIP, CATEGORY_AUCTION_ADMIN})

# Result of a single-token FCM send.
SEND_OK = "sent"
SEND_INVALID_TOKEN = "invalid_token"  # token is dead/unregistered → prune it
SEND_ERROR = "error"  # transient failure → keep the token, try again later

_firebase_app = None
_firebase_lock = threading.Lock()


def push_configured():
    """True when FCM credentials are configured; when False, everything falls back to email."""
    return bool(getattr(settings, "FIREBASE_CREDENTIALS_JSON", ""))


def user_prefers_push(user):
    """Whether *user*'s notifications should go to the app instead of email.

    Thin module-level wrapper over ``UserData.user_prefers_push`` so send sites can call a plain
    function. Safe if userdata is missing (returns False rather than raising).
    """
    userdata = getattr(user, "userdata", None)
    if userdata is None:
        return False
    return userdata.user_prefers_push()


def user_has_app_push(user):
    """Whether *user* can be reached through the app right now, regardless of the email toggle.

    Thin module-level wrapper over ``UserData.has_app_push``, for the notification categories that
    were never emails (watched-lot "selling now" pushes) and so aren't governed by
    ``push_notifications_instead_of_email``. Safe if userdata is missing (returns False).
    """
    userdata = getattr(user, "userdata", None)
    if userdata is None:
        return False
    return userdata.has_app_push


def notify_user(user, *, category, title, body, url, send_email, auction_pk=None, invoice_pk=None, collapse_key=None):
    """Push if *user* prefers push and *category* is push-eligible; otherwise call ``send_email``.

    ``send_email`` is a zero-arg callable that performs the site's existing email exactly as before,
    so non-push users are entirely unaffected. Returns True if a push was enqueued, False if the
    email path was taken.
    """
    if category in PUSH_EXEMPT_CATEGORIES or not user_prefers_push(user):
        send_email()
        return False

    from auctions.tasks import send_push_to_user

    send_push_to_user.delay(
        user.pk,
        title=title,
        body=body,
        url=url,
        category=category,
        collapse_key=collapse_key,
        auction_pk=auction_pk,
        invoice_pk=invoice_pk,
    )
    return True


def notify_running_total(lot):
    """Push the winner their updated total the moment a lot is knocked down to them.

    In-person auctions only, and only to a winner whose account can receive an app notification:
    this is a number somebody wants while they are still standing in the room, and the same figure
    delivered by email hours later is noise. **One notification per auction, not per lot** -- the
    collapse key is the auction, so every sale rewrites the same alert in place and the phone shows
    the newest lot and the newest total rather than a column of them.

    The first running total a person ever receives is followed by a second, separate notification
    saying the setting exists. ``UserData.running_total_tip_sent`` is what holds that to once per
    person: without it the tip would arrive after every lot. It is set before the push is enqueued
    rather than after it lands, because a failed send that re-armed the tip would eventually deliver
    it twice, and a tip nobody sees costs less than one that repeats.

    This lives here rather than in the set-winners view because two callers need it -- that view and
    the app's offline sync (``mobile.services.offline``), which mirrors it. Returns True when a push
    was enqueued.
    """
    from django.contrib.sites.models import Site
    from django.db import transaction
    from django.urls import reverse

    from auctions.models import Invoice
    from auctions.tasks import send_push_to_user

    auction = lot.auction if lot else None
    if not auction or auction.is_online:
        return False
    tos = lot.auctiontos_winner
    user = tos.user if tos else None
    if not user:
        # In-person bidders often have no account at all; there is nobody to notify.
        return False
    userdata = getattr(user, "userdata", None)
    if userdata is None or not userdata.show_running_total_notification:
        return False
    if not userdata.has_app_push:
        return False
    invoice = Invoice.objects.filter(auctiontos_user=tos, auction=auction).first()
    if not invoice:
        return False

    symbol = lot.currency_symbol
    title = f"{lot.lot_name} {symbol}{lot.winning_price}"
    bought = invoice.lots_bought
    body = f"Your total so far: {symbol}{invoice.total_bought:.2f} for {bought} lot{'' if bought == 1 else 's'}"
    domain = Site.objects.get_current().domain
    invoice_url = f"https://{domain}{reverse('my_auction_invoice', kwargs={'slug': auction.slug})}"
    tip_url = f"https://{domain}{reverse('notification_preferences')}"
    send_tip = not userdata.running_total_tip_sent
    if send_tip:
        userdata.running_total_tip_sent = True
        userdata.save(update_fields=["running_total_tip_sent"])

    def _enqueue():
        send_push_to_user.delay(
            user.pk,
            title=title,
            body=body,
            url=invoice_url,
            category=CATEGORY_RUNNING_TOTAL,
            collapse_key=f"running_total_{auction.pk}",
            auction_pk=auction.pk,
            invoice_pk=invoice.pk,
        )
        if send_tip:
            # Deliberately no collapse key: this must not replace, or be replaced by, the running
            # total it arrives alongside.
            send_push_to_user.delay(
                user.pk,
                title="Notifications as you win lots",
                body="Turn this off in preferences",
                url=tip_url,
                category=CATEGORY_RUNNING_TOTAL_TIP,
                auction_pk=auction.pk,
            )

    # The invoice this total was read off is written in the caller's transaction; enqueueing inside
    # it would let the task run against rows that are still uncommitted.
    transaction.on_commit(_enqueue)
    return True


def _get_firebase_app():
    """Lazily initialise (once) and return the firebase_admin app, or None if unavailable."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app
    raw = getattr(settings, "FIREBASE_CREDENTIALS_JSON", "")
    if not raw:
        return None
    with _firebase_lock:
        if _firebase_app is not None:
            return _firebase_app
        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError:
            logger.error("firebase-admin is not installed; push notifications are unavailable.")
            return None
        try:
            if raw.strip().startswith("{"):
                cred = credentials.Certificate(json.loads(raw))
            else:
                cred = credentials.Certificate(raw)  # treat as a path
            _firebase_app = firebase_admin.initialize_app(cred, name="fishauctions-push")
        except Exception:
            logger.exception("Failed to initialise Firebase for push notifications.")
            return None
    return _firebase_app


def send_fcm_message(token, *, title, body, url, category, collapse_key=None):
    """Send a single FCM notification+data hybrid message to *token*.

    Returns :data:`SEND_OK`, :data:`SEND_INVALID_TOKEN` (dead token — caller should prune it), or
    :data:`SEND_ERROR` (transient). Never raises.

    Hybrid message: the ``notification`` block lets the OS display the alert itself when the app is
    backgrounded or terminated (on both Android and iOS), while the ``data`` block carries the same
    fields for tap-routing — the WebView opens ``url`` on tap when the app handles the notification.
    """
    app = _get_firebase_app()
    if app is None:
        return SEND_ERROR
    try:
        from firebase_admin import messaging
    except ImportError:
        return SEND_ERROR

    apns_headers = {"apns-priority": "10"}
    if collapse_key:
        # iOS has no notion of android's collapse_key; apns-collapse-id is the equivalent, and
        # without it a phone that gets "coming up soon" then "about to be sold" stacks two alerts
        # for the same lot instead of replacing the first. Apple caps the id at 64 bytes.
        apns_headers["apns-collapse-id"] = collapse_key[:64]
    message = messaging.Message(
        notification=messaging.Notification(title=title or "", body=body or ""),
        data={
            "title": title or "",
            "body": body or "",
            "url": url or "",
            "category": category or "",
        },
        token=token,
        android=messaging.AndroidConfig(
            priority="high",
            collapse_key=collapse_key or None,
        ),
        apns=messaging.APNSConfig(
            headers=apns_headers,
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
        ),
    )
    try:
        messaging.send(message, app=app)
        return SEND_OK
    except (messaging.UnregisteredError, messaging.SenderIdMismatchError):
        return SEND_INVALID_TOKEN
    except ValueError:
        # Malformed/invalid token — treat as dead so it gets pruned.
        return SEND_INVALID_TOKEN
    except Exception:
        logger.exception("FCM send failed (transient) for category %s", category)
        return SEND_ERROR


def send_fcm_data_message(token, data):
    """Send a **data-only** FCM message to *token*. Same return values as :func:`send_fcm_message`.

    No ``notification`` block, deliberately: this is used to tell an app that is already open and on
    screen to do something (print a batch of labels), and it draws its own progress UI. A notification
    block would make the OS post an alert as well, which is wrong for a job the user started from
    their computer thirty seconds ago and is watching on that computer.

    FCM data values must be strings — the caller is responsible for that, and this asserts nothing
    about the keys beyond what the app agrees to read.

    iOS needs ``apns-push-type: background`` with ``content-available``, and Apple requires priority
    5 (not 10) for those; a background push at priority 10 is rejected outright. That is fine here:
    the contract for this feature is already that the app is foregrounded, where delivery is prompt.
    """
    app = _get_firebase_app()
    if app is None:
        return SEND_ERROR
    try:
        from firebase_admin import messaging
    except ImportError:
        return SEND_ERROR

    message = messaging.Message(
        data={str(key): str(value) for key, value in data.items()},
        token=token,
        android=messaging.AndroidConfig(priority="high"),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "5", "apns-push-type": "background"},
            payload=messaging.APNSPayload(aps=messaging.Aps(content_available=True)),
        ),
    )
    try:
        messaging.send(message, app=app)
        return SEND_OK
    except (messaging.UnregisteredError, messaging.SenderIdMismatchError):
        return SEND_INVALID_TOKEN
    except ValueError:
        return SEND_INVALID_TOKEN
    except Exception:
        logger.exception("FCM data-message send failed (transient); data keys: %s", sorted(data))
        return SEND_ERROR
