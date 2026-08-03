"""Tests for the palette's page catalog.

The first test in here is the important one, and it isn't really a test of behaviour: it is the
rule that every URL on the site is either something the assistant can reach or something somebody
has written down a reason for. Without it the catalog rots the moment a new page is added, and the
assistant quietly falls behind the UI again -- which is exactly the state this replaced.
"""

from django.test import override_settings
from django.urls import reverse

from auctions import palette_actions, palette_routes
from auctions.tests import StandardTestCase


class RouteAuditTests(StandardTestCase):
    """Every named URL is catalogued, excused, or the build fails."""

    def test_every_url_is_either_navigable_or_has_a_written_reason(self):
        audit = palette_routes.audit()
        self.assertEqual(
            audit["uncovered"],
            [],
            "These URLs are neither in palette_routes.ROUTES nor EXCLUDED, so the command palette "
            "assistant can't reach them and nobody has said why. Add a Route for each one, or add "
            "it to EXCLUDED with a reason.",
        )

    def test_no_route_points_at_a_url_that_no_longer_exists(self):
        audit = palette_routes.audit()
        self.assertEqual(
            audit["stale"],
            [],
            "These names are described in palette_routes but aren't in the URLconf any more. They "
            "would fail at reverse() in front of a user.",
        )

    def test_the_catalog_is_actually_substantial(self):
        """A guard against the catalog being gutted to make the audit pass."""
        audit = palette_routes.audit()
        self.assertGreater(len(audit["covered"]), 100)

    def test_every_excluded_reason_is_a_real_sentence(self):
        for name, reason in palette_routes.EXCLUDED.items():
            self.assertGreater(len(reason), 20, f"{name} needs a real reason, not '{reason}'")

    def test_every_route_reverses_with_the_kwargs_its_scope_supplies(self):
        """Catches a route whose URL takes ``lot`` when the scope hands it ``pk``."""
        from django.urls import NoReverseMatch

        dummy = {
            palette_routes.SCOPE_NONE: {},
            palette_routes.SCOPE_AUCTION: {"slug": "x"},
            palette_routes.SCOPE_AUCTION_BIDDER: {"slug": "x"},
            palette_routes.SCOPE_AUCTION_USERNAME: {"slug": "x", "username": "bob"},
            palette_routes.SCOPE_CLUB: {"slug": "x"},
            palette_routes.SCOPE_CLUB_TAB: {"slug": "x", "tab": "bap"},
            palette_routes.SCOPE_LOT: {},
            palette_routes.SCOPE_INVOICE: {"pk": 1},
            palette_routes.SCOPE_LOCATION: {"pk": 1},
            palette_routes.SCOPE_MEMBER: {"slug": "x", "pk": 1},
            palette_routes.SCOPE_USER: {"slug": "bob"},
            palette_routes.SCOPE_BLOG: {"slug": "x"},
        }
        for route in palette_routes.ROUTE_LIST:
            kwargs = dict(dummy[route.scope])
            kwargs.update(route.fixed)
            if route.scope == palette_routes.SCOPE_AUCTION_BIDDER:
                kwargs[route.param or "bidder_number"] = "7"
            if route.scope == palette_routes.SCOPE_LOT:
                kwargs[route.param or "pk"] = 1
            try:
                reverse(route.key, kwargs=kwargs) if kwargs else reverse(route.key)
            except NoReverseMatch:  # pragma: no cover - only runs when something is wrong
                self.fail(f"Route {route.key} (scope {route.scope!r}) can't be reversed with {kwargs}")


@override_settings(SINGLE_CLUB_MODE=False)
class RouteMatchingTests(StandardTestCase):
    """Free-text matching, which is the safety net when the model sends a description not a key."""

    def test_a_plain_english_phrase_finds_the_right_page(self):
        matches = palette_routes.match_routes("treasurer report")
        self.assertEqual(matches[0].key, "club_treasurer_report")

    def test_filler_words_do_not_drown_the_signal(self):
        matches = palette_routes.match_routes("can you take me to where I pay my dues")
        self.assertIn("club_membership_pay", [route.key for route in matches])

    def test_nonsense_matches_nothing(self):
        self.assertEqual(palette_routes.match_routes("xyzzy plugh"), [])

    def test_catalog_hides_admin_pages_from_ordinary_users(self):
        catalog = palette_routes.catalog_for_prompt(self.user)
        self.assertNotIn("admin_setup_checklist", catalog)

    def test_catalog_shows_site_admin_pages_to_superusers(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        catalog = palette_routes.catalog_for_prompt(self.admin_user)
        self.assertIn("admin_setup_checklist", catalog)

    def test_catalog_lists_pages_everyone_can_use(self):
        catalog = palette_routes.catalog_for_prompt(self.user)
        self.assertIn("my_invoices", catalog)
        self.assertIn("watched", catalog)


@override_settings(SINGLE_CLUB_MODE=False)
class PageContextTests(StandardTestCase):
    """Working out what the user is looking at from the path their browser sent."""

    def test_an_auction_page_resolves_to_that_auction(self):
        path = reverse("auction_main", kwargs={"slug": self.online_auction.slug})
        context = palette_routes.page_context_from_path(self.admin_user, path)
        self.assertEqual(context["auction"], self.online_auction.slug)
        self.assertEqual(context["auction_title"], self.online_auction.title)

    def test_a_lot_page_resolves_to_that_lot(self):
        path = reverse("lot_by_pk", kwargs={"pk": self.lot.pk})
        context = palette_routes.page_context_from_path(self.user, path)
        self.assertEqual(context["lot_id"], self.lot.pk)

    def test_an_auction_the_user_is_not_part_of_is_not_returned(self):
        """The client sends a path; it does not get to name an auction by sending one."""
        path = reverse("auction_main", kwargs={"slug": self.online_auction.slug})
        context = palette_routes.page_context_from_path(self.user_who_does_not_join, path)
        self.assertNotIn("auction", context)

    def test_rubbish_paths_are_ignored_rather_than_raising(self):
        for path in ("", "not-a-path", "/nope/nope/nope/", "//", "/" + "x" * 600):
            self.assertEqual(palette_routes.page_context_from_path(self.user, path), {})

    def test_page_context_beats_last_auction_used(self):
        """The whole point: the auction on screen wins over the stickier last-used one."""
        self.admin_user.userdata.last_auction_used = self.online_auction
        self.admin_user.userdata.save()
        page = palette_routes.page_context_from_path(
            self.admin_user, reverse("auction_main", kwargs={"slug": self.in_person_auction.slug})
        )
        auction, error = palette_actions.resolve_auction(self.admin_user, "", page)
        self.assertIsNone(error)
        self.assertEqual(auction.pk, self.in_person_auction.pk)

    def test_without_page_context_the_last_auction_is_still_used(self):
        self.admin_user.userdata.last_auction_used = self.online_auction
        self.admin_user.userdata.save()
        auction, error = palette_actions.resolve_auction(self.admin_user, "", {})
        self.assertIsNone(error)
        self.assertEqual(auction.pk, self.online_auction.pk)
