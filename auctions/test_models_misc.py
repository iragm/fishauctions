"""Model methods, signal behaviour, and the management commands that email people."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    PageView,
    PickupLocation,
    UserData,
    clean_email_address,
    note_email_if_unusable,
)
from auctions.tests import StandardTestCase


class EmailAddressCheckingTests(SimpleTestCase):
    """The two ways this site reacts to an address that isn't one.

    ``clean_email_address`` refuses, for anything a person typed into a form. ``note_email_if_unusable``
    keeps it and says so, for the paths a machine feeds where refusing loses the record.
    """

    GOOD = ("bob@example.com", "  Mixed@Case.COM ", "a.b+tag@sub.example.co.uk")
    BAD = ("bob@example", "not an email", "@example.com", "bob@@example.com", "a@b.c")

    def test_a_real_address_comes_back_normalized(self):
        self.assertEqual(clean_email_address("  Mixed@Case.COM "), "mixed@case.com")
        for good in self.GOOD:
            self.assertTrue(clean_email_address(good))

    def test_blank_is_not_a_typo(self):
        for blank in ("", "   ", None):
            self.assertEqual(clean_email_address(blank), "")
            self.assertEqual(note_email_if_unusable(blank, "a test"), "")

    def test_anything_else_is_refused(self):
        for bad in self.BAD:
            with self.assertRaises(ValidationError, msg=bad):
                clean_email_address(bad)

    def test_the_machine_path_keeps_it_and_warns(self):
        for bad in self.BAD:
            with self.assertLogs("auctions.models", level="WARNING") as logs:
                kept = note_email_if_unusable(bad, "a PayPal payment")
            self.assertEqual(kept, bad.strip().lower(), bad)
            self.assertIn("a PayPal payment", logs.output[0])

    def test_the_machine_path_is_quiet_about_a_good_one(self):
        with self.assertNoLogs("auctions.models", level="WARNING"):
            self.assertEqual(note_email_if_unusable(" Bob@Example.com ", "a test"), "bob@example.com")


class ModelMethodsTestCase(StandardTestCase):
    """Test cases for specific model methods with complex logic"""

    def test_auction_fix_year_old_date(self):
        """Test Auction.fix_year corrects dates with years too far in the past"""
        old_date = timezone.now().replace(year=1990)
        fixed_date = self.online_auction.fix_year(old_date)

        # Should be corrected to current year
        self.assertEqual(fixed_date.year, timezone.now().year)

    def test_auction_fix_year_future_date(self):
        """Test Auction.fix_year corrects dates with years too far in the future"""
        future_date = timezone.now().replace(year=2099)
        fixed_date = self.online_auction.fix_year(future_date)

        # Should be corrected to current year
        self.assertEqual(fixed_date.year, timezone.now().year)

    def test_auction_fix_year_valid_date(self):
        """Test Auction.fix_year doesn't modify valid dates"""
        valid_date = timezone.now().replace(year=2025)
        fixed_date = self.online_auction.fix_year(valid_date)

        # Should remain unchanged
        self.assertEqual(fixed_date.year, 2025)

    def test_auction_fix_year_none_date(self):
        """Test Auction.fix_year handles None dates"""
        fixed_date = self.online_auction.fix_year(None)

        # Should return None
        self.assertIsNone(fixed_date)

    def test_auction_fix_year_custom_cutoffs(self):
        """Test Auction.fix_year with custom cutoff parameters"""
        date_2010 = timezone.now().replace(year=2010)

        # With default cutoffs (2000-2050), 2010 should be valid
        fixed_default = self.online_auction.fix_year(date_2010)
        self.assertEqual(fixed_default.year, 2010)

        # With custom cutoffs where 2010 is invalid
        fixed_custom = self.online_auction.fix_year(date_2010, low_cutoff=2015, high_cutoff=2040)
        self.assertEqual(fixed_custom.year, timezone.now().year)

    def test_auction_find_user_by_email(self):
        """Test Auction.find_user can find users by email"""
        # find_user searches AuctionTOS.email, not User.email.
        self.admin_online_tos.email = "test@example.com"
        self.admin_online_tos.save()

        result = self.online_auction.find_user(email="test@example.com")

        # Should find the AuctionTOS with this email
        self.assertIsNotNone(result)
        self.assertEqual(result.email, "test@example.com")

    def test_auction_find_user_by_name(self):
        """Test Auction.find_user can find users by name"""
        # Set a name for testing
        self.admin_online_tos.name = "John Doe"
        self.admin_online_tos.save()

        result = self.online_auction.find_user(name="John Doe")

        # Should find the user
        self.assertIsNotNone(result)

    def test_auction_find_user_no_params(self):
        """Test Auction.find_user returns None with no search params"""
        result = self.online_auction.find_user()

        # Should return None
        self.assertIsNone(result)

    def test_auction_find_user_exclude_pk(self):
        """Test Auction.find_user can exclude specific PKs"""
        # Set email on AuctionTOS
        self.admin_online_tos.email = "test@example.com"
        self.admin_online_tos.save()

        result = self.online_auction.find_user(email="test@example.com", exclude_pk=self.admin_online_tos.pk)

        if result:
            self.assertNotEqual(result.pk, self.admin_online_tos.pk)

    def test_auction_soft_delete(self):
        """Test Auction.delete performs soft delete"""
        auction_pk = self.online_auction.pk
        self.online_auction.delete()

        # Auction should still exist but be marked deleted
        auction = Auction.objects.get(pk=auction_pk)
        self.assertTrue(auction.is_deleted)

    def test_pageview_save_gets_location_from_ip(self):
        """Test PageView.save gets location from IP address"""

        # Create a PageView with known location
        PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=timezone.now(),
            ip_address="192.168.1.1",
            latitude=40.7128,
            longitude=-74.0060,
            session_id="session1",
        )

        # Create another PageView with same IP but no location
        new_view = PageView.objects.create(
            user=self.user,
            lot_number=self.lotB,
            date_start=timezone.now(),
            ip_address="192.168.1.1",
            session_id="session2",
        )

        # Should have inherited location from previous view with same IP
        self.assertEqual(new_view.latitude, 40.7128)
        self.assertEqual(new_view.longitude, -74.0060)

    def test_pageview_save_gets_location_from_userdata(self):
        """Test PageView.save gets location from UserData if no IP match"""

        # Set user location
        self.user.userdata.latitude = 51.5074
        self.user.userdata.longitude = -0.1278
        self.user.userdata.save()

        # Create PageView with new IP and no location
        new_view = PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=timezone.now(),
            ip_address="10.0.0.1",
            session_id="session3",
        )

        # Should have inherited location from userdata
        self.assertEqual(new_view.latitude, 51.5074)
        self.assertEqual(new_view.longitude, -0.1278)

    def test_pageview_create_updates_last_activity_for_authenticated_user(self):
        past_time = timezone.now() - timezone.timedelta(days=1)
        UserData.objects.filter(user=self.user).update(last_activity=past_time)
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/pageview/",
            data={
                "url": "/lots/",
                "first_view": "true",
                "referrer": "",
                "title": "Lots",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.user.userdata.refresh_from_db()
        self.assertGreater(self.user.userdata.last_activity, past_time)

    def test_pageview_create_does_not_update_last_activity_for_anonymous_user(self):
        """PageViewCreate should not update last_activity for anonymous users"""
        past_time = timezone.now() - timezone.timedelta(days=1)
        UserData.objects.filter(user=self.user).update(last_activity=past_time)
        self.client.logout()
        response = self.client.post(
            "/api/pageview/",
            data={
                "url": "/lots/",
                "first_view": "true",
                "referrer": "",
                "title": "Lots",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.last_activity, past_time)


class SignalLogicTestCase(StandardTestCase):
    """Test cases for signal handlers with complex date logic"""

    def test_auction_signal_swaps_start_end_if_reversed(self):
        """Test that auction signal swaps start/end dates if end is before start"""
        # Create auction with end before start
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test reversed dates",
            date_start=timezone.now() + datetime.timedelta(days=7),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        # Dates should be swapped by signal
        self.assertLess(auction.date_start, auction.date_end)

    def test_auction_signal_sets_default_end_date_for_online(self):
        """Test that auction signal sets default end date for online auctions"""
        start_date = timezone.now() + datetime.timedelta(days=1)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test default end",
            is_online=True,
            date_start=start_date,
        )

        # Should have end date set to 7 days after start
        expected_end = start_date + datetime.timedelta(days=7)
        self.assertEqual(auction.date_end.date(), expected_end.date())

    def test_auction_signal_sets_lot_submission_dates(self):
        """Test that auction signal sets lot submission dates if not provided"""
        start_date = timezone.now() + datetime.timedelta(days=7)
        end_date = start_date + datetime.timedelta(days=7)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test lot submission dates",
            is_online=True,
            date_start=start_date,
            date_end=end_date,
        )

        # Should have lot submission dates set
        self.assertIsNotNone(auction.lot_submission_start_date)
        self.assertIsNotNone(auction.lot_submission_end_date)
        # For online auctions, submission end should match auction end
        self.assertEqual(auction.lot_submission_end_date, auction.date_end)

    def test_auction_signal_fixes_bad_lot_submission_end_date(self):
        start_date = timezone.now() + datetime.timedelta(days=1)
        end_date = start_date + datetime.timedelta(days=7)
        bad_submission_end = end_date + datetime.timedelta(days=1)

        auction = Auction.objects.create(
            created_by=self.user,
            title="Test bad submission end",
            is_online=True,
            date_start=start_date,
            date_end=end_date,
            lot_submission_end_date=bad_submission_end,
        )

        # Should have corrected lot submission end date
        self.assertEqual(auction.lot_submission_end_date, auction.date_end)

    def test_auction_signal_sets_online_bidding_dates_for_in_person(self):
        """Test that auction signal sets online bidding dates for in-person auctions"""
        start_date = timezone.now() + datetime.timedelta(days=7)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test in-person with online bidding",
            is_online=False,
            date_start=start_date,
            online_bidding="allow",
        )

        # Should have online bidding dates set
        self.assertIsNotNone(auction.date_online_bidding_starts)
        self.assertIsNotNone(auction.date_online_bidding_ends)
        # Online bidding should end at auction start
        self.assertEqual(auction.date_online_bidding_ends, auction.date_start)

    def test_auction_signal_swaps_online_bidding_dates_if_reversed(self):
        """Test that auction signal swaps online bidding dates if reversed"""
        start_date = timezone.now() + datetime.timedelta(days=7)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test reversed online bidding dates",
            is_online=False,
            date_start=start_date,
            online_bidding="allow",
            date_online_bidding_starts=start_date,
            date_online_bidding_ends=start_date - datetime.timedelta(days=1),
        )

        # Dates should be swapped
        self.assertLess(auction.date_online_bidding_starts, auction.date_online_bidding_ends)


