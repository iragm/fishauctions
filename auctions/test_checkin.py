"""Tests for Part 6 — proximity check-in & welcome (mobile ping/join/set-location)."""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.models import Auction, AuctionHistory, AuctionTOS, Club, ClubMember, PickupLocation

User = get_user_model()

VENUE = (44.47, -73.21)
AT = (44.47, -73.21)  # 0 mi — within 500 ft
NEAR = (44.475, -73.21)  # ~0.35 mi — outside 500 ft, within 2 mi
FAR = (44.52, -73.21)  # ~3.45 mi — outside 2 mi


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class CheckinBase(TestCase):
    def setUp(self):
        now = timezone.now()
        self.creator = User.objects.create_user(username="venue_admin", password="x", email="admin@example.com")
        self.arrival = User.objects.create_user(username="arriving", password="x", email="arrive@example.com")
        self.venue = Auction.objects.create(
            created_by=self.creator,
            title="Spring Fish Auction",
            is_online=False,
            date_start=now,
            date_end=now + datetime.timedelta(hours=6),
        )
        # In-person auctions get one auto-created default pickup location (signals.py); a real
        # single-location auction is exactly that one row. Re-save to materialise it, then pin coords.
        self.venue.save()
        self.location = self.venue.location_qs.exclude(pickup_by_mail=True).first()
        self.location.latitude = VENUE[0]
        self.location.longitude = VENUE[1]
        self.location.save()

    def _ping(self, user, lat, lon):
        return self.client.post(
            reverse("mobile-checkin-ping"),
            data={"latitude": lat, "longitude": lon},
            content_type="application/json",
            **_bearer(user),
        )

    def _types(self, response):
        return {a["type"] for a in response.json()["actions"]}


class CheckinPingGeofenceTests(CheckinBase):
    def test_requires_auth(self):
        resp = self.client.post(
            reverse("mobile-checkin-ping"),
            data={"latitude": AT[0], "longitude": AT[1]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_junk_coordinates_400(self):
        resp = self.client.post(
            reverse("mobile-checkin-ping"),
            data={"latitude": 200, "longitude": 0},
            content_type="application/json",
            **_bearer(self.arrival),
        )
        self.assertEqual(resp.status_code, 400)

    def test_join_offer_within_500ft(self):
        resp = self._ping(self.arrival, *AT)
        self.assertEqual(resp.status_code, 200)
        actions = resp.json()["actions"]
        offer = next(a for a in actions if a["type"] == "join_offer")
        self.assertEqual(offer["auction"], self.venue.slug)
        self.assertIn("Welcome to the Spring Fish Auction", offer["message"])
        self.assertEqual(offer["rules_url"], self.venue.get_absolute_url())

    def test_no_join_offer_outside_500ft(self):
        # NEAR is within the 2 mi admin radius but well outside the 500 ft welcome radius.
        self.assertNotIn("join_offer", self._types(self._ping(self.arrival, *NEAR)))

    def test_nothing_when_outside_2mi(self):
        self.assertEqual(self._ping(self.arrival, *FAR).json()["actions"], [])

    def test_online_auction_never_matches(self):
        self.venue.is_online = True
        self.venue.save()
        self.assertEqual(self._ping(self.arrival, *AT).json()["actions"], [])

    def test_multi_location_never_matches(self):
        PickupLocation.objects.create(name="Overflow", auction=self.venue, latitude=VENUE[0], longitude=VENUE[1])
        self.assertEqual(self._ping(self.arrival, *AT).json()["actions"], [])

    def test_before_window_no_offer(self):
        self.venue.date_start = timezone.now() + datetime.timedelta(hours=4)  # > 3 h out
        self.venue.date_end = timezone.now() + datetime.timedelta(hours=10)
        self.venue.save()
        self.assertEqual(self._ping(self.arrival, *AT).json()["actions"], [])

    def test_after_ended_no_offer(self):
        self.venue.date_start = timezone.now() - datetime.timedelta(hours=8)
        self.venue.date_end = timezone.now() - datetime.timedelta(hours=1)  # ended
        self.venue.save()
        self.assertEqual(self._ping(self.arrival, *AT).json()["actions"], [])

    def test_nudge_dedupe_no_repeat_join_offer(self):
        self.assertIn("join_offer", self._types(self._ping(self.arrival, *AT)))
        self.assertNotIn("join_offer", self._types(self._ping(self.arrival, *AT)))  # second ping: no repeat


class CheckinPingStateTests(CheckinBase):
    def _make_checkin_mode(self):
        club = Club.objects.create(name="Fish Club")
        ClubMember.objects.create(club=club, user=self.creator, permission_admin=True)
        self.venue.club = club
        self.venue.manage_users_through_club = "checkin"
        self.venue.save()
        self.assertTrue(self.venue.use_check_in_mode)
        return club

    def test_email_matched_tos_bound_and_checked_in(self):
        self._make_checkin_mode()
        # Added-by-email row with no user FK; the ping should bind it and auto-check-in.
        AuctionTOS.objects.create(
            auction=self.venue, pickup_location=self.location, email="arrive@example.com", user=None, name="Arrive"
        )
        types = self._types(self._ping(self.arrival, *AT))
        self.assertIn("checked_in", types)
        self.assertNotIn("join_offer", types)
        tos = AuctionTOS.objects.get(auction=self.venue, email__iexact="arrive@example.com")
        self.assertEqual(tos.user, self.arrival)
        self.assertIsNotNone(tos.checked_in)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.venue, action__icontains="checked in via the app").exists()
        )

    def test_auto_check_in_idempotent(self):
        self._make_checkin_mode()
        AuctionTOS.objects.create(auction=self.venue, pickup_location=self.location, user=self.arrival, name="Arrive")
        self.assertIn("checked_in", self._types(self._ping(self.arrival, *AT)))
        # A second ping must not re-check-in or re-emit the action.
        self.assertNotIn("checked_in", self._types(self._ping(self.arrival, *AT)))
        self.assertEqual(
            AuctionHistory.objects.filter(auction=self.venue, action__icontains="checked in via the app").count(), 1
        )

    def test_non_checkin_auction_no_checked_in_action(self):
        # Joined already, but the auction isn't in check-in mode → no auto check-in.
        AuctionTOS.objects.create(auction=self.venue, pickup_location=self.location, user=self.arrival)
        types = self._types(self._ping(self.arrival, *AT))
        self.assertNotIn("checked_in", types)
        self.assertNotIn("join_offer", types)  # already has a TOS

    def test_admin_location_offer_within_2mi(self):
        # Admin, exact_location_set False, within 2 mi (but outside 500 ft) → only the location offer.
        types = self._types(self._ping(self.creator, *NEAR))
        self.assertIn("set_location_offer", types)

    def test_admin_location_offer_absent_when_already_exact(self):
        self.venue.exact_location_set = True
        self.venue.save()
        self.assertNotIn("set_location_offer", self._types(self._ping(self.creator, *NEAR)))

    def test_admin_offer_can_coexist_with_join(self):
        # The creator has no TOS on their own auction: within 500 ft they get BOTH the join offer and
        # the admin location offer.
        types = self._types(self._ping(self.creator, *AT))
        self.assertIn("join_offer", types)
        self.assertIn("set_location_offer", types)

    def test_non_admin_no_location_offer(self):
        self.assertNotIn("set_location_offer", self._types(self._ping(self.arrival, *NEAR)))


