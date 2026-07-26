"""Tests for GET /api/mobile/auctions/last-used/ — the command palette's AR-gating lookup.

The app fetches this once when the command palette opens to decide, client-side, whether to surface
the native AR lot-scanning entry. It is read-only with no side effects (deliberately not
``checkin/ping/``), always returns 200 for the empty case, and mirrors ``MyLastAuctionLots``'s
"plain when unset/deleted" fallback (see ``test_my_last_auction_plain_*`` in test_ar.py).
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.models import Auction, PickupLocation

User = get_user_model()

# Pittsburgh — the single-location coordinates the happy path reports back.
PGH = (40.4406, -79.9959)


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class MobileLastUsedAuctionTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.user = User.objects.create_user(username="palette_user", password="x", email="p@example.com")
        self.venue = Auction.objects.create(
            created_by=self.user,
            title="Spring Fry Swap 2026",
            is_online=False,
            date_start=now,
            date_end=now + datetime.timedelta(hours=6),
        )
        # In-person auctions auto-create exactly one default (non-mail) pickup location via signals;
        # re-save to materialise it, then this is "the single physical location".
        self.venue.save()
        self.location = self.venue.location_qs.exclude(pickup_by_mail=True).first()
        self.location.latitude, self.location.longitude = PGH
        self.location.save()

    def _get(self, user=None):
        return self.client.get(reverse("mobile-last-used-auction"), **(_bearer(user) if user else {}))

    def _set_last(self, auction):
        self.user.userdata.last_auction_used = auction
        self.user.userdata.save()

    # --- auth -----------------------------------------------------------------
    def test_requires_auth(self):
        self.assertEqual(self._get().status_code, 401)

    # --- empty-data case is 200 with all-null (never 404) ---------------------
    def test_unset_returns_all_null_200(self):
        resp = self._get(self.user)  # no last_auction_used
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "slug": None,
                "title": None,
                "is_online": None,
                "pretty_much_over": None,
                "latitude": None,
                "longitude": None,
            },
        )

    def test_deleted_auction_returns_all_null_200(self):
        self.venue.is_deleted = True
        self.venue.save()
        self._set_last(self.venue)
        resp = self._get(self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["slug"])
        self.assertIsNone(resp.json()["latitude"])

    # --- populated ------------------------------------------------------------
    def test_in_person_with_coordinates(self):
        self._set_last(self.venue)
        data = self._get(self.user).json()
        self.assertEqual(data["slug"], self.venue.slug)
        self.assertEqual(data["title"], "Spring Fry Swap 2026")
        self.assertFalse(data["is_online"])
        self.assertFalse(data["pretty_much_over"])
        self.assertAlmostEqual(data["latitude"], PGH[0])
        self.assertAlmostEqual(data["longitude"], PGH[1])

    def test_online_auction_reports_is_online_true_and_no_coords(self):
        online = Auction.objects.create(
            created_by=self.user,
            title="Online Only",
            is_online=True,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        self._set_last(online)
        data = self._get(self.user).json()
        self.assertTrue(data["is_online"])
        # Online auctions get no auto physical location, so there's nothing to distance-gate on.
        self.assertIsNone(data["latitude"])
        self.assertIsNone(data["longitude"])

    def test_pretty_much_over_passthrough(self):
        # A fully-past in-person auction is wound down (>24h) → pretty_much_over. Built fresh with all
        # dates in the past rather than mutating self.venue, whose already-set lot_submission_end_date
        # would otherwise keep wind_down at "now".
        past = timezone.now() - datetime.timedelta(days=3)
        over = Auction.objects.create(
            created_by=self.user,
            title="Last Year's Swap",
            is_online=False,
            date_start=past,
            date_end=past + datetime.timedelta(hours=6),
            lot_submission_end_date=past,
        )
        self._set_last(over)
        data = self._get(self.user).json()
        self.assertTrue(data["pretty_much_over"])
        # Still reported — deciding whether to hide AR on a stale auction is the app's job, not ours.
        self.assertEqual(data["slug"], over.slug)

    # --- coordinate edge cases: null pair means "can't distance-gate" ---------
    def test_unset_coordinates_report_null(self):
        # (0, 0) is the codebase's "coordinates unset" sentinel, not a real point off Africa.
        self.location.latitude = 0
        self.location.longitude = 0
        self.location.save()
        self._set_last(self.venue)
        data = self._get(self.user).json()
        self.assertEqual(data["slug"], self.venue.slug)  # auction still reported
        self.assertIsNone(data["latitude"])
        self.assertIsNone(data["longitude"])

    def test_multiple_physical_locations_report_null_coords(self):
        PickupLocation.objects.create(name="Overflow", auction=self.venue, latitude=PGH[0], longitude=PGH[1])
        self._set_last(self.venue)
        data = self._get(self.user).json()
        self.assertEqual(data["slug"], self.venue.slug)  # auction still reported
        self.assertIsNone(data["latitude"])  # ambiguous → can't distance-gate
        self.assertIsNone(data["longitude"])

    def test_mail_only_location_reports_null_coords(self):
        # A lone mail-only location is not a physical location to gate on.
        self.location.pickup_by_mail = True
        self.location.save()
        self._set_last(self.venue)
        data = self._get(self.user).json()
        self.assertEqual(data["slug"], self.venue.slug)
        self.assertIsNone(data["latitude"])
        self.assertIsNone(data["longitude"])
