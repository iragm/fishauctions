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
CATEGORY_CHAT = "chat"
CATEGORY_MEMBERSHIP = "membership"
CATEGORY_AUCTION_ADMIN = "auction_admin"
CATEGORY_PROMO = "promo"

PUSH_EXEMPT_CATEGORIES = frozenset({CATEGORY_ACCOUNT})

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
    """Send a single FCM data message to *token*.

    Returns :data:`SEND_OK`, :data:`SEND_INVALID_TOKEN` (dead token — caller should prune it), or
    :data:`SEND_ERROR` (transient). Never raises. Data-only message (no ``notification`` block) so
    the app renders and routes it consistently; the WebView opens ``url`` on tap.
    """
    app = _get_firebase_app()
    if app is None:
        return SEND_ERROR
    try:
        from firebase_admin import messaging
    except ImportError:
        return SEND_ERROR

    message = messaging.Message(
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
