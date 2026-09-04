"""Site-wide configuration: currency, email fields, locations, demo data and defaults."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubMember,
    Invoice,
    Lot,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase


class CurrencyCustomizationTests(StandardTestCase):
    """Tests for currency display customization"""

    def test_userdata_default_currency(self):
        """Test that UserData has a default currency of USD"""
        user = User.objects.create_user(username="test_currency_user", password="testpassword")
        self.assertEqual(user.userdata.preferred_currency, "USD")
        self.assertEqual(user.userdata.currency, "USD")

    def test_userdata_preferred_currency_gbp(self):
        """Test that UserData can be set to GBP"""
        user = User.objects.create_user(username="uk_user", password="testpassword")
        user.userdata.preferred_currency = "GBP"
        user.userdata.save()
        self.assertEqual(user.userdata.currency, "GBP")

    def test_userdata_preferred_currency_cad(self):
        """Test that UserData can be set to CAD"""
        user = User.objects.create_user(username="ca_user", password="testpassword")
        user.userdata.preferred_currency = "CAD"
        user.userdata.save()
        self.assertEqual(user.userdata.currency, "CAD")

    def test_lot_currency_from_auction_creator(self):
        """Test that Lot gets currency from auction creator"""
        # Set auction creator to GBP
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.online_auction,
            quantity=1,
            user=self.user,
        )

        self.assertEqual(lot.currency, "GBP")
        self.assertEqual(lot.currency_symbol, "£")

    def test_lot_currency_from_lot_owner_standalone(self):
        """Test that standalone lot gets currency from owner"""
        # Create a user with CAD preference
        cad_user = User.objects.create_user(username="cad_user", password="testpassword")
        cad_user.userdata.preferred_currency = "CAD"
        cad_user.userdata.save()

        # Create a standalone lot (no auction)
        lot = Lot.objects.create(
            lot_name="Standalone Lot",
            auction=None,
            quantity=1,
            user=cad_user,
        )

        self.assertEqual(lot.currency, "CAD")
        self.assertEqual(lot.currency_symbol, "$")

    def test_auction_currency_from_creator(self):
        """Test that Auction gets currency from creator"""
        # Set auction creator to GBP
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        self.assertEqual(self.online_auction.currency, "GBP")
        self.assertEqual(self.online_auction.currency_symbol, "£")

    def test_invoice_currency_from_auction_creator(self):
        """Test that Invoice gets currency from auction creator"""
        # Set auction creator to CAD
        self.user.userdata.preferred_currency = "CAD"
        self.user.userdata.save()

        invoice = Invoice.objects.create(auctiontos_user=self.online_tos, auction=self.online_auction)

        self.assertEqual(invoice.currency, "CAD")
        self.assertEqual(invoice.currency_symbol, "$")

    def test_currency_symbol_usd(self):
        """Test USD currency symbol"""
        user = User.objects.create_user(username="usd_user", password="testpassword")
        user.userdata.preferred_currency = "USD"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="USD Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "$")

    def test_currency_symbol_gbp(self):
        """Test GBP currency symbol"""
        user = User.objects.create_user(username="gbp_user", password="testpassword")
        user.userdata.preferred_currency = "GBP"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="GBP Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "£")

    def test_lot_label_prices_use_currency_symbol(self):
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        lot = Lot.objects.create(
            lot_name="GBP Label Lot",
            auction=self.online_auction,
            quantity=1,
            user=self.user,
            reserve_price=Decimal("5.00"),
            buy_now_price=Decimal("9.00"),
        )

        self.assertEqual(lot.min_bid_label, "Min: £5.00")
        self.assertEqual(lot.buy_now_label, "Buy: £9.00")

    def test_reserve_and_buy_now_info_uses_currency_symbol(self):
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        lot = Lot.objects.create(
            lot_name="GBP Reserve Label Lot",
            auction=self.online_auction,
            quantity=1,
            user=self.user,
            reserve_price=Decimal("10.00"),
            buy_now_price=Decimal("15.00"),
        )

        self.assertEqual(lot.reserve_and_buy_now_info, " Min bid: £10.00 Buy now: £15.00")

    def test_printed_label_line_3_uses_currency_symbol_for_non_multi_location(self):
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        lot = Lot.objects.create(
            lot_name="GBP Printed Label Lot",
            auction=self.online_auction,
            quantity=1,
            user=self.user,
            reserve_price=Decimal("10.00"),
            buy_now_price=Decimal("15.00"),
        )

        self.assertEqual(lot.label_line_3, " Min bid: £10.00 Buy now: £15.00")

    def test_printed_label_line_2_uses_currency_symbol_for_multi_location(self):
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()
        PickupLocation.objects.create(
            name="Second pickup",
            auction=self.online_auction,
            pickup_time=self.location.pickup_time,
        )

        lot = Lot.objects.create(
            lot_name="GBP Multi-location Printed Label Lot",
            auction=self.online_auction,
            quantity=1,
            user=self.user,
            reserve_price=Decimal("10.00"),
            buy_now_price=Decimal("15.00"),
        )

        self.assertEqual(lot.label_line_2, " Min bid: £10.00 Buy now: £15.00")

    def test_change_user_preferences_form_includes_currency(self):
        """Test that ChangeUserPreferencesForm includes preferred_currency field"""
        from auctions.forms import ChangeUserPreferencesForm

        form = ChangeUserPreferencesForm(user=self.user, instance=self.user.userdata)
        self.assertIn("preferred_currency", form.fields)

    def test_preferences_view_can_change_currency(self):
        """Test that user can change their preferred currency via preferences page"""
        self.client.login(username="my_lot", password="testpassword")

        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Change currency to GBP
        url = reverse("preferences")
        response = self.client.post(
            url,
            {
                "preferred_currency": "GBP",
                "distance_unit": "mi",
                "email_visible": False,
                "username_visible": True,
                "share_lot_images": True,
                "auto_add_images": True,
            },
            follow=True,
        )

        # Check that currency was changed
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.preferred_currency, "GBP")

    def test_currency_symbol_eur(self):
        """Test EUR currency symbol"""
        user = User.objects.create_user(username="eur_user", password="testpassword")
        user.userdata.preferred_currency = "EUR"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="EUR Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "€")

    def test_currency_symbol_jpy(self):
        """Test JPY currency symbol"""
        user = User.objects.create_user(username="jpy_user", password="testpassword")
        user.userdata.preferred_currency = "JPY"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="JPY Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "¥")

    def test_currency_symbol_aud(self):
        """Test AUD currency symbol"""
        user = User.objects.create_user(username="aud_user", password="testpassword")
        user.userdata.preferred_currency = "AUD"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="AUD Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "$")

    def test_currency_symbol_chf(self):
        """Test CHF currency symbol"""
        user = User.objects.create_user(username="chf_user", password="testpassword")
        user.userdata.preferred_currency = "CHF"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="CHF Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "CHF")

    def test_currency_symbol_cny(self):
        """Test CNY currency symbol"""
        user = User.objects.create_user(username="cny_user", password="testpassword")
        user.userdata.preferred_currency = "CNY"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="CNY Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "¥")

    def test_all_currency_choices_available(self):
        """Test that all 8 currencies are available in choices"""
        from auctions.forms import ChangeUserPreferencesForm

        form = ChangeUserPreferencesForm(user=self.user, instance=self.user.userdata)
        currency_choices = [choice[0] for choice in form.fields["preferred_currency"].choices]

        expected_currencies = ["USD", "CAD", "GBP", "EUR", "JPY", "AUD", "CHF", "CNY"]
        for currency in expected_currencies:
            self.assertIn(currency, currency_choices)


class AuctionEmailFieldsTest(StandardTestCase):
    """Tests for the new auction email tracking fields and signal handling."""

    def test_new_online_auction_has_email_due_dates(self):
        """Test that a new online auction has email due dates set correctly."""
        user = User.objects.create_user(username="email_test_user", password="testpassword", email="email@example.com")
        future_end = timezone.now() + datetime.timedelta(days=7)
        future_start = timezone.now() + datetime.timedelta(hours=1)

        auction = Auction.objects.create(
            created_by=user,
            title="Email Test Auction",
            is_online=True,
            date_start=future_start,
            date_end=future_end,
        )

        # Welcome email should be due 24 hours after creation
        self.assertIsNotNone(auction.welcome_email_due)
        self.assertFalse(auction.welcome_email_sent)

        # Invoice email should be due 1 hour after auction end (for online auctions)
        self.assertIsNotNone(auction.invoice_email_due)
        self.assertFalse(auction.invoice_email_sent)

        # Follow-up email should be due 24 hours after auction end (for online auctions)
        self.assertIsNotNone(auction.followup_email_due)
        self.assertFalse(auction.followup_email_sent)

    def test_new_inperson_auction_has_invoice_marked_sent(self):
        """Test that a new in-person auction has invoice email marked as sent."""
        user = User.objects.create_user(
            username="inperson_test_user", password="testpassword", email="inperson@example.com"
        )
        future_start = timezone.now() + datetime.timedelta(hours=1)

        auction = Auction.objects.create(
            created_by=user,
            title="In-Person Test Auction",
            is_online=False,
            date_start=future_start,
        )

        # Invoice email should be marked as sent for in-person auctions
        self.assertTrue(auction.invoice_email_sent)

        # Follow-up email should be due 24 hours after auction start (for in-person auctions)
        self.assertIsNotNone(auction.followup_email_due)
        self.assertFalse(auction.followup_email_sent)

    def test_email_due_dates_updated_when_dates_change(self):
        """Test that email due dates are updated when auction dates change."""
        user = User.objects.create_user(username="date_change_user", password="testpassword", email="date@example.com")
        future_end = timezone.now() + datetime.timedelta(days=7)
        future_start = timezone.now() + datetime.timedelta(hours=1)

        auction = Auction.objects.create(
            created_by=user,
            title="Date Change Test Auction",
            is_online=True,
            date_start=future_start,
            date_end=future_end,
        )

        original_invoice_due = auction.invoice_email_due
        original_followup_due = auction.followup_email_due

        # Change the auction end date
        new_end = timezone.now() + datetime.timedelta(days=14)
        auction.date_end = new_end
        auction.save()

        # Refresh from database
        auction.refresh_from_db()

        # Invoice and follow-up due dates should be updated
        self.assertNotEqual(auction.invoice_email_due, original_invoice_due)
        self.assertNotEqual(auction.followup_email_due, original_followup_due)


class UserLocationUpdateTests(StandardTestCase):
    """Tests for updating user contact info and syncing to recent AuctionTOS records."""

    def setUp(self):
        super().setUp()
        # Create UserData for the user
        self.user_data, _ = UserData.objects.get_or_create(
            user=self.user,
            defaults={
                "phone_number": "555-1234",
                "address": "123 Old Street",
            },
        )
        self.user.first_name = "John"
        self.user.last_name = "Doe"
        self.user.save()

        # Set contact info on the online_tos
        self.online_tos.name = "John Doe"
        self.online_tos.phone_number = "555-1234"
        self.online_tos.address = "123 Old Street"
        self.online_tos.save()

        # Set contact info on the in_person_tos
        self.in_person_tos.name = "John Doe"
        self.in_person_tos.phone_number = "555-1234"
        self.in_person_tos.address = "123 Old Street"
        self.in_person_tos.save()

    def test_recent_auctiontos_updated_on_contact_change(self):
        """When a user updates their contact info, recent AuctionTOS records should be updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Post updated contact info
        response = self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        # Refresh the AuctionTOS records from the database
        self.online_tos.refresh_from_db()
        self.in_person_tos.refresh_from_db()

        # Check that the AuctionTOS records were updated
        self.assertEqual(self.online_tos.name, "Jane Smith")
        self.assertEqual(self.online_tos.phone_number, "555-9999")
        self.assertEqual(self.online_tos.address, "456 New Avenue")

        self.assertEqual(self.in_person_tos.name, "Jane Smith")
        self.assertEqual(self.in_person_tos.phone_number, "555-9999")
        self.assertEqual(self.in_person_tos.address, "456 New Avenue")

    def test_auction_history_created_on_contact_update(self):
        """An AuctionHistory record should be created when contact info is updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Clear existing history
        AuctionHistory.objects.filter(auction=self.online_auction).delete()

        # Post updated contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Check that history was created
        history = AuctionHistory.objects.filter(
            auction=self.online_auction,
            user=self.user,
            applies_to="USERS",
        )
        self.assertTrue(history.exists())
        self.assertIn("Updated contact info", history.first().action)

    def test_old_auctiontos_not_updated(self):
        """AuctionTOS records older than 30 days should not be updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Make the online_tos older than 30 days
        old_date = timezone.now() - datetime.timedelta(days=31)
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(createdon=old_date)
        self.online_tos.refresh_from_db()

        # Post updated contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Refresh from database
        self.online_tos.refresh_from_db()
        self.in_person_tos.refresh_from_db()

        # Old TOS should not be updated
        self.assertEqual(self.online_tos.name, "John Doe")
        self.assertEqual(self.online_tos.phone_number, "555-1234")
        self.assertEqual(self.online_tos.address, "123 Old Street")

        # Recent TOS should be updated
        self.assertEqual(self.in_person_tos.name, "Jane Smith")
        self.assertEqual(self.in_person_tos.phone_number, "555-9999")
        self.assertEqual(self.in_person_tos.address, "456 New Avenue")

    def test_manually_added_auctiontos_not_updated(self):
        """AuctionTOS records that were manually added should not be updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Mark the online_tos as manually added
        self.online_tos.manually_added = True
        self.online_tos.save()

        # Post updated contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Refresh from database
        self.online_tos.refresh_from_db()
        self.in_person_tos.refresh_from_db()

        # Manually added TOS should not be updated
        self.assertEqual(self.online_tos.name, "John Doe")
        self.assertEqual(self.online_tos.phone_number, "555-1234")
        self.assertEqual(self.online_tos.address, "123 Old Street")

        # Non-manually added TOS should be updated
        self.assertEqual(self.in_person_tos.name, "Jane Smith")
        self.assertEqual(self.in_person_tos.phone_number, "555-9999")
        self.assertEqual(self.in_person_tos.address, "456 New Avenue")

    def test_update_message_shown_for_single_auction(self):
        """The form should show a message about updating a single auction."""
        self.client.login(username="my_lot", password="testpassword")

        # Make one TOS old and the other manually added
        old_date = timezone.now() - datetime.timedelta(days=31)
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(createdon=old_date)

        response = self.client.get("/contact_info/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("auctiontos_update_message", response.context)
        # When there's only one auction, it shows the auction name, not "1 auction"
        self.assertIn(str(self.in_person_auction), response.context["auctiontos_update_message"])

    def test_update_message_shown_for_multiple_auctions(self):
        """The form should show a message about updating multiple auctions."""
        self.client.login(username="my_lot", password="testpassword")

        response = self.client.get("/contact_info/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("auctiontos_update_message", response.context)
        self.assertIn("2 auctions", response.context["auctiontos_update_message"])

    def test_no_update_message_when_no_recent_auctiontos(self):
        """No message should be shown when there are no recent AuctionTOS records."""
        self.client.login(username="my_lot", password="testpassword")

        # Make all TOS old
        old_date = timezone.now() - datetime.timedelta(days=31)
        AuctionTOS.objects.filter(user=self.user).update(createdon=old_date)

        response = self.client.get("/contact_info/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("auctiontos_update_message", response.context)

    def test_no_changes_if_info_same(self):
        """If contact info hasn't changed, no history should be created."""
        self.client.login(username="my_lot", password="testpassword")

        # Clear existing history
        AuctionHistory.objects.filter(auction=self.online_auction).delete()

        # Post the same contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "John",
                "last_name": "Doe",
                "phone_number": "555-1234",
                "address": "123 Old Street",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Check that no history was created for the auctions
        history = AuctionHistory.objects.filter(
            auction=self.online_auction,
            applies_to="USERS",
        )
        self.assertEqual(history.count(), 0)


