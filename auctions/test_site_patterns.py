"""The help's site-wide tables and charts, and the two things collected for the auction stats page: invoice
emails opened, and bids started and not placed.
"""

import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from auctions import help_guides
from auctions.friction_models import AbandonedBid
from auctions.models import Auction, AuctionTOS, Bid, BlogPost, Club, Invoice, Lot, Species, VoiceCommandLog
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


def ago(**delta):
    return timezone.now() - datetime.timedelta(**delta)


class SiteCountsTests(StandardTestCase):
    def online(self, ends, days=7, lots=4, sold=2):
        auction = Auction.objects.create(
            created_by=self.user, title="Counted", is_online=True, date_start=ends - datetime.timedelta(days=days)
        )
        Auction.objects.filter(pk=auction.pk).update(date_end=ends)
        seller = AuctionTOS.objects.create(auction=auction, pickup_location=self.location, name="Seller")
        for i in range(lots):
            Lot.objects.create(
                lot_name=f"lot {i}", auction=auction, auctiontos_seller=seller, winning_price=10 if i < sold else None
            )
        return auction

    def test_online_timing_groups_by_end_day_and_length(self):
        from auctions.help_stats import _online_timing

        # 2026-06-07 was a Sunday; 3am UTC is still Saturday evening in New York.
        saturday = datetime.datetime(2026, 6, 7, 3, tzinfo=datetime.UTC)
        self.user.userdata.timezone = "America/New_York"
        self.user.userdata.save()
        for _ in range(2):
            self.online(saturday, days=7, sold=4)
            self.online(saturday + datetime.timedelta(days=2), days=2, sold=1)
        timing = _online_timing(min_lots=4, min_auctions=2)
        self.assertEqual(
            timing["weekdays"],
            [
                {"label": "Monday", "auctions": 2, "sold": 25},
                {"label": "Saturday", "auctions": 2, "sold": 100},
            ],
        )
        self.assertEqual([row["label"] for row in timing["lengths"]], ["3 days or less", "4 to 7 days"])
        self.assertEqual(_online_timing(min_lots=4, min_auctions=3), {})

    def test_a_sellers_later_lots_are_counted_in_order(self):
        from auctions.help_stats import _seller_rank

        auction = self.online(ago(days=30), lots=0)
        seller = AuctionTOS.objects.create(auction=auction, pickup_location=self.location, name="Big seller")
        for i in range(35):
            Lot.objects.create(
                lot_name=f"bag {i}",
                auction=auction,
                auctiontos_seller=seller,
                winning_price=20 if i < 10 else 5 if i < 25 else None,
            )
        rows = _seller_rank(min_lots=5, min_seller_lots=20)["rows"]
        self.assertEqual(
            [(row["label"], row["lots"], row["sold"], row["median"]) for row in rows],
            [("1st to 10th", 10, 100, 20.0), ("11th to 20th", 10, 100, 5.0), ("21st to 30th", 10, 50, 5.0)],
        )

    def test_bids_cluster_on_round_numbers(self):
        from auctions.help_stats import _bid_amounts

        auction = self.online(ago(days=30), lots=6, sold=0)
        lots = list(Lot.objects.filter(auction=auction))
        for lot, amount in zip(lots, (5, 10, 10, 11, 15, 70), strict=True):
            Bid.objects.create(user=self.user, lot_number=lot, amount=amount)
        chart = _bid_amounts(min_bids=1, dollars=20)
        self.assertEqual(chart["bids"], 5)
        self.assertEqual(chart["favourite"], 10)
        self.assertEqual(chart["round_pct"], 80)
        self.assertEqual(chart["percents"][9], 40.0)
        self.assertEqual(_bid_amounts(min_bids=6, dollars=20), {})

    def test_bred_species_skip_a_clubs_own(self):
        from auctions.help_stats import _bred_species

        auction = self.online(ago(days=30), lots=0)
        seller = AuctionTOS.objects.create(auction=auction, pickup_location=self.location, name="Breeder")
        club = Club.objects.create(name="A club")
        for name, count, owner in (("Guppy", 3, None), ("Platy", 2, None), ("Molly", 4, None), ("Secret", 5, club)):
            species = Species.objects.create(common_name=name, scientific_name=f"{name} sp.", club=owner)
            for i in range(count):
                Lot.objects.create(
                    lot_name=name, auction=auction, auctiontos_seller=seller, i_bred_this_fish=True, species=species
                )
        rows = _bred_species(min_lots=2)["rows"]
        self.assertEqual([row["name"] for row in rows], ["Molly", "Guppy", "Platy"])
        self.assertEqual(_bred_species(min_lots=3), {})

    def test_one_line_facts(self):
        from auctions.help_stats import _feedback, _invoice_opening, _joining, _voice

        auction = self.online(ago(days=30), lots=0)
        for when in (ago(days=60), ago(days=30, hours=3)):
            tos = AuctionTOS.objects.create(auction=auction, pickup_location=self.location, name="Joiner")
            AuctionTOS.objects.filter(pk=tos.pk).update(createdon=when)
        self.assertEqual(_joining(1)["online_join_last_day"], 50)

        winner = AuctionTOS.objects.create(auction=auction, pickup_location=self.location, name="Buyer")
        for rating in (1, 1, -1, 0):
            Lot.objects.create(
                lot_name="rated", auction=auction, auctiontos_winner=winner, winning_price=5, feedback_rating=rating
            )
        facts = _feedback(Lot.objects.filter(auction=auction), 1)
        self.assertEqual(facts, {"feedback_positive": 67, "feedback_left": 100})

        VoiceCommandLog.objects.create(auction=auction, slot="lot", chosen="1")
        VoiceCommandLog.objects.create(auction=auction, slot="lot", chosen="2", corrected_to="3")
        VoiceCommandLog.objects.create(auction=auction, heard="mumble")
        VoiceCommandLog.objects.create(auction=auction, slot="price", chosen="5")
        self.assertEqual(_voice(1), {"voice_right": 50})
        self.assertEqual(auction.voice_accuracy, {"commands": 4, "right": 2, "percent": 50})

        sent = ago(hours=10)
        Invoice.objects.filter(pk=self.invoice.pk).update(
            email_sent_on=sent, opened_on=sent + datetime.timedelta(hours=2)
        )
        Invoice.objects.filter(pk=self.invoiceB.pk).update(email_sent_on=sent)
        self.assertEqual(_invoice_opening(1), {"invoice_opened": 50, "invoice_open_hours": 2})
        self.assertEqual(_invoice_opening(3), {})


