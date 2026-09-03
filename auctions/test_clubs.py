"""Clubs: the model, the pages, and who is allowed to do what inside one.

``ClubPermissionTests`` is the big one and is worth reading before changing anything that calls
``check_club_permission``.
"""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    BapAward,
    Club,
    ClubHistory,
    ClubMember,
    ClubMoney,
    Invoice,
    Lot,
    PickupLocation,
)
from auctions.tests import CsvImportTestMixin


class ClubModelTests(TestCase):
    """Tests for Club, ClubMember, ClubHistory models"""

    def setUp(self):
        self.owner = User.objects.create_user(username="club_owner", password="testpass", email="owner@example.com")
        self.member_user = User.objects.create_user(
            username="club_member", password="testpass", email="member@example.com"
        )
        self.club = Club.objects.create(
            name="Test Fish Club",
            allow_joining=True,
        )

    def test_club_slug_auto_generated(self):
        """Club slug should be auto-generated from name"""
        self.assertIsNotNone(self.club.slug)
        self.assertIn("test-fish-club", self.club.slug)

    def test_club_str(self):
        self.assertEqual(str(self.club), "Test Fish Club")

    def test_club_member_phone_as_string(self):
        """phone_as_string should format a 10-digit number with dashes"""
        member = ClubMember.objects.create(
            club=self.club,
            name="Alice Smith",
            phone_number="5551234567",
        )
        self.assertEqual(member.phone_as_string, "555-123-4567")

    def test_club_member_phone_as_string_non_10_digit(self):
        """phone_as_string returns raw digits for non-10-digit numbers"""
        member = ClubMember.objects.create(
            club=self.club,
            name="Bob Jones",
            phone_number="123456",
        )
        self.assertEqual(member.phone_as_string, "123456")

    def test_club_member_str_with_name(self):
        member = ClubMember.objects.create(club=self.club, name="Alice Smith")
        self.assertEqual(str(member), "Alice Smith")

    def test_club_member_str_with_email_no_name(self):
        member = ClubMember.objects.create(club=self.club, email="alice@example.com")
        self.assertEqual(str(member), "alice@example.com")

    def test_club_member_str_fallback(self):
        member = ClubMember.objects.create(club=self.club)
        self.assertIn("Member #", str(member))

    def test_club_member_defaults(self):
        member = ClubMember.objects.create(club=self.club, name="Test User")
        self.assertFalse(member.is_deleted)
        self.assertEqual(member.source, "manually_added")
        self.assertEqual(member.contact_status, "contact")
        self.assertEqual(member.bap_points, 0)
        self.assertEqual(member.hap_points, 0)

    def test_club_history_str_with_user(self):
        history = ClubHistory.objects.create(
            club=self.club,
            user=self.owner,
            action="Added a member",
            applies_to="MEMBERS",
        )
        result = str(history)
        self.assertIn("Added a member", result)

    def test_club_history_str_system(self):
        history = ClubHistory.objects.create(
            club=self.club,
            action="System sync",
            applies_to="MEMBERS",
        )
        self.assertIn("System", str(history))

    def test_club_member_permission_defaults(self):
        """All permission fields should default to False"""
        member = ClubMember.objects.create(club=self.club, name="Test")
        for field in [
            "permission_admin",
            "permission_view",
            "permission_export",
            "permission_add_edit",
            "permission_edit_club",
            "permission_money",
            "permission_manage_auctions",
            "permission_manage_bap",
        ]:
            self.assertFalse(getattr(member, field), f"{field} should default to False")

    def test_has_any_permission_false_by_default(self):
        member = ClubMember.objects.create(club=self.club, name="Test")
        self.assertFalse(member.has_any_permission)

    def test_has_any_permission_true_when_one_set(self):
        member = ClubMember.objects.create(club=self.club, name="Test", permission_view=True)
        self.assertTrue(member.has_any_permission)

    def test_club_member_with_user(self):
        member = ClubMember.objects.create(
            club=self.club,
            user=self.member_user,
            name="Jane Doe",
            source="joined",
        )
        self.assertEqual(member.user, self.member_user)
        self.assertEqual(member.source, "joined")


