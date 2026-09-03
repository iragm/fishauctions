"""The utility layer -- helper functions, model utilities, template tags, context processors."""

import datetime
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    AuctionIgnore,
    AuctionTOS,
    Club,
    Lot,
    add_price_info,
)
from auctions.tests import StandardTestCase


class HelperFunctionsTestCase(StandardTestCase):
    """Test cases for helper_functions.py"""

    def test_get_currency_symbol_all_supported_currencies(self):
        """Test that all documented currency codes return correct symbols"""
        from auctions.helper_functions import get_currency_symbol

        # Test all documented currencies
        self.assertEqual(get_currency_symbol("USD"), "$")
        self.assertEqual(get_currency_symbol("CAD"), "$")
        self.assertEqual(get_currency_symbol("AUD"), "$")
        self.assertEqual(get_currency_symbol("GBP"), "£")
        self.assertEqual(get_currency_symbol("EUR"), "€")
        self.assertEqual(get_currency_symbol("JPY"), "¥")
        self.assertEqual(get_currency_symbol("CNY"), "¥")
        self.assertEqual(get_currency_symbol("CHF"), "CHF")

    def test_get_currency_symbol_unsupported_currency(self):
        """Test that unsupported currencies default to $"""
        from auctions.helper_functions import get_currency_symbol

        self.assertEqual(get_currency_symbol("XXX"), "$")
        self.assertEqual(get_currency_symbol(""), "$")
        self.assertEqual(get_currency_symbol("INVALID"), "$")

    def test_get_currency_symbol_case_sensitivity(self):
        """Test currency code case sensitivity - should be case sensitive"""
        from auctions.helper_functions import get_currency_symbol

        # Currency codes should be uppercase
        self.assertEqual(get_currency_symbol("usd"), "$")  # Will default to $ as lowercase not in map
        self.assertEqual(get_currency_symbol("Usd"), "$")  # Will default to $ as mixed case not in map

    def test_bin_data_with_datetime_values(self):
        """Test bin_data with datetime field values"""
        from auctions.helper_functions import bin_data

        # Create test lots with different dates
        base_time = timezone.now() - datetime.timedelta(days=10)
        for i in range(10):
            lot = Lot.objects.create(
                lot_name=f"Test lot datetime {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                active=False,
            )
            lot.date_posted = base_time + datetime.timedelta(days=i)
            lot.save()

        qs = Lot.objects.filter(auction=self.online_auction, lot_name__startswith="Test lot datetime")
        result = bin_data(qs, "date_posted", 5)
        self.assertEqual(len(result), 5)

    def test_bin_data_empty_queryset(self):
        """Test bin_data with empty queryset returns empty bins"""
        from auctions.helper_functions import bin_data

        qs = Lot.objects.filter(lot_name="NONEXISTENT")
        result = bin_data(qs, "winning_price", 5, start_bin=0, end_bin=100)
        # Should return 5 bins all with 0 count
        self.assertEqual(len(result), 5)
        self.assertEqual(sum(result), 0)

    def test_bin_data_invalid_field_raises_error(self):
        """Test bin_data with invalid field raises appropriate error"""
        from auctions.helper_functions import bin_data

        qs = Lot.objects.filter(auction=self.online_auction)
        # Should raise ValueError when field doesn't exist and can't be ordered
        with self.assertRaises(ValueError) as context:
            bin_data(qs, "nonexistent_field", 5)
        self.assertIn("start_bin and end_bin are required", str(context.exception))

    def test_bin_data_string_field_raises_error(self):
        """Test bin_data with string field raises appropriate error"""
        from auctions.helper_functions import bin_data

        # Create a lot with a string field
        Lot.objects.create(
            lot_name="String test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            active=False,
        )

        qs = Lot.objects.filter(lot_name="String test")
        # Should raise ValueError when field is not datetime or numeric
        with self.assertRaises(ValueError) as context:
            bin_data(qs, "lot_name", 5)
        self.assertIn("needs to be either a datetime or an integer value", str(context.exception))

    def test_bin_data_single_bin(self):
        """Test bin_data with single bin works correctly"""
        from auctions.helper_functions import bin_data

        # Create test lots
        for i in range(5):
            Lot.objects.create(
                lot_name=f"Single bin test {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=i * 10,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Single bin test")
        result = bin_data(qs, "winning_price", 1)
        self.assertEqual(len(result), 1)
        # Note: Due to the >= comparison in bin_data, the max value (40) is excluded
        # and goes to high_overflow. So only 4 items (0,10,20,30) are in the bin.
        self.assertEqual(result[0], 4)

    def test_bin_data_zero_range(self):
        """Test bin_data behavior when start_bin equals end_bin"""
        from auctions.helper_functions import bin_data

        # Create test lots with same value
        for i in range(5):
            Lot.objects.create(
                lot_name=f"Zero range test {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=50,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Zero range test")
        # When start equals end, bin_size will be 0, which should now raise ValueError
        with self.assertRaises(ValueError) as context:
            bin_data(qs, "winning_price", 5, start_bin=50, end_bin=50)
        self.assertIn("zero bin size", str(context.exception))