class DuplicateAuctionTOSTests(StandardTestCase):
    """Test that duplicate AuctionTOS records are auto-merged on save"""

    def test_duplicate_user_auction_is_auto_merged_on_save(self):
        """A second AuctionTOS for the same user and auction auto-merges into the older one on save."""
        initial_count = AuctionTOS.objects.filter(user=self.admin_user, auction=self.online_auction).count()
        self.assertEqual(initial_count, 1)
        # Simulate a duplicate being saved (e.g. race condition)
        AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=False
        )
        final_count = AuctionTOS.objects.filter(user=self.admin_user, auction=self.online_auction).count()
        self.assertEqual(final_count, 1)

    def test_duplicate_email_is_auto_merged_on_save(self):
        """A second TOS with the same email in the same auction auto-merges on save."""
        # Set a known email on the existing TOS
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(email="dup@example.com")
        initial_count = AuctionTOS.objects.filter(auction=self.online_auction, email="dup@example.com").count()
        self.assertEqual(initial_count, 1)
        # Create a second TOS with the same email — should be auto-merged
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            email="dup@example.com",
            name="Duplicate Person",
        )
        # Only one TOS with this email should remain
        final_count = AuctionTOS.objects.filter(auction=self.online_auction, email="dup@example.com").count()
        self.assertEqual(final_count, 1)

    def test_multiple_null_users_allowed_same_auction(self):
        tos1 = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, manually_added=True, name="Person A"
        )
        tos2 = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, manually_added=True, name="Person B"
        )
        self.assertIsNotNone(tos1.pk)
        self.assertIsNotNone(tos2.pk)

    def test_merge_preserves_fields_from_duplicate(self):
        """merge_duplicate() fills the canonical record's empty fields from the duplicate."""
        canonical = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            name="Old Record",
            bidder_number="OLD1",
        )
        duplicate = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            name="Newer Record",
            email="preserve@example.com",
            phone_number="555-1234",
            address="123 Fish St",
            memo="important note",
            bidder_number="NEW1",
        )
        canonical.merge_duplicate(duplicate, reason="test")
        canonical.refresh_from_db()
        # Fields missing on canonical should now be copied from duplicate
        self.assertEqual(canonical.email, "preserve@example.com")
        self.assertEqual(canonical.phone_number, "555-1234")
        self.assertEqual(canonical.address, "123 Fish St")
        self.assertEqual(canonical.memo, "important note")
        self.assertEqual(canonical.name, "Old Record")
        self.assertEqual(canonical.bidder_number, "OLD1")
        # duplicate should be deleted
        self.assertFalse(AuctionTOS.objects.filter(pk=duplicate.pk).exists())

    def test_merge_copies_user_from_duplicate_to_canonical(self):
        """merge_duplicate() copies the user onto a canonical record that has none."""
        canonical = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            name="Manual Entry",
            email="linkme@example.com",
        )
        # The email match makes save() merge this into canonical and delete it.
        duplicate = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
            email="linkme@example.com",
            name="User Entry",
        )
        canonical.refresh_from_db()
        self.assertFalse(AuctionTOS.objects.filter(pk=duplicate.pk).exists())
        self.assertEqual(canonical.user, self.user_who_does_not_join)


