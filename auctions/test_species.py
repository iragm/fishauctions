"""Tests for scientific names on lots: matching, the picker, labels, and genus BAP points.

The matching tests carry a small hand-built species list rather than anything from FishBase, so
they say what the *rules* are without depending on a snapshot: what counts as a match, what
deliberately does not, and what happens when neither the database nor the model can answer.

Everything touching the language model runs against a scripted :class:`FakeProvider`, so there is
no network here and a test can say exactly what the model "replied", including nonsense.
"""

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from auctions import llm, views
from auctions.llm import LLMError, LLMProvider, LLMResult
from auctions.models import (
    Auction,
    AuctionTOS,
    BapAward,
    Category,
    Club,
    ClubAPIKey,
    ClubBapCategoryOverride,
    ClubBapGenusOverride,
    ClubMember,
    LLMUsage,
    Lot,
    Species,
    SpeciesCommonName,
    SpeciesSearchCache,
)
from auctions.species_matching import (
    MAX_GENUS_MATCHES,
    MAX_SUGGESTIONS,
    exact_matches,
    keywords,
    normalize,
    search_matches,
    singularize,
    strip_quantity,
    suggest_species,
)
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


class FakeProvider(LLMProvider):
    """A scripted provider. Hand it the replies you want, in order."""

    name = "fake"

    def __init__(self, replies=None):
        super().__init__(model="fake-model", api_key="fake-key")
        self.replies = list(replies or [])
        self.calls = []

    def is_configured(self):
        return True

    def complete_json(self, system, messages, max_tokens=1000):
        self.calls.append({"system": system, "messages": messages})
        if not self.replies:
            msg = "FakeProvider ran out of scripted replies"
            raise LLMError(msg)
        return LLMResult(data=self.replies.pop(0), model="fake-model", prompt_tokens=11, completion_tokens=7)

    @property
    def call_count(self):
        return len(self.calls)


def make_species(genus, epithet, common=None, extra_names=(), source="fishbase", speccode=None, aquarium_use=""):
    """One species plus its common names.  ``common`` is the designated primary name (FBname).

    ``aquarium_use`` is FishBase's rating; passing "commercial" is how a test says "the hobby keeps
    this one".  It goes through ``save()``, which is what sets the species-level ``trade_rank`` --
    the *genus* tier needs ``Species.recompute_trade_ranks()``, because it can only be worked out
    by looking at every sibling.
    """
    species = Species.objects.create(
        genus=genus, species=epithet, common_name=common or "", source=source, aquarium_use=aquarium_use
    )
    if speccode is not None:
        Species.objects.filter(pk=species.pk).update(speccode=speccode)
    if common:
        SpeciesCommonName.objects.create(species=species, name=common, is_preferred=True)
    for name in extra_names:
        SpeciesCommonName.objects.create(species=species, name=name)
    return species


@isolated_cache("species-matching")
class SpeciesMatchingTests(StandardTestCase):
    """The rules search_matches follows, and the ones it deliberately refuses to follow."""

    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.guppy = make_species("Poecilia", "reticulata", "Guppy", ["Millionfish"])
        # Several poeciliids genuinely carry "Guppy" as a synonym; only one is *the* guppy.
        self.other_guppy = make_species("Poecilia", "vivipara", "Eye spot toothcarp", ["Guppy"])
        self.cardinal = make_species("Paracheirodon", "axelrodi", "Cardinal tetra", ["Neon"])
        self.tropheus = make_species("Tropheus", "duboisi", "White spotted cichlid")
        self.ramirezi = make_species("Mikrogeophagus", "ramirezi", "Ram cichlid", ["Ram"])
        self.altispinosus = make_species("Mikrogeophagus", "altispinosus")
        self.sponge = make_species("Tethya", "aurantium", "Ball sponge", source="sealifebase")

    def test_normalize_strips_punctuation_and_case(self):
        self.assertEqual(normalize("  Betta   Splendens (pair)! "), "betta splendens pair")

    def test_normalize_deletes_an_apostrophe_but_splits_on_a_hyphen(self):
        """The one punctuation mark people leave out has to normalise to the same string either way."""
        self.assertEqual(normalize("Adolf's catfish"), "adolfs catfish")
        self.assertEqual(normalize("adolfs catfish"), "adolfs catfish")
        self.assertEqual(normalize("Black-banded leporinus"), "black banded leporinus")

    def test_singularize_handles_the_common_endings(self):
        self.assertEqual(singularize("tetras"), "tetra")
        self.assertEqual(singularize("guppies"), "guppy")
        self.assertEqual(singularize("bass"), "bass")

    def test_keywords_drop_the_site_stopword_list(self):
        # "pair", "young" and colours are in settings.IGNORE_WORDS because they describe the lot,
        # not the animal.
        self.assertNotIn("pair", keywords("young blue guppy pair"))
        self.assertIn("guppy", keywords("young blue guppy pair"))

    def test_exact_common_name_prefers_the_designated_species(self):
        """ "Guppy" must land on Poecilia reticulata, not the other fish that share the name."""
        found = list(exact_matches("guppy"))
        self.assertEqual(found[0], self.guppy)
        self.assertIn(self.other_guppy, found)

    def test_exact_match_survives_a_plural(self):
        self.assertEqual(list(exact_matches("guppies"))[0], self.guppy)

    def test_scientific_name_in_a_longer_lot_name(self):
        """A full scientific name wins even buried in collection-location detail."""
        self.assertEqual(search_matches("Tropheus duboisi maswa F1"), [self.tropheus])

    def test_common_name_phrase_survives_a_plural_and_a_quantity(self):
        self.assertEqual(search_matches("6 young cardinal tetras"), [self.cardinal])

    def test_a_bare_genus_that_matches_one_species_is_offered(self):
        self.assertEqual(search_matches("Tropheus sp. Ikola"), [self.tropheus])

    def test_equipment_matches_nothing(self):
        """The failure this guards: 'sponge filter' matching an actual sponge."""
        self.assertEqual(search_matches("sponge filter"), [])

    def test_a_shared_epithet_alone_is_not_a_match(self):
        """'davidi' belongs to lots of unrelated species; on its own it names none of them."""
        make_species("Formosania", "davidi")
        make_species("Rhinogobius", "davidi")
        self.assertEqual(search_matches("Neocaridina davidi"), [])

    def test_a_single_word_common_name_does_not_hijack_a_longer_name(self):
        """'Bolivian ram' must not resolve to the fish FishBase simply calls 'Ram'."""
        self.assertEqual(search_matches("Bolivian ram"), [])

    def test_weak_genus_match_with_too_many_species_returns_nothing(self):
        """A genus that matched more than a picklist's worth has told the user nothing."""
        for index in range(MAX_GENUS_MATCHES + 4):
            make_species("Ancistrus", f"species{index}")
        self.assertEqual(search_matches("Ancistrus sp. L144"), [])

    def test_a_small_genus_is_offered_in_full(self):
        """Typing just "Tropheus" should show the Tropheus, not nothing."""
        for index in range(MAX_GENUS_MATCHES - 1):
            make_species("Tropheus", f"species{index}")
        found = search_matches("tropheus")
        self.assertEqual(len(found), MAX_GENUS_MATCHES)
        self.assertIn(self.tropheus, found)

    def test_a_big_genus_falls_back_to_the_ones_in_the_trade(self):
        """Seventy-seven Ancistrus in FishBase are two in the hobby, and those are the answer."""
        for index in range(MAX_GENUS_MATCHES + 4):
            make_species("Ancistrus", f"species{index}")
        kept = make_species("Ancistrus", "cirrhosus", "Bristlenose", aquarium_use="commercial")
        self.assertEqual(search_matches("Ancistrus sp. L144"), [kept])

    def test_a_bare_epithet_offers_the_species_that_carry_it(self):
        """The reported failure: "saulosi" is two real fish and used to return nothing."""
        chindongo = make_species("Chindongo", "saulosi")
        aulonocara = make_species("Aulonocara", "saulosi", "Greenface aulonocara")
        self.assertEqual(search_matches("saulosi"), [aulonocara, chindongo])

    def test_a_bare_epithet_survives_a_quantity(self):
        chindongo = make_species("Chindongo", "saulosi")
        self.assertEqual(search_matches("6 saulosi"), [chindongo])

    def test_a_bare_epithet_ending_in_s_still_counts_as_one_word(self):
        """keywords() emits a singular alongside every word, which made "elegans" look like two."""
        species = make_species("Melanotaenia", "elegans")
        self.assertEqual(search_matches("elegans"), [species])

    def test_an_epithet_shared_by_too_many_species_is_still_nothing(self):
        for index in range(MAX_GENUS_MATCHES + 1):
            make_species(f"Genus{index}", "davidi")
        self.assertEqual(search_matches("davidi"), [])

    def test_an_aquarium_species_outranks_one_nobody_keeps(self):
        # Alphabetically Aulonocara comes first, so the trade rating has to be what moves
        # Zebrasoma ahead of it -- otherwise the test would pass on the sort order alone.
        wild = make_species("Aulonocara", "saulosi")
        kept = make_species("Zebrasoma", "saulosi", aquarium_use="commercial")
        found = search_matches("saulosi")
        self.assertEqual(found[0].pk, kept.pk)
        self.assertIn(wild, found)

    def test_the_category_only_ever_breaks_a_tie(self):
        cichlids = Category.objects.create(name="Cichlids")
        one = make_species("Chindongo", "saulosi")
        two = make_species("Aulonocara", "saulosi")
        Species.objects.filter(pk=one.pk).update(category=cichlids)
        found = search_matches("saulosi", category=cichlids)
        self.assertEqual(found[0].pk, one.pk)
        # ...and never removes the other one.
        self.assertEqual({species.pk for species in found}, {one.pk, two.pk})

    def test_suggest_species_reports_where_the_answer_came_from(self):
        self.assertEqual(suggest_species("guppy", use_llm=False)[1], "exact")
        self.assertEqual(suggest_species("Tropheus duboisi maswa F1", use_llm=False)[1], "search")
        self.assertEqual(suggest_species("sponge filter", use_llm=False)[1], "none")

    def test_never_more_than_five_suggestions(self):
        for index in range(10):
            make_species("Corydoras", f"species{index}", "Cory catfish")
        matches, _ = suggest_species("cory catfish", use_llm=False)
        self.assertLessEqual(len(matches), 5)


