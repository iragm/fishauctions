"""Clubs that keep something other than fish: what their interests change.

A club whose interests are all non-aquatic gets three things: new auctions start with species
suggestions off, lots take only its interests' categories, and its own noun where the site says fish.
A club with no interests, or any aquatic one, is untouched.
"""

import datetime

from django import forms
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.forms import narrow_categories
from auctions.models import Auction, Category, Club, GeneralInterest, Lot
from auctions.services import finish_new_auction


class ClubInterestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("organizer", "organizer@example.com", "testpassword")
        cls.uncategorized = Category.objects.get_or_create(name="Uncategorized")[0]
        cls.cichlids = Category.objects.create(name="Cichlids test")
        cls.snakes = Category.objects.create(name="Snakes test")
        cls.lizards = Category.objects.create(name="Lizards test")
        cls.reptiles = GeneralInterest.objects.create(name="Reptiles test", aquatic=False, noun="reptile")
        cls.reptiles.categories.set([cls.snakes, cls.lizards])
        cls.fishy = GeneralInterest.objects.create(name="Cichlids interest")
        cls.reptile_club = Club.objects.create(name="Herp Society of Somewhere")
        cls.reptile_club.interests.add(cls.reptiles)
        cls.fish_club = Club.objects.create(name="Aquarium Society of Somewhere")
        cls.fish_club.interests.add(cls.fishy)
        cls.mixed_club = Club.objects.create(name="Everything Keepers of Somewhere")
        cls.mixed_club.interests.add(cls.fishy, cls.reptiles)
        cls.plain_club = Club.objects.create(name="Club With No Interests")

    def auction_for(self, club, **fields):
        start = timezone.now() + datetime.timedelta(days=1)
        return Auction.objects.create(
            created_by=self.user,
            title=f"{club.name} auction",
            club=club,
            date_start=start,
            date_end=start + datetime.timedelta(days=1),
            **fields,
        )

    def test_only_an_all_non_aquatic_club_is_treated_differently(self):
        self.assertFalse(self.reptile_club.keeps_aquatic)
        self.assertEqual(self.reptile_club.kept_noun, "reptile")
        for club in (self.fish_club, self.mixed_club, self.plain_club):
            with self.subTest(club=club.name):
                club = Club.objects.get(pk=club.pk)
                self.assertTrue(club.keeps_aquatic)
                self.assertEqual(club.kept_noun, "fish")

    def test_a_club_offers_its_interests_categories_and_uncategorized(self):
        self.assertEqual(set(self.reptile_club.lot_categories()), {self.snakes, self.lizards, self.uncategorized})
        self.assertIsNone(self.fish_club.lot_categories())
        self.assertIsNone(self.plain_club.lot_categories())

    def test_a_new_reptile_auction_starts_without_species_suggestions(self):
        reptile_auction = self.auction_for(self.reptile_club)
        fish_auction = self.auction_for(self.fish_club)
        for auction in (reptile_auction, fish_auction):
            finish_new_auction(auction, self.user)
            auction.refresh_from_db()
        self.assertFalse(reptile_auction.use_scientific_name)
        self.assertTrue(fish_auction.use_scientific_name)

    def test_the_breeder_label_names_what_the_club_keeps(self):
        self.assertEqual(self.auction_for(self.reptile_club).i_bred_this_label, "I bred this reptile")
        with override_settings(I_BRED_THIS_FISH_LABEL="I bred this fish"):
            self.assertEqual(self.auction_for(self.fish_club).i_bred_this_label, "I bred this fish")

    def test_a_guessed_category_stays_inside_the_clubs_list(self):
        elsewhere = self.auction_for(self.fish_club, promote_this_auction=True)
        for name, category in (("banded python", self.cichlids), ("python morph", self.snakes)):
            lot = Lot.objects.create(lot_name=name, auction=elsewhere, quantity=1)
            Lot.objects.filter(pk=lot.pk).update(species_category=category, category_automatically_added=False)
        lot = Lot.objects.create(lot_name="banded python", auction=self.auction_for(self.reptile_club), quantity=1)
        self.assertEqual(lot.species_category, self.snakes)

    def test_a_picker_offers_the_clubs_categories_and_keeps_a_lots_own(self):
        field = forms.ModelChoiceField(queryset=Category.objects.all())
        narrow_categories(field, self.auction_for(self.reptile_club), keep=self.cichlids)
        self.assertEqual(set(field.queryset), {self.snakes, self.lizards, self.uncategorized, self.cichlids})
        field = forms.ModelChoiceField(queryset=Category.objects.all())
        narrow_categories(field, self.auction_for(self.fish_club))
        self.assertIn(self.cichlids, field.queryset)

    @override_settings(ENABLE_CLUB_FINDER=True)
    def test_the_finder_title_follows_the_interest_picked(self):
        Club.objects.filter(pk=self.reptile_club.pk).update(outreach_stage=Club.LISTED)
        response = self.client.get(reverse("clubs"), {"interest": self.reptiles.pk})
        self.assertContains(response, "Find a reptile club near you")
        self.assertContains(self.client.get(reverse("clubs")), "Find a fish club near you")

    def test_the_lot_forms_category_guess_stays_inside_the_clubs_list(self):
        elsewhere = self.auction_for(self.fish_club, promote_this_auction=True)
        lot = Lot.objects.create(lot_name="banded python", auction=elsewhere, quantity=1)
        Lot.objects.filter(pk=lot.pk).update(species_category=self.cichlids, category_automatically_added=False)
        self.client.force_login(self.user)
        url = reverse("guess_category")
        self.assertEqual(self.client.post(url, {"lot_name": "banded python"}).json()["value"], self.cichlids.pk)
        reptile_auction = self.auction_for(self.reptile_club)
        guess = self.client.post(url, {"lot_name": "banded python", "auction": reptile_auction.pk}).json()
        self.assertIsNone(guess["value"])