class ClubViewTests(TestCase):
    """Tests for club views"""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="club_owner2", password="testpass", email="owner2@example.com")
        self.other_user = User.objects.create_user(username="other2", password="testpass", email="other2@example.com")
        self.club = Club.objects.create(
            name="View Test Club",
            allow_joining=True,
        )
        self.owner_member = ClubMember.objects.create(
            club=self.club,
            user=self.owner,
            name="Owner User",
            permission_admin=True,
        )

    def test_club_detail_anonymous_can_view_when_enabled(self):
        """Anonymous user can view a club detail page."""
        self.club.save()
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_club_admin_requires_login(self):
        """Anonymous user should be redirected or blocked"""
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 301, 403])

    def test_club_detail_logged_in(self):
        """Logged-in user can view club detail page"""
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_viewing_club_page_records_last_club_used_for_member(self):
        """A member viewing a club page has it recorded as their last club used (for the palette)."""
        self.owner.userdata.refresh_from_db()
        self.assertIsNone(self.owner.userdata.last_club_used)
        self.client.login(username="club_owner2", password="testpass")
        self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.owner.userdata.refresh_from_db()
        self.assertEqual(self.owner.userdata.last_club_used, self.club)

    def test_viewing_club_page_does_not_record_for_non_member(self):
        """A non-member viewing a public club page does not get it recorded as their last club used."""
        self.club.save()
        self.client.login(username="other2", password="testpass")
        self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.other_user.userdata.refresh_from_db()
        self.assertIsNone(self.other_user.userdata.last_club_used)

    def test_club_detail_tab_route_shows_requested_tab_chart_and_recent_auctions(self):
        self.club.enable_breeder_award_program = True
        self.club.homepage = "https://example.com"
        self.club.facebook_page = "https://facebook.com/view-test-club"
        self.club.discord_invite_link = "https://discord.gg/viewclub"
        self.club.location = "123 Club St"
        self.club.latitude = 39.5
        self.club.longitude = -96.5
        self.club.save()
        BapAward.objects.create(club_member=self.owner_member, date=timezone.now().date(), points=4)
        start = timezone.now() - datetime.timedelta(days=20)
        end = timezone.now() - datetime.timedelta(days=10)
        for i in range(11):
            Auction.objects.create(
                created_by=self.owner,
                club=self.club,
                title=f"Club Auction {i}",
                date_start=start + datetime.timedelta(days=i),
                date_end=end + datetime.timedelta(days=i),
                winning_bid_percent_to_club=25,
                lot_entry_fee=0,
                unsold_lot_fee=0,
                tax=0,
                # The club page's "recent auctions" list is the promoted ones; the model default is
                # False, so an auction that is meant to appear there has to say so.
                promote_this_auction=True,
            )
        self.client.login(username="club_owner2", password="testpass")
        response = self.client.get(reverse("club_detail_tab", kwargs={"slug": self.club.slug, "tab": "my-points"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="my-points-tab-btn"')
        self.assertContains(response, 'id="my-points-chart"')
        self.assertContains(response, "Membership")
        self.assertContains(response, "Discord")
        self.assertContains(response, "Map")
        self.assertNotContains(response, "View membership details")
        self.assertContains(response, "Club Auction 10")
        self.assertNotContains(response, "Club Auction 0")

    def test_club_tabs_collapse_into_a_more_menu_when_there_are_too_many(self):
        """Events/BAP/HAP/Culture/My Points runs off the side of a phone; three tabs don't."""
        self.club.enable_breeder_award_program = True
        self.club.save()
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_detail", kwargs={"slug": self.club.slug})

        # Events, BAP, My Points: three fit, and a More menu holding one item is worse than a tab.
        response = self.client.get(url)
        self.assertFalse(response.context["club_tabs_overflow"])
        self.assertRegex(response.content.decode(), r'class="nav-link[^"]*" id="my-points-tab-btn"')

        # Turning on the other two award tracks makes five, so everything past BAP moves into More.
        self.club.separate_hap = True
        self.club.separate_cap = True
        self.club.save()
        response = self.client.get(url)
        self.assertTrue(response.context["club_tabs_overflow"])
        html = response.content.decode()
        for tab in ("hap", "culture", "my-points"):
            self.assertRegex(html, rf'class="dropdown-item[^"]*" id="{tab}-tab-btn"')
        # Events and BAP stay where they were.
        self.assertRegex(html, r'class="nav-link[^"]*" id="bap-tab-btn"')

    def test_club_detail_shows_join_button_for_non_member(self):
        self.club.homepage = "https://example.com"
        self.club.facebook_page = "https://facebook.com/view-test-club"
        self.club.discord_invite_link = "https://discord.gg/viewclub"
        self.club.save()
        self.client.login(username="other2", password="testpass")
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Join")
        self.assertContains(
            response,
            '<button type="submit" class="btn btn-sm btn-success">',
            html=False,
        )
        self.assertNotContains(response, 'data-club-panel-toggle="join-panel"')
        self.assertContains(response, "Website")
        self.assertContains(response, "Facebook")
        self.assertContains(response, "Discord")

    def test_club_admin_owner_can_access(self):
        """Club owner with admin role can access admin page"""
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_club_admin_non_member_blocked(self):
        """Non-admin user cannot access club admin page"""
        self.client.login(username="other2", password="testpass")
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        # Should return 403 or redirect
        self.assertIn(response.status_code, [403, 302])

    def test_club_admin_membership_filters_hidden_without_fee(self):
        """Paid/Unpaid membership chips are hidden when the club charges no dues, but the
        source/other chips (modeled on the auction users page) are still offered."""
        self.club.membership_annual_fee = None
        self.club.save()
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        keys = [key for _, key in response.context["possible_filters"]]
        self.assertNotIn("current", keys)
        self.assertNotIn("expired", keys)
        self.assertIn("joined", keys)
        self.assertIn("deactivated", keys)

    def test_club_admin_membership_filters_shown_with_fee(self):
        """Paid club member / Unpaid chips appear once the club has a membership fee."""
        self.club.membership_annual_fee = 20
        self.club.save()
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        filters = response.context["possible_filters"]
        self.assertIn(("<i class='bi bi-person-badge'></i> Paid club member", "current"), filters)
        self.assertIn(("<i class='bi bi-person'></i> Unpaid", "expired"), filters)
        # The shared HTMX template renders the chips as toggleable checkboxes.
        content = response.content.decode(response.charset or "utf-8")
        self.assertIn('data-filter-key="current"', content)
        self.assertIn('data-filter-key="expired"', content)
        self.assertIn("Paid club member", content)

    def test_club_edit_owner_can_access(self):
        """Club admin member can access edit page (permission_admin grants permission_edit_club)"""
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_edit", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_club_membership_settings_no_longer_shows_contact_email(self):
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_membership_settings", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # contact_email moved to the email settings page; membership settings only
        # carries pure membership / payment configuration.
        self.assertNotContains(response, "id_contact_email")
        self.assertNotContains(response, "id_send_membership_expiration_reminders")

    def test_club_history_owner_can_access(self):
        """Club owner with admin permission can view history"""
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_history", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_club_stats_owner_can_access(self):
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_stats", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("club_stats", kwargs={"slug": self.club.slug}))

    def test_club_stats_chart_data_uses_checkins_and_membership_growth(self):
        self.client.login(username="club_owner2", password="testpass")
        now = timezone.now()
        normal_auction = Auction.objects.create(
            title="Normal club auction",
            date_start=now - datetime.timedelta(days=60),
            date_end=now - datetime.timedelta(days=59),
            created_by=self.owner,
            club=self.club,
        )
        normal_location = PickupLocation.objects.create(
            name="Normal pickup",
            auction=normal_auction,
            pickup_time=now + datetime.timedelta(days=1),
        )
        normal_tos = AuctionTOS.objects.create(
            auction=normal_auction, pickup_location=normal_location, name="Normal bidder"
        )
        Lot.objects.create(
            lot_name="Normal lot",
            auction=normal_auction,
            auctiontos_seller=normal_tos,
            quantity=1,
            winning_price=10,
        )
        Invoice.objects.create(auction=normal_auction, auctiontos_user=normal_tos)

        checkin_auction = Auction.objects.create(
            title="Check-in club auction",
            date_start=now - datetime.timedelta(days=30),
            date_end=now - datetime.timedelta(days=29),
            created_by=self.owner,
            club=self.club,
            manage_users_through_club="checkin",
        )
        checkin_location = PickupLocation.objects.create(
            name="Check-in pickup",
            auction=checkin_auction,
            pickup_time=now + datetime.timedelta(days=2),
        )
        checked_in_tos = AuctionTOS.objects.create(
            auction=checkin_auction,
            pickup_location=checkin_location,
            name="Checked in bidder",
            checked_in=now,
        )
        AuctionTOS.objects.create(
            auction=checkin_auction,
            pickup_location=checkin_location,
            name="Not checked in bidder",
        )
        Lot.objects.create(
            lot_name="Check-in lot 1",
            auction=checkin_auction,
            auctiontos_seller=checked_in_tos,
            quantity=1,
            winning_price=5,
        )
        Lot.objects.create(
            lot_name="Check-in lot 2",
            auction=checkin_auction,
            auctiontos_seller=checked_in_tos,
            quantity=1,
            winning_price=15,
        )
        # Club stats charts read from cached auction stats, so populate caches for these test auctions.
        normal_auction.recalculate_stats()
        checkin_auction.recalculate_stats()

        old_paid_member = ClubMember.objects.create(
            club=self.club,
            name="Old paid member",
            membership_expiration_date=timezone.now().date() + datetime.timedelta(days=30),
        )
        ClubMember.objects.filter(pk=old_paid_member.pk).update(createdon=now - datetime.timedelta(days=365 * 11))
        new_paid_member = ClubMember.objects.create(
            club=self.club,
            name="New paid member",
            membership_expiration_date=timezone.now().date() + datetime.timedelta(days=30),
        )
        ClubMember.objects.filter(pk=new_paid_member.pk).update(createdon=now - datetime.timedelta(days=20))
        new_unpaid_member = ClubMember.objects.create(
            club=self.club,
            name="New unpaid member",
            membership_expiration_date=timezone.now().date() - datetime.timedelta(days=1),
        )
        ClubMember.objects.filter(pk=new_unpaid_member.pk).update(createdon=now - datetime.timedelta(days=10))

        response = self.client.get(reverse("club_stats", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

        auction_chart = response.context["club_auction_stats"]
        auction_datasets = {dataset["label"]: dataset["data"] for dataset in auction_chart["datasets"]}
        self.assertEqual(auction_datasets["Gross"], [10.0, 20.0])
        self.assertEqual(auction_datasets["Lots"], [1, 2])
        self.assertEqual(auction_datasets["Checked in"], [1, 1])

        membership_chart = response.context["club_membership_growth"]
        membership_datasets = {dataset["label"]: dataset["data"] for dataset in membership_chart["datasets"]}
        self.assertEqual(membership_datasets["Members"][0], 1)
        self.assertEqual(membership_datasets["Paid members"][0], 1)
        self.assertEqual(membership_datasets["Members"][-1], 4)
        self.assertEqual(membership_datasets["Paid members"][-1], 2)

    def test_club_404_for_invalid_slug(self):
        """Non-existent club slug returns 404"""
        self.client.login(username="club_owner2", password="testpass")
        url = reverse("club_detail", kwargs={"slug": "nonexistent-club-xyz"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_club_admin_anonymous_redirects_to_login(self):
        """Anonymous user accessing club_admin should be redirected to login, not get 403"""
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_club_edit_anonymous_redirects_to_login(self):
        """Anonymous user accessing club_edit should be redirected to login"""
        url = reverse("club_edit", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_club_history_anonymous_redirects_to_login(self):
        """Anonymous user accessing club_history should be redirected to login"""
        url = reverse("club_history", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_club_stats_anonymous_redirects_to_login(self):
        url = reverse("club_stats", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_club_admin_non_member_gets_403(self):
        """Authenticated non-member user gets 403 on club admin"""
        self.client.login(username="other2", password="testpass")
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_club_edit_non_member_gets_403(self):
        """Authenticated non-member user gets 403 on club edit"""
        self.client.login(username="other2", password="testpass")
        url = reverse("club_edit", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_club_history_non_member_gets_403(self):
        """Authenticated non-member user gets 403 on club history"""
        self.client.login(username="other2", password="testpass")
        url = reverse("club_history", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_club_detail_non_member_can_view(self):
        """Non-member authenticated user can view a club detail page."""
        self.club.save()
        self.client.login(username="other2", password="testpass")
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_club_admin_regular_member_gets_403(self):
        """View-only member cannot access club edit"""
        regular_user = User.objects.create_user(username="regular_member", password="testpass", email="reg@example.com")
        ClubMember.objects.create(club=self.club, user=regular_user, name="Regular", permission_view=True)
        self.client.login(username="regular_member", password="testpass")
        url = reverse("club_edit", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_club_superuser_can_access_all(self):
        """Superuser can access all club views"""
        User.objects.create_superuser(username="su_test", password="testpass", email="su@example.com")
        self.client.login(username="su_test", password="testpass")
        for url_name in ["club_admin", "club_edit", "club_history", "club_stats"]:
            url = reverse(url_name, kwargs={"slug": self.club.slug})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"{url_name} should return 200 for superuser")


class ClubPermissionTests(CsvImportTestMixin, TestCase):
    """Verify that each club permission level grants exactly the right access.

    Three user categories are tested for each view:
    - non_member: authenticated but has no ClubMember record
    - Various specific-permission members (view_user, add_edit_user, etc.)
    - admin_user: ClubMember with permission_admin=True (wildcard)
    """

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(
            name="Permission Test Club",
            allow_joining=True,
            enable_breeder_award_program=True,
        )
        self.non_member = User.objects.create_user(
            username="perm_non_member", password="testpass", email="perm_non@example.com"
        )
        self.view_user = User.objects.create_user(
            username="perm_view", password="testpass", email="perm_view@example.com"
        )
        self.add_edit_user = User.objects.create_user(
            username="perm_add_edit", password="testpass", email="perm_add_edit@example.com"
        )
        self.export_user = User.objects.create_user(
            username="perm_export", password="testpass", email="perm_export@example.com"
        )
        self.edit_club_user = User.objects.create_user(
            username="perm_edit_club", password="testpass", email="perm_edit_club@example.com"
        )
        self.money_user = User.objects.create_user(
            username="perm_money", password="testpass", email="perm_money@example.com"
        )
        self.bap_user = User.objects.create_user(username="perm_bap", password="testpass", email="perm_bap@example.com")
        self.admin_user = User.objects.create_user(
            username="perm_admin", password="testpass", email="perm_admin@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.view_user, name="View", permission_view=True)
        ClubMember.objects.create(club=self.club, user=self.add_edit_user, name="AddEdit", permission_add_edit=True)
        ClubMember.objects.create(club=self.club, user=self.export_user, name="Export", permission_export=True)
        ClubMember.objects.create(club=self.club, user=self.edit_club_user, name="EditClub", permission_edit_club=True)
        ClubMember.objects.create(club=self.club, user=self.money_user, name="Money", permission_money=True)
        ClubMember.objects.create(club=self.club, user=self.bap_user, name="Bap", permission_manage_bap=True)
        ClubMember.objects.create(club=self.club, user=self.admin_user, name="Admin", permission_admin=True)
        self.target_member = ClubMember.objects.create(club=self.club, name="Target Member", email="target@example.com")

    def _login(self, user):
        self.client.login(username=user.username, password="testpass")

    def test_bap_csv_import_creates_awards_after_confirm(self):
        """The BAP importer also routes through the preview: an award is only created after confirm,
        matched to the member by email."""
        self._login(self.bap_user)
        url = reverse("club_bap_import", kwargs={"slug": self.club.slug})
        csv_file = SimpleUploadedFile(
            "bap.csv", b"email,bap,hap,cap\ntarget@example.com,3,0,0\n", content_type="text/csv"
        )
        before = BapAward.objects.filter(club_member=self.target_member).count()
        # Upload alone must not create the award.
        self.client.post(url, {"csv_file": csv_file})
        self.assertEqual(BapAward.objects.filter(club_member=self.target_member).count(), before)
        # Confirm via the helper (re-uploads + confirms) and check the award lands.
        csv_file = SimpleUploadedFile(
            "bap.csv", b"email,bap,hap,cap\ntarget@example.com,3,0,0\n", content_type="text/csv"
        )
        self.run_csv_import(url, csv_file)
        self.assertEqual(BapAward.objects.filter(club_member=self.target_member).count(), before + 1)
        self.assertEqual(BapAward.objects.filter(club_member=self.target_member).latest("pk").points, 3)

    # --- Anonymous access ---

    def test_anonymous_redirected_from_club_admin(self):
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_anonymous_redirected_from_club_edit(self):
        url = reverse("club_edit", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_anonymous_redirected_from_club_history(self):
        url = reverse("club_history", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_anonymous_redirected_from_club_stats(self):
        url = reverse("club_stats", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_anonymous_redirected_from_renew_page(self):
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_anonymous_redirected_from_bap_settings(self):
        url = reverse("club_bap_settings", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_anonymous_redirected_from_bap_lots(self):
        url = reverse("club_bap", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    # --- Non-member access ---

    def test_non_member_blocked_from_club_admin(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_club_edit(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_club_history(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_history", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_club_stats(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_stats", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_renew_page(self):
        self._login(self.non_member)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_bap_settings(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_bap_lots(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_bap", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_member_permissions_view(self):
        self._login(self.non_member)
        response = self.client.get(reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_csv_export(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_member_export", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_membership_settings(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_non_member_blocked_from_treasurer_report(self):
        self._login(self.non_member)
        response = self.client.get(reverse("club_treasurer_report", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    # --- permission_view ---

    def test_view_only_can_access_admin_panel(self):
        """A view-only member can see the member list"""
        self._login(self.view_user)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_club_admin_search_hides_deactivated_by_default(self):
        from auctions.filters import ClubMemberFilter

        ClubMember.objects.create(club=self.club, name="Retired Search Member", is_deleted=True)
        qs = ClubMember.objects.filter(club=self.club)
        filtered = ClubMemberFilter({"query": "Retired Search"}, queryset=qs).qs
        self.assertEqual(filtered.count(), 0)

    def test_club_admin_search_includes_deactivated_when_token_present(self):
        from auctions.filters import ClubMemberFilter

        deactivated = ClubMember.objects.create(club=self.club, name="Retired Search Member", is_deleted=True)
        qs = ClubMember.objects.filter(club=self.club)
        filtered = ClubMemberFilter({"query": "Retired Search deactivated"}, queryset=qs).qs
        self.assertEqual(list(filtered.values_list("pk", flat=True)), [deactivated.pk])

    def test_club_admin_search_prefers_active_results(self):
        from auctions.filters import ClubMemberFilter

        active = ClubMember.objects.create(club=self.club, name="Shared Search Name", is_deleted=False)
        ClubMember.objects.create(club=self.club, name="Shared Search Name", is_deleted=True)
        qs = ClubMember.objects.filter(club=self.club)
        filtered = ClubMemberFilter({"query": "Shared Search"}, queryset=qs).qs
        self.assertEqual(list(filtered.values_list("pk", flat=True)), [active.pk])

    def test_view_only_can_access_history(self):
        self._login(self.view_user)
        response = self.client.get(reverse("club_history", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_view_only_can_access_stats(self):
        self._login(self.view_user)
        response = self.client.get(reverse("club_stats", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_view_only_blocked_from_club_edit(self):
        self._login(self.view_user)
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_view_only_blocked_from_renew_page(self):
        self._login(self.view_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_view_only_blocked_from_bap_settings(self):
        self._login(self.view_user)
        response = self.client.get(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_view_only_blocked_from_csv_export(self):
        self._login(self.view_user)
        response = self.client.get(reverse("club_member_export", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_view_only_blocked_from_member_permissions_view(self):
        self._login(self.view_user)
        response = self.client.get(reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk}))
        self.assertEqual(response.status_code, 403)

    # --- permission_add_edit implicitly grants permission_view ---

    def test_add_edit_implicitly_can_access_admin_panel(self):
        """A member with add_edit but not view should still see the admin panel"""
        self._login(self.add_edit_user)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_add_edit_can_access_renew_page(self):
        self._login(self.add_edit_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_add_edit_blocked_from_club_edit(self):
        self._login(self.add_edit_user)
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_add_edit_blocked_from_bap_settings(self):
        self._login(self.add_edit_user)
        response = self.client.get(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_add_edit_blocked_from_csv_export(self):
        self._login(self.add_edit_user)
        response = self.client.get(reverse("club_member_export", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_add_edit_blocked_from_member_permissions_view(self):
        self._login(self.add_edit_user)
        response = self.client.get(reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk}))
        self.assertEqual(response.status_code, 403)

    # --- permission_export ---

    def test_export_can_export_csv(self):
        self._login(self.export_user)
        response = self.client.get(reverse("club_member_export", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_export_implicitly_can_access_admin_panel(self):
        self._login(self.export_user)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_export_blocked_from_club_edit(self):
        self._login(self.export_user)
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    # --- permission_edit_club ---

    def test_edit_club_can_access_club_edit(self):
        self._login(self.edit_club_user)
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_edit_club_can_access_membership_settings(self):
        self._login(self.edit_club_user)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_edit_club_implicitly_can_access_admin_panel(self):
        self._login(self.edit_club_user)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_edit_club_blocked_from_bap_settings(self):
        self._login(self.edit_club_user)
        response = self.client.get(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_edit_club_blocked_from_renew_page(self):
        self._login(self.edit_club_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_edit_club_blocked_from_member_permissions_view(self):
        """permission_edit_club does not grant permission_admin (needed for permissions view)"""
        self._login(self.edit_club_user)
        response = self.client.get(reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk}))
        self.assertEqual(response.status_code, 403)

    # --- permission_money ---

    def test_money_user_can_access_membership_settings(self):
        self._login(self.money_user)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    @override_settings(PAYPAL_CLIENT_ID="test-client", PAYPAL_SECRET="test-secret")
    def test_paypal_connect_button_hidden_when_user_not_enabled(self):
        self.money_user.userdata.paypal_enabled = False
        self.money_user.userdata.save(update_fields=["paypal_enabled"])
        self._login(self.money_user)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Connect a PayPal account for this club")

    @override_settings(PAYPAL_CLIENT_ID="test-client", PAYPAL_SECRET="test-secret")
    def test_paypal_connect_button_shown_when_user_enabled(self):
        self.money_user.userdata.paypal_enabled = True
        self.money_user.userdata.save(update_fields=["paypal_enabled"])
        self._login(self.money_user)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect a PayPal account for this club")

    @override_settings(PAYPAL_CLIENT_ID="test-client", PAYPAL_SECRET="test-secret")
    def test_paypal_connect_view_blocks_user_not_enabled(self):
        self.money_user.userdata.paypal_enabled = False
        self.money_user.userdata.save(update_fields=["paypal_enabled"])
        self._login(self.money_user)
        response = self.client.get(reverse("paypal_connect") + f"?club={self.club.slug}")
        self.assertEqual(response.status_code, 302)

    @override_settings(SQUARE_APPLICATION_ID="test-app", SQUARE_CLIENT_SECRET="test-secret")
    def test_square_connect_button_hidden_when_user_not_enabled(self):
        self.money_user.userdata.square_enabled = False
        self.money_user.userdata.save(update_fields=["square_enabled"])
        self._login(self.money_user)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Connect a Square account for this club")

    @override_settings(SQUARE_APPLICATION_ID="test-app", SQUARE_CLIENT_SECRET="test-secret")
    def test_square_connect_button_shown_when_user_enabled(self):
        self.money_user.userdata.square_enabled = True
        self.money_user.userdata.save(update_fields=["square_enabled"])
        self._login(self.money_user)
        response = self.client.get(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect a Square account for this club")

    def test_square_connect_view_blocks_user_not_enabled(self):
        self.money_user.userdata.square_enabled = False
        self.money_user.userdata.save(update_fields=["square_enabled"])
        self._login(self.money_user)
        response = self.client.get(reverse("square_connect") + f"?club={self.club.slug}")
        self.assertEqual(response.status_code, 302)

    def test_money_user_can_access_treasurer_report(self):
        self._login(self.money_user)
        response = self.client.get(reverse("club_treasurer_report", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_money_user_blocked_from_member_list(self):
        self._login(self.money_user)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    # --- permission_manage_bap ---

    def test_bap_user_can_access_bap_lots(self):
        self._login(self.bap_user)
        response = self.client.get(reverse("club_bap_lots", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_bap_user_can_access_bap_settings(self):
        self._login(self.bap_user)
        response = self.client.get(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_bap_user_implicitly_can_access_admin_panel(self):
        self._login(self.bap_user)
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_bap_user_blocked_from_club_edit(self):
        self._login(self.bap_user)
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_bap_user_blocked_from_renew_page(self):
        self._login(self.bap_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_non_bap_user_blocked_from_bap_lots(self):
        """A member with view-only access cannot see BAP lots"""
        self._login(self.view_user)
        response = self.client.get(reverse("club_bap_lots", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    # --- permission_admin (wildcard) ---

    def test_admin_can_access_all_views(self):
        self._login(self.admin_user)
        for url_name, kwargs in [
            ("club_admin", {"slug": self.club.slug}),
            ("club_edit", {"slug": self.club.slug}),
            ("club_history", {"slug": self.club.slug}),
            ("club_stats", {"slug": self.club.slug}),
            ("club_membership_settings", {"slug": self.club.slug}),
            ("club_bap_settings", {"slug": self.club.slug}),
            ("club_bap", {"slug": self.club.slug}),
            ("club_member_export", {"slug": self.club.slug}),
            ("club_member_renew_page", {"slug": self.club.slug, "pk": self.target_member.pk}),
            ("clubmember_permissions", {"pk": self.target_member.pk}),
        ]:
            url = reverse(url_name, kwargs=kwargs)
            response = self.client.get(url)
            self.assertEqual(
                response.status_code, 200, f"{url_name} should be 200 for admin, got {response.status_code}"
            )

    def test_admin_can_set_member_permissions(self):
        """Only permission_admin members can change other members' permissions"""
        self._login(self.admin_user)
        url = reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_renew_page_updates_membership_date(self):
        """Admin can renew a membership via the dedicated renew page"""
        self._login(self.admin_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.post(url, {"membership_expiration_date": "2026-01-15"})
        self.assertEqual(response.status_code, 302)
        self.target_member.refresh_from_db()
        self.assertEqual(str(self.target_member.membership_expiration_date), "2026-01-15")

    def test_renew_page_records_old_and_new_date_in_history(self):
        """ClubMemberRenewPageView history entry shows both old and new expiration dates."""
        self.club.membership_annual_fee = Decimal("20.00")
        self.club.save(update_fields=["membership_annual_fee"])
        self.target_member.membership_expiration_date = datetime.date(2025, 6, 1)
        self.target_member.save(update_fields=["membership_expiration_date"])
        self._login(self.admin_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        self.client.post(url, {"membership_expiration_date": "2026-06-01"})
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").order_by("-pk").first()
        self.assertIsNotNone(history)
        self.assertIn("6/1/2025", history.action)
        self.assertIn("6/1/2026", history.action)

    def test_renew_page_does_not_create_clubmoney(self):
        """ClubMemberRenewPageView is a record correction — it must not book a ClubMoney entry."""
        self.club.membership_annual_fee = Decimal("20.00")
        self.club.save(update_fields=["membership_annual_fee"])
        self._login(self.admin_user)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        self.client.post(url, {"membership_expiration_date": "2026-06-01"})
        self.assertFalse(ClubMoney.objects.filter(club=self.club).exists())

    def test_renew_page_post_non_member_gets_403(self):
        """Non-member cannot POST to the renew page"""
        self._login(self.non_member)
        url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.target_member.pk})
        response = self.client.post(url, {"membership_expiration_date": "2026-01-15"})
        self.assertEqual(response.status_code, 403)

    def test_cross_club_renew_page_returns_404(self):
        """A member from another club cannot renew a member that doesn't belong to their club"""
        other_club = Club.objects.create(name="Other Club")
        ClubMember.objects.create(club=other_club, user=self.admin_user, name="Admin", permission_admin=True)
        url = reverse("club_member_renew_page", kwargs={"slug": other_club.slug, "pk": self.target_member.pk})
        self._login(self.admin_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class ClubMemberUpdateTests(CsvImportTestMixin, TestCase):
    """Tests for club member self-service update and CSV import/export"""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="cu_owner", password="testpass", email="cu_owner@example.com")
        self.member_user = User.objects.create_user(
            username="cu_member", password="testpass", email="cu_member@example.com"
        )
        self.other_user = User.objects.create_user(
            username="cu_other", password="testpass", email="cu_other@example.com"
        )
        self.club = Club.objects.create(name="Update Test Club", allow_joining=True)
        self.member = ClubMember.objects.create(
            club=self.club, user=self.member_user, name="Jane Doe", email="cu_member@example.com"
        )

    def test_member_can_update_info(self):
        """A club member can update their own contact info"""
        self.client.login(username="cu_member", password="testpass")
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"action": "update", "name": "Janet Doe"})
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.name, "Janet Doe")

    def test_non_member_update_is_ignored(self):
        """A non-member's update action is silently ignored"""
        self.client.login(username="cu_other", password="testpass")
        url = reverse("club_detail", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"action": "update", "name": "Hacker X"})
        self.assertEqual(response.status_code, 302)
        # member record unchanged
        self.member.refresh_from_db()
        self.assertEqual(self.member.name, "Jane Doe")

    def test_csv_import_adds_members(self):
        """CSV import creates new club members"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        csv_content = "email,first name,last name\nnewmember@example.com,New,Member\n"
        csv_file = SimpleUploadedFile("members.csv", csv_content.encode("utf-8"), content_type="text/csv")
        url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        response = self.run_csv_import(url, csv_file)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ClubMember.objects.filter(club=self.club, email="newmember@example.com").exists())
        imported = ClubMember.objects.get(club=self.club, email="newmember@example.com")
        self.assertFalse(imported.send_welcome_email)
        self.assertTrue(imported.welcome_email_sent)

    def test_csv_import_skips_rows_without_name_or_email(self):
        """CSV import skips rows that have neither a name nor an email (no way to identify the person)"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        csv_content = "email,first name\n,\n"
        csv_file = SimpleUploadedFile("members.csv", csv_content.encode("utf-8"), content_type="text/csv")
        url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        initial_count = ClubMember.objects.filter(club=self.club, is_deleted=False).count()
        response = self.run_csv_import(url, csv_file)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClubMember.objects.filter(club=self.club, is_deleted=False).count(), initial_count)

    def test_csv_import_rhyming_name_is_flagged_not_duplicated(self):
        """A no-email import row whose name rhymes with an existing member (Bob -> Robert) is surfaced as a
        possible duplicate and, on the default merge, does not create a second member."""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        ClubMember.objects.create(club=self.club, name="Robert Smith", email="robert@example.com")
        self.client.login(username="cu_owner", password="testpass")
        before = ClubMember.objects.filter(club=self.club, is_deleted=False).count()
        csv_file = SimpleUploadedFile("members.csv", b"first name,last name\nBob,Smith\n", content_type="text/csv")
        url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        self.run_csv_import(url, csv_file)  # default decision = merge
        self.assertEqual(ClubMember.objects.filter(club=self.club, is_deleted=False).count(), before)

    def test_csv_import_duplicate_email_rows_collapse_to_one_member(self):
        """Two import rows sharing a normalized email become a single member instead of two records."""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        before = ClubMember.objects.filter(club=self.club, is_deleted=False).count()
        csv_file = SimpleUploadedFile(
            "members.csv",
            b"name,email,phone\nFinn,Twin@Example.com,\nFinn, twin@example.com ,555-9000\n",
            content_type="text/csv",
        )
        url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        self.run_csv_import(url, csv_file)
        matches = ClubMember.objects.filter(club=self.club, email="twin@example.com", is_deleted=False)
        self.assertEqual(matches.count(), 1)
        self.assertEqual(ClubMember.objects.filter(club=self.club, is_deleted=False).count(), before + 1)
        self.assertEqual(matches.first().phone_number, "555-9000")

    def test_csv_import_ragged_row_does_not_500(self):
        """A member row with more columns than the header is imported rather than crashing the upload."""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        csv_file = SimpleUploadedFile(
            "members.csv", b"name,email\nRag,rag@example.com,oops,more\n", content_type="text/csv"
        )
        url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        response = self.run_csv_import(url, csv_file)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ClubMember.objects.filter(club=self.club, email="rag@example.com").exists())

    def test_csv_import_non_admin_gets_403(self):
        """Non-admin user cannot import CSV"""
        self.client.login(username="cu_other", password="testpass")
        csv_file = SimpleUploadedFile("members.csv", b"email\ntest@example.com\n", content_type="text/csv")
        url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        response = self.run_csv_import(url, csv_file)
        self.assertEqual(response.status_code, 403)

    def test_csv_export_requires_permission(self):
        """Non-admin user cannot export CSV"""
        self.client.login(username="cu_other", password="testpass")
        url = reverse("club_member_export", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_csv_export_all_returns_csv(self):
        """Owner can export all members as CSV"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("club_member_export", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("Name", content)
        self.assertIn("Jane", content)

    def test_csv_export_respects_filter(self):
        """Export with query filter only returns matching members"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_export = True
        owner_member.save()
        ClubMember.objects.create(club=self.club, name="Bob Smith", email="bob@example.com")
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("club_member_export", kwargs={"slug": self.club.slug})
        response = self.client.get(url, {"query": "Jane"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Jane", content)
        self.assertNotIn("Bob", content)

    def test_renew_membership_requires_permission(self):
        """Non-admin user cannot renew membership"""
        self.client.login(username="cu_other", password="testpass")
        url = reverse("club_member_renew", kwargs={"pk": self.member.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_renew_membership_sets_today(self):
        """Admin can renew membership and it sets membership_last_paid to today"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("club_member_renew", kwargs={"pk": self.member.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        from django.utils import timezone

        self.assertEqual(self.member.membership_last_paid, timezone.now().date())

    def test_delete_member_requires_permission(self):
        """Non-admin user cannot delete a club member"""
        self.client.login(username="cu_other", password="testpass")
        url = reverse("club_member_delete", kwargs={"pk": self.member.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_delete_member_soft_deletes(self):
        """Admin can soft-delete a club member"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("club_member_delete", kwargs={"pk": self.member.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("closeModal", response.content.decode("utf-8"))
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_deleted)

    def test_club_member_duplicate_name_validation_returns_warning_message(self):
        """Duplicate-name validation should warn when the member is already in this club"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("clubmember_validation", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"name": self.member.name})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name_tooltip"], f"{self.member} is already in this club")

    def test_club_member_autofill_searches_clubs_with_manage_auctions_permission(self):
        """Club member autofill should search auctions from clubs the user can manage auctions for"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.save()
        other_club = Club.objects.create(name="Other Autofill Club")
        ClubMember.objects.create(club=other_club, user=self.owner, permission_manage_auctions=True)
        club_auction = Auction.objects.create(
            created_by=self.other_user,
            club=other_club,
            title="Other Club Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=2),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        club_location = PickupLocation.objects.create(
            name="other club location",
            auction=club_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        AuctionTOS.objects.create(
            auction=club_auction,
            pickup_location=club_location,
            manually_added=True,
            name="Searchable Member",
            email="searchable@example.com",
            phone_number="555-0100",
            address="123 Fish St",
        )
        self.client.force_login(self.owner)
        url = reverse("clubmember_validation", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"name": "Searchable Member"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id_email"], "searchable@example.com")
        self.assertEqual(response.json()["id_phone_number"], "555-0100")
        self.assertEqual(response.json()["id_address"], "123 Fish St")

    def test_club_member_autofill_uses_managed_club_members_without_auction_history(self):
        """Club member autofill should use manageable club members even without auction history"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.save()
        other_club = Club.objects.create(name="Member Autofill Club")
        ClubMember.objects.create(club=other_club, user=self.owner, permission_manage_auctions=True)
        ClubMember.objects.create(
            club=other_club,
            name="Managed Member",
            email="managed-member@example.com",
            phone_number="555-0111",
            address="456 Club Rd",
        )
        self.client.force_login(self.owner)
        url = reverse("clubmember_validation", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"name": "Managed Member"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id_email"], "managed-member@example.com")
        self.assertEqual(response.json()["id_phone_number"], "555-0111")
        self.assertEqual(response.json()["id_address"], "456 Club Rd")

    def test_club_member_create_modal_uses_inline_name_note_for_duplicates(self):
        """The club-member modal should render JS that shows duplicate-name warnings inline"""
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.save()
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("clubmember_create", kwargs={"slug": self.club.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "function cmHasAutocompleteData(response)")
        self.assertContains(response, "function cmSetFieldNote(fieldId, message)")
        self.assertContains(response, 'cmSetFieldNote("id_name", response.name_tooltip);')

    def test_merge_member_review_updates_kept_member_before_delete(self):
        owner_member, _ = ClubMember.objects.get_or_create(club=self.club, user=self.owner)
        owner_member.permission_add_edit = True
        owner_member.permission_view = True
        owner_member.save()
        source = ClubMember.objects.create(
            club=self.club,
            name="Source Member",
            email="source@example.com",
            phone_number="5551112222",
            address="111 Source St",
            permission_export=True,
            membership_last_paid=timezone.now().date(),
        )
        self.client.login(username="cu_owner", password="testpass")
        url = reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": source.pk})

        response = self.client.post(
            url, {"target": self.member.pk, "club_slug": self.club.slug, "exclude_member": source.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "will be deactivated")
        self.assertContains(response, "Jane Doe")

        response = self.client.post(
            url,
            {
                "step": "review",
                "target": self.member.pk,
                "name": "Merged Member",
                "email": "merged@example.com",
                "phone_number": "5553334444",
                "address": "222 Updated Ave",
            },
        )
        self.assertRedirects(response, reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.member.refresh_from_db()
        source.refresh_from_db()
        self.assertTrue(source.is_deleted)
        self.assertEqual(self.member.name, "Merged Member")
        self.assertEqual(self.member.email, "merged@example.com")
        self.assertEqual(self.member.phone_number, "5553334444")
        self.assertEqual(self.member.address, "222 Updated Ave")
        self.assertTrue(self.member.permission_export)
        self.assertEqual(self.member.membership_last_paid, timezone.now().date())
