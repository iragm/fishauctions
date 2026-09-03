"""The club REST API's read side: members, BAP lots, auctions and lots.

The tests that matter most here are the negative ones -- a key without the privacy flag must not see
a name, an email or a bidder number, in any field, through any filter or ordering.
"""

import datetime
import io
import json
from pathlib import Path

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionHistory,
    AuctionTOS,
    BapAward,
    Category,
    Club,
    ClubAPIKey,
    ClubAPIKeyFieldMap,
    ClubMember,
    Lot,
    LotImage,
    PickupLocation,
    Species,
)
from auctions.tests import WritableMediaRoot
from fishauctions._env import parse_bool_env, require_secure_prod_secrets


class ClubAPITests(TestCase):
    """Tests for the DRF REST API for club members"""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="api_owner", password="testpass", email="api@example.com")
        self.club = Club.objects.create(
            name="API Test Club",
            allow_joining=True,
        )

    def test_api_requires_authentication(self):
        """API endpoint should return 401 for unauthenticated requests"""
        url = reverse("api_club_members", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)

    def test_api_club_members_list(self):
        """Authenticated user with permission_view can list club members via API"""
        from rest_framework.authtoken.models import Token  # noqa: PLC0415

        token = Token.objects.create(user=self.owner)
        ClubMember.objects.create(club=self.club, user=self.owner, permission_view=True)
        ClubMember.objects.create(club=self.club, name="Test Member", email="tm@example.com")
        url = reverse("api_club_members", kwargs={"slug": self.club.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(len(data), 1)

    def test_api_does_not_expose_lat_lng(self):
        """lat/lng coordinates must never appear in the API response to protect member location privacy."""
        from rest_framework.authtoken.models import Token  # noqa: PLC0415

        token = Token.objects.create(user=self.owner)
        ClubMember.objects.create(club=self.club, user=self.owner, permission_view=True)
        member = ClubMember.objects.create(
            club=self.club, name="Located Member", email="loc@example.com", lat=40.7128, lng=-74.0060
        )
        url = reverse("api_club_members", kwargs={"slug": self.club.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for record in data:
            self.assertNotIn("lat", record, "lat must not be exposed in the API for member privacy")
            self.assertNotIn("lng", record, "lng must not be exposed in the API for member privacy")
        # distance_to is allowed (rounded) but raw coordinates must be absent
        detail_url = reverse("api_club_member_detail", kwargs={"slug": self.club.slug, "pk": member.pk})
        detail = self.client.get(detail_url, HTTP_AUTHORIZATION=f"Token {token.key}").json()
        self.assertNotIn("lat", detail)
        self.assertNotIn("lng", detail)


class ClubAPIKeyMemberPermissionTests(TestCase):
    """API key permission checks for club member API endpoints."""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="club_key_owner", password="testpass", email="key@example.com")
        self.club = Club.objects.create(name="Club Key API Club", enable_breeder_award_program=True)
        self.member = ClubMember.objects.create(
            club=self.club,
            name="Existing Member",
            email="existing@example.com",
            memo="before",
        )
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Extended API",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.owner,
            can_add_club_members=True,
        )
        self.raw_key = raw_key
        self.list_url = reverse("api_club_members", kwargs={"slug": self.club.slug})
        self.detail_url = reverse("api_club_member_detail", kwargs={"slug": self.club.slug, "pk": self.member.pk})
        self.bap_url = reverse("api_club_member_bap_awards", kwargs={"slug": self.club.slug, "pk": self.member.pk})

    def test_api_key_list_requires_read_permission(self):
        response = self.client.get(self.list_url, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 403)

    def test_api_key_can_list_members_when_enabled(self):
        self.api_key.can_read_club_member_list = True
        self.api_key.save(update_fields=["can_read_club_member_list"])
        response = self.client.get(self.list_url, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["email"], "existing@example.com")

    def test_api_key_create_supports_extended_field_mapping(self):
        ClubAPIKeyFieldMap.objects.create(
            api_key=self.api_key, external_field="discord_user", internal_field="discord_id"
        )
        response = self.client.post(
            self.list_url,
            {"email": "mappedmember@example.com", "discord_user": "12345"},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 201)
        created = ClubMember.objects.get(club=self.club, email="mappedmember@example.com")
        self.assertEqual(created.discord_id, "12345")
        self.assertEqual(created.source, self.api_key.name)

    def test_api_key_update_requires_update_permission(self):
        response = self.client.patch(
            self.detail_url,
            {"memo": "after"},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 403)

    def test_api_key_update_uses_field_mapping(self):
        self.api_key.can_update_club_members = True
        self.api_key.save(update_fields=["can_update_club_members"])
        ClubAPIKeyFieldMap.objects.create(api_key=self.api_key, external_field="member_note", internal_field="memo")
        response = self.client.patch(
            self.detail_url,
            {"member_note": "after"},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.memo, "after")

    def test_api_key_cannot_delete_member(self):
        self.api_key.can_update_club_members = True
        self.api_key.save(update_fields=["can_update_club_members"])
        response = self.client.delete(self.detail_url, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_deleted)

    def test_api_key_can_add_bap_points_when_enabled(self):
        self.api_key.can_add_bap_points = True
        self.api_key.save(update_fields=["can_add_bap_points"])
        response = self.client.post(
            self.bap_url,
            {"points": 7, "notes": "bonus"},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 201)
        award = BapAward.objects.get(club_member=self.member)
        self.assertEqual(award.points, 7)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bap_points, 7)

    def test_api_key_bap_endpoint_requires_permission(self):
        response = self.client.post(
            self.bap_url,
            {"points": 3},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 403)


class ClubBapLotAPITests(TestCase):
    """The BAP lot feed: /api/v1/clubs/<slug>/bap-lots/"""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="bap_lot_owner", password="testpass", email="bl@example.com")
        self.club = Club.objects.create(name="BAP Lot Club", enable_breeder_award_program=True)
        self.auction = Auction.objects.create(
            created_by=self.owner,
            title="BAP Lot Club Auction",
            is_online=True,
            club=self.club,
            date_start=timezone.now() - datetime.timedelta(days=40),
            date_end=timezone.now() - datetime.timedelta(days=39),
        )
        self.location = PickupLocation.objects.create(
            name="bap lot location", auction=self.auction, pickup_time=timezone.now() + datetime.timedelta(days=1)
        )
        self.seller = AuctionTOS.objects.create(
            name="Mike Smith",
            email="mike@example.com",
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="601",
        )
        self.buyer = AuctionTOS.objects.create(
            name="Dana Lee",
            email="dana@example.com",
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="602",
        )
        self.recent_lot = self.make_lot("Recent lot", days_ago=2, winner=self.buyer, quantity=6)
        self.old_lot = self.make_lot("Old lot", days_ago=90, winner=self.buyer)
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="BAP Lot Key",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.owner,
        )
        self.raw_key = raw_key
        self.url = reverse("api_club_bap_lots", kwargs={"slug": self.club.slug})

    def make_lot(self, lot_name, days_ago, winner=None, quantity=1, **kwargs):
        lot = Lot.objects.create(
            lot_name=lot_name,
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=quantity,
            active=False,
            auctiontos_winner=winner,
            winning_price=5 if winner else None,
            **kwargs,
        )
        # date_end is set by the ending/selling code paths, not by Lot.save(), so tests set it here
        Lot.objects.filter(pk=lot.pk).update(date_end=timezone.now() - datetime.timedelta(days=days_ago))
        lot.refresh_from_db()
        return lot

    def enable_bap_permission(self):
        self.api_key.can_add_bap_points = True
        self.api_key.save(update_fields=["can_add_bap_points"])

    def get(self, **params):
        return self.client.get(self.url, params, HTTP_X_API_KEY=self.raw_key)

    def test_requires_bap_permission(self):
        self.assertEqual(self.get().status_code, 403)

    def test_requires_a_key(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_default_range_is_last_30_days(self):
        self.enable_bap_permission()
        response = self.get()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual([lot["lot_name"] for lot in data["results"]], ["Recent lot"])

    def test_returns_the_requested_fields(self):
        self.enable_bap_permission()
        lot = self.get().json()["results"][0]
        self.assertEqual(
            sorted(lot.keys()),
            [
                "bap_auto_reason",
                "bap_award",
                "bap_eligible",
                "bap_ineligible_reason",
                "bap_ineligible_reason_display",
                "bap_points_awarded",
                "category",
                "custom_checkbox",
                "custom_checkbox_name",
                "donation",
                "i_bred_this_fish",
                "lot_id",
                "lot_name",
                "lot_number_display",
                "manually_approved",
                "program",
                "quantity",
                "seller_email",
                "seller_name",
                "sold",
                "timestamp",
                "winner_email",
                "winner_name",
            ],
        )
        self.assertEqual(lot["lot_number_display"], str(self.recent_lot.lot_number_display))
        self.assertEqual(lot["quantity"], 6)
        self.assertEqual(lot["seller_name"], "Mike Smith")
        self.assertEqual(lot["seller_email"], "mike@example.com")
        self.assertEqual(lot["winner_name"], "Dana Lee")
        self.assertEqual(lot["winner_email"], "dana@example.com")
        self.assertTrue(lot["timestamp"].startswith(str(self.recent_lot.date_end.year)))

    def test_all_times_are_utc(self):
        """Mixing the club's local offset into some fields and UTC into others would be a trap."""
        self.enable_bap_permission()
        with override_settings(TIME_ZONE="America/New_York", USE_TZ=True):
            data = self.get(start="2026-01-01").json()
        self.assertTrue(data["start"].endswith("Z"), data["start"])
        self.assertTrue(data["end"].endswith("Z"), data["end"])
        self.assertTrue(data["results"][0]["timestamp"].endswith("Z"), data["results"][0]["timestamp"])

    def test_lot_id_is_the_lots_permanent_id(self):
        """lot_id is the caller's idempotency key, so it has to be the real primary key."""
        self.enable_bap_permission()
        lot = self.get().json()["results"][0]
        self.assertEqual(lot["lot_id"], self.recent_lot.pk)
        self.recent_lot.lot_name = "Renamed after the fact"
        self.recent_lot.save()
        self.assertEqual(self.get().json()["results"][0]["lot_id"], self.recent_lot.pk)

    def test_bap_fields_for_an_eligible_lot(self):
        self.enable_bap_permission()
        category = Category.objects.create(name="Test BAP Catfish", bap_points=5)
        ClubMember.objects.create(club=self.club, name="Mike Smith", email="mike@example.com")
        self.recent_lot.species_category = category
        self.recent_lot.i_bred_this_fish = True
        self.recent_lot.save()
        lot = self.get().json()["results"][0]
        self.assertTrue(lot["sold"])
        self.assertEqual(lot["category"], "Test BAP Catfish")
        self.assertEqual(lot["program"], "BAP")
        self.assertTrue(lot["i_bred_this_fish"])
        self.assertFalse(lot["donation"])
        self.assertTrue(lot["bap_eligible"])
        self.assertEqual(lot["bap_ineligible_reason"], "")
        self.assertEqual(lot["bap_ineligible_reason_display"], "")
        self.assertIsNone(lot["bap_award"])
        self.assertEqual(lot["bap_points_awarded"], 0)
        self.assertFalse(lot["manually_approved"])

    def test_ineligible_lot_says_why(self):
        self.enable_bap_permission()
        # Nobody ticked the breeder checkbox, so this lot was never a BAP candidate
        lot = self.get().json()["results"][0]
        self.assertFalse(lot["bap_eligible"])
        self.assertEqual(lot["bap_ineligible_reason"], "not_bred")
        self.assertEqual(lot["bap_ineligible_reason_display"], "Didn't breed this fish")

    def test_ineligible_reason_is_recomputed_not_read_from_the_stored_one(self):
        """bap_auto_reason is a historical record; bap_ineligible_reason answers "right now"."""
        self.enable_bap_permission()
        Lot.objects.filter(pk=self.recent_lot.pk).update(bap_auto_reason="not_club_member")
        lot = self.get().json()["results"][0]
        self.assertEqual(lot["bap_auto_reason"], "not_club_member")
        self.assertEqual(lot["bap_ineligible_reason"], "not_bred")

    def test_awarded_lot_reports_its_award(self):
        self.enable_bap_permission()
        member = ClubMember.objects.create(club=self.club, name="Mike Smith", email="mike@example.com")
        award = BapAward.objects.create(
            club_member=member,
            date=timezone.now().date(),
            points=5,
            lot=self.recent_lot,
            notes="Bred corydoras",
        )
        Lot.objects.filter(pk=self.recent_lot.pk).update(bap_points_awarded=5, manually_approved=True)
        lot = self.get().json()["results"][0]
        self.assertEqual(lot["bap_points_awarded"], 5)
        self.assertTrue(lot["manually_approved"])
        self.assertEqual(lot["bap_award"]["id"], award.pk)
        self.assertEqual(lot["bap_award"]["points"], 5)
        self.assertEqual(lot["bap_award"]["hap_points"], 0)
        self.assertEqual(lot["bap_award"]["cap_points"], 0)
        self.assertEqual(lot["bap_award"]["notes"], "Bred corydoras")
        # awarded_by is null on this award, which is how the site records an automatic one
        self.assertTrue(lot["bap_award"]["auto_awarded"])

    def test_manually_awarded_points_are_not_reported_as_automatic(self):
        self.enable_bap_permission()
        member = ClubMember.objects.create(club=self.club, name="Mike Smith", email="mike@example.com")
        BapAward.objects.create(
            club_member=member,
            date=timezone.now().date(),
            points=5,
            lot=self.recent_lot,
            awarded_by=self.owner,
        )
        self.assertFalse(self.get().json()["results"][0]["bap_award"]["auto_awarded"])

    def test_custom_checkbox_name_only_when_the_auction_uses_one(self):
        self.enable_bap_permission()
        self.assertEqual(self.get().json()["results"][0]["custom_checkbox_name"], "")
        self.auction.use_custom_checkbox_field = True
        self.auction.custom_checkbox_name = "Rare species"
        self.auction.save()
        self.assertEqual(self.get().json()["results"][0]["custom_checkbox_name"], "Rare species")

    def test_days_widens_the_range(self):
        self.enable_bap_permission()
        data = self.get(days=120).json()
        self.assertEqual(data["count"], 2)
        # newest first
        self.assertEqual([lot["lot_name"] for lot in data["results"]], ["Recent lot", "Old lot"])

    def test_explicit_start_and_end_dates(self):
        self.enable_bap_permission()
        old_day = (timezone.localtime(self.old_lot.date_end)).date()
        data = self.get(start=old_day.isoformat(), end=old_day.isoformat()).json()
        self.assertEqual([lot["lot_name"] for lot in data["results"]], ["Old lot"])

    def test_start_and_end_take_precedence_over_days(self):
        self.enable_bap_permission()
        old_day = (timezone.localtime(self.old_lot.date_end)).date()
        data = self.get(start=old_day.isoformat(), end=old_day.isoformat(), days=1).json()
        self.assertEqual([lot["lot_name"] for lot in data["results"]], ["Old lot"])

    def test_unparsable_date_is_a_400(self):
        self.enable_bap_permission()
        response = self.get(start="last tuesday")
        self.assertEqual(response.status_code, 400)
        # The error names the accepted formats without echoing what the caller sent
        self.assertIn("YYYY-MM-DD", response.json()["error"])
        self.assertNotIn("last tuesday", response.json()["error"])

    def test_bad_days_is_a_400(self):
        self.enable_bap_permission()
        self.assertEqual(self.get(days="lots").status_code, 400)
        self.assertEqual(self.get(days=0).status_code, 400)

    def test_end_before_start_is_a_400(self):
        self.enable_bap_permission()
        self.assertEqual(self.get(start="2026-03-01", end="2026-02-01").status_code, 400)

    def test_unsold_lot_has_blank_winner_fields(self):
        self.enable_bap_permission()
        self.make_lot("Nobody wanted it", days_ago=1)
        results = {lot["lot_name"]: lot for lot in self.get().json()["results"]}
        self.assertEqual(results["Nobody wanted it"]["winner_name"], "")
        self.assertEqual(results["Nobody wanted it"]["winner_email"], "")

    def test_deleted_and_banned_lots_are_excluded(self):
        self.enable_bap_permission()
        self.make_lot("Deleted lot", days_ago=1, is_deleted=True)
        self.make_lot("Banned lot", days_ago=1, banned=True)
        names = [lot["lot_name"] for lot in self.get().json()["results"]]
        self.assertEqual(names, ["Recent lot"])

    def test_lots_from_another_club_are_excluded(self):
        self.enable_bap_permission()
        other_club = Club.objects.create(name="Some Other Club", enable_breeder_award_program=True)
        other_auction = Auction.objects.create(
            created_by=self.owner,
            title="Other Club Auction",
            is_online=True,
            club=other_club,
            date_start=timezone.now() - datetime.timedelta(days=5),
            date_end=timezone.now() - datetime.timedelta(days=4),
        )
        other_lot = Lot.objects.create(lot_name="Other club lot", auction=other_auction, quantity=1, active=False)
        Lot.objects.filter(pk=other_lot.pk).update(date_end=timezone.now() - datetime.timedelta(days=4))
        names = [lot["lot_name"] for lot in self.get().json()["results"]]
        self.assertEqual(names, ["Recent lot"])

    def test_key_cannot_read_another_clubs_lots(self):
        self.enable_bap_permission()
        other_club = Club.objects.create(name="Not My Club", enable_breeder_award_program=True)
        url = reverse("api_club_bap_lots", kwargs={"slug": other_club.slug})
        response = self.client.get(url, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 403)

    def test_404_when_bap_is_off_for_the_club(self):
        self.enable_bap_permission()
        self.club.enable_breeder_award_program = False
        self.club.save(update_fields=["enable_breeder_award_program"])
        self.assertEqual(self.get().status_code, 404)

    def test_reading_lots_updates_last_used(self):
        self.enable_bap_permission()
        self.get()
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)