@isolated_cache("species-llm")
class SpeciesLLMTests(StandardTestCase):
    """The language-model fallback: when it is asked, what it may return, and what it costs."""

    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        llm.set_provider_override(self.provider)
        self.ramirezi = make_species("Mikrogeophagus", "ramirezi", "Ram cichlid", ["Ram"])
        self.altispinosus = make_species("Mikrogeophagus", "altispinosus")
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")

    def tearDown(self):
        llm.set_provider_override(None)
        super().tearDown()

    def test_a_database_answer_never_costs_a_model_call(self):
        suggest_species("guppy")
        self.assertEqual(self.provider.call_count, 0)

    def test_model_picks_from_the_shortlist(self):
        self.provider.replies = [{"id": self.altispinosus.pk}]
        matches, source = suggest_species("Bolivian ram")
        self.assertEqual(source, "llm")
        self.assertEqual(matches, [self.altispinosus])

    def test_shortlist_includes_genus_siblings(self):
        """The Bolivian ram is only reachable because its genus sibling is called 'Ram'.

        Without the sibling expansion the model is shown a list that cannot contain the right
        answer, and correctly answers null -- which reads as the feature not working.
        """
        self.provider.replies = [{"id": self.altispinosus.pk}]
        suggest_species("Bolivian ram")
        shortlist = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn(str(self.altispinosus.pk), shortlist)

    def test_an_id_that_was_not_offered_is_discarded(self):
        """Nothing the model says can put a species on a lot that we didn't shortlist."""
        self.provider.replies = [{"id": 99999999}]
        matches, source = suggest_species("Bolivian ram")
        self.assertEqual(matches, [])
        self.assertEqual(source, "none")

    def test_null_is_an_answer_and_gets_remembered(self):
        self.provider.replies = [{"id": None}]
        self.assertEqual(suggest_species("Bolivian ram")[0], [])
        cached = SpeciesSearchCache.objects.get(search_text="bolivian ram")
        self.assertIsNone(cached.species)

    def test_the_cache_stops_a_second_call(self):
        self.provider.replies = [{"id": self.altispinosus.pk}]
        suggest_species("Bolivian ram")
        matches, source = suggest_species("BOLIVIAN RAM!")
        self.assertEqual(source, "cache")
        self.assertEqual(matches, [self.altispinosus])
        self.assertEqual(self.provider.call_count, 1)

    def test_a_provider_failure_degrades_to_no_species(self):
        self.provider.replies = []  # raises LLMError
        matches, source = suggest_species("Bolivian ram")
        self.assertEqual(matches, [])
        self.assertEqual(source, "none")

    def test_every_call_is_recorded(self):
        self.provider.replies = [{"id": self.altispinosus.pk}]
        suggest_species("Bolivian ram", user=self.user)
        usage = LLMUsage.objects.get(query="Bolivian ram")
        self.assertEqual(usage.response_kind, "species")
        self.assertEqual(usage.user, self.user)

    def test_rate_limit_stops_asking(self):
        """Bulk-adding fifty lots that all miss the cache must not be able to run up the bill."""
        from auctions import species_matching

        self.assertTrue(species_matching.check_rate_limit(self.user, limit=2))
        self.assertTrue(species_matching.check_rate_limit(self.user, limit=2))
        self.assertFalse(species_matching.check_rate_limit(self.user, limit=2))

    def test_a_user_over_the_limit_gets_no_species_rather_than_an_error(self):
        from auctions import species_matching

        original = species_matching.MAX_LLM_CALLS_PER_USER_PER_DAY
        species_matching.MAX_LLM_CALLS_PER_USER_PER_DAY = 0
        try:
            matches, _source = suggest_species("Bolivian ram", user=self.user)
        finally:
            species_matching.MAX_LLM_CALLS_PER_USER_PER_DAY = original
        self.assertEqual(matches, [])
        self.assertEqual(self.provider.call_count, 0)


