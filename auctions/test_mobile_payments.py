"""Tap to Pay from inside the app: confirming a payment, and which Square seller it routes to."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.urls import reverse

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    SquareSeller,
)
from auctions.tests import StandardTestCase


class MobilePaymentConfirmTests(StandardTestCase):
    """confirm_mobile_payment verifies an on-device Tap to Pay charge + idempotent recording.

    The Mobile Payments SDK charges the card on-device and returns a completed payment_id; the
    server re-fetches it via GetPayment (client.payments.get) and verifies it before recording.

    Tap to Pay is operated by the merchant (auction admin), so the service is driven here as
    ``self.admin_user`` (an is_admin TOS on the auction); the buyer is never authorized.
    """

    def setUp(self):
        super().setUp()
        # A fresh buyer with no lots + one ADD adjustment owes a deterministic $20.
        self.buyer = User.objects.create_user("mobilebuyer", "mb@example.com", "pw")
        tos = AuctionTOS.objects.create(user=self.buyer, auction=self.online_auction, pickup_location=self.location)
        self.pay_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()

    def _mock_seller(self, get_return=None, get_side_effect=None):
        seller = MagicMock()
        seller.get_valid_access_token.return_value = "tok"
        seller.get_location_id.return_value = "LOC1"
        client = MagicMock()
        if get_side_effect is not None:
            client.payments.get.side_effect = get_side_effect
        else:
            client.payments.get.return_value = get_return
        seller.get_square_client.return_value = client
        return seller, client

    def _payment_response(
        self,
        pid="PAY1",
        status_="COMPLETED",
        receipt="RC123",
        amount=2000,
        currency=None,
        location_id="LOC1",
        reference_id=None,
    ):
        from types import SimpleNamespace

        # Mirror the squareup 44.x typed GetPaymentResponse: .errors, .payment, and a nested Money
        # object (attributes, not a dict) on .amount_money.
        currency = currency if currency is not None else self.pay_invoice.currency
        reference_id = reference_id if reference_id is not None else str(self.pay_invoice.pk)
        payment = SimpleNamespace(
            id=pid,
            status=status_,
            receipt_number=receipt,
            amount_money=SimpleNamespace(amount=amount, currency=currency),
            location_id=location_id,
            reference_id=reference_id,
        )
        return SimpleNamespace(errors=None, payment=payment)

    def test_confirm_verifies_payment_records_and_marks_paid(self):
        from auctions.mobile.services.payments import PaymentService

        seller, client = self._mock_seller(get_return=self._payment_response())
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views.base._ensure_invoice_renewal_state"),
            patch("auctions.views.base._process_invoice_membership_renewal"),
        ):
            result = PaymentService.confirm_mobile_payment(
                invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="idem-1", user=self.admin_user
            )
        self.assertTrue(client.payments.get.called)  # GetPayment, not payments.create
        self.assertFalse(client.payments.create.called)  # the server must NOT charge anything
        self.assertEqual(client.payments.get.call_args.kwargs["payment_id"], "PAY1")
        self.assertEqual(result["payment_id"], "PAY1")
        self.pay_invoice.refresh_from_db()
        self.assertEqual(self.pay_invoice.status, "PAID")
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.pay_invoice, external_id="PAY1").count(), 1)

    def test_create_and_confirm_use_rounded_amount(self):
        """With invoice rounding on, Tap to Pay charges/verifies the rounded balance, not the cents.

        A fractional residual ($19.60 owed) is charged at the rounded $19.00 (customer's favour);
        confirm must accept the $19.00 (1900c) Square charge and mark the invoice PAID even though a
        fractional residual remains on net_after_payments.
        """

        from auctions.mobile.services.payments import PaymentService

        self.assertTrue(self.online_auction.invoice_rounding)  # default; the fix is a no-op without it
        tos = AuctionTOS.objects.create(
            user=User.objects.create_user("roundbuyer", "rb@example.com", "pw"),
            auction=self.online_auction,
            pickup_location=self.location,
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        # $20 owed, less a $0.40 partial payment, leaves a fractional $19.60 balance.
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=invoice)
        InvoicePayment.objects.create(
            invoice=invoice, payment_method="Cash", amount=Decimal("0.40"), currency=invoice.currency
        )
        invoice.refresh_from_db()
        # Rounding must actually change the amount for this test to be meaningful.
        unrounded = Decimal("0.00") - Decimal(invoice.net_after_payments)
        rounded = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
        self.assertNotEqual(rounded, unrounded)
        self.assertEqual(rounded, Decimal("19.00"))

        amount_cents = int(rounded * 100)
        seller, _ = self._mock_seller(
            get_return=self._payment_response(amount=amount_cents, reference_id=str(invoice.pk))
        )
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views.base._ensure_invoice_renewal_state"),
            patch("auctions.views.base._process_invoice_membership_renewal"),
        ):
            create_result = PaymentService.create_mobile_payment(invoice_pk=invoice.pk, user=self.admin_user)
            self.assertEqual(create_result["amount"], str(rounded))  # rounded, not the fractional balance

            confirm_result = PaymentService.confirm_mobile_payment(
                invoice_pk=invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.admin_user
            )
        self.assertEqual(confirm_result["payment_id"], "PAY1")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "PAID")
        payment = InvoicePayment.objects.get(invoice=invoice, external_id="PAY1")
        self.assertEqual(payment.amount, rounded)  # recorded the verified (rounded) Square amount

    def _assert_rejected_and_unrecorded(self, payment_response):
        """A verification failure must raise ValueError and record / mark nothing."""
        from auctions.mobile.services.payments import PaymentService

        seller, _ = self._mock_seller(get_return=payment_response)
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views.base._ensure_invoice_renewal_state") as ensure,
            patch("auctions.views.base._process_invoice_membership_renewal") as renew,
        ):
            with self.assertRaises(ValueError):
                PaymentService.confirm_mobile_payment(
                    invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.admin_user
                )
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.pay_invoice).count(), 0)
        self.pay_invoice.refresh_from_db()
        self.assertNotEqual(self.pay_invoice.status, "PAID")
        ensure.assert_not_called()
        renew.assert_not_called()

    def test_confirm_rejects_wrong_amount(self):
        self._assert_rejected_and_unrecorded(self._payment_response(amount=1999))

    def test_confirm_rejects_wrong_currency(self):
        self._assert_rejected_and_unrecorded(self._payment_response(currency="EUR"))

    def test_confirm_rejects_non_completed_status(self):
        self._assert_rejected_and_unrecorded(self._payment_response(status_="PENDING"))

    def test_confirm_rejects_wrong_location(self):
        self._assert_rejected_and_unrecorded(self._payment_response(location_id="LOC_OTHER"))

    def test_confirm_rejects_wrong_reference_id(self):
        # A payment bound to a different invoice's reference (its pk) must not pay this one.
        self._assert_rejected_and_unrecorded(self._payment_response(reference_id=str(self.pay_invoice.pk + 99999)))

    def test_confirm_rejects_already_paid(self):
        from auctions.mobile.services.payments import PaymentService

        self.pay_invoice.status = "PAID"
        self.pay_invoice.save(update_fields=["status"])
        seller, client = self._mock_seller(get_return=self._payment_response())
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            with self.assertRaises(ValueError):
                PaymentService.confirm_mobile_payment(
                    invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.admin_user
                )
        self.assertFalse(client.payments.get.called)

    def test_confirm_is_idempotent_on_external_id(self):
        from auctions.mobile.services.payments import PaymentService

        # Owe more than the pre-existing payment so a balance remains (a payment that covers the
        # whole invoice would trip the "no amount due" guard before the dedup path is reached).
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=40, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()
        # Simulate the Square webhook (or a prior retry) already recording this payment.
        InvoicePayment.objects.create(
            invoice=self.pay_invoice,
            external_id="PAY1",
            payment_method="Square",
            amount=20,
            amount_available_to_refund=20,
            currency=self.pay_invoice.currency,
        )
        # $60 owed, $20 already recorded → $40 (4000 cents) due at confirm time; the verification
        # recomputes amount_due net of the existing payment, so the fetched payment must match it.
        seller, _ = self._mock_seller(get_return=self._payment_response(pid="PAY1", amount=4000))
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views.base._ensure_invoice_renewal_state") as ensure,
            patch("auctions.views.base._process_invoice_membership_renewal") as renew,
        ):
            PaymentService.confirm_mobile_payment(
                invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.admin_user
            )
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.pay_invoice, external_id="PAY1").count(), 1)
        ensure.assert_not_called()  # didn't create the record → must not re-run renewal side effects
        renew.assert_not_called()
        self.pay_invoice.refresh_from_db()
        self.assertEqual(self.pay_invoice.status, "PAID")

    def test_confirm_surfaces_actionable_message_on_idempotency_key_reuse(self):
        # The footgun: the create idempotency key is stable per invoice, so after an earlier Tap to Pay
        # charge a re-tap reuses it and Square returns that ORIGINAL (already-recorded) charge instead
        # of charging the new balance. Confirm must raise the specific PaymentAlreadyChargedError —
        # naming the prior charge and what is still due — not the generic mismatch error, and record
        # nothing new.
        from auctions.mobile.services.payments import PaymentAlreadyChargedError, PaymentService

        # A prior $20 Square charge (external_id PAY1) is already on the invoice...
        InvoicePayment.objects.create(
            invoice=self.pay_invoice,
            external_id="PAY1",
            payment_method="Square",
            amount=20,
            amount_available_to_refund=20,
            currency=self.pay_invoice.currency,
        )
        # ...then $30 more is added, so $30 is now due. Re-tapping deduped to the original $20 payment.
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=30, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()

        seller, _ = self._mock_seller(get_return=self._payment_response(pid="PAY1", amount=2000))
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views.base._ensure_invoice_renewal_state") as ensure,
            patch("auctions.views.base._process_invoice_membership_renewal") as renew,
        ):
            with self.assertRaises(PaymentAlreadyChargedError) as cm:
                PaymentService.confirm_mobile_payment(
                    invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.admin_user
                )
        # PaymentAlreadyChargedError is still a ValueError subclass (so generic handlers catch it too).
        self.assertIsInstance(cm.exception, ValueError)
        msg = str(cm.exception)
        self.assertIn("20.00", msg)  # the amount already charged
        self.assertIn("30.00", msg)  # the amount still due
        # Nothing new recorded, no renewal side effects, invoice not flipped to PAID off the stale charge.
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.pay_invoice).count(), 1)
        ensure.assert_not_called()
        renew.assert_not_called()

    def test_confirm_square_error_raises_valueerror(self):
        from auctions.mobile.services.payments import PaymentService

        seller, _ = self._mock_seller(get_side_effect=Exception("payment not found"))
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            with self.assertRaises(ValueError):
                PaymentService.confirm_mobile_payment(
                    invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.admin_user
                )

    def test_create_returns_access_token_not_application_id(self):
        from auctions.mobile.services.payments import PaymentService

        seller, _ = self._mock_seller()
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            result = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user)
        self.assertEqual(result["access_token"], "tok")
        self.assertNotIn("square_application_id", result)
        self.assertEqual(result["location_id"], "LOC1")
        self.assertEqual(result["amount"], "20.00")
        # The client must charge with this reference_id; it matches the web convention (str(pk)).
        self.assertEqual(result["reference_id"], str(self.pay_invoice.pk))
        # Every documented create field is present (and nothing extra leaks).
        self.assertEqual(
            set(result),
            {
                "invoice_pk",
                "amount",
                "currency",
                "location_id",
                "reference_id",
                "access_token",
                "attempt_id",
                "idempotency_key",
                "square_environment",
            },
        )

    def test_create_issues_a_fresh_attempt_id_every_time(self):
        """The value the app hands the SDK as ``paymentAttemptId`` names ONE attempt.

        It used to be a stable per-invoice string, with a comment describing the Payments API's
        server-side ``idempotency_key`` — a different concept with the opposite behaviour. Nothing
        ever deduplicated; what actually happened, on hardware, is that the retry after a declined
        card was refused by Square with ``payment_attempt_id_reused``, which is Tap to Pay failing
        exactly when it is needed. Double-charge safety is the attempt record now, not this string.
        """
        from auctions.mobile.services.payments import PaymentService

        other_tos = AuctionTOS.objects.create(
            user=User.objects.create_user("idembuyer", "idem@example.com", "pw"),
            auction=self.online_auction,
            pickup_location=self.location,
        )
        other_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=other_tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=other_invoice)
        other_invoice.refresh_from_db()

        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._mock_seller()[0]):
            first = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user)
            # The attempt has to be closed the way a decline closes it, or create refuses the retry.
            PaymentService.close_attempt(attempt_id=first["attempt_id"], outcome="failed", user=self.admin_user)
            again = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user)
            other = PaymentService.create_mobile_payment(invoice_pk=other_invoice.pk, user=self.admin_user)

        self.assertNotEqual(first["attempt_id"], again["attempt_id"])
        self.assertNotEqual(first["attempt_id"], other["attempt_id"])
        # Still invoice-derived, so a charge is traceable from the Square dashboard to an invoice.
        self.assertIn(str(self.pay_invoice.pk), first["attempt_id"])
        self.assertLessEqual(len(first["attempt_id"]), 45)  # Square caps both fields at 45 chars
        # The old field name carries the same value, for app builds that predate attempt_id.
        self.assertEqual(first["idempotency_key"], first["attempt_id"])

    def test_create_blocks_seller_without_tap_to_pay_scope(self):
        # A legacy Square account (token lacks PAYMENTS_WRITE_IN_PERSON) is blocked before the device
        # is handed a token, with a distinguishable error so the app can prompt a reconnect.
        from auctions.mobile.services.payments import PaymentService, SquareReconnectRequired

        seller, _ = self._mock_seller()
        seller.supports_tap_to_pay = False
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            with self.assertRaises(SquareReconnectRequired):
                PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user)

    def test_create_denies_buyer(self):
        # The buyer must NOT be able to create a payment — that would leak the seller's Square token.
        from auctions.mobile.services.payments import PaymentService

        seller, _ = self._mock_seller()
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            with self.assertRaises(PermissionError):
                PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.buyer)

    def test_create_denies_non_admin_other_user(self):
        from auctions.mobile.services.payments import PaymentService

        with self.assertRaises(PermissionError):
            PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.userB)

    def test_create_allows_is_admin_tos_on_square_auction_without_club(self):
        # A Square auction with no club: anyone with an is_admin AuctionTOS (not just the creator)
        # can take payment. online_auction has no club, and this fresh admin isn't its creator.
        from auctions.mobile.services.payments import PaymentService

        self.assertIsNone(self.online_auction.club_id)  # no club on this auction
        tos_admin = User.objects.create_user("tos_admin", "ta@example.com", "pw")
        AuctionTOS.objects.create(
            user=tos_admin, auction=self.online_auction, pickup_location=self.location, is_admin=True
        )
        seller, _ = self._mock_seller()
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            result = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=tos_admin)
        self.assertEqual(result["access_token"], "tok")

    def test_create_allows_club_manage_auctions_permission(self):
        # A club member with "manage auctions" can take payment for that club's auction invoice —
        # even when the auction is not manage_users_through_club (so Auction.permission_check alone,
        # which gates the club branch on is_club_managed, would not grant it).
        from auctions.mobile.services.payments import PaymentService

        club = Club.objects.create(name="Mgr Club")
        self.online_auction.club = club
        self.online_auction.manage_users_through_club = False
        self.online_auction.save()
        manager = User.objects.create_user("club_mgr", "mgr@example.com", "pw")
        ClubMember.objects.create(club=club, user=manager, name="Mgr", permission_manage_auctions=True)
        self.assertFalse(self.online_auction.permission_check(manager))  # not granted by the auction alone
        seller, _ = self._mock_seller()
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            result = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=manager)
        self.assertEqual(result["access_token"], "tok")

    def test_create_denies_club_member_without_payment_permission(self):
        # A plain club member (no money / manage-auctions / admin permission) is still denied.
        from auctions.mobile.services.payments import PaymentService

        club = Club.objects.create(name="Plain Club")
        self.online_auction.club = club
        self.online_auction.manage_users_through_club = False
        self.online_auction.save()
        member = User.objects.create_user("plain_member", "pm@example.com", "pw")
        ClubMember.objects.create(club=club, user=member, name="Plain")
        with self.assertRaises(PermissionError):
            PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=member)

    def test_confirm_denies_buyer_before_charging(self):
        # Buyer is rejected before any Square call and nothing is recorded.
        from auctions.mobile.services.payments import PaymentService

        seller, client = self._mock_seller(get_return=self._payment_response())
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            with self.assertRaises(PermissionError):
                PaymentService.confirm_mobile_payment(
                    invoice_pk=self.pay_invoice.pk, payment_id="PAY1", idempotency_key="i", user=self.buyer
                )
        self.assertFalse(client.payments.get.called)
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.pay_invoice).count(), 0)


class MobilePaymentEndpointTests(StandardTestCase):
    """The /api/mobile/payments/ HTTP layer: JWT auth, the PermissionError->403 mapping, and that
    only the merchant (auction admin) — not the buyer — can reach create/confirm."""

    def setUp(self):
        super().setUp()
        from rest_framework_simplejwt.tokens import RefreshToken

        self._RefreshToken = RefreshToken
        self.buyer = User.objects.create_user("endpointbuyer", "eb@example.com", "pw")
        tos = AuctionTOS.objects.create(user=self.buyer, auction=self.online_auction, pickup_location=self.location)
        self.pay_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()
        self.create_url = reverse("mobile-payment-create")
        self.confirm_url = reverse("mobile-payment-confirm")

    def _bearer(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._RefreshToken.for_user(user).access_token}"}

    def _mock_seller(self):
        seller = MagicMock()
        seller.get_valid_access_token.return_value = "tok"
        seller.get_location_id.return_value = "LOC1"
        return seller

    def test_create_requires_jwt(self):
        self.assertIn(self.client.post(self.create_url, {"invoice_pk": self.pay_invoice.pk}).status_code, (401, 403))

    def test_admin_can_create_and_gets_reference_id(self):
        from auctions.mobile.services.payments import PaymentService

        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._mock_seller()):
            resp = self.client.post(
                self.create_url, {"invoice_pk": self.pay_invoice.pk}, **self._bearer(self.admin_user)
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["access_token"], "tok")
        self.assertEqual(body["reference_id"], str(self.pay_invoice.pk))

    def test_buyer_create_is_403(self):
        # Even with Square configured, the buyer must get 403 and never see the access token.
        from auctions.mobile.services.payments import PaymentService

        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._mock_seller()):
            resp = self.client.post(self.create_url, {"invoice_pk": self.pay_invoice.pk}, **self._bearer(self.buyer))
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("access_token", resp.json())

    def test_buyer_confirm_is_403(self):
        resp = self.client.post(
            self.confirm_url,
            {"invoice_pk": self.pay_invoice.pk, "payment_id": "PAY1", "idempotency_key": "i"},
            **self._bearer(self.buyer),
        )
        self.assertEqual(resp.status_code, 403)

    def test_confirm_already_charged_returns_409_with_actionable_code(self):
        # The idempotency-key-reuse footgun must reach the app as an actionable 409 (code
        # "already_charged" + a cashier-facing detail), not the generic "couldn't verify" message.
        from types import SimpleNamespace

        from auctions.mobile.services.payments import PaymentService

        # A prior $20 Square charge is on the invoice; then $30 more is added, so $30 is now due. The
        # re-tap deduped to the original $20 charge (same payment_id), which no longer covers the due.
        InvoicePayment.objects.create(
            invoice=self.pay_invoice,
            external_id="PAY1",
            payment_method="Square",
            amount=20,
            amount_available_to_refund=20,
            currency=self.pay_invoice.currency,
        )
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=30, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()

        seller = self._mock_seller()
        client = MagicMock()
        client.payments.get.return_value = SimpleNamespace(
            errors=None,
            payment=SimpleNamespace(
                id="PAY1",
                status="COMPLETED",
                receipt_number="RC",
                amount_money=SimpleNamespace(amount=2000, currency=self.pay_invoice.currency),
                location_id="LOC1",
                reference_id=str(self.pay_invoice.pk),
            ),
        )
        seller.get_square_client.return_value = client
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller):
            resp = self.client.post(
                self.confirm_url,
                {"invoice_pk": self.pay_invoice.pk, "payment_id": "PAY1", "idempotency_key": "i"},
                **self._bearer(self.admin_user),
            )
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["code"], "already_charged")
        self.assertIn("still due", body["detail"])
        # No second payment was recorded off the stale charge.
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.pay_invoice).count(), 1)


class SquareSellerRoutingTests(StandardTestCase):
    """Which Square account an auction's payments route to -- and which token Tap to Pay hands out.

    Auction invoices always carry ``club=None`` (that FK is for membership invoices), so anything
    resolving the seller from ``invoice.club`` alone silently falls through to the auction
    creator's *personal* Square account on a club auction. That both misroutes the club's money and
    ships a personal merchant token to whoever holds an is_admin AuctionTOS.
    """

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Routing Club")
        # self.user creates online_auction; give them a personal Square account to compete with the
        # club's, so a test failing over means money landed in the wrong account.
        self.creator_seller = SquareSeller.objects.create(
            user=self.user, square_merchant_id="CREATOR_MID", access_token="creator-tok", payer_email="c@example.com"
        )
        self.club_owner = User.objects.create_user("club_owner", "co@example.com", "pw")
        self.buyer = User.objects.create_user("routingbuyer", "rb@example.com", "pw")
        tos = AuctionTOS.objects.create(user=self.buyer, auction=self.online_auction, pickup_location=self.location)
        self.pay_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()

    def _connect_club_square(self):
        return SquareSeller.objects.create(
            user=self.club_owner,
            club=self.club,
            square_merchant_id="CLUB_MID",
            access_token="club-tok",
            payer_email="club@example.com",
        )

    def test_club_auction_routes_to_club_seller_not_creator(self):
        club_seller = self._connect_club_square()
        self.online_auction.club = self.club
        self.online_auction.save()
        self.assertEqual(self.online_auction.effective_square_seller, club_seller)
        self.assertEqual(self.online_auction.square_information, "CLUB_MID")

    def test_club_auction_falls_back_to_creator_when_club_has_no_square(self):
        # The club never connected Square, so the creator's account is the only way to take money.
        self.online_auction.club = self.club
        self.online_auction.save()
        self.assertEqual(self.online_auction.effective_square_seller, self.creator_seller)

    def test_non_club_auction_uses_creator_seller(self):
        self.assertIsNone(self.online_auction.club_id)
        self.assertEqual(self.online_auction.effective_square_seller, self.creator_seller)

    def test_auction_invoice_on_club_auction_resolves_club_seller(self):
        # The regression: invoice.club is None on an auction invoice, so resolving from the invoice
        # alone would hand back self.creator_seller here.
        from auctions.mobile.services.payments import PaymentService

        club_seller = self._connect_club_square()
        self.online_auction.club = self.club
        self.online_auction.save()
        self.pay_invoice.refresh_from_db()
        self.assertIsNone(self.pay_invoice.club_id)
        self.assertEqual(PaymentService._get_seller_for_invoice(self.pay_invoice), club_seller)

    def test_tap_to_pay_hands_out_club_token_not_creator_token(self):
        from auctions.mobile.services.payments import PaymentService
        from auctions.models import SQUARE_TAP_TO_PAY_SCOPE

        club_seller = self._connect_club_square()
        club_seller.scopes = SQUARE_TAP_TO_PAY_SCOPE
        club_seller.save()
        self.online_auction.club = self.club
        self.online_auction.save()
        with patch.object(SquareSeller, "get_location_id", return_value="LOC1"):
            result = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user)
        # "creator-tok" here would mean an auction admin was handed a personal merchant credential.
        self.assertEqual(result["access_token"], "club-tok")

    def test_web_payment_link_routes_to_club_seller(self):
        # SquareAPIMixin.create_payment_link had the identical mismatch as the mobile path.
        from auctions.views import SquareAPIMixin

        club_seller = self._connect_club_square()
        self.online_auction.club = self.club
        self.online_auction.save()
        self.pay_invoice.refresh_from_db()
        mixin = SquareAPIMixin()
        mixin.request = MagicMock()
        # autospec so the bound instance shows up as call_args[0][0] -- that is the assertion.
        with patch.object(
            SquareSeller, "create_payment_link", autospec=True, return_value=("https://sq/pay", None)
        ) as create_link:
            url, error = mixin.create_payment_link(self.pay_invoice)
        self.assertEqual(url, "https://sq/pay")
        self.assertIsNone(error)
        self.assertEqual(create_link.call_args[0][0], club_seller)

    def test_membership_invoice_uses_club_seller_only(self):
        # A membership invoice has no auction, so there is no creator to fall back to.
        from auctions.mobile.services.payments import PaymentService

        club_seller = self._connect_club_square()
        membership_invoice = Invoice.objects.create(club=self.club, buyer=self.buyer)
        self.assertEqual(PaymentService._get_seller_for_invoice(membership_invoice), club_seller)


class SquareTokenHandoutAuditTests(StandardTestCase):
    """Every create_mobile_payment that returns a token must leave a history entry.

    The response carries a merchant-wide OAuth token, so the audit entry is the only record that a
    given admin pulled the credential -- and a create with no matching payment is the signal.
    """

    def setUp(self):
        super().setUp()
        self.buyer = User.objects.create_user("auditbuyer", "ab@example.com", "pw")
        tos = AuctionTOS.objects.create(user=self.buyer, auction=self.online_auction, pickup_location=self.location)
        self.pay_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()

    def _mock_seller(self):
        seller = MagicMock()
        seller.get_valid_access_token.return_value = "tok"
        seller.get_location_id.return_value = "LOC1"
        return seller

    def test_successful_create_records_auction_history_with_admin_and_ip(self):
        from auctions.mobile.services.payments import PaymentService

        request = MagicMock()
        request.META = {"HTTP_X_FORWARDED_FOR": "203.0.113.7, 70.41.3.18"}
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._mock_seller()):
            PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user, request=request)
        history = AuctionHistory.objects.filter(auction=self.online_auction, user=self.admin_user).first()
        self.assertIsNotNone(history)
        self.assertIn("Square Tap to Pay access token issued", history.action)
        self.assertIn(str(self.pay_invoice.pk), history.action)
        self.assertIn("203.0.113.7", history.action)  # first X-Forwarded-For hop, not the proxy
        self.assertEqual(history.applies_to, "INVOICES")

    def test_denied_create_records_nothing(self):
        # No token left the building, so there is nothing to audit.
        from auctions.mobile.services.payments import PaymentService

        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._mock_seller()):
            with self.assertRaises(PermissionError):
                PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.buyer)
        self.assertFalse(AuctionHistory.objects.filter(action__contains="Square Tap to Pay access token").exists())

    def test_history_failure_does_not_block_payment(self):
        # A cashier must never be blocked from taking money by an audit write.
        from auctions.mobile.services.payments import PaymentService

        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._mock_seller()):
            with patch.object(Auction, "create_history", side_effect=Exception("db down")):
                result = PaymentService.create_mobile_payment(invoice_pk=self.pay_invoice.pk, user=self.admin_user)
        self.assertEqual(result["access_token"], "tok")
