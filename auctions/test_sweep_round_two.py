"""Regression tests for the second review sweep: club member merge roles, BAP award lots, the API-key member
write allowlist, check-in of existing members and renewing deactivated ones, the donation send quota, the
browser timezone cookie, club page bad input, Google sign-in onto an unverified account, Tap to Pay for club
officers, password changes signing out the app, NaN coordinates, malformed PassKit bodies, device handover,
removing a winning bid, editing a lot at the per-seller limit, and palette lot scoping, refunds, current
auction permissions and money formatting.
"""

import datetime
import uuid
from decimal import Decimal
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions import donations, palette_actions
from auctions.mobile.services.devices import DeviceService
from auctions.mobile.services.payments import PaymentService
from auctions.mobile.views import MobileGoogleAuthView
from auctions.models import (
    AppleDeviceRegistration,
    Auction,
    AuctionTOS,
    BapAward,
    Bid,
    Club,
    ClubAPIKey,
    ClubMember,
    DonationEmail,
    Invoice,
    Lot,
    MobileDevice,
    PickupLocation,
    SquareSeller,
)
from auctions.test_donations import ROUTING_SETTINGS, DonationTestMixin
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase

FAKE_WALLET_SETTINGS = {
    "APPLE_WALLET_CERT_FILE": "cert.p12",
    "APPLE_WALLET_CERT_PASSWORD": "",
    "APPLE_WALLET_WWDR_FILE": "wwdr.pem",
    "APPLE_WALLET_PASS_TYPE_IDENTIFIER": "pass.com.example.membership",
    "APPLE_WALLET_TEAM_IDENTIFIER": "ABCDE12345",
}


def _club_auction(club, creator, **kwargs):
    now = timezone.now()
    defaults = {
        "title": f"{club.name} auction",
        "created_by": creator,
        "club": club,
        "date_start": now - datetime.timedelta(days=3),
        "date_end": now - datetime.timedelta(days=1),
    }
    defaults.update(kwargs)
    auction = Auction.objects.create(**defaults)
    location = PickupLocation.objects.create(
        name="pickup", auction=auction, pickup_time=now + datetime.timedelta(days=2)
    )
    return auction, location


@override_settings(SINGLE_CLUB_MODE=False)
class ClubMemberMergeRoleTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Merge Role Club")
        self.officer = User.objects.create_user(username="merge_officer", password="pw", email="off@example.com")
        self.officer_member = ClubMember.objects.create(
            club=self.club, user=self.officer, name="Officer", permission_add_edit=True
        )
        self.president = ClubMember.objects.create(
            club=self.club, name="President", permission_admin=True, permission_money=True
        )

    def _merge(self, source, target_pk, name="Kept"):
        return self.client.post(
            reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": source.pk}),
            {"step": "review", "target": target_pk, "name": name, "email": "", "phone_number": "", "address": ""},
        )

    def test_add_edit_cannot_merge_roles_into_their_own_row(self):
        self.client.force_login(self.officer)
        response = self._merge(self.president, self.officer_member.pk, name="Officer")
        self.assertEqual(response.status_code, 302)
        self.officer_member.refresh_from_db()
        self.president.refresh_from_db()
        self.assertTrue(self.president.is_deleted)
        self.assertFalse(self.officer_member.permission_admin)
        self.assertFalse(self.officer_member.permission_money)

    def test_a_club_admin_merging_carries_the_roles_over(self):
        admin = User.objects.create_user(username="merge_admin", password="pw", email="adm@example.com")
        ClubMember.objects.create(club=self.club, user=admin, name="Admin", permission_admin=True)
        plain = ClubMember.objects.create(club=self.club, name="Plain")
        self.client.force_login(admin)
        response = self._merge(self.president, plain.pk, name="Plain")
        self.assertEqual(response.status_code, 302)
        plain.refresh_from_db()
        self.assertTrue(plain.permission_admin)
        self.assertTrue(plain.permission_money)

    def test_merging_a_member_into_themselves_is_a_404(self):
        self.client.force_login(self.officer)
        response = self._merge(self.president, self.president.pk, name="President")
        self.assertEqual(response.status_code, 404)
        self.president.refresh_from_db()
        self.assertFalse(self.president.is_deleted)

    def test_a_non_numeric_target_is_a_404(self):
        self.client.force_login(self.officer)
        response = self._merge(self.president, "abc")
        self.assertEqual(response.status_code, 404)
        self.president.refresh_from_db()
        self.assertFalse(self.president.is_deleted)


