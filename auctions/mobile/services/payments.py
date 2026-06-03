import logging
import uuid
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)


class PaymentService:
    """Mobile Square Tap-to-Pay infrastructure.

    Flow
    ----
    1. Mobile calls ``create_mobile_payment(invoice_pk, user)`` to receive the
       parameters needed to initialise the Square In-Person SDK on-device.
    2. The SDK collects the card tap and returns a ``source_id`` (nonce).
    3. Mobile calls ``confirm_mobile_payment(invoice_pk, source_id,
       idempotency_key, user)``; this service charges the card via the Square
       Payments API and records the result on the invoice.

    Square Terminal/In-Person SDK integration lives entirely in the Flutter
    app.  This service only handles server-side payment creation and
    confirmation.
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
    def create_mobile_payment(invoice_pk: int, user) -> dict:
        """Validate an invoice and return payment context for the mobile SDK.

        The returned dict contains everything the Flutter client needs to
        initialise ``SquareInPersonSDK`` and start a Tap-to-Pay transaction.

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
            "idempotency_key": str(uuid.uuid4()),
            "square_application_id": settings.SQUARE_APPLICATION_ID,
            "square_environment": settings.SQUARE_ENVIRONMENT,
        }

    @staticmethod
    def confirm_mobile_payment(invoice_pk: int, source_id: str, idempotency_key: str, user) -> dict:
        """Charge the card nonce returned by the mobile SDK and record the payment.

        Returns a dict with ``payment_id``, ``status``, and ``receipt_number`` from Square.

        Raises
        ------
        PermissionError  — user is not the invoice buyer.
        ValueError       — invoice already paid, Square error, zero amount.
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

        result = client.payments.create_payment(
            body={
                "source_id": source_id,
                "idempotency_key": idempotency_key,
                "amount_money": {
                    "amount": amount_cents,
                    "currency": invoice.currency,
                },
                "location_id": location_id,
                "reference_id": str(invoice_pk),
            }
        )

        if result.is_success():
            from auctions.views import _ensure_invoice_renewal_state, _process_invoice_membership_renewal

            sq_payment = result.body["payment"]
            payment_id = sq_payment.get("id", "")
            receipt_number = (sq_payment.get("receipt_number") or "")[:10]
            InvoicePayment.objects.create(
                invoice=invoice,
                payment_method="Square",
                amount=amount_due,
                amount_available_to_refund=amount_due,
                currency=invoice.currency,
                external_id=payment_id,
                receipt_number=receipt_number or None,
            )
            invoice.status = "PAID"
            invoice.save(update_fields=["status"])
            try:
                _ensure_invoice_renewal_state(invoice)
            except Exception:
                logger.exception("Failed to ensure renewal state for invoice %s (mobile Square)", invoice_pk)
            try:
                _process_invoice_membership_renewal(invoice, payment_method="Square", external_id=payment_id)
            except Exception:
                logger.exception("Failed to process membership renewal for invoice %s (mobile Square)", invoice_pk)
            logger.info("Mobile Square payment confirmed for invoice %s: %s", invoice_pk, payment_id)
            return {
                "payment_id": payment_id,
                "status": sq_payment.get("status"),
                "receipt_number": receipt_number or None,
            }

        errors = result.errors or []
        detail = "; ".join(e.get("detail", str(e)) for e in errors)
        logger.error("Square payment failed for invoice %s: %s", invoice_pk, errors)
        msg = f"Square payment failed: {detail}"
        raise ValueError(msg)
