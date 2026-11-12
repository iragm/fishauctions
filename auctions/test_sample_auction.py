"""Tests for the sample data offered on a test auction (``auctions.sample_auction``).

Covers the banner on the auction page (who sees it, and when), the POST that fills the auction, and
the fixture file itself: its counts, that nobody in it has contact details, and that no name matches
the standard test fixture's people.
"""

import datetime

from django.contrib.messages import get_messages
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from auctions.models import Auction, AuctionTOS, Lot, PickupLocation
from auctions.sample_auction import DATA_FILE, load_fixture, scaled_price
from auctions.tests import StandardTestCase

BANNER = "Looks like a test auction, do you want to pre-populate with some sample data?"


class SampleAuctionTests(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.test_auction = Auction.objects.create(
            created_by=cls.admin_user,
            title="Test auction for trying things",
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=3),
            promote_this_auction=False,
        )
        PickupLocation.objects.create(
            name="Test location", auction=cls.test_auction, pickup_time=timezone.now() + datetime.timedelta(days=3)
        )
        cls.url = reverse("auction_sample_data", kwargs={"slug": cls.test_auction.slug})

    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")

    def page(self):
        return self.client.get(self.test_auction.get_absolute_url())

    def test_banner_shows_for_admin_on_empty_test_auction(self):
        self.assertContains(self.page(), BANNER)

    def test_banner_hidden_for_non_test_name(self):
        auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Spring fish swap",
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=3),
        )
        self.assertNotContains(self.client.get(auction.get_absolute_url()), BANNER)

    def test_banner_hidden_for_non_admin(self):
        self.client.login(username="no_tos", password="testpassword")
        self.assertNotContains(self.page(), BANNER)

    def test_banner_hidden_once_lots_exist(self):
        seller = AuctionTOS.objects.create(
            auction=self.test_auction,
            pickup_location=self.test_auction.location_qs.first(),
            name="Someone",
            manually_added=True,
        )
        Lot.objects.create(lot_name="A lot", auction=self.test_auction, auctiontos_seller=seller, quantity=1)
        self.assertNotContains(self.page(), BANNER)

    def test_post_adds_ten_people_and_twenty_lots_without_email(self):
        before_people = AuctionTOS.objects.filter(auction=self.test_auction).count()
        response = self.client.post(self.url)
        self.assertRedirects(response, self.test_auction.get_absolute_url(), fetch_redirect_response=False)
        people = AuctionTOS.objects.filter(auction=self.test_auction)
        self.assertEqual(people.count() - before_people, 10)
        self.assertEqual(Lot.objects.filter(auction=self.test_auction).count(), 20)
        self.assertFalse(people.exclude(email__isnull=True).exclude(email="").exists())
        self.assertTrue(all(tos.manually_added for tos in people))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual([str(m) for m in get_messages(response.wsgi_request)], ["Added 10 sample people and 20 lots."])
        # Every lot has a seller from the sample people, and a minimum-bid-respecting price.
        for lot in Lot.objects.filter(auction=self.test_auction):
            self.assertGreaterEqual(lot.reserve_price, self.test_auction.minimum_bid)
            self.assertEqual(lot.reserve_price, lot.reserve_price.to_integral_value())
        self.assertNotContains(self.page(), BANNER)

    def test_post_by_non_admin_creates_nothing(self):
        self.client.login(username="no_tos", password="testpassword")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Lot.objects.filter(auction=self.test_auction).exists())

    def test_post_on_non_test_auction_does_nothing(self):
        auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Spring fish swap",
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=3),
        )
        PickupLocation.objects.create(name="Spot", auction=auction, pickup_time=timezone.now())
        self.client.post(reverse("auction_sample_data", kwargs={"slug": auction.slug}))
        self.assertFalse(Lot.objects.filter(auction=auction).exists())

    def test_second_post_creates_nothing(self):
        self.client.post(self.url)
        people = AuctionTOS.objects.filter(auction=self.test_auction).count()
        self.client.post(self.url)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.test_auction).count(), people)
        self.assertEqual(Lot.objects.filter(auction=self.test_auction).count(), 20)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_no_pickup_location_explains(self):
        self.test_auction.location_qs.delete()
        response = self.client.post(self.url)
        self.assertFalse(Lot.objects.filter(auction=self.test_auction).exists())
        self.assertIn("pickup location", " ".join(str(m) for m in get_messages(response.wsgi_request)))

    def test_prices_scale_to_minimum_bid(self):
        self.test_auction.minimum_bid = 5
        self.assertEqual(scaled_price(3, self.test_auction), 5)
        self.assertEqual(scaled_price(8, self.test_auction), 8)

    def test_fixture_shape_and_names(self):
        data = load_fixture()
        self.assertTrue(DATA_FILE.exists())
        self.assertEqual(len(data["participants"]), 10)
        self.assertEqual(len(data["lots"]), 20)
        self.assertEqual({lot["seller"] for lot in data["lots"]}, set(range(10)))
        for row in data["participants"]:
            self.assertEqual(set(row), {"name"})
            self.assertEqual(len(row["name"].split()), 2)
        for lot in data["lots"]:
            self.assertLessEqual(len(lot["lot_name"]), 40)
        existing = {n.lower() for n in AuctionTOS.objects.exclude(name__isnull=True).values_list("name", flat=True)}
        existing |= {u.username.lower() for u in type(self.admin_user).objects.all()}
        self.assertFalse({row["name"].lower() for row in data["participants"]} & existing)