class ModelUtilityFunctionsTestCase(StandardTestCase):
    """Test cases for utility functions in models.py"""

    def test_median_value_odd_count(self):
        """Test median_value with odd number of items"""
        from auctions.models import median_value

        # Create test lots with different prices
        for i in range(5):
            Lot.objects.create(
                lot_name=f"Median test odd {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=i * 10,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Median test odd")
        result = median_value(qs, "winning_price")
        # With values 0, 10, 20, 30, 40, median should be 20
        self.assertEqual(result, 20)

    def test_median_value_even_count(self):
        """Test median_value with even number of items"""
        from auctions.models import median_value

        # Create test lots with different prices
        for i in range(6):
            Lot.objects.create(
                lot_name=f"Median test even {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=i * 10,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Median test even")
        result = median_value(qs, "winning_price")
        # With values 0, 10, 20, 30, 40, 50, the median is the mean of the two middle values (20, 30)
        self.assertEqual(result, 25)

    def test_add_price_info_requires_lot_queryset(self):
        """Test that add_price_info only accepts Lot querysets"""

        # Should raise TypeError when not passed a Lot queryset
        with self.assertRaises(TypeError) as context:
            add_price_info(AuctionTOS.objects.all())
        self.assertIn("must be passed a queryset of the Lot model", str(context.exception))

    def test_add_price_info_sold_lot_calculations(self):
        """Test add_price_info calculates correctly for sold lots"""

        # Create a sold lot
        lot = Lot.objects.create(
            lot_name="Sold lot test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=100,
            auctiontos_winner=self.tosB,
            active=False,
        )

        qs = add_price_info(Lot.objects.filter(pk=lot.pk))
        annotated_lot = qs.first()

        # Should have the annotated fields
        self.assertTrue(hasattr(annotated_lot, "your_cut"))
        self.assertTrue(hasattr(annotated_lot, "club_cut"))
        self.assertTrue(hasattr(annotated_lot, "pre_register_discount"))

    def test_add_price_info_unsold_lot(self):
        """Test add_price_info for unsold lots"""

        # Create an unsold lot
        lot = Lot.objects.create(
            lot_name="Unsold lot test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=None,
            active=False,
        )

        qs = add_price_info(Lot.objects.filter(pk=lot.pk))
        annotated_lot = qs.first()

        # Unsold lots should have negative your_cut (unsold lot fee)
        self.assertLessEqual(annotated_lot.your_cut, 0)
        self.assertEqual(annotated_lot.club_cut, 0)

    def test_auction_save_removes_disallowed_summernote_content(self):
        """Auction Summernote HTML should strip images and scripts before saving."""
        self.online_auction.summernote_description = (
            '<p onclick="alert(1)" onmouseover="alert(1)">Rules</p>'
            '<script>alert(1)</script><img src="/bad.png"><a href="javascript:alert(1)">Link</a>'
            '<a href="https://example.com">Safe</a>'
        )
        self.online_auction.save()
        self.online_auction.refresh_from_db()

        self.assertEqual(
            self.online_auction.summernote_description,
            '<p>Rules</p><a>Link</a><a href="https://example.com">Safe</a>',
        )

    def test_lot_save_removes_disallowed_summernote_content(self):
        """Lot Summernote HTML should strip images and scripts before saving."""
        self.lot.summernote_description = '<p>Fish</p><img src="https://example.com/fish.png"><script>bad()</script>'
        self.lot.save()
        self.lot.refresh_from_db()

        self.assertEqual(self.lot.summernote_description, "<p>Fish</p>")

    def test_club_save_removes_disallowed_summernote_content(self):
        """Club Summernote HTML should strip images and scripts before saving."""
        club = Club.objects.create(
            name="Sanitized Club",
            description='<p>About us</p><img src="/logo.png"><script>alert(1)</script>',
        )
        club.refresh_from_db()

        self.assertEqual(club.description, "<p>About us</p>")

    def test_club_save_allows_empty_description(self):
        """Club save should keep empty Summernote content empty."""
        club = Club.objects.create(name="Empty Club", description="")

        self.assertEqual(club.description, "")

    def test_add_price_info_donation_lot(self):
        """Test add_price_info for donation lots"""

        # Create a donation lot
        lot = Lot.objects.create(
            lot_name="Donation lot test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=100,
            auctiontos_winner=self.tosB,
            donation=True,
            active=False,
        )

        qs = add_price_info(Lot.objects.filter(pk=lot.pk))
        annotated_lot = qs.first()

        # Donation lots should have 0 your_cut
        self.assertEqual(annotated_lot.your_cut, 0)

    def test_distance_to_sql_injection_protection(self):
        """Test distance_to function rejects SQL injection attempts"""
        from auctions.models import distance_to

        # Test with quotes in parameters - should raise TypeError
        with self.assertRaises(TypeError) as context:
            distance_to("45.5'", 90.0)
        self.assertIn("invalid character", str(context.exception))

        with self.assertRaises(TypeError) as context:
            distance_to(45.5, '90.0"')
        self.assertIn("invalid character", str(context.exception))

        with self.assertRaises(TypeError) as context:
            distance_to(45.5, 90.0, lat_field_name="latitude'; DROP TABLE--")
        self.assertIn("invalid character", str(context.exception))

    def test_distance_to_miles_vs_km(self):
        """Test distance_to returns correct SQL for miles vs kilometers"""
        from auctions.models import distance_to

        # Test miles (default)
        distance_miles = distance_to(40.7128, -74.0060)
        self.assertIsNotNone(distance_miles)

        # Test kilometers
        distance_km = distance_to(40.7128, -74.0060, unit="km")
        self.assertIsNotNone(distance_km)

    def test_distance_to_allows_qualified_sql_field_names(self):
        """Backtick-qualified table.column identifiers are valid for raw SQL annotation."""
        from auctions.models import distance_to

        distance = distance_to(
            40.7128,
            -74.0060,
            lat_field_name="`auctions_lot`.`latitude`",
            lng_field_name="`auctions_lot`.`longitude`",
        )
        self.assertIsNotNone(distance)

    def test_find_image_with_user(self):
        """Test find_image prioritizes images from specific user"""
        from auctions.models import find_image

        # find_image requires images to be uploaded, which is restricted in tests
        # This test validates the function exists and handles basic inputs
        result = find_image("Test Lot", self.user, self.online_auction)
        # Should return None when no images exist
        self.assertIsNone(result)

    def test_add_tos_info_requires_auctiontos_queryset(self):
        """Test that add_tos_info only accepts AuctionTOS querysets"""
        from auctions.models import add_tos_info

        # Should raise TypeError when not passed an AuctionTOS queryset
        with self.assertRaises(TypeError) as context:
            add_tos_info(Lot.objects.all())
        self.assertIn("must be passed a queryset of the AuctionTOS model", str(context.exception))

    def test_add_tos_info_annotates_fields(self):
        """Test add_tos_info adds expected annotations"""
        from auctions.models import add_tos_info

        qs = add_tos_info(AuctionTOS.objects.filter(pk=self.online_tos.pk))
        annotated_tos = qs.first()

        # Check that annotations are present
        self.assertTrue(hasattr(annotated_tos, "lots_bid"))
        self.assertTrue(hasattr(annotated_tos, "lots_viewed"))
        self.assertTrue(hasattr(annotated_tos, "lots_won"))
        self.assertTrue(hasattr(annotated_tos, "lots_submitted"))
        self.assertTrue(hasattr(annotated_tos, "other_auctions"))
        self.assertTrue(hasattr(annotated_tos, "lots_outbid"))
        self.assertTrue(hasattr(annotated_tos, "account_age_days"))
        self.assertTrue(hasattr(annotated_tos, "has_ever_granted_permission"))

    def test_add_tos_info_permission_filtering(self):
        """Test add_tos_info respects permission flags"""
        from auctions.models import add_tos_info

        # Create a manually added user (no permission granted)
        manual_user = User.objects.create_user(
            username="manual_user", password="testpassword", email="manual@example.com"
        )
        manual_tos = AuctionTOS.objects.create(
            user=manual_user,
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
        )

        qs = add_tos_info(AuctionTOS.objects.filter(pk=manual_tos.pk))
        annotated_tos = qs.first()

        # Manually added users without permission should have filtered data
        self.assertEqual(annotated_tos.lots_bid, 0)
        self.assertEqual(annotated_tos.lots_viewed, 0)

    def test_nearby_auctions_basic(self):
        """Test nearby_auctions returns auctions within distance"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # Test with a location that should match
        auctions, distances = nearby_auctions(40.7128, -74.0060, distance=100)

        # Should return lists
        self.assertIsInstance(auctions, list)
        self.assertIsInstance(distances, list)
        self.assertEqual(len(auctions), len(distances))

    def test_nearby_auctions_return_slugs(self):
        """Test nearby_auctions can return just slugs"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # Test return_slugs parameter
        slugs = nearby_auctions(40.7128, -74.0060, distance=100, return_slugs=True)

        # Should return list of slugs
        self.assertIsInstance(slugs, list)

    def test_nearby_auctions_filters_ignored(self):
        """Test nearby_auctions filters out ignored auctions for users"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # User ignores the auction
        AuctionIgnore.objects.create(user=self.user, auction=self.online_auction)

        # Should not return ignored auction for this user
        auctions, distances = nearby_auctions(40.7128, -74.0060, distance=100, user=self.user)

        auction_slugs = [a.slug for a in auctions]
        self.assertNotIn(self.online_auction.slug, auction_slugs)

    def test_nearby_auctions_filters_already_joined(self):
        """Test nearby_auctions can filter already joined auctions"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # User already has TOS (joined)
        # Test with include_already_joined=False (default)
        auctions, distances = nearby_auctions(
            40.7128, -74.0060, distance=100, user=self.user, include_already_joined=False
        )

        # User has already joined online_auction, so it should be filtered out
        auction_slugs = [a.slug for a in auctions]
        self.assertNotIn(self.online_auction.slug, auction_slugs)

    def test_nearby_auctions_includes_joined_when_requested(self):
        """Test nearby_auctions includes joined auctions when flag is set"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # Make the auction active/current
        self.online_auction.date_start = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.save()

        # Test with include_already_joined=True
        auctions, distances = nearby_auctions(
            40.7128, -74.0060, distance=100, user=self.user, include_already_joined=True
        )

        # Should include the auction even though user has TOS
        auction_slugs = [a.slug for a in auctions]
        self.assertIn(self.online_auction.slug, auction_slugs)


class FormsUtilityTestCase(TestCase):
    """Test cases for utility functions in forms.py"""

    def test_clean_summernote_short_html(self):
        """Test clean_summernote doesn't modify short HTML"""
        from auctions.forms import clean_summernote

        short_html = "<p>This is a short paragraph</p>"
        result = clean_summernote(short_html)
        self.assertEqual(result, short_html)

    def test_clean_summernote_long_html(self):
        """Test clean_summernote truncates long HTML"""
        from auctions.forms import clean_summernote

        # Create HTML longer than max_length
        long_html = "<p>" + "x" * 20000 + "</p>"
        result = clean_summernote(long_html, max_length=100)
        self.assertLessEqual(len(result), 100)

    def test_clean_summernote_preserves_br_tags(self):
        """Test clean_summernote preserves br tags when truncating"""
        from auctions.forms import clean_summernote

        # Create HTML with br tags
        html_with_br = "<p>Text<br/>More text<br />Even more</p>" + "x" * 20000
        result = clean_summernote(html_with_br, max_length=100)
        # br tags should be preserved in the output
        self.assertIn("<br", result)

    def test_clean_summernote_removes_other_tags_when_truncating(self):
        """Test clean_summernote removes non-br tags when truncating"""
        from auctions.forms import clean_summernote

        # Create HTML with various tags
        html = "<div><p><span>Text</span></p></div>" + "x" * 20000
        result = clean_summernote(html, max_length=100)
        # Should have removed tags but kept content
        self.assertNotIn("<div>", result)
        self.assertNotIn("<span>", result)

    def test_clean_summernote_empty_string(self):
        """Test clean_summernote handles empty string"""
        from auctions.forms import clean_summernote

        result = clean_summernote("")
        self.assertEqual(result, "")

    def test_clean_summernote_custom_max_length(self):
        """Test clean_summernote respects custom max_length parameter"""
        from auctions.forms import clean_summernote

        long_html = "x" * 1000
        result = clean_summernote(long_html, max_length=50)
        self.assertLessEqual(len(result), 50)

    def test_clean_summernote_none_returns_empty_string(self):
        """Test clean_summernote safely normalizes None values."""
        from auctions.forms import clean_summernote

        result = clean_summernote(None)

        self.assertEqual(result, "")

    def test_clean_summernote_removes_disallowed_tags(self):
        """Test clean_summernote strips script and image tags."""
        from auctions.forms import clean_summernote

        html = (
            "<p>Allowed</p><img src='/bad.png'><script>alert(1)</script>"
            "<iframe src='https://example.com'></iframe><embed src='/test.swf'><object data='/test'></object>"
        )
        result = clean_summernote(html)

        self.assertEqual(result, "<p>Allowed</p>")

    def test_clean_summernote_removes_scriptable_attributes(self):
        """Test clean_summernote strips dangerous attributes from allowed tags."""
        from auctions.forms import clean_summernote

        html = (
            '<p onclick="alert(1)" onerror="alert(1)">Text</p><a href="javascript:alert(1)">Link</a>'
            '<a href="vbscript:msgbox(1)">VB</a><a href="data:text/html;base64,PHNjcmlwdD4=">Data</a>'
            '<a href="java\nscript:alert(1)">Obfuscated</a>'
            '<a href="https://example.com">Safe</a>'
        )
        result = clean_summernote(html)

        self.assertEqual(
            result,
            '<p>Text</p><a>Link</a><a>VB</a><a>Data</a><a>Obfuscated</a><a href="https://example.com">Safe</a>',
        )

    def test_clean_summernote_removes_foreign_content_tags(self):
        """Allowlist sanitizer must strip <svg>/<math> and their subtrees (mutation-XSS vectors)
        that the old blocklist did not enumerate, while unwrapping unknown-but-benign tags."""
        from auctions.forms import clean_summernote

        self.assertEqual(clean_summernote("<svg><script>alert(1)</script></svg>"), "")
        self.assertEqual(clean_summernote("<math><mtext><script>alert(1)</script></mtext></math>"), "")
        self.assertEqual(clean_summernote("<svg><desc><img src=x onerror=alert(1)></desc></svg>"), "")
        # Unknown, non-executable tags are unwrapped so their text content survives.
        self.assertEqual(clean_summernote("<p>Keep <acme>this</acme></p>"), "<p>Keep this</p>")

    def test_summernote_widget_includes_upload_url_in_rendered_html(self):
        """Summernote widget should include upload URL and drag-drop disabling in rendered HTML."""
        from django.urls import reverse
        from django_summernote.widgets import SummernoteWidget

        upload_url = reverse("django_summernote-upload_attachment")
        html = SummernoteWidget().render("description", "", attrs={"id": "id_description"})

        self.assertIn(upload_url, html)
        self.assertIn('"disableDragAndDrop": true', html)


