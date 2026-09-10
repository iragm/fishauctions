"""Phase 8: verifying the clubs we have, and finding the ones we do not.  Network mocked throughout.

Two rules are what these tests exist to hold, because both are the kind that decay silently:

* **Nothing a machine found is ever published.**  Everything ingested lands at ``PROSPECT``, which
  is the map gate.  A regression here would put a crawler's guesses on a map this site treats as
  authoritative.
* **A club is never declared dead on one failed fetch.**  A timeout is a slow host and a 403 is a
  bot filter; neither is evidence about whether a club exists.

See auctions/club_verification.py and auctions/club_discovery.py.
"""

import datetime
from unittest.mock import patch

import requests
from django.test import TestCase
from django.utils import timezone

from auctions import club_discovery, club_verification
from auctions.club_discovery import (
    Crawler,
    FoundClub,
    domain_of,
    extract_clubs,
    find_existing,
    ingest,
    is_a_club_host,
    is_an_umbrella,
    links_on_page,
    looks_like_a_club,
    search_city,
    unwrap_redirect,
)
from auctions.club_verification import (
    FetchResult,
    clubs_due_for_verification,
    dead_candidates,
    fetch,
    looks_dead,
    verify_club,
)
from auctions.llm import LLMResult, set_provider_override
from auctions.models import Auction, Club, ClubMember
from auctions.test_species import FakeProvider


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FetchTests(TestCase):
    """The one network call in phase 8, and what each kind of failure is allowed to mean."""

    def _fetch(self, **kwargs):
        with patch("requests.get", **kwargs) as mocked:
            return fetch("https://club.example", want_text=True), mocked

    def test_a_page_that_answers_is_reachable(self):
        result, _ = self._fetch(return_value=FakeResponse(200, "<html>hi</html>"))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "<html>hi</html>")

    def test_a_404_is_evidence_the_page_is_gone(self):
        result, _ = self._fetch(return_value=FakeResponse(404))
        self.assertIs(result.reachable, False)

    def test_a_500_is_not_evidence_of_anything(self):
        result, _ = self._fetch(return_value=FakeResponse(500))
        self.assertIsNone(result.reachable)

    def test_a_403_from_a_bot_filter_is_not_evidence_of_anything(self):
        result, _ = self._fetch(return_value=FakeResponse(403))
        self.assertIsNone(result.reachable)

    def test_a_dead_domain_is_evidence(self):
        result, _ = self._fetch(side_effect=requests.exceptions.ConnectionError("no such host"))
        self.assertIs(result.reachable, False)

    def test_a_timeout_is_a_slow_host_and_nothing_more(self):
        result, _ = self._fetch(side_effect=requests.exceptions.Timeout("slow"))
        self.assertIsNone(result.reachable)

    def test_a_broken_certificate_is_not_a_dead_club(self):
        result, _ = self._fetch(side_effect=requests.exceptions.SSLError("expired"))
        self.assertIsNone(result.reachable)

    def test_a_bare_hostname_is_given_a_scheme(self):
        with patch("requests.get", return_value=FakeResponse(200)) as mocked:
            fetch("club.example")
        self.assertEqual(mocked.call_args[0][0], "https://club.example")

    def test_no_url_is_not_a_request(self):
        with patch("requests.get") as mocked:
            self.assertIsNone(fetch("").reachable)
        mocked.assert_not_called()

    def test_it_identifies_itself(self):
        with patch("requests.get", return_value=FakeResponse(200)) as mocked:
            fetch("https://club.example")
        self.assertIn("auction.fish", mocked.call_args.kwargs["headers"]["User-Agent"])


class VerifyClubTests(TestCase):
    def test_it_records_what_answered(self):
        club = Club.objects.create(name="Checked Society", homepage="https://checked.example")
        with patch("requests.get", return_value=FakeResponse(200)):
            verify_club(club)
        club.refresh_from_db()
        self.assertTrue(club.homepage_reachable)
        self.assertIsNotNone(club.date_links_checked)
        self.assertIn("homepage", club.link_check_note)

    def test_a_club_with_no_links_is_still_stamped_as_checked(self):
        """Otherwise it looks like the run skipped it, and it never reaches anybody."""
        club = Club.objects.create(name="Linkless Society")
        with patch("requests.get") as mocked:
            verify_club(club)
        mocked.assert_not_called()
        club.refresh_from_db()
        self.assertIsNotNone(club.date_links_checked)
        self.assertIsNone(club.homepage_reachable)
        self.assertEqual(club.link_check_note, "no links on file")

    def test_the_facebook_page_is_checked_too(self):
        club = Club.objects.create(name="Social Society", facebook_page="https://facebook.com/social")
        with patch("requests.get", return_value=FakeResponse(404)):
            verify_club(club)
        club.refresh_from_db()
        self.assertIs(club.facebook_reachable, False)