@isolated_cache("species-endpoint")
class SpeciesSuggestionEndpointTests(StandardTestCase):
    """The endpoint the lot forms call."""

    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.url = reverse("species_suggestions")

    def test_login_required(self):
        response = self.client.post(self.url, {"name": "guppy"})
        self.assertIn(response.status_code, (302, 401, 403))

    def test_returns_the_match(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(self.url, {"name": "guppy"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "guppy")
        self.assertEqual(data["choices"][0]["id"], self.guppy.pk)
        self.assertEqual(data["choices"][0]["scientific_name"], "Poecilia reticulata")

    def test_echoes_the_name_so_a_slow_reply_can_be_discarded(self):
        """The bulk-add page has several rows in flight; a stale reply must be identifiable."""
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.client.post(self.url, {"name": "sponge filter"}).json()["name"], "sponge filter")

    def test_blank_name_is_not_an_error(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(self.url, {"name": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"], [])


class SpeciesOnLotFormsTests(StandardTestCase):
    """The picker on the lot forms: shown when the auction wants it, validated always."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()

    def test_scientific_name_defaults_to_on(self):
        fresh = Auction.objects.create(title="fresh", created_by=self.user, date_start=timezone.now())
        self.assertTrue(fresh.use_scientific_name)

    def test_quick_add_lot_hides_the_field_when_the_auction_says_so(self):
        from auctions.forms import quick_add_lot_form_class

        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        form = quick_add_lot_form_class()(auction=self.online_auction, is_admin=False, tos=self.online_tos)
        self.assertTrue(form.fields["species"].widget.is_hidden)

    def test_quick_add_lot_shows_the_field_by_default(self):
        from auctions.forms import quick_add_lot_form_class

        form = quick_add_lot_form_class()(auction=self.online_auction, is_admin=False, tos=self.online_tos)
        self.assertFalse(form.fields["species"].widget.is_hidden)

    def test_the_bulk_form_has_no_search_box(self):
        """Deliberately not on the bulk pages: there is already a great deal on a row there, and a
        lot added in bulk is corrected afterwards on a form that does have it."""
        from auctions.forms import quick_add_lot_form_class

        form = quick_add_lot_form_class()(auction=self.online_auction, is_admin=False, tos=self.online_tos)
        self.assertNotIn("data-species-search", str(form["species"]))

    def test_the_admin_lot_form_has_a_search_box(self):
        """An auction admin fixing a wrong species is the whole reason this exists."""
        from auctions.forms import EditLot

        form = EditLot(auction=self.online_auction, lot=self.lot, user=self.user)
        self.assertIn("data-species-search", str(form["species"]))

    def test_the_widget_only_renders_the_chosen_option(self):
        """139k options in every lot form would be megabytes of HTML; the rest arrive over ajax."""
        from auctions.forms import quick_add_lot_form_class

        make_species("Betta", "splendens", "Siamese fighting fish")
        form = quick_add_lot_form_class()(
            auction=self.online_auction, is_admin=False, tos=self.online_tos, initial={"species": self.guppy.pk}
        )
        html = str(form["species"])
        self.assertIn("Poecilia reticulata", html)
        self.assertIn("No species", html)
        self.assertNotIn("Betta splendens", html)

    def test_a_species_that_does_not_exist_is_rejected(self):
        from auctions.forms import quick_add_lot_form_class

        form = quick_add_lot_form_class()(
            data={"lot_name": "test", "quantity": 1, "reserve_price": 2, "species": 99999999},
            auction=self.online_auction,
            is_admin=False,
            tos=self.online_tos,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("species", form.errors)

    def test_a_posted_species_is_dropped_when_the_auction_does_not_use_the_field(self):
        from auctions.forms import quick_add_lot_form_class

        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        form = quick_add_lot_form_class()(
            data={"lot_name": "test", "quantity": 1, "reserve_price": 2, "species": self.guppy.pk},
            auction=self.online_auction,
            is_admin=False,
            tos=self.online_tos,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["species"])

    def test_bulk_add_ajax_saves_a_species_and_remembers_it(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.online_auction.slug}),
            data={"lot_name": "Fancy guppy pair", "quantity": 1, "reserve_price": 2, "species": self.guppy.pk},
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"], response.json())
        lot = Lot.objects.filter(lot_name="Fancy guppy pair").first()
        self.assertEqual(lot.species, self.guppy)
        # A name a human paired with a species as they typed it is the best signal there is.
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="fancy guppy pair").species, self.guppy)

    def test_editing_a_lot_later_does_not_teach_the_cache(self):
        """Renaming a row without clearing its species must not poison the shared cache."""
        self.client.login(username="my_lot", password="testpassword")
        url = reverse("save_lot_ajax", kwargs={"slug": self.online_auction.slug})
        created = self.client.post(
            url,
            data={"lot_name": "Fancy guppy pair", "quantity": 1, "reserve_price": 2, "species": self.guppy.pk},
            content_type="application/json",
        ).json()
        self.client.post(
            url,
            data={
                "lot_id": created["lot_id"],
                "lot_name": "Sponge filter",
                "quantity": 1,
                "reserve_price": 2,
                "species": self.guppy.pk,
            },
            content_type="application/json",
        )
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())

    def test_bulk_add_ajax_rejects_a_species_that_does_not_exist(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.online_auction.slug}),
            data={"lot_name": "Fancy guppy pair", "quantity": 1, "reserve_price": 2, "species": 99999999},
            content_type="application/json",
        )
        self.assertFalse(response.json()["success"])
        self.assertIn("species", response.json()["errors"])


class SpeciesPickerRendersTests(StandardTestCase):
    """The picker and its attribution actually reach the page.

    Form-level tests can pass while the field never renders -- it has to survive the crispy layout
    and the ``{% if %}`` around it -- so these go through the views.
    """

    def setUp(self):
        super().setUp()
        make_species("Poecilia", "reticulata", "Guppy")
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()
        self.client.login(username="my_lot", password="testpassword")
        self.bulk_url = reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.online_auction.slug})

    def test_bulk_add_page_renders_the_picker_and_the_citation(self):
        response = self.client.get(self.bulk_url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('data-field="species"', body)
        self.assertIn("No species", body)
        # FishBase asks to be cited wherever its data is used.
        self.assertIn("FishBase", body)
        self.assertIn("Froese", body)

    def test_bulk_add_page_leaves_the_picker_out_when_the_auction_says_so(self):
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        body = self.client.get(self.bulk_url).content.decode()
        self.assertNotIn('data-field="species"', body)
        # No picker means no data used, which means nothing to attribute.
        self.assertNotIn("FishBase", body)

    def test_the_label_settings_page_offers_the_field(self):
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auction_label_config", kwargs={"slug": self.online_auction.slug})
        body = self.client.get(url).content.decode()
        self.assertIn("Scientific name", body)

    def test_the_custom_fields_page_offers_the_switch(self):
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("edit_auction_custom_fields", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("use_scientific_name", response.content.decode())


class ScientificNameOnLabelsTests(StandardTestCase):
    """What gets printed, and what an auction can turn off."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.lot.species = self.guppy
        self.lot.save()

    def test_new_auctions_print_it_by_default(self):
        fresh = Auction.objects.create(title="fresh", created_by=self.user, date_start=timezone.now())
        self.assertIn("scientific_name", fresh.label_print_fields)

    def test_the_label_line_is_the_scientific_name(self):
        self.assertEqual(self.lot.scientific_name_label, "Poecilia reticulata")

    def test_nothing_prints_for_a_lot_with_no_species(self):
        """Hardware and mixed lots have no species, and must not leave a blank line."""
        self.assertEqual(self.unsoldLot.scientific_name_label, "")

    def test_nothing_prints_when_the_auction_turned_the_field_off(self):
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.scientific_name_label, "")

    def test_the_label_settings_form_offers_it(self):
        from auctions.forms import LabelPrintFieldsForm

        form = LabelPrintFieldsForm(auction=self.online_auction)
        self.assertIn("scientific_name", [field["value"] for field in form.available_fields])

    def test_unticking_it_removes_it(self):
        from auctions.forms import LabelPrintFieldsForm

        form = LabelPrintFieldsForm(auction=self.online_auction, data={"lot_name": True})
        self.assertTrue(form.is_valid())
        form.save()
        self.online_auction.refresh_from_db()
        self.assertNotIn("scientific_name", self.online_auction.label_print_fields)


class GenusBapOverrideTests(StandardTestCase):
    """Per-genus BAP points, and how they rank against the per-category rule."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Test club", enable_breeder_award_program=True, points_per_lot=5)
        self.category = Category.objects.create(name="Cichlids", bap_points=7)
        self.tropheus = make_species("Tropheus", "duboisi", "White spotted cichlid")
        self.online_auction.club = self.club
        self.online_auction.save()
        self.lot.species = self.tropheus
        self.lot.species_category = self.category
        self.lot.save()

    def test_no_overrides_falls_back_to_the_club_rate(self):
        self.assertEqual(self.lot.bap_points_for_club(self.club), 5)

    def test_category_override_beats_the_club_rate(self):
        ClubBapCategoryOverride.objects.create(club=self.club, category=self.category, points=10)
        self.assertEqual(self.lot.bap_points_for_club(self.club), 10)

    def test_genus_override_beats_the_category_override(self):
        """The whole point: 'Cichlids are worth 10, but Tropheus is worth 20'."""
        ClubBapCategoryOverride.objects.create(club=self.club, category=self.category, points=10)
        ClubBapGenusOverride.objects.create(club=self.club, genus="Tropheus", points=20)
        self.assertEqual(self.lot.bap_points_for_club(self.club), 20)

    def test_a_lot_with_no_species_is_unaffected_by_genus_rules(self):
        ClubBapGenusOverride.objects.create(club=self.club, genus="Tropheus", points=20)
        self.lot.species = None
        self.lot.save()
        self.assertEqual(self.lot.bap_points_for_club(self.club), 5)

    def test_genus_is_stored_capitalised_so_typing_case_does_not_matter(self):
        override = ClubBapGenusOverride.objects.create(club=self.club, genus="  tropheus ", points=20)
        self.assertEqual(override.genus, "Tropheus")
        self.assertEqual(self.lot.bap_points_for_club(self.club), 20)

    def test_a_genus_with_no_species_is_refused(self):
        """A rule for a misspelt genus would silently never fire; say so at the point of entry."""
        from auctions.forms import ClubBapGenusOverrideForm

        form = ClubBapGenusOverrideForm(data={"genus": "Trophaeus", "points": 20})
        self.assertFalse(form.is_valid())
        self.assertIn("genus", form.errors)

    def test_a_real_genus_is_accepted(self):
        from auctions.forms import ClubBapGenusOverrideForm

        self.assertTrue(ClubBapGenusOverrideForm(data={"genus": "tropheus", "points": 20}).is_valid())

    def test_awarded_points_use_the_genus_rule(self):
        """End to end: the award a sold lot actually generates."""
        ClubBapGenusOverride.objects.create(club=self.club, genus="Tropheus", points=20)
        self.club.auto_add_points = True
        self.club.only_sold_lots = False
        self.club.only_active_members_can_participate = False
        self.club.min_quantity = 1
        self.club.save()
        ClubMember.objects.create(club=self.club, user=self.user, name="seller")
        self.lot.i_bred_this_fish = True
        self.lot.date_end = timezone.now()
        self.lot.user = self.user
        self.lot.save()
        self.lot.auto_award_bap_points()
        award = BapAward.objects.filter(lot=self.lot).first()
        self.assertIsNotNone(award, self.lot.sold_lot_no_bap_reason)
        self.assertEqual(award.points, 20)


class SpeciesModelTests(StandardTestCase):
    """The model's own rules."""

    def test_scientific_name_is_built_from_genus_and_epithet(self):
        species = make_species("Poecilia", "reticulata", "Guppy")
        self.assertEqual(species.scientific_name, "Poecilia reticulata")

    def test_scientific_name_is_rebuilt_on_save(self):
        """Storing it separately is only safe if it can't drift from the two real columns."""
        species = make_species("Poecilia", "reticulata", "Guppy")
        species.species = "wingei"
        species.save()
        self.assertEqual(species.scientific_name, "Poecilia wingei")

    def test_a_genus_only_species_has_no_trailing_space(self):
        self.assertEqual(make_species("Ancistrus", "").scientific_name, "Ancistrus")

    def test_the_label_carries_the_common_name_for_context(self):
        self.assertEqual(make_species("Poecilia", "reticulata", "Guppy").label, "Poecilia reticulata (Guppy)")

    def test_speccode_is_only_unique_within_a_source(self):
        """FishBase and SeaLifeBase both number from 1; a bare unique would lose 36,000 fish."""
        make_species("Poecilia", "reticulata", source="fishbase", speccode=1)
        make_species("Caridina", "multidentata", source="sealifebase", speccode=1)
        self.assertEqual(Species.objects.filter(speccode=1).count(), 2)

    def test_lot_scientific_name_is_blank_without_a_species(self):
        self.assertEqual(self.unsoldLot.scientific_name, "")


class AuctionScientificNameSettingTests(StandardTestCase):
    """The auction-level switch."""

    def test_the_custom_fields_form_carries_it(self):
        from auctions.forms import AuctionCustomFieldsForm

        self.assertIn("use_scientific_name", AuctionCustomFieldsForm(instance=self.online_auction).fields)

    def test_cloning_an_auction_carries_the_setting(self):
        """Copying an auction has to bring this with it, or a club re-opts-in every year."""
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(
            f"{reverse('create_auction')}?clone={self.online_auction.slug}",
            {
                "title": "Cloned auction",
                "cloned_from": self.online_auction.slug,
                "date_start": (timezone.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "date_end": (timezone.now() + datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        clone = Auction.objects.filter(title="Cloned auction").first()
        self.assertIsNotNone(clone)
        self.assertFalse(clone.use_scientific_name)


class SpeciesSearchCacheTests(StandardTestCase):
    """The remembered-answers table."""

    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")

    def test_a_cached_hit_is_counted(self):
        SpeciesSearchCache.objects.create(search_text="fancy guppy", species=self.guppy, source="user")
        suggest_species("Fancy Guppy")
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="fancy guppy").hits, 1)

    def test_a_cached_none_is_honoured(self):
        """A remembered 'this is not a species' has to short-circuit too, or it costs a call."""
        SpeciesSearchCache.objects.create(search_text="sponge filter", species=None, source="user")
        matches, source = suggest_species("Sponge filter")
        self.assertEqual(source, "cache")
        self.assertEqual(matches, [])

    def test_the_species_list_outranks_a_bad_cache_row(self):
        """The cache is global and holds guesses; it must never beat a name the list knows.

        Otherwise one person mis-tagging a lot teaches every club, permanently.
        """
        SpeciesSearchCache.objects.create(search_text="guppy", species=None, source="user")
        matches, source = suggest_species("guppy")
        self.assertEqual(source, "exact")
        self.assertEqual(matches, [self.guppy])


class SpeciesVarietyTests(StandardTestCase):
    """Cultivars: a strain is a name for a fish, not a taxon, and the model says so."""

    def setUp(self):
        super().setUp()
        self.shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", source="aquarium")
        self.blue_dream = Species.objects.create(
            genus="Neocaridina",
            species="davidi",
            variety="Blue Dream",
            common_name="Blue dream shrimp",
            parent=self.shrimp,
            source="aquarium",
        )
        SpeciesCommonName.objects.create(species=self.blue_dream, name="blue dream shrimp", is_preferred=True)

    def test_a_variety_keeps_the_parents_scientific_name(self):
        """Everything reasoning about the science -- genus BAP rules, family, breeder points --
        has to see a cherry shrimp."""
        self.assertEqual(self.blue_dream.scientific_name, "Neocaridina davidi")
        self.assertEqual(self.blue_dream.genus, "Neocaridina")

    def test_the_strain_shows_in_the_full_name_and_the_label(self):
        self.assertEqual(self.blue_dream.full_scientific_name, "Neocaridina davidi 'Blue Dream'")
        self.assertEqual(self.blue_dream.label, "Neocaridina davidi 'Blue Dream' (Blue dream shrimp)")

    def test_the_strain_name_finds_the_variety(self):
        self.assertEqual(list(exact_matches("blue dream shrimp")), [self.blue_dream])

    def test_the_species_name_does_not_offer_every_strain(self):
        """ "Neocaridina davidi" means the shrimp, not a menu of its thirteen colours."""
        self.assertEqual(list(exact_matches("Neocaridina davidi")), [self.shrimp])
        self.assertEqual(search_matches("Neocaridina davidi"), [self.shrimp])

    def test_a_lot_prints_and_shows_the_strain(self):
        self.lot.species = self.blue_dream
        self.lot.save()
        self.assertEqual(self.lot.scientific_name, "Neocaridina davidi 'Blue Dream'")
        self.assertEqual(self.lot.scientific_name_label, "Neocaridina davidi 'Blue Dream'")

    def test_deleting_the_parent_takes_its_strains_with_it(self):
        self.shrimp.delete()
        self.assertFalse(Species.objects.filter(pk=self.blue_dream.pk).exists())


class AquariumSpeciesListTests(StandardTestCase):
    """The curated list: what it loads, and what it refuses to load."""

    def _write(self, tmp_path, body):
        path = tmp_path / "aquarium_species.csv"
        path.write_text("scientific_name,variety,common_names,family,order,kind,habitat\n" + body, encoding="utf-8")
        return path

    def test_the_shipped_file_parses(self):
        from auctions import aquarium_species

        rows = aquarium_species.read_rows()
        self.assertGreater(len(rows), 100)
        # Every cultivar in the file names a parent that is also in it, or a FishBase species.
        self.assertTrue(all(row.common_names or row.is_variety for row in rows))

    def test_loading_creates_species_varieties_and_common_names(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                Path(folder),
                "Neocaridina davidi,,cherry shrimp|rcs,Atyidae,Decapoda,invert,fresh\n"
                "Neocaridina davidi,Blue Dream,blue dream shrimp,Atyidae,Decapoda,invert,fresh\n",
            )
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 2)
        shrimp = Species.objects.get(scientific_name="Neocaridina davidi", variety="")
        self.assertEqual(shrimp.source, "aquarium")
        self.assertEqual(shrimp.common_name, "Cherry shrimp")
        self.assertTrue(shrimp.freshwater)
        self.assertFalse(shrimp.saltwater)
        self.assertEqual(shrimp.family, "Atyidae")
        variety = Species.objects.get(variety="Blue Dream")
        self.assertEqual(variety.parent, shrimp)
        self.assertEqual(list(exact_matches("cherry shrimp")), [shrimp])

    def test_a_cultivar_with_no_parent_is_skipped_not_invented(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), "Betta splendens,Halfmoon,halfmoon betta,,,fish,fresh\n")
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(Species.objects.filter(variety="Halfmoon").exists())

    def test_a_cultivar_finds_a_parent_that_came_from_fishbase(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        betta = make_species("Betta", "splendens", "Siamese fighting fish")
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), "Betta splendens,Halfmoon,halfmoon betta,,,fish,fresh\n")
            aquarium_species.load(path)
        self.assertEqual(Species.objects.get(variety="Halfmoon").parent, betta)

    def test_reloading_updates_rather_than_duplicates(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        body = "Neocaridina davidi,,cherry shrimp,Atyidae,Decapoda,invert,fresh\n"
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), body)
            aquarium_species.load(path)
            existing = Species.objects.get(scientific_name="Neocaridina davidi")
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(Species.objects.filter(scientific_name="Neocaridina davidi").count(), 1)
        # The lot pointing at it keeps its species: the primary key never moves.
        self.assertEqual(Species.objects.get(scientific_name="Neocaridina davidi").pk, existing.pk)


class SpeciesCategoryFromTaxonomyTests(StandardTestCase):
    """Family and order decide the category, so a lot with a species stops needing the guesser."""

    def setUp(self):
        super().setUp()
        self.cichlids = Category.objects.create(name="Cichlids")
        self.livebearers = Category.objects.create(name="Livebearers")
        self.angel = make_species("Pterophyllum", "scalare", "Freshwater angelfish")
        Species.objects.filter(pk=self.angel.pk).update(family="Cichlidae", order="Cichliformes")
        self.angel.refresh_from_db()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        Species.objects.filter(pk=self.guppy.pk).update(family="Poeciliidae", order="Cyprinodontiformes")
        self.guppy.refresh_from_db()

    def test_a_family_maps_to_a_category(self):
        from auctions.species_categories import assign_categories

        assign_categories()
        self.angel.refresh_from_db()
        self.assertEqual(self.angel.category, self.cichlids)

    def test_the_family_beats_the_order(self):
        """A guppy is a Cyprinodontiform, but nobody files one with the killifish."""
        from auctions.species_categories import assign_categories

        assign_categories()
        self.guppy.refresh_from_db()
        self.assertEqual(self.guppy.category, self.livebearers)

    def test_a_category_this_site_does_not_have_is_reported_not_created(self):
        from auctions.species_categories import assign_categories

        catfish = make_species("Corydoras", "aeneus", "Bronze corydoras")
        Species.objects.filter(pk=catfish.pk).update(family="Callichthyidae", order="Siluriformes")
        _, resolver = assign_categories()
        catfish.refresh_from_db()
        self.assertIsNone(catfish.category)
        self.assertIn("catfish", resolver.unmatched_hints)
        self.assertFalse(Category.objects.filter(name="Catfish").exists())

    def test_a_variety_inherits_its_parents_category(self):
        from auctions.species_categories import assign_categories

        Species.objects.create(
            genus="Pterophyllum", species="scalare", variety="Koi", parent=self.angel, source="aquarium"
        )
        assign_categories()
        self.assertEqual(Species.objects.get(variety="Koi").category, self.cichlids)

    def test_the_curated_list_says_what_a_plant_is(self):
        """Only the list knows a Microsorum is a plant; the taxonomy map is a fish map."""
        from auctions.species_categories import assign_categories

        plants = Category.objects.create(name="Plants")
        fern = Species.objects.create(genus="Microsorum", species="pteropus", source="aquarium", family="Polypodiaceae")
        assign_categories()
        fern.refresh_from_db()
        self.assertEqual(fern.category, plants)

    def test_a_lot_takes_the_category_from_its_species(self):
        Species.objects.filter(pk=self.angel.pk).update(category=self.cichlids)
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species = Species.objects.get(pk=self.angel.pk)
        lot.species_category = None
        lot.category_checked = False
        lot.save()
        self.assertEqual(lot.species_category, self.cichlids)

    def test_the_species_beats_a_category_that_was_guessed(self):
        """The keyword guesser runs on the lot name; the species knows its family."""
        Species.objects.filter(pk=self.angel.pk).update(category=self.cichlids)
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species_category = self.livebearers
        lot.category_automatically_added = True
        lot.category_checked = True
        lot.species = Species.objects.get(pk=self.angel.pk)
        lot.save()
        self.assertEqual(lot.species_category, self.cichlids)

    def test_a_lot_with_no_species_still_gets_a_guess(self):
        # Created here rather than relied on: a full suite run flushes the test database, so the
        # seed row this falls back to is not there on a --keepdb re-run.
        Category.objects.get_or_create(name="Uncategorized")
        self.lot.species = None
        self.lot.species_category = None
        self.lot.category_checked = False
        self.lot.save()
        # Whatever guess_category made of it, the species didn't decide it and nothing crashed.
        self.assertIsNotNone(self.lot.species_category)


class SpeciesOnTheSingleLotFormTests(StandardTestCase):
    """The one form where the auction is picked in a dropdown, so the picker must always exist."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()

    def _form(self, auction=None, **kwargs):
        from auctions.forms import CreateLotForm

        return CreateLotForm(user=self.user, cloned_from=None, auction=auction, **kwargs)

    def test_the_picker_renders_even_with_no_auction_selected_yet(self):
        """The reported bug: /lots/new/ with nothing pre-selected had no picker at all."""
        form = self._form()
        self.assertFalse(form.fields["species"].widget.is_hidden)
        self.assertIn("No species", str(form["species"]))

    def test_the_page_renders_the_picker(self):
        # The new-lot view bounces anyone without contact details, and an empty 302 body would
        # make this test pass or fail for reasons that have nothing to do with the picker.
        self.user.first_name = "Test"
        self.user.last_name = "Seller"
        self.user.save()
        self.user.userdata.address = "123 Fish St"
        self.user.userdata.save()
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("new_lot"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('id="id_species"', body)
        self.assertIn("FishBase", body)
        # The escape hatch.  Everything in the picker comes from the lot name, so without a way to
        # search the list by hand a name the matcher can't place has no reachable species at all.
        self.assertIn("data-species-search", body)

    def test_the_auction_info_endpoint_says_whether_to_show_it(self):
        """What lets the dropdown re-hide the picker without a page reload."""
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(reverse("get_auction_info"), {"auction": self.online_auction.pk})
        self.assertTrue(response.json()["use_scientific_name"])
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        response = self.client.post(reverse("get_auction_info"), {"auction": self.online_auction.pk})
        self.assertFalse(response.json()["use_scientific_name"])

    def test_a_species_is_still_dropped_when_the_auction_does_not_use_the_field(self):
        """Rendering it always must not mean accepting it always."""
        from auctions.forms import clean_species_for_auction

        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        cleaned = clean_species_for_auction({"species": self.guppy}, self.online_auction, derive_category=True)
        self.assertIsNone(cleaned["species"])

    def test_picking_a_species_sets_the_category(self):
        from auctions.forms import clean_species_for_auction

        cichlids = Category.objects.create(name="Cichlids")
        wrong = Category.objects.create(name="Livebearers")
        Species.objects.filter(pk=self.guppy.pk).update(category=cichlids)
        self.guppy.refresh_from_db()
        cleaned = clean_species_for_auction(
            {"species": self.guppy, "species_category": wrong}, self.online_auction, derive_category=True
        )
        self.assertEqual(cleaned["species_category"], cichlids)

    def test_an_admin_editing_a_lot_keeps_the_category_they_chose(self):
        """The admin modal still shows the category field, so it is theirs to set.

        Saved rather than only cleaned: cleaning it was never the half that was broken.  A lot with
        a species re-derives its category on every save while ``category_automatically_added`` is
        set, so a version of this test that stopped at ``cleaned_data`` passed for a year while the
        admin's choice was reverted the moment the lot hit the database.
        """
        from auctions.forms import clean_species_for_auction

        cichlids = Category.objects.create(name="Cichlids")
        chosen = Category.objects.create(name="Livebearers")
        Species.objects.filter(pk=self.guppy.pk).update(category=cichlids)
        self.guppy.refresh_from_db()
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species = self.guppy
        lot.species_category = cichlids
        lot.category_automatically_added = True
        cleaned = clean_species_for_auction(
            {"species": self.guppy, "species_category": chosen}, self.online_auction, instance=lot
        )
        self.assertEqual(cleaned["species_category"], chosen)
        lot.species_category = cleaned["species_category"]
        lot.save()
        lot.refresh_from_db()
        self.assertEqual(lot.species_category, chosen)

    def test_the_suggestions_endpoint_reports_the_category(self):
        cichlids = Category.objects.create(name="Cichlids")
        Species.objects.filter(pk=self.guppy.pk).update(category=cichlids)
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(reverse("species_suggestions"), {"name": "guppy"})
        self.assertEqual(response.json()["choices"][0]["category"], "Cichlids")


class ScientificNameIsVisibleTests(StandardTestCase):
    """Where the scientific name shows up once a seller has picked one."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.lot.species = self.guppy
        self.lot.save()

    def test_the_lot_page_shows_it(self):
        body = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk})).content.decode()
        self.assertIn("Poecilia reticulata", body)

    def test_the_auction_lot_csv_has_a_column(self):
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("lot_list", kwargs={"slug": self.lot.auction.slug})
        body = self.client.get(url).content.decode()
        self.assertIn("Scientific name", body)
        self.assertIn("Poecilia reticulata", body)

    def test_the_column_is_left_out_when_the_auction_turned_the_field_off(self):
        self.lot.auction.use_scientific_name = False
        self.lot.auction.save()
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("lot_list", kwargs={"slug": self.lot.auction.slug})
        self.assertNotIn("Scientific name", self.client.get(url).content.decode())

    def test_my_lots_csv_has_a_column(self):
        self.client.login(username="my_lot", password="testpassword")
        body = self.client.get(reverse("my_lot_report")).content.decode()
        self.assertIn("Scientific name", body)

    def test_the_ar_overlay_carries_it(self):
        from django.test import RequestFactory

        from auctions.mobile.services.ar import build_lot_metadata

        rows = build_lot_metadata(self.lot.auction, [self.lot.pk], self.user, RequestFactory().get("/"))
        self.assertEqual(rows[0]["scientific_name"], "Poecilia reticulata")


