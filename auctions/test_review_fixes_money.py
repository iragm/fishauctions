"""WP2 regression tests: Square refund amounts and keys, refund webhook redelivery, the PAID total freeze,
the join and numbering lock, the club member list paging, and the welcome-letter override on switching.
"""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubAPIKey,
    ClubMember,
    Invoice,
    InvoicePayment,
    Lot,
    SquareSeller,
)
from auctions.services import join_auction, switch_to_club_managed
from auctions.tests import StandardTestCase


def _square_seller(user, merchant_id):
    return SquareSeller.objects.create(
        user=user,
        square_merchant_id=merchant_id,
        access_token="TEST_ACCESS_TOKEN",
        refresh_token="TEST_REFRESH_TOKEN",
        token_expires_at=timezone.now() + datetime.timedelta(days=30),
    )


class SquareRefundAmountTests(StandardTestCase):
    """The Square refund is the PayPal amount (tax included, half-up) and carries a deterministic key."""

    def setUp(self):
        super().setUp()
        self.square_payment = InvoicePayment.objects.create(
            invoice=self.invoiceB,
            payment_method="square",
            amount=Decimal("100.00"),
            amount_available_to_refund=Decimal("100.00"),
            external_id="PAY_SQUARE_REFUND_TEST",
        )
        self.seller = _square_seller(self.admin_user, "MERCHANT_REFUND_AMOUNTS")
        # The lot's own price is 10 on a 25% tax auction: a 33% refund is 4.125 with tax, 4.13 half-up.
        self.lot.winning_price = 10
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

    def test_square_refund_includes_tax_and_rounds(self):
        with (
            patch.object(Auction, "effective_square_seller", self.seller),
            patch.object(SquareSeller, "process_refund", return_value=None) as process,
        ):
            error = Lot.objects.get(pk=self.lot.pk).square_refund(33)
        self.assertIsNone(error)
        refund_amount = process.call_args.args[1]
        self.assertEqual(refund_amount, Decimal("4.13"))

    def test_process_refund_sends_half_up_cents(self):
        client = MagicMock()
        with patch.object(SquareSeller, "get_square_client", return_value=client):
            error = self.seller.process_refund(self.square_payment, Decimal("4.125"), "reason", "KEY-1")
        self.assertIsNone(error)
        sent = client.refunds.refund_payment.call_args.kwargs
        self.assertEqual(sent["amount_money"]["amount"], 413)
        self.assertEqual(sent["idempotency_key"], "KEY-1")

    def test_square_refund_key_is_deterministic(self):
        client = MagicMock()
        with (
            patch.object(Auction, "effective_square_seller", self.seller),
            patch.object(SquareSeller, "get_square_client", return_value=client),
        ):
            Lot.objects.get(pk=self.lot.pk).square_refund(50)
            Lot.objects.get(pk=self.lot.pk).square_refund(50)
            Lot.objects.get(pk=self.lot.pk).square_refund(100)
        keys = [call.kwargs["idempotency_key"] for call in client.refunds.refund_payment.call_args_list]
        self.assertEqual(keys[0], f"af-lot-{self.lot.pk}-refund-0-50")
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])


class SquareRefundWebhookTests(StandardTestCase):
    """A redelivered refund.updated webhook takes the refund off the payment once."""

    def setUp(self):
        super().setUp()
        self.payment = InvoicePayment.objects.create(
            invoice=self.invoiceB,
            payment_method="square",
            amount=Decimal("100.00"),
            amount_available_to_refund=Decimal("100.00"),
            external_id="PAY_RF",
        )
        _square_seller(self.admin_user, "MERCHANT_RF")

    def test_refund_webhook_redelivery_subtracts_once(self):
        refund = {
            "id": "REFUND_RF",
            "payment_id": "PAY_RF",
            "status": "COMPLETED",
            "amount_money": {"amount": 1000, "currency": "USD"},
            "reason": "test",
        }
        body = {"merchant_id": "MERCHANT_RF", "type": "refund.updated", "data": {"object": {"refund": refund}}}
        for _ in range(2):
            with patch.object(SquareSeller, "get_square_client", return_value=MagicMock()):
                response = self.client.post(reverse("square_webhook"), data=body, content_type="application/json")
            self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount_available_to_refund, Decimal("90.00"))
        self.assertEqual(InvoicePayment.objects.filter(external_id="REFUND_RF").count(), 1)


