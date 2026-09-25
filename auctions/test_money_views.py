"""The views that move money or decide who owes it: bulk invoice status changes (ready, paid), the lot
refund dialog, bulk-selling lots to the online high bidder, viewing and adjusting an invoice, creating
one, removing a bid, and the auctioneer's set-winners page with its undo. Each gets its permission gate,
the database effect of its happy path, and the inputs that would corrupt an invoice if let through.
"""

import datetime

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from auctions.models import (
    Auction,
    AuctionHistory,
    Bid,
    Invoice,
    InvoiceAdjustment,
    Lot,
    LotHistory,
)
from auctions.tests import StandardTestCase


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class MarkInvoicesReadyTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_invoices_ready", kwargs={"slug": self.online_auction.slug})

    def test_non_admin_is_refused_and_nothing_changes(self):
        self.client.force_login(self.userB)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {}).status_code, 403)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "DRAFT")

    def test_admin_sets_open_invoices_ready_and_schedules_notifications(self):
        other_auction_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        paid = Invoice.objects.get(pk=self.invoiceB.pk)
        paid.status = "PAID"
        paid.save()
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"send_invoice_ready_notification_emails": "on"})
        self.assertEqual(response.status_code, 200)
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.status, "UNPAID")
        self.assertIsNotNone(invoice.invoice_notification_due)
        self.assertTrue(PeriodicTask.objects.filter(kwargs__contains=str(invoice.pk)).exists())
        # Only DRAFT invoices in this auction move.
        self.assertEqual(Invoice.objects.get(pk=self.invoiceB.pk).status, "PAID")
        self.assertEqual(Invoice.objects.get(pk=other_auction_invoice.pk).status, "DRAFT")
        self.assertTrue(Auction.objects.get(pk=self.online_auction.pk).email_users_when_invoices_ready)
        self.assertTrue(AuctionHistory.objects.filter(auction=self.online_auction, applies_to="INVOICES").exists())

    def test_unchecked_email_box_turns_ready_emails_off(self):
        self.online_auction.email_users_when_invoices_ready = True
        self.online_auction.save()
        self.client.force_login(self.user)
        self.client.post(self.url, {})
        self.assertFalse(Auction.objects.get(pk=self.online_auction.pk).email_users_when_invoices_ready)

    def test_no_open_invoices_is_a_harmless_no_op(self):
        Invoice.objects.filter(auctiontos_user__auction=self.online_auction).update(status="PAID")
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Invoice.objects.filter(auctiontos_user__auction=self.online_auction, status="PAID").count(), 2)


class MarkInvoicesPaidTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_invoices_paid", kwargs={"slug": self.online_auction.slug})
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.status = "UNPAID"
        invoice.save()

    def test_non_admin_is_refused(self):
        self.client.force_login(self.user_who_does_not_join)
        self.assertEqual(self.client.post(self.url, {}).status_code, 403)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "UNPAID")

    def test_admin_marks_ready_invoices_paid_and_leaves_open_ones(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 200)
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.status, "PAID")
        self.assertIsNotNone(invoice.date_paid)
        self.assertEqual(Invoice.objects.get(pk=self.invoiceB.pk).status, "DRAFT")
        self.assertIn("1 invoice marked paid.", _messages(response))


class LotRefundDialogTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("lot_refund", kwargs={"pk": self.lot.pk})

    def test_non_admin_is_refused(self):
        self.client.force_login(self.userB)
        self.assertEqual(self.client.post(self.url, {"partial_refund_percent": 100}).status_code, 403)
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).partial_refund_percent, 0)

    def test_lot_without_a_seller_is_404(self):
        orphan = Lot.objects.create(lot_name="no seller", auction=self.online_auction, quantity=1)
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(reverse("lot_refund", kwargs={"pk": orphan.pk})).status_code, 404)

    def test_refund_reduces_what_the_winner_owes(self):
        before = Invoice.objects.get(pk=self.invoiceB.pk)
        before.recalculate()
        owed_before = before.net
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"partial_refund_percent": 50})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).partial_refund_percent, 50)
        self.assertTrue(LotHistory.objects.filter(lot=self.lot, message__contains="50% refund").exists())
        after = Invoice.objects.get(pk=self.invoiceB.pk)
        self.assertGreater(after.net, owed_before)

    def test_refund_over_100_percent_is_rejected(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"partial_refund_percent": 150})
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).partial_refund_percent, 0)

    def test_removing_an_unsold_lot(self):
        self.client.force_login(self.admin_user)
        self.client.post(reverse("lot_refund", kwargs={"pk": self.unsoldLot.pk}), {"banned": "on"})
        self.assertTrue(Lot.objects.get(pk=self.unsoldLot.pk).banned)