class ClubAuctionReadAPITests(WritableMediaRoot, TestCase):
    """The read-only auction and lot feed: /api/v1/clubs/<slug>/auctions/…"""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="auction_api_owner", password="testpass", email="a@example.com")
        self.club = Club.objects.create(name="Read API Club")
        self.auction = Auction.objects.create(
            created_by=self.owner,
            title="Read API Spring Auction",
            is_online=True,
            club=self.club,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=3),
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=2),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=2),
            summernote_description="<p>No bags on the floor</p>",
        )
        self.location = PickupLocation.objects.create(
            name="Clubhouse",
            auction=self.auction,
            address="1 Main St",
            pickup_time=timezone.now() + datetime.timedelta(days=4),
        )
        self.seller = AuctionTOS.objects.create(
            name="Mike Smith",
            email="mike@example.com",
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="601",
        )
        self.buyer = AuctionTOS.objects.create(
            name="Dana Lee",
            email="dana@example.com",
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="602",
        )
        self.species = Species.objects.create(genus="Corydoras", species="panda", common_name="Panda cory")
        self.category = Category.objects.create(name="Catfish")
        self.lot = Lot.objects.create(
            lot_name="6 Corydoras panda fry",
            auction=self.auction,
            auctiontos_seller=self.seller,
            auctiontos_winner=self.buyer,
            winning_price=22,
            quantity=6,
            species=self.species,
            species_category=self.category,
            summernote_description="<p>Home bred</p>",
            reserve_price=5,
            i_bred_this_fish=True,
        )
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Club Website",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.owner,
        )
        self.raw_key = raw_key

    def allow(self, *permissions):
        for permission in permissions:
            setattr(self.api_key, permission, True)
        self.api_key.save(update_fields=list(permissions))

    def url(self, name, **kwargs):
        return reverse(name, kwargs={"slug": self.club.slug, **kwargs})

    def get(self, name, params=None, **kwargs):
        return self.client.get(self.url(name, **kwargs), params or {}, HTTP_X_API_KEY=self.raw_key)

    def auctions(self, **params):
        return self.get("api_club_auctions", params)

    def auction_detail(self, identifier="current"):
        return self.get("api_club_auction_detail", identifier=identifier)

    def lots(self, identifier="current", **params):
        return self.get("api_club_auction_lots", params, identifier=identifier)

    # --- permissions -----------------------------------------------------------------

    def test_every_endpoint_needs_a_key(self):
        for name, kwargs in (
            ("api_club_auctions", {}),
            ("api_club_auction_detail", {"identifier": "current"}),
            ("api_club_auction_lots", {"identifier": "current"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(self.url(name, **kwargs)).status_code, 401)

    def test_auction_info_permission_is_separate_from_lots(self):
        self.assertEqual(self.auctions().status_code, 403)
        self.assertEqual(self.lots().status_code, 403)
        self.allow("can_read_auction_info")
        self.assertEqual(self.auctions().status_code, 200)
        self.assertEqual(self.auction_detail().status_code, 200)
        # Reading the auction says nothing about reading the lots in it.
        self.assertEqual(self.lots().status_code, 403)
        self.allow("can_read_public_lots")
        self.assertEqual(self.lots().status_code, 200)

    def test_lots_can_be_read_without_auction_info(self):
        self.allow("can_read_public_lots")
        self.assertEqual(self.lots().status_code, 200)
        self.assertEqual(self.auctions().status_code, 403)

    def test_key_cannot_read_another_clubs_auctions(self):
        self.allow("can_read_auction_info")
        other = Club.objects.create(name="Not My Club")
        response = self.client.get(
            reverse("api_club_auctions", kwargs={"slug": other.slug}), HTTP_X_API_KEY=self.raw_key
        )
        self.assertEqual(response.status_code, 403)

    def test_reading_updates_last_used(self):
        self.allow("can_read_auction_info")
        self.auctions()
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)

    # --- which auction ---------------------------------------------------------------

    def test_auction_list_names_current_and_latest(self):
        self.allow("can_read_auction_info")
        newer = Auction.objects.create(
            created_by=self.owner,
            title="Read API Next Year",
            is_online=True,
            club=self.club,
            date_start=timezone.now() + datetime.timedelta(days=300),
            date_end=timezone.now() + datetime.timedelta(days=303),
        )
        data = self.auctions().json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["current"], self.auction.slug)
        self.assertEqual(data["latest"], newer.slug)
        self.assertEqual([row["slug"] for row in data["results"]], [newer.slug, self.auction.slug])

    def test_latest_includes_an_unpromoted_auction(self):
        """The point of `latest`: an auction that has not been announced yet is still the newest."""
        self.allow("can_read_auction_info")
        draft = Auction.objects.create(
            created_by=self.owner,
            title="Read API Unannounced",
            is_online=True,
            club=self.club,
            promote_this_auction=False,
            date_start=timezone.now() + datetime.timedelta(days=200),
            date_end=timezone.now() + datetime.timedelta(days=203),
        )
        self.assertEqual(self.auctions().json()["latest"], draft.slug)
        self.assertEqual(self.auction_detail("latest").json()["slug"], draft.slug)

    def test_current_prefers_the_pinned_auction(self):
        self.allow("can_read_auction_info")
        pinned = Auction.objects.create(
            created_by=self.owner,
            title="Read API Pinned",
            is_online=True,
            club=self.club,
            date_start=timezone.now() + datetime.timedelta(days=20),
            date_end=timezone.now() + datetime.timedelta(days=23),
        )
        Club.objects.filter(pk=self.club.pk).update(current_auction=pinned)
        self.assertEqual(self.auction_detail("current").json()["slug"], pinned.slug)

    def test_a_real_slug_wins_over_the_reserved_word(self):
        self.allow("can_read_auction_info")
        Auction.objects.filter(pk=self.auction.pk).update(slug="latest")
        self.assertEqual(self.auction_detail("latest").json()["title"], "Read API Spring Auction")

    def test_an_auction_from_another_club_is_a_404(self):
        self.allow("can_read_auction_info")
        other_club = Club.objects.create(name="Somebody Else")
        theirs = Auction.objects.create(
            created_by=self.owner,
            title="Somebody Elses Auction",
            is_online=True,
            club=other_club,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        self.assertEqual(self.auction_detail(theirs.slug).status_code, 404)

    def test_no_current_auction_is_a_404_not_a_crash(self):
        self.allow("can_read_auction_info")
        Auction.objects.filter(pk=self.auction.pk).update(is_deleted=True)
        self.assertEqual(self.auction_detail("current").status_code, 404)

    # --- auction payload -------------------------------------------------------------

    def test_auction_detail_carries_the_rules_dates_and_settings(self):
        self.allow("can_read_auction_info")
        data = self.auction_detail(self.auction.slug).json()
        self.assertEqual(data["slug"], self.auction.slug)
        self.assertEqual(data["rules"], "<p>No bags on the floor</p>")
        self.assertTrue(data["url"].startswith("http"))
        self.assertEqual(data["club"], {"id": self.club.pk, "name": self.club.name})
        self.assertTrue(data["date_start"].endswith("Z"))
        self.assertTrue(data["status"]["started"])
        self.assertFalse(data["status"]["closed"])
        self.assertTrue(data["status"]["lot_submission_open"])
        self.assertEqual(data["lot_count"], 1)
        self.assertEqual(data["fees"]["lot_entry_fee"], self.auction.lot_entry_fee)
        self.assertEqual(data["lot_fields"]["use_scientific_name"], True)
        self.assertEqual([location["name"] for location in data["pickup_locations"]], ["Clubhouse"])

    def test_the_auctions_private_block(self):
        self.allow("can_read_auction_info")
        self.assertNotIn("private", self.auction_detail(self.auction.slug).json())
        self.allow("can_read_private_lots")
        private = self.auction_detail(self.auction.slug).json()["private"]
        self.assertEqual(private["participant_count"], 2)
        self.assertEqual(private["created_by"], "auction_api_owner")

    def test_the_google_drive_link_is_never_returned(self):
        """The sheet is shared "anyone with the link can view", so the link is the credential."""
        self.allow("can_read_auction_info", "can_read_public_lots", "can_read_private_lots")
        Auction.objects.filter(pk=self.auction.pk).update(
            google_drive_link="https://docs.google.com/spreadsheets/d/secret/edit"
        )
        blob = json.dumps(self.auction_detail(self.auction.slug).json()) + json.dumps(self.auctions().json())
        self.assertNotIn("docs.google.com", blob)
        self.assertNotIn("google_drive_link", blob)

    def test_custom_dropdown_options_come_with_the_auction(self):
        self.allow("can_read_auction_info")
        Auction.objects.filter(pk=self.auction.pk).update(
            use_custom_dropdown_field="allow", custom_dropdown_name="Tank size"
        )
        AuctionDropdown.objects.create(auction=self.auction, value="10 gallon")
        fields = self.auction_detail(self.auction.slug).json()["lot_fields"]
        self.assertEqual(fields["custom_dropdown_name"], "Tank size")
        self.assertEqual(fields["custom_dropdown_options"], ["10 gallon"])

    # --- lot payload -----------------------------------------------------------------

    def test_public_lot_payload(self):
        self.allow("can_read_public_lots")
        data = self.lots().json()
        self.assertEqual(data["auction"], self.auction.slug)
        self.assertEqual(data["count"], 1)
        lot = data["results"][0]
        self.assertEqual(lot["lot_id"], self.lot.pk)
        self.assertEqual(lot["lot_number"], str(self.lot.lot_number_display))
        self.assertEqual(lot["lot_name"], "6 Corydoras panda fry")
        self.assertEqual(lot["quantity"], 6)
        self.assertEqual(lot["description"], "<p>Home bred</p>")
        self.assertTrue(lot["sold"])
        self.assertEqual(lot["min_bid"], "5.00")
        self.assertEqual(lot["winning_price"], "22.00")
        self.assertNotIn("private", lot)

    def test_references_carry_the_id_and_the_name(self):
        self.allow("can_read_public_lots")
        lot = self.lots().json()["results"][0]
        self.assertEqual(lot["category"], {"id": self.category.pk, "name": "Catfish"})
        self.assertEqual(
            lot["species"],
            {"id": self.species.pk, "scientific_name": "Corydoras panda", "common_name": "Panda cory"},
        )

    def test_no_species_when_the_auction_turned_scientific_names_off(self):
        self.allow("can_read_public_lots")
        Auction.objects.filter(pk=self.auction.pk).update(use_scientific_name=False)
        self.assertIsNone(self.lots().json()["results"][0]["species"])

    def test_the_lot_link_is_absolute_and_tagged_with_the_key_name(self):
        self.allow("can_read_public_lots")
        url = self.lots().json()["results"][0]["url"]
        self.assertTrue(url.startswith("http"))
        self.assertIn(self.lot.lot_link, url)
        self.assertTrue(url.endswith("?src=Club+Website"))

    def test_public_payload_names_nobody(self):
        """The whole point of the split: a key on a public page cannot leak a person."""
        self.allow("can_read_public_lots", "can_read_auction_info")
        blob = json.dumps(self.lots().json()) + json.dumps(self.auction_detail().json())
        for secret in ("Mike Smith", "mike@example.com", "Dana Lee", "dana@example.com", "hunter2"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_private_block_holds_the_buyer_and_seller(self):
        self.allow("can_read_public_lots", "can_read_private_lots")
        private = self.lots().json()["results"][0]["private"]
        self.assertEqual(private["seller_name"], "Mike Smith")
        self.assertEqual(private["seller_email"], "mike@example.com")
        self.assertEqual(private["seller_number"], "601")
        self.assertEqual(private["winner_name"], "Dana Lee")
        self.assertEqual(private["winner_email"], "dana@example.com")
        self.assertEqual(private["winner_number"], "602")
        self.assertFalse(private["removed"])

    def test_removed_lots_are_public_to_nobody_and_visible_to_admins(self):
        self.allow("can_read_public_lots")
        removed = Lot.objects.create(
            lot_name="Pulled lot",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=1,
            banned=True,
            ban_reason="Wrong species",
        )
        self.assertEqual([lot["lot_name"] for lot in self.lots().json()["results"]], ["6 Corydoras panda fry"])
        self.allow("can_read_private_lots")
        rows = {lot["lot_name"]: lot for lot in self.lots().json()["results"]}
        self.assertIn("Pulled lot", rows)
        self.assertTrue(rows["Pulled lot"]["private"]["removed"])
        self.assertEqual(rows["Pulled lot"]["private"]["ban_reason"], "Wrong species")
        self.assertEqual(removed.lot_name, "Pulled lot")

    def test_deleted_lots_are_never_returned(self):
        self.allow("can_read_public_lots", "can_read_private_lots")
        Lot.objects.filter(pk=self.lot.pk).update(is_deleted=True)
        self.assertEqual(self.lots().json()["count"], 0)

    def test_lots_from_another_auction_are_not_included(self):
        self.allow("can_read_public_lots")
        other = Auction.objects.create(
            created_by=self.owner,
            title="Read API Other Auction",
            is_online=True,
            club=self.club,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        other_tos = AuctionTOS.objects.create(
            name="Someone", email="s@example.com", auction=other, pickup_location=self.location, bidder_number="1"
        )
        Lot.objects.create(lot_name="Not this one", auction=other, auctiontos_seller=other_tos, quantity=1)
        self.assertEqual([lot["lot_name"] for lot in self.lots().json()["results"]], ["6 Corydoras panda fry"])

    def test_limit_and_offset(self):
        self.allow("can_read_public_lots")
        for index in range(3):
            Lot.objects.create(
                lot_name=f"Extra {index}", auction=self.auction, auctiontos_seller=self.seller, quantity=1
            )
        first = self.lots(limit=2).json()
        self.assertEqual(first["count"], 4)
        self.assertEqual(len(first["results"]), 2)
        second = self.lots(limit=2, offset=2).json()
        self.assertEqual(len(second["results"]), 2)
        self.assertNotEqual([lot["lot_id"] for lot in first["results"]], [lot["lot_id"] for lot in second["results"]])

    def test_a_bad_limit_is_a_400(self):
        self.allow("can_read_public_lots")
        self.assertEqual(self.lots(limit="lots").status_code, 400)

    # --- filtering, ordering and sparse fields ---------------------------------------

    def extra_lots(self):
        """Two more lots, deliberately out of lot-number order by name and price."""
        plants = Category.objects.create(name="Plants")
        self.anubias = Lot.objects.create(
            lot_name="Anubias nana",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=1,
            species_category=plants,
            reserve_price=3,
            donation=True,
            summernote_description="<p>Grown on driftwood</p>",
            custom_field_1="CARES species",
            custom_dropdown="10 gallon",
        )
        self.zebra = Lot.objects.create(
            lot_name="Zebra danio trio",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=3,
            reserve_price=9,
            i_bred_this_fish=True,
            custom_checkbox=True,
        )
        return plants

    def names(self, **params):
        return [lot["lot_name"] for lot in self.lots(**params).json()["results"]]

    def test_a_parameter_named_after_a_column_matches_that_column(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.names(lot_name="zebra"), ["Zebra danio trio"])
        self.assertEqual(self.names(lot_name="nothing here"), [])
        self.assertEqual(self.names(description="driftwood"), ["Anubias nana"])
        self.assertEqual(self.names(custom_field_1="cares"), ["Anubias nana"])
        # A dropdown is a controlled vocabulary, so this one matches the whole value.
        self.assertEqual(self.names(custom_dropdown="10 GALLON"), ["Anubias nana"])
        self.assertEqual(self.names(custom_dropdown="gallon"), [])

    def test_filter_by_lot_number(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.names(lot_number=str(self.lot.lot_number_display)), ["6 Corydoras panda fry"])

    def test_the_generic_filter_searches_every_public_column(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        for term, expected in (
            ("zebra", ["Zebra danio trio"]),  # lot name
            ("driftwood", ["Anubias nana"]),  # description
            ("CARES", ["Anubias nana"]),  # custom text field
            ("10 gallon", ["Anubias nana"]),  # custom dropdown
            ("Corydoras panda", ["6 Corydoras panda fry"]),  # scientific name
            ("Panda cory", ["6 Corydoras panda fry"]),  # common name
            ("Plants", ["Anubias nana"]),  # category
        ):
            with self.subTest(term=term):
                self.assertEqual(self.names(filter=term), expected)

    def test_a_number_in_the_generic_filter_is_a_lot_number(self):
        """Otherwise "1" matches "10 gallon" and half the descriptions, and buries the lot."""
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.names(filter=str(self.lot.lot_number_display)), ["6 Corydoras panda fry"])
        self.assertEqual(self.names(filter=str(self.anubias.lot_number_display)), ["Anubias nana"])
        # The per-column parameter is still there for somebody who really does want digits in text.
        self.assertEqual(self.names(custom_dropdown="10 gallon"), ["Anubias nana"])

    def test_the_generic_filter_never_reaches_a_persons_name(self):
        """Otherwise a public key could confirm a name one character at a time."""
        self.allow("can_read_public_lots", "can_read_private_lots")
        for term in ("Mike", "Mike Smith", "mike@example.com", "601", "Dana Lee"):
            with self.subTest(term=term):
                self.assertEqual(self.names(filter=term), [])

    def test_seller_and_winner_filters_need_the_privacy_flag(self):
        self.allow("can_read_public_lots")
        response = self.lots(seller="Mike Smith")
        self.assertEqual(response.status_code, 400)
        self.assertIn("private", response.json()["error"])
        self.assertEqual(self.lots(winner="602").status_code, 400)
        self.allow("can_read_private_lots")
        self.assertEqual(self.names(seller="mike"), ["6 Corydoras panda fry"])
        self.assertEqual(self.names(seller="601"), ["6 Corydoras panda fry"])
        self.assertEqual(self.names(seller="mike@example.com"), ["6 Corydoras panda fry"])
        self.assertEqual(self.names(winner="Dana Lee"), ["6 Corydoras panda fry"])
        self.assertEqual(self.names(winner="nobody"), [])

    def test_filter_by_category_by_name_or_by_id(self):
        self.allow("can_read_public_lots")
        plants = self.extra_lots()
        self.assertEqual(self.names(category="plants"), ["Anubias nana"])
        self.assertEqual(self.names(category_id=plants.pk), ["Anubias nana"])
        self.assertEqual(self.lots(category="no such category").status_code, 400)
        self.assertEqual(self.lots(category="plants", category_id=plants.pk).status_code, 400)

    def test_filter_by_species(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.names(species_id=self.species.pk), ["6 Corydoras panda fry"])
        self.assertEqual(self.lots(species_id="cory").status_code, 400)

    def test_filter_by_the_boolean_lot_fields(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.names(donation="true"), ["Anubias nana"])
        self.assertEqual(self.names(i_bred_this_fish="true"), ["6 Corydoras panda fry", "Zebra danio trio"])
        self.assertEqual(self.names(custom_checkbox="true"), ["Zebra danio trio"])
        self.assertEqual(len(self.names(donation="false")), 2)
        self.assertEqual(self.lots(donation="maybe").status_code, 400)
        self.assertEqual(self.lots(custom_checkbox="maybe").status_code, 400)

    def test_filter_by_sold_reads_the_price_too(self):
        """Lot.sold is a winner *and* a price, so a winner with no price is not sold."""
        self.allow("can_read_public_lots")
        self.extra_lots()
        Lot.objects.filter(pk=self.zebra.pk).update(auctiontos_winner=self.buyer, winning_price=None)
        self.assertEqual(self.names(sold="true"), ["6 Corydoras panda fry"])
        self.assertEqual(sorted(self.names(sold="false")), ["Anubias nana", "Zebra danio trio"])

    def test_filters_narrow_the_count_as_well_as_the_page(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.lots(filter="anubias").json()["count"], 1)
        self.assertEqual(self.lots(lot_name="anubias").json()["count"], 1)

    def test_ordering(self):
        self.allow("can_read_public_lots")
        self.extra_lots()
        self.assertEqual(self.names()[0], "6 Corydoras panda fry")
        self.assertEqual(self.names(ordering="lot_name")[0], "6 Corydoras panda fry")
        self.assertEqual(self.names(ordering="-lot_name")[0], "Zebra danio trio")
        self.assertEqual(self.names(ordering="min_bid")[0], "Anubias nana")
        self.assertEqual(self.names(ordering="-min_bid")[0], "Zebra danio trio")

    def test_an_unknown_ordering_is_a_400(self):
        """Not a pass-through to order_by: sorting on a winner's email would leak it."""
        self.allow("can_read_public_lots")
        self.assertEqual(self.lots(ordering="auctiontos_winner__email").status_code, 400)
        self.assertEqual(self.lots(ordering="nonsense").status_code, 400)

    def test_fields_narrows_each_lot(self):
        self.allow("can_read_public_lots")
        lot = self.lots(fields="lot_number,lot_name,thumbnail").json()["results"][0]
        self.assertEqual(sorted(lot), ["lot_name", "lot_number", "thumbnail"])

    def test_fields_cannot_conjure_the_private_block(self):
        self.allow("can_read_public_lots")
        lot = self.lots(fields="lot_number,private").json()["results"][0]
        self.assertEqual(sorted(lot), ["lot_number"])

    def test_an_unknown_field_is_a_400(self):
        self.allow("can_read_public_lots")
        response = self.lots(fields="lot_name,seller_name")
        self.assertEqual(response.status_code, 400)
        self.assertIn("seller_name", response.json()["error"])

    def test_fields_works_on_one_lot_too(self):
        self.allow("can_read_public_lots")
        response = self.client.get(
            self.url(
                "api_club_auction_lot_detail",
                identifier="current",
                lot_number=str(self.lot.lot_number_display),
            ),
            {"fields": "lot_number,url"},
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(sorted(response.json()), ["lot_number", "url"])

    # --- images ----------------------------------------------------------------------

    def make_image(self, lot, **kwargs):
        from PIL import Image as PILImage

        buffer = io.BytesIO()
        PILImage.new("RGB", (10, 10), "blue").save(buffer, format="JPEG")
        return LotImage.objects.create(
            lot_number=lot,
            image=SimpleUploadedFile("cory.jpg", buffer.getvalue(), content_type="image/jpeg"),
            **kwargs,
        )

    def test_thumbnail_and_the_image_list(self):
        self.allow("can_read_public_lots")
        primary = self.make_image(self.lot, is_primary=True, caption="Parents", image_source="ACTUAL")
        self.make_image(self.lot, is_primary=False)
        lot = self.lots().json()["results"][0]
        self.assertEqual(len(lot["images"]), 2)
        self.assertTrue(lot["thumbnail"].startswith("http"))
        first = lot["images"][0]
        self.assertEqual(first["id"], primary.pk)
        self.assertTrue(first["is_primary"])
        self.assertEqual(first["caption"], "Parents")
        self.assertEqual(first["image_source_display"], "This picture is of the exact item")
        self.assertTrue(first["url"].startswith("http"))
        self.assertTrue(first["thumbnail"].startswith("http"))

    def test_a_lot_with_no_pictures_has_a_null_thumbnail_and_an_empty_list(self):
        self.allow("can_read_public_lots")
        lot = self.lots().json()["results"][0]
        self.assertIsNone(lot["thumbnail"])
        self.assertEqual(lot["images"], [])

    def test_the_thumbnail_falls_back_to_an_auto_added_image(self):
        """The same picture the lot list on this site shows, so a club's own page isn't blank."""
        self.allow("can_read_public_lots")
        older = Auction.objects.create(
            created_by=self.owner,
            title="Read API Last Year",
            is_online=True,
            club=self.club,
            date_start=timezone.now() - datetime.timedelta(days=400),
            date_end=timezone.now() - datetime.timedelta(days=397),
        )
        older_tos = AuctionTOS.objects.create(
            name="Mike Smith", email="mike@example.com", auction=older, pickup_location=self.location, bidder_number="1"
        )
        older_lot = Lot.objects.create(
            lot_name="6 Corydoras panda fry", auction=older, auctiontos_seller=older_tos, quantity=6
        )
        self.make_image(older_lot, is_primary=True)
        # Lot.auto_image only reaches back into auctions run by *this* auction's admin team, and
        # membership of that team is an AuctionTOS row rather than the created_by column.
        AuctionTOS.objects.create(
            name="Admin",
            email="a@example.com",
            user=self.owner,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="1",
            is_admin=True,
        )
        self.assertIsNotNone(self.lots().json()["results"][0]["thumbnail"])

    # --- one lot ---------------------------------------------------------------------

    def test_one_lot_by_its_number(self):
        self.allow("can_read_public_lots")
        response = self.client.get(
            self.url(
                "api_club_auction_lot_detail",
                identifier="current",
                lot_number=str(self.lot.lot_number_display),
            ),
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lot_id"], self.lot.pk)

    def test_an_unknown_lot_number_is_a_404(self):
        self.allow("can_read_public_lots")
        response = self.client.get(
            self.url("api_club_auction_lot_detail", identifier="current", lot_number="99999"),
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 404)

    # --- a person rather than a key ---------------------------------------------------

    def test_a_club_admin_can_read_it_signed_in(self):
        ClubMember.objects.create(club=self.club, user=self.owner, permission_manage_auctions=True)
        self.client.force_login(self.owner)
        response = self.client.get(self.url("api_club_auction_lots", identifier="current"))
        self.assertEqual(response.status_code, 200)
        # No key, so no ?src= to attribute the traffic to.
        self.assertNotIn("?src=", response.json()["results"][0]["url"])
        self.assertIn("private", response.json()["results"][0])

    def test_an_ordinary_club_member_cannot(self):
        member = User.objects.create_user(username="just_a_member", password="testpass", email="m@example.com")
        ClubMember.objects.create(club=self.club, user=member)
        self.client.force_login(member)
        self.assertEqual(self.client.get(self.url("api_club_auctions")).status_code, 403)


class ParseBoolEnvTests(TestCase):
    """Cover fishauctions._env.parse_bool_env, which gates settings.DEBUG."""

    def test_unset_returns_default(self) -> None:
        self.assertTrue(parse_bool_env(None, default=True))
        self.assertFalse(parse_bool_env(None, default=False))

    def test_truthy_spellings(self) -> None:
        for value in ("1", "true", "True", "TRUE", "yes", "on", "t", "y", "  true  "):
            with self.subTest(value=value):
                self.assertTrue(parse_bool_env(value, default=False))

    def test_falsy_spellings(self) -> None:
        # "False" is the legacy spelling in .env.example; "false" was the
        # previously-broken case under the old `== "False"` check.
        # "   " covers the whitespace-only path documented in _env.py.
        for value in ("0", "false", "False", "FALSE", "no", "off", "f", "n", "", "   "):
            with self.subTest(value=value):
                self.assertFalse(parse_bool_env(value, default=True))

    def test_unrecognized_value_raises(self) -> None:
        # A typo in the env value must fail loudly, not silently default.
        with self.assertRaises(ValueError):
            parse_bool_env("maybe", default=False)

    def test_entrypoint_uses_shared_bool_parser_for_debug(self) -> None:
        entrypoint = Path(__file__).resolve().parent.parent / "entrypoint.sh"
        entrypoint_text = entrypoint.read_text(encoding="utf-8")

        self.assertIn("parse_bool_env", entrypoint_text)
        self.assertNotIn('[ "${DEBUG}" = "True" ]', entrypoint_text)

    def test_entrypoint_requires_setup_complete(self) -> None:
        entrypoint = Path(__file__).resolve().parent.parent / "entrypoint.sh"
        entrypoint_text = entrypoint.read_text(encoding="utf-8")

        self.assertIn("SETUP_COMPLETE", entrypoint_text)
        self.assertIn("Run ./update.sh", entrypoint_text)

    def test_update_script_marks_setup_complete(self) -> None:
        update_script = Path(__file__).resolve().parent.parent / "update.sh"
        update_text = update_script.read_text(encoding="utf-8")

        self.assertIn("SETUP_COMPLETE", update_text)
        self.assertIn("SITE_DOMAIN", update_text)


class RequireSecureProdSecretsTests(TestCase):
    """Cover fishauctions._env.require_secure_prod_secrets."""

    def test_all_secrets_set_passes(self) -> None:
        require_secure_prod_secrets(
            {
                "SECRET_KEY": "a-real-secret-value",
                "DATABASE_PASSWORD": "real-db-pw",
                "REDIS_PASSWORD": "real-redis-pw",
            }
        )

    def test_each_insecure_value_raises(self) -> None:
        # None covers "env var unset"; "" and "unsecure" are the literal
        # placeholders shipped in settings.py and docker-compose.yaml.
        for value in (None, "", "unsecure"):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    require_secure_prod_secrets({"SECRET_KEY": value})
                self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_multiple_offenders_reported_together(self) -> None:
        with self.assertRaises(ImproperlyConfigured) as ctx:
            require_secure_prod_secrets(
                {
                    "SECRET_KEY": "unsecure",
                    "DATABASE_PASSWORD": None,
                    "REDIS_PASSWORD": "real-redis-pw",
                }
            )
        message = str(ctx.exception)
        self.assertIn("SECRET_KEY", message)
        self.assertIn("DATABASE_PASSWORD", message)
        self.assertNotIn("REDIS_PASSWORD", message)


class ClubAuctionIntegrationTests(TestCase):
    """Tests for the club-auction integration feature"""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="ca_owner", password="testpass", email="ca_owner@example.com")
        self.club = Club.objects.create(
            name="Auction Test Club",
        )
        # Set club on owner's userdata
        self.owner.userdata.club = self.club
        self.owner.userdata.save()
        # Make owner a club member with admin permission
        self.owner_member = ClubMember.objects.create(
            club=self.club,
            user=self.owner,
            name="Owner User",
            permission_admin=True,
        )

    def _create_auction_via_view(self, user):
        """Helper to create an auction via the create auction view."""
        self.client.login(username=user.username, password="testpass")
        from django.utils import timezone

        start = timezone.now() + timezone.timedelta(days=7)
        response = self.client.post(
            reverse("create_auction") + "?online=true",
            {
                "title": "Test Auction",
                "date_start": start.strftime("%Y-%m-%d %H:%M"),
                "is_online": "true",
                "cloned_from": "",
            },
        )
        return response

    def test_auction_associated_with_club_on_creation(self):
        """Auction is automatically associated with club when creator has admin permission"""
        response = self._create_auction_via_view(self.owner)
        # Should redirect (success)
        self.assertEqual(response.status_code, 302)
        auction = Auction.objects.filter(created_by=self.owner).last()
        self.assertIsNotNone(auction)
        self.assertEqual(auction.club, self.club)

    def test_auction_history_created_for_club_association(self):
        """History note is created when auction is associated with club"""
        self._create_auction_via_view(self.owner)
        auction = Auction.objects.filter(created_by=self.owner).last()
        history = AuctionHistory.objects.filter(auction=auction, applies_to="RULES").order_by("pk")
        actions = [h.action for h in history]
        self.assertTrue(any("Automatically associated with club" in a for a in actions))

    def test_auction_creation_updates_last_auction_used(self):
        self._create_auction_via_view(self.owner)
        auction = Auction.objects.filter(created_by=self.owner).last()
        self.owner.userdata.refresh_from_db()
        self.assertEqual(self.owner.userdata.last_auction_used, auction)

    def test_no_club_association_when_no_permission(self):
        """Auction is not associated with club if user has no admin/manage_auctions permission"""
        user_no_perm = User.objects.create_user(username="no_perm", password="testpass", email="no_perm@example.com")
        user_no_perm.userdata.club = self.club
        user_no_perm.userdata.save()
        # Add as member with no relevant permissions
        ClubMember.objects.create(club=self.club, user=user_no_perm, name="No Perm")
        response = self._create_auction_via_view(user_no_perm)
        self.assertEqual(response.status_code, 302)
        auction = Auction.objects.filter(created_by=user_no_perm).last()
        self.assertIsNone(auction.club)

    def test_club_detail_shows_promoted_auctions(self):
        """The club page's event list includes promoted auctions belonging to that club"""
        auction = Auction.objects.create(
            title="Club Promoted Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=self.owner,
            club=self.club,
            promote_this_auction=True,
        )
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(auction, [event.auction for event in response.context["upcoming_events"]])

    def test_club_detail_does_not_show_unpromoted_auctions(self):
        """Unpromoted auctions are never mirrored onto the calendar"""
        Auction.objects.create(
            title="Unpromoted Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=self.owner,
            club=self.club,
            promote_this_auction=False,
        )
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(response.context["upcoming_events"])), 0)

    def test_club_detail_lists_soonest_events_first(self):
        """The list is a calendar now, so it reads forward in time rather than newest-first."""
        sooner = Auction.objects.create(
            title="Sooner Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=self.owner,
            club=self.club,
            promote_this_auction=True,
        )
        later = Auction.objects.create(
            title="Later Auction",
            date_start=timezone.now() + timezone.timedelta(days=21),
            date_end=timezone.now() + timezone.timedelta(days=28),
            created_by=self.owner,
            club=self.club,
            promote_this_auction=True,
        )
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        events = list(response.context["upcoming_events"])
        self.assertEqual([event.auction_id for event in events[:2]], [sooner.pk, later.pk])

    def test_role_assignment_fills_club_on_existing_auctions(self):
        """When a member gains manage_auctions permission, existing auctions get club filled in"""
        user2 = User.objects.create_user(username="role_assign", password="testpass", email="role_assign@example.com")
        # User must have the same club in preferences for the signal to associate
        user2.userdata.club = self.club
        user2.userdata.save()
        # Create auction without club
        auction = Auction.objects.create(
            title="No Club Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=user2,
            club=None,
        )
        self.assertIsNone(auction.club)
        # Create club member and assign manage_auctions permission
        ClubMember.objects.create(club=self.club, user=user2, name="Role Assign", permission_manage_auctions=True)
        # Auction should now have club set
        auction.refresh_from_db()
        self.assertEqual(auction.club, self.club)

    def test_role_assignment_creates_history_notes(self):
        """Auction history note is created when club is set via permission assignment"""
        user2 = User.objects.create_user(username="role_hist", password="testpass", email="role_hist@example.com")
        # User must have the same club in preferences for the signal to associate
        user2.userdata.club = self.club
        user2.userdata.save()
        auction = Auction.objects.create(
            title="Role History Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=user2,
            club=None,
        )
        ClubMember.objects.create(club=self.club, user=user2, name="Role Hist", permission_manage_auctions=True)
        history = AuctionHistory.objects.filter(auction=auction, applies_to="RULES")
        self.assertTrue(history.exists())
        self.assertTrue(any("Automatically associated with club" in h.action for h in history))

    def test_role_assignment_no_effect_without_club_in_preferences(self):
        """Permission assignment does NOT fill club if user's preferences club differs"""
        user3 = User.objects.create_user(username="role_nopref", password="testpass", email="role_nopref@example.com")
        # user3 has no club in preferences (default is None)
        auction = Auction.objects.create(
            title="No Pref Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=user3,
            club=None,
        )
        ClubMember.objects.create(club=self.club, user=user3, name="No Pref", permission_manage_auctions=True)
        auction.refresh_from_db()
        # club should remain None since user's preferences don't point to this club
        self.assertIsNone(auction.club)

    def test_club_abbreviation_auto_filled_on_save(self):
        """Club abbreviation is auto-filled from initials when blank"""
        from auctions.models import Club as ClubModel  # noqa: PLC0415

        club = ClubModel.objects.create(name="Greater Pacific Fish Society")
        self.assertEqual(club.abbreviation, "GPFS")

    def test_club_abbreviation_not_overwritten_if_set(self):
        """Existing club abbreviation is not overwritten on save"""
        from auctions.models import Club as ClubModel  # noqa: PLC0415

        club = ClubModel.objects.create(name="Greater Pacific Fish Society", abbreviation="CUSTOM")
        self.assertEqual(club.abbreviation, "CUSTOM")

    def test_club_abbreviation_persisted_with_update_fields(self):
        """Auto-filled abbreviation is saved even when update_fields is specified"""
        from auctions.models import Club as ClubModel  # noqa: PLC0415

        club = ClubModel.objects.create(name="Pacific Fish Club")
        # Clear abbreviation and resave with update_fields
        ClubModel.objects.filter(pk=club.pk).update(abbreviation="")
        club.refresh_from_db()
        self.assertEqual(club.abbreviation, "")
        club.save(update_fields=["name"])
        club.refresh_from_db()
        self.assertEqual(club.abbreviation, "PFC")

    def test_club_detail_accessible_with_manage_auctions_role(self):
        """An officer who only manages auctions can still open their club's page."""
        club_no_page = Club.objects.create(name="Private Club")
        user_manage = User.objects.create_user(username="manage_user", password="testpass", email="manage@example.com")
        ClubMember.objects.create(club=club_no_page, user=user_manage, permission_manage_auctions=True)
        self.client.login(username="manage_user", password="testpass")
        url = reverse("club_detail", kwargs={"slug": club_no_page.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_club_admins_added_as_tos_on_pickup_location_create(self):
        """When first pickup location is created for a club auction, club admin members get AuctionTOS"""
        # Create a second user with manage_auctions permission
        user2 = User.objects.create_user(username="admin2_tos", password="testpass", email="admin2_tos@example.com")
        ClubMember.objects.create(
            club=self.club,
            user=user2,
            name="Admin Two",
            email="admin2_tos@example.com",
            permission_manage_auctions=True,
        )
        # Create auction already associated with club (no location yet)
        auction = Auction.objects.create(
            title="Location Hook Auction",
            date_start=timezone.now() + timezone.timedelta(days=7),
            date_end=timezone.now() + timezone.timedelta(days=14),
            created_by=self.owner,
            club=self.club,
        )
        self.assertFalse(AuctionTOS.objects.filter(auction=auction, user=user2).exists())
        # Create a pickup location via the view
        self.client.login(username=self.owner.username, password="testpass")
        response = self.client.post(
            reverse("create_auction_pickup_location", kwargs={"slug": auction.slug}),
            {
                "name": "Main Location",
                "pickup_time": (timezone.now() + timezone.timedelta(days=14)).strftime("%Y-%m-%d %H:%M"),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuctionTOS.objects.filter(auction=auction, user=user2).exists())