class DueForVerificationTests(TestCase):
    def test_never_checked_comes_first(self):
        recent = Club.objects.create(name="Recent Society", date_links_checked=timezone.now())
        never = Club.objects.create(name="Never Society")
        due = list(clubs_due_for_verification())
        self.assertEqual(due[0], never)
        self.assertNotIn(recent, due)

    def test_a_club_checked_over_a_year_ago_is_due_again(self):
        stale = Club.objects.create(
            name="Stale Society",
            date_links_checked=timezone.now() - datetime.timedelta(days=club_verification.CHECK_INTERVAL_DAYS + 1),
        )
        self.assertIn(stale, clubs_due_for_verification())


class LooksDeadTests(TestCase):
    def _checked(self, **kwargs):
        return Club.objects.create(name="Quiet Aquarium Society", date_links_checked=timezone.now(), **kwargs)

    def test_every_signal_absent_is_the_only_thing_that_counts(self):
        dead, _reason = looks_dead(self._checked())
        self.assertTrue(dead)

    def test_a_club_nobody_has_verified_is_never_nominated(self):
        dead, reason = looks_dead(Club.objects.create(name="Unchecked Aquarium Society"))
        self.assertFalse(dead)
        self.assertEqual(reason, "not verified yet")

    def test_a_reachable_homepage_keeps_it_alive(self):
        dead, _reason = looks_dead(self._checked(homepage="https://alive.example", homepage_reachable=True))
        self.assertFalse(dead)

    def test_a_fetch_that_failed_for_an_unknown_reason_keeps_it_alive(self):
        """The rule that stops one bad afternoon un-listing a club."""
        club = self._checked(homepage="https://slow.example", homepage_reachable=None)
        dead, reason = looks_dead(club)
        self.assertFalse(dead)
        self.assertIn("could not be checked", reason)

    def test_an_auction_here_keeps_it_alive(self):
        club = self._checked()
        Auction.objects.create(title="Still going", club=club, date_start=timezone.now())
        dead, reason = looks_dead(club)
        self.assertFalse(dead)
        self.assertIn("auctions", reason)

    def test_a_member_here_keeps_it_alive(self):
        club = self._checked()
        ClubMember.objects.create(club=club, name="Somebody")
        self.assertFalse(looks_dead(club)[0])

    def test_dead_candidates_never_writes_anything(self):
        club = self._checked()
        self.assertEqual([row[0] for row in dead_candidates()], [club])
        club.refresh_from_db()
        self.assertTrue(club.active)


