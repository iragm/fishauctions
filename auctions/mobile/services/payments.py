"""Taking a card payment in the room, through the app's Tap to Pay.

:class:`PaymentService` opens an attempt, hands the app what Square needs, verifies the result and
books it against the invoice once, through the same invoice and renewal helpers as the web. The
exception classes are the states a volunteer at the table has to be told about.
"""

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


class PaymentVerificationError(ValueError):
    """A charge couldn't be verified with Square *after* the card was charged.

    Lets the view say the charge may have gone through (the webhook reconciles by reference_id).
    Subclasses ``ValueError`` for existing handlers.
    """


class PaymentAlreadyChargedError(PaymentVerificationError):
    """``confirm`` was handed a Square payment already recorded on this invoice: no new money moved.

    The message names the prior charge and what's still due, so the cashier collects the rest another way.
    """

    def __init__(self, user_message):
        # Cashier-facing, no internals; an explicit attribute keeps str(exc) out of the response.
        super().__init__(user_message)
        self.user_message = user_message


class TapToPayAttemptOpen(ValueError):
    """A charge attempt on this invoice was started and never finished, so ``create`` refuses.

    The card may already be charged with the confirm lost. The message gives the start time and says
    to check Square first. Ages out after ``OPEN_ATTEMPT_TIMEOUT``; the app closes attempts on every
    non-capture path. The create view maps it to a 409 before the generic handler.
    """

    def __init__(self, user_message):
        # Rendered verbatim by the app, so the wording can change without a release.
        super().__init__(user_message)
        self.user_message = user_message


class SquareReconnectRequired(ValueError):
    """The seller's Square token lacks PAYMENTS_WRITE_IN_PERSON; they must reconnect (refresh keeps old
    scopes). Raised before a token reaches the device.
    """


