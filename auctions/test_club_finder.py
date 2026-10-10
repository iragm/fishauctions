"""Tests for the public club finder: what it lists, and what it refuses to say about a club.

Two things are being held down here.

The **listing rule** is ``Club.objects.listed()``, and it is asserted in three places rather than
one, because the finder now has three ways to name a club -- the table, the map payload and the
card -- and a club nobody has approved must appear in none of them. A page that grew a second
surface and only gated the first is exactly how this would go wrong.

The **privacy rule** is that everything on this page is something the club's own public page
already shows. Most of the tests below are therefore assertions of *absence*: no street address
(this page has only ever published a pin), no contact address, no members. The filters get the same
treatment, because a filter is a way of reading a field one yes/no answer at a time.
"""

import datetime
import json
import re
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import AgentProposal, Club, ClubEvent, ClubMember, GeneralInterest

MAP_PAYLOAD_RE = re.compile(r'id="club-map-data"[^>]*>(.*?)</script>', re.DOTALL)


def map_payload(response):
    """The clubs the map would draw, read back out of the page's json_script block."""
    match = MAP_PAYLOAD_RE.search(response.content.decode())
    if not match:
        return []
    return json.loads(match.group(1))


@override_settings(ENABLE_CLUB_FINDER=True)
class ClubFinderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cichlids = GeneralInterest.objects.create(name="Cichlids")
        cls.plants = GeneralInterest.objects.create(name="Plants")
        cls.listed = Club.objects.create(
            name="Listed Aquarium Society",
            abbreviation="LAS",
            outreach_stage=Club.LISTED,
            latitude=42.0,
            longitude=-73.0,
            location="12 Private Street, Albany NY",
            contact_email="secretary@example.com",
            notes="Told us they use paper, try again in spring",
            homepage="example.com",
            allow_joining=True,
        )
        cls.listed.interests.add(cls.cichlids)
        cls.plant_club = Club.objects.create(
            name="Planted Tank Club",
            outreach_stage=Club.LISTED,
            latitude=44.0,
            longitude=-73.0,
        )
        cls.plant_club.interests.add(cls.plants)
        cls.prospect = Club.objects.create(
            name="Prospect Aquarium Society",
            outreach_stage=Club.PROSPECT,
            latitude=42.1,
            longitude=-73.1,
        )
        cls.folded = Club.objects.create(
            name="Folded Aquarium Society",
            outreach_stage=Club.LISTED,
            active=False,
            latitude=42.2,
            longitude=-73.2,
        )

    def test_listed_clubs_are_on_the_list_and_others_are_not(self):
        response = self.client.get(reverse("clubs"))
        self.assertContains(response, "Listed Aquarium Society")
        self.assertNotContains(response, "Prospect Aquarium Society")
        self.assertNotContains(response, "Folded Aquarium Society")

    def test_the_map_payload_is_gated_the_same_way_the_list_is(self):
        names = [row["name"] for row in map_payload(self.client.get(reverse("clubs")))]
        self.assertIn("Listed Aquarium Society", names)
        self.assertNotIn("Prospect Aquarium Society", names)
        self.assertNotIn("Folded Aquarium Society", names)

    def test_the_finder_is_public(self):
        """No login: a club finder that needs an account can't find anybody a club."""
        self.assertEqual(self.client.get(reverse("clubs")).status_code, 200)

    def test_a_row_links_straight_to_the_club_page(self):
        """No card: the club's own page is the only place a club is described."""
        response = self.client.get(reverse("clubs"))
        self.assertContains(response, f"href='{reverse('club_detail', kwargs={'slug': self.listed.slug})}'")

    def test_the_finder_does_not_publish_the_address_the_contact_address_or_the_notes(self):
        """The map has only ever shown a pin, the club page draws its email button for signed-in
        visitors only, and notes are a record of our conversations with the club."""
        for query in ("", "within 50 miles", "joinable"):
            response = self.client.get(reverse("clubs"), {"query": query})
            self.assertNotContains(response, "12 Private Street", msg_prefix=query)
            self.assertNotContains(response, "secretary@example.com", msg_prefix=query)
            self.assertNotContains(response, "try again in spring", msg_prefix=query)

    def test_no_member_is_named_by_the_finder(self):
        ClubMember.objects.create(club=self.listed, name="Wilma Fingerdoo", email="wilma@example.com")
        response = self.client.get(reverse("clubs"))
        self.assertNotContains(response, "Wilma Fingerdoo")
        self.assertNotContains(response, "wilma@example.com")

    @override_settings(
        # No hyphen: escapejs writes one as -, which is the same string to JavaScript but not to
        # assertContains.
        LOCATION_FIELD={**settings.LOCATION_FIELD, "provider.google.api_key": "testkey123"},
        GOOGLE_MAPS_MAP_ID="abc123",
    )
    def test_the_map_uses_googles_bootstrap_loader_and_a_map_id(self):
        """Per Google's docs: the dynamic library import bootstrap loader is the recommended way to
        load the API, and advanced markers cannot load without a Map ID. The key is pinned because
        the loader is only included when there is one, and CI has none."""
        response = self.client.get(reverse("clubs"))
        self.assertContains(response, 'l="importLibrary"', count=1)
        self.assertContains(response, 'key: "testkey123"')
        self.assertContains(response, "mapId: 'abc123'")
        # The direct script-tag loader is not on the page as well: the API only loads once.
        self.assertNotContains(response, "maps.googleapis.com/maps/api/js?key=")

    def test_the_map_payload_carries_only_what_the_club_page_shows(self):
        """The info window is a second public surface: every field in it is one the club page
        already shows a signed-out visitor. Adding a field here means checking that first."""
        rows = map_payload(self.client.get(reverse("clubs")))
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), {"slug", "name", "lat", "lng", "homepage", "facebook", "interests"})

    def test_a_pin_lists_the_clubs_links_and_interests(self):
        Club.objects.filter(pk=self.listed.pk).update(facebook_page="https://facebook.com/las")
        self.listed.interests.add(self.plants)
        row = next(row for row in map_payload(self.client.get(reverse("clubs"))) if row["slug"] == self.listed.slug)
        # Prefixed the way the club page's own Website button is.
        self.assertEqual(row["homepage"], "https://example.com")
        self.assertEqual(row["facebook"], "https://facebook.com/las")
        self.assertEqual(row["interests"], ["Cichlids", "Plants"])
        bare = next(
            row for row in map_payload(self.client.get(reverse("clubs"))) if row["slug"] == self.plant_club.slug
        )
        self.assertEqual((bare["homepage"], bare["facebook"]), ("", ""))

    def test_a_typed_in_script_url_is_not_a_live_link(self):
        Club.objects.filter(pk=self.listed.pk).update(homepage="javascript:alert(1)")
        row = next(row for row in map_payload(self.client.get(reverse("clubs"))) if row["slug"] == self.listed.slug)
        self.assertEqual(row["homepage"], "https://javascript:alert(1)")

    def test_searching_matches_the_name_and_the_abbreviation(self):
        for query in ("Listed", "LAS"):
            response = self.client.get(reverse("clubs"), {"query": query})
            self.assertContains(response, "Listed Aquarium Society", msg_prefix=query)
            self.assertNotContains(response, "Planted Tank Club", msg_prefix=query)

    def test_filtering_by_interest(self):
        response = self.client.get(reverse("clubs"), {"interest": str(self.plants.pk)})
        self.assertContains(response, "Planted Tank Club")
        self.assertNotContains(response, "Listed Aquarium Society")

    def test_the_interest_menu_only_offers_interests_a_listed_club_has(self):
        """An interest nobody listed is into would be a filter that finds nothing.

        It would also be a small statement about the clubs we have not published, which is the
        thing this page is not for.
        """
        secret = GeneralInterest.objects.create(name="Axolotls")
        self.prospect.interests.add(secret)
        response = self.client.get(reverse("clubs"))
        self.assertContains(response, "Cichlids")
        self.assertNotContains(response, "Axolotls")

    def test_the_joinable_keyword(self):
        response = self.client.get(reverse("clubs"), {"query": "joinable"})
        self.assertContains(response, "Listed Aquarium Society")
        self.assertNotContains(response, "Planted Tank Club")

    def test_the_website_keyword(self):
        response = self.client.get(reverse("clubs"), {"query": "website"})
        self.assertContains(response, "Listed Aquarium Society")
        self.assertNotContains(response, "Planted Tank Club")

    def test_the_events_keyword(self):
        ClubEvent.objects.create(
            club=self.plant_club,
            title="Monthly meeting",
            date_start=timezone.now() + datetime.timedelta(days=7),
        )
        response = self.client.get(reverse("clubs"), {"query": "events"})
        self.assertContains(response, "Planted Tank Club")
        self.assertNotContains(response, "Listed Aquarium Society")

    def test_a_radius_typed_into_the_search_box(self):
        """ "within 50 miles" is a filter, not a text search -- and it needs a location to mean anything."""
        self.client.cookies["latitude"] = "42.0"
        self.client.cookies["longitude"] = "-73.0"
        response = self.client.get(reverse("clubs"), {"query": "within 50 miles"})
        self.assertContains(response, "Listed Aquarium Society")
        # Two degrees of latitude is about 138 miles.
        self.assertNotContains(response, "Planted Tank Club")

    def test_a_radius_with_no_location_set_does_not_empty_the_page(self):
        response = self.client.get(reverse("clubs"), {"query": "within 50 miles"})
        self.assertContains(response, "Listed Aquarium Society")

    @override_settings(ENABLE_CLUB_FINDER=False)
    def test_the_finder_can_be_turned_off(self):
        response = self.client.get(reverse("clubs"))
        self.assertEqual(response.status_code, 302)