class LegacySpeciesRowTests(StandardTestCase):
    """Rows that predate the import: typed by hand, no genus, and easy to destroy by accident."""

    def test_saving_a_hand_typed_row_does_not_erase_its_name(self):
        """Species.save() rebuilds scientific_name from genus + epithet.  With neither filled in,
        that used to blank the only name the row had -- an admin ticking a box was enough."""
        species = Species(scientific_name="Poecilia reticulata", common_name="Guppy", source="manual")
        species.save()
        species.refresh_from_db()
        self.assertEqual(species.scientific_name, "Poecilia reticulata")
        self.assertEqual(species.genus, "Poecilia")
        self.assertEqual(species.species, "reticulata")

    def test_a_split_hand_typed_row_can_be_searched_for(self):
        species = Species(scientific_name="Poecilia reticulata", common_name="Guppy", source="manual")
        species.save()
        self.assertEqual(search_matches("Poecilia reticulata pair"), [species])

    def test_the_importer_folds_a_duplicate_onto_the_imported_row(self):
        from auctions.management.commands.import_fishbase import Command

        imported = make_species("Poecilia", "reticulata", "Guppy")
        legacy = Species.objects.create(scientific_name="Poecilia reticulata", common_name="Guppy", source="manual")
        self.lot.species = legacy
        self.lot.save()
        Command()._merge_legacy(dry_run=False)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, imported)
        self.assertFalse(Species.objects.filter(pk=legacy.pk).exists())

    def test_a_hand_typed_row_with_no_match_is_left_alone(self):
        from auctions.management.commands.import_fishbase import Command

        legacy = Species.objects.create(scientific_name="Made Upicus", common_name="Nothing", source="manual")
        Command()._merge_legacy(dry_run=False)
        legacy.refresh_from_db()
        self.assertEqual(legacy.scientific_name, "Made Upicus")

    def test_a_dry_run_changes_nothing(self):
        from auctions.management.commands.import_fishbase import Command

        make_species("Poecilia", "reticulata", "Guppy")
        legacy = Species.objects.create(scientific_name="Poecilia reticulata", source="manual")
        Command()._merge_legacy(dry_run=True)
        self.assertTrue(Species.objects.filter(pk=legacy.pk).exists())


class TradeRankTests(StandardTestCase):
    """How likely it is that anybody actually keeps a species, in three steps."""

    def test_a_flagged_species_ranks_first_without_waiting_for_a_pass(self):
        """save() owns the species' own tier; only the genus one needs the full recompute."""
        species = make_species("Tropheus", "duboisi", aquarium_use="commercial")
        self.assertEqual(Species.objects.get(pk=species.pk).trade_rank, Species.TRADE_RANK_SPECIES)

    def test_a_genus_with_a_kept_species_lifts_its_siblings(self):
        """The reason this tier exists: FishBase files Chindongo saulosi under 'never/rarely'."""
        make_species("Chindongo", "demasoni", aquarium_use="commercial")
        sibling = make_species("Chindongo", "saulosi", aquarium_use="never/rarely")
        stranger = make_species("Gadus", "morhua")
        Species.recompute_trade_ranks()
        self.assertEqual(Species.objects.get(pk=sibling.pk).trade_rank, Species.TRADE_RANK_GENUS)
        self.assertEqual(Species.objects.get(pk=stranger.pk).trade_rank, Species.TRADE_RANK_NONE)

    def test_an_admin_can_overrule_fishbase(self):
        species = make_species("Chindongo", "saulosi")
        Species.objects.filter(pk=species.pk).update(aquarium_use="never/rarely", in_trade_override=True)
        Species.recompute_trade_ranks()
        self.assertEqual(Species.objects.get(pk=species.pk).trade_rank, Species.TRADE_RANK_SPECIES)

    def test_the_curated_list_counts_without_fishbase_having_an_opinion(self):
        species = make_species("Neocaridina", "davidi", source="aquarium")
        Species.recompute_trade_ranks()
        self.assertEqual(Species.objects.get(pk=species.pk).trade_rank, Species.TRADE_RANK_SPECIES)

    def test_the_genus_tier_cannot_rescue_a_genus_that_is_too_big(self):
        """Every member of a genus shares its genus rank, so the narrowing uses the species one."""
        for index in range(MAX_GENUS_MATCHES + 4):
            make_species("Ancistrus", f"species{index}")
        kept = make_species("Ancistrus", "cirrhosus", "Bristlenose", aquarium_use="commercial")
        Species.recompute_trade_ranks()
        self.assertEqual(search_matches("Ancistrus sp. L144"), [kept])