class BulkSetLotsWonTests(StandardTestCase):
    """In-person lots: their bids never age out, since the lot has no end until it's sold."""

    def setUp(self):
        super().setUp()
        self.url = reverse("bulk_set_lots_won", kwargs={"slug": self.in_person_auction.slug})
        self.other_lot = Lot.objects.create(
            lot_name="Guppies",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
        )
        Bid.objects.create(user=self.user_with_no_lots, lot_number=self.in_person_lot, amount=20)
        Bid.objects.create(user=self.user_with_no_lots, lot_number=self.other_lot, amount=30)

    def test_non_admin_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.post(self.url, {"got_it": "on"}).status_code, 403)
        self.assertIsNone(Lot.objects.get(pk=self.in_person_lot.pk).auctiontos_winner)

    def test_sells_only_the_lots_matching_the_filter(self):
        high_bid = Lot.objects.get(pk=self.other_lot.pk).high_bid
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"got_it": "on", "query": "Guppies"})
        self.assertEqual(response.status_code, 200)
        sold = Lot.objects.get(pk=self.other_lot.pk)
        self.assertEqual(sold.auctiontos_winner, self.in_person_buyer)
        self.assertEqual(sold.winning_price, high_bid)
        self.assertIsNone(Lot.objects.get(pk=self.in_person_lot.pk).auctiontos_winner)

    def test_without_confirmation_nothing_sells(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"query": "Guppies"})
        self.assertIsNone(Lot.objects.get(pk=self.other_lot.pk).auctiontos_winner)

    def test_lot_without_bids_stays_unsold(self):
        no_bids = Lot.objects.create(
            lot_name="Nobody wants this",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
        )
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"got_it": "on"})
        no_bids = Lot.objects.get(pk=no_bids.pk)
        self.assertIsNone(no_bids.auctiontos_winner)
        self.assertIsNone(no_bids.winning_price)
        self.assertEqual(Lot.objects.get(pk=self.in_person_lot.pk).auctiontos_winner, self.in_person_buyer)


