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

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import Club, ClubEvent, ClubMember, GeneralInterest

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

    def test_the_map_payload_carries_only_what_a_pin_needs(self):
        rows = map_payload(self.client.get(reverse("clubs")))
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), {"slug", "name", "lat", "lng"})

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
