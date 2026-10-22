"""Where visitors came from, and the address the browser is left showing.

Covers the ``src``/``utm_source`` labels, the seller's lot ids behind the selling dashboard's history and
the auction stat of joins by source.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from auctions.models import Auction, AuctionCampaign, AuctionTOS, Lot, PageView, PickupLocation
from auctions.tests import StandardTestCase
from auctions.views import page_view_history
from auctions.views.lot_pages import seller_lot_ids


def _view(*, when=None, **fields):
    row = PageView.objects.create(**fields)
    if when is not None:
        PageView.objects.filter(pk=row.pk).update(date_start=when)
    return row


class SourceLabelTests(StandardTestCase):
    def test_our_own_sources_read_as_words(self):
        labels = PageView.source_labels({"my_lots", "buying", "lot_tile", "copied_link", "someone_elses"})
        self.assertEqual(labels["my_lots"], "From the selling page")
        self.assertEqual(labels["buying"], "From the buying page")
        self.assertEqual(labels["lot_tile"], "From a lot list")
        self.assertEqual(labels["copied_link"], "A copied link")
        self.assertEqual(labels["someone_elses"], "someone_elses")

    def test_a_campaign_uuid_is_named_for_its_campaign(self):
        reminder = AuctionCampaign.objects.create(auction=self.online_auction, user=self.userB)
        promo = AuctionCampaign.objects.create(
            auction=self.online_auction,
            user=self.user_who_does_not_join,
            kind=AuctionCampaign.KIND_PROMO,
            source=AuctionCampaign.SOURCE_PROMO_EMAIL,
        )
        stranger = "00000000-0000-4000-8000-000000000000"
        labels = PageView.source_labels({str(reminder.uuid), str(promo.uuid), stranger})
        self.assertEqual(labels[str(reminder.uuid)], "From a join reminder email")
        self.assertEqual(labels[str(promo.uuid)], "From a promotion email")
        self.assertEqual(labels[stranger], stranger)

    def test_sources_with_one_label_are_one_row_and_one_chart_band(self):
        for source in ("lot_list", "lot_tile", "lot_tile"):
            _view(lot_number=self.lot, source=source, user=self.user)
        rows = PageView.source_breakdown(PageView.objects.filter(lot_number=self.lot))
        self.assertEqual([(row["label"], row["views"]) for row in rows], [("From a lot list", 3)])
        history = page_view_history(PageView.objects.filter(lot_number=self.lot))
        self.assertEqual(history["chart"]["series"], ["From a lot list"])
        self.assertEqual(sum(history["chart"]["data"][0]), 3)


class SellerLotIdsTests(StandardTestCase):
    def test_lots_sold_by_account_or_by_seller_tos_and_not_deleted(self):
        own = Lot.objects.create(lot_name="Mine, no auction", user=self.user, quantity=1)
        gone = Lot.objects.create(lot_name="Deleted", user=self.user, quantity=1, is_deleted=True)
        ids = seller_lot_ids(self.user)
        self.assertIn(own.pk, ids)
        self.assertIn(self.lot.pk, ids)  # sold through the user's seller TOS
        self.assertNotIn(gone.pk, ids)
        self.assertNotIn(self.lotB.pk, seller_lot_ids(self.userB))

    def test_selling_history_for_someone_with_no_lots(self):
        nobody = User.objects.create_user(username="sells_nothing", password="x")
        self.client.force_login(nobody)
        response = self.client.get(reverse("my_lots_page_view_history"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["history"]["total_views"], 0)


class JoinSourcesStatTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Where did they come from",
            is_online=True,
            date_start=timezone.now() - timedelta(days=3),
            date_end=timezone.now() + timedelta(days=2),
        )
        location = PickupLocation.objects.create(
            name="here", auction=self.auction, pickup_time=timezone.now() + timedelta(days=3)
        )
        self.people = [User.objects.create_user(username=f"joiner{n}", password="x") for n in range(5)]
        self.joined_at = timezone.now()
        for person in self.people:
            tos = AuctionTOS.objects.create(user=person, auction=self.auction, pickup_location=location)
            AuctionTOS.objects.filter(pk=tos.pk).update(createdon=self.joined_at)
        AuctionTOS.objects.create(user=self.userB, auction=self.auction, pickup_location=location, manually_added=True)

    def stat(self):
        stat = self.auction.set_stat_join_sources()
        return dict(zip(stat["labels"], stat["data"][0]))

    def test_first_tracked_view_before_joining_counts(self):
        before = self.joined_at - timedelta(hours=1)
        first, second, third, fourth, _ = self.people
        _view(auction=self.auction, user=first, source="facebook", when=before - timedelta(hours=1))
        _view(auction=self.auction, user=first, source="copied_link", when=before)
        _view(auction=self.auction, user=second, source="copied_link", when=before)
        _view(auction=self.auction, user=third, referrer="https://www.facebook.com/", when=before)
        # After joining: not how they found it.
        _view(auction=self.auction, user=fourth, source="newsletter", when=self.joined_at + timedelta(hours=1))
        self.assertEqual(
            self.stat(),
            {
                "Shared on Facebook": 1,
                "A copied link": 1,
                "www.facebook.com": 1,
                "No link tracked": 2,  # the fourth and fifth; the admin-added one isn't a join
            },
        )

    def test_nobody_joined(self):
        AuctionTOS.objects.filter(auction=self.auction).delete()
        self.assertEqual(self.stat(), {})

    def test_stats_page_renders_it(self):
        self.auction.recalculate_stats()
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_stats", kwargs={"slug": self.auction.slug}))
        self.assertContains(response, "auction_stats_join_sources")
        self.assertContains(response, "No link tracked")


class AddressBarTests(StandardTestCase):
    def test_lot_page_tags_the_address_with_copied_link(self):
        html = self.client.get(self.lot.lot_link, HTTP_USER_AGENT="Mozilla/5.0").content.decode()
        self.assertIn("cleanUrl.searchParams.set('utm_source', 'copied_link')", html)

    def test_not_in_the_app(self):
        app = self.client.get(self.lot.lot_link, HTTP_USER_AGENT="Mozilla/5.0 FishAuctionsApp/1.0 (Flutter; iOS)")
        self.assertNotIn("'copied_link')", app.content.decode())

    def test_landing_page_shows_the_lot_list_address(self):
        self.client.force_login(self.user)
        response = self.client.get(f"/?{self.online_auction.slug}")
        self.assertEqual(response.context["rewrite_url"], f"/lots/all/?auction={self.online_auction.slug}")

    def test_landing_page_shows_the_auction_address_to_someone_who_has_not_joined(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(f"/?{self.online_auction.slug}")
        self.assertEqual(response.context["rewrite_url"], self.online_auction.get_absolute_url())

    def test_share_links_carry_utm_source(self):
        self.client.force_login(self.user)
        html = self.client.get(self.online_auction.get_absolute_url()).content.decode()
        self.assertIn(f"/?{self.online_auction.slug}&amp;utm_source=copied_link", html)
        self.assertIn("utm_source%3Dfacebook", html)
