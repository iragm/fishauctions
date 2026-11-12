"""The growth reports on ``/mcp/admin/`` (:mod:`auctions.mcp.admin_reports`), the 404 page's title they
count by, and the palette's verb for the page it was opened on."""

import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.utils import timezone

from auctions import command_palette, palette_routes
from auctions.mcp import admin, admin_reports
from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    CommandPaletteSearch,
    Invoice,
    Lot,
    MCPCall,
    PageView,
)
from auctions.tests import StandardTestCase

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
CRAWLER = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


class Helpers(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_superuser("report_owner", "report_owner@example.com", "pw")

    def run_report(self, name, params=None):
        request = RequestFactory().get("/mcp/admin/")
        request.user = self.owner
        result = admin_reports.__dict__[name](request, params or {})
        self.assertTrue(result.get("ok"), result)
        return admin.private(result)

    def view(self, url, *, session="", user=None, referrer="", agent=BROWSER, title="", minutes_ago=0, source=""):
        row = PageView.objects.create(
            url=url,
            session_id=session or None,
            user=user,
            referrer=referrer,
            user_agent=agent,
            title=title,
            source=source,
        )
        if minutes_ago:
            PageView.objects.filter(pk=row.pk).update(
                date_start=timezone.now() - datetime.timedelta(minutes=minutes_ago)
            )
        return row


class ShapeTests(Helpers):
    def test_every_report_is_an_admin_read(self):
        for action in admin_reports.READS:
            self.assertIn(action.name, admin.ADMIN_TOOLS)
            self.assertIn(action.name, admin.INSTRUCTIONS)
            self.assertNotEqual(action.danger, "confirm")

    def test_every_report_runs_on_an_empty_window(self):
        for action in admin_reports.READS:
            with self.subTest(action.name):
                self.run_report(action.name)

    def test_path_family_hides_which_one(self):
        self.assertEqual(admin_reports.path_family("/lots/123/blue-shrimp/"), "/lots/<n>/*/")
        self.assertEqual(admin_reports.path_family("/auctions/spring-swap/lots/"), "/auctions/*/lots/")
        self.assertEqual(admin_reports.path_family("/"), "/")
        self.assertEqual(admin_reports.path_family(""), "(none)")

    def test_referrers_are_hosts_or_our_own_pages(self):
        own = admin_reports._own_host()
        self.assertEqual(admin_reports.referrer_label("", own), "(direct)")
        self.assertEqual(admin_reports.referrer_label("Google", own), "Google")
        self.assertEqual(admin_reports.referrer_label("someclub.org/auction/2026", own), "«someclub.org»")
        self.assertTrue(admin_reports.referrer_label(f"{own}/lots/5/x/?a=1", own).startswith("(this site) /lots/"))


class FunnelTests(Helpers):
    def test_signup_views_crawlers_and_depth(self):
        self.view("/signup/", session="s1", agent=BROWSER, referrer="Google")
        self.view("/lots/4/x/", session="s1")
        self.view("/signup/", session="s2", agent=CRAWLER)
        result = self.run_report("signup_funnel")
        self.assertEqual(result["views"], 2)
        self.assertEqual(result["signed_out_sessions"], 2)
        browsers = {row["browser"]: row["views"] for row in result["by_browser"]}
        self.assertEqual(browsers["(crawler)"], 1)
        self.assertEqual(result["sessions_by_pages_seen"], {"1 page": 1, "2-3 pages": 1})
        self.assertNotIn("example.com", json.dumps(result))

    def test_entry_pages_and_who_reached_signup(self):
        self.view("/lots/9/fish/", session="a", referrer="Google", minutes_ago=5)
        self.view("/signup/", session="a")
        self.view("/lots/10/other/", session="b")
        self.view("/lots/11/other/", session="c", agent=CRAWLER)
        result = self.run_report("entry_page_outcomes")
        self.assertEqual(result["sessions"], 2)
        self.assertEqual(result["crawler_sessions_left_out"], 1)
        page = result["by_first_page"][0]
        self.assertEqual(
            (page["first_page"], page["sessions"], page["reached_signup_or_login"]), ("/lots/<n>/*/", 2, 1)
        )
        sources = {row["came_from"]: row["reached_signup_or_login"] for row in result["by_source"]}
        self.assertEqual(sources["Google"], 1)


class ClubTests(Helpers):
    def test_retention_counts_a_second_auction(self):
        club = Club.objects.create(name="Retention club")
        past = timezone.now() - datetime.timedelta(days=400)
        for days in (0, 30):
            Auction.objects.create(
                created_by=self.user,
                title=f"R{days}",
                club=club,
                date_start=past + datetime.timedelta(days=days),
                date_end=past + datetime.timedelta(days=days + 1),
            )
        result = self.run_report("club_retention")
        cohort = next(row for row in result["cohorts"] if row["first_auction"] == admin_reports._quarter(past))
        self.assertGreaterEqual(cohort["ran_a_2nd"], 1)
        self.assertEqual(result["median_days_first_to_second"], 30)

    def test_unclaimed_people_counts_without_emails(self):
        club = Club.objects.create(name="Typed-in club")
        self.online_auction.club = club
        self.online_auction.save()
        nobody = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            email="Nobody@Elsewhere.test",
        )
        AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, manually_added=True, email=self.user.email
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=nobody, auction=self.online_auction)
        Invoice.objects.filter(pk=invoice.pk).update(opened=True)
        result = self.run_report("unclaimed_people")
        row = next(row for row in result["clubs"] if row["club"] == "Typed-in club")
        self.assertEqual((row["people"], row["opened_an_invoice_link"]), (1, 1))
        self.assertNotIn("elsewhere.test", json.dumps(result).lower())

    def test_referrers_without_club_leave_out_clubs_and_search_engines(self):
        Club.objects.create(name="Known", homepage="https://www.knownclub.org/")
        self.view("/", session="x", referrer="knownclub.org/auctions")
        self.view("/", session="y", referrer="Google")
        self.view("/lots/3/a/", session="z", referrer="newclub.org/swap")
        self.view("/", session="w", referrer="bing.com")
        self.view("/", session="v", referrer="planet.com/reef")
        self.view("/", session="u", referrer="t.co/abc")
        result = self.run_report("referrers_without_club")
        self.assertEqual(sorted(row["site"] for row in result["sites"]), ["«newclub.org»", "«planet.com»"])
        landed = {row["site"]: row["landed_on"] for row in result["sites"]}
        self.assertEqual(landed["«newclub.org»"], "/lots/<n>/*/")

    def test_a_capped_read_keeps_the_newest_rows(self):
        self.view("/old/", session="o", minutes_ago=60)
        self.view("/new/", session="n")
        with mock.patch.object(admin_reports, "MAX_ROWS", 1):
            rows, stopped = admin_reports._capped(PageView.objects.all(), ("url",))
        self.assertEqual((rows, stopped), ([("/new/",)], True))

    def test_seller_reach_finds_a_seller_in_two_clubs(self):
        one, two = Club.objects.create(name="Club one"), Club.objects.create(name="Club two")
        for club in (one, two):
            auction = Auction.objects.create(
                created_by=self.user,
                title=f"A {club.name}",
                club=club,
                date_start=timezone.now(),
                date_end=timezone.now() + datetime.timedelta(days=1),
            )
            Lot.objects.create(lot_name="fish", auction=auction, user=self.user_with_no_lots, quantity=1)
        loose = Auction.objects.create(
            created_by=self.user,
            title="Unfiled swap",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        Lot.objects.create(lot_name="plant", auction=loose, user=self.user_with_no_lots, quantity=1)
        result = self.run_report("seller_reach")
        row = next(row for row in result["sellers"] if row["user"] == self.user_with_no_lots.pk)
        self.assertEqual(row["clubs"], ["Club one", "Club two"])
        self.assertEqual(row["club_less_auctions"], [loose.slug])
        self.assertNotIn("Unfiled swap", json.dumps(result))
        self.assertNotIn("no_lots", json.dumps(result))

    def test_feature_adoption_lists_what_a_club_uses(self):
        club = Club.objects.create(name="BAP club", enable_breeder_award_program=True)
        Auction.objects.create(
            created_by=self.user,
            title="Recent",
            club=club,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=2),
        )
        result = self.run_report("feature_adoption_by_club")
        row = next(row for row in result["clubs"] if row["club"] == "BAP club")
        self.assertIn("breeder_award_program", row["uses"])
        self.assertIn("square", row["doesnt_use"])

    def test_setup_time_tells_copies_from_fresh(self):
        copy = Auction.objects.create(
            created_by=self.user,
            title="Copy",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        AuctionHistory.objects.create(auction=copy, action="Created auction by copying Original", applies_to="RULES")
        AuctionHistory.objects.create(
            auction=copy, action="Edited", applies_to="RULES", changed_fields={"date_start": {"from": 1, "to": 2}}
        )
        Lot.objects.create(lot_name="first", auction=copy, user=self.user, quantity=1)
        result = self.run_report("auction_setup_time")
        self.assertEqual(result["copied"]["auctions"], 1)
        self.assertIsNotNone(result["copied"]["median_hours_to_first_lot"])
        self.assertEqual(result["fields_changed_after_a_copy"], [{"field": "date_start", "times": 1}])


class DeadEndTests(Helpers):
    def test_the_404_page_is_titled_for_the_report(self):
        response = self.client.get("/no-such-page-anywhere/", HTTP_SEC_FETCH_DEST="document")
        self.assertEqual(response.status_code, 404)
        self.assertIn(admin_reports.NOT_FOUND_TITLE, response.content.decode())

    def test_404s_and_login_bounces(self):
        own = admin_reports._own_host()
        self.view("/None", session="n", title=admin_reports.NOT_FOUND_TITLE, referrer=f"{own}/signup/?next=None")
        self.view("/login/", session="m", referrer=f"{own}/lots/5/x/")
        self.view("/users/someone/", session="k", title=admin_reports.NOT_FOUND_TITLE)
        result = self.run_report("dead_ends")
        paths = {row["path"] for row in result["not_found_by_path"]}
        self.assertEqual(paths, {"«/None»", "«/users/*/»"})
        self.assertEqual(result["login_from_page"][0]["was_on"], "(this site) /lots/<n>/*/")

    def test_help_misses_group_and_recheck(self):
        for user in (self.user, self.admin_user):
            MCPCall.objects.create(
                user=user,
                tool="search_help",
                outcome=MCPCall.EMPTY,
                arguments=json.dumps({"query": "Zorblax quuxing works?"}),
            )
        bounce = CommandPaletteSearch.RESULT_BOUNCE
        CommandPaletteSearch.objects.create(user=self.user, search="zorblax quuxing works", result=bounce)
        CommandPaletteSearch.objects.create(user=self.user, search="Jane Doe", result=bounce)
        MCPCall.objects.create(
            user=self.user,
            tool="search_help",
            outcome=MCPCall.EMPTY,
            arguments=json.dumps({"query": "jane.doe@mail.test refund"}),
        )
        result = self.run_report("help_search_misses")
        text = json.dumps(result).lower()
        self.assertNotIn("jane doe", text)
        self.assertNotIn("mail test", text)
        row = result["phrases"][0]
        self.assertEqual((row["phrase"], row["times"], row["people"]), ("«zorblax quuxing works»", 3, 2))
        self.assertFalse(row["a_guide_answers_it_now"])
        self.assertEqual(result["still_unanswered"], 2)

    def test_a_shared_common_word_is_not_an_answer(self):
        from auctions import help_guides

        self.assertFalse(admin_reports.guide_answers(help_guides, "how do i zorblax"))
        self.assertTrue(admin_reports.guide_answers(help_guides, "preregistration discount"))


class PageVerbTests(Helpers):
    def request_for(self, user):
        request = RequestFactory().get("/command-palette/")
        request.user = user
        return request

    def test_auction_page_offers_add_a_lot(self):
        Auction.objects.filter(pk=self.online_auction.pk).update(
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=1),
        )
        items = command_palette.page_verb_items(self.request_for(self.user), f"/auctions/{self.online_auction.slug}/")
        self.assertEqual([item["title"] for item in items], ["Add a lot"])
        self.assertIn(f"auction={self.online_auction.slug}", items[0]["url"])
        groups = command_palette.default_items(
            self.request_for(self.user), path=f"/auctions/{self.online_auction.slug}/"
        )
        self.assertEqual(groups[0]["label"], "On this page")

    def test_no_verb_when_submission_is_closed_or_not_joined(self):
        Auction.objects.filter(pk=self.online_auction.pk).update(
            lot_submission_end_date=timezone.now() - datetime.timedelta(days=1)
        )
        path = f"/auctions/{self.online_auction.slug}/"
        self.assertEqual(command_palette.page_verb_items(self.request_for(self.user), path), [])
        self.assertEqual(command_palette.page_verb_items(self.request_for(self.user_who_does_not_join), path), [])
        self.assertEqual(command_palette.page_verb_items(self.request_for(self.user), "not a path"), [])
        self.assertEqual(command_palette.page_verb_items(self.request_for(self.user), "/no/such/page/"), [])

    def test_invoice_page_offers_mark_paid_to_its_admin_only(self):
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.online_tos, auction=self.online_auction)
        path = f"/invoices/{invoice.pk}/"
        with mock.patch("auctions.palette_assist.assist_enabled_for", return_value=True):
            items = command_palette.page_verb_items(self.request_for(self.user), path)
            bidder = self.online_tos.bidder_number
            self.assertEqual(
                [(item["type"], item["title"]) for item in items], [("example", f"mark bidder {bidder} paid")]
            )
            self.assertEqual(
                palette_routes.page_context_from_path(self.user, path)["auction"], self.online_auction.slug
            )
            self.assertNotIn("auction", palette_routes.page_context_from_path(self.user_who_does_not_join, path))
            with mock.patch("auctions.palette_assist.navigate_only", return_value=True):
                self.assertEqual(command_palette.page_verb_items(self.request_for(self.user), path), [])
            self.assertEqual(command_palette.page_verb_items(self.request_for(self.user_who_does_not_join), path), [])
            Invoice.objects.filter(pk=invoice.pk).update(status="PAID")
            self.assertEqual(command_palette.page_verb_items(self.request_for(self.user), path), [])

    def test_the_palette_view_passes_the_path(self):
        self.client.force_login(self.user)
        with mock.patch.object(command_palette, "page_verb_items", return_value=[]) as verbs:
            self.client.get("/command-palette/", {"q": "", "path": "/selling/"})
        self.assertEqual(verbs.call_args.args[1], "/selling/")


class ShortcutCandidateTests(Helpers):
    def test_palette_writes_are_counted_reads_left_out(self):
        from auctions.models import LLMUsage

        for kind in ("done", "done", "countdown"):
            LLMUsage.objects.create(user=self.user, action="add_person", success=True, response_kind=kind)
        LLMUsage.objects.create(user=self.user, action="describe_lot", success=True, response_kind="done")
        request = RequestFactory().get("/mcp/admin/")
        request.user = self.owner
        result = admin.palette_shortcut_candidates(request, {})
        self.assertEqual(result["actions_run"], [{"action": "add_person", "times": 2, "people": 1}])