class InvoiceRecalculateFreezeTests(StandardTestCase):
    def test_recalculate_leaves_paid_total(self):
        Invoice.objects.filter(pk=self.invoiceB.pk).update(status="PAID", calculated_total=Decimal("1.00"))
        invoice = Invoice.objects.get(pk=self.invoiceB.pk)
        invoice.recalculate()
        self.assertEqual(Invoice.objects.get(pk=self.invoiceB.pk).calculated_total, Decimal("1.00"))

    def test_recalculate_updates_unpaid_total(self):
        Invoice.objects.filter(pk=self.invoiceB.pk).update(calculated_total=Decimal("999.00"))
        invoice = Invoice.objects.get(pk=self.invoiceB.pk)
        invoice.recalculate()
        self.assertNotEqual(Invoice.objects.get(pk=self.invoiceB.pk).calculated_total, Decimal("999.00"))


class JoinAuctionOnceTests(StandardTestCase):
    def test_join_twice_makes_one_row(self):
        joiner = User.objects.create_user(username="joiner", password="testpassword", email="joiner@example.com")
        first, created_first, problem = join_auction(joiner, self.online_auction, self.location)
        second, created_second, problem_again = join_auction(joiner, self.online_auction, self.location)
        self.assertEqual((problem, problem_again), ("", ""))
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction, user=joiner).count(), 1)


class ClubMemberListPagingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="paging_owner", password="testpass", email="owner@example.com")
        self.club = Club.objects.create(name="Paging Club")
        self.url = reverse("api_club_members", kwargs={"slug": self.club.slug})
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        ClubAPIKey.objects.create(
            club=self.club,
            name="Paging Key",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.owner,
            can_read_club_member_list=True,
        )
        self.headers = {"HTTP_X_API_KEY": raw_key}
        self._add_members(3)

    def _add_members(self, count):
        start = ClubMember.objects.filter(club=self.club).count()
        for i in range(start, start + count):
            ClubMember.objects.create(club=self.club, name=f"Member {i}", email=f"member{i}@example.com")

    def test_member_list_limit_offset(self):
        unpaged = self.client.get(self.url, **self.headers)
        self.assertEqual(unpaged.status_code, 200)
        self.assertEqual(len(unpaged.json()), 3)
        page = self.client.get(self.url, {"limit": 2, "offset": 2}, **self.headers)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json()["count"], 3)
        self.assertEqual(len(page.json()["results"]), 1)

    def test_member_list_query_count(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url, **self.headers)
        self._add_members(10)
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url, **self.headers)
        self.assertEqual(len(large.captured_queries), len(small.captured_queries))


class SwitchToClubManagedWelcomeTests(StandardTestCase):
    def test_switch_to_club_managed_sends_no_welcome(self):
        from auctions.forms import AuctionEditForm

        club = Club.objects.create(name="Switch Club")
        auction = Auction.objects.create(
            created_by=self.user,
            title="Copied auction",
            club=club,
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=2),
            copy_users_when_copying_this_auction=True,
            send_club_welcome_letter=True,
        )
        AuctionTOS.objects.create(auction=auction, name="Jane Doe", email="jane@example.com", bidder_number="701")
        with (
            patch("auctions.views.base.check_club_permission", return_value=True),
            patch.object(AuctionEditForm, "_rebuild_auctiontos_from_club"),
        ):
            self.assertTrue(switch_to_club_managed(auction, self.admin_user))
        member = ClubMember.objects.get(club=club, email="jane@example.com")
        self.assertFalse(member.send_welcome_email)