class InvoiceViewTests(StandardTestCase):
    def url(self, invoice):
        return reverse("invoice_by_pk", kwargs={"pk": invoice.pk})

    def adjustment_post(self, amount):
        return {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-adjustment_type": "ADD",
            "form-0-amount": str(amount),
            "form-0-notes": "club dues",
        }

    def test_stranger_is_sent_home(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(self.url(self.invoiceB))
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_owner_sees_it_and_it_is_marked_opened(self):
        self.client.force_login(self.userB)
        response = self.client.get(self.url(self.invoiceB))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Invoice.objects.get(pk=self.invoiceB.pk).opened)

    def test_admin_sees_it_without_marking_it_opened(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url(self.invoiceB))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_admin"])
        self.assertFalse(Invoice.objects.get(pk=self.invoiceB.pk).opened)

    def test_admin_adds_an_adjustment(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url(self.invoice), self.adjustment_post(5))
        self.assertRedirects(response, self.url(self.invoice), fetch_redirect_response=False)
        adjustment = InvoiceAdjustment.objects.get(invoice=self.invoice)
        self.assertEqual(adjustment.amount, 5)
        self.assertEqual(adjustment.user, self.admin_user)

    def test_owner_cannot_adjust_their_own_invoice(self):
        self.client.force_login(self.userB)
        count = InvoiceAdjustment.objects.filter(invoice=self.invoiceB).count()
        response = self.client.post(self.url(self.invoiceB), self.adjustment_post(-500))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(self.url(self.invoiceB), self.adjustment_post(5))
        self.assertEqual(InvoiceAdjustment.objects.filter(invoice=self.invoiceB).count(), count)

    def test_negative_adjustment_is_rejected(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url(self.invoice), self.adjustment_post(-5))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice=self.invoice).exists())

    def test_no_invoice_in_auction_redirects_to_the_auction(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(reverse("my_auction_invoice", kwargs={"slug": self.online_auction.slug}))
        self.assertRedirects(response, self.online_auction.get_absolute_url(), fetch_redirect_response=False)

    # The invoice_by_pk route has no slug; reading one was a KeyError and a 500.
    def test_unknown_invoice_pk_is_404(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("invoice_by_pk", kwargs={"pk": 999999}))
        self.assertEqual(response.status_code, 404)


class InvoiceCreateViewTests(StandardTestCase):
    def url(self, tos):
        return reverse("create_invoice", kwargs={"pk": tos.pk})

    def test_non_admin_is_refused(self):
        self.client.force_login(self.userB)
        self.assertEqual(self.client.get(self.url(self.tosC)).status_code, 403)
        self.assertFalse(Invoice.objects.filter(auctiontos_user=self.tosC).exists())

    def test_admin_creates_an_invoice(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url(self.tosC))
        invoice = Invoice.objects.get(auctiontos_user=self.tosC)
        self.assertEqual(invoice.auction, self.online_auction)
        self.assertRedirects(response, invoice.get_absolute_url(), fetch_redirect_response=False)

    def test_existing_invoice_is_reused(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url(self.tosB))
        self.assertRedirects(response, self.invoiceB.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(Invoice.objects.filter(auctiontos_user=self.tosB).count(), 1)

    def test_unknown_participant_goes_home(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("create_invoice", kwargs={"pk": 999999}))
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)


class BidDeleteTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.live_auction = Auction.objects.create(
            created_by=self.user,
            title="Live online auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        self.live_lot = Lot.objects.create(
            lot_name="Live lot",
            auction=self.live_auction,
            user=self.user,
            quantity=1,
            active=True,
        )
        self.bid = Bid.objects.create(user=self.userB, lot_number=self.live_lot, amount=10)
        self.url = reverse("delete_bid", kwargs={"pk": self.bid.pk})

    def test_stranger_cannot_remove_a_bid(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.post(self.url)
        self.assertRedirects(response, self.live_lot.get_absolute_url(), fetch_redirect_response=False)
        self.assertFalse(Bid.objects.get(pk=self.bid.pk).is_deleted)

    def test_bidder_cannot_remove_own_bid_unless_auction_allows_it(self):
        self.client.force_login(self.userB)
        self.client.post(self.url)
        self.assertFalse(Bid.objects.get(pk=self.bid.pk).is_deleted)
        self.live_auction.allow_deleting_bids = True
        self.live_auction.save()
        self.client.post(self.url)
        self.assertTrue(Bid.objects.get(pk=self.bid.pk).is_deleted)

    def test_admin_removes_a_bid_and_it_is_recorded(self):
        older = Bid.objects.create(user=self.userB, lot_number=self.live_lot, amount=5)
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertRedirects(response, self.live_lot.get_absolute_url(), fetch_redirect_response=False)
        self.assertTrue(Bid.objects.get(pk=self.bid.pk).is_deleted)
        # The bidder's other bids on this lot go too, or the next-highest of theirs would become the high bid.
        self.assertTrue(Bid.objects.get(pk=older.pk).is_deleted)
        self.assertTrue(LotHistory.objects.filter(lot=self.live_lot, message__contains="removed").exists())

    def test_removing_the_bid_on_a_lot_bought_early_reopens_it(self):
        self.live_lot.winner = self.userB
        self.live_lot.auctiontos_winner = self.tosB
        self.live_lot.winning_price = 10
        self.live_lot.active = False
        self.live_lot.save()
        self.client.force_login(self.user)
        self.client.post(self.url)
        lot = Lot.objects.get(pk=self.live_lot.pk)
        self.assertIsNone(lot.auctiontos_winner)
        self.assertIsNone(lot.winning_price)
        self.assertTrue(lot.active)

    def test_bids_on_a_closed_auction_stay(self):
        bid = Bid.objects.create(user=self.userB, lot_number=self.lot, amount=10)
        self.client.force_login(self.user)
        self.client.post(reverse("delete_bid", kwargs={"pk": bid.pk}))
        self.assertFalse(Bid.objects.get(pk=bid.pk).is_deleted)

    def test_bid_on_a_lot_outside_any_auction_is_refused_not_a_500(self):
        lot = Lot.objects.create(
            lot_name="Standalone",
            user=self.user,
            quantity=1,
            active=True,
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        bid = Bid.objects.create(user=self.userB, lot_number=lot, amount=10)
        self.client.force_login(self.userB)
        response = self.client.post(reverse("delete_bid", kwargs={"pk": bid.pk}))
        self.assertRedirects(response, lot.get_absolute_url(), fetch_redirect_response=False)
        self.assertFalse(Bid.objects.get(pk=bid.pk).is_deleted)


class DynamicSetLotWinnerTests(StandardTestCase):
    """in_person_auction uses seller-dash numbering: in_person_lot is "101-1", in_person_buyer is 555."""

    def setUp(self):
        super().setUp()
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        self.client.force_login(self.admin_user)

    def post(self, action="save", lot="101-1", price="10", winner="555"):
        return self.client.post(self.url, {"lot": lot, "price": price, "winner": winner, "action": action}).json()

    def assert_unsold(self):
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertIsNone(lot.auctiontos_winner)
        self.assertIsNone(lot.winning_price)

    def test_non_admin_cannot_save(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(self.url, {"lot": "101-1", "price": "10", "winner": "555", "action": "save"})
        self.assertEqual(response.status_code, 403)
        self.assert_unsold()

    def test_save_sells_the_lot(self):
        data = self.post()
        self.assertEqual(data["lot"], "valid")
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertEqual(lot.auctiontos_winner, self.in_person_buyer)
        self.assertEqual(lot.winning_price, 10)
        self.assertFalse(lot.active)

    def test_unknown_lot(self):
        data = self.post(lot="999-9")
        self.assertEqual(data["lot"], "No lot found")
        self.assertIsNone(data["success_message"])

    def test_unknown_bidder(self):
        data = self.post(winner="9999")
        self.assertEqual(data["winner"], "No bidder found")
        self.assert_unsold()

    def test_price_below_reserve_needs_force_save(self):
        self.in_person_lot.reserve_price = 20
        self.in_person_lot.save()
        data = self.post(price="5")
        self.assertIn("minimum bid", data["price"])
        self.assert_unsold()
        self.post(action="force_save", price="5")
        self.assertEqual(Lot.objects.get(pk=self.in_person_lot.pk).winning_price, 5)

    def test_bad_prices_are_rejected_even_with_force_save(self):
        for price in ("-5", "NaN", "Infinity", "1000000", "1e9", "9" * 60):
            for action in ("save", "force_save"):
                with self.subTest(price=price, action=action):
                    data = self.post(action=action, price=price)
                    self.assertNotEqual(data["price"], "valid")
                    self.assertIsNone(data["success_message"])
                    self.assert_unsold()

    def test_winner_with_a_closed_invoice(self):
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        invoice.status = "PAID"
        invoice.save()
        data = self.post()
        self.assertEqual(data["winner"], "This user's invoice is not open")
        self.assert_unsold()

    def test_seller_with_a_closed_invoice(self):
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.admin_in_person_tos)
        invoice.status = "UNPAID"
        invoice.save()
        data = self.post()
        self.assertEqual(data["lot"], "The seller's invoice is not open")
        self.assert_unsold()

    def test_already_sold_to_someone_else_is_not_overwritten(self):
        self.post()
        data = self.post(price="15", winner="504")
        self.assertEqual(data["banner"], "error")
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertEqual(lot.auctiontos_winner, self.in_person_buyer)
        self.assertEqual(lot.winning_price, 10)

    def test_same_sale_entered_twice_is_double_checked(self):
        self.post()
        data = self.post()
        self.assertEqual(data["success_message"], "This lot has been double checked")
        self.assertTrue(Lot.objects.get(pk=self.in_person_lot.pk).admin_validated)

    def test_end_unsold(self):
        data = self.post(action="end_unsold", price="", winner="")
        self.assertIsNotNone(data["success_message"])
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertFalse(lot.active)
        self.assertIsNone(lot.auctiontos_winner)

    def test_undo_clears_the_sale(self):
        self.post()
        undo_url = reverse("auction_unsell_lot", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.post(undo_url, {"lot_number": "101-1"})
        self.assertEqual(response.json()["hide_undo_button"], "true")
        self.assert_unsold()
        self.assertTrue(Lot.objects.get(pk=self.in_person_lot.pk).active)
        self.assertEqual(self.client.post(undo_url, {"lot_number": "999-9"}).json()["message"], "No lot found")

    def test_non_admin_cannot_undo(self):
        self.post()
        self.client.force_login(self.user_with_no_lots)
        undo_url = reverse("auction_unsell_lot", kwargs={"slug": self.in_person_auction.slug})
        self.assertEqual(self.client.post(undo_url, {"lot_number": "101-1"}).status_code, 403)
        self.assertEqual(Lot.objects.get(pk=self.in_person_lot.pk).winning_price, 10)