class NameAndHostTests(TestCase):
    def test_a_domain_ignores_www_and_the_scheme(self):
        self.assertEqual(domain_of("https://www.Club.Example/page"), "club.example")
        self.assertEqual(domain_of("club.example"), "club.example")
        self.assertEqual(domain_of(""), "")

    def test_social_sites_are_never_a_club_homepage(self):
        for url in ("https://facebook.com/a", "https://www.youtube.com/x", "https://m.facebook.com/y"):
            self.assertFalse(is_a_club_host(url), url)
        self.assertTrue(is_a_club_host("https://gsas.example"))

    def test_club_names_are_recognised(self):
        for name in (
            "Greater Seattle Aquarium Society",
            "Boston Fish Club",
            "Ohio Cichlid Association",
            "Aquatic Gardeners Society",
        ):
            self.assertTrue(looks_like_a_club(name), name)

    def test_clubs_that_do_not_call_themselves_a_society(self):
        """All four were dropped by the first real run over the AGA's list, and all four are clubs.

        "Enthusiasts" is as ordinary a word for a club as "society" is, and "Aquaria" is not the
        word "aquarium" -- which a pattern written from memory rather than from a real page missed.
        """
        for name in (
            "Arizona Aquatic Plant Enthusiasts",
            "Southern California Aquatic Plant Enthusiasts",
            "London Aquaria Society (Ontario)",
            "Malaysian Aquascaping Club",
            "Champaign Area Fish Exchange",
            "California's Organization of Aquatic Show Tropicals",
            # Puerto Rico and Quebec are in scope, and neither names itself in English.
            "Asociacion Acuaristas Metro Este Puerto Rico",
            "Societe D'Aquariophilie de Montreal",
        ):
            self.assertTrue(looks_like_a_club(name), name)

    def test_widening_the_pattern_did_not_let_the_umbrellas_back_in(self):
        """Every widening above was a real club found in a skip list, so this is the other half."""
        for name in (
            "Federation of American Aquarium Societies",
            "Northeast Council of Aquarium Societies",
            "Canadian Association of Aquarium Clubs",
        ):
            self.assertTrue(is_an_umbrella(name), name)
            self.assertFalse(looks_like_a_club(name), name)

    def test_things_that_are_not_clubs_are_not(self):
        for name in (
            "Home",
            "Contact us",
            "Big Al's Pet Store",
            "Sponsors",
            "",
            # A trade body, on the AGA's list among the clubs.
            "Pet Industry Joint Advisory Council",
        ):
            self.assertFalse(looks_like_a_club(name), name)

    def test_national_bodies_are_not_clubs(self):
        """The first real run against NANFA returned the AKA and the ALA alongside one real club.

        Umbrella directories link to each other, so these are over-represented in exactly the pages
        this module reads, and every one of them reads like a club to ``_CLUB_NAME``.
        """
        for name in (
            "American Livebearer Association",
            "American Killifish Association",
            "American Cichlid Association",
            "North American Native Fishes Association",
            "Federation of American Aquarium Societies",
            "Northeast Council of Aquarium Societies",
        ):
            self.assertTrue(is_an_umbrella(name), name)
            self.assertFalse(looks_like_a_club(name), name)

    def test_a_local_club_is_not_mistaken_for_a_national_body(self):
        """The shape is "country, fish, organisation", and local names get close to it.

        ``National`` is not one of the words that triggers this for exactly that reason: a club that
        meets in a city called National City is a club.
        """
        for name in (
            "National Capital Aquarium Society",
            "Southern New England Killifish Association",
            "Greater City Aquarium Society",
            "Potomac Valley Aquarium Society",
        ):
            self.assertFalse(is_an_umbrella(name), name)
            self.assertTrue(looks_like_a_club(name), name)

    def test_a_page_on_a_directory_site_is_that_directory(self):
        """A fact rather than a shape: we know what the umbrella sites are, we read them."""
        self.assertTrue(is_an_umbrella("Anything At All", "https://www.nanfa.org/links.shtml"))
        self.assertTrue(is_an_umbrella("Anything At All", "https://aka.org"))
        self.assertFalse(is_an_umbrella("Colorado Aquarium Society", "http://www.coloradoaquarium.org/"))

    def test_dropping_a_source_does_not_make_it_a_club(self):
        """NEC, the ALA and FAAS are no longer read, for three unrelated reasons.

        None of those reasons is "it turned out to be a local club", so their pages must still be
        filtered out when some other source links to them -- which those sources do constantly.
        """
        for url in ("https://livebearers.org/links/", "https://www.northeastcouncil.org/", "https://faas.info/"):
            self.assertTrue(is_an_umbrella("Some Aquarium Society", url), url)


class LinksOnPageTests(TestCase):
    def test_relative_links_are_made_absolute(self):
        html = '<a href="/links.html">Links</a><a href="#x">skip</a><a href="mailto:a@b">skip</a>'
        self.assertEqual(
            links_on_page(html, "https://club.example/index.html"), [("Links", "https://club.example/links.html")]
        )


