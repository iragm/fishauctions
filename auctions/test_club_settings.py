"""A club's own settings pages: BAP, general settings and email routing."""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.email_routing import resolve_routed_recipient
from auctions.forms import (
    ClubEmailSettingsForm,
)
from auctions.models import (
    Auction,
    Category,
    Club,
    ClubBapCategoryOverride,
    ClubHistory,
    ClubMember,
    PayPalSeller,
)
from auctions.tests import StandardTestCase


class ClubBapSettingsViewTests(TestCase):
    """Permission and basic access tests for ClubBapSettingsView."""

    def setUp(self):
        self.owner = User.objects.create_user(username="bap_owner", password="testpass", email="bap_owner@example.com")
        self.club = Club.objects.create(name="BAP Test Club", enable_breeder_award_program=True)
        self.bap_user = User.objects.create_user(username="bap_user", password="testpass", email="bap_user@example.com")
        self.bap_member = ClubMember.objects.create(club=self.club, user=self.bap_user, permission_manage_bap=True)
        self.plain_user = User.objects.create_user(
            username="plain_user", password="testpass", email="plain@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.plain_user)
        self.url = reverse("club_bap_settings", kwargs={"slug": self.club.slug})

    def test_anon_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_member_without_bap_permission_gets_403(self):
        self.client.login(username="plain_user", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_bap_admin_can_access(self):
        self.client.login(username="bap_user", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_bap_admin_can_save_settings(self):
        self.client.login(username="bap_user", password="testpass")
        response = self.client.post(
            self.url,
            {
                "auto_add_points": True,
                "points_per_lot": 0,
                "points_for_custom_checkbox": 0,
                "min_quantity": 3,
                "days_between_same_name_lots": 0,
                "days_between_same_species_lots": 0,
                "only_active_members_can_participate": False,
                "only_donation_lots": False,
                "separate_hap": False,
                "separate_cap": False,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertEqual(self.club.min_quantity, 3)

    def test_form_save_creates_bap_history(self):
        self.client.login(username="bap_user", password="testpass")
        self.client.post(
            self.url,
            {
                "auto_add_points": True,
                "points_per_lot": 0,
                "points_for_custom_checkbox": 0,
                "min_quantity": 5,
                "days_between_same_name_lots": 0,
                "days_between_same_species_lots": 0,
                "only_active_members_can_participate": False,
                "only_donation_lots": False,
                "separate_hap": False,
                "separate_cap": False,
            },
        )
        history = ClubHistory.objects.filter(club=self.club, applies_to="BAP").first()
        self.assertIsNotNone(history)
        self.assertEqual(history.user, self.bap_user)

    def test_club_admin_without_bap_role_gets_403(self):
        """permission_edit_club alone does not grant access to BAP settings."""
        settings_user = User.objects.create_user(
            username="settings_user", password="testpass", email="settings@example.com"
        )
        ClubMember.objects.create(club=self.club, user=settings_user, permission_edit_club=True)
        self.client.login(username="settings_user", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    # --- category override save/delete ---

    def test_bap_admin_can_save_category_override(self):
        category = Category.objects.create(name="Cichlids", bap_points=5)
        self.client.login(username="bap_user", password="testpass")
        url = reverse("club_bap_category_override_save", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"category": category.pk, "points": 10})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ClubBapCategoryOverride.objects.filter(club=self.club, category=category, points=10).exists())

    def test_plain_member_cannot_save_category_override(self):
        category = Category.objects.create(name="Tetras", bap_points=5)
        self.client.login(username="plain_user", password="testpass")
        url = reverse("club_bap_category_override_save", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"category": category.pk, "points": 10})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ClubBapCategoryOverride.objects.filter(club=self.club, category=category).exists())

    def test_bap_admin_can_delete_category_override(self):
        category = Category.objects.create(name="Barbs", bap_points=5)
        override = ClubBapCategoryOverride.objects.create(club=self.club, category=category, points=8)
        self.client.login(username="bap_user", password="testpass")
        url = reverse("club_bap_category_override_delete", kwargs={"slug": self.club.slug, "pk": override.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ClubBapCategoryOverride.objects.filter(pk=override.pk).exists())

    def test_plain_member_cannot_delete_category_override(self):
        category = Category.objects.create(name="Danios", bap_points=5)
        override = ClubBapCategoryOverride.objects.create(club=self.club, category=category, points=8)
        self.client.login(username="plain_user", password="testpass")
        url = reverse("club_bap_category_override_delete", kwargs={"slug": self.club.slug, "pk": override.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ClubBapCategoryOverride.objects.filter(pk=override.pk).exists())

    def test_save_override_is_idempotent_upsert(self):
        """Saving the same category twice updates points rather than creating a duplicate."""
        category = Category.objects.create(name="Livebearers", bap_points=5)
        self.client.login(username="bap_user", password="testpass")
        url = reverse("club_bap_category_override_save", kwargs={"slug": self.club.slug})
        self.client.post(url, {"category": category.pk, "points": 10})
        self.client.post(url, {"category": category.pk, "points": 15})
        self.assertEqual(ClubBapCategoryOverride.objects.filter(club=self.club, category=category).count(), 1)
        self.assertEqual(ClubBapCategoryOverride.objects.get(club=self.club, category=category).points, 15)

    def test_delete_override_from_other_club_does_nothing(self):
        """A BAP admin cannot delete an override belonging to a different club."""
        other_club = Club.objects.create(name="Other Club")
        category = Category.objects.create(name="Goldfish", bap_points=5)
        other_override = ClubBapCategoryOverride.objects.create(club=other_club, category=category, points=3)
        self.client.login(username="bap_user", password="testpass")
        url = reverse("club_bap_category_override_delete", kwargs={"slug": self.club.slug, "pk": other_override.pk})
        self.client.post(url)
        self.assertTrue(ClubBapCategoryOverride.objects.filter(pk=other_override.pk).exists())


class ClubSettingsViewTests(TestCase):
    def setUp(self):
        self.editor = User.objects.create_user(
            username="club_settings_editor", password="testpass", email="club_settings_editor@example.com"
        )
        self.auction_manager = User.objects.create_user(
            username="club_settings_auction_manager",
            password="testpass",
            email="club_settings_auction_manager@example.com",
        )
        self.plain = User.objects.create_user(
            username="club_settings_plain", password="testpass", email="club_settings_plain@example.com"
        )
        self.club = Club.objects.create(name="Settings Club", enable_membership=True)
        ClubMember.objects.create(club=self.club, user=self.editor, permission_edit_club=True, permission_add_edit=True)
        self.auction_member = ClubMember.objects.create(
            club=self.club,
            user=self.auction_manager,
            permission_manage_auctions=True,
        )
        ClubMember.objects.create(club=self.club, user=self.plain)
        self.edit_url = reverse("club_edit", kwargs={"slug": self.club.slug})
        self.membership_url = reverse("club_membership_settings", kwargs={"slug": self.club.slug})
        self.email_url = reverse("club_email_settings", kwargs={"slug": self.club.slug})

    def test_edit_view_rejects_external_next_redirect(self):
        self.client.login(username="club_settings_editor", password="testpass")
        response = self.client.post(
            f"{self.edit_url}?next=https://evil.example.com",
            {
                "name": "Updated Settings Club",
                "homepage": "https://example.com",
                "facebook_page": "https://facebook.com/settingsclub",
                "discord_invite_link": "https://discord.gg/settingsclub",
                "allow_joining": "on",
                "enable_breeder_award_program": "on",
                "enable_membership": "on",
                "description": "Updated description",
                "location": "Somewhere",
                "location_coordinates": "",
            },
        )
        self.assertRedirects(response, reverse("club_detail", kwargs={"slug": "updated-settings-club"}))
        self.club.refresh_from_db()
        self.assertEqual(self.club.name, "Updated Settings Club")
        self.assertEqual(self.club.discord_invite_link, "https://discord.gg/settingsclub")
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action="Updated club settings").exists())

    def test_membership_settings_save_updates_fields_and_creates_history(self):
        self.client.login(username="club_settings_editor", password="testpass")
        response = self.client.post(
            self.membership_url,
            {
                "membership_system": "rolling",
                "membership_annual_fee": "20.00",
                # show_member_barcode omitted → False (unchecked checkbox)
            },
        )
        self.assertRedirects(response, reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.club.refresh_from_db()
        self.assertEqual(self.club.membership_system, "rolling")
        self.assertEqual(self.club.membership_annual_fee, Decimal("20.00"))
        self.assertFalse(self.club.send_membership_expiration_reminders)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action="Updated membership settings").exists())

    def test_membership_settings_none_system_forces_zero_fee(self):
        """Selecting 'No membership fees' zeroes the fee even if one was submitted."""
        self.client.login(username="club_settings_editor", password="testpass")
        response = self.client.post(
            self.membership_url,
            {
                "membership_system": "none",
                "membership_annual_fee": "20.00",
            },
        )
        self.assertRedirects(response, reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.club.refresh_from_db()
        self.assertEqual(self.club.membership_system, "none")
        self.assertEqual(self.club.membership_annual_fee, Decimal(0))

    def test_membership_settings_paid_system_rejects_zero_fee(self):
        """A paid membership system requires a fee greater than 0."""
        self.client.login(username="club_settings_editor", password="testpass")
        response = self.client.post(
            self.membership_url,
            {
                "membership_system": "rolling",
                "membership_annual_fee": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "membership_annual_fee",
            [
                'Enter a fee greater than 0, or choose "No membership fees" above.',
            ],
        )
        self.club.refresh_from_db()
        self.assertEqual(self.club.membership_system, "none")

    def test_membership_settings_shows_form_without_connected_accounts(self):
        self.editor.userdata.paypal_enabled = True
        self.editor.userdata.save(update_fields=["paypal_enabled"])
        self.client.login(username="club_settings_editor", password="testpass")
        with override_settings(PAYPAL_CLIENT_ID="test_id", PAYPAL_SECRET="test_secret"):
            response = self.client.get(self.membership_url)
        self.assertEqual(response.status_code, 200)
        # The Payments section is always rendered; without a connected account or site PayPal,
        # the user is prompted to connect.
        self.assertContains(response, "Payments")
        self.assertContains(response, "Connect a PayPal account for this club")

    def test_plain_member_cannot_access_membership_settings(self):
        self.client.login(username="club_settings_plain", password="testpass")
        response = self.client.get(self.membership_url)
        self.assertEqual(response.status_code, 403)

    @override_settings(SES_ROUTE_EMAILS_ENABLED=False)
    def test_email_settings_available_without_ses(self):
        self.client.login(username="club_settings_editor", password="testpass")
        response = self.client.get(self.email_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send welcome letter to new club members")
        self.assertNotContains(response, "forwarding addresses")

    @override_settings(SES_ROUTE_EMAILS_ENABLED=True, EMAIL_ROUTING_DOMAIN="auction.fish")
    def test_email_settings_page_shows_when_using_ses(self):
        self.client.login(username="club_settings_editor", password="testpass")
        response = self.client.get(self.email_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"{self.club.slug}-auctions@auction.fish")
        self.assertContains(response, f"{self.club.slug}-contact@auction.fish")
        self.assertContains(response, "Send expiration reminder 30 days before membership expires")

    @override_settings(SES_ROUTE_EMAILS_ENABLED=True, EMAIL_ROUTING_DOMAIN="auction.fish")
    def test_email_settings_save_updates_fields_and_creates_history(self):
        self.client.login(username="club_settings_editor", password="testpass")
        editor_member = ClubMember.objects.get(club=self.club, user=self.editor)
        self.club.membership_annual_fee = Decimal("20.00")
        self.club.save(update_fields=["membership_annual_fee"])
        payment_user = User.objects.create_user(
            username="club_settings_payment_user",
            password="testpass",
            email="club_settings_payment_user@example.com",
        )
        PayPalSeller.objects.create(user=payment_user, club=self.club, paypal_merchant_id="merchant_123")
        response = self.client.post(
            self.email_url,
            {
                "auction_email_member": str(self.auction_member.pk),
                "contact_email_member": str(editor_member.pk),
                "send_welcome_email_to_new_members": "on",
                "send_membership_expiration_reminders_30_days": "on",
                "send_membership_expiration_reminders": "on",
                "send_membership_renewal_confirmation": "on",
                "welcome_opening": "Welcome to the club!",
                "welcome_closing": "See you at the next meeting.",
                "renewal_opening": "Your membership has been renewed!",
                "renewal_closing": "Thanks for staying with us.",
                "expiring_soon_opening": "Your membership expires soon.",
                "expiring_soon_closing": "Renew today to stay connected.",
            },
        )
        self.assertRedirects(response, reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_email_member, self.auction_member)
        self.assertEqual(self.club.contact_email_member, editor_member)
        self.assertTrue(self.club.send_welcome_email_to_new_members)
        self.assertTrue(self.club.send_membership_expiration_reminders_30_days)
        self.assertTrue(self.club.send_membership_expiration_reminders)
        self.assertTrue(self.club.send_membership_renewal_confirmation)
        self.assertEqual(self.club.welcome_opening, "Welcome to the club!")
        self.assertEqual(self.club.welcome_closing, "See you at the next meeting.")
        self.assertEqual(self.club.renewal_opening, "Your membership has been renewed!")
        self.assertEqual(self.club.renewal_closing, "Thanks for staying with us.")
        self.assertEqual(self.club.expiring_soon_opening, "Your membership expires soon.")
        self.assertEqual(self.club.expiring_soon_closing, "Renew today to stay connected.")
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action="Updated email settings").exists())

    def test_email_text_still_rejects_html_and_links(self):
        from auctions.forms import ClubEmailSettingsForm

        for value, message in (
            ("<b>Welcome</b>", "HTML tags are not allowed in email text."),
            ("Read https://example.com", "Links (URLs) are not allowed in email text."),
        ):
            form = ClubEmailSettingsForm(
                {"welcome_opening": value},
                instance=self.club,
                show_email_routing=False,
            )
            form.is_valid()
            self.assertIn(message, form.errors.get("welcome_opening", []), value)

    def test_a_value_built_to_be_slow_is_validated_quickly(self):
        """The tag check runs on submitted text, so it has to stay linear in its length.

        The old ``<[^>]+>`` scanned to the end of the value from every "<" in it, and took ~15
        seconds on this input; it now takes milliseconds.
        """
        import time

        from auctions.forms import ClubEmailSettingsForm

        form = ClubEmailSettingsForm(
            {"welcome_opening": "<" * 200000},
            instance=self.club,
            show_email_routing=False,
        )
        started = time.monotonic()
        form.is_valid()
        self.assertLess(time.monotonic() - started, 2.0)


class ClubEmailRoutingTests(TestCase):
    @override_settings(
        ADMINS=[("Admin", "admin@example.com")], SES_ROUTE_EMAILS_ENABLED=True, EMAIL_ROUTING_DOMAIN="auction.fish"
    )
    def test_resolve_routed_recipient_uses_configured_members_and_auction_creator(self):
        club = Club.objects.create(name="Routing Club")
        membership_user = User.objects.create_user("membership_route", email="membership@example.com", password="pw")
        auction_user = User.objects.create_user("auction_route", email="auction@example.com", password="pw")
        creator = User.objects.create_user("auction_creator", email="creator@example.com", password="pw")
        membership_member = ClubMember.objects.create(club=club, user=membership_user, permission_add_edit=True)
        auction_member = ClubMember.objects.create(club=club, user=auction_user, permission_manage_auctions=True)
        club.contact_email_member = membership_member
        club.auction_email_member = auction_member
        club.save(update_fields=["contact_email_member", "auction_email_member"])
        auction = Auction.objects.create(
            title="Routing Auction",
            created_by=creator,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        self.assertEqual(resolve_routed_recipient("info"), "admin@example.com")
        self.assertEqual(resolve_routed_recipient(f"{club.slug}-auctions"), "auction@example.com")
        self.assertEqual(resolve_routed_recipient(f"{club.slug}-contact"), "membership@example.com")
        self.assertEqual(resolve_routed_recipient(auction.slug), "creator@example.com")

    @override_settings(
        ADMINS=[("Admin", "admin@example.com")], SES_ROUTE_EMAILS_ENABLED=True, EMAIL_ROUTING_DOMAIN="auction.fish"
    )
    def test_resolve_routed_recipient_returns_none_for_unknown_aliases(self):
        """Unrecognized aliases and missing clubs/auctions return None so the caller can drop them."""
        club = Club.objects.create(name="Fallback Club")
        # Club exists but no members configured → auctions falls back to site admin, contact is dropped
        self.assertEqual(resolve_routed_recipient(f"{club.slug}-auctions"), "admin@example.com")
        self.assertIsNone(resolve_routed_recipient(f"{club.slug}-contact"))
        # No club with this slug → None (drop)
        self.assertIsNone(resolve_routed_recipient("nonexistent-slug-auctions"))
        self.assertIsNone(resolve_routed_recipient("nonexistent-slug-contact"))
        # Completely unrecognized pattern → None (drop)
        self.assertIsNone(resolve_routed_recipient("random-unknown-alias"))
        self.assertIsNone(resolve_routed_recipient("relay"))


class RoutedSenderDisplayNameTests(TestCase):
    """What the From line reads as once SES stops rewriting it.

    Gmail shows the display name and hides the address, so a routed alias with no name on it reads
    as a slug -- "spring-fling-2026" -- which tells a recipient less than "info" did.
    """

    routing = {"SES_ROUTE_EMAILS_ENABLED": True, "EMAIL_ROUTING_DOMAIN": "auction.fish"}

    def setUp(self):
        self.club = Club.objects.create(name="Burlington Fish Club")
        self.user = User.objects.create_user("sender_name_user", "sender_name@example.com", "pw")
        now = timezone.now()
        self.auction = Auction.objects.create(
            title="Spring Fling 2026",
            created_by=self.user,
            club=self.club,
            date_start=now,
            date_end=now + datetime.timedelta(days=1),
        )

    @override_settings(**routing)
    def test_auction_mail_is_from_the_club_over_the_auctions_own_address(self):
        self.assertEqual(
            self.auction.sender_email_with_name,
            f"Burlington Fish Club <{self.auction.slug}@auction.fish>",
        )

    @override_settings(**routing)
    def test_an_auction_with_no_club_falls_back_to_the_site(self):
        """Quoted because a dot is a special character in a display name; clients show it plain."""
        self.auction.club = None
        self.assertEqual(self.auction.sender_email_with_name, f'"auction.fish" <{self.auction.slug}@auction.fish>')

    @override_settings(**routing)
    def test_club_mail_is_from_the_club(self):
        self.assertEqual(
            self.club.contact_sender_email_with_name,
            f"Burlington Fish Club <{self.club.slug}-contact@auction.fish>",
        )

    @override_settings(SES_ROUTE_EMAILS_ENABLED=False)
    def test_no_routing_means_no_sender_at_all(self):
        """post_office reads None as "use DEFAULT_FROM_EMAIL", which is what happened before."""
        self.assertIsNone(self.auction.sender_email_with_name)
        self.assertIsNone(self.club.contact_sender_email_with_name)

    @override_settings(**routing)
    def test_a_club_name_with_a_quote_in_it_stays_one_address(self):
        """An f-string here would write a broken From and take the address down with it."""
        from email.utils import parseaddr

        self.club.name = 'Bob\'s "Fish" Club'
        name, address = parseaddr(self.club.contact_sender_email_with_name)
        self.assertEqual(name, 'Bob\'s "Fish" Club')
        self.assertEqual(address, f"{self.club.slug}-contact@auction.fish")


class SesSendsTheMessagesOwnFromAddressTests(TestCase):
    """django-ses must not be told to override the From address of every message.

    ``settings.AWS_SES_FROM_EMAIL`` is handed to the SES API as ``FromEmailAddress`` (``Source`` on
    the v1 path), and that parameter wins over the From header of the message.  While it was set to
    ``DEFAULT_FROM_EMAIL`` every email left the site as ``info@<domain>``, so the per-auction,
    per-club and per-vendor aliases that :mod:`auctions.email_routing` exists to put on the From
    line were built, queued, and then thrown away by SES -- replies went to the site admin instead
    of the club, and the From line read "info".  Nothing else in the suite can see this: every test
    above stops at what post_office stored, which was right all along.
    """

    def test_django_ses_sends_the_alias_the_caller_asked_for(self):
        from django.core.mail import EmailMessage
        from django_ses import SESBackend
        from django_ses.conf import settings as ses_settings

        alias = '"Test Aquarium Society" <test-aquarium-society-donations-1234567890@auction.fish>'
        message = EmailMessage(
            subject="Donation request",
            body="Please donate",
            from_email=alias,
            to=["vendor@example.com"],
        )
        # The same two lines SESBackend.send_messages() runs for every outgoing message.
        source = ses_settings.AWS_SES_FROM_EMAIL
        params = SESBackend()._get_send_email_parameters(message, source, None)
        self.assertEqual(params.get("FromEmailAddress") or params.get("Source"), alias)


class InboundEmailRoutingAPITests(TestCase):
    """Tests for the InboundEmailRoutingView API endpoint."""

    url = "/api/v1/email-routing/resolve/"

    @override_settings(
        ADMINS=[("Admin", "admin@example.com")],
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_resolves_info_to_admin(self):
        response = self.client.get(self.url, {"address": "info"}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recipient"], "admin@example.com")
        self.assertIn("display_name", response.json())

    @override_settings(
        ADMINS=[("Admin", "admin@example.com")],
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_accepts_full_email_address(self):
        response = self.client.get(self.url, {"address": "info@auction.fish"}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recipient"], "admin@example.com")

    @override_settings(
        ADMINS=[("Admin", "admin@example.com")],
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_resolves_club_auction_alias(self):
        club = Club.objects.create(name="API Routing Club")
        user = User.objects.create_user("api_route_user", "api_auction@example.com", "pw")
        member = ClubMember.objects.create(club=club, user=user, permission_manage_auctions=True)
        club.auction_email_member = member
        club.save(update_fields=["auction_email_member"])
        response = self.client.get(self.url, {"address": f"{club.slug}-auctions"}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recipient"], "api_auction@example.com")

    @override_settings(
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_returns_401_for_wrong_secret(self):
        response = self.client.get(self.url, {"address": "info"}, HTTP_X_ROUTING_SECRET="wrong-secret")
        self.assertEqual(response.status_code, 401)

    @override_settings(
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_returns_401_for_missing_secret(self):
        response = self.client.get(self.url, {"address": "info"})
        self.assertEqual(response.status_code, 401)

    @override_settings(
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_returns_400_for_missing_address(self):
        response = self.client.get(self.url, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 400)

    @override_settings(
        SES_ROUTE_EMAILS_ENABLED=False,
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_returns_503_when_routing_disabled(self):
        response = self.client.get(self.url, {"address": "info"}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 503)

    @override_settings(
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="",
    )
    def test_returns_401_when_no_secret_configured(self):
        response = self.client.get(self.url, {"address": "info"}, HTTP_X_ROUTING_SECRET="anything")
        self.assertEqual(response.status_code, 401)

    @override_settings(
        ADMINS=[("Admin", "admin@example.com")],
        SES_ROUTE_EMAILS_ENABLED=True,
        EMAIL_ROUTING_DOMAIN="auction.fish",
        INBOUND_ROUTING_SECRET="test-secret",
    )
    def test_returns_404_for_unknown_alias(self):
        """Completely unknown aliases should return 404 so the Lambda can drop them."""
        response = self.client.get(self.url, {"address": "relay"}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 404)
        response = self.client.get(
            self.url, {"address": "nonexistent-unknown-alias"}, HTTP_X_ROUTING_SECRET="test-secret"
        )
        self.assertEqual(response.status_code, 404)


class AuctionSlugSanitizationTests(TestCase):
    """Auction slugs must not end in -auctions or -contact (email routing collision)."""

    def _make_auction(self, title):
        user = User.objects.create_user(username=f"slug_test_{title[:8]}", password="pw")
        return Auction.objects.create(
            title=title,
            created_by=user,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

    def test_slug_strips_auctions_suffix(self):
        auction = self._make_auction("Spring Auctions")
        self.assertFalse(auction.slug.endswith("-auctions"), f"slug was {auction.slug!r}")

    def test_slug_strips_contact_suffix(self):
        auction = self._make_auction("Club Contact")
        self.assertFalse(auction.slug.endswith("-contact"), f"slug was {auction.slug!r}")

    def test_slug_unaffected_when_no_reserved_suffix(self):
        auction = self._make_auction("Spring Auction 2024")
        self.assertIn("spring", auction.slug)


class AuctionEmailSenderTests(StandardTestCase):
    @override_settings(SES_ROUTE_EMAILS_ENABLED=True, EMAIL_ROUTING_DOMAIN="auction.fish")
    def test_send_tos_notification_uses_auction_slug_sender(self):
        from email.utils import parseaddr

        from auctions.management.commands.auctiontos_notifications import send_tos_notification

        with patch("auctions.management.commands.auctiontos_notifications.mail.send") as mock_send:
            send_tos_notification("online_auction_welcome", self.online_tos)

        # The From line carries a display name as well now -- which one is
        # RoutedSenderDisplayNameTests' business, and it depends on whether this auction ended up
        # with a club, which SINGLE_CLUB_MODE decides. What this test is about is the address
        # behind the name: the auction's own routed alias.
        name, address = parseaddr(mock_send.call_args.kwargs["sender"])
        self.assertEqual(address, f"{self.online_auction.slug}@auction.fish")
        self.assertTrue(name)


class ClubEmailSettingsFormTests(TestCase):
    @override_settings(SES_ROUTE_EMAILS_ENABLED=True, EMAIL_ROUTING_DOMAIN="auction.fish")
    def test_form_limits_choices_by_permission(self):
        club = Club.objects.create(name="Email Form Club")
        membership_user = User.objects.create_user(
            "email_form_membership", email="membership@example.com", password="pw"
        )
        auction_user = User.objects.create_user("email_form_auction", email="auction@example.com", password="pw")
        membership_member = ClubMember.objects.create(club=club, user=membership_user, permission_add_edit=True)
        auction_member = ClubMember.objects.create(club=club, user=auction_user, permission_manage_auctions=True)

        form = ClubEmailSettingsForm(instance=club)

        self.assertEqual(
            set(form.fields["auction_email_member"].queryset.values_list("pk", flat=True)), {auction_member.pk}
        )
        self.assertEqual(
            set(form.fields["contact_email_member"].queryset.values_list("pk", flat=True)),
            {membership_member.pk},
        )

    def test_expiration_reminder_fields_are_disabled_without_membership_payments(self):
        club = Club.objects.create(name="No Payments Club", membership_annual_fee=Decimal("20.00"))
        form = ClubEmailSettingsForm(instance=club)

        self.assertTrue(form.fields["send_membership_expiration_reminders_30_days"].disabled)
        self.assertTrue(form.fields["send_membership_expiration_reminders"].disabled)
