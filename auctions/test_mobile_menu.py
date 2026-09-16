"""The app's navigation drawer: /api/mobile/config/ -> "menu".

It used to be compiled into the app from the web navbar, so it was permanently a release behind. It
is built by `auctions.mobile.menu` now, which trades staleness for drift: nothing stops the two
lists diverging, and the app drops rows it doesn't get without complaining.

`NavbarDriftTests` is the guard: it renders the real navbar, pulls the links out of the HTML, and
fails if one is missing from that user's payload. Deliberately web-only links go in WEB_ONLY_PATHS
with a reason.
"""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.mobile import menu as menu_module
from auctions.mobile.menu import menu_for
from auctions.test_support import isolated_cache

# Navbar links that deliberately don't appear in the drawer; each needs a reason. Empty on purpose:
# the four missing rows aren't links at all -- sign out, offline mode, Tap to Pay and Clubs are
# native screens the app supplies, and sign out does five things a web /logout/ link doesn't.
WEB_ONLY_PATHS = set()


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def _paths(payload):
    """Every path in a menu payload, flattened."""
    return [item["path"] for section in payload["sections"] for item in section["items"]]


def _section_ids(payload):
    return [section["id"] for section in payload["sections"]]


def _section(payload, section_id):
    for section in payload["sections"]:
        if section["id"] == section_id:
            return section
    return None


