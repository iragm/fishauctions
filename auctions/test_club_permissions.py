"""Club permissions in the awkward cases: wildcards, dialogs, Discord admin, view-only."""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    ClubMembershipSettingsForm,
)
from auctions.models import (
    Auction,
    Club,
    ClubHistory,
    ClubMember,
    Invoice,
    InvoicePayment,
    PayPalSeller,
    SquareSeller,
)


class ClubPermissionWildcardTests(TestCase):
    """Tests for check_club_permission — admin wildcard and individual bool checks."""

    def setUp(self):
        from auctions.views import check_club_permission

        self.check = check_club_permission
        self.club = Club.objects.create(name="Perm Wildcard Club")
        self.user = User.objects.create_user(username="perm_user", password="testpass", email="perm@example.com")

    def _make_member(self, **kwargs):
        defaults = {
            "permission_admin": False,
            "permission_view": False,
            "permission_export": False,
            "permission_add_edit": False,
            "permission_edit_club": False,
            "permission_money": False,
            "permission_manage_auctions": False,
            "permission_manage_bap": False,
        }
        defaults.update(kwargs)
        member, _ = ClubMember.objects.update_or_create(club=self.club, user=self.user, defaults=defaults)
        return member

    def test_admin_passes_all_permissions(self):
        self._make_member(permission_admin=True)
        for perm in [
            "permission_admin",
            "permission_view",
            "permission_export",
            "permission_add_edit",
            "permission_edit_club",
            "permission_money",
            "permission_manage_auctions",
            "permission_manage_bap",
        ]:
            self.assertTrue(self.check(self.user, self.club, perm), f"admin should pass {perm}")

    def test_no_permissions_fails_all(self):
        self._make_member()
        for perm in [
            "permission_admin",
            "permission_view",
            "permission_export",
            "permission_add_edit",
            "permission_edit_club",
            "permission_money",
            "permission_manage_auctions",
            "permission_manage_bap",
        ]:
            self.assertFalse(self.check(self.user, self.club, perm), f"no-perm should fail {perm}")

    def test_individual_bool_passes_only_its_own_check(self):
        all_perms = [
            "permission_admin",
            "permission_view",
            "permission_export",
            "permission_add_edit",
            "permission_edit_club",
            "permission_money",
            "permission_manage_auctions",
            "permission_manage_bap",
        ]
        for target_perm in all_perms:
            self._make_member(**{target_perm: True})
            for perm in all_perms:
                if perm == "permission_admin":
                    # permission_admin grants wildcard — skip for non-admin fields
                    continue
                if target_perm == "permission_admin":
                    # admin wildcard — all should pass
                    self.assertTrue(self.check(self.user, self.club, perm))
                elif perm == target_perm:
                    self.assertTrue(self.check(self.user, self.club, perm), f"{target_perm} set; {perm} should pass")
                else:
                    self.assertFalse(self.check(self.user, self.club, perm), f"{target_perm} set; {perm} should fail")

    def test_unauthenticated_user_fails_all(self):
        from django.contrib.auth.models import AnonymousUser

        anon = AnonymousUser()
        self.assertFalse(self.check(anon, self.club, "permission_view"))

    def test_superuser_passes_all(self):
        su = User.objects.create_superuser("superuser_perm", "su@example.com", "testpass")
        for perm in [
            "permission_admin",
            "permission_view",
            "permission_export",
            "permission_add_edit",
            "permission_edit_club",
            "permission_money",
            "permission_manage_auctions",
            "permission_manage_bap",
        ]:
            self.assertTrue(self.check(su, self.club, perm))

    def test_non_member_fails_all(self):
        other = User.objects.create_user(username="non_member", password="testpass", email="nm@example.com")
        for perm in ["permission_view", "permission_admin", "permission_export"]:
            self.assertFalse(self.check(other, self.club, perm))


