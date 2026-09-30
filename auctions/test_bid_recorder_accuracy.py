import datetime
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from auctions import test_lot_money_fixes as money
from auctions.models import AuctionHistory, Lot, LotHistory
from auctions.test_lot_money_fixes import InPersonSaleBase
from auctions.test_support import isolated_cache


class SalesRecordedTests(InPersonSaleBase):
    def test_a_sale_recorded_once_is_accurate(self):
        self.sell()
        self.assertEqual(self.sold_lot().sales_recorded, 1)
        self.assertEqual(self.in_person_auction.bid_recorder_accuracy, {"lots": 1, "unchanged": 1, "percent": 100})

    def test_a_corrected_winner_is_not(self):
        self.sell()
        self.sell(action="force_save", price="15", winner="504")
        self.assertEqual(self.sold_lot().sales_recorded, 2)
        self.assertEqual(self.in_person_auction.bid_recorder_accuracy["percent"], 0)

    def test_unsold_and_online_lots_are_not_counted(self):
        self.assertIsNone(self.in_person_auction.bid_recorder_accuracy)
        self.assertIsNone(self.online_auction.bid_recorder_accuracy)


class LotAdminPriceEditTests(InPersonSaleBase):
    url = money.LotAdminTests.url
    data = money.LotAdminTests.data

    def test_changing_only_the_price_counts(self):
        self.sell()
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="12"))
        self.assertEqual(self.sold_lot().winning_price, 12)
        self.assertEqual(self.sold_lot().sales_recorded, 2)

    def test_saving_without_a_change_does_not(self):
        self.sell()
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="10"))
        self.assertEqual(self.sold_lot().sales_recorded, 1)


@isolated_cache("bid-recorder-accuracy")
class BackfillSalesRecordedTests(InPersonSaleBase):
    def backfill(self):
        call_command("backfill_sales_recorded", stdout=StringIO())
        return self.sold_lot().sales_recorded

    def edit(self, labels, changed_fields=None, when=None):
        row = AuctionHistory.objects.create(
            auction=self.in_person_auction,
            applies_to="LOTS",
            action=f"Edited lot 101-1: {labels}",
            changed_fields=changed_fields or {},
        )
        if when:
            AuctionHistory.objects.filter(pk=row.pk).update(timestamp=when)

    def test_winner_messages_are_counted(self):
        self.sell()
        self.sell(action="force_save", price="15", winner="504")
        Lot.objects.filter(pk=self.in_person_lot.pk).update(sales_recorded=0)
        self.assertEqual(self.backfill(), 2)
        self.assertEqual(self.backfill(), 2)

    def test_old_price_only_edits_are_counted(self):
        self.sell()
        LotHistory.objects.filter(lot=self.in_person_lot).update(timestamp=timezone.now() - datetime.timedelta(hours=2))
        self.edit("winning price")
        self.edit("Lot name, winning price", {"lot_name": {}, "winning_price": {}})
        self.edit("Winner, winning price")
        self.edit("Lot name")
        self.assertEqual(self.backfill(), 3)

    def test_a_price_edit_that_wrote_a_winner_message_counts_once(self):
        self.sell()
        self.edit("winning price", {"winning_price": {}})
        self.assertEqual(self.backfill(), 1)

    def test_the_cached_stats_and_the_guide_pick_it_up(self):
        from auctions.help_stats import _site_stats, auction_facts

        self.sell()
        self.in_person_auction.cached_stats = {"misc": {}}
        self.in_person_auction.save()
        self.backfill()
        self.in_person_auction.refresh_from_db()
        accuracy = self.in_person_auction.cached_stats["misc"]["bid_recorder_accuracy"]
        self.assertEqual(accuracy["percent"], 100)
        self.assertNotIn("recorded_once", auction_facts(self.in_person_auction))
        self.in_person_auction.cached_stats["misc"]["bid_recorder_accuracy"]["lots"] = 40
        self.assertEqual(auction_facts(self.in_person_auction)["recorded_once"], 100)

        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(days=30)
        self.in_person_auction.save()
        self.assertEqual(_site_stats(min_lots=1)["recorded_once"], 100)

    def test_the_stats_page_and_the_guide_show_it(self):
        from django.core.cache import cache

        from auctions import help_guides
        from auctions.help_stats import SITE_CACHE_KEY

        self.sell()
        response = self.client.get(reverse("auction_stats", kwargs={"slug": self.in_person_auction.slug}))
        self.assertContains(response, "Bid recorder accuracy")
        self.assertContains(response, "100% (1 of 1)")

        cache.set(SITE_CACHE_KEY, {"recorded_once": 95})
        response = self.client.get(help_guides.GUIDES["run-an-in-person-auction"].url)
        self.assertContains(response, "95% of lots sold here are recorded once and never changed.")
