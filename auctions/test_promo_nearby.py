"""Auctions near a signed-out visitor, placed by Cloudflare's location headers: when the headers are believed,
what the promo page's strip lists, and the auction list's distances without a click."""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.client_ip import visitor_coordinates
from auctions.models import Auction, PickupLocation

EDGE = "172.64.0.10"  # a Cloudflare address
# Near Albany, NY; the auctions below sit around it.
HERE = {"HTTP_X_REAL_IP": EDGE, "HTTP_CF_IPLATITUDE": "42.65", "HTTP_CF_IPLONGITUDE": "-73.75"}


class FakeRequest:
    def __init__(self, **meta):
        self.META = meta


@override_settings(BEHIND_CLOUDFLARE=True)
class VisitorCoordinatesTests(TestCase):
    def test_read_from_cloudflare(self):
        self.assertEqual(visitor_coordinates(FakeRequest(**HERE)), (42.65, -73.75))

    def test_not_believed_unless_it_came_through_cloudflare(self):
        self.assertEqual(visitor_coordinates(FakeRequest(**{**HERE, "HTTP_X_REAL_IP": "203.0.113.9"})), (None, None))

    @override_settings(BEHIND_CLOUDFLARE=False)
    def test_not_believed_when_not_behind_cloudflare(self):
        self.assertEqual(visitor_coordinates(FakeRequest(**HERE)), (None, None))

    def test_junk_and_unknown_are_nothing(self):
        for latitude, longitude in (("", ""), ("abc", "1"), ("nan", "1"), ("95", "1"), ("1", "inf"), ("0", "0")):
            with self.subTest(latitude=latitude, longitude=longitude):
                request = FakeRequest(**{**HERE, "HTTP_CF_IPLATITUDE": latitude, "HTTP_CF_IPLONGITUDE": longitude})
                self.assertEqual(visitor_coordinates(request), (None, None))


@override_settings(BEHIND_CLOUDFLARE=True)
class PromoNearbyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="nearby_owner")
        now = timezone.now()
        cls.near = cls._auction("Near and soon", now + datetime.timedelta(days=5), 42.7, -73.8)
        cls.far = cls._auction("Far away", now + datetime.timedelta(days=5), 34.05, -118.25)
        cls.ended = cls._auction(
            "Already over", now - datetime.timedelta(days=9), 42.7, -73.8, end=now - datetime.timedelta(days=2)
        )
        cls.hidden = cls._auction("Not promoted", now + datetime.timedelta(days=5), 42.7, -73.8, promote=False)

    @classmethod
    def _auction(cls, title, start, latitude, longitude, end=None, promote=True):
        auction = Auction.objects.create(
            created_by=cls.user,
            title=title,
            is_online=False,
            date_start=start,
            date_end=end or start + datetime.timedelta(hours=6),
            promote_this_auction=promote,
        )
        for name in ("hall", "annex"):
            PickupLocation.objects.create(
                name=name, auction=auction, pickup_time=start, latitude=latitude, longitude=longitude
            )
        return auction

    def _strip(self, **meta):
        return self.client.get(reverse("promo_nearby"), **meta)

    def test_lists_promoted_upcoming_auctions_near_the_address_once_each(self):
        response = self._strip(**HERE)
        self.assertContains(response, "Near and soon", count=1)
        for left_out in ("Far away", "Already over", "Not promoted"):
            self.assertNotContains(response, left_out)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("noindex", response["X-Robots-Tag"])

    def test_nothing_without_a_location(self):
        self.assertEqual(self._strip().content.strip(), b"")

    def test_a_location_they_gave_wins(self):
        self.client.cookies["latitude"] = "34.05"
        self.client.cookies["longitude"] = "-118.25"
        response = self._strip(**HERE)
        self.assertContains(response, "Far away")
        self.assertNotContains(response, "Near and soon")

    def test_crawlers_get_nothing(self):
        self.assertEqual(self._strip(HTTP_USER_AGENT="Googlebot/2.1", **HERE).content.strip(), b"")

    @override_settings(ENABLE_PROMO_PAGE=True)
    def test_the_promo_page_asks_for_it(self):
        self.assertContains(self.client.get(reverse("promo")), reverse("promo_nearby"))

    def test_the_auction_list_measures_from_the_address_for_signed_out_visitors(self):
        response = self.client.get(reverse("auctions"), **HERE)
        self.assertTrue(response.context["view"]._user_has_location)