class ClubPermissionsDialogTests(TestCase):
    """Tests for ClubMemberPermissionsView — admin-only HTMx dialog."""

    def setUp(self):
        self.club = Club.objects.create(name="Perms Dialog Club")
        self.admin_user = User.objects.create_user(
            username="perms_admin", password="testpass", email="perms_admin@example.com"
        )
        self.admin_member = ClubMember.objects.create(club=self.club, user=self.admin_user, permission_admin=True)
        self.target_user = User.objects.create_user(
            username="perms_target", password="testpass", email="perms_target@example.com"
        )
        self.target_member = ClubMember.objects.create(club=self.club, user=self.target_user)
        self.plain_user = User.objects.create_user(
            username="perms_plain", password="testpass", email="perms_plain@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.plain_user, permission_view=True)
        self.url = reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk})

    def test_admin_can_open_dialog(self):
        self.client.login(username="perms_admin", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_admin_gets_403_on_get(self):
        self.client.login(username="perms_plain", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_non_admin_gets_403_on_post(self):
        self.client.login(username="perms_plain", password="testpass")
        response = self.client.post(self.url, {"permission_add_edit": True})
        self.assertEqual(response.status_code, 403)

    def test_anon_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_admin_post_grants_permission(self):
        self.client.login(username="perms_admin", password="testpass")
        self.client.post(self.url, {"permission_add_edit": "on"})
        self.target_member.refresh_from_db()
        self.assertTrue(self.target_member.permission_add_edit)

    def test_admin_post_revokes_permission(self):
        self.target_member.permission_add_edit = True
        self.target_member.save()
        self.client.login(username="perms_admin", password="testpass")
        # Posting without the checkbox = False
        self.client.post(self.url, {})
        self.target_member.refresh_from_db()
        self.assertFalse(self.target_member.permission_add_edit)

    def test_admin_post_creates_club_history(self):
        self.client.login(username="perms_admin", password="testpass")
        before = ClubHistory.objects.filter(club=self.club).count()
        self.client.post(self.url, {"permission_view": "on"})
        self.assertGreater(ClubHistory.objects.filter(club=self.club).count(), before)


class ClubMemberDiscordAdminViewTests(TestCase):
    """Tests for ClubMemberDiscordAdminView — permission gating and basic functionality."""

    def setUp(self):
        self.club = Club.objects.create(name="Discord Test Club", discord_server_id="111222333")
        self.admin_user = User.objects.create_user(
            username="discord_admin", password="testpass", email="discord_admin@example.com"
        )
        self.edit_club_user = User.objects.create_user(
            username="discord_editclub", password="testpass", email="discord_editclub@example.com"
        )
        self.add_edit_user = User.objects.create_user(
            username="discord_addedit", password="testpass", email="discord_addedit@example.com"
        )
        self.view_user = User.objects.create_user(
            username="discord_view", password="testpass", email="discord_view@example.com"
        )
        self.non_member = User.objects.create_user(
            username="discord_nonmember", password="testpass", email="discord_nonmember@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.admin_user, name="Admin", permission_admin=True)
        ClubMember.objects.create(club=self.club, user=self.edit_club_user, name="EditClub", permission_edit_club=True)
        ClubMember.objects.create(club=self.club, user=self.add_edit_user, name="AddEdit", permission_add_edit=True)
        ClubMember.objects.create(club=self.club, user=self.view_user, name="View", permission_view=True)
        self.target_member = ClubMember.objects.create(
            club=self.club, name="Target", email="target_discord@example.com"
        )
        self.url = reverse("clubmember_discord", kwargs={"pk": self.target_member.pk})

    # --- Access control ---

    def test_anon_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_non_member_gets_403(self):
        self.client.login(username="discord_nonmember", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_view_only_gets_403(self):
        self.client.login(username="discord_view", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_add_edit_gets_403(self):
        self.client.login(username="discord_addedit", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_permission_edit_club_can_access(self):
        self.client.login(username="discord_editclub", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_permission_admin_can_access(self):
        self.client.login(username="discord_admin", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_cross_club_user_gets_403(self):
        other_club = Club.objects.create(name="Other Club")
        other_admin = User.objects.create_user(
            username="discord_other_admin", password="testpass", email="discord_other_admin@example.com"
        )
        ClubMember.objects.create(club=other_club, user=other_admin, name="OtherAdmin", permission_admin=True)
        self.client.login(username="discord_other_admin", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    # --- POST saves discord_id ---

    def test_admin_can_set_discord_id(self):
        self.client.login(username="discord_admin", password="testpass")
        response = self.client.post(
            self.url,
            {
                "discord_id": "987654321098765432",
                "discord_role_auto_managed": "on",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        self.target_member.refresh_from_db()
        self.assertEqual(self.target_member.discord_id, "987654321098765432")

    def test_admin_can_clear_discord_id(self):
        self.target_member.discord_id = "999888777666555444"
        self.target_member.save()
        self.client.login(username="discord_admin", password="testpass")
        self.client.post(
            self.url,
            {
                "discord_id": "",
                "discord_role_auto_managed": "on",
            },
        )
        self.target_member.refresh_from_db()
        self.assertIsNone(self.target_member.discord_id)

    def test_save_creates_club_history(self):
        self.client.login(username="discord_admin", password="testpass")
        before = ClubHistory.objects.filter(club=self.club).count()
        self.client.post(self.url, {"discord_id": "111222333444555666", "discord_role_auto_managed": "on"})
        self.assertGreater(ClubHistory.objects.filter(club=self.club).count(), before)

    def test_view_only_post_gets_403(self):
        self.client.login(username="discord_view", password="testpass")
        response = self.client.post(self.url, {"discord_id": "123"})
        self.assertEqual(response.status_code, 403)


class ClubMemberManagementViewTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Managed Club", enable_membership=True)
        self.editor_user = User.objects.create_user(
            username="club_editor", password="testpass", email="club_editor@example.com"
        )
        self.viewer_user = User.objects.create_user(
            username="club_viewer", password="testpass", email="club_viewer@example.com"
        )
        self.source_user = User.objects.create_user(
            username="club_source", password="testpass", email="club_source@example.com"
        )
        self.target_user = User.objects.create_user(
            username="club_target", password="testpass", email="club_target@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.editor_user, permission_add_edit=True, permission_view=True)
        ClubMember.objects.create(club=self.club, user=self.viewer_user, permission_view=True)
        self.source_member = ClubMember.objects.create(
            club=self.club,
            user=self.source_user,
            name="Source Member",
            email="source@example.com",
            phone_number="555-1111",
            membership_last_paid=timezone.now().date(),
            permission_manage_bap=True,
        )
        self.target_member = ClubMember.objects.create(
            club=self.club,
            user=self.target_user,
            name="Target Member",
            email="",
        )
        self.create_url = reverse("clubmember_create", kwargs={"slug": self.club.slug})
        self.validation_url = reverse("clubmember_validation", kwargs={"slug": self.club.slug})

    def test_editor_can_create_member_and_history(self):
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(
            self.create_url,
            {
                "name": "New Member",
                "email": "newmember@example.com",
                "phone_number": "",
                "address": "",
                "contact_status": "contact",
                "send_welcome_email": "on",
                "discord_role_auto_managed": "on",
                "discord_role_override": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        created = ClubMember.objects.get(club=self.club, email="newmember@example.com")
        self.assertEqual(created.source, "manually_added")
        self.assertEqual(created.added_by, self.editor_user)
        body = response.content.decode("utf-8")
        self.assertIn("closeModal", body)
        self.assertIn("reload-page", body)
        self.assertTrue(created.send_welcome_email)
        self.assertFalse(created.welcome_email_sent)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="Added member New Member").exists())

    def test_unsent_welcome_checkbox_shows_on_member_edit_modal(self):
        self.client.login(username="club_editor", password="testpass")
        response = self.client.get(reverse("clubmember_admin", kwargs={"pk": self.target_member.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id_send_welcome_email")

    def test_sent_welcome_checkbox_hidden_on_member_edit_modal(self):
        self.target_member.welcome_email_sent = True
        self.target_member.save(update_fields=["welcome_email_sent"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.get(reverse("clubmember_admin", kwargs={"pk": self.target_member.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'type="checkbox" name="send_welcome_email"')

    def test_viewer_cannot_create_member(self):
        self.client.login(username="club_viewer", password="testpass")
        response = self.client.get(self.create_url)
        self.assertEqual(response.status_code, 403)

    def test_member_validation_reports_duplicate_name_and_email(self):
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(
            self.validation_url,
            {"name": "Source Member", "email": "source@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name_tooltip"], "Source Member is already in this club")
        self.assertEqual(payload["email_tooltip"], "Email is already in this club")

    def test_member_validation_reports_duplicate_bidder_number(self):
        self.source_member.bidder_number = "42"
        self.source_member.save(update_fields=["bidder_number"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(self.validation_url, {"bidder_number": "42"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bidder_number_tooltip"], "Bidder number is already in this club")

    def test_renew_endpoint_updates_membership_and_history(self):
        self.source_member.membership_last_paid = None
        self.source_member.save(update_fields=["membership_last_paid"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        self.source_member.refresh_from_db()
        self.assertEqual(self.source_member.membership_last_paid, timezone.now().date())
        # For january_first system, expiration should be Jan 1 of next year
        expected_expiration = datetime.date(timezone.now().year + 1, 1, 1)
        self.assertEqual(self.source_member.membership_expiration_date, expected_expiration)
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, action__contains="Renewed membership for Source Member").exists()
        )

    @patch("auctions.models.ClubMember.maybe_assign_discord_role")
    def test_renew_endpoint_defers_discord_role_sync_to_daily_job(self, maybe_assign):
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        maybe_assign.assert_not_called()

    def test_renew_rolling_extends_from_current_expiration_if_future(self):
        """Rolling: if current expiration is in future, extend from that date."""
        self.club.membership_system = "rolling"
        self.club.save(update_fields=["membership_system"])
        future_expiration = timezone.now().date() + datetime.timedelta(days=100)
        self.source_member.membership_expiration_date = future_expiration
        self.source_member.save(update_fields=["membership_expiration_date"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        self.source_member.refresh_from_db()
        expected_expiration = future_expiration.replace(year=future_expiration.year + 1)
        self.assertEqual(self.source_member.membership_expiration_date, expected_expiration)

    def test_renew_rolling_extends_from_today_if_expiration_past(self):
        """Rolling: if current expiration is in past, extend from today."""
        self.club.membership_system = "rolling"
        self.club.save(update_fields=["membership_system"])
        past_expiration = timezone.now().date() - datetime.timedelta(days=100)
        self.source_member.membership_expiration_date = past_expiration
        self.source_member.save(update_fields=["membership_expiration_date"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        self.source_member.refresh_from_db()
        today = timezone.now().date()
        expected_expiration = today.replace(year=today.year + 1)
        self.assertEqual(self.source_member.membership_expiration_date, expected_expiration)

    def test_renew_rolling_extends_from_today_if_no_current_expiration(self):
        """Rolling: if no current expiration, extend from today."""
        self.club.membership_system = "rolling"
        self.club.save(update_fields=["membership_system"])
        self.source_member.membership_expiration_date = None
        self.source_member.membership_last_paid = None
        self.source_member.save(update_fields=["membership_expiration_date", "membership_last_paid"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        self.source_member.refresh_from_db()
        today = timezone.now().date()
        expected_expiration = today.replace(year=today.year + 1)
        self.assertEqual(self.source_member.membership_expiration_date, expected_expiration)

    def test_renew_january_first_extends_from_current_if_future(self):
        """January_first: if current expiration is in future, extend from that date to next Jan 1."""
        # Club defaults to january_first
        future_expiration = timezone.now().date() + datetime.timedelta(days=100)
        self.source_member.membership_expiration_date = future_expiration
        self.source_member.save(update_fields=["membership_expiration_date"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        self.source_member.refresh_from_db()
        expected_expiration = datetime.date(future_expiration.year + 1, 1, 1)
        self.assertEqual(self.source_member.membership_expiration_date, expected_expiration)

    def test_renew_january_first_extends_from_today_if_expiration_past(self):
        """January_first: if current expiration is in past, extend from today to next Jan 1."""
        # Club defaults to january_first
        past_expiration = timezone.now().date() - datetime.timedelta(days=100)
        self.source_member.membership_expiration_date = past_expiration
        self.source_member.save(update_fields=["membership_expiration_date"])
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        self.source_member.refresh_from_db()
        today = timezone.now().date()
        expected_expiration = datetime.date(today.year + 1, 1, 1)
        self.assertEqual(self.source_member.membership_expiration_date, expected_expiration)

    def test_renew_page_updates_requested_paid_date(self):
        self.client.login(username="club_editor", password="testpass")
        renew_page_url = reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.source_member.pk})
        response = self.client.post(renew_page_url, {"membership_expiration_date": "2024-01-15"})
        self.assertRedirects(response, reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.source_member.refresh_from_db()
        self.assertEqual(self.source_member.membership_expiration_date.isoformat(), "2024-01-15")

    def test_delete_endpoint_soft_deletes_member_and_logs_history(self):
        self.client.login(username="club_editor", password="testpass")
        response = self.client.post(reverse("club_member_delete", kwargs={"pk": self.source_member.pk}))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("closeModal", body)
        self.assertIn("clubMemberListChanged", response.get("HX-Trigger", ""))
        self.source_member.refresh_from_db()
        self.assertTrue(self.source_member.is_deleted)
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, action__contains="Deactivated member Source Member").exists()
        )

    def test_merge_view_combines_fields_permissions_and_soft_deletes_source(self):
        self.client.login(username="club_editor", password="testpass")
        merge_url = reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": self.source_member.pk})
        # Step 1: select target — should show review form
        response = self.client.post(merge_url, {"target": self.target_member.pk})
        self.assertEqual(response.status_code, 200)
        # Step 2: confirm merge with reviewed field values
        response = self.client.post(
            merge_url,
            {
                "step": "review",
                "target": self.target_member.pk,
                "name": "Target Member",
                "email": "source@example.com",
                "phone_number": "555-1111",
                "address": "",
            },
        )
        self.assertRedirects(response, reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.source_member.refresh_from_db()
        self.target_member.refresh_from_db()
        self.assertTrue(self.source_member.is_deleted)
        self.assertEqual(self.target_member.email, "source@example.com")
        self.assertEqual(self.target_member.phone_number, "555-1111")
        self.assertTrue(self.target_member.permission_manage_bap)
        self.assertEqual(self.target_member.membership_last_paid, timezone.now().date())
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="Merged member").exists())


class ClubViewOnlyAccessTests(TestCase):
    """Tests verifying view-only members can see the member list but not mutate anything."""

    def setUp(self):
        self.club = Club.objects.create(name="View Only Club")
        self.viewer_user = User.objects.create_user(
            username="viewer_user", password="testpass", email="viewer@example.com"
        )
        self.viewer_member = ClubMember.objects.create(club=self.club, user=self.viewer_user, permission_view=True)
        self.target_user = User.objects.create_user(
            username="view_target", password="testpass", email="view_target@example.com"
        )
        self.target_member = ClubMember.objects.create(club=self.club, user=self.target_user, name="Target Person")

    def test_viewer_can_access_club_admin(self):
        self.client.login(username="viewer_user", password="testpass")
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)

    def test_viewer_cannot_export_csv(self):
        self.client.login(username="viewer_user", password="testpass")
        response = self.client.get(reverse("club_member_export", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_access_bap_settings(self):
        self.client.login(username="viewer_user", password="testpass")
        response = self.client.get(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_edit_club(self):
        self.client.login(username="viewer_user", password="testpass")
        response = self.client.get(reverse("club_edit", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_post_to_clubmember_admin(self):
        self.client.login(username="viewer_user", password="testpass")
        url = reverse("clubmember_admin", kwargs={"pk": self.target_member.pk})
        response = self.client.post(url, {"name": "Hacked"})
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_access_permissions_dialog(self):
        self.client.login(username="viewer_user", password="testpass")
        url = reverse("clubmember_permissions", kwargs={"pk": self.target_member.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)


@override_settings(PAYPAL_CLIENT_ID="test_client_id", PAYPAL_SECRET="test_secret")
class ClubMembershipInvoiceTests(TestCase):
    """Tests for club-only membership invoices (no auction, no auctiontos_user).

    Covers Invoice model properties, _process_invoice_membership_renewal,
    ClubMembershipPaymentView, and error-redirect behaviour in the PayPal/Square views.
    """

    def setUp(self):
        self.client = Client()
        self.payment_user = User.objects.create_user(
            username="club_payer", password="testpass", email="payer@example.com"
        )
        self.payment_user.userdata.is_trusted = True
        self.payment_user.userdata.paypal_enabled = True
        self.payment_user.userdata.save()

        self.member_user = User.objects.create_user(
            username="club_member_u", password="testpass", email="member@example.com"
        )

        self.club = Club.objects.create(
            name="Pay Club",
            membership_annual_fee=Decimal("30.00"),
            membership_system="rolling",
        )
        self.club_member = ClubMember.objects.create(
            club=self.club,
            user=self.member_user,
            name="Alice Smith",
            email="member@example.com",
        )

    def _make_club_invoice(self, status="UNPAID"):
        return Invoice.objects.create(
            club=self.club,
            buyer=self.member_user,
            status=status,
            renewal_needed=True,
        )

    # -- model property tests --------------------------------------------------

    def test_currency_uses_payment_user_currency(self):
        invoice = self._make_club_invoice()
        # When no seller is linked, the invoice currency falls back to USD.
        self.assertEqual(invoice.currency, "USD")
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        invoice = self._make_club_invoice()
        self.assertEqual(invoice.currency, self.payment_user.userdata.currency)

    def test_membership_fee_amount_from_club(self):
        invoice = self._make_club_invoice()
        self.assertEqual(invoice.membership_fee_amount, Decimal("30.00"))

    def test_net_is_negative_membership_fee(self):
        invoice = self._make_club_invoice()
        self.assertEqual(invoice.net, Decimal("-30.00"))

    def test_net_after_payments_is_negative_before_payment(self):
        invoice = self._make_club_invoice()
        self.assertLess(invoice.net_after_payments, Decimal(0))

    def test_show_paypal_button_true_for_trusted_paypal_user(self):
        invoice = self._make_club_invoice()
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        self.assertTrue(invoice.show_paypal_button)

    def test_show_paypal_button_false_for_untrusted_payment_user(self):
        self.payment_user.userdata.is_trusted = False
        self.payment_user.userdata.save()
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        invoice = self._make_club_invoice()
        self.assertFalse(invoice.show_paypal_button)

    def test_show_paypal_button_true_when_use_site_paypal_account(self):
        # Equivalent to the old "superuser payment_user" behaviour: site PayPal is used.
        self.club.use_site_paypal_account = True
        self.club.save()
        invoice = self._make_club_invoice()
        self.assertTrue(invoice.show_paypal_button)

    def test_show_paypal_button_false_when_already_paid(self):
        invoice = self._make_club_invoice(status="PAID")
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        self.assertFalse(invoice.show_paypal_button)

    def test_show_payment_button_true_when_paypal_available(self):
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        invoice = self._make_club_invoice()
        self.assertTrue(invoice.show_payment_button)

    def test_show_payment_button_false_without_payment_credentials(self):
        invoice = self._make_club_invoice()
        self.assertFalse(invoice.show_paypal_button)
        self.assertFalse(invoice.show_square_button)
        self.assertFalse(invoice.show_payment_button)

    def test_reason_for_payment_not_available_returns_none_for_club_invoice(self):
        invoice = self._make_club_invoice()
        self.assertIsNone(invoice.reason_for_payment_not_available)

    def test_invoice_save_does_not_crash_without_auctiontos_user(self):
        invoice = self._make_club_invoice()
        invoice.status = "UNPAID"
        invoice.save()  # Should not raise

    def test_invoice_str_does_not_crash_without_auctiontos_user(self):
        invoice = self._make_club_invoice()
        result = str(invoice)
        self.assertIn("Pay Club", result)

    # -- _process_invoice_membership_renewal with club invoice -----------------

    def test_process_renewal_updates_membership_last_paid(self):
        from auctions.views.base import _process_invoice_membership_renewal

        invoice = self._make_club_invoice()
        _process_invoice_membership_renewal(invoice, payment_method="PayPal")
        self.club_member.refresh_from_db()
        self.assertIsNotNone(self.club_member.membership_last_paid)
        self.assertGreaterEqual(self.club_member.membership_last_paid, timezone.now().date())

    def test_process_renewal_creates_invoice_payment_record(self):
        from auctions.views.base import _process_invoice_membership_renewal

        invoice = self._make_club_invoice()
        _process_invoice_membership_renewal(invoice, payment_method="Square")
        self.assertTrue(
            InvoicePayment.objects.filter(club_member=self.club_member, payment_target="CLUB_MEMBER").exists()
        )

    def test_process_renewal_marks_invoice_renewal_processed(self):
        from auctions.views.base import _process_invoice_membership_renewal

        invoice = self._make_club_invoice()
        _process_invoice_membership_renewal(invoice, payment_method="PayPal")
        invoice.refresh_from_db()
        self.assertTrue(invoice.renewal_processed)

    def test_process_renewal_skipped_when_already_processed(self):
        from auctions.views.base import _process_invoice_membership_renewal

        invoice = self._make_club_invoice()
        invoice.renewal_processed = True
        invoice.save(update_fields=["renewal_processed"])
        _process_invoice_membership_renewal(invoice, payment_method="PayPal")
        self.assertIsNone(self.club_member.membership_last_paid)

    def test_process_renewal_via_club_member_link_no_user(self):
        from auctions.views.base import _process_invoice_membership_renewal

        member_no_user = ClubMember.objects.create(
            club=self.club,
            user=None,
            name="Bob Import",
            email="bob@example.com",
        )
        invoice = Invoice.objects.create(
            club=self.club,
            club_member=member_no_user,
            buyer=None,
            status="UNPAID",
            renewal_needed=True,
        )
        _process_invoice_membership_renewal(invoice, payment_method="PayPal")
        member_no_user.refresh_from_db()
        self.assertIsNotNone(member_no_user.membership_last_paid)
        self.assertIsNotNone(member_no_user.membership_expiration_date)
        invoice.refresh_from_db()
        self.assertTrue(invoice.renewal_processed)

    # -- ClubMembershipPaymentView ---------------------------------------------

    def test_payment_view_404_when_not_configured(self):
        # No PayPalSeller, no SquareSeller, no use_site_paypal_account => not configured.
        self.client.login(username="club_member_u", password="testpass")
        response = self.client.get(reverse("club_membership_pay", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 404)

    def test_payment_view_requires_login(self):
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        response = self.client.get(reverse("club_membership_pay", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 302)

    def test_payment_view_accessible_for_member(self):
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        self.client.login(username="club_member_u", password="testpass")
        response = self.client.get(reverse("club_membership_pay", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "30")

    def test_payment_view_creates_unpaid_invoice(self):
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        self.client.login(username="club_member_u", password="testpass")
        self.client.get(reverse("club_membership_pay", kwargs={"slug": self.club.slug}))
        self.assertTrue(
            Invoice.objects.filter(
                club=self.club, buyer=self.member_user, status="UNPAID", renewal_needed=True
            ).exists()
        )

    def test_payment_view_redirects_member_not_due_to_card(self):
        """A member whose dues are current is bounced back to their membership card."""
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_abc")
        self.club_member.membership_last_paid = timezone.now().date()
        self.club_member.membership_expiration_date = timezone.now().date() + datetime.timedelta(days=200)
        self.club_member.save()
        self.client.login(username="club_member_u", password="testpass")
        response = self.client.get(reverse("club_membership_pay", kwargs={"slug": self.club.slug}))
        self.assertRedirects(
            response,
            reverse("club_member_by_uuid", kwargs={"slug": self.club.slug, "uuid": self.club_member.uuid}),
            fetch_redirect_response=False,
        )

    # -- CreatePayPalOrderView redirects for club invoices ---------------------

    def test_paypal_order_view_redirects_to_club_pay_on_error(self):
        """When PayPal is not configured, the view should redirect to club_membership_pay, not invoice_no_login."""
        invoice = self._make_club_invoice()
        self.client.login(username="club_member_u", password="testpass")
        url = reverse("create_paypal_order", kwargs={"uuid": invoice.no_login_link})
        response = self.client.post(url)
        self.assertRedirects(
            response,
            reverse("club_membership_pay", kwargs={"slug": self.club.slug}),
            fetch_redirect_response=False,
        )

    @override_settings(PAYPAL_CLIENT_ID="x", PAYPAL_SECRET="y", SQUARE_APPLICATION_ID="sq", SQUARE_CLIENT_SECRET="sc")
    def test_paypal_order_view_blocked_when_only_square_configured(self):
        """show_payment_button=True (Square) but show_paypal_button=False should still block the PayPal endpoint."""
        # Give the club a Square seller but no PayPal seller.
        self.payment_user.userdata.square_enabled = True
        self.payment_user.userdata.save()
        SquareSeller.objects.create(user=self.payment_user, club=self.club, square_merchant_id="sq_merchant_only")
        invoice = self._make_club_invoice()
        # show_payment_button is True because Square is available.
        self.assertTrue(invoice.show_payment_button)
        # show_paypal_button must be False (no PayPal seller, not using site PayPal).
        self.assertFalse(invoice.show_paypal_button)

        self.client.login(username="club_member_u", password="testpass")
        url = reverse("create_paypal_order", kwargs={"uuid": invoice.no_login_link})
        response = self.client.post(url)
        # Should redirect back to club payment page, not proceed to PayPal.
        self.assertRedirects(
            response,
            reverse("club_membership_pay", kwargs={"slug": self.club.slug}),
            fetch_redirect_response=False,
        )

    def test_invoice_no_login_view_accessible_for_club_invoice(self):
        """Visiting a club invoice via no-login link should not crash (no auctiontos_user, no auction)."""
        invoice = self._make_club_invoice()
        self.client.login(username="club_member_u", password="testpass")
        url = reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class ClubMembershipSettingsFormFieldsTests(TestCase):
    """The form no longer exposes payment_user or allow_integrated_payments —
    those are managed via the per-provider seller links (PayPalSeller.club /
    SquareSeller.club) and Club.use_site_paypal_account in the Django admin.
    """

    def setUp(self):
        self.club = Club.objects.create(name="Test Club", enable_membership=True)

    def test_payment_user_not_in_form(self):
        form = ClubMembershipSettingsForm(instance=self.club)
        self.assertNotIn("payment_user", form.fields)

    def test_allow_integrated_payments_not_in_form(self):
        form = ClubMembershipSettingsForm(instance=self.club)
        self.assertNotIn("allow_integrated_payments", form.fields)


class PaymentSellerClubLinkTests(TestCase):
    """Behavioural tests for the new PayPalSeller.club / SquareSeller.club routing."""

    def setUp(self):
        self.creator = User.objects.create_user(username="creator", password="pw", email="creator@example.com")
        self.creator.userdata.is_trusted = True
        self.creator.userdata.paypal_enabled = True
        self.creator.userdata.save()

        self.club_payer = User.objects.create_user(username="cpayer", password="pw", email="cpayer@example.com")
        self.club_payer.userdata.is_trusted = True
        self.club_payer.userdata.paypal_enabled = True
        self.club_payer.userdata.save()

        self.club = Club.objects.create(name="Routing Club")

    @override_settings(PAYPAL_CLIENT_ID="x", PAYPAL_SECRET="y")
    def test_club_paypal_seller_takes_precedence_over_creator(self):
        """A club auction routes through the club's PayPalSeller, not the auction creator's."""
        PayPalSeller.objects.create(user=self.creator, paypal_merchant_id="creator_id")
        PayPalSeller.objects.create(user=self.club_payer, club=self.club, paypal_merchant_id="club_id")
        auction = Auction.objects.create(
            created_by=self.creator,
            club=self.club,
            title="Club Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        self.assertEqual(auction.paypal_information, "club_id")

    @override_settings(PAYPAL_CLIENT_ID="x", PAYPAL_SECRET="y")
    def test_use_site_paypal_account_returns_admin(self):
        """When the club is flagged for site PayPal, paypal_information is the 'admin' sentinel."""
        self.club.use_site_paypal_account = True
        self.club.save()
        auction = Auction.objects.create(
            created_by=self.creator,
            club=self.club,
            title="Site PayPal Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        self.assertEqual(auction.paypal_information, "admin")

    def test_link_existing_seller_via_view(self):
        """POSTing to club_link_payment_account attaches the user's seller to the club."""
        admin_member = ClubMember.objects.create(club=self.club, user=self.club_payer, permission_admin=True)
        self.assertIsNotNone(admin_member)
        seller = PayPalSeller.objects.create(user=self.club_payer, paypal_merchant_id="cp_id")
        self.client.login(username="cpayer", password="pw")
        url = reverse("club_link_payment_account", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"provider": "paypal", "action": "attach"})
        self.assertEqual(response.status_code, 302)
        seller.refresh_from_db()
        self.assertEqual(seller.club_id, self.club.pk)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, applies_to="SETTINGS").exists())

    def test_detach_seller_via_view(self):
        ClubMember.objects.create(club=self.club, user=self.club_payer, permission_admin=True)
        PayPalSeller.objects.create(user=self.club_payer, club=self.club, paypal_merchant_id="cp_id")
        self.client.login(username="cpayer", password="pw")
        url = reverse("club_link_payment_account", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"provider": "paypal", "action": "detach"})
        self.assertEqual(response.status_code, 302)
        seller = PayPalSeller.objects.get(user=self.club_payer)
        self.assertIsNone(seller.club_id)

    def test_disconnecting_seller_flips_club_auction_payments(self):
        ClubMember.objects.create(club=self.club, user=self.club_payer, permission_admin=True)
        seller = PayPalSeller.objects.create(user=self.club_payer, club=self.club, paypal_merchant_id="cp_id")
        auction = Auction.objects.create(
            created_by=self.creator,
            club=self.club,
            title="Disconnect Test Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
            enable_online_payments=True,
        )
        seller.delete()
        auction.refresh_from_db()
        self.assertFalse(auction.enable_online_payments)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, applies_to="SETTINGS").exists())