class GuidesQuoteTheCountsTests(StandardTestCase):
    def guide(self, slug, **counts):
        patches = [patch(f"auctions.help_stats.{name}", return_value=value) for name, value in counts.items()]
        for each in patches:
            each.start()
        try:
            return self.client.get(help_guides.GUIDES[slug].url)
        finally:
            for each in patches:
                each.stop()

    def test_each_guide_draws_its_table(self):
        row = {"label": "Sunday", "auctions": 9, "sold": 81}
        response = self.guide(
            "run-an-online-auction",
            online_timing={"weekdays": [row, row]},
            site_stats={"online_join_last_day": 31, "invoice_opened": 70, "invoice_open_hours": 5},
        )
        self.assertContains(response, "<td>Sunday</td>")
        self.assertContains(response, "31% of people here join an online auction in its last day")
        self.assertContains(response, "70% of invoice emails here get opened, usually within 5 hours")

        rank = {"label": "After the 30th", "lots": 400, "sold": 61, "median": 7.0}
        self.assertContains(self.guide("auction-rules", seller_rank={"rows": [rank, rank]}), "<td>After the 30th</td>")

        chart = {"percents": [1.0] * 60, "bids": 900, "round_pct": 44, "favourite": 10}
        response = self.guide(
            "online-auctions", bid_amounts=chart, site_stats={"feedback_left": 12, "feedback_positive": 97}
        )
        self.assertContains(response, 'id="bid-amounts-percents"')
        self.assertContains(response, "44% of bids here are a multiple of $5")
        self.assertContains(response, "12% of buyers here rate anything, and 97% of the ratings are +1")

        response = self.guide("breeder-award-programs", bred_species={"rows": [{"name": "Guppy", "lots": 9}] * 3})
        self.assertContains(response, "Bred most often here lately: Guppy, Guppy, Guppy.")

        response = self.guide("run-an-in-person-auction", site_stats={"in_person_join_on_day": 40, "voice_right": 88})
        self.assertContains(response, "40% of people join on the day")
        self.assertContains(response, "88% of voice commands here are right first time")

    def test_nothing_counted_falls_back_to_words(self):
        response = self.guide("online-auctions", bid_amounts={}, site_stats={})
        self.assertNotContains(response, 'id="bid-amounts-percents"')
        self.assertContains(response, "Most bids are a multiple of $5.")

    def test_the_old_blog_post_lands_in_the_help(self):
        BlogPost.objects.create(title="How much should you bid?", body="old")
        response = self.client.get(reverse("blog_post", kwargs={"slug": "how-much-should-you-bid"}))
        self.assertRedirects(response, help_guides.GUIDES["online-auctions"].url + "#how-much", status_code=301)

    def test_the_paypal_blog_post_lands_in_the_help(self):
        BlogPost.objects.create(title="PayPal Integration", slug="online-payments-suck", body="old")
        response = self.client.get(reverse("blog_post", kwargs={"slug": "online-payments-suck"}))
        self.assertRedirects(response, help_guides.GUIDES["payments"].url + "#paypal", status_code=301)