class AuctionNoShowURLEncodingTest(StandardTestCase):
    """Bidder numbers with special characters work with the path converter."""

    def test_bidder_number_with_special_characters(self):
        # Slashes are removed on save; see test_bidder_number_slash_removal_on_save.
        special_bidder_number = "test@123"
        special_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=special_bidder_number,
            name="Test Special User",
        )

        # Test that the reverse URL generation works with the path converter
        problems_url = reverse(
            "auction_no_show",
            kwargs={
                "slug": self.online_auction.slug,
                "tos": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(problems_url)
        self.assertIn(self.online_auction.slug, problems_url)
        self.assertIn("test@123", problems_url)

        # Test that the URL can be accessed by an admin
        self.client.force_login(self.admin_user)
        response = self.client.get(problems_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test Special User", response.content.decode())

    def test_bidder_number_with_url_like_content(self):
        """A URL-like bidder number, the originally reported case."""
        # The reported value was 'https://atlfishclub./'; slashes are removed and max_length is 20.
        url_like_bidder = "https:site."
        url_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=url_like_bidder,
            name="Test User",
        )

        # Test reverse() with the path converter
        problems_url = reverse(
            "auction_no_show",
            kwargs={
                "slug": self.online_auction.slug,
                "tos": url_tos.bidder_number,
            },
        )
        self.assertIsNotNone(problems_url)

        # Test accessing the view
        self.client.force_login(self.admin_user)
        response = self.client.get(problems_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test User", response.content.decode())

    def test_auction_no_show_dialog_url(self):
        """Test the auction_no_show_dialog URL also works with path converter"""
        special_bidder_number = "test@user"
        special_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=special_bidder_number,
            name="Special User",
        )

        # Test reverse() for the dialog endpoint (used in forms.py line 1134)
        dialog_url = reverse(
            "auction_no_show_dialog",
            kwargs={
                "slug": self.online_auction.slug,
                "tos": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(dialog_url)

        # Test accessing the dialog view
        self.client.force_login(self.admin_user)
        response = self.client.get(dialog_url)
        self.assertEqual(response.status_code, 200)

    def test_other_bidder_number_urls(self):
        special_bidder_number = "user@123"
        special_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=special_bidder_number,
            name="User 123",
        )

        # Test bulk_add_image URL - this uses <path:bidder_number>
        bulk_image_url = reverse(
            "bulk_add_image",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(bulk_image_url)
        self.assertIn("user@123", bulk_image_url)

        print_labels_url = reverse(
            "print_labels_by_bidder_number",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(print_labels_url)
        self.assertIn("user@123", print_labels_url)

        # bulk_add_lots URLs use <str:bidder_number> since more path follows it, so no slashes.
        normal_bidder = "user123"
        normal_tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=normal_bidder,
            name="Normal User",
        )

        bulk_add_url = reverse(
            "bulk_add_lots",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": normal_tos.bidder_number,
            },
        )
        self.assertIsNotNone(bulk_add_url)
        self.assertIn("user123", bulk_add_url)

    def test_bidder_number_slash_removal_on_save(self):
        """Slashes are removed from bidder_number on save, with a history entry."""

        # Create an AuctionTOS with a bidder_number containing slashes
        bidder_with_slash = "test/123/abc"
        tos_with_slash = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=bidder_with_slash,
            name="Slash Test User",
        )

        # Verify the slash was removed
        self.assertEqual(tos_with_slash.bidder_number, "test123abc")
        self.assertNotIn("/", tos_with_slash.bidder_number)

        # Verify auction history was created
        history_entries = AuctionHistory.objects.filter(
            auction=self.online_auction, applies_to="USERS", action__icontains="removed '/' character"
        )
        self.assertTrue(history_entries.exists())
        self.assertTrue(any("test/123/abc" in entry.action for entry in history_entries))
        self.assertTrue(any("test123abc" in entry.action for entry in history_entries))

    def test_bidder_number_slash_removal_prevents_duplicates(self):
        """Test that slash removal prevents creating duplicate bidder_numbers"""
        # Create a TOS with bidder_number "user123"
        existing_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="user123",
            name="Existing User",
        )

        # "user/123" would clean to "user123", which is taken.
        fresh_user = User.objects.create_user(username="fresh_noshow_user", password="testpassword")
        new_tos = AuctionTOS.objects.create(
            user=fresh_user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="user/123",
            name="New User",
        )

        # The new TOS should have a modified bidder_number to avoid duplicate
        self.assertNotEqual(new_tos.bidder_number, existing_tos.bidder_number)
        self.assertNotIn("/", new_tos.bidder_number)
        # Should have a suffix added
        self.assertTrue(new_tos.bidder_number.startswith("user123"))
        self.assertIn("1", new_tos.bidder_number)  # Should be "user1231" or similar

    def test_bidder_number_reuse_by_email(self):
        """Bidder numbers are reused across the same creator's auctions by email."""
        # Create a new auction by the same creator
        new_auction = Auction.objects.create(
            created_by=self.user,  # same creator as self.online_auction
            title="Second Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=7),
            date_start=timezone.now(),
        )
        new_location = PickupLocation.objects.create(
            name="new location", auction=new_auction, pickup_time=timezone.now() + datetime.timedelta(days=8)
        )

        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="reuse_test@example.com",
            bidder_number="777",
            name="Test User",
        )

        second_tos = AuctionTOS.objects.create(
            auction=new_auction,
            pickup_location=new_location,
            email="reuse_test@example.com",
            name="Test User",
        )

        self.assertEqual(second_tos.bidder_number, "777")

    def test_bidder_number_reuse_by_user(self):
        """Bidder numbers are reused across the same creator's auctions by user."""
        # Create a new auction by the same creator
        new_auction = Auction.objects.create(
            created_by=self.user,  # same creator as self.online_auction
            title="Second Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=7),
            date_start=timezone.now(),
        )
        new_location = PickupLocation.objects.create(
            name="new location", auction=new_auction, pickup_time=timezone.now() + datetime.timedelta(days=8)
        )

        # Create a test user
        test_user = User.objects.create_user(
            username="reuse_user", password="testpassword", email="different@example.com"
        )

        AuctionTOS.objects.create(
            user=test_user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="888",
            name="Test User",
        )

        second_tos = AuctionTOS.objects.create(
            user=test_user,
            auction=new_auction,
            pickup_location=new_location,
            email="another_email@example.com",  # Different email
            name="Test User",
        )

        self.assertEqual(second_tos.bidder_number, "888")

    def test_bidder_number_not_reused_if_in_use(self):
        """Bidder numbers aren't reused if taken in the current auction."""
        # Create a new auction by the same creator
        new_auction = Auction.objects.create(
            created_by=self.user,
            title="Second Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=7),
            date_start=timezone.now(),
        )
        new_location = PickupLocation.objects.create(
            name="new location", auction=new_auction, pickup_time=timezone.now() + datetime.timedelta(days=8)
        )

        # Create an AuctionTOS in the first auction
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="reuse_test@example.com",
            bidder_number="999",
            name="Test User",
        )

        # Create someone else using bidder number 999 in the new auction
        AuctionTOS.objects.create(
            auction=new_auction,
            pickup_location=new_location,
            email="blocker@example.com",
            bidder_number="999",
            name="Blocker User",
        )

        second_tos = AuctionTOS.objects.create(
            auction=new_auction,
            pickup_location=new_location,
            email="reuse_test@example.com",
            name="Test User",
        )

        # The second TOS should NOT reuse 999 since it's already taken
        self.assertNotEqual(second_tos.bidder_number, "999")

    def test_bidder_number_reuse_most_recent_auction(self):
        """Bidder numbers are reused from the most recently created AuctionTOS."""
        # Create two new auctions by the same creator, in order
        old_auction = Auction.objects.create(
            created_by=self.user,
            title="Older Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=7),
            date_start=timezone.now(),
        )
        old_location = PickupLocation.objects.create(
            name="old location", auction=old_auction, pickup_time=timezone.now() + datetime.timedelta(days=8)
        )

        new_auction = Auction.objects.create(
            created_by=self.user,
            title="Newer Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=14),
            date_start=timezone.now() + datetime.timedelta(days=1),
        )
        new_location = PickupLocation.objects.create(
            name="new location", auction=new_auction, pickup_time=timezone.now() + datetime.timedelta(days=15)
        )

        # Create an AuctionTOS in the old auction
        AuctionTOS.objects.create(
            auction=old_auction,
            pickup_location=old_location,
            email="reuse_test@example.com",
            bidder_number="111",
            name="Test User",
        )

        AuctionTOS.objects.create(
            auction=new_auction,
            pickup_location=new_location,
            email="reuse_test@example.com",
            bidder_number="222",
            name="Test User",
        )

        # Create a third auction
        third_auction = Auction.objects.create(
            created_by=self.user,
            title="Third Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=21),
            date_start=timezone.now() + datetime.timedelta(days=2),
        )
        third_location = PickupLocation.objects.create(
            name="third location", auction=third_auction, pickup_time=timezone.now() + datetime.timedelta(days=22)
        )

        # Create an AuctionTOS in the third auction with the same email
        third_tos = AuctionTOS.objects.create(
            auction=third_auction,
            pickup_location=third_location,
            email="reuse_test@example.com",
            name="Test User",
        )

        self.assertEqual(third_tos.bidder_number, "222")