class LoadDemoDataTests(TestCase):
    """Tests for the load_demo_data management command"""

    @override_settings(DEBUG=True, SINGLE_CLUB_MODE=False)
    def test_load_demo_data_with_debug_true(self):
        """Test that demo data loads successfully when DEBUG=True and no auctions exist"""
        from io import StringIO

        from django.core.management import call_command

        # Ensure no auctions exist
        Auction.objects.all().delete()

        # Call the command
        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        # Check output messages
        self.assertIn("Loading demo data because DEBUG=True", output)
        self.assertIn("Demo data loaded successfully!", output)

        # Verify demo data was created
        self.assertTrue(Auction.objects.filter(title__contains="Demo").exists())
        auctions = Auction.objects.filter(title__contains="Demo")
        self.assertEqual(auctions.count(), 3)

        # Verify auction types
        self.assertTrue(auctions.filter(is_online=False).exists())  # In-person auction
        self.assertTrue(auctions.filter(is_online=True).exists())  # Online auctions

        # Verify pickup locations including mail shipping
        mail_locations = PickupLocation.objects.filter(pickup_by_mail=True)
        self.assertGreater(mail_locations.count(), 0)

        # Verify users were created
        self.assertTrue(User.objects.filter(username__contains="demo_").exists())

        # Verify lots were created
        self.assertTrue(Lot.objects.filter(lot_number__gte=90000).exists())

        # Verify some lots have winners (ended auction)
        lots_with_winners = Lot.objects.filter(lot_number__gte=90000, winner__isnull=False)
        self.assertGreater(lots_with_winners.count(), 0)

    @override_settings(DEBUG=True, SINGLE_CLUB_MODE=False)
    def test_load_demo_data_skips_when_auctions_exist(self):
        """Test that demo data is not loaded when auctions already exist"""
        from io import StringIO

        from django.core.management import call_command

        # Create an auction to prevent demo data loading
        existing_auction = Auction.objects.create(
            title="Existing Auction",
            created_by=None,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        # Call the command
        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        # Check output messages
        self.assertIn("Skipping demo data load", output)
        self.assertIn("auction(s) already exist", output)

        # Verify no demo auctions were created
        demo_auctions = Auction.objects.filter(title__contains="Demo")
        self.assertEqual(demo_auctions.count(), 0)

        # Verify original auction still exists
        self.assertTrue(Auction.objects.filter(pk=existing_auction.pk).exists())

    @override_settings(DEBUG=False)
    def test_load_demo_data_skips_when_debug_false(self):
        """Test that demo data is not loaded when DEBUG=False"""
        from io import StringIO

        from django.core.management import call_command

        # Ensure no auctions exist
        Auction.objects.all().delete()

        # Call the command
        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        # Check output messages
        self.assertIn("Skipping demo data load - DEBUG=False", output)
        self.assertIn("production mode", output)

        # Verify no auctions were created
        self.assertEqual(Auction.objects.count(), 0)

    @override_settings(DEBUG=True, SINGLE_CLUB_MODE=True)
    def test_load_demo_data_skips_when_single_club_mode_enabled(self):
        from io import StringIO

        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        self.assertIn("Skipping demo data load - SINGLE_CLUB_MODE is enabled", output)
        self.assertEqual(Auction.objects.count(), 0)


class EnsureSiteDefaultsCommandTests(TestCase):
    @override_settings(DEBUG=True, SINGLE_CLUB_MODE=True, NAVBAR_BRAND="Command Club")
    def test_command_creates_single_club_and_memberships(self):
        user = User.objects.create_user("commanduser", "command@example.com", "pw")
        auction = Auction.objects.create(
            title="Needs Club",
            created_by=user,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        call_command("ensure_site_defaults")

        club = Club.objects.get(name="Command Club")
        self.assertTrue(ClubMember.objects.filter(club=club, user=user, is_deleted=False).exists())
        auction.refresh_from_db()
        self.assertEqual(auction.club, club)
        self.assertEqual(auction.manage_users_through_club, "all")


class AdminReadonlyFieldsTests(StandardTestCase):
    """Test that admin readonly fields are properly configured"""

    def test_auction_admin_readonly_fields(self):
        """Test that AuctionAdmin has created_by as readonly"""
        from auctions.admin import AuctionAdmin

        admin_instance = AuctionAdmin(Auction, None)
        self.assertIn("created_by", admin_instance.readonly_fields)

    def test_auctiontos_admin_readonly_fields(self):
        """Test that AuctionTOSAdmin has user, auction, and pickup_location as readonly"""
        from auctions.admin import AuctionTOSAdmin

        admin_instance = AuctionTOSAdmin(AuctionTOS, None)
        self.assertIn("user", admin_instance.readonly_fields)
        self.assertIn("auction", admin_instance.readonly_fields)
        self.assertIn("pickup_location", admin_instance.readonly_fields)
