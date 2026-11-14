"""Recording a sale: the one code path behind the set-winners page, the palette and the app's offline sync.

Covers ``auctions/sales.py`` and how the three ways in agree: a second sale is refused, a $0 sale is a sale,
a stale clerk gets the banner rather than a second sale, and the side effects that need the sale committed
wait for the commit.
"""

from decimal import Decimal
from unittest.mock import PropertyMock, patch

from django.test import RequestFactory
from django.urls import reverse

from auctions import sales
from auctions.mobile.services.offline import apply_ops
from auctions.models import AuctionHistory, AuctionTOS, Invoice, Lot
from auctions.tests import StandardTestCase
from auctions.views.selling import DynamicSetLotWinner


class RecordSaleTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.lot = self.in_person_lot
        self.buyer = self.in_person_buyer  # bidder 555
        self.other = self.in_person_tos  # bidder 504

    def test_second_sale_raises_already_sold(self):
        sales.record_sale(self.lot, self.buyer, Decimal(10), self.admin_user)
        with self.assertRaises(sales.AlreadySold) as ctx:
            sales.record_sale(Lot.objects.get(pk=self.lot.pk), self.other, Decimal(12), self.admin_user)
        self.assertEqual(ctx.exception.lot.auctiontos_winner_id, self.buyer.pk)

    def test_force_overwrites_and_recalculates_old_invoice(self):
        sales.record_sale(self.lot, self.buyer, Decimal(10), self.admin_user)
        old_invoice = Invoice.for_participant(self.buyer, self.in_person_auction)
        before = Invoice.objects.get(pk=old_invoice.pk).calculated_total

        sales.record_sale(Lot.objects.get(pk=self.lot.pk), self.other, Decimal(12), self.admin_user, force=True)

        self.lot.refresh_from_db()
        self.assertEqual(self.lot.auctiontos_winner_id, self.other.pk)
        after = Invoice.objects.get(pk=old_invoice.pk)
        self.assertNotEqual(before, after.calculated_total)
        self.assertEqual(after.calculated_total, after.rounded_net)

    def test_zero_dollar_is_not_sold(self):
        sales.record_sale(self.lot, self.buyer, Decimal(0), self.admin_user)
        fresh = Lot.objects.get(pk=self.lot.pk)
        self.assertFalse(sales.is_sold(fresh))
        # A $0 winner can be replaced by a real sale without force.
        sales.record_sale(fresh, self.other, Decimal(12), self.admin_user)
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).auctiontos_winner_id, self.other.pk)

    def test_after_commit_effects_wait_for_commit(self):
        with (
            patch("auctions.sales.notify_running_total") as notify,
            self.captureOnCommitCallbacks(execute=True) as callbacks,
        ):
            sales.record_sale(self.lot, self.buyer, Decimal(10), self.admin_user)
            notify.assert_not_called()
        self.assertTrue(callbacks)
        notify.assert_called_once()

    def test_stale_commit_gets_the_banner_not_a_second_sale(self):
        # Someone else recorded the sale after this clerk loaded the lot, so the in-memory copy is unsold.
        stale = Lot.objects.get(pk=self.lot.pk)
        Lot.objects.filter(pk=self.lot.pk).update(auctiontos_winner=self.other, winning_price=Decimal(10))

        view = DynamicSetLotWinner()
        view.request = RequestFactory().post("/")
        view.request.user = self.admin_user
        view.auction = self.in_person_auction
        view.kwargs = {}
        result = {"success_message": None}
        view.commit_winner(stale, self.buyer, Decimal(12), "save", result)

        self.assertEqual(result["banner"], "error")
        self.assertIn("already sold for", result["success_message"])
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).auctiontos_winner_id, self.other.pk)


class SetWinnerWebTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username=self.admin_user.username, password="testpassword")

    def _post(self, lot="101-1", **data):
        return self.client.post(self.url, data={"lot": lot, **data}).json()

    def test_web_second_clerk_gets_already_sold_banner(self):
        self._post(price="10", winner="555", action="save")
        second = self._post(price="12", winner="504", action="save")
        self.assertEqual(second["banner"], "error")
        self.assertIn("already sold for", second["success_message"])
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertEqual(lot.winning_price, Decimal(10))
        self.assertEqual(lot.auctiontos_winner_id, self.in_person_buyer.pk)

    def test_same_winner_and_price_is_a_double_check(self):
        self._post(price="10", winner="555", action="save")
        again = self._post(price="10", winner="555", action="save")
        self.assertEqual(again["success_message"], "This lot has been double checked")
        self.assertNotIn("banner", again)

    def test_zero_dollar_winner_is_not_refused_and_zero_without_winner_ends_unsold(self):
        self._post(price="0", winner="555", action="save")
        second = self._post(price="12", winner="504", action="save")
        self.assertNotIn("banner", second)
        self.assertEqual(second["price"], "valid")
        self.assertEqual(Lot.objects.get(pk=self.in_person_lot.pk).winning_price, Decimal(12))

        # Price 0 with no winner is how the page says "unsold".
        Lot.objects.filter(pk=self.in_person_lot.pk).update(auctiontos_winner=None, winning_price=None)
        self._post(price="0", winner="", action="save")
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertIsNone(lot.auctiontos_winner_id)
        self.assertTrue(lot.ended_unsold)

    def test_offline_and_web_share_side_effects(self):
        with patch.object(
            type(self.in_person_auction), "use_check_in_mode", new_callable=PropertyMock, return_value=True
        ):
            results = apply_ops(
                self.in_person_auction,
                self.admin_user,
                [{"op_id": "sold-1", "type": "set_winner", "lot": "101-1", "winner": "555", "winning_price": "10.00"}],
            )
            self.assertEqual(results[0]["status"], "applied")
            seller = AuctionTOS.objects.get(pk=self.admin_in_person_tos.pk)
            self.assertIsNotNone(seller.checked_in)
            self.assertTrue(
                AuctionHistory.objects.filter(
                    auction=self.in_person_auction, action__startswith="Checked in", action__endswith="(lot sold)"
                ).exists()
            )

            web_seller = AuctionTOS.objects.create(
                auction=self.in_person_auction,
                pickup_location=self.in_person_location,
                name="Jane Doe",
                bidder_number="77",
            )
            Lot.objects.create(
                lot_name="Web lot",
                auction=self.in_person_auction,
                auctiontos_seller=web_seller,
                quantity=1,
                custom_lot_number="102-9",
            )
            data = self._post(lot="102-9", price="10", winner="555", action="save")
            self.assertNotIn("banner", data)
            web_seller.refresh_from_db()
            self.assertIsNotNone(web_seller.checked_in)
            self.assertTrue(
                AuctionHistory.objects.filter(auction=self.in_person_auction, action="Set lot 102-9 as sold").exists()
            )


class OfflineSaleTests(StandardTestCase):
    def test_offline_conflict_when_server_sold(self):
        self.in_person_lot.auctiontos_winner = self.in_person_tos
        self.in_person_lot.winning_price = Decimal(10)
        self.in_person_lot.save()
        results = apply_ops(
            self.in_person_auction,
            self.admin_user,
            [{"op_id": "c1", "type": "set_winner", "lot": "101-1", "winner": "555", "winning_price": "12.00"}],
        )
        self.assertEqual(results[0]["status"], "conflict")
        self.assertEqual(results[0]["conflict"], "winner_conflict")
        self.assertEqual(Lot.objects.get(pk=self.in_person_lot.pk).auctiontos_winner_id, self.in_person_tos.pk)

    def test_offline_same_winner_and_price_is_already_applied(self):
        self.in_person_lot.auctiontos_winner = self.in_person_tos
        self.in_person_lot.winning_price = Decimal(10)
        self.in_person_lot.save()
        results = apply_ops(
            self.in_person_auction,
            self.admin_user,
            [{"op_id": "c2", "type": "set_winner", "lot": "101-1", "winner": "504", "winning_price": "10.00"}],
        )
        self.assertEqual(results[0]["status"], "already_applied")
