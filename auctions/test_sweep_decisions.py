"""The code sweep's product decisions (September 2026): each test pins one decision as it was made."""

import datetime
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import palette_actions
from auctions.models import (
    Category,
    Club,
    ClubAnnouncement,
    ClubMember,
    Invoice,
    InvoicePayment,
    Lot,
    UserIgnoreCategory,
)
from auctions.tests import StandardTestCase
from auctions.views.base import club_from_url
from auctions.views.payments import _enable_payments_on_last_auction


def _run(test, name, params, user):
    request = test.client.request().wsgi_request
    request.user = user
    request.palette_page = {}
    return palette_actions.run_action(request, name, params)


class AuctionPageActionTests(StandardTestCase):
    """The auction page's banner buttons are POSTs; the old query-string flags do nothing."""

    def action(self, action, auction=None):
        url = reverse("auction_page_action", kwargs={"slug": (auction or self.online_auction).slug})
        return self.client.post(url, {"action": action})

    def test_a_link_no_longer_dismisses_anything(self):
        self.client.force_login(self.user)
        self.client.get(
            self.online_auction.get_absolute_url() + "?dismissed_promo_banner=true&enable_online_payments=1"
        )
        self.online_auction.refresh_from_db()
        self.assertFalse(self.online_auction.dismissed_promo_banner)
        self.assertFalse(self.online_auction.enable_online_payments)

    def test_an_admin_dismisses_the_promo_banner(self):
        self.client.force_login(self.user)
        response = self.action("dismiss_promo_banner")
        self.assertRedirects(response, self.online_auction.get_absolute_url(), fetch_redirect_response=False)
        self.online_auction.refresh_from_db()
        self.assertTrue(self.online_auction.dismissed_promo_banner)

    def test_only_the_creator_hides_their_payment_prompt(self):
        self.client.force_login(self.admin_user)  # an auction admin, not the creator
        self.assertEqual(self.action("never_show_paypal_connect").status_code, 403)
        self.admin_user.userdata.refresh_from_db()
        self.assertFalse(self.admin_user.userdata.never_show_paypal_connect)
        self.client.force_login(self.user)
        self.action("never_show_paypal_connect")
        self.user.userdata.refresh_from_db()
        self.assertTrue(self.user.userdata.never_show_paypal_connect)

    def test_a_non_admin_is_refused(self):
        self.client.force_login(self.user_who_does_not_join)
        self.assertEqual(self.action("dismiss_promo_banner").status_code, 403)

    def test_only_a_superuser_trusts_the_creator(self):
        self.user.userdata.is_trusted = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.assertEqual(self.action("trust_creator").status_code, 403)
        self.user.userdata.refresh_from_db()
        self.assertFalse(self.user.userdata.is_trusted)

    def test_the_paypal_connect_turns_payments_on_for_the_last_auction(self):
        auction = self.user.userdata.last_auction_created
        self.assertFalse(auction.enable_online_payments)
        _enable_payments_on_last_auction(self.user, "enable_online_payments")
        auction.refresh_from_db()
        self.assertTrue(auction.enable_online_payments)

    def test_make_current_posts_to_the_club_page_and_comes_back(self):
        club = Club.objects.create(name="Current Club")
        ClubMember.objects.create(club=club, user=self.user, name="Owner", permission_admin=True)
        self.online_auction.club = club
        self.online_auction.save()
        self.client.force_login(self.user)
        auction_url = self.online_auction.get_absolute_url()
        response = self.client.post(
            reverse("club_detail", kwargs={"slug": club.slug}) + f"?next={auction_url}",
            {"action": "make_current", "auction": self.online_auction.pk},
        )
        self.assertRedirects(response, auction_url, fetch_redirect_response=False)
        club.refresh_from_db()
        self.assertEqual(club.current_auction, self.online_auction)


class HideCategoryTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.category = Category.objects.create(name="Corals")

    def test_the_page_writes_only_on_post(self):
        self.client.force_login(self.user)
        url = f"/api/userignorecategory/create/{self.category.pk}/"
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertFalse(UserIgnoreCategory.objects.filter(user=self.user).exists())
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertTrue(UserIgnoreCategory.objects.filter(user=self.user, category=self.category).exists())
        self.assertEqual(self.client.post(f"/api/userignorecategory/delete/{self.category.pk}/").status_code, 200)
        self.assertFalse(UserIgnoreCategory.objects.filter(user=self.user).exists())

    def test_the_skill_hides_and_shows_a_category(self):
        result = _run(self, "hide_category", {"category": "corals"}, self.user)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(UserIgnoreCategory.objects.filter(user=self.user, category=self.category).exists())
        _run(self, "hide_category", {"category": "Corals", "hidden": False}, self.user)
        self.assertFalse(UserIgnoreCategory.objects.filter(user=self.user).exists())


class ForcedDonationTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.in_person_auction.force_donation_threshold = 2
        self.in_person_auction.save()
        self.lot = Lot.objects.create(
            lot_name="Guppies",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
        )

    def test_a_corrected_price_gives_the_lot_back_to_the_seller(self):
        self.lot.winning_price = 1
        self.lot.save()
        self.assertTrue(self.lot.donation)
        self.assertTrue(self.lot.donation_forced)
        self.lot.winning_price = 20
        self.lot.save()
        self.assertFalse(self.lot.donation)
        self.assertFalse(self.lot.donation_forced)

    def test_a_donation_the_seller_chose_stays_one(self):
        self.lot.donation = True
        self.lot.winning_price = 1
        self.lot.save()
        self.assertFalse(self.lot.donation_forced)
        self.lot.winning_price = 20
        self.lot.save()
        self.assertTrue(self.lot.donation)


class OneInvoicePerParticipantTests(StandardTestCase):
    def test_the_database_refuses_a_second_invoice(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.create(auctiontos_user=self.tosB, auction=self.online_auction)

    def test_for_participant_finds_or_makes_the_one_invoice(self):
        self.assertEqual(Invoice.for_participant(self.tosB), self.invoiceB)
        made = Invoice.for_participant(self.tosC)
        self.assertEqual(made.auction, self.online_auction)
        self.assertEqual(Invoice.for_participant(self.tosC), made)

    def test_club_dues_invoices_are_not_limited(self):
        club = Club.objects.create(name="Dues Club")
        Invoice.objects.create(club=club, status="UNPAID")
        Invoice.objects.create(club=club, status="UNPAID")
        self.assertEqual(Invoice.objects.filter(club=club).count(), 2)

    def test_one_record_per_provider_charge(self):
        InvoicePayment.objects.create(invoice=self.invoiceB, amount=5, external_id="sq-1", payment_method="Square")
        with self.assertRaises(IntegrityError), transaction.atomic():
            InvoicePayment.objects.create(invoice=self.invoiceB, amount=5, external_id="sq-1", payment_method="Square")
        # Cash has no provider id, and any number of those is fine.
        InvoicePayment.objects.create(invoice=self.invoiceB, amount=5, payment_method="Cash")
        InvoicePayment.objects.create(invoice=self.invoiceB, amount=5, payment_method="Cash")


class ClubNumberTests(TestCase):
    """Club URLs other systems keep use a number a rename doesn't change; slugs still work."""

    def test_a_club_gets_a_ten_digit_number(self):
        club = Club.objects.create(name="Number Club")
        self.assertEqual(len(str(club.number)), 10)
        self.assertEqual(club.url_key, str(club.number))

    def test_the_number_and_the_slug_both_find_the_club(self):
        club = Club.objects.create(name="Number Club")
        self.assertEqual(club_from_url(str(club.number)), club)
        self.assertEqual(club_from_url(club.slug), club)

    def test_a_renamed_clubs_feed_still_answers_at_its_number(self):
        club = Club.objects.create(name="Old Name")
        url = reverse("club_events_ical", kwargs={"slug": club.url_key})
        club.name = "New Name"
        club.save()
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_generated_links_use_the_number(self):
        club = Club.objects.create(name="Number Club")
        self.assertIn(f"/clubs/{club.number}/events.ics", club.calendar_subscribe_url("example.com"))


class SessionMemberAPIPointsTests(TestCase):
    """permission_add_edit edits members; changing their BAP/HAP totals takes permission_manage_bap."""

    @classmethod
    def setUpTestData(cls):
        cls.club = Club.objects.create(name="Points Club")
        cls.editor = User.objects.create_user("editor", password="pw")
        ClubMember.objects.create(
            club=cls.club, user=cls.editor, name="Editor", permission_view=True, permission_add_edit=True
        )
        cls.member = ClubMember.objects.create(club=cls.club, name="Breeder", bap_points=10)

    def patch(self, data):
        url = reverse("api_club_member_detail", kwargs={"slug": self.club.slug, "pk": self.member.pk})
        return self.client.patch(url, data, content_type="application/json")

    def test_add_edit_cannot_change_points(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.patch({"bap_points": 500}).status_code, 403)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bap_points, 10)

    def test_add_edit_can_still_edit_a_member_sending_the_points_unchanged(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.patch({"name": "Renamed", "bap_points": 10}).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.name, "Renamed")

    def test_manage_bap_can_change_points(self):
        ClubMember.objects.filter(user=self.editor).update(permission_manage_bap=True)
        self.client.force_login(self.editor)
        self.assertEqual(self.patch({"bap_points": 500}).status_code, 200)


class RetractWhichAnnouncementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.club = Club.objects.create(name="Announcing Club")
        cls.officer = User.objects.create_user("officer", password="pw")
        ClubMember.objects.create(club=cls.club, user=cls.officer, name="Officer", permission_send_announcements=True)

    def announce(self, text):
        return ClubAnnouncement.objects.create(club=self.club, text=text, created_by=self.officer)

    def test_the_default_is_still_the_newest(self):
        self.announce("Meeting moved to Tuesday")
        newest = self.announce("Auction on Saturday")
        with patch("auctions.announcements.retract", return_value={"never_sent": True}) as retract:
            _run(self, "retract_announcement", {"club": self.club.name}, self.officer)
        self.assertEqual(retract.call_args.args[0], newest)

    def test_naming_an_older_one_retracts_that_one(self):
        older = self.announce("Meeting moved to Tuesday")
        self.announce("Auction on Saturday")
        with patch("auctions.announcements.retract", return_value={"never_sent": True}) as retract:
            _run(self, "retract_announcement", {"club": self.club.name, "announcement": "tuesday"}, self.officer)
        self.assertEqual(retract.call_args.args[0], older)


class UpemailReachesAuctionRowsTests(TestCase):
    def test_the_member_is_saved_not_updated(self):
        club = Club.objects.create(name="Chimp Club", mailchimp_webhook_secret="s3")
        member = ClubMember.objects.create(club=club, name="Pat", email="old@example.com")
        with patch("auctions.models.ClubMember.save", autospec=True) as save:
            self.client.post(
                reverse("mailchimp_webhook", kwargs={"slug": club.url_key, "secret": "s3"}),
                {"type": "upemail", "data[old_email]": "old@example.com", "data[new_email]": "new@example.com"},
            )
        # The save is what carries the address onto the member's auction rows.
        self.assertEqual(save.call_args.args[0].pk, member.pk)
        self.assertEqual(save.call_args.kwargs["update_fields"], ["email"])


class PurgeBotUsersTests(TestCase):
    def make(self, username, joined_days_ago):
        user = User.objects.create_user(username, email=f"{username}@example.com")
        joined = timezone.now() - datetime.timedelta(days=joined_days_ago)
        User.objects.filter(pk=user.pk).update(date_joined=joined)
        user.userdata.last_activity = joined + datetime.timedelta(hours=1)
        user.userdata.save()
        EmailAddress.objects.create(user=user, email=user.email, verified=False, primary=True)
        return user

    def test_only_accounts_a_week_old_are_purged(self):
        new = self.make("new_bot", joined_days_ago=2)
        old = self.make("old_bot", joined_days_ago=10)
        call_command("purge_bot_users")
        self.assertTrue(User.objects.filter(pk=new.pk).exists())
        self.assertFalse(User.objects.filter(pk=old.pk).exists())


@override_settings(DISCORD_BOT_TOKEN="")
class DiscordSettingsNoteTests(TestCase):
    def test_the_page_says_the_email_is_not_verified(self):
        club = Club.objects.create(name="Discord Note Club")
        admin = User.objects.create_user("discord_admin", password="pw")
        ClubMember.objects.create(club=club, user=admin, name="Admin", permission_admin=True)
        self.client.force_login(admin)
        response = self.client.get(reverse("club_discord_config", kwargs={"slug": club.slug}))
        self.assertContains(response, "isn't verified")