class SpeciesGapsViewTests(StandardTestCase):
    """The work queue of lots that should have a scientific name and don't."""

    def setUp(self):
        super().setUp()
        self.url = reverse("species_gaps")
        # AdminOnlyViewMixin means a *site* superuser, not an auction admin.
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.lot.species = None
        self.lot.lot_name = "Blue dream shrimp"
        self.lot.i_bred_this_fish = True
        self.lot.save()

    def test_admins_only(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_lists_a_lot_with_no_species(self):
        self.client.login(username="species_admin", password="testpassword")
        body = self.client.get(self.url).content.decode()
        self.assertIn("Blue dream shrimp", body)
        self.assertIn("never looked up", body)

    def test_a_lot_that_has_a_species_is_not_a_gap(self):
        self.lot.species = make_species("Neocaridina", "davidi", "Cherry shrimp")
        self.lot.save()
        self.client.login(username="species_admin", password="testpassword")
        self.assertNotIn("Blue dream shrimp", self.client.get(self.url).content.decode())

    def test_an_auction_that_turned_the_field_off_is_not_a_gap(self):
        """Nobody was ever offered the choice, so there is nothing missing."""
        self.lot.auction.use_scientific_name = False
        self.lot.auction.save()
        self.client.login(username="species_admin", password="testpassword")
        self.assertNotIn("Blue dream shrimp", self.client.get(self.url).content.decode())

    def test_it_says_what_the_matcher_decided_last_time(self):
        SpeciesSearchCache.objects.create(search_text="blue dream shrimp", species=None, source="llm")
        self.client.login(username="species_admin", password="testpassword")
        body = self.client.get(self.url).content.decode()
        self.assertIn("not a species", body)
        self.assertIn("decided by the language model", body)

    def test_a_name_that_cannot_be_a_species_is_dropped(self):
        """ "pair" is on the site's ignore list, so nothing here can name anything."""
        self.lot.lot_name = "10 pair"
        self.lot.save()
        self.client.login(username="species_admin", password="testpassword")
        self.assertNotIn("10 pair", self.client.get(self.url).content.decode())

    def test_a_remembered_species_is_listed_too(self):
        """The half of the cache that used to be invisible.

        A remembered *wrong* species is worse than a remembered "no": it is served ahead of the
        token search, to every club, and it ends up on a printed label and in a breeder award.
        The page only ever listed the "not a species" rows, so there was nowhere on the site a
        wrong one could be seen.
        """
        species = make_species("Caridina", "multidentata", "Amano shrimp")
        SpeciesSearchCache.objects.create(search_text="blue dream shrimp", species=species, source="user")
        self.client.login(username="species_admin", password="testpassword")
        body = self.client.get(self.url).content.decode()
        self.assertIn("Caridina multidentata", body)
        self.assertIn("Look it up again", body)


class SpeciesSearchCacheForgetTests(StandardTestCase):
    """Undoing a remembered answer, which before this had to be done in the Django admin."""

    def setUp(self):
        super().setUp()
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.row = SpeciesSearchCache.objects.create(
            search_text="angelfish", species=make_species("Holacanthus", "bermudensis"), source="user"
        )
        self.url = reverse("species_cache_forget", kwargs={"pk": self.row.pk})

    def test_admins_only(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertTrue(SpeciesSearchCache.objects.filter(pk=self.row.pk).exists())

    def test_forgetting_lets_the_matcher_answer_again(self):
        self.client.login(username="species_admin", password="testpassword")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SpeciesSearchCache.objects.filter(pk=self.row.pk).exists())

    def test_get_does_not_delete(self):
        self.client.login(username="species_admin", password="testpassword")
        self.client.get(self.url)
        self.assertTrue(SpeciesSearchCache.objects.filter(pk=self.row.pk).exists())


class SpeciesCreateViewTests(StandardTestCase):
    """Adding a species, or a strain of one, without opening the Django admin."""

    def setUp(self):
        super().setUp()
        self.url = reverse("species_create")
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")

    def _post(self, **overrides):
        data = {
            "scientific_name_input": "Ancistrus cirrhosus",
            "common_name": "Bristlenose pleco",
            "other_names": "bn pleco, bushynose pleco",
            "variety": "",
            "parent": "",
            "category": "",
            "freshwater": "on",
            "breeder_points": "on",
            "lot_name": "",
            "attach_to_lots": "",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_admins_only(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_scientific_name_is_split_into_genus_and_epithet(self):
        self._post()
        species = Species.objects.get(scientific_name="Ancistrus cirrhosus")
        self.assertEqual(species.genus, "Ancistrus")
        self.assertEqual(species.species, "cirrhosus")
        self.assertEqual(species.source, "admin")

    def test_every_common_name_becomes_searchable(self):
        self._post()
        self.assertEqual(list(exact_matches("bn pleco"))[0].scientific_name, "Ancistrus cirrhosus")
        self.assertEqual(list(exact_matches("bushynose pleco"))[0].scientific_name, "Ancistrus cirrhosus")

    def test_adding_one_by_hand_counts_as_in_the_hobby(self):
        """Somebody is adding it because a club is selling one, which beats FishBase's column."""
        self._post()
        species = Species.objects.get(scientific_name="Ancistrus cirrhosus")
        self.assertTrue(species.in_trade_override)
        self.assertEqual(species.trade_rank, Species.TRADE_RANK_SPECIES)

    def test_a_genus_on_its_own_is_allowed(self):
        self._post(scientific_name_input="Bucephalandra", common_name="Bucephalandra")
        species = Species.objects.get(genus="Bucephalandra")
        self.assertEqual(species.species, "")
        self.assertEqual(species.scientific_name, "Bucephalandra")

    def test_a_strain_hangs_off_its_parent(self):
        parent = make_species("Neocaridina", "davidi", "Cherry shrimp")
        self._post(
            scientific_name_input="",
            common_name="Blue dream shrimp",
            other_names="blue dream",
            variety="Blue Dream",
            parent=str(parent.pk),
        )
        strain = Species.objects.get(variety="Blue Dream")
        self.assertEqual(strain.parent, parent)
        # It keeps the parent's name, which is what everything taxonomic reads.
        self.assertEqual(strain.scientific_name, "Neocaridina davidi")
        self.assertEqual(strain.full_scientific_name, "Neocaridina davidi 'Blue Dream'")

    def test_a_strain_without_a_parent_is_refused(self):
        response = self._post(variety="Blue Dream")
        self.assertContains(response, "which species it is a strain of")
        self.assertFalse(Species.objects.filter(variety="Blue Dream").exists())

    def test_a_parent_without_a_strain_name_is_refused(self):
        parent = make_species("Neocaridina", "davidi", "Cherry shrimp")
        response = self._post(scientific_name_input="", parent=str(parent.pk))
        self.assertContains(response, "Give the strain a name")

    def test_a_species_that_already_exists_is_refused_with_a_link(self):
        existing = make_species("Ancistrus", "cirrhosus", "Bristlenose")
        response = self._post()
        self.assertContains(response, "already on the list")
        self.assertContains(response, f"/admin/auctions/species/{existing.pk}/change/")
        self.assertEqual(Species.objects.filter(scientific_name="Ancistrus cirrhosus").count(), 1)

    def test_it_attaches_the_species_to_the_lots_with_that_name(self):
        self.lot.species = None
        self.lot.lot_name = "Bristlenose pleco"
        self.lot.save()
        self._post(lot_name="Bristlenose pleco", attach_to_lots="on")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species.scientific_name, "Ancistrus cirrhosus")
        # ...and teaches the matcher, so the next person typing it is offered it straight away.
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="bristlenose pleco").species, self.lot.species)

    def test_it_never_overwrites_a_species_somebody_already_picked(self):
        guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.lot.species = guppy
        self.lot.lot_name = "Bristlenose pleco"
        self.lot.save()
        self._post(lot_name="Bristlenose pleco", attach_to_lots="on")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, guppy)

    def test_the_lots_take_the_new_species_category(self):
        catfish = Category.objects.create(name="Catfish")
        self.lot.species = None
        self.lot.lot_name = "Bristlenose pleco"
        self.lot.species_category = None
        self.lot.category_checked = False
        self.lot.save()
        self._post(lot_name="Bristlenose pleco", attach_to_lots="on", category=str(catfish.pk))
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species_category, catfish)

    def test_the_gaps_page_links_here_with_the_name_prefilled(self):
        self.lot.species = None
        self.lot.lot_name = "Bristlenose pleco"
        self.lot.save()
        body = self.client.get(reverse("species_gaps")).content.decode()
        self.assertIn("lot_name=Bristlenose", body)
        form_page = self.client.get(self.url, {"lot_name": "Bristlenose pleco"}).content.decode()
        self.assertIn("Bristlenose pleco", form_page)


class SpeciesAutocompleteTests(StandardTestCase):
    """The parent picker on the add-species form."""

    def setUp(self):
        super().setUp()
        self.url = reverse("species-autocomplete")
        self.shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp")
        self.client.login(username="my_lot", password="testpassword")

    def test_it_finds_a_species_by_scientific_name(self):
        response = self.client.get(self.url, {"q": "Neocaridina"})
        self.assertIn("Neocaridina davidi", response.content.decode())

    def test_it_finds_a_species_by_common_name(self):
        response = self.client.get(self.url, {"q": "cherry"})
        self.assertIn("Neocaridina davidi", response.content.decode())

    def test_a_strain_is_never_offered_as_a_parent(self):
        """A strain of a strain is not a thing, and nothing else knows how to walk that chain."""
        Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=self.shrimp, source="aquarium"
        )
        response = self.client.get(self.url, {"q": "Neocaridina"})
        self.assertNotIn("Blue Dream", response.content.decode())


