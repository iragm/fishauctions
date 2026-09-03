"""What PayPal, Square and the email provider send us, unprompted.

Every view here is an unauthenticated POST from somebody else's server, verified by signature
rather than by session. They are the reason ``_process_invoice_membership_renewal`` lives in
:mod:`auctions.views.base`: a renewal can arrive here as easily as it can be typed into an
invoice.
"""

import json
import logging
from decimal import Decimal, InvalidOperation

import channels.layers
import requests
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
)
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView, View

from auctions.filters import (
    AuctionTOSFilter,
)
from auctions.models import (
    AuctionTOS,
    Club,
    ClubHistory,
    ClubMember,
    ClubMoney,
    Invoice,
    InvoicePayment,
    PayPalSeller,
    SquareSeller,
    UserData,
)
from auctions.tasks import (
    maybe_send_membership_renewal_confirmation,
)

from .base import AuctionViewMixin, _ensure_invoice_renewal_state, _process_invoice_membership_renewal
from .payments import PayPalAPIMixin, PayPalRequestError, SquareAPIMixin

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class PayPalWebhookView(PayPalAPIMixin, View):
    """
    Minimal PayPal webhook handler that:
      - validates the webhook signature with PayPal (verify-webhook-signature)
      - processes a few important event types (onboarding, consent revoke, capture/refund, disputes)
    Requirements:
      - settings.PAYPAL_API_BASE (e.g. https://api-m.sandbox.paypal.com)
      - settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET
      - settings.PAYPAL_WEBHOOK_ID (the webhook id you registered in PayPal dashboard)
      - (optional) settings.PAYPAL_PARTNER_ATTRIBUTION_ID (BN code) if you want to include it in calls
    """

    def post(self, request, *args, **kwargs):
        # Read raw body and parse JSON
        raw_body = request.body
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            logger.exception("Invalid JSON in PayPal webhook: %s", exc)
            return HttpResponseBadRequest("invalid json")

        # Extract PayPal transmission headers (case-insensitive)
        # Django exposes headers as HTTP_<HEADER_NAME> in request.META
        def hdr(name):
            return request.META.get(f"HTTP_{name.upper().replace('-', '_')}", request.headers.get(name))

        transmission_id = hdr("PayPal-Transmission-Id")
        transmission_time = hdr("PayPal-Transmission-Time")
        cert_url = hdr("PayPal-Cert-Url")
        auth_algo = hdr("PayPal-Auth-Algo")
        transmission_sig = hdr("PayPal-Transmission-Sig")

        # Check webhook_id is configured
        webhook_id = getattr(settings, "PAYPAL_WEBHOOK_ID", None)
        if not webhook_id:
            logger.warning("PAYPAL_WEBHOOK_ID not configured, rejecting webhook")
            return HttpResponseBadRequest("webhook not configured")

        if not all([transmission_id, transmission_time, cert_url, auth_algo, transmission_sig]):
            logger.warning(
                "Missing PayPal webhook headers; rejecting. headers=%s webhook_id=%s",
                {
                    k: hdr(k)
                    for k in [
                        "PayPal-Transmission-Id",
                        "PayPal-Transmission-Time",
                        "PayPal-Cert-Url",
                        "PayPal-Auth-Algo",
                        "PayPal-Transmission-Sig",
                    ]
                },
                webhook_id,
            )
            return HttpResponseBadRequest("missing verification headers")

        # Build verification payload with webhook_id
        verify_payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": event,
        }

        # Get access token using client credentials (for webhook verification)
        try:
            access_token = self._get_access_token()
        except Exception as exc:
            logger.error("Failed to obtain PayPal access token for webhook verification: %s", exc)
            return HttpResponse(status=500)

        # Call PayPal verify webhook signature endpoint
        verify_resp = requests.post(
            f"{settings.PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=verify_payload,
            timeout=10,
        )
        try:
            verify_resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "PayPal verify-webhook-signature returned non-2xx: status=%s debug_id=%s body=%s exc=%s",
                verify_resp.status_code,
                verify_resp.headers.get("Paypal-Debug-Id"),
                verify_resp.text[:500],
                exc,
            )
            return HttpResponse(status=500)
        try:
            verify_data = verify_resp.json()
        except ValueError as exc:
            logger.error(
                "PayPal verify-webhook-signature returned non-JSON: status=%s debug_id=%s body=%s exc=%s",
                verify_resp.status_code,
                verify_resp.headers.get("Paypal-Debug-Id"),
                verify_resp.text[:500],
                exc,
            )
            return HttpResponse(status=500)
        verification_status = verify_data.get("verification_status")
        if verification_status != "SUCCESS":
            logger.warning(
                "PayPal webhook signature verification failed: status=%s response=%s",
                verification_status,
                verify_data,
            )
            return HttpResponseBadRequest("webhook verification failed")

        # At this point, the webhook is verified as coming from PayPal.
        event_type = event.get("event_type")
        resource = event.get("resource", {}) or {}

        logger.info("Verified PayPal webhook: %s", event_type)

        if event_type == "MERCHANT.ONBOARDING.COMPLETED":
            # Example: merchant onboarding completed
            # resource may contain merchantId / merchantIdInPayPal / tracking_id
            merchant_id_in_paypal = resource.get("merchant_id")
            tracking_id = resource.get("tracking_id")
            # try find user via tracking_id first
            user = None
            if tracking_id:
                ud = UserData.objects.filter(unsubscribe_link=tracking_id).first()
                if ud:
                    user = ud.user
            # fallback: attempt to find PayPalSeller by merchant id
            if not user and merchant_id_in_paypal:
                seller = PayPalSeller.objects.filter(paypal_merchant_id=merchant_id_in_paypal).first()
                user = seller.user if seller else None

            # Create or update PayPalSeller record (if user found)
            if user:
                seller, _ = PayPalSeller.objects.get_or_create(user=user)
                if merchant_id_in_paypal:
                    seller.paypal_merchant_id = merchant_id_in_paypal
                # email may not be present in the resource; if present, update
                email = resource.get("payerEmail") or resource.get("primary_email") or resource.get("primaryEmail")
                if email:
                    seller.payer_email = email
                seller.save()
                logger.info("Updated PayPalSeller for user %s after onboarding webhook", user.pk)
            else:
                logger.info(
                    "Onboarding webhook: no local user found for merchant %s tracking_id=%s",
                    merchant_id_in_paypal,
                    tracking_id,
                )

        elif event_type == "MERCHANT.PARTNER-CONSENT.REVOKED":
            # Mark seller as disconnected / revoke tokens
            merchant_id_in_paypal = resource.get("merchant_id")
            seller = None
            if merchant_id_in_paypal:
                seller = PayPalSeller.objects.filter(paypal_merchant_id=merchant_id_in_paypal).first()
            if seller:
                seller.delete()
                logger.info("Revoked selling for merchant_id=%s", merchant_id_in_paypal)
            else:
                logger.info("Partner-consent revoked for unknown merchant %s", merchant_id_in_paypal)

        elif event_type == "CHECKOUT.ORDER.COMPLETED":
            # Order is already captured - process the order data from the webhook resource
            # without making a redundant capture API call
            if resource.get("status") == "COMPLETED":
                error, _ = self._process_captured_order(resource)
                if error:
                    logger.error("Error handling completed order webhook: %s", error)
                else:
                    logger.info("Payment processed via CHECKOUT.ORDER.COMPLETED webhook")

        elif event_type == "CHECKOUT.CAPTURE.COMPLETED":
            """This one doesn't save the invoice"""
            try:
                order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
                # Fetch the order details to get purchase_units[0].reference_id (our invoice pk)
                order_data = self.get_from_paypal(f"v2/checkout/orders/{order_id}")
                purchase_unit = (order_data.get("purchase_units") or [{}])[0]
                reference_id = purchase_unit.get("reference_id")
                logger.info("Capture webhook resolved order_id=%s reference_id=%s", order_id, reference_id)

                invoice = Invoice.objects.filter(pk=reference_id).first()
                if invoice:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "capture_complete", "pk": invoice.pk},
                    )
            except Exception:
                logger.exception(
                    "Error processing capture webhook for resource: %s, debug_id %s", resource, self.paypal_debug
                )
            return JsonResponse({"status": "ok"})
        elif event_type == "CHECKOUT.ORDER.APPROVED":
            # Extract the reference_id (our invoice reference) from the approved order and print/log it.
            try:
                purchase_unit = resource.get("purchase_units", [{}])[0]
                reference_id = purchase_unit.get("reference_id")
                logger.info("PayPal webhook CHECKOUT.ORDER.APPROVED reference_id=%s", reference_id)
                invoice = Invoice.objects.filter(pk=reference_id).first()
                if invoice:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "invoice_approved", "pk": invoice.pk},
                    )
            except ValueError:
                logger.exception("Failed to extract reference_id for CHECKOUT.ORDER.APPROVED: %s", resource)
            return JsonResponse({"status": "ok"})

        elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.SALE.REFUNDED"):
            self.handle_refund(resource)
            logger.info("Refund received")

        else:
            # Unhandled event types: log for inspection
            logger.info("Unhandled PayPal webhook event_type=%s resource_keys=%s", event_type, list(resource.keys()))

        # Return success to PayPal
        return JsonResponse({"status": "ok"})