class TemplateTagsTestCase(TestCase):
    """Test cases for template tags"""

    def test_currency_symbol_filter(self):
        """Test currency_symbol template filter"""
        from auctions.templatetags.currency_filters import currency_symbol

        self.assertEqual(currency_symbol("USD"), "$")
        self.assertEqual(currency_symbol("GBP"), "£")
        self.assertEqual(currency_symbol("EUR"), "€")
        self.assertEqual(currency_symbol("JPY"), "¥")
        self.assertEqual(currency_symbol("CHF"), "CHF")
        self.assertEqual(currency_symbol("UNKNOWN"), "$")

    def test_format_price_filter_with_none(self):
        """Test format_price handles None values"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(None, "USD")
        self.assertEqual(result, "")

    def test_format_price_filter_usd(self):
        """Test format_price with USD currency"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(10.5, "USD")
        self.assertEqual(result, "$10.50")

    def test_format_price_filter_jpy(self):
        """Test format_price with JPY currency (no decimals)"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(1500.75, "JPY")
        self.assertEqual(result, "¥1500")

    def test_format_price_filter_chf(self):
        """Test format_price with CHF currency (space between symbol and amount)"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(25.50, "CHF")
        self.assertEqual(result, "CHF 25.50")

    def test_format_price_filter_invalid_value(self):
        """Test format_price with invalid price value"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price("invalid", "USD")
        self.assertEqual(result, "$invalid")

    def test_convert_distance_none(self):
        """Test convert_distance with None value"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance(None, None)
        self.assertIsNone(result)

    def test_convert_distance_zero(self):
        """Test convert_distance with zero distance"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance(0, None)
        self.assertIsNone(result)

    def test_convert_distance_negative(self):
        """Test convert_distance with negative distance"""
        from auctions.templatetags.distance_filters import convert_distance

        user = User.objects.create_user(username="test_user", password="testpass")
        result = convert_distance(-10, user)
        self.assertIsNone(result)

    def test_convert_distance_miles_for_anonymous(self):
        """Test convert_distance returns miles for anonymous users"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance(10, None)
        self.assertEqual(result, (10, "miles"))

    def test_convert_distance_miles_for_authenticated_user(self):
        """Test convert_distance returns miles for user with miles preference"""
        from auctions.templatetags.distance_filters import convert_distance

        user = User.objects.create_user(username="miles_user", password="testpass")
        user.userdata.distance_unit = "mi"
        user.userdata.save()

        result = convert_distance(10, user)
        self.assertEqual(result, (10, "miles"))

    def test_convert_distance_km_for_authenticated_user(self):
        """Test convert_distance converts to km for user with km preference"""
        from auctions.templatetags.distance_filters import MILES_TO_KM, convert_distance

        user = User.objects.create_user(username="km_user", password="testpass")
        user.userdata.distance_unit = "km"
        user.userdata.save()

        result = convert_distance(10, user)
        expected_km = int(round(10 * MILES_TO_KM))
        self.assertEqual(result, (expected_km, "km"))

    def test_convert_distance_invalid_string(self):
        """Test convert_distance with invalid string value"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance("invalid", None)
        self.assertIsNone(result)

    def test_convert_distance_valid_string(self):
        """Test convert_distance with valid numeric string"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance("15.5", None)
        self.assertEqual(result, (16, "miles"))  # Rounded

    def test_distance_display_filter(self):
        """Test distance_display template filter"""
        from auctions.templatetags.distance_filters import distance_display

        user = User.objects.create_user(username="display_user", password="testpass")
        user.userdata.distance_unit = "mi"
        user.userdata.save()

        result = distance_display(10, user)
        self.assertEqual(result, "10 miles")

    def test_distance_display_filter_none(self):
        """Test distance_display returns empty string for None"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(None, None)
        self.assertEqual(result, "")

    def test_distance_display_filter_zero(self):
        """Test distance_display returns empty string for zero"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(0, None)
        self.assertEqual(result, "")


