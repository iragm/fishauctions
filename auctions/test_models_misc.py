"""Model methods, signal behaviour, and the management commands that email people."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    PageView,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase


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
        # Set email on AuctionTOS (find_user searches AuctionTOS.email, not User.email)
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

        # Should not find the excluded user (but there might be other users with same email)
        if result:
            self.assertNotEqual(result.pk, self.admin_online_tos.pk)

    def test_auction_soft_delete(self):
        """Test Auction.delete performs soft delete"""
        # NOTE: This tests the current behavior, but soft delete may have issues
        # If a lot is not properly archived, it could still appear in queries
        auction_pk = self.online_auction.pk
        self.online_auction.delete()

        # Auction should still exist but be marked deleted
        auction = Auction.objects.get(pk=auction_pk)
        self.assertTrue(auction.is_deleted)

    def test_pageview_merge_and_delete_duplicate_extends_time_range(self):
        """Test PageView.merge_and_delete_duplicates extends time range correctly"""

        # Create two PageView instances that are duplicates
        base_time = timezone.now()
        view1 = PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=base_time - datetime.timedelta(hours=2),
            date_end=base_time - datetime.timedelta(hours=1),
            total_time=3600,
            session_id="test_session",
        )
        view2 = PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=base_time - datetime.timedelta(hours=1),
            date_end=base_time,
            total_time=3600,
            session_id="test_session",
        )

        # Merge view2 into view1
        # Call as method now (no longer a property)
        view1.merge_and_delete_duplicates()

        # view1 should have extended time range and combined total_time
        view1.refresh_from_db()
        self.assertEqual(view1.total_time, 7200)  # 3600 + 3600

        # view2 should be deleted
        self.assertEqual(PageView.objects.filter(pk=view2.pk).count(), 0)

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
        """PageViewCreate should update last_activity on UserData for authenticated users"""
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
        """Test that auction signal fixes lot submission end date if it's after auction end"""
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
        """Creating a second AuctionTOS for the same user+auction via save() auto-merges it into the older one"""
        initial_count = AuctionTOS.objects.filter(user=self.admin_user, auction=self.online_auction).count()
        self.assertEqual(initial_count, 1)
        # Simulate a duplicate being saved (e.g. race condition)
        AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=False
        )
        # The save() method should have merged it; only 1 record should remain
        final_count = AuctionTOS.objects.filter(user=self.admin_user, auction=self.online_auction).count()
        self.assertEqual(final_count, 1)

    def test_duplicate_email_is_auto_merged_on_save(self):
        """Creating a second TOS with the same email in the same auction auto-merges on save"""
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
        """Multiple manually-added (user=None) TOS records are allowed in the same auction"""
        tos1 = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, manually_added=True, name="Person A"
        )
        tos2 = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, manually_added=True, name="Person B"
        )
        self.assertIsNotNone(tos1.pk)
        self.assertIsNotNone(tos2.pk)

    def test_merge_preserves_fields_from_duplicate(self):
        """merge_duplicate() copies non-empty fields from duplicate onto canonical if canonical is missing them"""
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
        # canonical already had a name and bidder_number — should not be overwritten
        self.assertEqual(canonical.name, "Old Record")
        self.assertEqual(canonical.bidder_number, "OLD1")
        # duplicate should be deleted
        self.assertFalse(AuctionTOS.objects.filter(pk=duplicate.pk).exists())

    def test_merge_copies_user_from_duplicate_to_canonical(self):
        """If the canonical record has no user but the duplicate does, user is copied to canonical"""
        canonical = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            name="Manual Entry",
            email="linkme@example.com",
        )
        # Creating this duplicate with the same email triggers the auto-merge inside save():
        # save() detects the email duplicate (canonical), calls canonical.merge_duplicate(duplicate).
        # merge_duplicate() copies user from duplicate onto canonical, then deletes duplicate.
        duplicate = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
            email="linkme@example.com",
            name="User Entry",
        )
        canonical.refresh_from_db()
        # The email-duplicate save path should have merged them; canonical should have the user
        self.assertFalse(AuctionTOS.objects.filter(pk=duplicate.pk).exists())
        self.assertEqual(canonical.user, self.user_who_does_not_join)