def _parse_paypal_datetime_date(value):
    """PayPal ISO-8601 timestamp (e.g. next_billing_time) -> a date, or None."""
    from django.utils.dateparse import parse_datetime

    if not value:
        return None
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def _mask_subscription_id(subscription_id):
    """Redact a PayPal subscription id for logging.

    The full id (e.g. ``I-BW452GLLEP1G``) is a sensitive account identifier and must never reach the
    logs. Keep only the last 4 characters so log lines can still be correlated with each other (and
    with PayPal support) without exposing the whole id."""
    subscription_id = subscription_id or ""
    if len(subscription_id) <= 4:
        return "****"
    return f"****{subscription_id[-4:]}"


def _find_or_create_subscription_member(club, subscription_id, email):
    """Resolve the ClubMember for a subscription: by subscription id, then email, then create.

    Creation needs an email (we can't make a usable member without one). Returns None when no
    member exists and none can be created."""
    member = ClubMember.objects.filter(club=club, paypal_subscription_id=subscription_id, is_deleted=False).first()
    if member:
        return member
    if email:
        member = ClubMember.objects.filter(club=club, email__iexact=email, is_deleted=False).first()
        if member:
            return member
        member = ClubMember.objects.create(club=club, email=email)
        # user stays null: a webhook has no acting user, same as the ledger entry below
        ClubHistory.objects.create(
            club=club,
            action=f"Added member {member} from PayPal subscription {_mask_subscription_id(subscription_id)}",
            applies_to="MEMBERS",
        )
        return member
    return None


