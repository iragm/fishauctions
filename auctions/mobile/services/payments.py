"""Taking a card payment in the room, through the app's Tap to Pay.

:class:`PaymentService` is the whole flow: open an attempt, hand the app what Square needs, verify
what comes back, and book it against the invoice exactly once. The error classes at the top are the
states the app has to tell a volunteer about -- already charged, attempt still open, Square needs
reconnecting -- and they exist because "payment failed" is not an answer when somebody is standing
at the table with a card.

Booking a payment goes through the same invoice and renewal helpers the web pages use, so a payment
taken here and one typed into an invoice cannot end up meaning different things.
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
    """A Tap to Pay charge could not be verified against Square *after* the card was charged.

    Distinct from a plain ``ValueError`` (bad/early input) so the view can tell the operator the
    charge may have gone through — the Square webhook reconciles the same payment by reference_id,
    so refreshing the invoice usually shows it as paid — rather than a flat "invalid request".
    Subclasses ``ValueError`` so existing ``except ValueError`` handlers still catch it.
    """


class PaymentAlreadyChargedError(PaymentVerificationError):
    """``confirm`` was handed a Square payment that is already recorded on this invoice.

    Originally the other face of the stable-idempotency-key mistake: the key was fixed per invoice,
    so after a balance change a re-tap made Square return the ORIGINAL completed charge instead of
    charging the new amount. The key is per-create now (see ``create_mobile_payment``), so that
    particular route is closed -- but a client can still report a payment id this invoice has
    already applied, and the answer is the same one: no new money moved, re-tapping is futile.
    Raised with a cashier-facing message naming the prior charge and what is still due, so the
    operator collects the remainder another way. Subclasses ``PaymentVerificationError`` (hence
    ``ValueError``) so existing handlers still catch it.
    """

    def __init__(self, user_message):
        # This message is deliberately operator-facing (prior charge amount + remaining balance) and
        # carries no stack trace or system internals, so the view surfaces it to the cashier verbatim.
        # Exposing it via an explicit attribute — instead of str(exc) at the boundary — keeps that
        # intent in code and keeps the exception's stringification out of the HTTP response.
        super().__init__(user_message)
        self.user_message = user_message


class TapToPayAttemptOpen(ValueError):
    """A charge attempt on this invoice was started and never finished, so ``create`` refuses.

    The card may already have been charged on the device with no ``confirm`` reaching us, which is
    the one double-charge window left once the idempotency key became per-create. The message is
    written for somebody standing at a checkout desk with a queue behind them, names the time the
    attempt started, and says the single useful thing: check Square before charging again.

    Ages out (``PaymentService.OPEN_ATTEMPT_TIMEOUT``) so a wedged row cannot strand an invoice --
    and the app closes attempts itself on every path where the SDK returned without capturing, so
    an ordinary decline never lands here. Subclasses ``ValueError`` so existing handlers catch it,
    but the create view maps it to a 409 *before* the generic handler so the wording survives.
    """

    def __init__(self, user_message):
        # Cashier-facing and free of internals, like PaymentAlreadyChargedError: the app renders it
        # verbatim through its existing error path, so the wording stays ours and needs no release.
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
    4. Every path that ends without a capture -- cancel, decline, timeout, any SDK
       error -- is reported by the app to ``close_attempt``. Step 1 records an open
       :class:`~auctions.models.TapToPayAttempt` and refuses while one is open, which
       is the whole of the double-charge protection: see that model for why the old
       stable idempotency key was not it.

    Square Mobile Payments SDK (Tap to Pay) integration lives entirely in the
    Flutter app.  This service only handles server-side payment context creation
    and verification of the on-device charge.
    """

    @staticmethod
    def _get_seller_for_invoice(invoice):
        """Return the SquareSeller responsible for this invoice, or None.

        Auction invoices always carry ``club=None`` (that FK is for membership invoices), so the
        club routing for a club auction has to come from the auction, not the invoice -- reading
        ``invoice.club`` alone would charge the creator's personal Square account for a club
        auction and hand out that personal token. ``Auction.effective_square_seller`` applies the
        club-then-creator order and matches what ``Invoice.show_square_button`` advertises.
        """
        if invoice.club:
            return invoice.club.effective_square_seller
        if invoice.auction:
            return invoice.auction.effective_square_seller
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
    def _record_token_handout(invoice, user, request):
        """Log to auction/club history that a Square access token was handed to a device.

        The response ships the seller's merchant-wide OAuth token, so every issuance is worth an
        entry an auction owner can actually read: it is the only record that a given admin pulled
        the credential, and a create with no matching payment is the signal worth noticing.
        Best-effort — a history write must never block a cashier from taking payment.
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

    # Shown by the app verbatim when a signed-in user can't take payments. Sent from here rather
    # than compiled into the app so the reason can be reworded without an app release.
    NOT_A_MERCHANT_MESSAGE = "Only an auction admin with a connected Square account can set up Tap to Pay."

    @staticmethod
    def _latest_admin_auction(user):
        """The auction this user most plausibly collects money for, or None.

        ``last_auction_used`` first: it's the auction the app is already working in (the check-in
        ping and the command palette both write it), so it's the one whose seller the next tap will
        actually charge. Falling back to their newest auction keeps warm-up working for an admin
        who hasn't touched anything on this device yet.

        Only used to *pre*-authorize the reader. Getting it wrong costs one extra on-device
        ``authorize()`` when the real invoice arrives -- ``create_mobile_payment`` re-resolves the
        seller per invoice and is what actually decides which account is charged.
        """
        from auctions.models import Auction

        userdata = getattr(user, "userdata", None)
        candidate = getattr(userdata, "last_auction_used", None)
        if candidate and not candidate.is_deleted and candidate.permission_check(user):
            return candidate
        # Newest first by end date, then creation, so an auction with no end date still sorts.
        recent = (
            Auction.objects.filter(is_deleted=False)
            .select_related("club", "created_by")
            .order_by("-date_end", "-date_start")
        )
        # permission_check covers creator/superuser/AuctionTOS-admin/club-admin and needs a query
        # or two each, so only look at auctions this user is plausibly attached to, newest first.
        plausible = recent.filter(
            Q(created_by=user) | Q(auctiontos__user=user, auctiontos__is_admin=True) | Q(club__members__user=user)
        ).distinct()[:20]
        for auction in plausible:
            if auction.permission_check(user):
                return auction
        return None

    @staticmethod
    def get_payment_authorization(user) -> dict:
        """Credentials for warming up the Tap to Pay reader *before* there is an invoice.

        Apple requires the reader to start preparing when the app comes to the foreground
        (requirement 1.5) and the Tap to Pay UI to appear within a second 90% of the time (5.6).
        Square's SDK only starts preparing once it is authorized, and ``create_mobile_payment``
        authorizes per invoice -- i.e. at the moment the cashier presses the button, which is too
        late. This hands the same seller token out one step earlier.

        ``can_accept_terms`` answers requirement 3.8: only an administrator may accept Apple's Tap
        to Pay terms, and only the backend knows who administers an auction with a linked Square
        seller. It tracks eligibility exactly -- a user who may take payments here is by definition
        an auction/club admin for that seller.

        Never returns credentials for a user who could not charge right now: an account whose token
        is missing, expired beyond refresh, or predates the in-person scope reports
        ``eligible: true`` with no token, which the app handles by showing the setup UI and skipping
        the warm-up. Nothing here charges anything or has side effects.
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

        # Only now touch the token: get_valid_access_token can hit Square's refresh endpoint, and
        # there's no reason to spend that round trip on a seller who can't take an in-person charge.
        access_token = seller.get_valid_access_token()
        location_id = seller.get_location_id() if access_token else None
        if access_token and location_id:
            result["access_token"] = access_token
            result["location_id"] = location_id
            # Deliberately the application log, not auction history the way ``create`` records it.
            # The app calls this on every foreground, so a history row per call would bury the
            # entries that actually mean something (a token pulled against a specific invoice) under
            # thousands of routine warm-ups. This still leaves a trace of every issuance.
            logger.info(
                "Square Tap to Pay warm-up credentials issued to user %s for seller %s",
                user.pk,
                seller.pk,
            )
        return result

    @staticmethod
    def _seller_display_name(auction, seller) -> str:
        """What the app shows as the merchant taking the payment.

        The club or auction name, not the Square account's owner email — this is the name a buyer
        would recognise on a receipt, and it avoids putting an admin's personal address on screen.
        """
        club = getattr(seller, "club", None) or (auction.club if auction else None)
        if club:
            return club.name
        if auction:
            return auction.title
        return str(seller)

    @staticmethod
    def user_can_take_payments(user) -> bool:
        """True when this user administers any auction or club that could take a payment.

        Mirrors ``_check_admin_access`` (which needs an invoice) at the level the warm-up endpoint
        works at. Deliberately strict: this gate is what stands between a signed-in buyer and a
        seller's OAuth token.
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

    #: How long an attempt stays open before it stops blocking a new one. A tap takes seconds; this
    #: is the allowance for a cashier who started one, got distracted, and came back. Long enough
    #: that a captured-but-unconfirmed charge is still being warned about when the retry comes,
    #: short enough that an app killed mid-tap cannot hold the desk up for a shift.
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
        """What the cashier reads. Written for a desk, not for a log.

        The time is rendered in the site's active timezone -- the one the rest of the site prints
        times in -- because the person reading it is standing next to the till, comparing it with
        the clock on the wall and with the Square app in their other hand.
        """
        started = timezone.localtime(attempt.createdon).strftime("%I:%M %p").lstrip("0").lower()
        return (
            f"This invoice may already have been charged - a payment was started at {started} and "
            "never finished. Check it in Square before charging again."
        )

    @staticmethod
    def _open_new_attempt(invoice, user):
        """Record an open attempt and return the id the device charges with.

        Invoice-derived so a charge is still traceable from the Square dashboard back to an
        invoice, plus a nonce so it names *this* attempt: the SDK's ``paymentAttemptId`` is not
        Square's server-side ``idempotency_key``, and reusing one is an error rather than a dedup
        (``payment_attempt_id_reused``, which is what a declined card's retry used to hit). Square
        caps both at 45 characters, and this stays well inside it.
        """
        from auctions.models import TapToPayAttempt

        attempt_id = f"taptopay-inv-{invoice.pk}-{uuid.uuid4().hex[:8]}"
        return TapToPayAttempt.objects.create(invoice=invoice, created_by=user, attempt_id=attempt_id)

    @staticmethod
    def close_attempt(attempt_id: str, outcome: str, user) -> dict:
        """Close an open attempt the SDK returned from without capturing. Best-effort, by design.

        Without this the feature backfires: declines are routine, a declined card would leave the
        attempt open, ``create`` would refuse the retry, and the cashier would be blocked from the
        one action that is definitely correct. The app calls it on cancel, decline, timeout,
        authorize failure and any SDK error, and never shows the cashier a bookkeeping error -- so
        a 404 here (an older attempt already aged out, or a deployment without this endpoint) has
        to be harmless.

        Raises
        ------
        PermissionError  -- the caller does not administer the attempt's invoice.
        LookupError      -- no such attempt.
        ValueError       -- an outcome this endpoint does not accept.
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
        # Same gate as create/confirm: closing an attempt is a statement about somebody's money.
        if not PaymentService._check_admin_access(attempt.invoice, user):
            msg = "You do not have permission to take payment for this invoice"
            raise PermissionError(msg)
        if not attempt.outcome:
            attempt.outcome = outcome
            attempt.closed_at = timezone.now()
            attempt.save(update_fields=["outcome", "closed_at"])
        # Already closed is a success: the app retries best-effort and confirm may have won the race.
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
            # Bookkeeping must never undo a verified charge that is already on the invoice.
            logger.exception("Failed to close Tap to Pay attempts for invoice %s", invoice.pk)

    @staticmethod
    def create_mobile_payment(invoice_pk: int, user, request=None) -> dict:
        """Validate an invoice and return payment context for the mobile SDK.

        The returned dict contains everything the Flutter client needs to
        authorize the Square Mobile Payments SDK and start a Tap-to-Pay charge.

        ``request`` is optional and used only to record the caller's IP on the audit entry.

        Raises
        ------
        PermissionError     — user is not an admin of the invoice's auction/club.
        TapToPayAttemptOpen — a charge attempt on this invoice was started and never finished; the
                              card may already have been charged. Carries the cashier-facing
                              message. Subclasses ValueError, so catch it first.
        ValueError          — invoice already paid, Square not configured,
                              amount is zero/negative.
        LookupError         — invoice not found.
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

        # An attempt already open means a charge was started on this invoice and never finished --
        # possibly captured on-device with the confirm lost. Refuse rather than hand out another
        # token, and say why in words a cashier can act on. Checked after the cheap validation so a
        # paid or misconfigured invoice still gets its own (more useful) answer, and after the token
        # fetch so the invoice row is never locked across a call to Square's refresh endpoint.
        #
        # Locked, because two desks running quick checkout on the same person is exactly the shape
        # of double charge this exists to stop: without it both creates read "nothing open" and both
        # get a token.
        with transaction.atomic():
            locked_invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
            open_attempt = PaymentService._open_attempt(locked_invoice)
            if open_attempt:
                raise TapToPayAttemptOpen(PaymentService._attempt_in_progress_message(open_attempt))
            attempt = PaymentService._open_new_attempt(locked_invoice, user)

        # Nothing below here can fail, so this records exactly the calls that hand out a token.
        PaymentService._record_token_handout(invoice, user, request)

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
            # One value per create, recorded as an open attempt against this invoice. The app passes
            # it to the Mobile Payments SDK as ``paymentAttemptId``, which names *one attempt*: a
            # repeat is an error (``payment_attempt_id_reused``), not a dedup. This used to be a
            # stable per-invoice key, described in a comment about the Payments API's server-side
            # ``idempotency_key`` -- a different concept with the opposite behaviour. Nothing ever
            # deduplicated; what actually happened is that the retry after a declined card failed,
            # which is Tap to Pay failing exactly when it is needed. Double-charge safety lives in
            # the attempt row instead (see TapToPayAttempt), which can tell "already charged" from
            # "the last card was declined". Invoice-derived so a charge is still traceable in the
            # Square dashboard; Square caps both fields at 45 characters.
            "attempt_id": attempt.attempt_id,
            # The same value under the old name. An app build that predates ``attempt_id`` reads
            # this one and derives its own attempt id from it, so both are per-create either way.
            "idempotency_key": attempt.attempt_id,
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

        Returns a dict with ``payment_id``, ``status``, ``receipt_number`` and ``receipt_url``
        from Square.

        Raises
        ------
        PermissionError           — user is not an admin of the invoice's auction/club.
        PaymentAlreadyChargedError— the fetched payment is already recorded on this invoice; no new
                                    money moved and the message names the prior charge + remaining
                                    balance. Subclasses PaymentVerificationError (catch it first).
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
        # Square's own hosted receipt page. Requirement 5.10 says a confidential digital receipt
        # must be sendable for every outcome, approved or declined; the app shares this through the
        # OS share sheet, and with the URL that share is a real receipt instead of a bare number.
        receipt_url = getattr(sq_payment, "receipt_url", "") or ""
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
            # Footgun guard: the fetched payment can be one this invoice has already applied --
            # historically because the create idempotency key was stable per invoice and Square
            # returned the ORIGINAL charge after a balance change; now, only if a client reports an
            # id we have already recorded. Either way it looks like an amount mismatch even though
            # no new money moved, so raise a specific, actionable error (prior amount + what is
            # still due) instead of the generic one.
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
            from auctions.views.base import _ensure_invoice_renewal_state, _process_invoice_membership_renewal

            try:
                _ensure_invoice_renewal_state(invoice)
            except Exception:
                logger.exception("Failed to ensure renewal state for invoice %s (mobile Square)", invoice_pk)
            try:
                _process_invoice_membership_renewal(invoice, payment_method="Square", external_id=payment_id)
            except Exception:
                logger.exception("Failed to process membership renewal for invoice %s (mobile Square)", invoice_pk)

        # The charge is verified and recorded, so whatever attempt was open on this invoice ended
        # in a capture. Closing it is what lets the next legitimate charge on this invoice through.
        PaymentService._capture_attempts(invoice, payment_id)

        logger.info("Mobile Square payment confirmed for invoice %s: %s (new=%s)", invoice_pk, payment_id, created)
        return {
            "payment_id": payment_id,
            "status": payment_status,
            "receipt_number": receipt_number or None,
            # Additive: the app treats a missing/null link as "no receipt to share".
            "receipt_url": receipt_url or None,
        }
