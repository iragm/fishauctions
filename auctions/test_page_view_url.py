"""The shape of ``PageView.url``: a site-relative path, on the way in and on the rows already there.

Until 2026-09 the beacon in ``base_page_view.html`` posted ``window.location.href``, so every row it
wrote was ``https://auction.fish/lots/123`` while every reader of the column -- ``duplicates``,
``url__startswith="/account/"``, the traffic dashboard -- wants a path.  There was no test on either
side of that, which is why it shipped and stayed.

So the cases below are deliberately paired: one set fixes what the endpoint stores, and one set
fixes what migration 0425 does to the rows written before it, including the round trip through the
endpoint itself.
"""

from importlib import import_module

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from auctions.models import PageView
from auctions.views.ajax import page_view_path

# A migration module's name starts with a digit, so it cannot be imported with an import statement.
our_hosts = import_module("auctions.migrations.0425_page_view_url_to_path").our_hosts


class PageViewPathTests(TestCase):
    def test_our_own_origin_is_stripped(self):
        self.assertEqual(page_view_path("https://auction.fish/lots/123", "auction.fish"), "/lots/123")
        self.assertEqual(page_view_path("http://127.0.0.1/lots/123", "127.0.0.1"), "/lots/123")

    def test_a_bare_origin_becomes_the_homepage(self):
        self.assertEqual(page_view_path("https://auction.fish", "auction.fish"), "/")
        self.assertEqual(page_view_path("https://auction.fish/", "auction.fish"), "/")

    def test_a_path_is_left_alone(self):
        self.assertEqual(page_view_path("/lots/123", "auction.fish"), "/lots/123")

    def test_the_query_string_and_the_fragment_both_go(self):
        """The query string always did. The fragment did not, and split one page across rows --
        which the reach report groups on exactly."""
        self.assertEqual(page_view_path("https://auction.fish/lots/1?src=abc", "auction.fish"), "/lots/1")
        self.assertEqual(page_view_path("https://auction.fish/lots/1#chat", "auction.fish"), "/lots/1")
        self.assertEqual(page_view_path("/lots/1?page=2#bottom", "auction.fish"), "/lots/1")

    def test_another_host_is_kept_whole(self):
        """Stripping it would file someone else's page as one of ours, under a path that reads
        exactly like a real one. This endpoint takes anything: it is AllowAny."""
        self.assertEqual(page_view_path("https://example.com/lots/123", "auction.fish"), "https://example.com/lots/123")

    def test_only_http_urls_are_kept_at_all(self):
        """The admin traffic dashboard renders this column as ``<a href="...">``, and this endpoint
        is ``AllowAny`` -- so a ``javascript:`` URL stored here is a link on an admin's page. The
        old code stored whatever it was POSTed."""
        self.assertEqual(page_view_path("javascript:alert(document.cookie)", "auction.fish"), "")
        self.assertEqual(page_view_path("JavaScript:alert(1)", "auction.fish"), "")
        self.assertEqual(page_view_path("data:text/html,<script>alert(1)</script>", "auction.fish"), "")

    def test_a_protocol_relative_url_is_not_mistaken_for_a_path(self):
        """``//evil.com/lots/1`` has no scheme but is not one of our paths either."""
        self.assertEqual(page_view_path("//evil.com/lots/1", "auction.fish"), "//evil.com/lots/1")
        self.assertEqual(page_view_path("//auction.fish/lots/1", "auction.fish"), "/lots/1")

    def test_nothing_in_gives_nothing_out(self):
        """A POST with no url at all used to be a TypeError inside ``re.sub``."""
        self.assertEqual(page_view_path(None), "")
        self.assertEqual(page_view_path(""), "")

    def test_a_long_url_is_truncated_to_the_column(self):
        self.assertEqual(len(page_view_path("https://auction.fish/" + "a" * 800, "auction.fish")), 600)
        self.assertEqual(len(page_view_path("https://example.com/" + "a" * 800, "auction.fish")), 600)


class MigrationHostListTests(TestCase):
    """``our_hosts`` in migration 0425, which decides whose origin gets stripped off old rows."""

    @override_settings(ALLOWED_HOSTS=["auction.fish", "127.0.0.1"])
    def test_hosts_become_an_escaped_alternation(self):
        self.assertEqual(our_hosts(), r"127\.0\.0\.1|auction\.fish")

    @override_settings(ALLOWED_HOSTS=["auction.fish", "", "", ".sub.auction.fish"])
    def test_blanks_are_dropped_and_a_leading_dot_is_not_part_of_the_host(self):
        """Every unset ``ALLOWED_HOST_n`` leaves a blank in the list, and a leading dot is
        Django's "and subdomains" marker rather than something a URL ever contains."""
        self.assertEqual(our_hosts(), r"auction\.fish|sub\.auction\.fish")

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_a_wildcard_names_no_host_so_nothing_is_stripped(self):
        """An empty result makes the migration leave every row alone, which is the safe way to be
        wrong: a wildcard would otherwise read as "every host is ours"."""
        self.assertEqual(our_hosts(), "")


class PageViewCreateStoresAPathTests(TestCase):
    """End to end: what the beacon posts, through the endpoint, into the column."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="flow", email="flow@example.com", password="pw")

    def _post(self, url):
        response = self.client.post(
            "/api/pageview/",
            data={"url": url, "first_view": "true", "referrer": "", "title": "Lots"},
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 200)
        return PageView.objects.order_by("-pk").first()

    def test_an_absolute_url_is_stored_as_a_path(self):
        self.assertEqual(self._post("https://testserver/lots/123").url, "/lots/123")

    def test_the_homepage_is_stored_as_a_slash(self):
        self.assertEqual(self._post("https://testserver").url, "/")

    def test_a_path_posted_directly_is_unchanged(self):
        """The AR endpoint writes ``lot.lot_link``, which is already a path."""
        self.assertEqual(self._post("/lots/123").url, "/lots/123")
