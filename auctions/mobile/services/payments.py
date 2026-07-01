import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


class PaymentVerificationError(ValueError):
    """A Tap to Pay charge could not be verified against Square *after* the card was charged.

    Distinct from a plain ``ValueError`` (bad/early input) so the view can tell the operator the
    charge may have gone through — the Square webhook reconciles the same payment by reference_id,
    so refreshing the invoice usually shows it as paid — rather than a flat "invalid request".
    Subclasses ``ValueError`` so existing ``except ValueError`` handlers still catch it.
    """


class PaymentAlreadyChargedError(PaymentVerificationError):
    """The stable per-invoice idempotency key made Square return an EARLIER completed charge.

    The create idempotency key is stable per invoice, so if the balance changed after an earlier Tap
    to Pay charge, re-tapping makes the on-device SDK reuse that key and Square returns the original
    (already-recorded) payment instead of charging the new amount. No new money moves, so re-tapping
    is futile. Raised with a cashier-facing message naming the prior charge and what is still due, so
    the operator collects the remainder another way rather than tapping again. Subclasses
    ``PaymentVerificationError`` (hence ``ValueError``) so existing handlers still catch it.
    """

    def __init__(self, user_message):
        # This message is deliberately operator-facing (prior charge amount + remaining balance) and
        # carries no stack trace or system internals, so the view surfaces it to the cashier verbatim.
        # Exposing it via an explicit attribute — instead of str(exc) at the boundary — keeps that
        # intent in code and keeps the exception's stringification out of the HTTP response.
        super().__init__(user_message)
        self.user_message = user_message