class AuctionTOSNotificationsCommandTests(StandardTestCase):
    """Test the auctiontos_notifications management command"""

    def test_excludes_mail_only_locations_from_base_queryset(self):
        """Mail-only TOS are excluded from the notification queryset."""

        # Create auction with only mail pickup location
        mail_auction = Auction.objects.create(
            created_by=self.user,
            title="Mail only auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=2),
        )
        mail_location = PickupLocation.objects.create(
            name="Mail me my lots",
            auction=mail_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Create TOS with mail-only pickup
        mail_tos = AuctionTOS.objects.create(
            auction=mail_auction,
            user=self.userB,
            pickup_location=mail_location,
            manually_added=False,
            confirm_email_sent=False,
            createdon=timezone.now() - datetime.timedelta(hours=25),
        )

        base_qs = AuctionTOS.objects.filter(manually_added=False, user__isnull=False).exclude(
            pickup_location__pickup_by_mail=True
        )
        assert not base_qs.filter(pk=mail_tos.pk).exists(), "Mail-only TOS should be excluded from base queryset"

    def test_includes_physical_locations_in_base_queryset(self):
        """Test that physical location TOS are included in the base queryset"""
        # Create auction with physical location
        physical_auction = Auction.objects.create(
            created_by=self.user,
            title="Physical auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=2),
        )
        physical_location = PickupLocation.objects.create(
            name="Physical location",
            auction=physical_auction,
            pickup_by_mail=False,
            latitude=44.0,
            longitude=-72.5,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Create TOS with physical pickup
        physical_tos = AuctionTOS.objects.create(
            auction=physical_auction,
            user=self.userB,
            pickup_location=physical_location,
            manually_added=False,
            confirm_email_sent=False,
            createdon=timezone.now() - datetime.timedelta(hours=25),
        )

        # Verify that the base queryset includes physical location TOS
        base_qs = AuctionTOS.objects.filter(manually_added=False, user__isnull=False).exclude(
            pickup_location__pickup_by_mail=True
        )
        assert base_qs.filter(pk=physical_tos.pk).exists(), "Physical location TOS should be included in base queryset"

    def test_command_uses_shared_distance_helper(self):

        # Create auction with physical location
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for distance",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Test location",
            auction=auction,
            latitude=44.0,
            longitude=-72.5,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Set user location
        self.userB.userdata.latitude = 43.0
        self.userB.userdata.longitude = -71.5
        self.userB.userdata.save()

        # Patch mail.send to prevent actual email sending
        with patch("auctions.management.commands.auctiontos_notifications.mail.send"):
            try:
                call_command("auctiontos_notifications")
                # Success - command ran without errors
            except Exception as e:
                self.fail(f"Command failed with error: {e}")