@override_settings(ENABLE_CLUB_FINDER=True)
class ClubAddTests(TestCase):
    """A member adding their own club: off the finder, theirs to set up, and in the owner's approvals."""

    PLACE = {"latitude": 42.36, "longitude": -71.06, "coordinates": "42.36,-71.06", "address": "Boston, MA, USA"}

    @classmethod
    def setUpTestData(cls):
        cls.member = User.objects.create_user("club_founder", "founder@example.com", "testpassword")
        cls.owner = User.objects.create_superuser("site_owner", "owner@example.com", "testpassword")

    def add(self, **fields):
        self.client.force_login(self.member)
        data = {"name": "Harbor Aquarium Society", "homepage": "harboraquarium.example.org", "location": "Boston"}
        data.update(fields)
        with mock.patch("auctions.geocoding.geocode", return_value=self.PLACE):
            return self.client.post(reverse("club_add"), data)

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        response = self.client.get(reverse("club_add"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_an_added_club_is_off_the_finder_and_its_adder_runs_it(self):
        response = self.add()
        club = Club.objects.get(name="Harbor Aquarium Society")
        self.assertRedirects(response, reverse("club_setup", kwargs={"slug": club.slug}), fetch_redirect_response=False)
        self.assertEqual(club.outreach_stage, Club.PROSPECT)
        self.assertAlmostEqual(club.latitude, 42.36)
        self.assertEqual(club.homepage, "https://harboraquarium.example.org")
        self.assertTrue(ClubMember.objects.get(club=club, user=self.member).permission_admin)
        self.assertNotIn(club, Club.objects.listed())
        self.assertEqual(self.client.get(reverse("club_setup", kwargs={"slug": club.slug})).status_code, 200)

    def test_approving_the_proposal_puts_it_on_the_map(self):
        self.add()
        proposal = AgentProposal.objects.get(status=AgentProposal.STATUS_PENDING)
        self.assertEqual(proposal.proposed_by, self.member)
        self.client.force_login(self.owner)
        self.client.post(reverse("agent_proposals"), {"pk": proposal.pk, "decision": "approve"})
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AgentProposal.STATUS_APPLIED, proposal.results)
        self.assertEqual(Club.objects.get(name="Harbor Aquarium Society").outreach_stage, Club.LISTED)

    def test_rejecting_it_leaves_it_off_the_map(self):
        self.add()
        proposal = AgentProposal.objects.get()
        self.client.force_login(self.owner)
        self.client.post(reverse("agent_proposals"), {"pk": proposal.pk, "decision": "reject"})
        self.assertEqual(Club.objects.get(name="Harbor Aquarium Society").outreach_stage, Club.PROSPECT)

    def test_a_club_already_here_is_refused_without_naming_an_unlisted_one(self):
        Club.objects.create(name="Harbor Aquarium Society", outreach_stage=Club.LISTED)
        self.assertContains(self.add(), "Harbor Aquarium Society is already on the site.")
        Club.objects.create(name="Bay Killifish Association", outreach_stage=Club.PROSPECT)
        response = self.add(name="Bay Killifish Association", homepage="")
        self.assertContains(response, "already waiting for approval")
        self.assertEqual(Club.objects.count(), 2)
        self.assertFalse(AgentProposal.objects.exists())

    def test_a_facebook_page_is_not_a_website(self):
        response = self.add(homepage="https://www.facebook.com/groups/harbor")
        self.assertContains(response, "goes under Facebook")
        self.assertFalse(Club.objects.exists())

    def test_one_person_has_only_a_few_clubs_waiting(self):
        from auctions.club_onboarding import MAX_WAITING

        names = ["Harbor Aquarium Society", "Prairie Cichlid Club", "Mountain Killifish Group", "Delta Reef Keepers"]
        for name in names[:MAX_WAITING]:
            self.add(name=name, homepage="")
        response = self.add(name=names[MAX_WAITING], homepage="")
        self.assertContains(response, "still waiting for approval")
        self.assertEqual(Club.objects.count(), MAX_WAITING)

    def test_the_finder_links_to_it(self):
        self.assertContains(self.client.get(reverse("clubs")), reverse("club_add"))