def _book_paypal_subscription_payment(club, member, subscription):
    """Book a PayPal subscription charge into the club ledger. Idempotent; returns the row or None.

    A subscription renewal is real money into the club, exactly like the manual renewal button
    (ClubMembershipRenewView) and a paid dues invoice (Invoice.sync_club_money) -- without this the
    treasurer's ledger and the "Membership dues" total silently miss every auto-renewal.

    We book what PayPal actually charged (``billing_info.last_payment.amount.value``) rather than the
    club's ``membership_annual_fee``: the two drift whenever a club changes its fee after members have
    already subscribed, and the ledger must reflect the cash that really moved.

    Idempotency matters more here than anywhere else in this flow: PayPal retries webhooks, sends
    several BILLING.SUBSCRIPTION events per cycle, and a BILLING.SUBSCRIPTION.UPDATED can advance the
    billing date with no new payment at all. So the booking is keyed on (club, membership category,
    payment date, subscription id) -- the same charge can arrive any number of times and books once.
    That key is also why this runs *outside* the membership-date guard in the caller: a payment whose
    cycle dates didn't move (e.g. the first PAYMENT.SALE.COMPLETED after ACTIVATED already set the
    dates) still has to reach the ledger.
    """
    subscription_id = subscription.get("id") or ""
    if not subscription_id:
        return None
    last_payment = (subscription.get("billing_info") or {}).get("last_payment") or {}
    raw_amount = (last_payment.get("amount") or {}).get("value")
    if raw_amount is None:
        # ACTIVATED can arrive before the first charge posts; the next event carries the payment.
        return None
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning(
            "PayPal subscription %s: unparseable last_payment amount %r; not booking",
            _mask_subscription_id(subscription_id),
            raw_amount,
        )
        return None
    if amount <= 0:
        return None
    payment_date = _parse_paypal_datetime_date(last_payment.get("time")) or timezone.now().date()
    if ClubMoney.objects.filter(
        club=club,
        category=ClubMoney.CATEGORY_MEMBERSHIP,
        date=payment_date,
        description__contains=subscription_id,
    ).exists():
        return None
    entry = ClubMoney.objects.create(
        club=club,
        # created_by stays null: a webhook has no acting user (the field is nullable for this case).
        date=payment_date,
        amount=amount,
        description=f"PayPal subscription renewal for {member} ({subscription_id})",
        category=ClubMoney.CATEGORY_MEMBERSHIP,
    )
    logger.info(
        "PayPal subscription %s: booked %s to club %s ledger for member %s",
        _mask_subscription_id(subscription_id),
        amount,
        club.pk,
        member.pk,
    )
    return entry