class CategoryChosenByAPersonSticksTests(StandardTestCase):
    """A category somebody picked by hand survives the next save.

    ``Lot._do_save`` re-derives the category from the species whenever
    ``category_automatically_added`` is set, and that flag is the only record of who put the lot
    where it is.  Every path that lets a person choose a category has to clear it, or the choice is
    silently undone -- the modal closes, the page says 200, and the category is back.
    """

    def setUp(self):
        super().setUp()
        self.cichlids = Category.objects.create(name="Cichlids", bap_points=5)
        self.plants = Category.objects.create(name="Aquatic plants", bap_points=5)
        self.angel = make_species("Pterophyllum", "scalare", "Freshwater angelfish")
        Species.objects.filter(pk=self.angel.pk).update(category=self.cichlids)
        self.angel.refresh_from_db()
        self.club = Club.objects.create(name="Category club", enable_breeder_award_program=True)
        self.online_auction.club = self.club
        self.online_auction.save()
        self.bap_user = User.objects.create_user(username="bap_admin", password="testpassword")
        ClubMember.objects.create(club=self.club, user=self.bap_user, name="Bap", permission_manage_bap=True)
        self.lot.species = self.angel
        self.lot.species_category = self.cichlids
        self.lot.category_automatically_added = True
        self.lot.save()

    def test_the_bap_admin_modal_choice_is_not_reverted(self):
        """The reported bug: POST "Aquatic plants", get a 200, and the lot is back in Cichlids."""
        self.client.login(username="bap_admin", password="testpassword")
        response = self.client.post(
            reverse("club_bap_lot_category", kwargs={"pk": self.lot.pk}),
            {"species_category": self.plants.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species_category, self.plants)
        self.assertFalse(self.lot.category_automatically_added)
        # ...and it is still there after anything else touches the lot.
        self.lot.save()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species_category, self.plants)

    def test_changing_the_category_on_any_form_survives_the_save(self):
        """What every form with a visible category picker calls; the admin lot-edit modal is one."""
        from auctions.forms import note_category_chosen_by_person

        lot = Lot.objects.get(pk=self.lot.pk)
        note_category_chosen_by_person(lot, {"species_category": self.plants})
        lot.species_category = self.plants
        lot.save()
        lot.refresh_from_db()
        self.assertEqual(lot.species_category, self.plants)

    def test_re_saving_without_touching_the_category_is_not_a_decision(self):
        """Otherwise the first unrelated edit would freeze a machine-set category forever."""
        from auctions.forms import note_category_chosen_by_person

        lot = Lot.objects.get(pk=self.lot.pk)
        note_category_chosen_by_person(lot, {"species_category": self.cichlids})
        self.assertTrue(lot.category_automatically_added)

    def test_a_machine_set_category_still_follows_the_species(self):
        """The behaviour this is not allowed to break: nobody chose Livebearers, so it moves."""
        livebearers = Category.objects.create(name="Livebearers")
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species_category = livebearers
        lot.category_automatically_added = True
        lot.save()
        self.assertEqual(lot.species_category, self.cichlids)


class TurningTheFieldOffKeepsTheSpeciesTests(StandardTestCase):
    """``use_scientific_name`` hides the field.  It does not delete the column.

    A club that switches the setting off to see what it does -- or off and back on -- used to lose
    the species from every lot anybody touched in between, permanently, with the labels and the CSV
    staying blank afterwards.
    """

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()

    def test_an_ajax_save_of_another_field_does_not_wipe_it(self):
        self.client.login(username="my_lot", password="testpassword")
        url = reverse("save_lot_ajax", kwargs={"slug": self.online_auction.slug})
        created = self.client.post(
            url,
            data={"lot_name": "Fancy guppy pair", "quantity": 1, "reserve_price": 2, "species": self.guppy.pk},
            content_type="application/json",
        ).json()
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        response = self.client.post(
            url,
            data={"lot_id": created["lot_id"], "lot_name": "Fancy guppy pair", "quantity": 2, "reserve_price": 2},
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"], response.json())
        lot = Lot.objects.get(lot_number=created["lot_id"])
        self.assertEqual(lot.species, self.guppy)

    def test_cleaning_a_form_keeps_what_is_stored(self):
        from auctions.forms import clean_species_for_auction

        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species = self.guppy
        lot.save()
        cleaned = clean_species_for_auction({"species": None}, self.online_auction, instance=lot)
        self.assertEqual(cleaned["species"], self.guppy)

    def test_a_posted_species_still_cannot_get_in(self):
        """Keeping the stored value is not the same as accepting a hand-rolled POST."""
        from auctions.forms import clean_species_for_auction

        betta = make_species("Betta", "splendens", "Siamese fighting fish")
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species = self.guppy
        lot.save()
        cleaned = clean_species_for_auction({"species": betta}, self.online_auction, instance=lot)
        self.assertEqual(cleaned["species"], self.guppy)

    def test_the_name_is_hidden_while_the_setting_is_off_and_comes_back_after(self):
        """One display rule, everywhere -- the lot page and the label used to disagree."""
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species = self.guppy
        lot.save()
        self.assertEqual(lot.scientific_name, "Poecilia reticulata")
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.scientific_name, "")
        self.assertEqual(lot.scientific_name_label, "")
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.scientific_name, "Poecilia reticulata")


class BapCategoryNamesResolveTests(StandardTestCase):
    """The three category names the BAP rules match on by name have to be reachable.

    ``bap_placeholder``, the CAP-disabled ineligibility rule and the quantity-minimum exemption all
    compare ``species_category.name`` against these exact strings, so a species that resolves to
    anything else -- or to nothing -- silently drops out of the Culture and HAP tracks.
    """

    def setUp(self):
        super().setUp()
        from auctions.species_categories import CategoryResolver

        self.plants = Category.objects.create(name="Aquatic plants")
        self.inverts = Category.objects.create(name="Snails and other inverts")
        self.cultures = Category.objects.create(name="Live food cultures")
        self.resolver = CategoryResolver()

    def test_plants(self):
        self.assertEqual(self.resolver.resolve("plants"), self.plants)

    def test_invertebrates(self):
        self.assertEqual(self.resolver.resolve("invertebrates"), self.inverts)

    def test_live_food(self):
        self.assertEqual(self.resolver.resolve("live food"), self.cultures)

    def test_a_curated_shrimp_lands_in_the_invert_category(self):
        """End to end, because this is the half of the curated CSV that had no category at all."""
        from auctions.species_categories import assign_categories

        shrimp = Species.objects.create(genus="Neocaridina", species="davidi", source="aquarium")
        assign_categories()
        shrimp.refresh_from_db()
        self.assertEqual(shrimp.category, self.inverts)


class SpeciesBreederPointsFlagTests(StandardTestCase):
    """``Species.breeder_points`` is on the add-species form, so unticking it has to mean something."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Breeder club",
            enable_breeder_award_program=True,
            auto_add_points=True,
            min_quantity=1,
            only_sold_lots=False,
            only_active_members_can_participate=False,
            points_per_lot=5,
        )
        self.category = Category.objects.create(name="Cichlids", bap_points=5)
        self.online_auction.club = self.club
        self.online_auction.save()
        ClubMember.objects.create(club=self.club, user=self.user, name="seller")
        self.species = make_species("Tropheus", "duboisi", "White spotted cichlid")
        self.lot.species = self.species
        self.lot.species_category = self.category
        self.lot.i_bred_this_fish = True
        self.lot.user = self.user
        self.lot.date_end = timezone.now()
        self.lot.save()

    def test_a_species_that_earns_points_is_eligible(self):
        self.assertIsNone(self.lot.unsold_lot_no_bap_reason)

    def test_unticking_it_makes_the_lot_ineligible(self):
        Species.objects.filter(pk=self.species.pk).update(breeder_points=False)
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "species_not_eligible")

    def test_no_award_is_generated(self):
        """The probe that found this: a species with breeder_points=False still made 5 points."""
        Species.objects.filter(pk=self.species.pk).update(breeder_points=False)
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.auto_award_bap_points()
        self.assertFalse(BapAward.objects.filter(lot=lot).exists())
        self.assertEqual(lot.bap_auto_reason, "species_not_eligible")

    def test_a_strain_answers_for_its_parent(self):
        """The reason not to award points is a fact about the animal, not about the colour form."""
        Species.objects.filter(pk=self.species.pk).update(breeder_points=False)
        Species.objects.create(
            genus="Tropheus", species="duboisi", variety="Maswa", parent=self.species, source="admin"
        )
        strain = Species.objects.get(variety="Maswa")
        self.assertFalse(strain.earns_breeder_points)


@isolated_cache("species-quantity")
class QuantityInLotNamesTests(StandardTestCase):
    """ "6 guppies" is the commonest shape a lot name comes in, and it used to find nothing.

    exact_matches asks whether the *whole* typed name is a species name, so a leading count killed
    it; and the search step could not rescue it either, because its common-name rule needs a
    two-word phrase and "guppy" is one word.  Between them the single most common phrasing on the
    site had no answer at all while the same name without the count worked.
    """

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.cardinal = make_species("Paracheirodon", "axelrodi", "Cardinal tetra")

    def test_a_leading_count_is_ignored(self):
        found, source = suggest_species("6 guppies", use_llm=False)
        self.assertEqual(list(found), [self.guppy])
        self.assertEqual(source, "exact")

    def test_a_count_and_a_plural_together(self):
        found, _ = suggest_species("20 cardinal tetras", use_llm=False)
        self.assertEqual(list(found), [self.cardinal])

    def test_quantity_words_at_either_end(self):
        for name in ("trio of guppies", "guppy pair", "lot of 3 guppies", "guppies x 6"):
            with self.subTest(name=name):
                found, _ = suggest_species(name, use_llm=False)
                self.assertEqual(list(found), [self.guppy], name)

    def test_it_never_reaches_into_the_middle_of_a_name(self):
        """The cultivars are spelled out of ordinary adjectives; trimming inside would break them."""
        self.assertEqual(strip_quantity("blue dream shrimp"), "blue dream shrimp")
        self.assertEqual(strip_quantity("mickey mouse platy"), "mickey mouse platy")

    def test_hardware_still_answers_nothing(self):
        """The counts come off equipment names too; that must not turn one into a species."""
        for name in ("10 gallon tank", "5 gallon bucket", "2 sponge filters", "bag of gravel"):
            with self.subTest(name=name):
                found, _ = suggest_species(name, use_llm=False)
                self.assertEqual(list(found), [], name)

    def test_a_name_that_is_only_a_count_is_not_a_species(self):
        self.assertEqual(strip_quantity("6"), "")
        found, source = suggest_species("6", use_llm=False)
        self.assertEqual(list(found), [])
        self.assertEqual(source, "none")


@isolated_cache("species-habitat")
class FreshwaterRankingTests(StandardTestCase):
    """FishBase's habitat columns, used for the job the model comment says they were kept for.

    trade_rank cannot separate a reef angelfish from a freshwater one -- both are flagged for the
    aquarium trade -- so "Angelfish" answered with marine angelfish and never offered
    *Pterophyllum scalare* at all.
    """

    def test_a_freshwater_species_outranks_a_marine_one_sharing_a_name(self):
        marine = make_species("Holacanthus", "bermudensis", "Angelfish", aquarium_use="commercial")
        marine.saltwater = True
        marine.save()
        fresh = make_species("Pterophyllum", "scalare", "Freshwater angelfish", ["Angelfish"])
        fresh.freshwater = True
        fresh.save()
        found, _ = suggest_species("angelfish", use_llm=False)
        self.assertEqual(list(found)[0], fresh, "the freshwater fish has to come first")
        self.assertIn(marine, found, "the marine one is still offered, just not first")

    def test_the_lots_category_still_wins_over_habitat(self):
        """Habitat is only a tie-break, and it sits below the category the lot already looks like."""
        marine_category = Category.objects.create(name="Marine")
        marine = make_species("Holacanthus", "bermudensis", "Angelfish", aquarium_use="commercial")
        marine.saltwater = True
        marine.category = marine_category
        marine.save()
        fresh = make_species("Pterophyllum", "scalare", "Freshwater angelfish", ["Angelfish"])
        fresh.freshwater = True
        fresh.save()
        found, _ = suggest_species("angelfish", use_llm=False, category=marine_category.pk)
        self.assertEqual(list(found)[0], marine)


class NormalizedCommonNameTests(StandardTestCase):
    """A fifth of FishBase's common names carry punctuation the query has already had stripped."""

    def test_a_name_with_an_apostrophe_is_reachable(self):
        species = make_species("Corydoras", "adolfoi", "Adolf's catfish")
        self.assertEqual(list(exact_matches("adolfs catfish")), [species])
        self.assertEqual(list(exact_matches("Adolf's catfish")), [species])

    def test_the_species_own_common_name_is_normalized_too(self):
        """exact_matches queries Species.common_name directly, not only the common-name table."""
        species = Species.objects.create(genus="Corydoras", species="agassizii", common_name="Agassiz's corydoras")
        self.assertEqual(species.common_name_normalized, "agassizs corydoras")
        self.assertEqual(list(exact_matches("Agassiz's corydoras")), [species])

    def test_a_hyphenated_name_with_a_count_is_reached_exactly(self):
        """ "3 black-banded leporinus" is the whole name plus a count, so it never reaches search.

        strip_quantity() takes the count off before exact_matches asks its question, which is why
        this answers by the most trustworthy route rather than by phrase matching.  The hyphen is
        the other half: normalize() turns it into a space on both sides of the lookup.
        """
        species = make_species("Leporinus", "fasciatus", "Black-banded leporinus")
        found, source = suggest_species("3 black-banded leporinus", use_llm=False)
        self.assertEqual(list(found), [species])
        self.assertEqual(source, "exact")

    def test_a_hyphenated_name_is_reachable_as_a_phrase(self):
        """The same name buried in a longer one, where only phrase matching can find it."""
        species = make_species("Leporinus", "fasciatus", "Black-banded leporinus")
        found, source = suggest_species("black-banded leporinus wild caught", use_llm=False)
        self.assertEqual(list(found), [species])
        self.assertEqual(source, "search")

    def test_the_column_is_rebuilt_on_save(self):
        common = SpeciesCommonName.objects.create(
            species=make_species("Poecilia", "reticulata"), name="Ram's horn snail"
        )
        self.assertEqual(common.name_normalized, "rams horn snail")
        common.name = "Ramshorn snail"
        common.save()
        self.assertEqual(common.name_normalized, "ramshorn snail")