@override_settings(SINGLE_CLUB_MODE=False)
class BapAwardAdminLotTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="bap_manager", password="pw", email="bm@example.com")
        self.club = Club.objects.create(name="BAP Sweep Club", enable_breeder_award_program=True)
        ClubMember.objects.create(club=self.club, user=self.manager, name="Manager", permission_manage_bap=True)
        self.member = ClubMember.objects.create(club=self.club, name="Breeder")
        self.auction, location = _club_auction(self.club, self.manager)
        tos = AuctionTOS.objects.create(auction=self.auction, pickup_location=location, name="Breeder")
        self.lot = Lot.objects.create(
            lot_name="Guppies", auction=self.auction, auctiontos_seller=tos, quantity=1, active=False
        )
        self.other_club = Club.objects.create(name="Other BAP Club", enable_breeder_award_program=True)
        other_auction, other_location = _club_auction(self.other_club, self.manager)
        other_tos = AuctionTOS.objects.create(auction=other_auction, pickup_location=other_location, name="X")
        self.other_lot = Lot.objects.create(
            lot_name="Platies", auction=other_auction, auctiontos_seller=other_tos, quantity=1, active=False
        )
        self.client.force_login(self.manager)

    def _form(self, points):
        return {"club_member": self.member.pk, "date": "2026-09-01", "points": points, "notes": ""}

    def test_another_clubs_lot_is_ignored(self):
        url = reverse("bapaward_create", kwargs={"slug": self.club.slug}) + f"?lot_pk={self.other_lot.pk}"
        response = self.client.post(url, self._form(9))
        self.assertEqual(response.status_code, 200)
        award = BapAward.objects.get(club_member=self.member)
        self.assertIsNone(award.lot)
        self.other_lot.refresh_from_db()
        self.assertFalse(self.other_lot.manually_approved)
        self.assertNotEqual(self.other_lot.bap_points_awarded, 9)

    def test_a_non_numeric_lot_pk_does_not_500(self):
        url = reverse("bapaward_create", kwargs={"slug": self.club.slug}) + "?lot_pk=abc"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, self._form(3)).status_code, 200)

    def test_editing_an_award_updates_its_lot(self):
        award = BapAward.objects.create(club_member=self.member, date=datetime.date(2026, 9, 1), points=5, lot=self.lot)
        response = self.client.post(reverse("bapaward_admin", kwargs={"pk": award.pk}), self._form(12))
        self.assertEqual(response.status_code, 200)
        award.refresh_from_db()
        self.assertEqual(award.points, 12)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.bap_points_awarded, 12)


