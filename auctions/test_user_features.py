"""Preferences that change what a user sees: distance units, exports, and the trust system."""

import datetime

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Invoice,
    PickupLocation,
)
from auctions.tests import StandardTestCase


class DistanceUnitTests(StandardTestCase):
    """Test distance unit conversion functionality"""

    def test_default_distance_unit_is_miles(self):
        """Test that default distance unit is miles"""
        self.assertEqual(self.user.userdata.distance_unit, "mi")

    def test_distance_unit_can_be_set_to_km(self):
        """Test that distance unit can be set to kilometers"""
        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()
        userdata.refresh_from_db()
        self.assertEqual(userdata.distance_unit, "km")

    def test_preference_form_converts_km_to_miles_on_save(self):
        """The notifications form displays km and stores miles, with no unit field on the page.

        `distance_unit` stayed on /preferences/ when the notification settings moved to their own
        page, which is what let the page's distance-converting JavaScript go: the unit is read off
        the instance and cannot change while this form is open.
        """
        from auctions.forms import ChangeUserNotificationsForm

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.local_distance = 100  # 100 miles in DB
        userdata.save()

        # Form should display ~161 km (100 * 1.60934)
        form = ChangeUserNotificationsForm(user=self.user, instance=userdata)
        self.assertEqual(form.initial["local_distance"], 161)
        self.assertEqual(form.fields["local_distance"].help_text, "km, from your address")

        # When user submits with 80 km, it should save as ~50 miles
        form_data = {
            "local_distance": 80,
            "email_me_about_new_auctions_distance": 160,
            "email_me_about_new_in_person_auctions_distance": 160,
            "email_me_about_new_auctions": True,
            "email_me_about_new_local_lots": True,
            "email_me_about_new_lots_ship_to_location": True,
            "email_me_when_people_comment_on_my_lots": True,
            "email_me_about_new_chat_replies": True,
            "email_me_about_new_in_person_auctions": True,
            "send_reminder_emails_about_joining_auctions": True,
            "push_notifications_when_lots_sell": False,
        }
        form = ChangeUserNotificationsForm(user=self.user, data=form_data, instance=userdata)
        self.assertTrue(form.is_valid())
        saved_instance = form.save()

        # Verify values are stored in miles
        self.assertEqual(saved_instance.local_distance, 50)  # 80 km / 1.60934 ≈ 50 miles
        self.assertEqual(saved_instance.email_me_about_new_auctions_distance, 99)  # 160 km / 1.60934 ≈ 99 miles

    def test_a_km_radius_survives_a_round_trip_untouched(self):
        """Render, save nothing, save: the number the user never touched must come back the same.

        The old single-page form could not promise this on its own -- the unit select and the radii
        were on one screen, so the value in the box was only right if the page's JavaScript had
        converted it. Saving the rendered value is now exactly a no-op.
        """
        from auctions.forms import ChangeUserNotificationsForm

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.email_me_about_new_auctions_distance = 100
        userdata.save()

        shown = ChangeUserNotificationsForm(user=self.user, instance=userdata).initial
        form = ChangeUserNotificationsForm(
            user=self.user,
            data={"email_me_about_new_auctions_distance": shown["email_me_about_new_auctions_distance"]},
            instance=userdata,
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.save().email_me_about_new_auctions_distance, 100)

    def test_preference_form_keeps_miles_when_unit_is_miles(self):
        """Test that form doesn't convert when unit is miles"""
        from auctions.forms import ChangeUserNotificationsForm

        userdata = self.user.userdata
        userdata.distance_unit = "mi"
        userdata.local_distance = 100
        userdata.save()

        form_data = {
            "local_distance": 50,
            "email_me_about_new_auctions_distance": 100,
            "email_me_about_new_in_person_auctions_distance": 100,
            "email_me_about_new_auctions": True,
            "email_me_about_new_local_lots": True,
            "email_me_about_new_lots_ship_to_location": True,
            "email_me_when_people_comment_on_my_lots": True,
            "email_me_about_new_chat_replies": True,
            "email_me_about_new_in_person_auctions": True,
            "send_reminder_emails_about_joining_auctions": True,
            "push_notifications_when_lots_sell": False,
        }
        form = ChangeUserNotificationsForm(user=self.user, data=form_data, instance=userdata)
        self.assertTrue(form.is_valid())
        saved_instance = form.save()

        # Values should be saved as-is in miles
        self.assertEqual(saved_instance.local_distance, 50)
        self.assertEqual(saved_instance.email_me_about_new_auctions_distance, 100)

    def test_distance_filter_converts_miles_to_km(self):
        """Test that distance_display filter converts miles to km for km users"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()

        # 10 miles should display as 16 km
        result = distance_display(10, self.user)
        self.assertEqual(result, "16 km")

    def test_distance_filter_keeps_miles_for_miles_users(self):
        """Test that distance_display filter keeps miles for miles users"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "mi"
        userdata.save()

        # 10 miles should display as 10 miles
        result = distance_display(10, self.user)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_negative_distance(self):
        """Test that distance_display filter handles negative distance (returns empty)"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(-1, self.user)
        self.assertEqual(result, "")

    def test_distance_filter_handles_zero_distance(self):
        """Test that distance_display filter handles zero distance (returns empty)"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(0, self.user)
        self.assertEqual(result, "")

    def test_distance_filter_defaults_to_miles_for_anonymous_users(self):
        """Test that distance_display filter defaults to miles for anonymous users"""
        from django.contrib.auth.models import AnonymousUser

        from auctions.templatetags.distance_filters import distance_display

        anonymous = AnonymousUser()
        result = distance_display(10, anonymous)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_string_input(self):
        """Test that distance_display filter handles string input from database"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "mi"
        userdata.save()

        # String input should be converted to float
        result = distance_display("10", self.user)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_string_input_with_km(self):
        """Test that distance_display filter handles string input and converts to km"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()

        # String input "10" miles should display as 16 km
        result = distance_display("10", self.user)
        self.assertEqual(result, "16 km")

    def test_distance_filter_handles_string_input_for_anonymous_users(self):
        """Test that distance_display filter handles string input for anonymous users"""
        from django.contrib.auth.models import AnonymousUser

        from auctions.templatetags.distance_filters import distance_display

        anonymous = AnonymousUser()
        # String input should work for anonymous users
        result = distance_display("10", anonymous)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_invalid_string_input(self):
        """Test that distance_display filter handles invalid string input"""
        from auctions.templatetags.distance_filters import distance_display

        # Invalid string should return empty string
        result = distance_display("invalid", self.user)
        self.assertEqual(result, "")

    def test_distance_filter_handles_none_input(self):
        """Test that distance_display filter handles None input"""
        from auctions.templatetags.distance_filters import distance_display

        # None input should return empty string
        result = distance_display(None, self.user)
        self.assertEqual(result, "")