def _apply_paypal_subscription_event(club, subscription):
    """Apply an authoritative PayPal subscription resource (re-fetched from PayPal) to its member.

    Acts on the subscription's real status, not on which webhook triggered us:
      - CANCELLED/SUSPENDED/EXPIRED -> clear paypal_subscription_id (they no longer auto-renew).
        The paid-through date is left intact, so they keep the time already paid for and expiry
        reminders resume on their own once the subscription id is gone.
      - ACTIVE -> book the charge into the club ledger (see
        :func:`_book_paypal_subscription_payment`), record the subscription, and extend membership to
        next_billing_time. A renewal confirmation email goes out only when the membership is newly
        linked or actually advances, so PayPal's webhook retries (and its several
        BILLING.SUBSCRIPTION events per cycle) don't spam the member.
      - anything else (APPROVAL_PENDING / APPROVED -- created but not yet paid) -> do nothing, so
        an abandoned, never-paid subscription can't grant membership.
    ClubMember.save() reschedules expiry reminders."""
    subscription_id = subscription.get("id") or ""
    if not subscription_id:
        return
    status = (subscription.get("status") or "").upper()
    subscriber = subscription.get("subscriber") or {}
    email = (subscriber.get("email_address") or "").strip()

    if status in ("CANCELLED", "SUSPENDED", "EXPIRED"):
        member = ClubMember.objects.filter(club=club, paypal_subscription_id=subscription_id, is_deleted=False).first()
        if not member and email:
            member = ClubMember.objects.filter(club=club, email__iexact=email, is_deleted=False).first()
        if member and member.paypal_subscription_id:
            member.paypal_subscription_id = ""
            member.save(update_fields=["paypal_subscription_id"])
            ClubHistory.objects.create(
                club=club,
                action=(
                    f"{member} stopped auto-renewing (PayPal subscription "
                    f"{_mask_subscription_id(subscription_id)} {status.lower()}); paid-through date unchanged"
                ),
                applies_to="MEMBERSHIP",
            )
            logger.info(
                "PayPal subscription %s %s: cleared for member %s",
                _mask_subscription_id(subscription_id),
                status,
                member.pk,
            )
        return

    if status != "ACTIVE":
        logger.info(
            "PayPal subscription %s status %s: nothing to apply", _mask_subscription_id(subscription_id), status
        )
        return

    member = _find_or_create_subscription_member(club, subscription_id, email)
    if not member:
        logger.warning(
            "PayPal subscription %s (%s): no member and no email to create one in club %s",
            _mask_subscription_id(subscription_id),
            status,
            club.pk,
        )
        return
    # Book the cash first, and outside the change guard below: the ledger entry is keyed on the
    # payment itself (idempotent), so it must not be skipped just because this delivery didn't move
    # the membership dates.
    _book_paypal_subscription_payment(club, member, subscription)
    next_date = _parse_paypal_datetime_date((subscription.get("billing_info") or {}).get("next_billing_time"))
    newly_linked = not member.paypal_subscription_id
    advanced = bool(
        next_date and (not member.membership_expiration_date or next_date > member.membership_expiration_date)
    )
    if not newly_linked and not advanced:
        # Duplicate / out-of-order delivery for a cycle we already recorded -- nothing changed.
        logger.info(
            "PayPal subscription %s: already current for member %s", _mask_subscription_id(subscription_id), member.pk
        )
        return
    old_expiration = member.membership_expiration_date
    member.paypal_subscription_id = subscription_id
    member.membership_last_paid = timezone.now().date()
    if advanced:
        member.membership_expiration_date = next_date
    member.save()
    old_exp_str = old_expiration.strftime("%-m/%-d/%Y") if old_expiration else "none"
    new_exp_str = (
        member.membership_expiration_date.strftime("%-m/%-d/%Y") if member.membership_expiration_date else "unknown"
    )
    ClubHistory.objects.create(
        club=club,
        action=(
            f"{member} renewed via PayPal subscription {_mask_subscription_id(subscription_id)}; "
            f"expiration {old_exp_str} → {new_exp_str}"
        ),
        applies_to="MEMBERSHIP",
    )
    maybe_send_membership_renewal_confirmation(member)
    logger.info(
        "PayPal subscription %s (%s): renewed member %s through %s",
        _mask_subscription_id(subscription_id),
        status,
        member.pk,
        member.membership_expiration_date,
    )