class ClubMemberAPIKeyAllowlistTests(TestCase):
    """What an API key may write on a member is an allowlist."""

    FORBIDDEN = {
        "membership_expiration_date": "2099-01-01",
        "apple_pass_auth_token": "stolen-token",
        "lat": 12.5,
        "lng": -45.25,
        "cached_total_sold": "999.00",
        "mailchimp_status": "subscribed",
    }

    def setUp(self):
        owner = User.objects.create_user(username="allowlist_owner", password="pw", email="ao@example.com")
        self.club = Club.objects.create(name="Allowlist Club")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Allowlist API",
            prefix=prefix,
            key_hash=key_hash,
            created_by=owner,
            can_add_club_members=True,
            can_update_club_members=True,
        )
        self.raw_key = raw_key
        self.member = ClubMember.objects.create(club=self.club, name="Existing", email="existing@example.com")

    def _assert_untouched(self, member):
        self.assertIsNone(member.membership_expiration_date)
        self.assertNotEqual(member.apple_pass_auth_token, "stolen-token")
        self.assertIsNone(member.lat)
        self.assertIsNone(member.lng)
        self.assertIsNone(member.cached_total_sold)
        self.assertEqual(member.mailchimp_status, "")

    def _assert_response_hides_private_fields(self, body):
        for key in ("apple_pass_auth_token", "lat", "lng"):
            self.assertNotIn(key, body)

    def test_create_ignores_fields_outside_the_allowlist(self):
        response = self.client.post(
            reverse("api_club_members", kwargs={"slug": self.club.slug}),
            {"name": "New Member", "email": "new@example.com", **self.FORBIDDEN},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self._assert_response_hides_private_fields(response.json())
        self._assert_untouched(ClubMember.objects.get(club=self.club, email="new@example.com"))

    def test_update_ignores_fields_outside_the_allowlist(self):
        response = self.client.patch(
            reverse("api_club_member_detail", kwargs={"slug": self.club.slug, "pk": self.member.pk}),
            {"memo": "after", **self.FORBIDDEN},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self._assert_response_hides_private_fields(response.json())
        self.member.refresh_from_db()
        self.assertEqual(self.member.memo, "after")
        self._assert_untouched(self.member)


@override_settings(SINGLE_CLUB_MODE=False)
class ClubMemberCheckInAndRenewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="checkin_admin", password="pw", email="ca@example.com")
        self.club = Club.objects.create(name="Check-in Sweep Club")
        ClubMember.objects.create(club=self.club, user=self.admin, name="Admin", permission_add_edit=True)
        self.auction, self.location = _club_auction(
            self.club,
            self.admin,
            manage_users_through_club="checkin",
            date_start=timezone.now() - datetime.timedelta(hours=1),
            date_end=timezone.now() + datetime.timedelta(hours=5),
        )
        self.member = ClubMember.objects.create(
            club=self.club, name="Stored Name", email="stored@example.com", bidder_number="12"
        )
        self.walk_in = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.location, name="Walk In", bidder_number="77"
        )
        self.client.force_login(self.admin)

    def test_checking_in_an_existing_member_ignores_posted_edits(self):
        url = reverse("clubmember_create", kwargs={"slug": self.club.slug}) + f"?auction={self.auction.slug}"
        self.client.post(
            url,
            {
                "_existing_member_pk": self.member.pk,
                "name": "Posted Name",
                "email": "posted@example.com",
                "bidder_number": "77",
                "contact_status": "contact",
            },
        )
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=self.member)
        self.member.refresh_from_db()
        self.assertEqual(
            (self.member.name, self.member.email, self.member.bidder_number),
            ("Stored Name", "stored@example.com", "12"),
        )
        self.assertEqual((tos.name, tos.email, tos.bidder_number), ("Stored Name", "stored@example.com", "12"))
        self.walk_in.refresh_from_db()
        self.assertEqual(self.walk_in.bidder_number, "77")

    def test_a_deactivated_member_cannot_be_renewed(self):
        ClubMember.objects.filter(pk=self.member.pk).update(is_deleted=True)
        url = reverse("club_member_renew", kwargs={"pk": self.member.pk})
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.member.refresh_from_db()
        self.assertIsNone(self.member.membership_expiration_date)