class PaymentService:
    """Server side of Square Tap to Pay; the SDK integration is in the Flutter app.

    1. ``create_mobile_payment`` returns the seller's token, location and an attempt id, and records an
       open :class:`~auctions.models.TapToPayAttempt` (refusing while one is open, which is the
       double-charge protection).
    2. The SDK charges the card on-device; the server never calls ``payments.create``.
    3. ``confirm_mobile_payment`` re-fetches the payment from Square, verifies it and records it.
    4. Every non-capture outcome is reported to ``close_attempt``.
    """

    @staticmethod
    def _get_seller_for_invoice(invoice):
        """The SquareSeller for this invoice, or None. Auction invoices have ``club=None``, so club routing
        comes from ``Auction.effective_square_seller``; otherwise a club auction would use the creator's
        personal token.
        """
        if invoice.club:
            return invoice.club.effective_square_seller
        if invoice.auction:
            return invoice.auction.effective_square_seller
        return None

    @staticmethod
    def _square_error_detail(exc) -> str:
        """A readable message from a Square SDK ``ApiError``, as the web payment link does it."""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            errors = body.get("errors") or []
            if errors and isinstance(errors, list):
                return errors[0].get("detail") or errors[0].get("code") or str(exc)
        return str(exc)

    # permission_admin is a wildcard inside check_club_permission.
    _CLUB_PAYMENT_PERMISSIONS = ("permission_money", "permission_manage_auctions")

    @staticmethod
    def _check_admin_access(invoice, user) -> bool:
        """Authorize the merchant, never the buyer, who would otherwise get the seller's OAuth token.

        Auction invoices: ``Auction.permission_check`` (creator, superuser, auction admin). Club auctions
        and membership invoices: a club admin, money manager or auction manager, checked directly so it
        applies outside club-managed mode too.
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
    def _record_token_handout(invoice, user, request):
        """Log to auction/club history that a Square token was handed to a device: the only record of who
        pulled the credential. Best-effort; never blocks a payment.
        """
        from auctions.mobile.services.ar import _client_ip

        ip = _client_ip(request) if request else ""
        detail = f" from {ip}" if ip else ""
        action = f"Square Tap to Pay access token issued to {user} for invoice {invoice.pk}{detail}"
        try:
            if invoice.auction:
                invoice.auction.create_history(applies_to="INVOICES", action=action, user=user)
            elif invoice.club:
                from auctions.models import ClubHistory

                ClubHistory.objects.create(club=invoice.club, user=user, action=action[:800], applies_to="MEMBERSHIP")
        except Exception:
            logger.exception("Failed to record Square token handout for invoice %s", invoice.pk)

    # Sent from here so it can be reworded without an app release.
    NOT_A_MERCHANT_MESSAGE = "Only an auction admin with a connected Square account can set up Tap to Pay."

    @staticmethod
    def _latest_admin_auction(user):
        """The auction this user most plausibly collects money for, or None: ``last_auction_used`` first,
        then their newest. Only pre-authorizes the reader; ``create_mobile_payment`` decides the seller.
        """
        from auctions.models import Auction

        userdata = getattr(user, "userdata", None)
        candidate = getattr(userdata, "last_auction_used", None)
        if candidate and not candidate.is_deleted and candidate.permission_check(user):
            return candidate
        recent = (
            Auction.objects.filter(is_deleted=False)
            .select_related("club", "created_by")
            .order_by("-date_end", "-date_start")
        )
        # Only auctions this user is plausibly attached to, since permission_check costs queries.
        plausible = recent.filter(
            Q(created_by=user) | Q(auctiontos__user=user, auctiontos__is_admin=True) | Q(club__members__user=user)
        ).distinct()[:20]
        for auction in plausible:
            if auction.permission_check(user):
                return auction
        return None

    @staticmethod
    def get_payment_authorization(user) -> dict:
        """Credentials for warming up the Tap to Pay reader before there is an invoice.

        Apple requires the reader to prepare on foreground (1.5) and the UI within a second (5.6).
        ``can_accept_terms`` answers 3.8 (only an admin may accept Apple's terms). A user who can't charge
        right now gets ``eligible: true`` with no token, and the app shows setup instead. No side effects.
        """
        if not PaymentService.user_can_take_payments(user):
            return {
                "eligible": False,
                "can_accept_terms": False,
                "message": PaymentService.NOT_A_MERCHANT_MESSAGE,
            }

        auction = PaymentService._latest_admin_auction(user)
        seller = auction.effective_square_seller if auction else None
        result = {"eligible": True, "can_accept_terms": True}
        if seller:
            result["seller_name"] = PaymentService._seller_display_name(auction, seller)
        if not seller or not seller.supports_tap_to_pay:
            return result

        # Token only now: refreshing may call Square.
        access_token = seller.get_valid_access_token()
        location_id = seller.get_location_id() if access_token else None
        if access_token and location_id:
            result["access_token"] = access_token
            result["location_id"] = location_id
            # Application log, not auction history: this runs on every foreground.
            logger.info(
                "Square Tap to Pay warm-up credentials issued to user %s for seller %s",
                user.pk,
                seller.pk,
            )
        return result

    @staticmethod
    def _seller_display_name(auction, seller) -> str:
        """The merchant name the app shows: the club or auction, never the Square owner's email."""
        club = getattr(seller, "club", None) or (auction.club if auction else None)
        if club:
            return club.name
        if auction:
            return auction.title
        return str(seller)

    @staticmethod
    def user_can_take_payments(user) -> bool:
        """True when this user administers any auction or club that could take a payment. Strict: it guards
        the seller's OAuth token.
        """
        from auctions.models import Auction, ClubMember

        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        if Auction.objects.filter(is_deleted=False, created_by=user).exists():
            return True
        if Auction.objects.filter(is_deleted=False, auctiontos__user=user, auctiontos__is_admin=True).exists():
            return True
        return (
            ClubMember.objects.filter(user=user, is_deleted=False)
            .filter(
                Q(permission_admin=True) | Q(permission_money=True) | Q(permission_manage_auctions=True),
            )
            .exists()
        )

    #: How long an open attempt blocks a new one: long enough to warn about a lost confirm, short
    #: enough that a killed app can't hold up the desk.
    OPEN_ATTEMPT_TIMEOUT = timedelta(minutes=5)

    @staticmethod
    def _expire_stale_attempts(invoice):
        """Close attempts older than the timeout, so a wedged row can't strand an invoice."""
        from auctions.models import TapToPayAttempt

        cutoff = timezone.now() - PaymentService.OPEN_ATTEMPT_TIMEOUT
        TapToPayAttempt.objects.filter(invoice=invoice, outcome="", createdon__lt=cutoff).update(
            outcome=TapToPayAttempt.OUTCOME_EXPIRED, closed_at=timezone.now()
        )

    @staticmethod
    def _open_attempt(invoice):
        """The attempt currently blocking a new charge on this invoice, or None."""
        from auctions.models import TapToPayAttempt

        PaymentService._expire_stale_attempts(invoice)
        return TapToPayAttempt.objects.filter(invoice=invoice, outcome="").order_by("-createdon").first()

    @staticmethod
    def _attempt_in_progress_message(attempt) -> str:
        """The cashier's message, with the start time in the site timezone to compare against the wall clock."""
        started = timezone.localtime(attempt.createdon).strftime("%I:%M %p").lstrip("0").lower()
        return (
            f"This invoice may already have been charged - a payment was started at {started} and "
            "never finished. Check it in Square before charging again."
        )

    @staticmethod
    def _open_new_attempt(invoice, user):
        """Record an open attempt and return the id the device charges with.

        Invoice-derived for dashboard tracing, plus a nonce: the SDK's ``paymentAttemptId`` can't be reused
        (``payment_attempt_id_reused``). Under Square's 45-character cap.
        """
        from auctions.models import TapToPayAttempt

        attempt_id = f"taptopay-inv-{invoice.pk}-{uuid.uuid4().hex[:8]}"
        return TapToPayAttempt.objects.create(invoice=invoice, created_by=user, attempt_id=attempt_id)

    @staticmethod
    def close_attempt(attempt_id: str, outcome: str, user) -> dict:
        """Close an attempt the SDK returned from without capturing. Best-effort by design.

        Otherwise a declined card blocks the retry. The app never shows a bookkeeping error here, so a 404
        must be harmless. Raises PermissionError (not an admin), LookupError (no attempt), ValueError (bad
        outcome).
        """
        from auctions.models import TapToPayAttempt

        if outcome not in (TapToPayAttempt.OUTCOME_CANCELED, TapToPayAttempt.OUTCOME_FAILED):
            msg = f"Unknown attempt outcome {outcome!r}"
            raise ValueError(msg)
        attempt = (
            TapToPayAttempt.objects.select_related("invoice", "invoice__auction", "invoice__club")
            .filter(attempt_id=attempt_id)
            .first()
        )
        if not attempt:
            msg = f"Tap to Pay attempt {attempt_id} not found"
            raise LookupError(msg)
        # Same gate as create/confirm.
        if not PaymentService._check_admin_access(attempt.invoice, user):
            msg = "You do not have permission to take payment for this invoice"
            raise PermissionError(msg)
        if not attempt.outcome:
            attempt.outcome = outcome
            attempt.closed_at = timezone.now()
            attempt.save(update_fields=["outcome", "closed_at"])
        # Already closed is success; confirm may have won the race.
        return {"attempt_id": attempt.attempt_id, "outcome": attempt.outcome}

    @staticmethod
    def _capture_attempts(invoice, payment_id: str):
        """Close every open attempt on this invoice as captured. Never blocks recording a payment."""
        from auctions.models import TapToPayAttempt

        try:
            TapToPayAttempt.objects.filter(invoice=invoice, outcome="").update(
                outcome=TapToPayAttempt.OUTCOME_CAPTURED,
                closed_at=timezone.now(),
                payment_id=payment_id[:255],
            )
        except Exception:
            # Bookkeeping must never undo a verified charge.
            logger.exception("Failed to close Tap to Pay attempts for invoice %s", invoice.pk)

    @staticmethod
    def create_mobile_payment(invoice_pk: int, user, request=None) -> dict:
        """Validate an invoice and return what the app needs to start a Tap to Pay charge. ``request`` only
        supplies the IP for the audit entry.

        Raises PermissionError (not an admin), TapToPayAttemptOpen (catch before ValueError), ValueError
        (paid, Square not configured, nothing due), LookupError (no invoice).
        """
        from auctions.models import Invoice

        try:
            invoice = Invoice.objects.select_related(
                "auction", "auction__created_by", "club", "auctiontos_user__user"
            ).get(pk=invoice_pk)
        except Invoice.DoesNotExist:
            msg = f"Invoice {invoice_pk} not found"
            raise LookupError(msg)

        # Merchant only, never the buyer.
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

        # Before fetching a token: a legacy token would fail on-device with an opaque error.
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

        # The rounded balance, matching the total the buyer sees.
        amount_due = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
        if amount_due <= 0:
            msg = "No amount is due on this invoice"
            raise ValueError(msg)

        # An open attempt may be a captured charge whose confirm was lost: refuse. After cheap
        # validation (better answers) and after the token fetch (no lock across Square). Locked so
        # two desks can't both see nothing open.
        with transaction.atomic():
            locked_invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
            open_attempt = PaymentService._open_attempt(locked_invoice)
            if open_attempt:
                raise TapToPayAttemptOpen(PaymentService._attempt_in_progress_message(open_attempt))
            attempt = PaymentService._open_new_attempt(locked_invoice, user)

        # Nothing below can fail, so this logs exactly the calls that hand out a token.
        PaymentService._record_token_handout(invoice, user, request)

        return {
            "invoice_pk": invoice_pk,
            "amount": str(amount_due),
            "currency": invoice.currency,
            "location_id": location_id,
            # Charged with this reference_id so confirm and the webhook can find the invoice.
            "reference_id": str(invoice_pk),
            # The SDK's authorize(accessToken, locationId) requires the seller token on the device.
            "access_token": access_token,
            # Per create, used as the SDK's paymentAttemptId, which names one attempt: reuse is an
            # error, not a dedup. Double-charge safety is the attempt row.
            "attempt_id": attempt.attempt_id,
            # Same value under the old name for older app builds.
            "idempotency_key": attempt.attempt_id,
            "square_environment": settings.SQUARE_ENVIRONMENT,
        }

    @staticmethod
    def confirm_mobile_payment(invoice_pk: int, payment_id: str, idempotency_key: str, user) -> dict:
        """Verify an on-device Tap to Pay charge and record it. Charges nothing.

        Re-fetches the payment from Square and checks status, amount, currency, location and reference
        before recording. ``idempotency_key`` is accepted but unused. Returns ``payment_id``, ``status``,
        ``receipt_number``, ``receipt_url``.

        Raises PermissionError, PaymentAlreadyChargedError (catch before PaymentVerificationError),
        PaymentVerificationError (failed a check or couldn't fetch), ValueError (paid, nothing due, not
        configured), LookupError.
        """
        from auctions.models import Invoice, InvoicePayment

        try:
            invoice = Invoice.objects.select_related(
                "auction", "auction__created_by", "club", "auctiontos_user__user"
            ).get(pk=invoice_pk)
        except Invoice.DoesNotExist:
            msg = f"Invoice {invoice_pk} not found"
            raise LookupError(msg)

        # Merchant only, never the buyer.
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

        # The same rounded balance create charged.
        amount_due = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
        if amount_due <= 0:
            msg = "No amount is due on this invoice"
            raise ValueError(msg)

        amount_cents = int(amount_due * 100)

        # squareup 44.x: named kwargs, typed response, raises on failure. Fetch only, never charge.
        try:
            result = client.payments.get(payment_id=payment_id)
        except Exception as exc:
            detail = PaymentService._square_error_detail(exc)
            logger.error("Square get payment failed for invoice %s: %s", invoice_pk, detail)
            msg = f"Square payment lookup failed: {detail}"
            raise PaymentVerificationError(msg)

        if getattr(result, "errors", None):
            detail = "; ".join(getattr(e, "detail", None) or str(e) for e in result.errors)
            logger.error("Square get payment errors for invoice %s: %s", invoice_pk, result.errors)
            msg = f"Square payment lookup failed: {detail}"
            raise PaymentVerificationError(msg)

        sq_payment = result.payment
        fetched_payment_id = getattr(sq_payment, "id", "") or ""
        receipt_number = (getattr(sq_payment, "receipt_number", "") or "")[:10]
        # Square's hosted receipt, shareable for every outcome (requirement 5.10).
        receipt_url = getattr(sq_payment, "receipt_url", "") or ""
        payment_status = getattr(sq_payment, "status", None)

        # SECURITY BOUNDARY: the client only reports a payment_id. Verify it was a COMPLETED charge
        # for the right amount, currency, location and invoice; any mismatch records nothing.
        amount_money = getattr(sq_payment, "amount_money", None)
        sq_amount = getattr(amount_money, "amount", None)
        sq_currency = getattr(amount_money, "currency", None)
        sq_location_id = getattr(sq_payment, "location_id", None)
        sq_reference_id = getattr(sq_payment, "reference_id", None)
        # reference_id = str(invoice.pk), as the web payment link and webhook use.
        expected_reference_id = str(invoice_pk)

        # COMPLETED only, not auth-only APPROVED, matching the web webhook. Widen both together.
        if payment_status != "COMPLETED":
            msg = f"Square payment {payment_id} is not completed (status={payment_status})"
            raise PaymentVerificationError(msg)
        if sq_amount != amount_cents or sq_currency != invoice.currency:
            # A payment already recorded here looks like an amount mismatch; say so specifically.
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

        # Record Square's id and amount, not the client's.
        payment_id = fetched_payment_id or payment_id
        verified_amount = (Decimal(sq_amount) / 100) if sq_amount is not None else amount_due

        # Idempotent: the webhook get_or_creates on (invoice, external_id). Invoice locked.
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

        # Renewal hooks may not be idempotent: only the recording request runs them, after commit.
        if created:
            from auctions.views.base import _ensure_invoice_renewal_state, _process_invoice_membership_renewal

            try:
                _ensure_invoice_renewal_state(invoice)
            except Exception:
                logger.exception("Failed to ensure renewal state for invoice %s (mobile Square)", invoice_pk)
            try:
                _process_invoice_membership_renewal(invoice, payment_method="Square", external_id=payment_id)
            except Exception:
                logger.exception("Failed to process membership renewal for invoice %s (mobile Square)", invoice_pk)

        # Closing the attempt lets the next legitimate charge through.
        PaymentService._capture_attempts(invoice, payment_id)

        logger.info("Mobile Square payment confirmed for invoice %s: %s (new=%s)", invoice_pk, payment_id, created)
        return {
            "payment_id": payment_id,
            "status": payment_status,
            "receipt_number": receipt_number or None,
            # The app treats null as "no receipt to share".
            "receipt_url": receipt_url or None,
        }