class AbandonedBidTests(StandardTestCase):
    def beacon(self, stage, lot=None):
        lot = lot or self.lot
        return self.client.post(reverse("lot_bid_abandoned", kwargs={"pk": lot.pk}), {"stage": stage})

    def test_the_furthest_stage_is_kept(self):
        self.client.force_login(self.userB)
        self.beacon("confirm")
        self.beacon("typed")
        self.assertEqual(AbandonedBid.objects.get(user=self.userB, lot=self.lot).stage, "confirm")
        self.assertEqual(self.beacon("nonsense").status_code, 204)
        self.assertEqual(AbandonedBid.objects.count(), 1)

    def test_signed_out_is_not_recorded(self):
        self.assertEqual(self.beacon("typed").status_code, 204)
        self.assertFalse(AbandonedBid.objects.exists())

    def test_the_stats_say_who_came_back(self):
        self.client.force_login(self.userB)
        self.beacon("typed")
        self.client.force_login(self.user_with_no_lots)
        self.beacon("blocked")
        Bid.objects.create(user=self.user_with_no_lots, lot_number=self.lot, amount=20)
        totals = self.online_auction.abandoned_bids
        self.assertEqual((totals["total"], totals["typed"], totals["blocked"], totals["came_back"]), (2, 1, 1, 1))

    def test_the_lot_page_reports_it(self):
        self.client.force_login(self.userB)
        response = self.client.get(self.lot.get_absolute_url())
        self.assertContains(response, reverse("lot_bid_abandoned", kwargs={"pk": self.lot.pk}))


class InvoiceOpenedTests(StandardTestCase):
    def test_the_first_open_is_kept(self):
        url = reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)
        self.invoice.refresh_from_db()
        first = self.invoice.opened_on
        self.assertTrue(self.invoice.opened)
        self.assertIsNotNone(first)
        self.client.get(url)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.opened_on, first)

    def test_the_stats_page_counts_opens_after_the_email(self):
        sent = ago(hours=5)
        Invoice.objects.filter(pk=self.invoice.pk).update(
            email_sent_on=sent, opened_on=sent + datetime.timedelta(hours=3)
        )
        Invoice.objects.filter(pk=self.invoiceB.pk).update(
            email_sent_on=sent, opened_on=sent - datetime.timedelta(hours=1)
        )
        self.assertEqual(
            self.online_auction.invoice_opening, {"sent": 2, "opened": 1, "percent": 50, "median_hours": 3.0}
        )


class RefreshGuardTests(StandardTestCase):
    def test_a_failed_count_stops_pages_asking_again(self):
        from auctions import help_stats

        with isolated_cache("help-refresh-guard"):
            with patch("auctions.help_stats._bred_species", side_effect=RuntimeError), self.assertRaises(RuntimeError):
                help_stats.refresh()
            self.assertTrue(cache.get(help_stats.REFRESH_QUEUED_KEY))
            with (
                patch("auctions.tasks.refresh_help_stats.delay") as queued,
                self.captureOnCommitCallbacks(execute=True),
            ):
                self.assertEqual(help_stats.bred_species(), {})
            queued.assert_not_called()

            help_stats.refresh()
            self.assertIsNone(cache.get(help_stats.REFRESH_QUEUED_KEY))