@isolated_cache("sweep-round-two-donations")
@override_settings(**ROUTING_SETTINGS)
class DonationQuotaCountsWhatWasSentTests(DonationTestMixin, TestCase):
    """Drafting spends the allowance too, but the day's last draft must still be sendable."""

    def _spend_the_draft_budget(self):
        cache.set(donations._rate_limit_key(self.club, "draft"), donations.MAX_DONATION_EMAILS_PER_DAY, timeout=3600)

    def _record_sent(self, count):
        for index in range(count):
            DonationEmail.objects.create(
                vendor=self.vendor, direction=DonationEmail.DIRECTION_OUTGOING, subject=f"R{index}", body="Please"
            )

    def test_a_drafted_email_goes_out_while_fewer_than_the_limit_were_sent(self):
        self._spend_the_draft_budget()
        self._record_sent(donations.MAX_DONATION_EMAILS_PER_DAY - 1)
        self.assertTrue(donations.donation_email_quota(self.club).exhausted)
        donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        self.assertEqual(
            DonationEmail.objects.filter(direction=DonationEmail.DIRECTION_OUTGOING).count(),
            donations.MAX_DONATION_EMAILS_PER_DAY,
        )

    def test_once_the_limit_is_sent_it_is_refused(self):
        self._spend_the_draft_budget()
        self._record_sent(donations.MAX_DONATION_EMAILS_PER_DAY)
        with self.assertRaises(donations.DonationSendError):
            donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)


@override_settings(SINGLE_CLUB_MODE=False)
class BrowserTimezoneCookieTests(StandardTestCase):
    def test_an_invalid_cookie_falls_back_to_the_site_timezone(self):
        from auctions.views.base import browser_timezone

        request = RequestFactory().get("/")
        request.COOKIES["user_timezone"] = "Not/AZone"
        self.assertEqual(browser_timezone(request), settings.TIME_ZONE)
        request.COOKIES["user_timezone"] = "America/Chicago"
        self.assertEqual(browser_timezone(request), "America/Chicago")

    def test_the_club_event_page_survives_a_bad_cookie(self):
        club = Club.objects.create(name="Timezone Club")
        ClubMember.objects.create(club=club, user=self.user, name="Editor", permission_edit_club=True)
        self.client.force_login(self.user)
        self.client.cookies["user_timezone"] = "Not/AZone"
        response = self.client.get(reverse("club_event_add", kwargs={"slug": club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_the_auction_edit_page_survives_a_bad_cookie(self):
        self.client.force_login(self.user)
        self.client.cookies["user_timezone"] = "Not/AZone"
        response = self.client.get(reverse("edit_auction", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)


@override_settings(SINGLE_CLUB_MODE=False)
class ClubDetailBadInputTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="detail_admin", password="pw", email="da@example.com")
        self.club = Club.objects.create(name="Detail Input Club")
        ClubMember.objects.create(club=self.club, user=self.admin, name="Admin", permission_admin=True)

    def test_a_malformed_member_uuid_does_not_500(self):
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}) + "?user=not-a-uuid")
        self.assertEqual(response.status_code, 200)

    def test_make_current_with_a_non_numeric_auction_does_not_500(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club_detail", kwargs={"slug": self.club.slug}), {"action": "make_current", "auction": "abc"}
        )
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertIsNone(self.club.current_auction)


@isolated_cache("sweep-round-two-google")
@override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client.apps.googleusercontent.com")
class GoogleSignInOntoExistingAccountTests(TestCase):
    EMAIL = "owner@example.com"

    def setUp(self):
        cache.clear()
        self.squatter = User.objects.create_user(username="squatter", password="secret-pw", email=self.EMAIL)

    def _sign_in(self):
        with patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value={"sub": "google-sub-1", "email": self.EMAIL, "email_verified": True},
        ):
            return self.client.post(
                reverse("mobile-auth-google"), {"id_token": "stub"}, content_type="application/json"
            )

    def test_an_unverified_address_loses_its_password(self):
        EmailAddress.objects.create(user=self.squatter, email=self.EMAIL, verified=False, primary=True)
        response = self._sign_in()
        self.assertEqual(response.status_code, 200, response.content)
        self.squatter.refresh_from_db()
        self.assertFalse(self.squatter.has_usable_password())

    def test_a_verified_account_keeps_its_password(self):
        EmailAddress.objects.create(user=self.squatter, email=self.EMAIL, verified=True, primary=True)
        user = MobileGoogleAuthView._get_or_create_user(self.EMAIL, "google-sub-2")
        self.assertEqual(user, self.squatter)
        self.squatter.refresh_from_db()
        self.assertTrue(self.squatter.check_password("secret-pw"))

    def test_an_inactive_account_gets_nothing(self):
        User.objects.filter(pk=self.squatter.pk).update(is_active=False)
        self.assertIsNone(MobileGoogleAuthView._get_or_create_user(self.EMAIL, "google-sub-3"))
        self.squatter.refresh_from_db()
        self.assertTrue(self.squatter.check_password("secret-pw"))