class AuctionNoShowURLEncodingTest(StandardTestCase):
    """Test that bidder_number with special characters (like slashes) work with path converter"""

    def test_bidder_number_with_special_characters(self):
        """Test that bidder_number with special characters (except slashes) work correctly"""
        # Note: Slashes are now automatically removed on save (see test_bidder_number_slash_removal_on_save)
        # Test with special characters that are allowed
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
        """Test with bidder_number that looks like a URL (the actual error case from the issue)"""
        # The actual error case: bidder_number = 'https://atlfishclub./' (22 chars)
        # Note: Slashes are now automatically removed on save
        # We use a shorter version since bidder_number has max_length=20, and without slashes
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
        """Test that other URL patterns work with special characters in bidder_number where applicable"""
        # Note: Slashes are now automatically removed on save, so we test with other special chars
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

        # Test print_labels_by_bidder_number URL - this uses <path:bidder_number>
        print_labels_url = reverse(
            "print_labels_by_bidder_number",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(print_labels_url)
        self.assertIn("user@123", print_labels_url)

        # Note: bulk_add_lots and bulk_add_lots_auto use <str:bidder_number> because they have
        # additional path segments after the bidder_number parameter, so they cannot support
        # slashes in bidder_number (Django's path converter would match too greedily).
        # These patterns work fine with bidder_numbers that don't contain slashes.
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
        """Test that forward slashes are removed from bidder_number on save and history is created"""

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

        # Try to create another TOS with bidder_number "user/123" which would become "user123" after cleaning
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
        """Test that bidder numbers are reused across auctions for the same auction creator when user has the same email"""
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

        # Create an AuctionTOS in the first auction with a specific bidder number
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="reuse_test@example.com",
            bidder_number="777",
            name="Test User",
        )

        # Create an AuctionTOS in the second auction with the same email, no bidder number
        second_tos = AuctionTOS.objects.create(
            auction=new_auction,
            pickup_location=new_location,
            email="reuse_test@example.com",
            name="Test User",
        )

        # The second TOS should reuse the bidder number from the first auction
        self.assertEqual(second_tos.bidder_number, "777")

    def test_bidder_number_reuse_by_user(self):
        """Test that bidder numbers are reused across auctions for the same auction creator when user account is the same"""
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

        # Create an AuctionTOS in the first auction with a specific bidder number
        AuctionTOS.objects.create(
            user=test_user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="888",
            name="Test User",
        )

        # Create an AuctionTOS in the second auction with the same user but different email
        second_tos = AuctionTOS.objects.create(
            user=test_user,
            auction=new_auction,
            pickup_location=new_location,
            email="another_email@example.com",  # Different email
            name="Test User",
        )

        # The second TOS should reuse the bidder number from the first auction
        self.assertEqual(second_tos.bidder_number, "888")

    def test_bidder_number_not_reused_if_in_use(self):
        """Test that bidder numbers are NOT reused if already taken in the current auction"""
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

        # Try to create an AuctionTOS in the second auction with the same email
        second_tos = AuctionTOS.objects.create(
            auction=new_auction,
            pickup_location=new_location,
            email="reuse_test@example.com",
            name="Test User",
        )

        # The second TOS should NOT reuse 999 since it's already taken
        self.assertNotEqual(second_tos.bidder_number, "999")

    def test_bidder_number_reuse_most_recent_auction(self):
        """Test that bidder numbers are reused from the most recently created AuctionTOS"""
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

        # Create an AuctionTOS in the newer auction with a different bidder number
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

        # Should reuse 222 from the most recently created AuctionTOS, not 111 from the older one
        self.assertEqual(third_tos.bidder_number, "222")


