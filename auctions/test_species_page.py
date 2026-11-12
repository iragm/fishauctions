"""The public species page and the numbers behind it (``auctions/species_stats.py``)."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from auctions import species_stats
from auctions.models import Lot, LotImage, Species
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


@isolated_cache("species-page")
class SpeciesPageTests(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.betta = Species.objects.create(
            genus="Betta", species="splendens", common_name="Siamese fighting fish", source="fishbase", speccode=4768
        )
        cls.halfmoon = Species.objects.create(genus="Betta", species="splendens", variety="Halfmoon", parent=cls.betta)
        for lot, species, price in ((cls.lot, cls.betta, 10), (cls.lotB, cls.betta, 20), (cls.lotC, cls.halfmoon, 30)):
            lot.species = species
            lot.winning_price = price
            lot.save()
        # A second seller: one seller's prices are not a guide.
        Lot.objects.filter(pk=cls.lotB.pk).update(auctiontos_seller=cls.admin_online_tos)

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_counts_its_lots_and_its_varieties(self):
        stats = species_stats.species_stats(self.betta)
        self.assertEqual(stats["lots"], 3)
        self.assertEqual(stats["sold"], 3)
        self.assertEqual(stats["sellers"], 2)
        self.assertEqual(stats["buyers"], 1)
        self.assertEqual(stats["price"]["median"], "20")
        self.assertEqual(stats["trend"], "new")
        self.assertEqual(stats["rank"], 1)
        variety = species_stats.species_stats(self.halfmoon)
        self.assertEqual(variety["lots"], 1)
        self.assertIsNone(variety["price"])

    def test_deleted_removed_and_donated_lots_are_left_out(self):
        self.lot.banned = True
        self.lot.save()
        Lot.objects.filter(pk=self.lotB.pk).update(donation=True)
        stats = species_stats.species_stats(self.betta)
        self.assertEqual(stats["lots"], 2)
        self.assertIsNone(stats["price"])

    def test_one_seller_gets_no_price(self):
        Lot.objects.filter(pk=self.lotB.pk).update(auctiontos_seller=self.online_tos)
        self.assertIsNone(species_stats.species_stats(self.betta)["price"])

    def test_auctions_with_private_stats_are_left_out(self):
        self.online_auction.make_stats_public = False
        self.online_auction.save()
        self.assertEqual(species_stats.species_stats(self.betta)["lots"], 0)

    def test_unapproved_cultivars_lots_stay_off_the_parent(self):
        Species.objects.filter(pk=self.halfmoon.pk).update(approved=False)
        self.assertEqual(species_stats.species_stats(self.betta)["lots"], 2)

    def test_photo_only_from_promoted_auctions_and_own_uploads(self):
        LotImage.objects.create(lot_number=self.lot, image_source="ACTUAL", url="https://example.com/a.jpg")
        LotImage.objects.create(lot_number=self.lot, image_source="ACTUAL", image="images/betta.jpg")
        self.online_auction.promote_this_auction = False
        self.online_auction.save()
        self.assertIsNone(species_stats.representative_photo(self.betta))
        self.online_auction.promote_this_auction = True
        self.online_auction.save()
        photo = species_stats.representative_photo(self.betta)
        self.assertIsNotNone(photo)
        self.assertIn("betta.jpg", photo)

    def test_people_without_accounts_count_once_by_email(self):
        rows = [
            species_stats._person(None, None, "A@example.com", 1),
            species_stats._person(None, None, "a@example.com ", 2),
            species_stats._person(5, None, "a@example.com", 3),
        ]
        self.assertEqual(len(set(rows)), 2)

    def test_trend(self):
        now = timezone.now()
        old = now - datetime.timedelta(days=900)
        self.assertEqual(species_stats.trend(10, 4, old, now), "growing")
        self.assertEqual(species_stats.trend(5, 5, old, now), "steady")
        self.assertEqual(species_stats.trend(2, 10, old, now), "declining")
        self.assertEqual(species_stats.trend(1, 1, old, now), "")
        self.assertEqual(species_stats.trend(1, 0, now, now), "new")

    def test_price_guide_needs_three_sales_and_one_currency(self):
        self.assertIsNone(species_stats.price_guide([Decimal(5), Decimal(6)], None, "USD"))
        guide = species_stats.price_guide([Decimal(4), Decimal(8), Decimal(12), Decimal(100)], None, "USD")
        self.assertEqual((guide["low"], guide["median"], guide["high"]), ("7", "10", "34"))

    def test_page_is_public_and_carries_seo(self):
        response = self.client.get(self.betta.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Siamese fighting fish")
        self.assertContains(response, '<link rel="canonical"', html=False)
        self.assertContains(response, 'name="robots" content="noindex"', html=False)
        self.assertContains(response, "$20")
        self.assertContains(response, "Halfmoon")
        self.assertContains(response, "fishbase.se/summary/4768")
        self.assertNotContains(response, self.online_tos.name)

    def test_a_wrong_slug_redirects_to_the_right_one(self):
        response = self.client.get(f"/species/{self.betta.pk}/wrong/?ref=x")
        self.assertRedirects(
            response, self.betta.get_absolute_url() + "?ref=x", status_code=301, fetch_redirect_response=False
        )
        self.assertEqual(self.betta.get_absolute_url(), f"/species/{self.betta.pk}/betta-splendens/")

    def test_unapproved_species_is_only_for_superusers(self):
        pending = Species.objects.create(genus="Betta", species="imbellis", approved=False)
        self.assertEqual(self.client.get(pending.get_absolute_url()).status_code, 404)
        superuser = User.objects.create_superuser("species_super", "super@example.com", "pw")
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(pending.get_absolute_url()).status_code, 200)

    def test_lot_page_links_the_species_only_for_superusers(self):
        url = self.betta.get_absolute_url()
        self.assertNotContains(self.client.get(self.lot.get_absolute_url(), follow=True), url)
        superuser = User.objects.create_superuser("species_super", "super@example.com", "pw")
        self.client.force_login(superuser)
        self.assertContains(self.client.get(self.lot.get_absolute_url(), follow=True), url)