class ContextProcessorsTestCase(TestCase):
    """Test cases for context processors"""

    def test_google_analytics_context(self):
        """Test google_analytics context processor returns expected keys"""
        from django.test import RequestFactory

        from auctions.context_processors import google_analytics

        factory = RequestFactory()
        request = factory.get("/")

        context = google_analytics(request)
        self.assertIn("GOOGLE_MEASUREMENT_ID", context)
        self.assertIn("GOOGLE_TAG_ID", context)
        self.assertIn("GOOGLE_ADSENSE_ID", context)
        self.assertIn("show_ads", context)

    @override_settings(SHOW_ADS=False)
    def test_google_analytics_context_show_ads_off(self):
        """SHOW_ADS=False disables ads globally via the context processor"""
        from django.test import RequestFactory

        from auctions.context_processors import google_analytics

        context = google_analytics(RequestFactory().get("/"))
        self.assertFalse(context["show_ads"])

    @override_settings(SHOW_ADS=True)
    def test_google_analytics_context_show_ads_on(self):
        """SHOW_ADS=True enables ads globally via the context processor"""
        from django.test import RequestFactory

        from auctions.context_processors import google_analytics

        context = google_analytics(RequestFactory().get("/"))
        self.assertTrue(context["show_ads"])

    def test_google_oauth_context(self):
        """Test google_oauth context processor returns expected keys"""
        from django.test import RequestFactory

        from auctions.context_processors import google_oauth

        factory = RequestFactory()
        request = factory.get("/")

        context = google_oauth(request)
        self.assertIn("GOOGLE_OAUTH_LINK", context)
        self.assertIn("GOOGLE_LOGIN_ENABLED", context)

    @override_settings(GOOGLE_OAUTH_LINK="secret.apps.googleusercontent.com")
    def test_google_oauth_context_marks_default_placeholder_as_disabled(self):
        from django.test import RequestFactory

        from auctions.context_processors import google_oauth

        factory = RequestFactory()
        request = factory.get("/")

        context = google_oauth(request)
        self.assertFalse(context["GOOGLE_LOGIN_ENABLED"])

    @override_settings(GOOGLE_OAUTH_LINK="real-client-id.apps.googleusercontent.com")
    def test_google_oauth_context_marks_real_client_id_as_enabled(self):
        from django.test import RequestFactory

        from auctions.context_processors import google_oauth

        factory = RequestFactory()
        request = factory.get("/")

        context = google_oauth(request)
        self.assertTrue(context["GOOGLE_LOGIN_ENABLED"])

    def test_theme_context_anonymous_user(self):
        """Test theme context processor for anonymous users"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import theme

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()

        context = theme(request)
        self.assertIn("theme", context)

    def test_theme_context_authenticated_user(self):
        """Test theme context processor for authenticated users"""
        from django.test import RequestFactory

        from auctions.context_processors import theme

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="theme_user", password="testpass")
        request.user = user

        context = theme(request)
        self.assertIn("theme", context)

    def test_add_tz_with_cookie(self):
        """Test add_tz context processor with timezone cookie"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"user_timezone": "America/Los_Angeles"}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "America/Los_Angeles")
        self.assertTrue(context["user_timezone_set"])

    def test_add_tz_without_cookie(self):
        """Test add_tz context processor without timezone cookie"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "America/New_York")  # Default
        self.assertFalse(context["user_timezone_set"])

    def test_add_tz_authenticated_user_with_saved_timezone(self):
        """Test add_tz uses saved timezone for authenticated users"""
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="tz_user", password="testpass")
        user.userdata.timezone = "Europe/London"
        user.userdata.save()
        request.user = user
        request.COOKIES = {}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "Europe/London")

    def test_add_tz_rejects_invalid_cookie(self):
        """Invalid tz cookie value falls back to the default and is not flagged as set."""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"user_timezone": "Not/A_Real_Zone"}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "America/New_York")
        self.assertFalse(context["user_timezone_set"])

    def test_add_tz_rejects_invalid_userdata_timezone(self):
        """Garbage userdata.timezone value falls back to the default."""
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="bad_tz_user", password="testpass")
        user.userdata.timezone = "Not/A_Real_Zone"
        user.userdata.save()
        request.user = user
        request.COOKIES = {}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "America/New_York")

    def test_base_template_renders_without_context_processors(self):
        """Django's default 500/404 views call template.render() with no RequestContext,
        so context processors don't run and user_timezone is undefined. The base template
        must still render -- otherwise the error page itself errors out with
        `ValueError: ZoneInfo keys must be normalized relative paths, got: `."""
        from django.template.loader import get_template

        # Render with no context at all -- mirrors django.views.defaults.server_error.
        get_template("500.html").render()

    def test_add_location_with_cookies(self):
        """Test add_location context processor with location cookies"""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"latitude": "40.7128", "longitude": "-74.0060"}

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        context = add_location(request)
        self.assertTrue(context["has_user_location"])

    def test_add_location_without_cookies(self):
        """Test add_location context processor without location cookies"""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {}
        request.META = {}

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        context = add_location(request)
        self.assertFalse(context["has_user_location"])

    def test_add_location_saves_ip_for_authenticated_user(self):
        """Test add_location saves IP address for authenticated users"""
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="ip_user", password="testpass")
        request.user = user
        request.COOKIES = {}
        request.META = {"REMOTE_ADDR": "192.168.1.1"}

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        add_location(request)

        # Reload user data to check if IP was saved
        user.userdata.refresh_from_db()
        self.assertEqual(user.userdata.last_ip_address, "192.168.1.1")

    def test_add_location_handles_x_forwarded_for(self):
        """Test add_location handles X-Forwarded-For header"""
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="forwarded_user", password="testpass")
        request.user = user
        request.COOKIES = {}
        request.META = {
            "HTTP_X_FORWARDED_FOR": "10.0.0.1, 192.168.1.1",
            "REMOTE_ADDR": "192.168.1.1",
        }

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        add_location(request)

        # Should use first IP from X-Forwarded-For
        user.userdata.refresh_from_db()
        self.assertEqual(user.userdata.last_ip_address, "10.0.0.1")

    def test_dismissed_cookies_tos_with_cookie(self):
        """Test dismissed_cookies_tos with cookie present"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import dismissed_cookies_tos

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"hide_tos_banner": "true"}

        context = dismissed_cookies_tos(request)
        self.assertTrue(context["hide_tos_banner"])

    def test_dismissed_cookies_tos_without_cookie(self):
        """Test dismissed_cookies_tos without cookie"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import dismissed_cookies_tos

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {}

        context = dismissed_cookies_tos(request)
        self.assertFalse(context["hide_tos_banner"])

    def test_dismissed_cookies_tos_authenticated_user(self):
        """Test dismissed_cookies_tos for authenticated user with dismissed flag"""
        from django.test import RequestFactory

        from auctions.context_processors import dismissed_cookies_tos

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="tos_user", password="testpass")
        user.userdata.dismissed_cookies_tos = True
        user.userdata.save()
        request.user = user
        request.COOKIES = {}

        context = dismissed_cookies_tos(request)
        self.assertTrue(context["hide_tos_banner"])

    def test_site_config_context(self):
        """Test site_config context processor returns expected keys"""
        from django.test import RequestFactory

        from auctions.context_processors import site_config

        factory = RequestFactory()
        request = factory.get("/")

        context = site_config(request)
        self.assertIn("navbar_brand", context)
        self.assertIn("copyright_message", context)
        self.assertIn("show_footer_icon", context)
        self.assertIn("enable_club_finder", context)
        self.assertIn("enable_help", context)
        self.assertIn("enable_promo_page", context)
        self.assertIn("recaptcha_enabled", context)


class FooterIconTests(TestCase):
    def test_footer_icon_shown_by_default(self):
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "icon-footer.png")

    @override_settings(SHOW_FOOTER_ICON=False)
    def test_footer_icon_hidden_when_disabled(self):
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "icon-footer.png")


class SiteWebmanifestTests(TestCase):
    @override_settings(NAVBAR_BRAND="Test Auctions")
    def test_manifest_returns_brand_and_icons(self):
        response = self.client.get(reverse("site_webmanifest"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        data = json.loads(response.content)
        self.assertEqual(data["name"], "Test Auctions")
        sources = {icon["src"] for icon in data["icons"]}
        self.assertIn("/static/android-chrome-512x512.png", sources)
        # Maskable variants keep their art inside the launcher-crop safe zone
        self.assertIn("/static/android-chrome-maskable-512x512.png", sources)


class GoogleLoginTemplateVisibilityTests(TestCase):
    @override_settings(GOOGLE_OAUTH_LINK="secret.apps.googleusercontent.com")
    def test_login_page_hides_google_button_when_oauth_link_is_placeholder(self):
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "id='sign-in-google'")

    @override_settings(GOOGLE_OAUTH_LINK="real-client-id.apps.googleusercontent.com")
    def test_login_page_shows_google_button_when_oauth_link_is_real(self):
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id='sign-in-google'")

    @override_settings(GOOGLE_OAUTH_LINK="secret.apps.googleusercontent.com")
    def test_signup_page_hides_gmail_prompt_text_when_oauth_link_is_placeholder(self):
        response = self.client.get(reverse("account_signup"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Looks like a Gmail address!")


class AdminSetupChecklistViewTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("setupadmin", "setup@example.com", "testpass")
        self.client.force_login(self.superuser)
        # The checklist looks up the server's public IP over the network; pin it in tests.
        ip_patcher = patch("auctions.views.admin_checklist.get_server_public_ip", return_value="203.0.113.7")
        ip_patcher.start()
        self.addCleanup(ip_patcher.stop)

    def test_admin_menu_shows_setup_checklist_link(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Setup Checklist")

    @override_settings(SINGLE_CLUB_MODE=True, SETUP_COMPLETE=True)
    def test_setup_checklist_page_renders(self):
        response = self.client.get(reverse("admin_setup_checklist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Setup Checklist")
        self.assertContains(response, "Single club mode")
        self.assertContains(response, "Google Maps")
        self.assertContains(response, "Mailchimp")
        self.assertContains(response, "Square")
        # The server IP appears in the site-domain DNS instructions.
        self.assertContains(response, "203.0.113.7")

    @override_settings(SITE_DOMAIN="127.0.0.1")
    def test_site_domain_item_treats_localhost_default_as_configured(self):
        response = self.client.get(reverse("admin_setup_checklist"))
        self.assertEqual(response.status_code, 200)
        setup_items = response.context["setup_items"]
        site_domain_item = next(item for item in setup_items if item["name"] == "Site domain")
        self.assertTrue(site_domain_item["configured"])

    @override_settings(
        POST_OFFICE_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST_USER="user@example.com",
        EMAIL_HOST_PASSWORD="unsecure",
    )
    def test_email_delivery_item_requires_non_placeholder_smtp_credentials(self):
        response = self.client.get(reverse("admin_setup_checklist"))
        self.assertEqual(response.status_code, 200)
        setup_items = response.context["setup_items"]
        email_item = next(item for item in setup_items if item["name"] == "Email delivery")
        self.assertFalse(email_item["configured"])

    def test_wallet_items_say_uuid_links_can_add_and_not_owner_only(self):
        # The wallet cards are reachable by UUID link, not just the signed-in owner. The help text
        # must reflect that and must not repeat the old owner-only claim.
        response = self.client.get(reverse("admin_setup_checklist"))
        setup_items = response.context["setup_items"]
        for name in ("Google Wallet membership cards", "Apple Wallet membership cards"):
            item = next(i for i in setup_items if i["name"] == name)
            self.assertIn("UUID link", item["what_it_does"])
            self.assertNotIn("only the signed-in account", item["what_it_does"].lower())