class SquareReconnectRequired(ValueError):
    """The seller's Square account predates Tap to Pay (token missing PAYMENTS_WRITE_IN_PERSON).

    The seller must reconnect their Square account before any in-person charge will work; refreshing
    the existing token keeps the original (non-in-person) scopes. Raised *before* the device is ever
    handed a token, so the app can show a "Reconnect Square" prompt instead of failing mid-tap.
    Subclasses ``ValueError`` so existing ``except ValueError`` handlers still catch it as a fallback.
    """


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

    # Club permissions that may take a Tap to Pay payment. permission_admin is treated as a wildcard
    # inside check_club_permission, so it is covered implicitly.
    _CLUB_PAYMENT_PERMISSIONS = ("permission_money", "permission_manage_auctions")

    @staticmethod
    def _check_admin_access(invoice, user) -> bool:
        """Authorize the merchant operating Tap to Pay — never the buyer.

        Tap to Pay is run by the person collecting payment on a device authorized with the
        *seller's* Square account; buyers must not reach this flow (it would hand them the
        seller's OAuth token). Authorization mirrors the web admin checks:

        * Auction invoices — the auction creator, a superuser, or anyone with an ``is_admin``
          AuctionTOS on the auction (this is what covers a Square auction that has no club), via
          ``Auction.permission_check``.
        * Club auctions / membership invoices — a club admin, a money manager, or an auction
          manager (``permission_manage_auctions``) for the invoice's club. The club path is
          checked directly (not only via ``permission_check``) so it also applies when the
          auction is not ``manage_users_through_club``.

        Anything else is denied.
        """
        auction = invoice.auction
        if auction and auction.permission_check(user):
            return True
        club = invoice.club or (auction.club if auction else None)
        if club:
            from auctions.views import check_club_permission

            if any(check_club_permission(user, club, perm) for perm in PaymentService._CLUB_PAYMENT_PERMISSIONS):
                return True
        return False

    @staticmethod
    def create_mobile_payment(invoice_pk: int, user) -> dict:
        """Validate an invoice and return payment context for the mobile SDK.

        The returned dict contains everything the Flutter client needs to
        authorize the Square Mobile Payments SDK and start a Tap-to-Pay charge.

        Raises
        ------
        PermissionError  — user is not an admin of the invoice's auction/club.
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

        # Only the merchant (auction/club admin) may take a Tap to Pay payment — never the buyer.
        if not PaymentService._check_admin_access(invoice, user):
            msg = "You do not have permission to take payment for this invoice"
            raise PermissionError(msg)

        if invoice.status == "PAID":
            msg = "Invoice is already paid"
            raise ValueError(msg)

        seller = PaymentService._get_seller_for_invoice(invoice)
        if not seller:
            msg = "Square payments are not configured for this invoice"
            raise ValueError(msg)

        # Block before fetching a token: a legacy account's token lacks the in-person scope, so the
        # on-device authorize() would fail with an opaque Square error. Tell the operator to reconnect.
        if not seller.supports_tap_to_pay:
            msg = "This Square account must be reconnected to enable Tap to Pay."
            raise SquareReconnectRequired(msg)

        access_token = seller.get_valid_access_token()
        if not access_token:
            msg = "Square account token is invalid; the seller must reconnect Square"
            raise ValueError(msg)

        location_id = seller.get_location_id()
        if not location_id:
            msg = "No active Square location found for this seller"
            raise ValueError(msg)

        # Charge the rounded balance so the amount matches the invoice total shown to the buyer
        # (rounded_net_after_payments falls back to the exact amount when invoice rounding is off).
        amount_due = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
        if amount_due <= 0:
            msg = "No amount is due on this invoice"
            raise ValueError(msg)

        return {
            "invoice_pk": invoice_pk,
            "amount": str(amount_due),
            "currency": invoice.currency,
            "location_id": location_id,
            # The client must charge with this reference_id so confirm (and the Square webhook) can
            # bind the payment back to this invoice. Matches the web convention: str(invoice.pk).
            "reference_id": str(invoice_pk),
            # The Mobile Payments SDK authorizes on-device with authorize(accessToken, locationId),
            # so this ships the seller's OAuth access token to the device by design — the SDK
            # requires it. Prefer the shortest-lived token the seller's Square OAuth allows.
            "access_token": access_token,
            # Stable, invoice-derived idempotency key — NOT random. The Mobile Payments SDK keys the
            # on-device charge with this, so if create -> tap is retried for the same (still-unpaid)
            # invoice the duplicate collapses to a single Square charge instead of double-charging.
            # Deterministic so it is identical across retries; Square caps idempotency_key at 45 chars.
            "idempotency_key": f"taptopay-inv-{invoice_pk}",
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
        PermissionError           — user is not an admin of the invoice's auction/club.
        PaymentAlreadyChargedError— the stable idempotency key made Square return an earlier charge
                                    already recorded on this invoice; no new money moved and the
                                    message names the prior charge + remaining balance. Subclasses
                                    PaymentVerificationError (so catch it first).
        PaymentVerificationError  — the card may have been charged but the fetched payment failed a
                                    verification check (status/amount/currency/location/reference)
                                    or could not be fetched from Square. Subclasses ValueError.
        ValueError                — invoice already paid, zero amount, or Square not configured
                                    (checked before any charge would have happened).
        LookupError               — invoice not found.
        """
        from auctions.models import Invoice, InvoicePayment

        try:
            invoice = Invoice.objects.select_related(
                "auction", "auction__created_by", "club", "auctiontos_user__user"
            ).get(pk=invoice_pk)
        except Invoice.DoesNotExist:
            msg = f"Invoice {invoice_pk} not found"
            raise LookupError(msg)

        # Only the merchant (auction/club admin) may confirm a Tap to Pay payment — never the buyer.
        if not PaymentService._check_admin_access(invoice, user):
            msg = "You do not have permission to take payment for this invoice"
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

        # Verify against the rounded balance — the same amount create told the SDK to charge.
        amount_due = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
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
            raise PaymentVerificationError(msg)

        # Some SDK paths report errors on the response instead of raising; handle both.
        if getattr(result, "errors", None):
            detail = "; ".join(getattr(e, "detail", None) or str(e) for e in result.errors)
            logger.error("Square get payment errors for invoice %s: %s", invoice_pk, result.errors)
            msg = f"Square payment lookup failed: {detail}"
            raise PaymentVerificationError(msg)

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
        # Match the web Square convention (create_payment_link sets reference_id = str(invoice.pk),
        # and the webhook resolves the invoice by pk), so the webhook can also reconcile this charge.
        expected_reference_id = str(invoice_pk)

        # NOTE: we accept ONLY "COMPLETED" — not the auth-only "APPROVED" — to match the web Square
        # webhook handler, which treats only COMPLETED as paid. This may change later: if Tap-to-Pay
        # charges can legitimately settle as "APPROVED" for this integration, widen the check here
        # (and keep it consistent with the web flow).
        if payment_status != "COMPLETED":
            msg = f"Square payment {payment_id} is not completed (status={payment_status})"
            raise PaymentVerificationError(msg)
        if sq_amount != amount_cents or sq_currency != invoice.currency:
            # Footgun guard: the create idempotency key is stable per invoice, so if the balance
            # changed after an earlier Tap to Pay charge, re-tapping makes the SDK reuse that key and
            # Square returns the ORIGINAL completed charge (already recorded here) instead of charging
            # the new amount. That looks like an amount mismatch even though no new money moved — so
            # when the fetched payment is one we have already applied to this invoice, raise a
            # specific, actionable error (prior amount + what is still due) instead of the generic one.
            already_recorded = (
                sq_reference_id == expected_reference_id
                and InvoicePayment.objects.filter(
                    invoice=invoice, external_id=fetched_payment_id or payment_id
                ).exists()
            )
            if already_recorded:
                prior_display = f"{Decimal(sq_amount) / 100:.2f}" if sq_amount is not None else "the original amount"
                msg = (
                    f"This invoice was already charged {prior_display} {invoice.currency} with Tap to Pay, "
                    f"so the reader returned that earlier payment instead of making a new one. "
                    f"{amount_due:.2f} {invoice.currency} is still due — take it as cash or send a new "
                    f"payment link instead of tapping again."
                )
                raise PaymentAlreadyChargedError(msg)
            msg = (
                f"Square payment {payment_id} amount mismatch: "
                f"got {sq_amount} {sq_currency}, expected {amount_cents} {invoice.currency}"
            )
            raise PaymentVerificationError(msg)
        if sq_location_id != location_id:
            msg = f"Square payment {payment_id} location mismatch: got {sq_location_id}, expected {location_id}"
            raise PaymentVerificationError(msg)
        if sq_reference_id != expected_reference_id:
            msg = f"Square payment {payment_id} reference mismatch: got {sq_reference_id}, expected {expected_reference_id}"
            raise PaymentVerificationError(msg)

        # Verified — use Square's own id and amount for the record, not whatever the client claimed.
        payment_id = fetched_payment_id or payment_id
        verified_amount = (Decimal(sq_amount) / 100) if sq_amount is not None else amount_due

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
                    "amount": verified_amount,
                    "amount_available_to_refund": verified_amount,
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