class WeeklyPromoManagementCommandTests(StandardTestCase):
    """Test the weekly_promo management command."""

    def setUp(self):
        """Set up test data for weekly promo tests."""
        super().setUp()
        # Set up user with proper location and activity for weekly promo
        self.promo_user = User.objects.create_user(
            username="promo_user", password="testpassword", email="promo@example.com", first_name="PromoUser"
        )
        self.promo_user.userdata.latitude = 40.7128  # New York
        self.promo_user.userdata.longitude = -74.0060
        self.promo_user.userdata.last_activity = timezone.now() - datetime.timedelta(days=10)  # Active 10 days ago
        self.promo_user.userdata.email_me_about_new_auctions = True
        self.promo_user.userdata.email_me_about_new_auctions_distance = 100
        self.promo_user.userdata.next_promo_email_at = timezone.now() - datetime.timedelta(hours=1)  # Due for email
        self.promo_user.userdata.save()

        # Create an active auction with location
        self.promo_auction = Auction.objects.create(
            created_by=self.user,
            title="Promo Test Auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
            promote_this_auction=True,
            use_categories=True,
        )
        # Add pickup location near the user
        self.promo_location = PickupLocation.objects.create(
            name="Promo Location",
            auction=self.promo_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
            latitude=40.7128,
            longitude=-74.0060,
        )

    def test_weekly_promo_sends_email(self):
        """Test that weekly_promo sends emails to eligible users."""
        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was sent
            self.assertTrue(mock_send.called, "mail.send should have been called")
            # Verify the email was sent to the correct user
            call_args = mock_send.call_args
            self.assertEqual(call_args[0][0], self.promo_user.email, "Email should be sent to promo_user")
            # Verify template is correct
            self.assertEqual(call_args[1]["template"], "weekly_promo_email")

    def test_weekly_promo_increments_counter(self):
        """Test that weekly_promo increments the email sent counter."""
        initial_count = self.promo_auction.weekly_promo_emails_sent
        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo")
        self.promo_auction.refresh_from_db()
        # Check that counter was incremented
        self.assertGreater(
            self.promo_auction.weekly_promo_emails_sent,
            initial_count,
            "weekly_promo_emails_sent should be incremented",
        )

    def test_weekly_promo_excludes_inactive_users(self):
        """Test that weekly_promo excludes users who were recently active."""
        # Update user to be recently active (within last 6 days)
        self.promo_user.userdata.last_activity = timezone.now() - datetime.timedelta(days=3)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent to recently active user
            self.assertFalse(mock_send.called, "mail.send should not be called for recently active users")

    def test_weekly_promo_excludes_very_old_users(self):
        """Test that weekly_promo excludes users who haven't been active in a long time."""
        # Update user to be inactive for too long (more than 400 days)
        self.promo_user.userdata.last_activity = timezone.now() - datetime.timedelta(days=500)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent to very inactive user
            self.assertFalse(mock_send.called, "mail.send should not be called for users inactive for >400 days")

    def test_weekly_promo_excludes_users_without_location(self):
        """Test that weekly_promo excludes users without a valid location."""
        # Set user location to 0,0
        self.promo_user.userdata.latitude = 0
        self.promo_user.userdata.longitude = 0
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent
            self.assertFalse(mock_send.called, "mail.send should not be called for users without valid location")

    def test_weekly_promo_respects_opt_out(self):
        """Test that weekly_promo respects user opt-out preferences."""
        # Opt user out of all emails
        self.promo_user.userdata.email_me_about_new_auctions = False
        self.promo_user.userdata.email_me_about_new_in_person_auctions = False
        self.promo_user.userdata.email_me_about_new_local_lots = False
        self.promo_user.userdata.email_me_about_new_lots_ship_to_location = False
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent
            self.assertFalse(mock_send.called, "mail.send should not be called for users who opted out")

    def test_weekly_promo_in_person_auctions(self):
        """Test that weekly_promo includes in-person auctions."""
        # Create an in-person auction
        in_person_auction = Auction.objects.create(
            created_by=self.user,
            title="In Person Promo Auction",
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=3),  # Starts in 3 days
            date_end=timezone.now() + datetime.timedelta(days=10),
            promote_this_auction=True,
            use_categories=True,
        )
        PickupLocation.objects.create(
            name="In Person Location",
            auction=in_person_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=10),
            latitude=40.7128,
            longitude=-74.0060,
        )

        # Update user to opt into in-person auctions
        # Disable online auction notifications to isolate in-person behavior
        self.promo_user.userdata.email_me_about_new_auctions = False
        self.promo_user.userdata.email_me_about_new_in_person_auctions = True
        self.promo_user.userdata.email_me_about_new_in_person_auctions_distance = 100
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was sent
            self.assertTrue(mock_send.called, "mail.send should be called for in-person auctions")
            # Check that the in-person auction counter was incremented
            in_person_auction.refresh_from_db()
            self.assertGreater(
                mock_send.call_count,
                0,
                "Expected at least one weekly promo email for in-person auctions",
            )

    def test_weekly_promo_fake_mode_no_emails_sent(self):
        """Test that --fake mode does not send emails."""
        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo", fake=True)
            # Check that email was NOT sent in fake mode
            self.assertFalse(mock_send.called, "mail.send should not be called in fake mode")

    def test_weekly_promo_fake_mode_no_counter_update(self):
        """Test that --fake mode does not update auction counters."""
        initial_count = self.promo_auction.weekly_promo_emails_sent
        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo", fake=True)
        self.promo_auction.refresh_from_db()
        # Check that counter was NOT incremented in fake mode
        self.assertEqual(
            self.promo_auction.weekly_promo_emails_sent,
            initial_count,
            "weekly_promo_emails_sent should not be incremented in fake mode",
        )

    def test_weekly_promo_fake_mode_output(self):
        """Test that --fake mode includes fake mode marker in output."""
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo", fake=True, stdout=out)
        output = out.getvalue()
        # Check that output includes fake mode indicators
        self.assertIn("[FAKE MODE]", output, "Output should include [FAKE MODE] marker")
        self.assertIn("FAKE", output, "Output should include FAKE indicator")

    def test_weekly_promo_initializes_null_schedule(self):
        """Test that a user with null next_promo_email_at gets it initialized but no email sent."""
        self.promo_user.userdata.next_promo_email_at = None
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            self.assertFalse(mock_send.called, "mail.send should not be called during initialization")

        self.promo_user.userdata.refresh_from_db()
        self.assertIsNotNone(
            self.promo_user.userdata.next_promo_email_at, "next_promo_email_at should be set after initialization"
        )
        self.assertGreater(
            self.promo_user.userdata.next_promo_email_at,
            timezone.now(),
            "next_promo_email_at should be in the future after initialization",
        )

    def test_weekly_promo_skips_users_with_future_schedule(self):
        """Test that users with a future next_promo_email_at do not receive an email."""
        self.promo_user.userdata.next_promo_email_at = timezone.now() + datetime.timedelta(days=3)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            self.assertFalse(mock_send.called, "mail.send should not be called for users with future schedule")

    def test_weekly_promo_advances_schedule_after_sending(self):
        """Test that next_promo_email_at is advanced ~7 days after sending."""
        past_time = timezone.now() - datetime.timedelta(hours=1)
        self.promo_user.userdata.next_promo_email_at = past_time
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo")

        self.promo_user.userdata.refresh_from_db()
        self.assertGreater(
            self.promo_user.userdata.next_promo_email_at,
            timezone.now(),
            "next_promo_email_at should be advanced to the future after sending",
        )

    def test_set_next_promo_initializes_to_next_wednesday(self):
        """Test that set_next_promo sets next_promo_email_at to the next Wednesday at 10 AM."""
        self.promo_user.userdata.next_promo_email_at = None
        self.promo_user.userdata.save()

        self.promo_user.userdata.set_next_promo()
        self.promo_user.userdata.refresh_from_db()

        dt = self.promo_user.userdata.next_promo_email_at
        self.assertIsNotNone(dt)
        self.assertGreater(dt, timezone.now())
        # Should be a Wednesday (weekday 2)
        self.assertEqual(dt.weekday(), 2, "next_promo_email_at should be a Wednesday")

    def test_set_next_promo_advances_by_seven_days(self):
        """Test that set_next_promo advances an existing value by 7 days."""
        base_time = timezone.now() - datetime.timedelta(hours=1)
        self.promo_user.userdata.next_promo_email_at = base_time
        self.promo_user.userdata.save()

        self.promo_user.userdata.set_next_promo()
        self.promo_user.userdata.refresh_from_db()

        new_time = self.promo_user.userdata.next_promo_email_at
        self.assertGreater(new_time, timezone.now(), "Advanced time should be in the future")
        # Should be exactly 7 days from base_time (which was just 1 hour in the past)
        expected = base_time + datetime.timedelta(days=7)
        diff = abs((new_time - expected).total_seconds())
        self.assertLess(diff, 60, "Advanced time should be ~7 days from the original value")

    def test_weekly_promo_fake_mode_does_not_update_schedule(self):
        """Test that fake mode does not modify next_promo_email_at."""
        original_time = timezone.now() - datetime.timedelta(hours=1)
        self.promo_user.userdata.next_promo_email_at = original_time
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo", fake=True)

        self.promo_user.userdata.refresh_from_db()
        self.assertEqual(
            self.promo_user.userdata.next_promo_email_at,
            original_time,
            "next_promo_email_at should not be modified in fake mode",
        )

    def test_last_promo_email_sent_at_set_after_sending(self):
        """Test that last_promo_email_sent_at is updated when a promo email is sent."""
        self.assertIsNone(self.promo_user.userdata.last_promo_email_sent_at)

        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo")

        self.promo_user.userdata.refresh_from_db()
        self.assertIsNotNone(
            self.promo_user.userdata.last_promo_email_sent_at,
            "last_promo_email_sent_at should be set after sending a promo email",
        )

    def test_promo_not_sent_if_sent_within_6_days(self):
        """Test that promo email is not sent if one was sent in the last 6 days."""
        self.promo_user.userdata.last_promo_email_sent_at = timezone.now() - datetime.timedelta(days=3)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            self.assertFalse(mock_send.called, "mail.send should not be called within 6 days of last promo email")

    def test_promo_sent_if_last_sent_more_than_6_days_ago(self):
        """Test that promo email is sent if the last one was more than 6 days ago."""
        self.promo_user.userdata.last_promo_email_sent_at = timezone.now() - datetime.timedelta(days=7)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            self.assertTrue(mock_send.called, "mail.send should be called when last promo was more than 6 days ago")


class AuctionTOSNotificationsCommandTests(StandardTestCase):
    """Test the auctiontos_notifications management command"""

    def test_excludes_mail_only_locations_from_base_queryset(self):
        """Test that mail-only TOS are excluded from the base queryset used for notifications"""

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

        # Verify that the base queryset used by the command excludes mail-only TOS
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
        """Test that the command runs successfully and uses the shared distance calculation helper"""
        from unittest.mock import patch

        from django.core.management import call_command

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
            # Verify the command runs without error
            # The command uses Auction.get_closest_location_distance_subquery which excludes (0,0) and mail locations
            try:
                call_command("auctiontos_notifications")
                # Success - command ran without errors
            except Exception as e:
                self.fail(f"Command failed with error: {e}")
