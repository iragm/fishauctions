"""Lot searches that know species by every name, and the lot form's one-click add of a missing species."""

from django.urls import reverse

from auctions.filters import BuyingLotFilter, LotAdminFilter, LotFilter
from auctions.models import Lot, Species
from auctions.species_matching import visible_species
from auctions.test_species import make_species
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase
from auctions.views.species import MAX_PENDING_PER_USER


@isolated_cache("species-matching")
class LotSearchBySpeciesTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", extra_names=["Red cherry"])
        self.tagged = Lot.objects.create(
            lot_name="10 reds", auction=self.online_auction, user=self.user, species=self.shrimp
        )
        self.by_common = Lot.objects.create(lot_name="Cherry shrimp x10", auction=self.online_auction, user=self.user)
        self.by_scientific = Lot.objects.create(
            lot_name="Neocaridina davidi", auction=self.online_auction, user=self.user
        )
        self.other = Lot.objects.create(lot_name="Sponge filter", auction=self.online_auction, user=self.user)

    def found(self, text):
        lots = Lot.objects.filter(auction=self.online_auction)
        return set(LotFilter(user=self.user).text_filter(lots, "q", text))

    def test_a_scientific_name_finds_lots_called_by_its_common_name(self):
        self.assertEqual(self.found("Neocaridina davidi"), {self.tagged, self.by_common, self.by_scientific})

    def test_a_common_name_finds_lots_called_by_its_scientific_name(self):
        self.assertEqual(self.found("cherry shrimp"), {self.tagged, self.by_common, self.by_scientific})

    def test_another_common_name_finds_the_tagged_lot(self):
        self.assertIn(self.tagged, self.found("red cherry"))

    def test_a_strain_is_found_by_its_species(self):
        blue = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=self.shrimp, source="aquarium"
        )
        strain_lot = Lot.objects.create(lot_name="Blues", auction=self.online_auction, user=self.user, species=blue)
        self.assertIn(strain_lot, self.found("cherry shrimp"))

    def test_half_a_name_adds_nothing(self):
        self.assertEqual(self.found("Neocar"), {self.by_scientific})

    def test_another_persons_unapproved_species_is_not_used(self):
        hidden = make_species("Caridina", "secretus", "Ghost tiger")
        Species.objects.filter(pk=hidden.pk).update(approved=False, added_by=self.user_with_no_lots)
        Lot.objects.filter(pk=self.other.pk).update(species=hidden)
        self.assertEqual(self.found("Caridina secretus"), set())

    def test_the_admin_search_without_a_request(self):
        lots = Lot.objects.filter(auction=self.online_auction)
        self.assertIn(self.by_common, set(LotAdminFilter.generic(None, lots, "Neocaridina davidi")))

    def test_the_buying_dashboard(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = self.user
        search = BuyingLotFilter(request=request)
        lots = Lot.objects.filter(auction=self.online_auction)
        self.assertIn(self.by_common, set(search.lot_search(lots, "query", "Neocaridina davidi")))


@isolated_cache("species-matching")
class SpeciesQuickCreateTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("species_quick_create")
        self.pleco = make_species("Ancistrus", "cirrhosus", "Bristlenose pleco")
        Species.objects.filter(pk=self.pleco.pk).update(family="Loricariidae", freshwater=True)
        self.client.login(username="my_lot", password="testpassword")

    def test_a_new_epithet_in_a_known_genus_is_added_for_its_author_only(self):
        response = self.client.post(self.url, {"scientific_name": "ancistrus  Dolichopterus"})
        self.assertEqual(response.status_code, 200)
        species = Species.objects.get(pk=response.json()["id"])
        self.assertEqual(response.json()["text"], "Ancistrus dolichopterus")
        self.assertFalse(species.approved)
        self.assertEqual(species.added_by, self.user)
        self.assertIsNone(species.club)
        self.assertEqual(species.source, "admin")
        self.assertEqual(species.family, "Loricariidae")
        self.assertTrue(species.freshwater)
        self.assertIn(species, visible_species(self.user))
        self.assertNotIn(species, visible_species(self.user_with_no_lots))
        self.assertNotIn(species, visible_species())

    def test_an_existing_species_is_returned_not_duplicated(self):
        response = self.client.post(self.url, {"scientific_name": "Ancistrus cirrhosus"})
        self.assertEqual(response.json()["id"], self.pleco.pk)
        self.assertEqual(Species.objects.count(), 1)

    def test_an_unknown_genus_is_refused(self):
        response = self.client.post(self.url, {"scientific_name": "Cherry shrimp"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("isn't a genus", response.json()["error"])
        self.assertEqual(Species.objects.count(), 1)

    def test_a_genus_only_another_person_can_see_is_unknown(self):
        hidden = Species.objects.create(
            genus="Secretia", species="one", approved=False, added_by=self.user_with_no_lots
        )
        response = self.client.post(self.url, {"scientific_name": "Secretia two"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Species.objects.exclude(pk__in=[hidden.pk, self.pleco.pk]).exists())

    def test_anything_but_a_scientific_name_is_refused(self):
        for typed in ["Ancistrus", "Ancistrus 123", "Ancistrus <b>x</b>", "Ancistrus a b c", ""]:
            self.assertEqual(self.client.post(self.url, {"scientific_name": typed}).status_code, 400, typed)
        self.assertEqual(Species.objects.count(), 1)

    def test_the_lot_category_is_kept(self):
        from auctions.models import Category

        plecos = Category.objects.create(name="Plecos")
        response = self.client.post(self.url, {"scientific_name": "Ancistrus sp. L144", "category": plecos.pk})
        species = Species.objects.get(pk=response.json()["id"])
        self.assertEqual(species.category, plecos)
        self.assertEqual(species.scientific_name, "Ancistrus sp. l144")

    def test_a_person_can_only_have_so_many_waiting(self):
        Species.objects.bulk_create(
            Species(genus="Ancistrus", species=f"x{index}", approved=False, added_by=self.user)
            for index in range(MAX_PENDING_PER_USER)
        )
        response = self.client.post(self.url, {"scientific_name": "Ancistrus ranunculus"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Species.objects.filter(species="ranunculus").exists())

    def test_signed_out_is_refused(self):
        self.client.logout()
        response = self.client.post(self.url, {"scientific_name": "Ancistrus ranunculus"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Species.objects.filter(species="ranunculus").exists())

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_the_full_page_lot_form_offers_it(self):
        from auctions.forms import CreateLotForm

        form = CreateLotForm(user=self.user, cloned_from=None, auction=None)
        self.assertIn(f'data-species-create="{self.url}"', str(form["species"]))
