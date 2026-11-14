"""Review fixes for the Summernote sanitizer and the custom-dropdown options API.

Covers preformatted nodes (CDATA, comments, declarations) being dropped before the tag rule runs,
the class allowlist that keeps the site's layout classes out of stored content, and the admin-only
read of an auction's dropdown options.
"""

from django.test import SimpleTestCase
from django.urls import reverse

from auctions.html_sanitize import sanitize_summernote_html
from auctions.tests import StandardTestCase


class SummernoteSanitizerReviewTests(SimpleTestCase):
    def test_cdata_is_dropped(self):
        result = sanitize_summernote_html("<p>a<![CDATA[><img src=x onerror=alert(1)>]]>b</p>")
        self.assertNotIn("<img", result)
        self.assertNotIn("onerror", result)

    def test_comments_and_declarations_are_dropped(self):
        result = sanitize_summernote_html("<!DOCTYPE html><p>x</p><!-- <img src=x onerror=alert(1)> -->")
        self.assertEqual(result, "<p>x</p>")

    def test_layout_classes_are_dropped(self):
        result = sanitize_summernote_html('<div class="position-fixed top-0 w-100 h-100">x</div>')
        self.assertEqual(result, "<div>x</div>")

    def test_table_classes_survive(self):
        result = sanitize_summernote_html('<table class="table table-bordered w-100"><tr><td>x</td></tr></table>')
        self.assertIn('class="table table-bordered"', result)
        self.assertNotIn("w-100", result)


class AuctionDropdownOptionsGetReviewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_custom_dropdown_options", kwargs={"slug": self.online_auction.slug})

    def test_dropdown_options_get_is_admin_only(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