@method_decorator(csrf_exempt, name="dispatch")
class PayPalSubscriptionWebhookView(PayPalAPIMixin, View):
    """Club membership subscription webhooks at /clubs/paypal/webhook.

    One shared URL serves every club. Because the incoming request doesn't say which club it's
    for, the club is identified by whichever configured club webhook ID verifies the PayPal
    signature: existing members carry the subscription id (a one-club fast path), and a brand-new
    subscription is verified against each candidate club's webhook id until one succeeds. Only after
    the signature verifies do we act.

    We don't trust the webhook body's state: each recurring charge arrives as PAYMENT.SALE.COMPLETED
    (which carries no subscriber email or next_billing_time), and PayPal sends several
    BILLING.SUBSCRIPTION events per subscription. So once verified, we re-fetch the authoritative
    subscription from PayPal and apply *that* (see ``_apply_paypal_subscription_event``).
    """

    # Subscription lifecycle events whose ``resource.id`` is the subscription id. CREATED
    # (approval-pending, unpaid) and PAYMENT.FAILED are deliberately excluded: re-fetching on those
    # would find a not-yet-active / unchanged subscription, and repeated failures end as
    # SUSPENDED/CANCELLED, which we do handle.
    _HANDLED_SUBSCRIPTION_EVENTS = (
        "BILLING.SUBSCRIPTION.ACTIVATED",
        "BILLING.SUBSCRIPTION.UPDATED",
        "BILLING.SUBSCRIPTION.CANCELLED",
        "BILLING.SUBSCRIPTION.SUSPENDED",
        "BILLING.SUBSCRIPTION.EXPIRED",
    )

    _WEBHOOK_HEADER_NAMES = {
        "auth_algo": "PayPal-Auth-Algo",
        "cert_url": "PayPal-Cert-Url",
        "transmission_id": "PayPal-Transmission-Id",
        "transmission_sig": "PayPal-Transmission-Sig",
        "transmission_time": "PayPal-Transmission-Time",
    }

    def _get_access_token(self):
        """Cache the token per credential-set for this request so the multi-club verify loop
        doesn't re-authenticate once per club (all site-PayPal clubs share one token)."""
        creds = getattr(self, "club_paypal_credentials", None)
        key = creds or "__site__"
        cache = getattr(self, "_sub_token_cache", None)
        if cache is None:
            cache = self._sub_token_cache = {}
        if key not in cache:
            cache[key] = super()._get_access_token()
        return cache[key]

    def _subscription_id_for_event(self, event_type, resource):
        """The subscription id this event concerns, or "" when we don't handle the event.

        Recurring subscription *payments* arrive as PAYMENT.SALE.COMPLETED carrying
        ``billing_agreement_id`` (the subscription id); a PAYMENT.SALE.COMPLETED without one is a
        non-subscription sale and is ignored. Subscription lifecycle changes carry the id directly.
        """
        if event_type == "PAYMENT.SALE.COMPLETED":
            return resource.get("billing_agreement_id") or ""
        if event_type in self._HANDLED_SUBSCRIPTION_EVENTS:
            return resource.get("id") or ""
        return ""

    def _webhook_headers(self, request):
        def hdr(name):
            return request.META.get(f"HTTP_{name.upper().replace('-', '_')}", request.headers.get(name))

        return {key: hdr(name) for key, name in self._WEBHOOK_HEADER_NAMES.items()}

    def _verify_for_club(self, club, headers, event):
        """Ask PayPal to verify this transmission against ``club``'s webhook id + credentials."""
        # club.paypal_credentials is None for site-PayPal clubs, which _get_access_token reads as
        # "use the site keys" -- the same account that owns those clubs' webhooks.
        self.club_paypal_credentials = club.paypal_credentials
        try:
            access_token = self._get_access_token()
        except Exception:
            logger.exception("PayPal subscription webhook: token fetch failed for club %s", club.pk)
            return False
        verify_payload = {
            "auth_algo": headers["auth_algo"],
            "cert_url": headers["cert_url"],
            "transmission_id": headers["transmission_id"],
            "transmission_sig": headers["transmission_sig"],
            "transmission_time": headers["transmission_time"],
            "webhook_id": club.paypal_webhook_id,
            "webhook_event": event,
        }
        try:
            resp = requests.post(
                f"{settings.PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json=verify_payload,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("verification_status") == "SUCCESS"
        except (requests.RequestException, ValueError):
            logger.exception("PayPal subscription webhook: signature verify errored for club %s", club.pk)
            return False

    def _candidate_clubs(self):
        """Active clubs that both have a webhook id and could actually verify one.

        Mirrors ``Club.supports_paypal_subscriptions`` at the DB level: only site-PayPal clubs and
        own-credential (non-OAuth) clubs can produce a token that owns their webhook, so OAuth-only
        clubs are skipped (they'd always fail verification and just cost a round-trip)."""
        return (
            Club.objects.filter(active=True)
            .exclude(paypal_webhook_id="")
            .filter(
                Q(use_site_paypal_account=True)
                | (Q(allow_non_oauth_paypal=True) & ~Q(paypal_client_id="") & ~Q(paypal_secret=""))
            )
        )

    def _identify_and_verify_club(self, subscription_id, headers, event):
        member = (
            ClubMember.objects.filter(paypal_subscription_id=subscription_id, is_deleted=False)
            .select_related("club")
            .first()
        )
        if member and member.club and member.club.paypal_webhook_id:
            return member.club if self._verify_for_club(member.club, headers, event) else None
        for club in self._candidate_clubs():
            if self._verify_for_club(club, headers, event):
                return club
        return None

    def post(self, request, *args, **kwargs):
        try:
            event = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return HttpResponseBadRequest("invalid json")
        if not isinstance(event, dict):
            return HttpResponseBadRequest("invalid json")

        event_type = event.get("event_type", "") or ""
        resource = event.get("resource", {})
        if not isinstance(resource, dict):
            resource = {}
        subscription_id = self._subscription_id_for_event(event_type, resource)
        # Ack events we don't handle (other event types, one-off sales, missing id) so PayPal stops
        # retrying them.
        if not subscription_id:
            return JsonResponse({"status": "ignored"})

        headers = self._webhook_headers(request)
        if not all(headers.values()):
            logger.warning("PayPal subscription webhook: missing verification headers")
            return HttpResponseBadRequest("missing verification headers")

        club = self._identify_and_verify_club(subscription_id, headers, event)
        if not club:
            logger.warning(
                "PayPal subscription webhook: no club verified for subscription %s",
                _mask_subscription_id(subscription_id),
            )
            return HttpResponseBadRequest("webhook verification failed")

        # Re-fetch authoritative subscription state (the triggering event body may be a bare sale
        # with no subscriber/next_billing_time, or a stale/out-of-order lifecycle event).
        self.club_paypal_credentials = club.paypal_credentials
        try:
            subscription = self.get_from_paypal(f"v1/billing/subscriptions/{subscription_id}", include_bn_code=False)
        except PayPalRequestError:
            logger.exception(
                "PayPal subscription webhook: failed to fetch subscription %s", _mask_subscription_id(subscription_id)
            )
            # 500 -> PayPal retries later, so a transient fetch failure doesn't drop the renewal.
            return HttpResponse(status=500)

        _apply_paypal_subscription_event(club, subscription)
        return JsonResponse({"status": "ok"})


class SquareWebhookView(SquareAPIMixin, View):
    """Handle Square webhook events for payment notifications
    Implements webhook signature verification using HMAC-SHA256
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def verify_signature(self, request, raw_body, signature):
        """Verify Square webhook signature using Square SDK
        Square signs webhooks with: base64(HMAC-SHA256(signature_key, notification_url + request_body))
        Returns True if signature is valid, False otherwise
        """
        if not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
            logger.warning("SQUARE_WEBHOOK_SIGNATURE_KEY not configured - skipping signature verification")
            if settings.DEBUG:
                return True  # Allow webhook if signature key not configured
            else:
                return False  # Reject webhook if signature key not configured in production

        try:
            from square.utils.webhooks_helper import verify_signature as square_verify_signature

            # Prefer an explicit configured URL if provided (useful behind proxies)
            notification_url = getattr(settings, "SQUARE_WEBHOOK_PUBLIC_URL", "").strip()
            if not notification_url:
                # Fallback: absolute URL of this request (no query string per Square docs)
                notification_url = request.build_absolute_uri(request.path)

            # Ensure raw_body is a string as expected by Square SDK
            body_str = raw_body if isinstance(raw_body, str) else raw_body.decode("utf-8")

            # Use Square SDK's signature verification
            return square_verify_signature(
                request_body=body_str,
                signature_header=signature,
                signature_key=settings.SQUARE_WEBHOOK_SIGNATURE_KEY,
                notification_url=notification_url,
            )
        except Exception as e:
            logger.exception("Error verifying Square webhook signature: %s", e)
            return False

    def post(self, request, *args, **kwargs):
        # In production, require SQUARE_WEBHOOK_SIGNATURE_KEY if Square is configured
        if not settings.DEBUG and not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
            if settings.SQUARE_APPLICATION_ID or settings.SQUARE_CLIENT_SECRET:
                msg = "SQUARE_WEBHOOK_SIGNATURE_KEY must be set in production when Square is configured"
                raise ImproperlyConfigured(msg)

        # Read raw body
        try:
            raw_body = request.body.decode("utf-8")
            event = json.loads(raw_body)
        except Exception as exc:
            logger.exception("Invalid JSON in Square webhook: %s", exc)
            return HttpResponseBadRequest("invalid json")

        # Verify webhook signature if configured
        if settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
            signature = request.headers.get("X-Square-Hmacsha256-Signature", "")
            if not signature:
                logger.error("Square webhook missing signature header")
                return HttpResponseForbidden("missing signature")

            if not self.verify_signature(request, raw_body, signature):
                logger.error("Square webhook signature verification failed")
                return HttpResponseForbidden("invalid signature")

        event_type = event.get("type")
        logger.info("Received Square webhook: %s", event_type)

        if event_type == "payment.updated":
            # Payment completed or updated
            data = event.get("data", {})
            payment_object = data.get("object", {})
            payment = payment_object.get("payment", {})

            payment_status = payment.get("status")
            payment_id = payment.get("id")
            order_id = payment.get("order_id")
            reference_id = None
            # Handle COMPLETED status - create payment record and mark invoice paid
            if payment_status == "COMPLETED":
                merchant_id = event.get("merchant_id", "")
                seller = SquareSeller.objects.filter(square_merchant_id=merchant_id).first()
                if not seller:
                    logger.warning("Square webhook: SquareSeller not found for merchant_id: %s", merchant_id)
                elif seller.square_merchant_id:
                    client = seller.get_square_client()
                    if client:
                        try:
                            order_response = client.orders.get(order_id=order_id)
                            # Square SDK returns response objects with attributes
                            if hasattr(order_response, "order") and order_response.order:
                                reference_id = getattr(order_response.order, "reference_id", None)
                            if not reference_id:
                                logger.error("reference id not found for Square order %s", order_id)
                                logger.error(order_response)
                        except Exception as e:
                            logger.exception("Error retrieving Square order %s: %s", order_id, e)
                    else:
                        logger.error("Could not get Square client for user %s", seller.user.pk)

                # Only proceed if we have a reference_id to look up the invoice. reference_id is our
                # invoice pk as a string; guard against any non-numeric value so a stray payment
                # can't raise (Invoice.pk is an int) and 500 the webhook.
                if reference_id:
                    try:
                        invoice = Invoice.objects.filter(pk=int(reference_id)).first()
                    except (TypeError, ValueError):
                        invoice = None
                        logger.warning("Square webhook: non-numeric reference_id: %s", reference_id)
                    if invoice:
                        amount_money = payment.get("amount_money", {})
                        amount_value = Decimal(amount_money.get("amount", 0)) / 100
                        currency = amount_money.get("currency", "USD")
                        receipt_number = payment.get("receipt_number", "")

                        payment_record, created = InvoicePayment.objects.get_or_create(
                            invoice=invoice,
                            external_id=payment_id,
                            defaults={
                                "amount": amount_value,
                                "amount_available_to_refund": amount_value,
                                "currency": currency,
                                "payment_method": "Square",
                                "receipt_number": receipt_number,
                            },
                        )
                        # If the payment already existed, never restore refundability that refunds
                        # have consumed. Square fires payment.updated for many lifecycle changes, so a
                        # fully-refunded payment (amount_available_to_refund == 0) must not become
                        # refundable again -- otherwise a second full refund could be issued.
                        # amount_available_to_refund is initialized once, when the record is created
                        # (in the get_or_create defaults above).
                        if not created:
                            # If the payment amount itself legitimately changed, move the refundable
                            # balance by the delta so accounting stays correct without resetting it.
                            if amount_value != payment_record.amount:
                                payment_record.amount_available_to_refund += amount_value - payment_record.amount
                                payment_record.amount = amount_value
                            # Update receipt_number if it wasn't set before
                            if receipt_number and not payment_record.receipt_number:
                                payment_record.receipt_number = receipt_number
                            payment_record.save()
                        if invoice.auctiontos_user and invoice.auction:
                            try:
                                action = f"Payment via Square for bidder {invoice.auctiontos_user.bidder_number} in the amount of {amount_value} {currency}"
                                invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
                            except Exception:
                                logger.exception("create_history failed for Square payment on invoice %s", invoice.pk)
                        # Use the rounded balance so a rounded-down charge (the amount we billed)
                        # still settles the invoice.
                        if invoice.rounded_net_after_payments >= 0:
                            if not invoice.renewal_needed:
                                try:
                                    _ensure_invoice_renewal_state(invoice)
                                except Exception:
                                    logger.exception(
                                        "Failed to ensure renewal state for invoice %s before Square PAID",
                                        invoice.pk,
                                    )
                            invoice.status = "PAID"
                            invoice.save()
                            try:
                                _process_invoice_membership_renewal(
                                    invoice, payment_method="Square", external_id=payment_id
                                )
                            except Exception:
                                logger.exception(
                                    "membership renewal failed after Square payment on invoice %s", invoice.pk
                                )

                            # Send websocket notification for payment completion
                            try:
                                channel_layer = channels.layers.get_channel_layer()
                                async_to_sync(channel_layer.group_send)(
                                    f"invoice_{invoice.pk}",
                                    {
                                        "type": "invoice_status",
                                        "message": "paid",
                                    },
                                )
                                if invoice.auction:
                                    async_to_sync(channel_layer.group_send)(
                                        f"auctions_{invoice.auction.pk}",
                                        {
                                            "type": "invoice_paid",
                                            "pk": invoice.pk,
                                        },
                                    )
                            except Exception:
                                logger.exception(
                                    "Failed to send websocket notification for Square payment on invoice %s",
                                    invoice.pk,
                                )
                        logger.info("Square payment completed for invoice %s", invoice.pk)
                    else:
                        logger.warning("Square webhook: Invoice not found for reference_id: %s", reference_id)

        elif event_type == "refund.updated":
            # Refund processed
            data = event.get("data", {})
            refund_object = data.get("object", {})
            refund = refund_object.get("refund", {})
            refund_id = refund.get("id", {})

            if refund.get("status") == "COMPLETED":
                payment_id = refund.get("payment_id")
                # Find the original payment and mark refund
                payment_record = InvoicePayment.objects.filter(external_id=payment_id).first()
                if payment_record and refund_id:
                    refund_amount = Decimal(refund.get("amount_money", {}).get("amount", 0)) / 100

                    # Square redelivers/re-fires events for the same refund. Capture the prior refund
                    # amount for this refund id before update_or_create overwrites it, then move the
                    # refundable balance only by the delta so a duplicate delivery is a no-op and an
                    # amount change adjusts correctly.
                    existing_refund = InvoicePayment.objects.filter(external_id=refund_id).first()
                    previous_refund_abs = abs(existing_refund.amount) if existing_refund else Decimal("0.00")

                    refund_payment, created = InvoicePayment.objects.update_or_create(
                        external_id=refund_id,
                        defaults={
                            "invoice": payment_record.invoice,
                            "amount": -abs(refund_amount),  # Ensure negative for refund
                            "currency": payment_record.currency,
                            "payment_method": "Square Refund",
                            "memo": refund.get("reason", "")[:500],
                        },
                    )

                    refund_delta = refund_amount - previous_refund_abs
                    if refund_delta:
                        payment_record.amount_available_to_refund -= refund_delta
                        payment_record.save()

                    payment_record.invoice.recalculate()
                    if created:
                        action = f"Refund via Square for bidder {payment_record.invoice.auctiontos_user.bidder_number} in the amount of {refund_amount} {payment_record.currency}"
                        payment_record.invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
                    logger.info("Square refund completed for payment %s", payment_id)

        elif event_type == "oauth.authorization.revoked":
            # Merchant revoked OAuth authorization - delete SquareSeller instance
            merchant_id = event.get("merchant_id")
            if merchant_id:
                square_seller = SquareSeller.objects.filter(square_merchant_id=merchant_id).first()
                if square_seller:
                    square_seller.delete()
        return HttpResponse(status=200)


class QuickCheckout(AuctionViewMixin, TemplateView):
    """Enter a bidder number or name and mark their invoice as paid
    For https://github.com/iragm/fishauctions/issues/292"""

    template_name = "auctions/quick_checkout.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        # The camera scanner (scan a bidder number or member card to pull up the invoice) is meant
        # for small-screen devices and the native app -- not desktop. The server can't see the
        # browser viewport, so we always ship the script and let a Bootstrap responsive class hide
        # the camera on larger screens (see the d-md-none wrapper in the template). The app's WebView
        # reports a phone-sized viewport, so it's covered by the same small-screen rule.
        context["show_camera_scanner"] = True
        return context


class QuickCheckoutHTMX(AuctionViewMixin, PayPalAPIMixin, SquareAPIMixin, TemplateView):
    """For use with HTMX calls on QuickCheckout"""

    template_name = "auctions/quick_checkout_htmx.html"

    def _normalize_scanned_term(self, term):
        """Translate a scanned barcode into something the checkout search can match.

        The checkout search matches a bidder number directly but knows nothing about paddle
        barcodes (11111 + bidder number) or membership card numbers, so map those to the
        bidder number here. Returns the term unchanged if it isn't a recognizable barcode."""
        term = (term or "").strip()
        if not term:
            return term
        # Paddle barcode: 11111 followed by the bidder number
        if term.startswith("11111") and len(term) > 5 and term[5:].isdigit():
            return term[5:]
        # Membership card: a bare number matching a ClubMember in this auction's club
        if term.isdigit() and self.auction.club_id:
            member = ClubMember.objects.filter(
                club=self.auction.club, membership_number=int(term), is_deleted=False
            ).first()
            if member and member.bidder_number:
                return member.bidder_number
        return term

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        qs = AuctionTOS.objects.filter(auction=self.auction)
        search_term = kwargs.get("filter")
        # The camera scanner posts ?barcode=1 so paddle/membership-card barcodes get translated to a
        # bidder number; typed searches are left alone so a numeric name search still behaves normally.
        if self.request.GET.get("barcode"):
            search_term = self._normalize_scanned_term(search_term)
        filtered_qs = AuctionTOSFilter.generic(self, qs, search_term)
        invoice = None
        if filtered_qs.count() > 1:
            context["multiple_tos"] = True
        else:
            context["multiple_tos"] = False
            context["tos"] = filtered_qs.first()
            if context["tos"]:
                invoice = context["tos"].invoice
                context["invoice"] = invoice
                _ensure_invoice_renewal_state(invoice)
            if invoice:
                # Generate PayPal QR code if available.
                # The in-person QR flow relies on PayPal webhooks (CHECKOUT.ORDER.APPROVED /
                # CHECKOUT.CAPTURE.COMPLETED) to update the cashier screen once the payer approves
                # on their phone. Clubs using their own (non-OAuth) credentials have no webhook
                # wired up, so skip the QR for them -- they can still take Square/cash here.
                if (
                    invoice.show_paypal_button
                    and not invoice.reason_for_payment_not_available
                    and not invoice.paypal_credentials
                ):
                    try:
                        context["paypal_qr_code_link"] = self.create_order(invoice)
                    except Exception:
                        logger.warning("PayPal order creation failed for invoice %s", invoice.pk, exc_info=True)
                # Generate Square QR code if available
                if invoice.show_square_button and not invoice.reason_for_payment_not_available:
                    payment_url, error_message = self.create_payment_link(invoice)
                    if payment_url:
                        context["square_qr_code_link"] = payment_url
                    elif error_message:
                        # Log the error but don't show QR code
                        logger.warning(
                            "Square payment link creation failed for invoice %s: %s", invoice.pk, error_message
                        )
        return context