class PointsPerLotZeroTests(StandardTestCase):
    """0 points per lot means nought, not "fall through to the category default"."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Points club", enable_breeder_award_program=True)
        self.category = Category.objects.create(name="Cichlids", bap_points=7)
        self.online_auction.club = self.club
        self.online_auction.save()
        self.lot.species_category = self.category
        self.lot.save()

    def test_blank_falls_through_to_the_category(self):
        self.club.points_per_lot = None
        self.club.save()
        self.assertEqual(self.lot.bap_points_for_club(self.club), 7)

    def test_zero_means_zero(self):
        self.club.points_per_lot = 0
        self.club.save()
        self.assertEqual(self.lot.bap_points_for_club(self.club), 0)

    def test_a_flat_rate_still_beats_the_category(self):
        self.club.points_per_lot = 3
        self.club.save()
        self.assertEqual(self.lot.bap_points_for_club(self.club), 3)


class BackfillLotSpeciesCommandTests(StandardTestCase):
    """The one pass that clears the historical backlog the gaps page is otherwise full of."""

    def setUp(self):
        super().setUp()
        self.tropheus = make_species("Tropheus", "duboisi", "White spotted cichlid")
        self.cichlids = Category.objects.create(name="Cichlids")
        Species.objects.filter(pk=self.tropheus.pk).update(category=self.cichlids)
        self.tropheus.refresh_from_db()
        self.lot.lot_name = "Tropheus duboisi maswa"
        self.lot.species = None
        self.lot.save()

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("backfill_lot_species", *args, stdout=out)
        return out.getvalue()

    def test_it_sets_the_species_the_matcher_is_sure_about(self):
        self._run()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.tropheus)

    def test_a_dry_run_writes_nothing(self):
        output = self._run("--dry-run")
        self.assertIn("Tropheus duboisi", output)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)

    def test_it_leaves_a_name_it_cannot_place_alone(self):
        self.lot.lot_name = "Sponge filter"
        self.lot.save()
        self._run()
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)

    def test_it_never_overwrites_a_species_somebody_picked(self):
        guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.lot.species = guppy
        self.lot.save()
        self._run()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, guppy)

    def test_the_category_is_left_alone_by_default(self):
        """Deriving it can move a lot between the BAP, HAP and Culture tracks; awards would disagree."""
        livebearers = Category.objects.create(name="Livebearers")
        Lot.objects.filter(pk=self.lot.pk).update(species_category=livebearers, category_automatically_added=True)
        self._run()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.tropheus)
        self.assertEqual(self.lot.species_category, livebearers)

    def test_set_category_only_moves_uncategorized_lots(self):
        uncategorized, _ = Category.objects.get_or_create(name="Uncategorized")
        Lot.objects.filter(pk=self.lot.pk).update(species_category=uncategorized)
        self._run("--set-category")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species_category, self.cichlids)

    def test_set_category_skips_a_lot_that_already_has_an_award(self):
        uncategorized, _ = Category.objects.get_or_create(name="Uncategorized")
        Lot.objects.filter(pk=self.lot.pk).update(species_category=uncategorized)
        club = Club.objects.create(name="Award club", enable_breeder_award_program=True)
        member = ClubMember.objects.create(club=club, user=self.user, name="seller")
        BapAward.objects.create(club_member=member, lot=self.lot, date=timezone.now().date(), points=5)
        self._run("--set-category")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.tropheus)
        self.assertEqual(self.lot.species_category, uncategorized)

    def test_the_auction_filter_leaves_other_auctions_alone(self):
        self._run("--auction", self.in_person_auction.slug)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)


class SpeciesGapsAttachByNormalizedNameTests(StandardTestCase):
    """The gaps page links with a normalised name; the attach has to match on one."""

    def setUp(self):
        super().setUp()
        self.url = reverse("species_create")
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        self.lot.lot_name = "Ram's Horn Snails"
        self.lot.species = None
        self.lot.save()

    def test_it_attaches_to_the_lots_the_normalised_name_came_from(self):
        # Exactly what the "not a species" table links with: the search cache never stored the
        # original spelling, so the normalised key is the only name that page has.
        self.assertEqual(normalize(self.lot.lot_name), "rams horn snails")
        response = self.client.post(
            self.url,
            {
                "scientific_name_input": "Planorbella duryi",
                "common_name": "Ramshorn snail",
                "other_names": "",
                "variety": "",
                "parent": "",
                "category": "",
                "freshwater": "on",
                "breeder_points": "on",
                "lot_name": normalize(self.lot.lot_name),
                "attach_to_lots": "on",
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species.scientific_name, "Planorbella duryi")


class SpeciesSmallerFixesTests(StandardTestCase):
    """The rest of the review's smaller items, each one line of behaviour."""

    def setUp(self):
        super().setUp()
        self.tropheus = make_species("Tropheus", "duboisi", "White spotted cichlid")

    def test_lot_search_finds_a_lot_by_its_scientific_name(self):
        """Nothing in the lot's own name says Tropheus; the species the seller picked does."""
        from auctions.filters import LotFilter

        self.lot.lot_name = "Six young fish"
        self.lot.species = self.tropheus
        self.lot.save()
        searched = LotFilter(user=self.user).text_filter(Lot.objects.filter(pk=self.lot.pk), "q", "Tropheus")
        self.assertIn(self.lot, searched)
        common = LotFilter(user=self.user).text_filter(Lot.objects.filter(pk=self.lot.pk), "q", "White spotted")
        self.assertIn(self.lot, common)

    def test_the_genus_box_has_the_datalist_it_points_at(self):
        club = Club.objects.create(name="Datalist club", enable_breeder_award_program=True)
        bap_user = User.objects.create_user(username="datalist_admin", password="testpassword")
        ClubMember.objects.create(club=club, user=bap_user, name="Bap", permission_manage_bap=True)
        self.online_auction.club = club
        self.online_auction.save()
        self.lot.species = self.tropheus
        self.lot.save()
        self.client.login(username="datalist_admin", password="testpassword")
        body = self.client.get(reverse("club_bap_settings", kwargs={"slug": club.slug})).content.decode()
        self.assertIn('id="bap-genus-list"', body)
        self.assertIn('value="Tropheus"', body)

    def test_the_bulk_add_page_clears_a_cloned_row_species(self):
        """A cloned row's suggestions belong to the previous lot name, not this one."""
        from django.template.loader import get_template

        source = get_template("auctions/bulk_add_lots.html").template.source
        self.assertIn("newElement.find('select[name$=\"-species\"]')", source)
        self.assertIn("delete this.dataset.userChosen", source)


class CopyingLotsToANewAuctionTests(StandardTestCase):
    """Relisting is where a scientific name is most worth keeping.

    The same fish, from the same breeder, a season later.  ``CLONE_LOT_FIELDS`` carried the
    category but not the species, so every copied lot arrived blank and the seller had to pick it
    again -- which is exactly the moment they don't, because the form already looks filled in.
    """

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.next_auction = Auction.objects.create(
            created_by=self.user,
            title="Next season",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=5),
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=1),
        )
        self.old_lot = Lot.objects.get(pk=self.lot.pk)
        self.old_lot.user = self.user
        self.old_lot.lot_name = "Fancy guppy trio"
        self.old_lot.species = self.guppy
        self.old_lot.save()

    def test_clone_lot_values_carries_the_species(self):
        from auctions.services import clone_lot_values

        self.assertEqual(clone_lot_values(self.old_lot)["species"], self.guppy)

    def test_the_copy_form_starts_with_the_species_filled_in(self):
        from auctions.forms import CreateLotForm

        form = CreateLotForm(user=self.user, cloned_from=self.old_lot.pk, auction=self.next_auction)
        self.assertEqual(form.fields["species"].initial, self.guppy)
        # ...and it renders, rather than being an initial nothing can see.
        self.assertIn("Poecilia reticulata", str(form["species"]))

    def test_the_new_auction_still_decides(self):
        """Copying is not a way round an auction that has the field switched off."""
        from auctions.forms import clean_species_for_auction

        self.next_auction.use_scientific_name = False
        self.next_auction.save()
        cleaned = clean_species_for_auction(
            {"species": self.guppy, "species_category": None},
            self.next_auction,
            derive_category=True,
            instance=Lot(),
        )
        self.assertIsNone(cleaned["species"])

    def test_the_palette_relist_carries_it_as_a_pk(self):
        """That path builds form *data*, not initial, so an object would fail to validate."""
        from auctions.forms import quick_add_lot_form_class
        from auctions.services import clone_lot_values

        data = clone_lot_values(self.old_lot)
        data["species_category"] = self.old_lot.species_category_id
        data["species"] = self.old_lot.species_id
        data["reserve_price"] = 2
        tos = AuctionTOS.objects.create(
            user=self.user, auction=self.next_auction, pickup_location=self.location, bidder_number="601"
        )
        form = quick_add_lot_form_class()(data=data, auction=self.next_auction, is_admin=False, tos=tos)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["species"], self.guppy)