class ExtractClubsTests(TestCase):
    def tearDown(self):
        set_provider_override(None)
        super().tearDown()

    def test_it_reads_clubs_off_a_listing_page(self):
        set_provider_override(
            FakeProvider(
                [
                    {
                        "clubs": [
                            {
                                "name": "Greater Seattle Aquarium Society",
                                "homepage": "https://gsas.example",
                                "location": "Seattle WA",
                            }
                        ]
                    }
                ]
            )
        )
        found = extract_clubs("<html>...</html>", "https://faas.example/members")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].homepage, "https://gsas.example")

    def test_a_social_url_is_not_kept_as_a_homepage(self):
        set_provider_override(
            FakeProvider([{"clubs": [{"name": "Boston Aquarium Society", "homepage": "https://facebook.com/bas"}]}])
        )
        self.assertEqual(extract_clubs("<html/>", "https://faas.example")[0].homepage, "")

    def test_nonsense_from_the_model_produces_nothing_rather_than_an_error(self):
        set_provider_override(FakeProvider([{"not_clubs": "what"}]))
        self.assertEqual(extract_clubs("<html/>", "https://faas.example"), [])

    def test_no_llm_configured_is_not_a_crash(self):
        self.assertEqual(extract_clubs("<html/>", "https://faas.example"), [])


class FindExistingTests(TestCase):
    def test_the_domain_wins_over_the_name(self):
        """Two spellings of one club, and the domain is the fact that settles it."""
        club = Club.objects.create(name="Aquarium Society of Virginia", homepage="https://asv.example")
        found = FoundClub(name="The Virginia Aquarium Society, Inc.", homepage="https://www.asv.example/home")
        self.assertEqual(find_existing(found, [club]), club)

    def test_a_close_name_with_no_domain_still_matches(self):
        club = Club.objects.create(name="Greater Seattle Aquarium Society")
        self.assertEqual(find_existing(FoundClub(name="GSAS"), [club]), club)

    def test_two_different_clubs_do_not_match(self):
        club = Club.objects.create(name="Boston Aquarium Society", homepage="https://bas.example")
        self.assertIsNone(find_existing(FoundClub(name="Seattle Aquarium Society"), [club]))


class IngestTests(TestCase):
    def test_everything_found_lands_as_a_prospect(self):
        """The map gate. A crawler's guess must never appear anywhere public."""
        report = ingest([FoundClub(name="New Aquarium Society", homepage="https://new.example")], source="test")
        club = report.created[0]
        self.assertEqual(club.outreach_stage, Club.PROSPECT)
        self.assertNotIn(club, Club.objects.listed())

    def test_it_says_where_a_club_came_from(self):
        report = ingest([FoundClub(name="Traced Aquarium Society")], source="faas")
        self.assertIn("faas", report.created[0].notes)

    def test_a_name_that_is_not_a_club_is_skipped(self):
        report = ingest([FoundClub(name="Sponsors", homepage="https://x.example")], source="test")
        self.assertEqual(report.created, [])
        self.assertEqual(len(report.skipped), 1)

    def test_a_blank_field_on_a_known_club_is_filled_in(self):
        club = Club.objects.create(name="Known Aquarium Society")
        ingest([FoundClub(name="Known Aquarium Society", homepage="https://known.example")], source="test")
        club.refresh_from_db()
        self.assertEqual(club.homepage, "https://known.example")

    def test_what_a_person_typed_is_never_overwritten(self):
        club = Club.objects.create(name="Known Aquarium Society", homepage="https://correct.example")
        ingest([FoundClub(name="Known Aquarium Society", homepage="https://wrong.example")], source="test")
        club.refresh_from_db()
        self.assertEqual(club.homepage, "https://correct.example")

    def test_running_it_twice_creates_one_club(self):
        found = [FoundClub(name="Idempotent Aquarium Society", homepage="https://idem.example")]
        ingest(found, source="test")
        second = ingest(found, source="test")
        self.assertEqual(second.created, [])
        self.assertEqual(Club.objects.filter(name="Idempotent Aquarium Society").count(), 1)