class CheckinJoinTests(CheckinBase):
    def _join(self, user):
        return self.client.post(
            reverse("mobile-checkin-join"),
            data={"auction": self.venue.slug},
            content_type="application/json",
            **_bearer(user),
        )

    def test_creates_tos_once_with_history(self):
        resp = self._join(self.arrival)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"joined": True, "checked_in": False, "rules_url": self.venue.get_absolute_url()})
        self.assertEqual(AuctionTOS.objects.filter(auction=self.venue, user=self.arrival).count(), 1)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.venue, action__icontains="joined via the app").exists()
        )
        # Idempotent: a second join doesn't create a duplicate TOS.
        self._join(self.arrival)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.venue, user=self.arrival).count(), 1)

    def test_checkin_mode_join_also_checks_in(self):
        club = Club.objects.create(name="Fish Club")
        ClubMember.objects.create(club=club, user=self.creator, permission_admin=True)
        self.venue.club = club
        self.venue.manage_users_through_club = "checkin"
        self.venue.save()
        resp = self._join(self.arrival)
        self.assertTrue(resp.json()["checked_in"])
        self.assertIsNotNone(AuctionTOS.objects.get(auction=self.venue, user=self.arrival).checked_in)

    def test_join_binds_added_by_email_row(self):
        AuctionTOS.objects.create(
            auction=self.venue, pickup_location=self.location, email="arrive@example.com", user=None
        )
        self._join(self.arrival)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.venue).count(), 1)
        self.assertEqual(AuctionTOS.objects.get(auction=self.venue).user, self.arrival)

    def test_join_refused_outside_window(self):
        self.venue.date_start = timezone.now() + datetime.timedelta(hours=4)
        self.venue.date_end = timezone.now() + datetime.timedelta(hours=10)
        self.venue.save()
        self.assertEqual(self._join(self.arrival).status_code, 400)


class CheckinSetLocationTests(CheckinBase):
    def _set(self, user, lat=45.0, lon=-72.0):
        return self.client.post(
            reverse("mobile-checkin-set-location"),
            data={"auction": self.venue.slug, "latitude": lat, "longitude": lon},
            content_type="application/json",
            **_bearer(user),
        )

    def test_admin_sets_coords_flag_and_history(self):
        resp = self._set(self.creator, lat=45.0, lon=-72.0)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"set": True})
        self.location.refresh_from_db()
        self.venue.refresh_from_db()
        self.assertEqual((self.location.latitude, self.location.longitude), (45.0, -72.0))
        self.assertTrue(self.venue.exact_location_set)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.venue, action__icontains="Exact location set").exists()
        )

    def test_non_admin_forbidden(self):
        self.assertEqual(self._set(self.arrival).status_code, 403)
        self.location.refresh_from_db()
        self.assertEqual(self.location.latitude, VENUE[0])  # unchanged


class CheckinCloneTests(TestCase):
    def test_clone_copies_exact_location_set(self):
        creator = User.objects.create_user(username="cloner", password="x", email="c@example.com", is_superuser=True)
        original = Auction.objects.create(
            created_by=creator,
            title="Original",
            is_online=False,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(hours=6),
            exact_location_set=True,
        )
        self.client.force_login(creator)
        # Cloning is triggered by ?copy=<slug>&clone on the create-auction POST (see AuctionCreateView).
        self.client.post(
            reverse("create_auction") + f"?copy={original.slug}&clone",
            {
                "title": "Cloned auction",
                "date_start": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cloned_from": original.slug,
            },
        )
        clone = Auction.objects.filter(title="Cloned auction").first()
        self.assertIsNotNone(clone)
        self.assertTrue(clone.exact_location_set)
