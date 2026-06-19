import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


class PaymentService:
    """Mobile Square Tap-to-Pay infrastructure.

    Flow
    ----
    1. Mobile calls ``create_mobile_payment(invoice_pk, user)`` to receive the
       parameters (including the seller's access token + location) needed to
       authorize the Square Mobile Payments SDK on-device.
    2. The SDK collects the card tap and **charges the card on-device**,
       returning a completed Square ``payment_id`` — there is no nonce, and the
       server never calls ``payments.create``.
    3. Mobile calls ``confirm_mobile_payment(invoice_pk, payment_id,
       idempotency_key, user)``; this service re-fetches the payment from Square
       (GetPayment), **verifies** it (status/amount/currency/location/reference),
       and records the result on the invoice.

    Square Mobile Payments SDK (Tap to Pay) integration lives entirely in the
    Flutter app.  This service only handles server-side payment context creation
    and verification of the on-device charge.
    """

    @staticmethod
    def _get_seller_for_invoice(invoice):
        """Return the SquareSeller responsible for this invoice, or None."""
        from auctions.models import SquareSeller

        if invoice.club:
            return invoice.club.effective_square_seller
        if invoice.auction and invoice.auction.created_by:
            return SquareSeller.objects.filter(user=invoice.auction.created_by).first()
        return None

    @staticmethod
    def _square_error_detail(exc) -> str:
        """Pull a human-readable message out of a Square SDK error.

        The new SDK raises ``square.core.api_error.ApiError`` with the API payload on ``.body``;
        this mirrors how ``SquareSeller.create_payment_link`` surfaces the same errors on the web.
        """
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            errors = body.get("errors") or []
            if errors and isinstance(errors, list):
                return errors[0].get("detail") or errors[0].get("code") or str(exc)
        return str(exc)

    @staticmethod
    def create_mobile_payment(invoice_pk: int, user) -> dict:
        """Validate an invoice and return payment context for the mobile SDK.

        The returned dict contains everything the Flutter client needs to
        authorize the Square Mobile Payments SDK and start a Tap-to-Pay charge.

        Raises
        ------
        PermissionError  — user is not the invoice buyer.
        ValueError       — invoice already paid, Square not configured,
                           amount is zero/negative.
        LookupError      — invoice not found.
        """
        from auctions.models import Invoice

        try:
            invoice = Invoice.objects.select_related(
                "auction", "auction__created_by", "club", "auctiontos_user__user"
            ).get(pk=invoice_pk)
        except Invoice.DoesNotExist:
            msg = f"Invoice {invoice_pk} not found"
            raise LookupError(msg)

        # Access check: buyer or auctiontos user must match
        invoice_user = (invoice.auctiontos_user.user if invoice.auctiontos_user else None) or invoice.buyer
        if invoice_user and invoice_user != user:
            msg = "You do not have access to this invoice"
            raise PermissionError(msg)

        if invoice.status == "PAID":
            msg = "Invoice is already paid"
            raise ValueError(msg)

        seller = PaymentService._get_seller_for_invoice(invoice)
        if not seller:
            msg = "Square payments are not configured for this invoice"
            raise ValueError(msg)

        if not seller.get_valid_access_token():
            msg = "Square account token is invalid; the seller must reconnect Square"
            raise ValueError(msg)

        location_id = seller.get_location_id()
        if not location_id:
            msg = "No active Square location found for this seller"
            raise ValueError(msg)

        amount_due = Decimal("0.00") - Decimal(invoice.net_after_payments)
        if amount_due <= 0:
            msg = "No amount is due on this invoice"
            raise ValueError(msg)

        return {
            "invoice_pk": invoice_pk,
            "amount": str(amount_due),
            "currency": invoice.currency,
            "location_id": location_id,
            # The Mobile Payments SDK authorizes on-device with authorize(accessToken, locationId),
            # so this ships the seller's OAuth access token to the device by design — the SDK
            # requires it. Prefer the shortest-lived token the seller's Square OAuth allows.
            "access_token": seller.get_valid_access_token(),
            "idempotency_key": str(uuid.uuid4()),
            "square_environment": settings.SQUARE_ENVIRONMENT,
        }

    @staticmethod
    def confirm_mobile_payment(invoice_pk: int, payment_id: str, idempotency_key: str, user) -> dict:
        """Verify an on-device Tap to Pay charge and record the payment.

        The Mobile Payments SDK charges the card on-device and returns a completed
        Square ``payment_id``; this service does NOT charge anything. It re-fetches
        the payment from Square (GetPayment) and verifies status/amount/currency/
        location/reference before recording — because the client reports the id,
        nothing is trusted until verified against Square. ``idempotency_key`` is
        accepted for contract compatibility but no longer used to charge.

        Returns a dict with ``payment_id``, ``status``, and ``receipt_number`` from Square.

        Raises
        ------
        PermissionError  — user is not the invoice buyer.
        ValueError       — invoice already paid, Square error, zero amount, or the
                           fetched payment fails any verification check.
        LookupError      — invoice not found.
        """
        from auctions.models import Invoice, InvoicePayment

        try:
            invoice = Invoice.objects.select_related(
                "auction", "auction__created_by", "club", "auctiontos_user__user"
            ).get(pk=invoice_pk)
        except Invoice.DoesNotExist:
            msg = f"Invoice {invoice_pk} not found"
            raise LookupError(msg)

        invoice_user = (invoice.auctiontos_user.user if invoice.auctiontos_user else None) or invoice.buyer
        if invoice_user and invoice_user != user:
            msg = "You do not have access to this invoice"
            raise PermissionError(msg)

        if invoice.status == "PAID":
            msg = "Invoice is already paid"
            raise ValueError(msg)

        seller = PaymentService._get_seller_for_invoice(invoice)
        if not seller:
            msg = "Square payments are not configured for this invoice"
            raise ValueError(msg)

        client = seller.get_square_client()
        if not client:
            msg = "Failed to initialise Square client"
            raise ValueError(msg)

        location_id = seller.get_location_id()
        if not location_id:
            msg = "No active Square location found"
            raise ValueError(msg)

        amount_due = Decimal("0.00") - Decimal(invoice.net_after_payments)
        if amount_due <= 0:
            msg = "No amount is due on this invoice"
            raise ValueError(msg)

        amount_cents = int(amount_due * 100)

        # squareup 44.x API: GetPayment takes named kwargs, returns a typed GetPaymentResponse
        # (with .payment and .errors), and raises on failure — not result.is_success()/.body, which
        # belong to the legacy SDK. We do NOT charge here: the card was already charged on-device by
        # the Mobile Payments SDK, so we only re-fetch the completed payment to verify it.
        try:
            result = client.payments.get(payment_id=payment_id)
        except Exception as exc:
            detail = PaymentService._square_error_detail(exc)
            logger.error("Square get payment failed for invoice %s: %s", invoice_pk, detail)
            msg = f"Square payment lookup failed: {detail}"
            raise ValueError(msg)

        # Some SDK paths report errors on the response instead of raising; handle both.
        if getattr(result, "errors", None):
            detail = "; ".join(getattr(e, "detail", None) or str(e) for e in result.errors)
            logger.error("Square get payment errors for invoice %s: %s", invoice_pk, result.errors)
            msg = f"Square payment lookup failed: {detail}"
            raise ValueError(msg)

        sq_payment = result.payment
        fetched_payment_id = getattr(sq_payment, "id", "") or ""
        receipt_number = (getattr(sq_payment, "receipt_number", "") or "")[:10]
        payment_status = getattr(sq_payment, "status", None)

        # SECURITY BOUNDARY: the card was charged on-device, so the client merely reports a
        # payment_id. Trust nothing until the payment we fetched from Square is confirmed to be a
        # successful charge, for the right amount/currency, taken on this seller's location, and
        # bound to this invoice. Any mismatch is a 400 and records nothing. The web flow only treats
        # "COMPLETED" as paid, so we match that (no auth-only acceptance).
        amount_money = getattr(sq_payment, "amount_money", None)
        sq_amount = getattr(amount_money, "amount", None)
        sq_currency = getattr(amount_money, "currency", None)
        sq_location_id = getattr(sq_payment, "location_id", None)
        sq_reference_id = getattr(sq_payment, "reference_id", None)
        expected_reference_id = f"invoice:{invoice_pk}"

        # NOTE: we accept ONLY "COMPLETED" — not the auth-only "APPROVED" — to match the web Square
        # webhook handler, which treats only COMPLETED as paid. This may change later: if Tap-to-Pay
        # charges can legitimately settle as "APPROVED" for this integration, widen the check here
        # (and keep it consistent with the web flow).
        if payment_status != "COMPLETED":
            msg = f"Square payment {payment_id} is not completed (status={payment_status})"
            raise ValueError(msg)
        if sq_amount != amount_cents or sq_currency != invoice.currency:
            msg = (
                f"Square payment {payment_id} amount mismatch: "
                f"got {sq_amount} {sq_currency}, expected {amount_cents} {invoice.currency}"
            )
            raise ValueError(msg)
        if sq_location_id != location_id:
            msg = f"Square payment {payment_id} location mismatch: got {sq_location_id}, expected {location_id}"
            raise ValueError(msg)
        if sq_reference_id != expected_reference_id:
            msg = f"Square payment {payment_id} reference mismatch: got {sq_reference_id}, expected {expected_reference_id}"
            raise ValueError(msg)

        # Verified — use Square's own id for the record, not whatever the client claimed.
        payment_id = fetched_payment_id or payment_id

        # Record idempotently. The Square webhook reconciles the same payment via get_or_create on
        # (invoice, external_id), so a double-tap or a webhook landing first must not double-record or
        # re-run renewal side effects. Lock the invoice so concurrent confirms serialize on this row.
        with transaction.atomic():
            locked_invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
            _, created = InvoicePayment.objects.get_or_create(
                invoice=locked_invoice,
                external_id=payment_id,
                defaults={
                    "payment_method": "Square",
                    "amount": amount_due,
                    "amount_available_to_refund": amount_due,
                    "currency": invoice.currency,
                    "receipt_number": receipt_number or None,
                },
            )
            if locked_invoice.status != "PAID":
                locked_invoice.status = "PAID"
                locked_invoice.save(update_fields=["status"])

        # Renewal hooks may not be idempotent, so only the request that actually recorded the payment
        # runs them — and only after commit, to avoid holding the row lock across email/Discord work.
        if created:
            from auctions.views import _ensure_invoice_renewal_state, _process_invoice_membership_renewal

            try:
                _ensure_invoice_renewal_state(invoice)
            except Exception:
                logger.exception("Failed to ensure renewal state for invoice %s (mobile Square)", invoice_pk)
            try:
                _process_invoice_membership_renewal(invoice, payment_method="Square", external_id=payment_id)
            except Exception:
                logger.exception("Failed to process membership renewal for invoice %s (mobile Square)", invoice_pk)

        logger.info("Mobile Square payment confirmed for invoice %s: %s (new=%s)", invoice_pk, payment_id, created)
        return {
            "payment_id": payment_id,
            "status": payment_status,
            "receipt_number": receipt_number or None,
        }