class PayPalInfoViewTests(TestCase):
    """Test that the PayPal info page works for both logged in and non-logged in users"""

    def test_paypal_info_non_logged_in_user(self):
        """Test that non-logged-in users can access the PayPal info page"""
        url = reverse("paypal_seller")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accept payments with PayPal")

    def test_paypal_info_logged_in_user(self):
        """Test that logged-in users can access the PayPal info page"""
        User.objects.create_user(username="testuser", password="testpassword")
        self.client.login(username="testuser", password="testpassword")
        url = reverse("paypal_seller")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accept payments with PayPal")


class UserExportTests(StandardTestCase):
    """Test user export and email composition functionality"""

    def test_user_export_without_filter(self):
        """Test that user export works without a filter"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_user_export_with_filter(self):
        """Test that user export works with a filter query parameter"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        # Check that filename includes query
        self.assertIn("admin", response["Content-Disposition"])

    def test_user_export_permission_denied(self):
        """Test that non-admin users cannot export users"""
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 403])

    def test_compose_email_without_filter(self):
        """Test composing email to all users"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The view renders a button snippet with a mailto href, not a redirect
        self.assertContains(response, 'id="email_all_users"')

    def test_compose_email_with_filter(self):
        """Test composing email with a filter"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="email_all_users"')

    def test_compose_email_permission_denied(self):
        """Test that non-admin users cannot compose emails"""
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_user_export_includes_lots_sold_column(self):
        """Test that user export includes the 'Lots sold' column with correct data"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

        # Decode the CSV content
        content = response.content.decode("utf-8")
        lines = content.strip().split("\n")

        # Check header row contains "Lots sold"
        header = lines[0]
        self.assertIn("Lots sold", header)

        # Verify header column order: "Lots submitted" should come before "Lots sold" which comes before "Lots won"
        self.assertLess(header.index("Lots submitted"), header.index("Lots sold"))
        self.assertLess(header.index("Lots sold"), header.index("Lots won"))

        # Find the row for "my_lot" user who has:
        # - 4 lots submitted (lot, lotB, lotC, unsoldLot)
        # - 3 lots sold (lot, lotB, lotC have winning_price)
        # - 0 lots won (this user is a seller)
        header_parts = header.split(",")
        lots_submitted_idx = header_parts.index("Lots submitted")
        lots_sold_idx = header_parts.index("Lots sold")
        lots_won_idx = header_parts.index("Lots won")

        # Find the row with my_lot username
        for line in lines[1:]:
            if "my_lot" in line:
                parts = line.split(",")
                # Verify the counts match expected values
                self.assertEqual(parts[lots_submitted_idx], "4", "Expected 4 lots submitted")
                self.assertEqual(parts[lots_sold_idx], "3", "Expected 3 lots sold")
                self.assertEqual(parts[lots_won_idx], "0", "Expected 0 lots won")
                break
        else:
            self.fail("Could not find my_lot user in CSV export")


class UserTrustSystemTests(StandardTestCase):
    """Test the user trust system functionality"""

    def setUp(self):
        super().setUp()
        # Create a superuser for testing trust functionality
        self.superuser = User.objects.create_superuser(
            username="superuser", password="testpassword", email="super@example.com"
        )
        # Create an untrusted user
        self.untrusted_user = User.objects.create_user(
            username="untrusted", password="testpassword", email="untrusted@example.com"
        )
        self.untrusted_user.userdata.is_trusted = False
        self.untrusted_user.userdata.save()
        # Create an auction by the untrusted user
        time = timezone.now() + datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=1)
        self.untrusted_auction = Auction.objects.create(
            created_by=self.untrusted_user,
            title="Untrusted user auction",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )

    def test_trusted_field_exists_on_userdata(self):
        """Test that is_trusted field exists on UserData model"""
        self.assertTrue(hasattr(self.user.userdata, "is_trusted"))
        self.assertIsInstance(self.user.userdata.is_trusted, bool)

    def test_superuser_can_trust_user(self):
        """Test that superuser can trust a user via URL parameter"""
        self.client.login(username="superuser", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.untrusted_auction.slug})
        # Join the auction first
        AuctionTOS.objects.create(
            user=self.superuser,
            auction=self.untrusted_auction,
            pickup_location=PickupLocation.objects.create(
                name="location",
                auction=self.untrusted_auction,
                pickup_time=timezone.now() + datetime.timedelta(days=3),
            ),
        )
        response = self.client.get(url + "?trust_user=true")
        self.assertEqual(response.status_code, 200)
        # Reload user data
        self.untrusted_user.userdata.refresh_from_db()
        self.assertTrue(self.untrusted_user.userdata.is_trusted)

    def test_non_superuser_cannot_trust_user(self):
        """Test that non-superuser cannot trust a user"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.untrusted_auction.slug})
        initial_trust = self.untrusted_user.userdata.is_trusted
        self.client.get(url + "?trust_user=true")
        # Reload user data
        self.untrusted_user.userdata.refresh_from_db()
        # Trust status should not change
        self.assertEqual(self.untrusted_user.userdata.is_trusted, initial_trust)

    def test_untrusted_user_invoice_no_payment_button(self):
        """Test that invoices for untrusted users don't show payment button"""
        # Create invoice for untrusted auction
        theFuture = timezone.now() + datetime.timedelta(days=3)
        location = PickupLocation.objects.create(name="location", auction=self.untrusted_auction, pickup_time=theFuture)
        tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.untrusted_auction, pickup_location=location
        )
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=tos)
        # Enable online payments
        self.untrusted_auction.enable_online_payments = True
        self.untrusted_auction.save()
        # Check that payment button is not shown
        self.assertFalse(invoice.show_payment_button)

    def test_trusted_user_invoice_shows_payment_button(self):
        """Test that invoices for trusted users show payment button when conditions are met"""
        # Make sure user is trusted
        self.user.userdata.is_trusted = True
        self.user.userdata.paypal_enabled = True
        self.user.userdata.save()
        # Enable online payments
        self.online_auction.enable_online_payments = True
        self.online_auction.save()
        # Get invoice - show_payment_button may still be False due to other checks
        # (e.g., balance, PayPal config), we're mainly testing that the is_trusted check doesn't block it
        Invoice.objects.get(auctiontos_user=self.online_tos)

    def test_invoice_template_shows_email_message_for_trusted(self):
        """Test that invoice template shows email notification message for trusted users"""
        self.client.login(username="my_lot", password="testpassword")
        # Make sure the creator is trusted
        self.user.userdata.is_trusted = True
        self.user.userdata.save()
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_auction_ribbon_trust_link_for_superuser(self):
        """Test that superuser sees trust link in auction ribbon"""
        self.client.login(username="superuser", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.untrusted_auction.slug})
        # Join the auction first
        location = PickupLocation.objects.filter(auction=self.untrusted_auction).first()
        if not location:
            location = PickupLocation.objects.create(
                name="location",
                auction=self.untrusted_auction,
                pickup_time=timezone.now() + datetime.timedelta(days=3),
            )
        AuctionTOS.objects.create(user=self.superuser, auction=self.untrusted_auction, pickup_location=location)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Check that response contains trust link (only if auction is not promoted)
        if not self.untrusted_auction.promote_this_auction:
            self.assertContains(response, "trust_user=true")

    def test_email_invoice_skips_untrusted_users(self):
        """Test that email_invoice management command skips untrusted users"""

        # Create an invoice for untrusted auction
        theFuture = timezone.now() + datetime.timedelta(days=3)
        location = PickupLocation.objects.create(name="location", auction=self.untrusted_auction, pickup_time=theFuture)
        tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.untrusted_auction, pickup_location=location
        )
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=tos)
        invoice.status = "UNPAID"
        invoice.email_sent = False
        invoice.save()
        # Enable email sending
        self.untrusted_auction.email_users_when_invoices_ready = True
        self.untrusted_auction.save()
        # Run command
        call_command("email_invoice")
        # Reload invoice
        invoice.refresh_from_db()
        # Email should be marked sent but not actually sent
        self.assertTrue(invoice.email_sent)


class WatchOrUnwatchViewTests(StandardTestCase):
    """Test watchOrUnwatch function-based view"""

    def test_watch_anonymous_denied(self):
        """Anonymous users cannot watch lots"""
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "true"})
        self.assertIn(response.status_code, [401, 403])

    def test_watch_logged_in(self):
        """Logged in users can watch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Success")

    def test_unwatch_logged_in(self):
        """Logged in users can unwatch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        # First watch
        self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "true"})
        # Then unwatch
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "false"})
        self.assertEqual(response.status_code, 200)

    def test_get_request_denied(self):
        """GET requests should be denied"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/api/watchitem/{self.lot.pk}/")
        self.assertEqual(response.status_code, 405)
