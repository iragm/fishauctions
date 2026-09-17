"""Email to mobile-push routing.

App users can opt into push (Firebase Cloud Messaging) instead of email for everything except
account mail. :func:`notify_user` is the single choke point: it sends the email or enqueues a push,
never both, and falls back to email whenever push isn't available.

This module owns the decision and the FCM send; the fan-out to a user's devices runs in the
``auctions.tasks.send_push_to_user`` Celery task.
"""

import json
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

# Notification categories. Account mail is never pushed.
CATEGORY_ACCOUNT = "account"
CATEGORY_INVOICE = "invoice"
CATEGORY_WATCHED = "watched"
CATEGORY_AUCTION_CONFIRM = "auction_confirm"
# "You looked at lots in this auction but never joined."
CATEGORY_AUCTION_REMINDER = "auction_reminder"
CATEGORY_CHAT = "chat"
CATEGORY_MEMBERSHIP = "membership"
CATEGORY_AUCTION_ADMIN = "auction_admin"
CATEGORY_PROMO = "promo"
# "Your Bluetooth printer is supported now." Push-only: it follows an in-app pairing.
CATEGORY_PRINTER = "printer"
# "Someone needs help right now." Push-only: an email arrives too late.
CATEGORY_VOLUNTEER = "volunteer"
# "A lot you're watching is being sold right now." Push-only, unlike the nightly CATEGORY_WATCHED.
CATEGORY_LOT_SELLING = "lot_selling"
# "You just won a lot, and here's what you've spent." One per auction, collapsed on the auction.
CATEGORY_RUNNING_TOTAL = "running_total"
# The one-time "you can turn this off" tip after a first running total; its own category so it
# isn't folded into the running total's collapse key.
CATEGORY_RUNNING_TOTAL_TIP = "running_total_tip"
# "Tap to Pay on iPhone is here." Apple requires separate email (6.1) and push (6.3) copy, so this
# must never fall back to emailing its own text.
CATEGORY_TAP_TO_PAY_LAUNCH = "tap_to_pay_launch"
# "Your club just said something." Push-only: the club picks its channels one by one.
CATEGORY_CLUB_ANNOUNCEMENT = "club_announcement"

# Categories with no email equivalent; an undeliverable push is dropped rather than emailed.
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

# Always emailed, never pushed:
#   account       - a wrong or signed-out phone must never get password resets
#   membership    - club correspondence, often to people who aren't site users
#   auction_admin - desk work done from an inbox, with detail a notification can't hold
PUSH_EXEMPT_CATEGORIES = frozenset({CATEGORY_ACCOUNT, CATEGORY_MEMBERSHIP, CATEGORY_AUCTION_ADMIN})

# Result of a single-token FCM send.
SEND_OK = "sent"
SEND_INVALID_TOKEN = "invalid_token"  # token is dead/unregistered → prune it
SEND_ERROR = "error"  # transient failure → keep the token, try again later

_firebase_app = None
_firebase_lock = threading.Lock()


def push_configured():
    """True when FCM credentials are configured; otherwise everything falls back to email."""
    return bool(getattr(settings, "FIREBASE_CREDENTIALS_JSON", ""))


def user_prefers_push(user):
    """Whether *user*'s notifications go to the app instead of email.

    Wraps ``UserData.user_prefers_push``; False when userdata is missing.
    """
    userdata = getattr(user, "userdata", None)
    if userdata is None:
        return False
    return userdata.user_prefers_push()


def user_has_app_push(user):
    """Whether *user* can be reached through the app at all, regardless of the email toggle.

    Wraps ``UserData.has_app_push``, for categories that were never emails.
    """
    userdata = getattr(user, "userdata", None)
    if userdata is None:
        return False
    return userdata.has_app_push


def notify_user(user, *, category, title, body, url, send_email, auction_pk=None, invoice_pk=None, collapse_key=None):
    """Push if *user* prefers push and *category* allows it, otherwise call ``send_email``.

    ``send_email`` is a zero-arg callable. Returns True if a push was enqueued.
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
    """Push the winner their updated total as a lot is knocked down to them.

    In-person auctions only, and only to a winner reachable in the app. One notification per auction:
    the collapse key is the auction, so each sale rewrites the same alert.

    The first running total is followed by a one-time tip about the setting; ``UserData.running_total_tip_sent``
    is set before enqueueing, since a repeat costs more than a missed tip.

    Shared by the set-winners view and the app's offline sync. Returns True when a push was enqueued.
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
        # In-person bidders often have no account.
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
            # No collapse key: this must not replace the running total it arrives with.
            send_push_to_user.delay(
                user.pk,
                title="Notifications as you win lots",
                body="Turn this off in preferences",
                url=tip_url,
                category=CATEGORY_RUNNING_TOTAL_TIP,
                auction_pk=auction.pk,
            )

    # The invoice is written in the caller's transaction, so enqueue after commit.
    transaction.on_commit(_enqueue)
    return True


def _get_firebase_app():
    """Lazily initialise and return the firebase_admin app, or None."""
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
    """Send one FCM notification+data message to *token*.

    Returns :data:`SEND_OK`, :data:`SEND_INVALID_TOKEN` (prune it) or :data:`SEND_ERROR` (transient).
    Never raises. The ``notification`` block lets the OS display the alert when the app is backgrounded;
    the ``data`` block carries the same fields for tap-routing.
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
        # iOS uses apns-collapse-id (capped at 64 bytes) instead of collapse_key.
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
    """Send a data-only FCM message to *token*, with the same return values as :func:`send_fcm_message`.

    No ``notification`` block: this tells an app already on screen to do something (print a batch) and
    it draws its own UI. Values must be strings. iOS needs ``apns-push-type: background`` with
    ``content-available`` at priority 5, which is fine since the app is foregrounded.
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
