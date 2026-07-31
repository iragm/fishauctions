"""Tests for account deletion (Part D).

Deleting your account has to be possible from inside the app for both stores, and it has to do what
the page says it does. The interesting parts are the boundaries: what belongs to the person and goes,
what belongs to a club or an auction and stays, and the grace period that makes an accidental
deletion recoverable.
"""

import datetime
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.account_deletion import (
    GRACE_PERIOD_DAYS,
    cancel_deletion,
    delete_account,
    deletion_due_date,
    process_due_deletions,
    request_deletion,
)
from auctions.models import (
    AuctionTOS,
    Club,
    ClubMember,
    Lot,
    MobileDevice,
    PickupLocation,
    UserData,
    Watch,
)
from auctions.tests import StandardTestCase


class AccountDeletionRequestTests(TestCase):
    """The request itself: reversible, self-service, and it ends the session."""

    def setUp(self):
        self.user = User.objects.create_user(username="leaver", password="testpassword", email="leaver@example.com")
        self.client.login(username="leaver", password="testpassword")

    def test_page_is_reachable_from_preferences(self):
        response = self.client.get(reverse("preferences"))
        self.assertContains(response, reverse("account_delete"))

    def test_page_explains_before_it_asks(self):
        response = self.client.get(reverse("account_delete"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "What is deleted")
        self.assertContains(response, "What stays")
        self.assertContains(response, reverse("privacy_policy"))

    def test_requires_sign_in(self):
        self.client.logout()
        response = self.client.get(reverse("account_delete"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_the_wrong_username_does_nothing(self):
        response = self.client.post(reverse("account_delete"), {"confirm_username": "somebody-else"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(UserData.objects.get(user=self.user).account_deletion_requested)

    def test_confirming_schedules_it_and_signs_out(self):
        response = self.client.post(reverse("account_delete"), {"confirm_username": "leaver"})
        userdata = UserData.objects.get(user=self.user)
        self.assertIsNotNone(userdata.account_deletion_requested)
        # Ending at /logout/ is what turns the web sign-out into a full native sign-out in the app.
        self.assertIn(reverse("account_logout"), response.url)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_nothing_is_deleted_yet(self):
        self.client.post(reverse("account_delete"), {"confirm_username": "leaver"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "leaver")
        self.assertTrue(self.user.is_active)

    def test_it_emails_the_date(self):
        with patch("post_office.mail.send") as send:
            self.client.post(reverse("account_delete"), {"confirm_username": "leaver"})
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0], "leaver@example.com")

    def test_asking_twice_does_not_extend_the_grace_period(self):
        first = request_deletion(self.user)
        self.user.userdata.refresh_from_db()
        again = request_deletion(self.user)
        self.assertEqual(first, again)

    def test_signing_in_cancels_it(self):
        request_deletion(self.user)
        self.client.logout()
        self.assertTrue(self.client.login(username="leaver", password="testpassword"))
        self.assertIsNone(UserData.objects.get(user=self.user).account_deletion_requested)

    def test_cancel_button_cancels_it(self):
        request_deletion(self.user)
        response = self.client.post(reverse("account_delete"), {"action": "cancel"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(UserData.objects.get(user=self.user).account_deletion_requested)

    def test_the_page_offers_the_way_out_once_scheduled(self):
        request_deletion(self.user)
        response = self.client.get(reverse("account_delete"))
        self.assertContains(response, "Keep my account")

    def test_confirmation_page_is_public(self):
        # The session is gone by the time this loads, so it can't require one.
        self.client.logout()
        self.assertEqual(self.client.get(reverse("account_deleted")).status_code, 200)


class AccountDeletionScheduleTests(TestCase):
    """The grace period is the only undo there is; the job that ends it has to be exact."""

    def setUp(self):
        self.user = User.objects.create_user(username="waiting", password="x", email="waiting@example.com")

    def test_due_date_is_the_grace_period(self):
        due = request_deletion(self.user)
        self.assertEqual(due.date(), (timezone.now() + datetime.timedelta(days=GRACE_PERIOD_DAYS)).date())

    def test_not_run_before_the_grace_period_is_up(self):
        request_deletion(self.user)
        self.assertEqual(process_due_deletions(), 0)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_run_once_the_grace_period_is_up(self):
        request_deletion(self.user)
        UserData.objects.filter(user=self.user).update(
            account_deletion_requested=timezone.now() - datetime.timedelta(days=GRACE_PERIOD_DAYS + 1)
        )
        self.assertEqual(process_due_deletions(), 1)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_cancelled_requests_are_left_alone(self):
        request_deletion(self.user)
        UserData.objects.filter(user=self.user).update(
            account_deletion_requested=timezone.now() - datetime.timedelta(days=GRACE_PERIOD_DAYS + 1)
        )
        cancel_deletion(self.user)
        self.assertEqual(process_due_deletions(), 0)

    def test_accounts_that_never_asked_are_left_alone(self):
        self.assertIsNone(deletion_due_date(self.user.userdata))
        self.assertEqual(process_due_deletions(), 0)

    def test_the_management_command_runs_it(self):
        request_deletion(self.user)
        UserData.objects.filter(user=self.user).update(
            account_deletion_requested=timezone.now() - datetime.timedelta(days=GRACE_PERIOD_DAYS + 1)
        )
        call_command("delete_pending_accounts")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_one_broken_account_does_not_stall_the_others(self):
        other = User.objects.create_user(username="waiting2", password="x")
        for user in (self.user, other):
            request_deletion(user)
        UserData.objects.all().update(
            account_deletion_requested=timezone.now() - datetime.timedelta(days=GRACE_PERIOD_DAYS + 1)
        )
        real_delete = delete_account

        message = "club integration is down"

        def explode(user):
            if user.pk == self.user.pk:
                raise RuntimeError(message)
            return real_delete(user)

        with patch("auctions.account_deletion.delete_account", side_effect=explode):
            self.assertEqual(process_due_deletions(), 1)
        other.refresh_from_db()
        self.assertFalse(other.is_active)


class PersonalDataIsDeletedTests(TestCase):
    """Everything that is only ever about this one person actually goes."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="gone", password="x", email="gone@example.com", first_name="Gone", last_name="Person"
        )
        userdata = self.user.userdata
        userdata.phone_number = "555-1234"
        userdata.address = "1 Fish Street"
        userdata.latitude = 40.4
        userdata.longitude = -79.9
        userdata.last_ip_address = "10.0.0.1"
        userdata.save()
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        delete_account(self.user)
        self.user.refresh_from_db()

    def test_the_sign_in_is_gone(self):
        self.assertEqual(self.user.username, f"deleted-user-{self.user.pk}")
        self.assertEqual(self.user.email, "")
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.user.has_usable_password())

    def test_the_name_is_gone(self):
        self.assertEqual(self.user.first_name, "")
        self.assertEqual(self.user.last_name, "")

    def test_the_profile_is_gone(self):
        userdata = UserData.objects.get(user=self.user)
        self.assertIsNone(userdata.phone_number)
        self.assertIsNone(userdata.address)
        self.assertIsNone(userdata.last_ip_address)
        self.assertEqual(userdata.latitude, 0)
        self.assertEqual(userdata.longitude, 0)

    def test_devices_and_their_push_tokens_are_gone(self):
        self.assertFalse(MobileDevice.objects.filter(user=self.user).exists())

    def test_they_cannot_sign_in_again(self):
        self.assertFalse(self.client.login(username="gone", password="x"))

    def test_running_it_twice_is_harmless(self):
        delete_account(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, f"deleted-user-{self.user.pk}")


class ClubRecordsSurviveTests(TestCase):
    """A club's own records are the club's, and can't be wiped by a member leaving the site.

    ``ClubMember.admin_edited`` is the line: a record an admin created or edited stays whole and
    only loses the account link; one the member made about themselves goes with the account.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="member", password="x", email="member@example.com")
        self.club = Club.objects.create(name="Test club")
        self.admin_record = ClubMember.objects.create(
            club=self.club,
            user=self.user,
            name="Real Name",
            email="member@example.com",
            phone_number="555-0000",
            bap_points=42,
            admin_edited=True,
        )
        self.own_record = ClubMember.objects.create(
            club=Club.objects.create(name="Other club"),
            user=self.user,
            name="Real Name",
            email="member@example.com",
            admin_edited=False,
        )

    def test_a_record_the_club_edited_keeps_everything_but_the_link(self):
        delete_account(self.user)
        self.admin_record.refresh_from_db()
        self.assertIsNone(self.admin_record.user)
        self.assertEqual(self.admin_record.name, "Real Name")
        self.assertEqual(self.admin_record.email, "member@example.com")
        self.assertEqual(self.admin_record.bap_points, 42)
        self.assertFalse(self.admin_record.is_deleted)

    def test_a_record_the_member_made_themselves_goes(self):
        delete_account(self.user)
        self.own_record.refresh_from_db()
        self.assertIsNone(self.own_record.user)
        self.assertNotEqual(self.own_record.name, "Real Name")
        self.assertIsNone(self.own_record.email)
        self.assertTrue(self.own_record.is_deleted)

    def test_admin_edited_defaults_to_kept(self):
        """Existing rows and anything an admin touches are the club's — the safe default."""
        member = ClubMember.objects.create(club=self.club, name="Someone")
        self.assertTrue(member.admin_edited)

    def test_joining_a_club_yourself_is_your_own_record(self):
        self.club.allow_joining = True
        self.club.save()
        joiner = User.objects.create_user(username="joiner", password="testpassword", email="joiner@example.com")
        self.client.login(username="joiner", password="testpassword")
        self.client.post(reverse("club_detail", kwargs={"slug": self.club.slug}), {"action": "join"})
        member = ClubMember.objects.get(club=self.club, user=joiner)
        self.assertFalse(member.admin_edited)

    def test_an_admin_edit_hands_the_record_to_the_club(self):
        from auctions.forms import ClubMemberPermissionsForm

        form = ClubMemberPermissionsForm({"permission_view": True}, instance=self.own_record)
        self.assertTrue(form.is_valid())
        form.save()
        self.own_record.refresh_from_db()
        self.assertTrue(self.own_record.admin_edited)

    def test_a_kept_record_is_marked_do_not_contact(self):
        """The club keeps the record, but the person asked to be gone -- and the next admin edit
        would otherwise sync them back onto the mailing list we just removed them from."""
        delete_account(self.user)
        self.admin_record.refresh_from_db()
        self.assertEqual(self.admin_record.contact_status, "do_not_contact")

    def test_the_page_says_which_is_which(self):
        self.client.login(username="member", password="x")
        response = self.client.get(reverse("account_delete"))
        self.assertContains(response, "stay")
        self.assertContains(response, "the club's own record of a member")

    def test_marketing_contacts_are_deleted_not_just_unsubscribed(self):
        """An unsubscribe archives the contact, and the archive still holds the address."""
        self.club.mailchimp_access_token = "token"
        self.club.mailchimp_audience_id = "list"
        self.club.mailchimp_server_prefix = "us1"
        self.club.save()
        with (
            patch("auctions.tasks.delete_marketing_contact.delay") as delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_account(self.user)
        delay.assert_called_once_with(self.club.pk, "member@example.com")


class AuctionRecordsSurviveTests(StandardTestCase):
    """Bids, invoices and sold lots are other people's records too — they keep adding up."""

    def setUp(self):
        super().setUp()
        self.leaver = self.user_with_no_lots
        self.tos = self.in_person_buyer

    def test_the_auction_keeps_the_bidder_number_and_loses_the_person(self):
        self.tos.name = "Real Name"
        self.tos.email = "real@example.com"
        self.tos.phone_number = "555-9999"
        self.tos.save()
        delete_account(self.leaver)
        self.tos.refresh_from_db()
        self.assertEqual(self.tos.bidder_number, "555")
        self.assertIsNone(self.tos.user)
        self.assertEqual(self.tos.name, "Deleted user")
        self.assertIsNone(self.tos.email)
        self.assertIsNone(self.tos.phone_number)

    def test_a_club_managed_auction_keeps_what_the_club_keeps(self):
        """Blanking the auction row would only leave the club's own records disagreeing."""
        club = Club.objects.create(name="Managed club")
        self.in_person_auction.club = club
        self.in_person_auction.save()
        member = ClubMember.objects.create(
            club=club, user=self.leaver, name="Real Name", email="real@example.com", admin_edited=True
        )
        self.tos.clubmember = member
        self.tos.name = "Real Name"
        self.tos.save()
        delete_account(self.leaver)
        self.tos.refresh_from_db()
        self.assertIsNone(self.tos.user)
        self.assertEqual(self.tos.name, "Real Name")

    def test_sold_lots_stay_in_their_auction(self):
        lot = Lot.objects.create(
            lot_name="Sold before leaving",
            auction=self.online_auction,
            auctiontos_seller=self.tosC,
            user=self.leaver,
            quantity=1,
            winning_price=25,
            active=False,
        )
        delete_account(self.leaver)
        lot.refresh_from_db()
        self.assertIsNone(lot.user)
        self.assertEqual(lot.winning_price, 25)
        self.assertFalse(lot.is_deleted)

    def test_a_standalone_lot_comes_off_the_site(self):
        """Nobody is left to sell it, and it isn't part of anyone else's records."""
        lot = Lot.objects.create(lot_name="Just mine", user=self.leaver, quantity=1, reserve_price=5)
        delete_account(self.leaver)
        lot.refresh_from_db()
        self.assertTrue(lot.deactivated)

    def test_bids_keep_resolving(self):
        from auctions.models import Bid

        bid = Bid.objects.create(user=self.leaver, lot_number=self.lot, amount=15)
        delete_account(self.leaver)
        bid.refresh_from_db()
        self.assertEqual(bid.amount, 15)
        self.assertEqual(bid.user_id, self.leaver.pk)

    def test_invoices_still_add_up(self):
        from auctions.models import Invoice

        invoice = Invoice.objects.get_or_create(auctiontos_user=self.tosC)[0]
        total_before = invoice.rounded_net
        delete_account(self.leaver)
        invoice.refresh_from_db()
        self.assertEqual(invoice.rounded_net, total_before)

    def test_watched_lots_are_the_users_own_and_go(self):
        Watch.objects.create(lot_number=self.lot, user=self.leaver)
        delete_account(self.leaver)
        self.assertFalse(Watch.objects.filter(user=self.leaver).exists())

    def test_page_views_keep_the_count_and_lose_the_person(self):
        from auctions.models import PageView

        PageView.objects.create(user=self.leaver, lot_number=self.lot, ip_address="10.0.0.5", session_id="abc")
        delete_account(self.leaver)
        view = PageView.objects.filter(lot_number=self.lot).first()
        self.assertIsNotNone(view)
        self.assertIsNone(view.user)
        self.assertIsNone(view.ip_address)


class DeletionSummaryTests(StandardTestCase):
    """The page counts this account's own records, so the warning isn't generic."""

    def test_counts_what_will_happen(self):
        from auctions.account_deletion import deletion_summary

        club = Club.objects.create(name="Summary club")
        ClubMember.objects.create(club=club, user=self.user_with_no_lots, admin_edited=True)
        ClubMember.objects.create(
            club=Club.objects.create(name="Summary club 2"), user=self.user_with_no_lots, admin_edited=False
        )
        summary = deletion_summary(self.user_with_no_lots)
        self.assertEqual(summary["club_memberships_kept"], 1)
        self.assertEqual(summary["auctions_created"], 0)
        self.assertEqual(summary["club_memberships_deleted"], 1)
        self.assertEqual(summary["auctions"], AuctionTOS.objects.filter(user=self.user_with_no_lots).count())


class PickupLocationSanityTests(TestCase):
    """A deleted seller must not take an auction's pickup locations down with them."""

    def test_locations_survive(self):
        user = User.objects.create_user(username="organizer", password="x")
        from auctions.models import Auction

        auction = Auction.objects.create(
            created_by=user,
            title="Someone else's auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=1),
            date_start=timezone.now(),
        )
        location = PickupLocation.objects.create(
            name="Pickup", auction=auction, user=user, pickup_time=timezone.now() + datetime.timedelta(days=1)
        )
        delete_account(user)
        location.refresh_from_db()
        auction.refresh_from_db()
        self.assertEqual(location.name, "Pickup")
        self.assertFalse(auction.is_deleted)


class MobileSignInCancelsDeletionTests(TestCase):
    """Someone who deleted from inside the app comes back through the app, not the web login.

    The web cancels on the ``user_logged_in`` signal, which a JWT login never fires — without this
    the page's promise ("sign in again and it's cancelled") would be false for app users.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="apper", password="testpassword", email="apper@example.com")
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        request_deletion(self.user)

    def test_mobile_login_cancels_it(self):
        response = self.client.post(
            reverse("mobile-auth-login"),
            data={"credential": "apper", "password": "testpassword"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(UserData.objects.get(user=self.user).account_deletion_requested)

    def test_a_failed_login_does_not(self):
        self.client.post(
            reverse("mobile-auth-login"),
            data={"credential": "apper", "password": "wrong"},
            content_type="application/json",
        )
        self.assertIsNotNone(UserData.objects.get(user=self.user).account_deletion_requested)