class TapToPayClubOfficerTests(StandardTestCase):
    """A club officer gets the club's Square token, never the auction creator's personal one."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Tap Club")
        Auction.objects.filter(pk=self.online_auction.pk).update(club=self.club)
        SquareSeller.objects.create(
            user=self.user, square_merchant_id="CREATOR_MID", access_token="creator-tok", payer_email="c@example.com"
        )
        self.treasurer = User.objects.create_user(username="treasurer", password="pw", email="tr@example.com")
        ClubMember.objects.create(club=self.club, user=self.treasurer, name="Treasurer", permission_money=True)

    def _invoice(self):
        return Invoice.objects.select_related("auction__club").get(pk=self.invoice.pk)

    def test_refused_when_the_creators_personal_seller_is_used(self):
        self.assertFalse(PaymentService._check_admin_access(self._invoice(), self.treasurer))

    def test_allowed_when_the_clubs_own_seller_is_used(self):
        club_owner = User.objects.create_user(username="tap_club_owner", password="pw", email="to@example.com")
        SquareSeller.objects.create(
            user=club_owner, club=self.club, square_merchant_id="CLUB_MID", access_token="club-tok"
        )
        self.assertTrue(PaymentService._check_admin_access(self._invoice(), self.treasurer))


class PasswordChangeSignsOutTheAppTests(TestCase):
    def setUp(self):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        self.user = User.objects.create_user(username="jwt_user", password="pw", email="jwt@example.com")
        RefreshToken.for_user(self.user)
        RefreshToken.for_user(self.user)
        self.assertEqual(OutstandingToken.objects.filter(user=self.user).count(), 2)

    def _blacklisted(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

        return BlacklistedToken.objects.filter(token__user=self.user).count()

    def test_password_changed_blacklists_refresh_tokens(self):
        from allauth.account.signals import password_changed

        password_changed.send(sender=User, request=None, user=self.user)
        self.assertEqual(self._blacklisted(), 2)

    def test_password_reset_blacklists_refresh_tokens(self):
        from allauth.account.signals import password_reset

        password_reset.send(sender=User, request=None, user=self.user)
        self.assertEqual(self._blacklisted(), 2)


class CheckinNanCoordinatesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="nan_user", password="pw", email="nan@example.com")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(self.user).access_token}"}

    def test_ping_with_nan_is_a_400(self):
        response = self.client.post(
            reverse("mobile-checkin-ping"),
            {"latitude": "nan", "longitude": "0"},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_set_location_with_nan_is_a_400(self):
        response = self.client.post(
            reverse("mobile-checkin-set-location"),
            {"auction": "anything", "latitude": "nan", "longitude": "0"},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)


@override_settings(**FAKE_WALLET_SETTINGS)
class PassKitMalformedBodyTests(TestCase):
    def setUp(self):
        from auctions.apple_wallet import ensure_apple_pass_auth_token

        club = Club.objects.create(name="PassKit Sweep Club")
        self.member = ClubMember.objects.create(club=club, name="Pass Holder")
        self.token = ensure_apple_pass_auth_token(self.member)

    def _register_url(self, serial=None):
        return reverse(
            "passkit_registration",
            kwargs={
                "device_library_id": "device-1",
                "pass_type_id": FAKE_WALLET_SETTINGS["APPLE_WALLET_PASS_TYPE_IDENTIFIER"],
                "serial_number": serial or f"member-{self.member.pk}",
            },
        )

    def _register(self, body, serial=None):
        return self.client.post(
            self._register_url(serial),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"ApplePass {self.token}",
        )

    def test_log_with_a_list_body_is_200(self):
        response = self.client.post(reverse("passkit_log"), data='["oops"]', content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_log_with_a_non_list_logs_is_200(self):
        response = self.client.post(reverse("passkit_log"), data='{"logs": {}}', content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_register_with_a_non_dict_body_is_400(self):
        self.assertEqual(self._register('["pushToken"]').status_code, 400)
        self.assertFalse(AppleDeviceRegistration.objects.exists())

    def test_register_with_a_non_string_push_token_is_400(self):
        self.assertEqual(self._register('{"pushToken": 5}').status_code, 400)
        self.assertEqual(self._register('{"pushToken": {"a": 1}}').status_code, 400)
        self.assertFalse(AppleDeviceRegistration.objects.exists())

    def test_a_superscript_digit_serial_is_a_404(self):
        self.assertEqual(self._register('{"pushToken": "t"}', serial="member-²").status_code, 404)


class DeviceHandoverTests(TestCase):
    def setUp(self):
        self.first = User.objects.create_user(username="first_owner", password="pw", email="f@example.com")
        self.second = User.objects.create_user(username="second_owner", password="pw", email="s@example.com")
        self.device_uuid = uuid.uuid4()
        DeviceService.register_or_update(self.first, self.device_uuid, fcm_token="first-owners-token")

    def test_a_device_changing_hands_without_a_token_drops_the_old_one(self):
        device, created = DeviceService.register_or_update(self.second, self.device_uuid)
        self.assertFalse(created)
        self.assertEqual(device.user, self.second)
        self.assertEqual(MobileDevice.objects.get(device_uuid=self.device_uuid).fcm_token, "")

    def test_the_same_owner_without_a_token_keeps_it(self):
        DeviceService.register_or_update(self.first, self.device_uuid)
        self.assertEqual(MobileDevice.objects.get(device_uuid=self.device_uuid).fcm_token, "first-owners-token")


@override_settings(SINGLE_CLUB_MODE=False)
class RemoveWinningBidReopensLotTests(StandardTestCase):
    """A lot ended by buy-now while the auction runs: taking the winner's bid away reopens it."""

    def setUp(self):
        super().setUp()
        self.running = Auction.objects.create(
            created_by=self.user,
            title="Still running",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        location = PickupLocation.objects.create(
            name="running location", auction=self.running, pickup_time=timezone.now() + datetime.timedelta(days=4)
        )
        self.bidder_tos = AuctionTOS.objects.create(
            user=self.userB, auction=self.running, pickup_location=location, bidder_number="611"
        )
        self.bought = Lot.objects.create(
            lot_name="Bought now",
            auction=self.running,
            user=self.user,
            quantity=1,
            reserve_price=2,
            buy_now_price=30,
            winner=self.userB,
            auctiontos_winner=self.bidder_tos,
            winning_price=30,
            buy_now_used=True,
            active=False,
        )
        Bid.objects.create(user=self.userB, lot_number=self.bought, amount=20)
        self.top_bid = Bid.objects.create(user=self.userB, lot_number=self.bought, amount=30)

    def _assert_reopened(self):
        self.bought.refresh_from_db()
        self.assertIsNone(self.bought.winner)
        self.assertIsNone(self.bought.auctiontos_winner)
        self.assertIsNone(self.bought.winning_price)
        self.assertTrue(self.bought.active)
        self.assertFalse(Bid.objects.filter(lot_number=self.bought, user=self.userB, is_deleted=False).exists())

    def test_the_bid_delete_page_reopens_the_lot(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("delete_bid", kwargs={"pk": self.top_bid.pk}))
        self.assertEqual(response.status_code, 302)
        self._assert_reopened()

    def test_the_palette_reopens_the_lot_too(self):
        request = self.client.request().wsgi_request
        request.user = self.user
        request.palette_page = {"lot_id": self.bought.pk}
        result = palette_actions.run_action(request, "remove_bid", {"person": "611", "auction": self.running.slug})
        self.assertTrue(result.get("ok"), result)
        self._assert_reopened()


@override_settings(SINGLE_CLUB_MODE=False)
class EditLotAtTheLotLimitTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.in_person_auction.max_lots_per_user = 1
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=7)
        self.in_person_auction.save()
        self.my_lot = Lot.objects.create(
            lot_name="Limit Shrimp",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            user=self.user,
            quantity=1,
            reserve_price=5,
        )

    def test_the_palette_can_edit_a_lot_at_the_limit(self):
        request = self.client.request().wsgi_request
        request.user = self.user
        request.palette_page = {}
        result = palette_actions.run_action(request, "edit_lot", {"lot": "Limit Shrimp", "reserve_price": 12})
        self.assertTrue(result.get("ok"), result)
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.reserve_price, 12)

    def test_the_bulk_add_formset_can_edit_a_lot_at_the_limit(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug}),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-lot_number": self.my_lot.pk,
                "form-0-lot_name": "Limit Shrimp",
                "form-0-quantity": "1",
                "form-0-reserve_price": "7",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.reserve_price, 7)