class ScientificNameStaysOnForFutureAuctionsTests(StandardTestCase):
    """Nothing in this branch may leave a club's next auction with the field switched off."""

    def test_a_brand_new_auction_has_it_on(self):
        fresh = Auction.objects.create(title="Brand new", created_by=self.user, date_start=timezone.now())
        self.assertTrue(fresh.use_scientific_name)

    def test_a_clone_of_an_auction_that_has_it_keeps_it(self):
        """The setting is in fields_to_clone, so whatever the source says propagates forward."""
        self.assertTrue(self.online_auction.use_scientific_name)
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(
            f"{reverse('create_auction')}?clone={self.online_auction.slug}",
            {
                "title": "Next season",
                "cloned_from": self.online_auction.slug,
                "date_start": (timezone.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "date_end": (timezone.now() + datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        clone = Auction.objects.filter(title="Next season").first()
        self.assertIsNotNone(clone)
        self.assertTrue(clone.use_scientific_name)
        self.assertIn("scientific_name", clone.label_print_fields)

    def test_no_migration_turned_it_off_for_an_auction_taking_lots(self):
        """0388 deliberately leaves the setting alone; see its docstring for why."""
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=5)
        self.online_auction.save()
        self.online_auction.refresh_from_db()
        self.assertTrue(self.online_auction.use_scientific_name)


class UnconfiguredProvider(FakeProvider):
    """A deployment that never set an API key: ``get_provider()`` returns one of these anyway.

    So ``assist_enabled()`` -- not the provider being absent -- is what says the model is off.
    """

    def is_configured(self):
        return False


@isolated_cache("species-lookup-api")
class ClubSpeciesLookupAPITests(StandardTestCase):
    """The club API endpoint: /api/v1/clubs/<slug>/species-lookup/

    It is the lot form's matcher behind an API key, so these tests are mostly about the two things
    that are new -- who is allowed to ask, and what the answer looks like on the wire -- plus the
    one rule that keeps it cheap: the language model is off unless the caller asks for it.
    """

    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.club = Club.objects.create(name="Cichlid Keepers Club")
        self.other_club = Club.objects.create(name="Some Other Club")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Club website",
            prefix=prefix,
            key_hash=key_hash,
            can_look_up_species=True,
        )
        self.raw_key = raw_key
        # Another club's key, with the permission, so "has the permission" can't be mistaken for
        # "may read this slug".
        other_raw_key, other_prefix, other_key_hash = ClubAPIKey.generate()
        ClubAPIKey.objects.create(
            club=self.other_club,
            name="Someone else's website",
            prefix=other_prefix,
            key_hash=other_key_hash,
            can_look_up_species=True,
        )
        self.other_raw_key = other_raw_key
        self.url = reverse("api_club_species_lookup", kwargs={"slug": self.club.slug})
        self.cichlids = Category.objects.create(name="Cichlids")
        self.yellow_lab = make_species(
            "Labidochromis", "caeruleus", "Yellow lab", ["Electric yellow"], aquarium_use="commercial"
        )
        Species.objects.filter(pk=self.yellow_lab.pk).update(
            family="Cichlidae", order="Cichliformes", category=self.cichlids
        )
        self.yellow_lab.refresh_from_db()
        self.shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", source="aquarium")
        self.blue_dream = Species.objects.create(
            genus="Neocaridina",
            species="davidi",
            variety="Blue Dream",
            common_name="Blue dream shrimp",
            parent=self.shrimp,
            source="aquarium",
        )
        SpeciesCommonName.objects.create(species=self.blue_dream, name="Blue dream shrimp", is_preferred=True)
        # The pair the model tests use: "Bolivian ram" is a real fish that no database rule here can
        # reach, and its genus sibling is the one FishBase simply calls "Ram", so a shortlist exists.
        self.ramirezi = make_species("Mikrogeophagus", "ramirezi", "Ram cichlid", ["Ram"])
        self.altispinosus = make_species("Mikrogeophagus", "altispinosus")

    def get(self, **params):
        return self.client.get(self.url, params, HTTP_X_API_KEY=self.raw_key)

    def test_a_key_is_required(self):
        """Nothing reaches the matcher without one -- not even the free database steps."""
        response = self.client.get(self.url, {"q": "guppy"})
        self.assertEqual(response.status_code, 401)

    def test_a_key_without_the_permission_is_refused(self):
        self.api_key.can_look_up_species = False
        self.api_key.save(update_fields=["can_look_up_species"])
        self.assertEqual(self.get(q="yellow lab").status_code, 403)

    def test_another_clubs_key_cannot_use_this_slug(self):
        response = self.client.get(self.url, {"q": "yellow lab"}, HTTP_X_API_KEY=self.other_raw_key)
        self.assertEqual(response.status_code, 403)

    def test_a_made_up_key_is_refused(self):
        response = self.client.get(self.url, {"q": "yellow lab"}, HTTP_X_API_KEY="ck_deadbeef.nope")
        self.assertEqual(response.status_code, 401)

    def test_get_only(self):
        response = self.client.post(self.url, {"q": "yellow lab"}, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 405)

    def test_a_missing_or_blank_q_is_the_one_error(self):
        for params in ({}, {"q": ""}, {"q": "   "}):
            response = self.get(**params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn("q is required", response.json()["error"])

    def test_a_scientific_name_matches_exactly(self):
        data = self.get(q="Labidochromis caeruleus").json()
        self.assertEqual(data["source"], "exact")
        self.assertTrue(data["unambiguous"])
        self.assertEqual(data["results"][0]["id"], self.yellow_lab.pk)

    def test_a_common_name_matches(self):
        """What the API is for: nobody types a scientific name."""
        data = self.get(q="Yellow lab").json()
        self.assertEqual(data["query"], "Yellow lab")
        self.assertEqual(data["source"], "exact")
        self.assertTrue(data["unambiguous"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["full_scientific_name"], "Labidochromis caeruleus")

    def test_the_whole_record_travels_with_the_match(self):
        """A caller filing this into their own system shouldn't need a second call."""
        result = self.get(q="Yellow lab").json()["results"][0]
        self.assertEqual(result["scientific_name"], "Labidochromis caeruleus")
        self.assertEqual(result["common_name"], "Yellow lab")
        self.assertEqual(result["genus"], "Labidochromis")
        self.assertEqual(result["species_epithet"], "caeruleus")
        self.assertEqual(result["variety"], "")
        self.assertIsNone(result["parent"])
        self.assertEqual(result["family"], "Cichlidae")
        self.assertEqual(result["order"], "Cichliformes")
        self.assertEqual(result["category"], {"id": self.cichlids.pk, "name": "Cichlids"})
        self.assertEqual(result["trade_rank"], Species.TRADE_RANK_SPECIES)
        self.assertEqual(result["source"], "fishbase")
        self.assertEqual(result["label"], "Labidochromis caeruleus (Yellow lab)")

    def test_a_cultivar_comes_back_under_its_full_name(self):
        """scientific_name is the parent species for all thirteen colour strains; the full name isn't."""
        data = self.get(q="blue dream shrimp").json()
        self.assertTrue(data["unambiguous"])
        result = data["results"][0]
        self.assertEqual(result["id"], self.blue_dream.pk)
        self.assertEqual(result["full_scientific_name"], "Neocaridina davidi 'Blue Dream'")
        self.assertEqual(result["scientific_name"], "Neocaridina davidi")
        self.assertEqual(result["variety"], "Blue Dream")
        self.assertEqual(result["parent"], {"id": self.shrimp.pk, "scientific_name": "Neocaridina davidi"})

    def test_no_match_is_an_answer_not_an_error(self):
        response = self.get(q="sponge filter")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["source"], "none")
        self.assertFalse(data["unambiguous"])

    def test_several_candidates_are_not_unambiguous(self):
        """The signal the site itself acts on: fill it in only when there is exactly one answer."""
        make_species("Labidochromis", "gigas")
        data = self.get(q="Labidochromis").json()
        self.assertGreater(data["total_matches"], 1)
        self.assertFalse(data["unambiguous"])

    def test_limit_truncates_without_hiding_how_many_there_were(self):
        make_species("Labidochromis", "gigas")
        make_species("Labidochromis", "vellicans")
        data = self.get(q="Labidochromis", limit=1).json()
        self.assertEqual(data["limit"], 1)
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["results"]), 1)
        self.assertGreaterEqual(data["total_matches"], 3)

    def test_a_silly_limit_is_capped_rather_than_refused(self):
        self.assertEqual(self.get(q="Yellow lab", limit=500).json()["limit"], MAX_GENUS_MATCHES)
        self.assertEqual(self.get(q="Yellow lab").json()["limit"], MAX_SUGGESTIONS)

    def test_a_limit_that_is_not_a_number_is_refused(self):
        for bad in ("0", "-3", "lots"):
            response = self.get(q="Yellow lab", limit=bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("limit", response.json()["error"])

    def test_the_model_is_off_unless_it_is_asked_for(self):
        """The one step that costs money can't be reached by a caller who didn't ask."""
        provider = FakeProvider([{"id": self.altispinosus.pk}])
        llm.set_provider_override(provider)
        try:
            data = self.get(q="Bolivian ram").json()
            self.assertFalse(data["llm"])
            self.assertEqual(provider.call_count, 0)
            self.assertEqual(data["results"], [])
        finally:
            llm.set_provider_override(None)

    def test_asking_for_the_model_reaches_it(self):
        provider = FakeProvider([{"id": self.altispinosus.pk}])
        llm.set_provider_override(provider)
        try:
            data = self.get(q="Bolivian ram", llm="true").json()
            self.assertTrue(data["llm"])
            self.assertEqual(data["source"], "llm")
            self.assertEqual(data["results"][0]["id"], self.altispinosus.pk)
            self.assertEqual(provider.call_count, 1)
        finally:
            llm.set_provider_override(None)

    def test_the_model_answer_is_remembered_so_the_next_caller_is_free(self):
        """One club paying for a lot name is every club's answer -- that is what keeps this cheap."""
        provider = FakeProvider([{"id": self.altispinosus.pk}])
        llm.set_provider_override(provider)
        try:
            self.get(q="Bolivian ram", llm="true")
            data = self.get(q="BOLIVIAN RAM!", llm="true").json()
            self.assertEqual(data["source"], "cache")
            self.assertEqual(provider.call_count, 1)
        finally:
            llm.set_provider_override(None)

    def test_a_key_that_has_spent_its_daily_budget_falls_back_to_the_database(self):
        """Out of budget is not an error: the database steps still answer, the model just doesn't."""
        provider = FakeProvider([{"id": self.altispinosus.pk}])
        llm.set_provider_override(provider)
        original = views.SPECIES_LOOKUP_LLM_CALLS_PER_KEY_PER_DAY
        views.SPECIES_LOOKUP_LLM_CALLS_PER_KEY_PER_DAY = 0
        try:
            data = self.get(q="Bolivian ram", llm="true").json()
            self.assertFalse(data["llm"])
            self.assertEqual(provider.call_count, 0)
            self.assertEqual(self.get(q="Yellow lab", llm="true").json()["results"][0]["id"], self.yellow_lab.pk)
        finally:
            views.SPECIES_LOOKUP_LLM_CALLS_PER_KEY_PER_DAY = original
            llm.set_provider_override(None)

    def test_asking_for_the_model_does_nothing_when_the_site_has_none(self):
        """A deployment with no model configured answers from the database and says llm: false."""
        llm.set_provider_override(UnconfiguredProvider())
        try:
            data = self.get(q="Bolivian ram", llm="true").json()
            self.assertFalse(data["llm"])
            self.assertEqual(data["results"], [])
        finally:
            llm.set_provider_override(None)

    def test_using_the_key_stamps_it(self):
        self.assertIsNone(self.api_key.last_used_at)
        self.get(q="Yellow lab")
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)

    def _log_in_as_a_club_admin(self):
        ClubMember.objects.create(club=self.club, user=self.user, permission_edit_club=True)
        self.client.login(username="my_lot", password="testpassword")

    def test_the_permission_can_be_granted_from_the_key_page(self):
        """A permission nobody can tick is a permission nobody has."""
        self._log_in_as_a_club_admin()
        create_url = reverse("club_api_key_create", kwargs={"slug": self.club.slug})
        self.assertContains(self.client.get(create_url), "can_look_up_species")
        self.client.post(
            create_url, {"name": "Website", "can_look_up_species_present": "1", "can_look_up_species": "on"}
        )
        self.assertTrue(ClubAPIKey.objects.get(club=self.club, name="Website").can_look_up_species)

    def test_the_key_page_documents_the_endpoint_once_it_is_granted(self):
        """Those docs are the only place a club admin finds out this exists."""
        self._log_in_as_a_club_admin()
        url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": self.api_key.pk})
        self.assertContains(self.client.get(url), f"/api/v1/clubs/{self.club.slug}/species-lookup/")
        self.api_key.can_look_up_species = False
        self.api_key.save(update_fields=["can_look_up_species"])
        self.assertNotContains(self.client.get(url), "species-lookup")