class CrawlerTests(TestCase):
    """A crawler you can run rather than one you have to watch."""

    def _pages(self, mapping):
        def fake_fetch(url, timeout=None, want_text=False):
            if url.endswith("/robots.txt"):
                return FetchResult(url=url, reachable=True, note="ok", text=mapping.get("robots", ""))
            body = mapping.get(url)
            if body is None:
                return FetchResult(url=url, reachable=False, note="http 404")
            return FetchResult(url=url, reachable=True, note="ok", text=body)

        return fake_fetch

    def test_it_finds_clubs_linked_from_a_seed(self):
        pages = {
            "https://seed.example/links": '<a href="https://gsas.example">Greater Seattle Aquarium Society</a>',
            "https://gsas.example": "<html/>",
        }
        with patch.object(club_discovery, "fetch", self._pages(pages)):
            found = Crawler(delay=0).crawl(["https://seed.example/links"])
        self.assertEqual([club.name for club in found], ["Greater Seattle Aquarium Society"])

    def test_robots_txt_is_obeyed(self):
        pages = {
            "robots": "User-agent: *\nDisallow: /",
            "https://seed.example/links": '<a href="https://gsas.example">Greater Seattle Aquarium Society</a>',
        }
        with patch.object(club_discovery, "fetch", self._pages(pages)):
            found = Crawler(delay=0).crawl(["https://seed.example/links"])
        self.assertEqual(found, [])

    def test_the_budget_is_spent_and_no_more(self):
        pages = {f"https://club{index}.example": "<html/>" for index in range(10)}
        with patch.object(club_discovery, "fetch", self._pages(pages)):
            crawler = Crawler(budget=3, delay=0)
            crawler.crawl(list(pages))
        self.assertEqual(crawler.spent, 3)

    def test_a_page_is_never_fetched_twice(self):
        pages = {"https://seed.example": '<a href="https://seed.example">Aquarium Society</a>'}
        with patch.object(club_discovery, "fetch", self._pages(pages)):
            crawler = Crawler(delay=0)
            crawler.crawl(["https://seed.example", "https://seed.example"])
        self.assertEqual(crawler.spent, 1)

    def test_it_stops_at_two_hops(self):
        pages = {
            "https://a.example": '<a href="https://b.example">B Aquarium Society</a>',
            "https://b.example": '<a href="https://c.example">C Aquarium Society</a>',
            "https://c.example": '<a href="https://d.example">D Aquarium Society</a>',
        }
        with patch.object(club_discovery, "fetch", self._pages(pages)):
            crawler = Crawler(delay=0, max_hops=2)
            found = crawler.crawl(["https://a.example"])
        self.assertIn("https://c.example", crawler.seen)
        self.assertNotIn("https://d.example", crawler.seen)
        # D is named on a page we did fetch, so its name is still worth having even though the
        # crawl stopped before following it.
        self.assertIn("D Aquarium Society", [club.name for club in found])


class SearchCityTests(TestCase):
    def test_a_tracking_link_is_resolved_back_to_the_club(self):
        """Without this every result is thrown away as "not a club site" and the source finds nothing."""
        self.assertEqual(
            unwrap_redirect("//duckduckgo.com/l/?uddg=https%3A%2F%2Fbas.example%2F&rut=x"),
            "https://bas.example/",
        )

    def test_an_ordinary_link_is_left_alone(self):
        self.assertEqual(unwrap_redirect("https://bas.example/"), "https://bas.example/")

    def test_it_keeps_club_shaped_results_and_drops_the_rest(self):
        html = (
            '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fbas.example">Boston Aquarium Society</a>'
            '<a href="https://facebook.com/bas">Boston Aquarium Society on Facebook</a>'
            '<a href="https://petstore.example">Big Al\'s</a>'
        )
        with patch.object(
            club_discovery,
            "fetch",
            lambda url, **kwargs: FetchResult(url=url, reachable=True, note="ok", text=html),
        ):
            found = search_city("Boston MA", terms=("aquarium society",))
        self.assertEqual([club.homepage for club in found], ["https://bas.example"])
        self.assertEqual(found[0].location, "Boston MA")

    def test_an_engine_that_says_nothing_is_a_normal_outcome(self):
        with patch.object(
            club_discovery,
            "fetch",
            lambda url, **kwargs: FetchResult(url=url, reachable=None, note="http 429"),
        ):
            self.assertEqual(search_city("Boston MA", terms=("aquarium society",)), [])


class DirectoryRegistryTests(TestCase):
    def test_every_directory_is_distinct_and_addressable(self):
        keys = [directory.key for directory in club_discovery.DIRECTORIES]
        self.assertEqual(len(keys), len(set(keys)))
        for directory in club_discovery.DIRECTORIES:
            self.assertTrue(directory.url.startswith("https://"), directory.key)
            self.assertTrue(directory.name)


class LLMResultShapeTests(TestCase):
    def test_the_fake_provider_matches_the_real_one(self):
        self.assertIsInstance(LLMResult(data={"clubs": []}).data, dict)