@override_settings(SINGLE_CLUB_MODE=False)
class PaletteScopingAndWordingTests(StandardTestCase):
    def _request(self, user, page=None):
        request = self.client.request().wsgi_request
        request.user = user
        request.palette_page = page or {}
        return request

    def test_a_lot_id_in_an_auction_the_user_has_not_joined_finds_nothing(self):
        lot, problem = palette_actions._resolve_lot(self._request(self.user_who_does_not_join), {"lot_id": self.lot.pk})
        self.assertIsNone(lot)
        self.assertIn("more_info_needed", problem)

    def test_the_lot_on_screen_is_still_found(self):
        request = self._request(self.user_who_does_not_join, page={"lot_id": self.lot.pk})
        lot, problem = palette_actions._resolve_lot(request, {"lot_id": self.lot.pk})
        self.assertEqual(lot, self.lot)
        self.assertIsNone(problem)

    def test_refund_with_a_word_for_a_percent_asks_instead_of_refunding_it_all(self):
        sold = Lot.objects.create(
            lot_name="Half refund lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            auctiontos_winner=self.in_person_buyer,
            quantity=1,
            winning_price=8,
            custom_lot_number="504-8",
            active=False,
        )
        result = palette_actions.run_action(
            self._request(self.admin_user),
            "refund_lot",
            {"auction": self.in_person_auction.slug, "lot": "Half refund lot", "percent": "half"},
        )
        self.assertIn("more_info_needed", result)
        sold.refresh_from_db()
        self.assertEqual(sold.partial_refund_percent, 0)

    def test_set_current_auction_refuses_settings_only_access(self):
        club = Club.objects.create(name="Current Auction Club")
        Auction.objects.filter(pk=self.online_auction.pk).update(club=club)
        editor = User.objects.create_user(username="settings_editor", password="pw", email="se@example.com")
        ClubMember.objects.create(club=club, user=editor, name="Editor", permission_edit_club=True)
        result = palette_actions.run_action(
            self._request(editor), "set_current_auction", {"club": club.slug, "auction": self.online_auction.slug}
        )
        self.assertIn("error", result)
        club.refresh_from_db()
        self.assertIsNone(club.current_auction)

    def test_money_is_the_sentence_formatter(self):
        self.assertEqual(palette_actions._money(Decimal("1234.5")), "1,234.50")