@isolated_cache("mobile-menu")
class MenuPayloadTests(TestCase):
    """What `menu_for` builds, per user."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="menuuser", email="menu@example.com", password="testpass")
        cls.superuser = User.objects.create_superuser(
            username="menuadmin", email="menuadmin@example.com", password="testpass"
        )

    def test_signed_out_gets_only_the_public_sections(self):
        """The app draws no drawer while signed out, but the honest answer is the signed-out navbar."""
        from django.contrib.auth.models import AnonymousUser

        payload = menu_for(AnonymousUser())
        self.assertEqual(_section_ids(payload), ["main", "about"])
        self.assertNotIn(reverse("selling"), _paths(payload))
        self.assertNotIn(reverse("account"), _paths(payload))

    def test_signed_in_gets_the_account_sections(self):
        payload = menu_for(self.user)
        self.assertEqual(_section_ids(payload), ["main", "lots", "account", "about"])
        self.assertIn(reverse("my_bids"), _paths(payload))
        self.assertIn(reverse("account_setup"), _paths(payload))

    def test_the_settings_pages_are_behind_the_account_row(self):
        """The settings pages are behind the Account row, on the web and here; each carries the Account setup
        sidebar, which is the app's whole navigation on those pages.
        """
        paths = _paths(menu_for(self.user))
        for name in ("preferences", "contact_info", "printing", "ignore_categories", "messages"):
            self.assertNotIn(reverse(name), paths, name)

    def test_admin_section_is_for_superusers_only(self):
        """The Admin section is superusers only -- the condition base.html uses, and a server question, which
        is why the drawer moved here.
        """
        self.assertNotIn("admin", _section_ids(menu_for(self.user)))
        payload = menu_for(self.superuser)
        self.assertIn("admin", _section_ids(payload))
        self.assertIn(reverse("admin_dashboard"), _paths(payload))

    def test_the_two_big_sections_are_collapsed_and_carry_an_icon(self):
        """The two big sections are collapsed and carry an icon: a twelve-row Admin group rendered flat buries
        everything under it.
        """
        payload = menu_for(self.superuser)
        for section_id in ("admin", "about"):
            section = _section(payload, section_id)
            self.assertTrue(section["collapsed"])
            self.assertTrue(section["icon"].startswith("bi-"))

    def test_the_top_section_has_no_header(self):
        """`title` is the group header; the top group is not a group."""
        self.assertNotIn("title", _section(menu_for(self.user), "main"))

    def test_the_apps_merge_anchors_are_present(self):
        """The app's merge anchors (`main` and `account`) are present; a row whose anchor is missing is
        appended near the bottom rather than lost.
        """
        self.assertEqual({"main", "account"} - set(_section_ids(menu_for(self.user))), set())

    def test_section_ids_are_unique(self):
        ids = _section_ids(menu_for(self.superuser))
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_path_is_site_relative(self):
        """Every path is site-relative: these load in the app's own WebView chrome, under the same rule
        terms_url and privacy_policy_url live under.
        """
        for path in _paths(menu_for(self.superuser)):
            self.assertTrue(path.startswith("/"), path)
            self.assertFalse(path.startswith("//"), path)
            self.assertNotIn("://", path)

    def test_every_row_has_a_title_and_an_icon(self):
        for section in menu_for(self.superuser)["sections"]:
            for item in section["items"]:
                self.assertTrue(item["title"])
                self.assertTrue(item["icon"].startswith("bi-"), item)

    def test_query_strings_survive(self):
        """Query strings survive: `?days=30` on the admin links is load-bearing."""
        self.assertIn(reverse("admin_traffic") + "?days=30", _paths(menu_for(self.superuser)))

    def test_about_site_follows_the_promo_page_setting(self):
        with override_settings(ENABLE_PROMO_PAGE=True):
            self.assertIn(reverse("promo"), _paths(menu_for(self.user)))
        with override_settings(ENABLE_PROMO_PAGE=False):
            payload = menu_for(self.user)
            self.assertNotIn(reverse("promo"), _paths(payload))
            # The rest of the section survives -- only the one row is gated.
            self.assertIn(reverse("faq"), _paths(payload))

    def test_the_rows_the_app_owns_are_never_sent(self):
        """The rows the app owns are never sent: each is a native screen with its own gating, and sign out does
        five things a web /logout/ link doesn't.
        """
        payload = menu_for(self.superuser)
        titles = [item["title"].lower() for section in payload["sections"] for item in section["items"]]
        for forbidden in ("sign out", "log out", "logout", "offline", "tap to pay", "clubs"):
            self.assertNotIn(forbidden, titles)
        self.assertNotIn(reverse("account_logout"), _paths(payload))

    def test_version_is_advertised(self):
        self.assertEqual(menu_for(self.user)["version"], menu_module.MENU_VERSION)


@isolated_cache("mobile-menu-rows")
class RowSanitizerTests(TestCase):
    """A bad row is dropped rather than sent. Nothing built today can trip these -- every path comes from
    reverse() -- but the drawer is a list of links loaded in the app's own chrome.
    """

    def test_a_row_needs_a_title_and_a_path(self):
        self.assertIsNone(menu_module._row("", "/lots/"))
        self.assertIsNone(menu_module._row("Lots", ""))

    def test_an_off_host_url_is_dropped(self):
        self.assertIsNone(menu_module._row("Elsewhere", "https://example.com/lots/"))
        self.assertIsNone(menu_module._row("Protocol relative", "//example.com/lots/"))

    def test_a_relative_path_with_a_query_string_survives(self):
        self.assertEqual(
            menu_module._row("Traffic", "/admin-traffic/?days=30", "bi-graph-up"),
            {"title": "Traffic", "path": "/admin-traffic/?days=30", "icon": "bi-graph-up"},
        )

    def test_a_section_with_no_usable_rows_is_dropped(self):
        self.assertIsNone(menu_module._section("empty", [None, None]))

    def test_optional_section_keys_are_omitted_rather_than_empty(self):
        section = menu_module._section("main", [menu_module._row("Lots", "/lots/")])
        self.assertEqual(set(section), {"id", "items"})


@isolated_cache("mobile-menu-drift")
class NavbarDriftTests(TestCase):
    """The navbar and the drawer are two lists of the same links. This is what stops them drifting.

    It renders the real page, so it fails on the *rendered* navbar for that user -- including
    anything the template gates on a setting or a permission -- rather than on the template source.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="drifter", email="drift@example.com", password="testpass")
        cls.superuser = User.objects.create_superuser(
            username="driftadmin", email="driftadmin@example.com", password="testpass"
        )

    def _navbar(self, user):
        """The rendered HTML of a page that draws the full navbar for this user."""
        self.client.force_login(user)
        response = self.client.get(reverse("selling"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    @staticmethod
    def _dropdown_items(html, start_marker, end_marker):
        """The hrefs of one navbar dropdown, taken between two markers in the rendered page."""
        start = html.index(start_marker)
        end = html.index(end_marker, start)
        return re.findall(r'class="dropdown-item[^"]*"\s+href="([^"]+)"', html[start:end])

    def _assert_covered(self, hrefs, payload, dropdown):
        self.assertTrue(hrefs, f"found no links in the {dropdown} dropdown -- the markers moved")
        paths = set(_paths(payload))
        for href in hrefs:
            if href in WEB_ONLY_PATHS:
                continue
            self.assertIn(
                href,
                paths,
                f"\"{href}\" is in the navbar's {dropdown} dropdown but not in the app's drawer. "
                f"Add it to auctions/mobile/menu.py, or to WEB_ONLY_PATHS in this file with a "
                f"reason it belongs on the web only.",
            )

    def test_every_account_dropdown_link_reaches_the_app(self):
        html = self._navbar(self.user)
        hrefs = self._dropdown_items(
            html,
            f'aria-expanded="false">{self.user.username}</a>',
            reverse("account_logout"),  # the sign-out form closes the dropdown
        )
        self._assert_covered(hrefs, menu_for(self.user), "account")

    def test_every_admin_dropdown_link_reaches_a_superuser(self):
        html = self._navbar(self.superuser)
        hrefs = self._dropdown_items(
            html,
            'aria-expanded="false">Admin</a>',
            'aria-expanded="false">About</a>',  # the next dropdown along
        )
        self._assert_covered(hrefs, menu_for(self.superuser), "admin")

    @override_settings(ENABLE_PROMO_PAGE=True)
    def test_every_about_dropdown_link_reaches_the_app(self):
        """Pinned on, so the gated "About site" row is in both lists: a row only one side gates is the bug this
        test is for.
        """
        html = self._navbar(self.user)
        hrefs = self._dropdown_items(
            html,
            'aria-expanded="false">About</a>',
            "</div>",  # this dropdown holds only links, so the first close ends it
        )
        self._assert_covered(hrefs, menu_for(self.user), "about")

    def test_a_normal_user_is_not_offered_the_admin_pages(self):
        """The drawer must not carry a link the navbar wouldn't have drawn for this user."""
        html = self._navbar(self.user)
        self.assertNotIn('aria-expanded="false">Admin</a>', html)
        self.assertNotIn(reverse("admin_dashboard"), _paths(menu_for(self.user)))


@isolated_cache("mobile-menu-config")
class ConfigEndpointTests(TestCase):
    """The endpoint itself: public, but personalised when a token is sent."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="configuser", email="config@example.com", password="testpass")
        cls.superuser = User.objects.create_superuser(
            username="configadmin", email="configadmin@example.com", password="testpass"
        )

    def setUp(self):
        self.url = reverse("mobile-config")

    def test_anonymous_still_gets_a_200_and_a_menu(self):
        """Anonymous still gets a 200 and a menu: this endpoint is read before sign-in to wire up Square,
        Firebase and the social buttons.
        """
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_section_ids(response.json()["menu"]), ["main", "about"])

    def test_a_bearer_token_personalises_the_menu(self):
        payload = self.client.get(self.url, **_bearer(self.user)).json()["menu"]
        self.assertIn("account", _section_ids(payload))
        self.assertNotIn("admin", _section_ids(payload))

    def test_a_superusers_token_gets_the_admin_section(self):
        payload = self.client.get(self.url, **_bearer(self.superuser)).json()["menu"]
        self.assertIn("admin", _section_ids(payload))

    def test_an_unusable_token_is_anonymous_rather_than_a_401(self):
        """An unusable token is treated as anonymous rather than 401: a phone whose token aged out overnight
        must not lose the whole config over one optional block.
        """
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer not-a-real-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_section_ids(response.json()["menu"]), ["main", "about"])

    def test_a_web_session_is_not_a_mobile_credential(self):
        """A web session is not a mobile credential (IsMobileAuthenticated), so a signed-in browser gets the
        public menu.
        """
        self.client.force_login(self.user)
        payload = self.client.get(self.url).json()["menu"]
        self.assertEqual(_section_ids(payload), ["main", "about"])

    def test_the_response_is_not_cached(self):
        """The response is not cached: it varies per user, and a blanket cache would serve one user's drawer,
        admin section included, to the next caller.
        """
        response = self.client.get(self.url, **_bearer(self.superuser))
        self.assertNotIn("max-age", response.get("Cache-Control", ""))
