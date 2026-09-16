"""Tests for scientific names on lots: matching, the picker, labels, and genus BAP points."""

import datetime
import re

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
    SpeciesNameRejection,
    SpeciesSearchCache,
)
from auctions.species_matching import (
    MAX_GENUS_MATCHES,
    MAX_NAMES_USING_A_WORD,
    MAX_SINGLE_WORD_MATCHES,
    MAX_SUGGESTIONS,
    base_words,
    exact_matches,
    keywords,
    normalize,
    record_choice,
    remember,
    search_matches,
    singularize,
    strip_quantity,
    suggest_species,
    visible_species,
)
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase, give_contact_info


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


#: A round-one reply that passes the question on to the shortlist round. See
#: species_matching.identify.
CANNOT_NAME_IT = {"kind": "unknown"}


def make_species(genus, epithet, common=None, extra_names=(), source="fishbase", speccode=None, aquarium_use=""):
    """One species plus its common names; ``common`` is the primary name.

    ``aquarium_use="commercial"`` sets the species-level ``trade_rank``; the genus tier needs
    ``Species.recompute_trade_ranks()``.
    """
    species = Species.objects.create(
        genus=genus, species=epithet, common_name=common or "", source=source, aquarium_use=aquarium_use
    )
    if speccode is not None:
        Species.objects.filter(pk=species.pk).update(speccode=speccode)
    # Names carry the species' source, which the matcher reads. See
    # species_matching._single_word_matches.
    if common:
        SpeciesCommonName.objects.create(species=species, name=common, is_preferred=True, source=source)
    for name in extra_names:
        SpeciesCommonName.objects.create(species=species, name=name, source=source)
    return species


@isolated_cache("species-matching")
class SpeciesMatchingTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.guppy = make_species("Poecilia", "reticulata", "Guppy", ["Millionfish"])
        self.other_guppy = make_species("Poecilia", "vivipara", "Eye spot toothcarp", ["Guppy"])
        self.cardinal = make_species("Paracheirodon", "axelrodi", "Cardinal tetra", ["Neon"])
        self.tropheus = make_species("Tropheus", "duboisi", "White spotted cichlid")
        self.ramirezi = make_species("Mikrogeophagus", "ramirezi", "Ram cichlid", ["Ram"])
        self.altispinosus = make_species("Mikrogeophagus", "altispinosus")
        self.sponge = make_species("Tethya", "aurantium", "Ball sponge", source="sealifebase")

    def test_normalize_strips_punctuation_and_case(self):
        self.assertEqual(normalize("  Betta   Splendens (pair)! "), "betta splendens pair")

    def test_normalize_deletes_an_apostrophe_but_splits_on_a_hyphen(self):
        self.assertEqual(normalize("Adolf's catfish"), "adolfs catfish")
        self.assertEqual(normalize("adolfs catfish"), "adolfs catfish")
        self.assertEqual(normalize("Black-banded leporinus"), "black banded leporinus")

    def test_singularize_handles_the_common_endings(self):
        self.assertEqual(singularize("tetras"), "tetra")
        self.assertEqual(singularize("guppies"), "guppy")
        self.assertEqual(singularize("bass"), "bass")

    def test_keywords_drop_the_site_stopword_list(self):
        self.assertNotIn("pair", keywords("young blue guppy pair"))
        self.assertIn("guppy", keywords("young blue guppy pair"))

    def test_exact_common_name_prefers_the_designated_species(self):
        found = list(exact_matches("guppy"))
        self.assertEqual(found[0], self.guppy)
        self.assertIn(self.other_guppy, found)

    def test_exact_match_survives_a_plural(self):
        self.assertEqual(list(exact_matches("guppies"))[0], self.guppy)

    def test_scientific_name_in_a_longer_lot_name(self):
        self.assertEqual(search_matches("Tropheus duboisi maswa F1"), [self.tropheus])

    def test_common_name_phrase_survives_a_plural_and_a_quantity(self):
        self.assertEqual(search_matches("6 young cardinal tetras"), [self.cardinal])

    def test_a_bare_genus_that_matches_one_species_is_offered(self):
        self.assertEqual(search_matches("Tropheus sp. Ikola"), [self.tropheus])

    def test_equipment_matches_nothing(self):
        self.assertEqual(search_matches("sponge filter"), [])

    def test_a_shared_epithet_alone_is_not_a_match(self):
        make_species("Formosania", "davidi")
        make_species("Rhinogobius", "davidi")
        self.assertEqual(search_matches("Neocaridina davidi"), [])

    def test_a_single_word_common_name_does_not_hijack_a_longer_name(self):
        self.assertEqual(search_matches("Bolivian ram"), [])

    def test_weak_genus_match_with_too_many_species_returns_nothing(self):
        for index in range(MAX_GENUS_MATCHES + 4):
            make_species("Ancistrus", f"species{index}")
        self.assertEqual(search_matches("Ancistrus sp. L144"), [])

    def test_a_small_genus_is_offered_in_full(self):
        for index in range(MAX_GENUS_MATCHES - 1):
            make_species("Tropheus", f"species{index}")
        found = search_matches("tropheus")
        self.assertEqual(len(found), MAX_GENUS_MATCHES)
        self.assertIn(self.tropheus, found)

    def test_a_big_genus_falls_back_to_the_ones_in_the_trade(self):
        for index in range(MAX_GENUS_MATCHES + 4):
            make_species("Ancistrus", f"species{index}")
        kept = make_species("Ancistrus", "cirrhosus", "Bristlenose", aquarium_use="commercial")
        self.assertEqual(search_matches("Ancistrus sp. L144"), [kept])

    def test_a_bare_epithet_offers_the_species_that_carry_it(self):
        chindongo = make_species("Chindongo", "saulosi")
        aulonocara = make_species("Aulonocara", "saulosi", "Greenface aulonocara")
        self.assertEqual(search_matches("saulosi"), [aulonocara, chindongo])

    def test_a_bare_epithet_survives_a_quantity(self):
        chindongo = make_species("Chindongo", "saulosi")
        self.assertEqual(search_matches("6 saulosi"), [chindongo])

    def test_a_bare_epithet_ending_in_s_still_counts_as_one_word(self):
        species = make_species("Melanotaenia", "elegans")
        self.assertEqual(search_matches("elegans"), [species])

    def test_an_epithet_shared_by_too_many_species_is_still_nothing(self):
        for index in range(MAX_GENUS_MATCHES + 1):
            make_species(f"Genus{index}", "davidi")
        self.assertEqual(search_matches("davidi"), [])

    def test_an_aquarium_species_outranks_one_nobody_keeps(self):
        # Alphabetically Aulonocara comes first, so only the trade rating can move Zebrasoma ahead.
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
        self.provider.replies = [CANNOT_NAME_IT, {"id": self.altispinosus.pk}]
        matches, source = suggest_species("Bolivian ram")
        self.assertEqual(source, "llm")
        self.assertEqual(matches, [self.altispinosus])

    def test_shortlist_includes_genus_siblings(self):
        """The Bolivian ram is only reachable through its genus sibling called 'Ram'."""
        self.provider.replies = [CANNOT_NAME_IT, {"id": self.altispinosus.pk}]
        suggest_species("Bolivian ram")
        shortlist = self.provider.calls[1]["messages"][0]["content"]
        self.assertIn(str(self.altispinosus.pk), shortlist)

    def test_an_id_that_was_not_offered_is_discarded(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": 99999999}]
        matches, source = suggest_species("Bolivian ram")
        self.assertEqual(matches, [])
        self.assertEqual(source, "none")

    def test_the_model_may_name_a_species_the_shortlist_missed(self):
        """The model may name a species our keyword search failed to shortlist."""
        lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        self.provider.replies = [CANNOT_NAME_IT, {"id": None, "scientific_name": "Labidochromis caeruleus"}]
        matches, source = suggest_species("yellow lab")
        self.assertEqual(matches, [lab])
        self.assertEqual(source, "llm")

    def test_a_species_it_names_that_we_do_not_have_is_no_species(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": None, "scientific_name": "Betta imbellis"}]
        self.assertEqual(suggest_species("peaceful betta")[0], [])

    def test_a_name_it_makes_up_out_of_free_text_is_not_looked_up(self):
        self.provider.replies = [
            CANNOT_NAME_IT,
            {"id": None, "scientific_name": "some kind of small brown fish"},
        ]
        self.assertEqual(suggest_species("brown fish")[0], [])

    def test_null_is_an_answer_and_gets_remembered(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": None}]
        self.assertEqual(suggest_species("Bolivian ram")[0], [])
        cached = SpeciesSearchCache.objects.get(search_text="bolivian ram")
        self.assertIsNone(cached.species)

    def test_the_cache_stops_a_second_call(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": self.altispinosus.pk}]
        suggest_species("Bolivian ram")
        matches, source = suggest_species("BOLIVIAN RAM!")
        self.assertEqual(source, "cache")
        self.assertEqual(matches, [self.altispinosus])
        self.assertEqual(self.provider.call_count, 2)

    def test_a_provider_failure_degrades_to_no_species(self):
        self.provider.replies = []  # raises LLMError
        matches, source = suggest_species("Bolivian ram")
        self.assertEqual(matches, [])
        self.assertEqual(source, "none")

    def test_every_call_is_recorded(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": self.altispinosus.pk}]
        suggest_species("Bolivian ram", user=self.user)
        # A lookup's bill is the sum of its rounds.
        kinds = list(LLMUsage.objects.filter(query="Bolivian ram").values_list("response_kind", flat=True))
        self.assertEqual(sorted(kinds), ["species", "unknown"])
        self.assertEqual(LLMUsage.objects.filter(query="Bolivian ram", user=self.user).count(), 2)

    def test_rate_limit_stops_asking(self):
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


@isolated_cache("species-shortlist")
class SpeciesShortlistTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.caudopunctatus = make_species("Neolamprologus", "caudopunctatus", "Caudopunk")
        self.brichardi = make_species("Neolamprologus", "brichardi", "Fairy cichlid")
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

    def shortlist(self, name):
        from auctions.species_matching import _shortlist, keywords, normalize

        return _shortlist(keywords(name), normalize(name))

    def test_a_word_that_is_an_epithet_shortlists_that_species(self):
        """An abbreviated genus plus a full epithet shortlists the species by its epithet."""
        self.assertIn(self.caudopunctatus, self.shortlist("neolamp caudopunctatus red fin 6"))

    def test_the_epithet_beats_a_genus_too_big_to_fit(self):
        """The epithet layer runs before the genus layer, so an oversized genus can't crowd it out."""
        from auctions.species_matching import LLM_SHORTLIST_SIZE

        for number in range(LLM_SHORTLIST_SIZE + 10):
            make_species("Neolamprologus", f"filler{number}")
        self.assertIn(self.caudopunctatus, self.shortlist("neolamp caudopunctatus red fin 6"))

    def test_a_strain_brings_its_plain_species_with_it(self):
        """A strain we don't stock needs its plain species on the list too."""
        shortlist = self.shortlist("blue dream shrimp")
        self.assertIn(self.blue_dream, shortlist)
        self.assertIn(self.shrimp, shortlist)

    def test_a_short_word_that_happens_to_be_an_epithet_is_a_coincidence(self):
        """ "Geo" is also the epithet of a marine tilefish; a short word must not match it."""
        # No common name contains "geo", so the epithet is the only way in.
        tilefish = make_species("Hoplolatilus", "geo", "Yellow tilefish")
        self.assertNotIn(tilefish, self.shortlist("geo alto sinu"))

    def test_a_typed_epithet_answers_with_the_species_rather_than_its_strains(self):
        """The epithet layer asks for nominal species only, not their strains."""
        for number in range(3):
            Species.objects.create(
                genus="Neocaridina",
                species="davidi",
                variety=f"Strain {number}",
                parent=self.shrimp,
                source="aquarium",
            )
        self.assertIn(self.shrimp, self.shortlist("davidi"))


@isolated_cache("species-identify")
class SpeciesIdentifyFirstTests(StandardTestCase):
    """Round one: the lot name on its own, with no list in front of it."""

    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        llm.set_provider_override(self.provider)
        self.ludwigia = make_species("Ludwigia", "repens", "Water primrose", source="aquarium")
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.other_guppy = make_species("Poecilia", "vivipara", "Eye spot toothcarp", ["Guppy"])
        self.shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", source="aquarium")
        self.blue_dream = Species.objects.create(
            genus="Neocaridina",
            species="davidi",
            variety="Blue Dream",
            parent=self.shrimp,
            source="aquarium",
        )
        SpeciesCommonName.objects.create(species=self.blue_dream, name="blue dream shrimp", is_preferred=True)

    def tearDown(self):
        llm.set_provider_override(None)
        super().tearDown()

    def test_a_misspelling_is_read_through_without_a_shortlist(self):
        """A misspelling no substring match can reach is read through by round one."""
        self.provider.replies = [{"kind": "species", "scientific_name": "Ludwigia repens"}]
        matches, source = suggest_species("red luwigia")
        self.assertEqual(matches, [self.ludwigia])
        self.assertEqual(source, "llm")
        self.assertEqual(self.provider.call_count, 1)

    def test_the_first_round_is_not_shown_our_list(self):
        """Round one is not shown our list; a menu of near misses invites "none of the above"."""
        self.provider.replies = [{"kind": "species", "scientific_name": "Ludwigia repens"}]
        suggest_species("red luwigia")
        self.assertNotIn("Candidates", self.provider.calls[0]["messages"][0]["content"])

    def test_an_answer_it_gives_is_still_looked_up_here(self):
        self.provider.replies = [
            {"kind": "species", "scientific_name": "Yssichromis piceatus"},
            {"id": None},
        ]
        self.assertEqual(suggest_species("yssichromis piceatus pitch black fulu")[0], [])

    def test_a_strain_is_reached_by_the_common_name(self):
        """A cultivar shares its parent's scientific name, so the common name picks the row."""
        self.provider.replies = [
            {"kind": "species", "scientific_name": "Neocaridina davidi", "common_name": "blue dream shrimp"}
        ]
        self.assertEqual(suggest_species("bluedream shrimp 6x")[0], [self.blue_dream])

    def test_a_common_name_several_species_carry_is_not_an_identification(self):
        """A common name several species carry is settled by the binomial."""
        self.provider.replies = [{"kind": "species", "scientific_name": "Poecilia reticulata", "common_name": "guppy"}]
        self.assertEqual(suggest_species("fancy guppys")[0], [self.guppy])

    def test_equipment_is_confirmed_by_the_second_round_before_it_is_written_down(self):
        """A remembered negative is permanent and site-wide, so both rounds must agree on it."""
        self.provider.replies = [{"kind": "not an organism"}, {"id": None}]
        matches, _source = suggest_species("sponge filter")
        self.assertEqual(matches, [])
        self.assertEqual(self.provider.call_count, 2)
        self.assertIsNone(SpeciesSearchCache.objects.get(search_text="sponge filter").species)

    def test_an_unconfirmed_negative_is_never_written_down(self):
        """Round one alone saying "equipment" is never written down."""
        self.provider.replies = [{"kind": "not an organism"}]  # round two raises LLMError
        self.assertEqual(suggest_species("sponge filter")[0], [])
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())

    def test_the_second_round_may_overturn_it(self):
        """Round two, which sees our list, may overturn round one's negative."""
        self.provider.replies = [{"kind": "not an organism"}, {"id": self.guppy.pk}]
        self.assertEqual(suggest_species("some club's word for a guppy")[0], [self.guppy])

    def test_a_name_it_cannot_place_falls_through_to_the_shortlist_round(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": self.guppy.pk}]
        matches, _source = suggest_species("some club's word for a guppy")
        self.assertEqual(matches, [self.guppy])
        self.assertEqual(self.provider.call_count, 2)

    def test_a_shrug_is_not_written_down_at_all(self):
        self.provider.replies = [CANNOT_NAME_IT, {"id": None, "scientific_name": ""}]
        suggest_species("mystery bag")
        row = SpeciesSearchCache.objects.get(search_text="mystery bag")
        self.assertEqual(row.scientific_name, "")
        self.assertFalse(row.is_a_gap)

    def test_a_corrected_spelling_goes_back_through_the_species_list(self):
        """Round one fixes the spelling; the species list does the identifying."""
        self.provider.replies = [{"kind": "unknown", "common_name": "red ludwigia"}]
        matches, source = suggest_species("red luwigia")
        self.assertEqual(matches, [self.ludwigia])
        self.assertEqual(source, "llm")
        self.assertEqual(self.provider.call_count, 1)

    def test_a_corrected_spelling_is_never_written_to_the_shared_cache(self):
        """A corrected spelling is never cached: the key would be the misspelling."""
        self.provider.replies = [{"kind": "unknown", "common_name": "red ludwigia"}]
        suggest_species("red luwigia")
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="red luwigia").exists())

    def test_a_correction_that_matches_nothing_still_falls_through_to_round_two(self):
        self.provider.replies = [
            {"kind": "unknown", "common_name": "some plant"},
            # Named rather than picked by id: "smoe plnt" shortlists nothing.
            {"id": None, "scientific_name": "Poecilia reticulata"},
        ]
        self.assertEqual(suggest_species("smoe plnt")[0], [self.guppy])
        self.assertEqual(self.provider.call_count, 2)

    def test_the_corrected_spelling_builds_round_twos_shortlist(self):
        """Round two's shortlist is built from the corrected spelling."""
        plant = make_species("Micranthemum", "umbrosum", "Mudflower carpet plant", source="aquarium")
        self.provider.replies = [
            {"kind": "unknown", "common_name": "mudflower"},
            {"id": plant.pk},
        ]
        self.assertEqual(suggest_species("mudflwoer")[0], [plant])
        listing = self.provider.calls[1]["messages"][0]["content"]
        self.assertIn("Micranthemum umbrosum", listing)
        # ...and what the seller typed is still the question being asked.
        self.assertIn("mudflwoer", listing)

    def test_the_typed_name_is_still_searched_on(self):
        """The typed name is still searched, so round two sees both candidates."""
        typed_only = make_species("Alternanthera", "reineckii", "Shade loving stem plant", source="aquarium")
        reading_only = make_species("Micranthemum", "umbrosum", "Mudflower carpet plant", source="aquarium")
        self.provider.replies = [
            {"kind": "unknown", "common_name": "mudflower"},
            {"id": None},
        ]
        self.assertEqual(suggest_species("mudflwoer shade")[0], [])
        listing = self.provider.calls[1]["messages"][0]["content"]
        self.assertIn(typed_only.scientific_name, listing)
        self.assertIn(reading_only.scientific_name, listing)

    def test_a_species_it_resolved_is_not_also_recorded_as_a_gap(self):
        """A species resolved by common name is not also recorded as a binomial gap."""
        snail = make_species("Anentome", "helena", "Assassin snail", source="aquarium")
        self.provider.replies = [{"kind": "species", "scientific_name": "Clea helena", "common_name": "assassin snail"}]
        self.assertEqual(suggest_species("8 assasin snails")[0], [snail])
        row = SpeciesSearchCache.objects.get(search_text="8 assasin snails")
        self.assertEqual(row.species, snail)
        self.assertEqual(row.scientific_name, "")
        self.assertFalse(row.is_a_gap)

    def test_round_two_placing_the_lot_clears_round_ones_gap(self):
        """Round two placing the lot clears round one's gap."""
        self.provider.replies = [
            {"kind": "species", "scientific_name": "Clea helena"},
            # Named rather than picked: the lot name shortlists nothing.
            {"id": None, "scientific_name": "Poecilia reticulata"},
        ]
        self.assertEqual(suggest_species("mystery snail bag")[0], [self.guppy])
        row = SpeciesSearchCache.objects.get(search_text="mystery snail bag")
        self.assertEqual(row.species, self.guppy)
        self.assertEqual(row.scientific_name, "")

    def test_round_two_failing_keeps_round_ones_gap(self):
        """Round two failing keeps round one's gap."""
        self.provider.replies = [
            {"kind": "species", "scientific_name": "Clea helena"},
            {"id": None},
        ]
        self.assertEqual(suggest_species("mystery snail bag")[0], [])
        row = SpeciesSearchCache.objects.get(search_text="mystery snail bag")
        self.assertIsNone(row.species)
        self.assertEqual(row.scientific_name, "Clea helena")
        self.assertTrue(row.is_a_gap)

    def test_one_clubs_word_for_a_fish_is_not_handed_to_another_club(self):
        """The model must not hand one club's scoped name for a fish to another club."""
        club = Club.objects.create(name="Someone else's club")
        SpeciesCommonName.objects.create(species=self.guppy, name="clubfish", approved=False, club=club)
        self.provider.replies = [
            {"kind": "species", "scientific_name": "Poecilia reticulata"},
            {"id": None},
        ]
        self.assertEqual(suggest_species("clubfish")[0], [])
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="clubfish").exists())


@isolated_cache("species-gap-rows")
class SpeciesGapRowTests(StandardTestCase):
    """A species we don't stock is a gap in the list, not a "not a species" verdict."""

    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        llm.set_provider_override(self.provider)

    def tearDown(self):
        llm.set_provider_override(None)
        super().tearDown()

    def ask(self):
        self.provider.replies = [
            {"kind": "species", "scientific_name": "Yssichromis piceatus"},
            {"id": None},
        ]
        return suggest_species("yssichromis piceatus pitch black fulu")

    def test_the_name_is_remembered_even_though_the_species_is_not(self):
        self.assertEqual(self.ask()[0], [])
        row = SpeciesSearchCache.objects.get(search_text="yssichromis piceatus pitch black fulu")
        self.assertIsNone(row.species)
        self.assertEqual(row.scientific_name, "Yssichromis piceatus")
        self.assertTrue(row.is_a_gap)

    def test_it_answers_by_itself_once_the_species_is_imported(self):
        self.ask()
        calls_so_far = self.provider.call_count
        fish = make_species("Yssichromis", "piceatus", "Pitch black fulu")
        matches, source = suggest_species("yssichromis piceatus pitch black fulu")
        self.assertEqual(matches, [fish])
        self.assertEqual(source, "cache")
        self.assertEqual(self.provider.call_count, calls_so_far)

    def test_the_healed_row_keeps_the_answer(self):
        self.ask()
        fish = make_species("Yssichromis", "piceatus", "Pitch black fulu")
        suggest_species("yssichromis piceatus pitch black fulu")
        row = SpeciesSearchCache.objects.get(search_text="yssichromis piceatus pitch black fulu")
        self.assertEqual(row.species, fish)
        self.assertFalse(row.is_a_gap)

    def test_a_species_only_one_club_can_see_never_heals_the_shared_row(self):
        """A species only one club can see never heals the shared row."""
        self.ask()
        club = Club.objects.create(name="A club with its own list")
        private = Species.objects.create(
            genus="Yssichromis", species="piceatus", approved=False, club=club, source="admin"
        )
        suggest_species("yssichromis piceatus pitch black fulu")
        row = SpeciesSearchCache.objects.get(search_text="yssichromis piceatus pitch black fulu")
        self.assertIsNone(row.species)
        # ...and the club that added it is still answered with it.
        self.assertEqual(suggest_species("yssichromis piceatus pitch black fulu", club=club)[0], [private])

    def test_a_retired_pairing_is_not_resurrected_by_healing(self):
        self.ask()
        fish = make_species("Yssichromis", "piceatus", "Pitch black fulu")
        SpeciesNameRejection.objects.create(search_text="yssichromis piceatus pitch black fulu", species=fish)
        self.assertEqual(suggest_species("yssichromis piceatus pitch black fulu")[0], [])

    def test_the_gaps_page_says_which_kind_of_no_this_is(self):
        """The gaps page distinguishes "not a species" from "never stocked"."""
        self.ask()
        User.objects.create_superuser("gap_admin", "gap_admin@example.com", "testpassword")
        self.client.login(username="gap_admin", password="testpassword")
        body = self.client.get(reverse("species_gaps")).content.decode()
        self.assertIn("Yssichromis piceatus", body)
        self.assertIn("Identified as", body)


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

    def test_quick_add_lot_has_the_field_but_no_picker(self):
        """Bulk rows show the matched name as text; the field is hidden but still validated."""
        from auctions.forms import quick_add_lot_form_class

        form = quick_add_lot_form_class()(auction=self.online_auction, is_admin=False, tos=self.online_tos)
        self.assertTrue(form.fields["species"].widget.is_hidden)
        self.assertIn("data-species-input", str(form["species"]))

    def test_the_bulk_form_has_no_search_box(self):
        """The bulk form has no species search box."""
        from auctions.forms import quick_add_lot_form_class

        form = quick_add_lot_form_class()(auction=self.online_auction, is_admin=False, tos=self.online_tos)
        self.assertNotIn("data-species-search", str(form["species"]))

    def test_the_admin_lot_form_searches_the_whole_list(self):
        """The admin lot form searches the whole species list."""
        from auctions.forms import EditLot

        form = EditLot(auction=self.online_auction, lot=self.lot, user=self.user)
        html = str(form["species"])
        self.assertIn("data-autocomplete-light-url", html)
        self.assertIn("varieties=1", html)

    def test_the_admin_lot_form_offers_a_way_to_add_a_species(self):
        from crispy_forms.layout import HTML

        from auctions.forms import EditLot

        def html_in(node):
            found = [str(node.html)] if isinstance(node, HTML) else []
            for child in getattr(node, "fields", None) or []:
                found += html_in(child)
            return found

        form = EditLot(auction=self.online_auction, lot=self.lot, user=self.user)
        self.assertIn(reverse("species_create"), " ".join(html_in(form.helper.layout)))

    def test_the_widget_only_renders_the_chosen_option(self):
        from auctions.forms import EditLot

        make_species("Betta", "splendens", "Siamese fighting fish")
        Lot.objects.filter(pk=self.lot.pk).update(species=self.guppy)
        form = EditLot(auction=self.online_auction, lot=Lot.objects.get(pk=self.lot.pk), user=self.user)
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
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="fancy guppy pair").species, self.guppy)

    def test_editing_a_lot_later_does_not_teach_the_cache(self):
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
    """The picker and its attribution survive the crispy layout and reach the page."""

    def setUp(self):
        super().setUp()
        make_species("Poecilia", "reticulata", "Guppy")
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()
        self.client.login(username="my_lot", password="testpassword")
        self.bulk_url = reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.online_auction.slug})

    def test_bulk_add_page_carries_the_species_field_without_a_picker(self):
        response = self.client.get(self.bulk_url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('data-field="species"', body)
        self.assertIn("species-input", body)
        self.assertNotIn("species-select", body)

    def test_the_forms_do_not_carry_the_citation(self):
        body = self.client.get(self.bulk_url).content.decode()
        self.assertNotIn("Froese", body)

    def test_bulk_add_page_leaves_the_picker_out_when_the_auction_says_so(self):
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        body = self.client.get(self.bulk_url).content.decode()
        self.assertNotIn('data-field="species"', body)
        # No picker means no data used, which means nothing to attribute.
        self.assertNotIn("FishBase", body)

    def test_the_lot_page_puts_the_citation_behind_a_question_mark(self):
        guppy = Species.objects.filter(scientific_name="Poecilia reticulata").first()
        Lot.objects.filter(pk=self.lot.pk).update(species=guppy)
        body = self.client.get(self.lot.get_absolute_url(), follow=True).content.decode()
        self.assertIn('id="fishbase-citation"', body)
        self.assertIn('data-bs-toggle="collapse"', body)
        self.assertIn("Froese", body)

    def test_a_lot_with_no_species_has_nothing_to_attribute(self):
        Lot.objects.filter(pk=self.lot.pk).update(species=None)
        body = self.client.get(self.lot.get_absolute_url(), follow=True).content.decode()
        self.assertNotIn("Froese", body)

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
        self.assertEqual(self.lot.scientific_name_line, "Poecilia reticulata")

    def test_nothing_prints_for_a_lot_with_no_species(self):
        self.assertEqual(self.unsoldLot.scientific_name_line, "")
        self.assertEqual(self.unsoldLot.common_name_line, "")

    def test_nothing_prints_when_the_auction_turned_the_field_off(self):
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.scientific_name_line, "")
        self.assertEqual(self.lot.common_name_line, "")

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
        from auctions.forms import ClubBapGenusOverrideForm

        form = ClubBapGenusOverrideForm(data={"genus": "Trophaeus", "points": 20})
        self.assertFalse(form.is_valid())
        self.assertIn("genus", form.errors)

    def test_a_real_genus_is_accepted(self):
        from auctions.forms import ClubBapGenusOverrideForm

        self.assertTrue(ClubBapGenusOverrideForm(data={"genus": "tropheus", "points": 20}).is_valid())

    def test_awarded_points_use_the_genus_rule(self):
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
        species = make_species("Poecilia", "reticulata", "Guppy")
        species.species = "wingei"
        species.save()
        self.assertEqual(species.scientific_name, "Poecilia wingei")

    def test_a_genus_only_species_has_no_trailing_space(self):
        self.assertEqual(make_species("Ancistrus", "").scientific_name, "Ancistrus")

    def test_the_label_is_the_scientific_name_and_nothing_else(self):
        """The label is the scientific name alone."""
        self.assertEqual(make_species("Poecilia", "reticulata", "Guppy").label, "Poecilia reticulata")

    def test_the_model_still_gets_the_common_name(self):
        species = make_species("Poecilia", "reticulata", "Guppy")
        self.assertEqual(species.label_with_common_name, "Poecilia reticulata (Guppy)")

    def test_a_species_with_no_scientific_name_still_has_a_label(self):
        self.assertEqual(make_species("", "", "Mystery snail").label, "Mystery snail")

    def test_speccode_is_only_unique_within_a_source(self):
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
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        give_contact_info(self.user)  # AuctionCreateView refuses somebody with no contact info
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
        SpeciesSearchCache.objects.create(search_text="sponge filter", species=None, source="user")
        matches, source = suggest_species("Sponge filter")
        self.assertEqual(source, "cache")
        self.assertEqual(matches, [])

    def test_the_species_list_outranks_a_bad_cache_row(self):
        """The cache is global and holds guesses, so the species list always outranks it."""
        SpeciesSearchCache.objects.create(search_text="guppy", species=None, source="user")
        matches, source = suggest_species("guppy")
        self.assertEqual(source, "exact")
        self.assertEqual(matches, [self.guppy])

    def test_a_cached_row_does_not_hand_one_clubs_name_to_another(self):
        """A cached row must not hand one club's scoped name to another club."""
        club = Club.objects.create(name="Cichlid Keepers")
        lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        SpeciesCommonName.objects.create(species=lab, name="Yellow lab", club=club, approved=False, source="admin")
        SpeciesSearchCache.objects.create(search_text="yellow lab", species=lab, source="llm")

        matches, source = suggest_species("yellow lab", use_llm=False)
        self.assertEqual(matches, [])
        self.assertEqual(source, "none")

        theirs, _source = suggest_species("yellow lab", use_llm=False, club=Club.objects.create(name="Some Other"))
        self.assertEqual(theirs, [])

    def test_the_club_that_taught_the_name_is_still_answered_with_it(self):
        club = Club.objects.create(name="Cichlid Keepers")
        lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        SpeciesCommonName.objects.create(species=lab, name="Yellow lab", club=club, approved=False, source="admin")
        SpeciesSearchCache.objects.create(search_text="yellow lab", species=lab, source="llm")
        matches, source = suggest_species("yellow lab", use_llm=False, club=club)
        # The name table answers first; the cache is only the fallback.
        self.assertEqual(matches, [lab])
        self.assertEqual(source, "exact")

    def test_approving_the_name_makes_the_cached_row_everybodys_again(self):
        club = Club.objects.create(name="Cichlid Keepers")
        lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        name = SpeciesCommonName.objects.create(
            species=lab, name="Yellow lab", club=club, approved=False, source="admin"
        )
        SpeciesSearchCache.objects.create(search_text="yellow lab", species=lab, source="llm")
        SpeciesCommonName.objects.filter(pk=name.pk).update(approved=True)
        matches, source = suggest_species("yellow lab", use_llm=False)
        self.assertEqual(matches, [lab])
        self.assertEqual(source, "exact")

    def test_a_row_no_name_claims_is_still_served_to_everybody(self):
        shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", source="aquarium")
        SpeciesSearchCache.objects.create(search_text="blue dream shrimp", species=shrimp, source="llm")
        matches, source = suggest_species("blue dream shrimp", use_llm=False)
        self.assertEqual(matches, [shrimp])
        self.assertEqual(source, "cache")


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
        """A variety keeps its parent's scientific name, which BAP rules and family read."""
        self.assertEqual(self.blue_dream.scientific_name, "Neocaridina davidi")
        self.assertEqual(self.blue_dream.genus, "Neocaridina")

    def test_the_strain_shows_in_the_full_name_and_the_label(self):
        self.assertEqual(self.blue_dream.full_scientific_name, "Neocaridina davidi 'Blue Dream'")
        self.assertEqual(self.blue_dream.label, "Neocaridina davidi 'Blue Dream'")
        self.assertEqual(self.blue_dream.label_with_common_name, "Neocaridina davidi 'Blue Dream' (Blue dream shrimp)")

    def test_the_strain_name_finds_the_variety(self):
        self.assertEqual(list(exact_matches("blue dream shrimp")), [self.blue_dream])

    def test_the_species_name_does_not_offer_every_strain(self):
        self.assertEqual(list(exact_matches("Neocaridina davidi")), [self.shrimp])
        self.assertEqual(search_matches("Neocaridina davidi"), [self.shrimp])

    def test_a_lot_prints_and_shows_the_strain(self):
        self.lot.species = self.blue_dream
        self.lot.save()
        self.assertEqual(self.lot.scientific_name, "Neocaridina davidi 'Blue Dream'")
        self.assertEqual(self.lot.scientific_name_line, "Neocaridina davidi 'Blue Dream'")

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
        self.assertTrue(all(row.common_names or row.is_variety for row in rows))

    def test_the_hybrids_in_the_shipped_file_are_named_and_nameless(self):
        """Every shipped hybrid has a common name and no binomial, so it's reachable by name."""
        from auctions import aquarium_species

        hybrids = [row for row in aquarium_species.read_rows() if row.is_hybrid]
        self.assertGreaterEqual(len(hybrids), 10)
        for row in hybrids:
            self.assertEqual(row.scientific_name, "", f"{row.variety} is a cross; it has no binomial")
            self.assertTrue(row.common_names, f"{row.variety} would be unreachable by typing its name")
            self.assertFalse(row.is_variety)
            self.assertFalse(row.is_names_only)
        self.assertIn("Tibee", [row.variety for row in hybrids])
        self.assertIn("Flowerhorn", [row.variety for row in hybrids])

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

    def test_a_row_with_no_scientific_name_is_a_hybrid(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), ",Tibee,tibee|tibee shrimp,Atyidae,Decapoda,invert,fresh\n")
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 1)
        tibee = Species.objects.get(variety="Tibee")
        self.assertTrue(tibee.is_hybrid)
        self.assertEqual(tibee.genus, "")
        self.assertEqual(tibee.species, "")
        self.assertIsNone(tibee.parent)
        # The family is the one piece of taxonomy a cross within one family keeps.
        self.assertEqual(tibee.family, "Atyidae")
        self.assertEqual(tibee.full_scientific_name, "Hybrid 'Tibee'")
        self.assertEqual(list(exact_matches("tibee shrimp")), [tibee])

    def test_a_hybrid_row_adopts_the_one_an_admin_already_added_by_hand(self):
        """An imported hybrid adopts one an admin already added by hand, matched on name and flag."""
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        theirs = Species.objects.create(variety="Flowerhorn", is_hybrid=True, source="admin")
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), ",Flowerhorn,flowerhorn|luohan,Cichlidae,Cichliformes,fish,fresh\n")
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.adopted, 1)
        self.assertEqual(Species.objects.filter(variety="Flowerhorn").count(), 1)
        self.assertEqual(list(exact_matches("luohan")), [theirs])

    def test_reloading_a_hybrid_updates_rather_than_duplicates(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        body = ",Tibee,tibee shrimp,Atyidae,Decapoda,invert,fresh\n"
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), body)
            aquarium_species.load(path)
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(Species.objects.filter(variety="Tibee").count(), 1)

    def test_a_line_with_neither_a_name_nor_a_variety_is_not_a_row(self):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), ",,nothing at all,,,,\n")
            result = aquarium_species.load(path)
        self.assertEqual(result.created, 0)
        self.assertFalse(Species.objects.filter(source="aquarium").exists())

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
        # The primary key never moves, so lots keep their species.
        self.assertEqual(Species.objects.get(scientific_name="Neocaridina davidi").pk, existing.pk)


class CommonNameProvenanceTests(StandardTestCase):
    """Hobby common names survive imports, via ``SpeciesCommonName.source``."""

    def _write(self, tmp_path, body):
        path = tmp_path / "aquarium_species.csv"
        path.write_text("scientific_name,variety,common_names,family,order,kind,habitat\n" + body, encoding="utf-8")
        return path

    def _load(self, body):
        import tempfile
        from pathlib import Path

        from auctions import aquarium_species

        with tempfile.TemporaryDirectory() as folder:
            return aquarium_species.load(self._write(Path(folder), body))

    def test_a_names_only_row_teaches_a_fishbase_species_without_cloning_it(self):
        fishbase = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        result = self._load("Labidochromis caeruleus,,yellow lab|electric yellow,,,,\n")
        self.assertEqual(result.created, 0, "the species already exists; a second copy is the bug")
        self.assertEqual(result.adopted, 1)
        self.assertEqual(Species.objects.filter(scientific_name="Labidochromis caeruleus").count(), 1)
        self.assertEqual(list(exact_matches("yellow lab")), [fishbase])

    def test_it_leaves_the_owning_lists_taxonomy_alone(self):
        fishbase = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        Species.objects.filter(pk=fishbase.pk).update(family="Cichlidae", order="Cichliformes")
        self._load("Labidochromis caeruleus,,yellow lab,Wrongidae,Wrongiformes,plant,salt\n")
        fishbase.refresh_from_db()
        self.assertEqual(fishbase.family, "Cichlidae")
        self.assertEqual(fishbase.source, "fishbase")
        self.assertEqual(fishbase.common_name, "Blue streak hap")

    def test_it_does_not_delete_the_names_the_other_list_owns(self):
        fishbase = make_species("Labidochromis", "caeruleus", "Blue streak hap", ["Yellow prince"])
        self._load("Labidochromis caeruleus,,yellow lab,,,,\n")
        self.assertEqual(
            sorted(fishbase.common_names.values_list("name", flat=True)),
            ["Blue streak hap", "Yellow prince", "yellow lab"],
        )

    def test_dropping_the_row_takes_its_names_with_it(self):
        fishbase = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        self._load("Labidochromis caeruleus,,yellow lab,,,,\n")
        self._load("Neocaridina davidi,,cherry shrimp,Atyidae,Decapoda,invert,fresh\n")
        self.assertEqual(list(fishbase.common_names.values_list("name", flat=True)), ["Blue streak hap"])

    def test_every_name_says_which_list_wrote_it(self):
        make_species("Labidochromis", "caeruleus", "Blue streak hap")
        self._load("Labidochromis caeruleus,,yellow lab,,,,\n")
        self.assertEqual(SpeciesCommonName.objects.get(name="yellow lab").source, "aquarium")
        self.assertEqual(SpeciesCommonName.objects.get(name="Blue streak hap").source, "fishbase")

    def test_a_names_only_row_for_a_species_that_does_not_exist_is_skipped(self):
        result = self._load("Labidochromis caerulues,,yellow lab,,,,\n")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.adopted, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(Species.objects.filter(genus="Labidochromis").exists())

    def test_every_names_only_row_in_the_shipped_file_carries_names(self):
        from auctions import aquarium_species

        for row in aquarium_species.read_rows():
            if row.is_names_only:
                self.assertTrue(row.common_names, f"{row.scientific_name} is a names-only row with no names")


class SpeciesCategoryFromTaxonomyTests(StandardTestCase):
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

    def test_a_cross_is_filed_by_the_family_both_its_parents_are_in(self):
        """A cross is filed by the family both its parents share."""
        from auctions.species_categories import assign_categories

        flowerhorn = Species.objects.create(
            variety="Flowerhorn", is_hybrid=True, source="aquarium", family="Cichlidae", order="Cichliformes"
        )
        assign_categories()
        flowerhorn.refresh_from_db()
        self.assertEqual(flowerhorn.category, self.cichlids)
        # And it is still a cross: nothing put a genus back on it.
        self.assertEqual(flowerhorn.genus, "")

    def test_the_curated_list_says_what_a_plant_is(self):
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
        Species.objects.filter(pk=self.angel.pk).update(category=self.cichlids)
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species_category = self.livebearers
        lot.category_automatically_added = True
        lot.category_checked = True
        lot.species = Species.objects.get(pk=self.angel.pk)
        lot.save()
        self.assertEqual(lot.species_category, self.cichlids)

    def test_a_lot_with_no_species_still_gets_a_guess(self):
        # A full suite run flushes the seed row, so create it for --keepdb re-runs.
        Category.objects.get_or_create(name="Uncategorized")
        self.lot.species = None
        self.lot.species_category = None
        self.lot.category_checked = False
        self.lot.save()
        self.assertIsNotNone(self.lot.species_category)


class SpeciesOnTheSingleLotFormTests(StandardTestCase):
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
        form = self._form()
        self.assertFalse(form.fields["species"].widget.is_hidden)
        self.assertIn("No species", str(form["species"]))

    def test_the_page_renders_the_picker(self):
        # The new-lot view redirects anyone without contact details.
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
        # The manual search box, for names the matcher can't place.
        self.assertIn("data-species-search", body)
        # Both pickers start collapsed; the citation is on the lot page.
        self.assertIn("species-summary", body)
        self.assertNotIn("Froese", body)

    def test_the_page_hides_both_pickers_behind_one_line_of_text(self):
        self.user.first_name = "Test"
        self.user.last_name = "Seller"
        self.user.save()
        self.user.userdata.address = "123 Fish St"
        self.user.userdata.save()
        self.client.login(username="my_lot", password="testpassword")
        body = self.client.get(reverse("new_lot")).content.decode()
        self.assertIn("species-summary", body)
        self.assertIn("species-edit", body)
        self.assertIn("refreshSpeciesUI", body)
        # Hidden by JavaScript, not removed: they still post.
        self.assertIn('id="id_species"', body)
        self.assertIn("div_id_species_category", body)

    def test_the_form_carries_the_flag_saying_the_category_picker_was_open(self):
        self.assertIn("category_shown", self._form().fields)

    def test_a_category_chosen_with_the_picker_open_is_not_overruled(self):
        from auctions.forms import clean_species_for_auction

        livebearers = Category.objects.create(name="Livebearers")
        cichlids = Category.objects.create(name="Cichlids")
        Species.objects.filter(pk=self.guppy.pk).update(category=livebearers)
        cleaned = {"species": Species.objects.get(pk=self.guppy.pk), "species_category": cichlids}
        clean_species_for_auction(cleaned, self.online_auction, derive_category=False)
        self.assertEqual(cleaned["species_category"], cichlids)

    def test_and_the_species_still_wins_while_the_picker_is_closed(self):
        from auctions.forms import clean_species_for_auction

        livebearers = Category.objects.create(name="Livebearers")
        cichlids = Category.objects.create(name="Cichlids")
        Species.objects.filter(pk=self.guppy.pk).update(category=livebearers)
        cleaned = {"species": Species.objects.get(pk=self.guppy.pk), "species_category": cichlids}
        clean_species_for_auction(cleaned, self.online_auction, derive_category=True)
        self.assertEqual(cleaned["species_category"], livebearers)

    def test_renaming_a_lot_re_derives_the_species_rather_than_keeping_the_old_one(self):
        from django.template.loader import get_template

        source = get_template("lot_form.html").template.source
        self.assertNotIn("select.dataset.userChosen) {", source)
        self.assertIn("} else if (choices.length === 1) {", source)

    def test_the_auction_info_endpoint_says_whether_to_show_it(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(reverse("get_auction_info"), {"auction": self.online_auction.pk})
        self.assertTrue(response.json()["use_scientific_name"])
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        response = self.client.post(reverse("get_auction_info"), {"auction": self.online_auction.pk})
        self.assertFalse(response.json()["use_scientific_name"])

    def test_a_species_is_still_dropped_when_the_auction_does_not_use_the_field(self):
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
        """Saving (not just cleaning) keeps an admin's chosen category on a lot with a species."""
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
    def test_saving_a_hand_typed_row_does_not_erase_its_name(self):
        """Saving a hand-typed row with no genus or epithet must not blank its scientific_name."""
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
        species = make_species("Tropheus", "duboisi", aquarium_use="commercial")
        self.assertEqual(Species.objects.get(pk=species.pk).trade_rank, Species.TRADE_RANK_SPECIES)

    def test_a_genus_with_a_kept_species_lifts_its_siblings(self):
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
        self.lot.lot_name = "10 pair"
        self.lot.save()
        self.client.login(username="species_admin", password="testpassword")
        self.assertNotIn("10 pair", self.client.get(self.url).content.decode())

    def test_a_remembered_species_is_listed_too(self):
        """Remembered species, not just remembered negatives, are listed on the gaps page."""
        species = make_species("Caridina", "multidentata", "Amano shrimp")
        SpeciesSearchCache.objects.create(search_text="blue dream shrimp", species=species, source="user")
        self.client.login(username="species_admin", password="testpassword")
        body = self.client.get(self.url).content.decode()
        self.assertIn("Caridina multidentata", body)
        self.assertIn("Look it up again", body)

    def test_who_taught_the_site_an_answer_is_shown(self):
        species = make_species("Caridina", "multidentata", "Amano shrimp")
        SpeciesSearchCache.objects.create(
            search_text="blue dream shrimp", species=species, source="user", created_by=self.user
        )
        self.client.login(username="species_admin", password="testpassword")
        self.assertIn("my_lot", self.client.get(self.url).content.decode())

    def test_species_waiting_for_approval_are_the_work_queue(self):
        pending = make_species("Ancistrus", "sp1", "Rare pleco")
        Species.objects.filter(pk=pending.pk).update(approved=False, added_by=self.admin_user)
        self.client.login(username="species_admin", password="testpassword")
        body = self.client.get(self.url).content.decode()
        self.assertIn("Species waiting for approval", body)
        self.assertIn("Ancistrus sp1", body)
        self.assertIn("admin_user", body)


class SpeciesSearchCacheForgetTests(StandardTestCase):
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

    def test_somebody_who_runs_no_auction_is_turned_away(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_an_auction_admin_may_add_one(self):
        self.client.login(username="admin_user", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 200)

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
        Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=self.shrimp, source="aquarium"
        )
        response = self.client.get(self.url, {"q": "Neocaridina"})
        self.assertNotIn("Blue Dream", response.content.decode())


class AdminLotFormLayoutTests(StandardTestCase):
    """Optional fields hidden with ``HiddenInput`` leave no empty grid columns."""

    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auctionlotadmin", kwargs={"pk": self.lot.pk})

    def _html(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _empty_columns(self, html):
        """Grid columns whose entire contents are one hidden input."""
        squished = re.sub(r"\s+", " ", html)
        return re.findall(r'<div class="col-sm-\d+" > <input type="hidden" name="(\w+)"[^>]*> </div>', squished)

    def test_a_field_the_auction_turned_off_leaves_no_empty_column(self):
        self.online_auction.use_quantity_field = False
        self.online_auction.use_custom_checkbox_field = False
        self.online_auction.save()
        html = self._html()
        self.assertEqual(self._empty_columns(html), [])

    def test_but_its_value_is_still_posted(self):
        self.online_auction.use_quantity_field = False
        self.online_auction.save()
        html = self._html()
        self.assertEqual(html.count('name="quantity"'), 1)
        self.assertIn('<input type="hidden" name="quantity"', html)

    def test_the_species_and_category_start_collapsed(self):
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        species = make_species("Poecilia", "reticulata", "Guppy")
        self.lot.species = species
        self.lot.save()
        html = self._html()
        # The summary line says what the lot claims now...
        self.assertIn("Poecilia reticulata", html)
        # ...the controls are behind the button...
        self.assertIn('data-bs-target="#lot-species-fields"', html)
        self.assertIn('id="lot-species-fields"', html)
        # ...and they are closed: `collapse` without `show`.
        block = re.search(r'id="lot-species-fields"\s*class="([^"]*)"', re.sub(r"\s+", " ", html))
        self.assertIsNotNone(block)
        self.assertIn("collapse", block.group(1))
        self.assertNotIn("show", block.group(1))

    def test_a_hybrid_is_summarised_by_the_name_the_trade_uses(self):
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        self.lot.species = Species.objects.create(variety="Tibee", is_hybrid=True)
        self.lot.save()
        self.assertIn("Hybrid &#x27;Tibee&#x27;", self._html())

    def test_an_auction_with_neither_gets_no_summary_line_at_all(self):
        self.online_auction.use_scientific_name = False
        self.online_auction.use_categories = False
        self.online_auction.save()
        html = self._html()
        self.assertNotIn("lot-species-fields", html)
        self.assertNotIn("Scientific name", html)
        # Still posted, still validated by clean_species_for_auction.
        self.assertIn('<input type="hidden" name="species"', html)


@isolated_cache("species-lot-admin")
class AdminLotFormSavesTheSpeciesTests(StandardTestCase):
    """``LotAdmin.post`` saves the species picked in the admin lot editor."""

    def setUp(self):
        super().setUp()
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy", aquarium_use="commercial")
        self.lot.lot_name = "Blue moscow"
        self.lot.save()
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auctionlotadmin", kwargs={"pk": self.lot.pk})

    def _data(self, **overrides):
        data = {
            "lot_name": self.lot.lot_name,
            "auction": self.online_auction.pk,
            "species": self.guppy.pk,
            "species_category": "",
            "summernote_description": "",
            "quantity": 1,
            "donation": "",
            "i_bred_this_fish": "",
            "buy_now_price": "",
            "reserve_price": 5,
            "banned": "",
            "auctiontos_winner": "",
            "winning_price": "",
            "custom_checkbox": "",
            "custom_field_1": "",
            "custom_dropdown": "",
        }
        data.update(overrides)
        return data

    def _post(self, **overrides):
        return self.client.post(self.url, self._data(**overrides))

    def test_the_species_is_actually_saved(self):
        self._post()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.guppy)

    def test_the_pairing_is_taught_to_the_rest_of_the_site(self):
        self._post()
        row = SpeciesSearchCache.objects.get(search_text="blue moscow")
        self.assertEqual(row.species, self.guppy)
        self.assertEqual(row.source, "user")
        self.assertEqual(row.created_by, self.admin_user)

    def test_saving_without_touching_the_species_teaches_nothing(self):
        self.lot.species = self.guppy
        self.lot.save()
        self._post()
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="blue moscow").exists())

    def test_clearing_the_species_teaches_nothing(self):
        self.lot.species = self.guppy
        self.lot.save()
        self._post(species="")
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="blue moscow").exists())

    def test_an_auction_with_the_field_switched_off_keeps_what_is_stored(self):
        """With the setting off, editing a lot keeps its stored species."""
        self.lot.species = self.guppy
        self.lot.save()
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        self._post(species="")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.guppy)


@isolated_cache("species-pending")
class PendingSpeciesTests(StandardTestCase):
    """A species an auction admin added is visible only to them until approved.

    Scoped to the user, not their club: many auctions have no club.
    """

    def setUp(self):
        super().setUp()
        self.mine = make_species("Ancistrus", "sp1", "Rare pleco", aquarium_use="commercial")
        Species.objects.filter(pk=self.mine.pk).update(approved=False, added_by=self.admin_user)
        self.mine.refresh_from_db()
        self.shared = make_species("Poecilia", "reticulata", "Guppy", aquarium_use="commercial")

    def _species_form_data(self, **overrides):
        data = {
            "scientific_name_input": "Corydoras habrosus",
            "common_name": "Salt and pepper cory",
            "other_names": "",
            "variety": "",
            "parent": "",
            "category": "",
            "freshwater": "on",
            "breeder_points": "on",
            "lot_name": "",
            "attach_to_lots": "",
        }
        data.update(overrides)
        return data

    def test_the_author_is_offered_their_own_species(self):
        self.assertIn(self.mine, visible_species(self.admin_user))
        self.assertEqual(list(exact_matches("rare pleco", user=self.admin_user)), [self.mine])

    def test_nobody_else_is(self):
        self.assertNotIn(self.mine, visible_species(self.user))
        self.assertEqual(list(exact_matches("rare pleco", user=self.user)), [])
        self.assertEqual(list(exact_matches("rare pleco")), [])

    def test_an_approved_species_is_everybodys(self):
        self.assertIn(self.shared, visible_species(self.user))
        self.assertIn(self.shared, visible_species(None))

    def test_the_search_box_cannot_route_around_it(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertNotIn(
            "Ancistrus sp1", self.client.get(reverse("species-autocomplete"), {"q": "pleco"}).content.decode()
        )
        self.client.login(username="admin_user", password="testpassword")
        # Found by common name; the picker shows the scientific name (Species.label).
        self.assertIn(
            "Ancistrus sp1", self.client.get(reverse("species-autocomplete"), {"q": "pleco"}).content.decode()
        )

    def test_an_unapproved_species_is_never_remembered(self):
        remember("rare pleco", self.mine, source="user", user=self.admin_user)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="rare pleco").exists())
        remember("guppy pair", self.shared, source="user", user=self.admin_user)
        row = SpeciesSearchCache.objects.get(search_text="guppy pair")
        self.assertEqual(row.species, self.shared)
        self.assertEqual(row.created_by, self.admin_user, "a wrong global answer has to be traceable")

    def test_a_species_added_on_the_site_carries_its_names_scope(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_create"), self._species_form_data())
        species = Species.objects.get(scientific_name="Corydoras habrosus")
        for name in species.common_names.all():
            self.assertFalse(name.approved)
            self.assertEqual(name.added_by, self.admin_user)

    def test_approving_a_species_approves_the_names_it_came_with(self):
        SpeciesCommonName.objects.filter(species=self.mine).update(approved=False, added_by=self.admin_user)
        name = SpeciesCommonName.objects.get(species=self.mine)
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        self.client.post(reverse("species_approve", kwargs={"pk": self.mine.pk}))
        name.refresh_from_db()
        self.assertTrue(name.approved)
        self.assertEqual(list(exact_matches("rare pleco", user=self.user)), [self.mine])

    def test_approving_it_makes_it_everybodys_and_teaches_the_name(self):
        self.lot.lot_name = "Rare pleco"
        self.lot.species = self.mine
        self.lot.save()
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        response = self.client.post(reverse("species_approve", kwargs={"pk": self.mine.pk}))
        self.assertEqual(response.status_code, 302)
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.approved)
        self.assertIn(self.mine, visible_species(self.user))
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="rare pleco").species, self.mine)

    def test_only_a_superuser_may_approve(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_approve", kwargs={"pk": self.mine.pk}))
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.approved)

    def test_one_added_by_an_auction_admin_starts_unapproved(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(
            reverse("species_create"),
            self._species_form_data(),
        )
        species = Species.objects.get(scientific_name="Corydoras habrosus")
        self.assertFalse(species.approved)
        self.assertEqual(species.added_by, self.admin_user)

    def test_one_added_by_a_superuser_is_approved_on_the_spot(self):
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        self.client.post(
            reverse("species_create"),
            self._species_form_data(),
        )
        self.assertTrue(Species.objects.get(scientific_name="Corydoras habrosus").approved)

    def test_a_club_mate_sees_it_too(self):
        club = Club.objects.create(name="Species Club")
        ClubMember.objects.create(club=club, user=self.admin_user)
        ClubMember.objects.create(club=club, user=self.user)
        Species.objects.filter(pk=self.mine.pk).update(club=club)
        self.assertIn(self.mine, visible_species(self.user), "a club mate can see it")
        self.assertIn(self.mine, visible_species(None, club), "so can a caller working for the club")
        self.assertNotIn(self.mine, visible_species(self.user_with_no_lots), "somebody else's club cannot")

    def test_a_species_with_no_club_is_still_visible_to_its_author(self):
        self.assertIsNone(self.mine.club)
        self.assertIn(self.mine, visible_species(self.admin_user))

    def test_no_club_in_the_lookup_does_not_open_the_gates(self):
        self.assertNotIn(self.mine, visible_species(None, None))
        self.assertNotIn(self.mine, visible_species(self.user, None))

    def test_the_club_is_filled_in_when_there_is_an_obvious_one(self):
        club = Club.objects.create(name="Species Club")
        ClubMember.objects.create(club=club, user=self.admin_user)
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_create"), self._species_form_data())
        self.assertEqual(Species.objects.get(scientific_name="Corydoras habrosus").club, club)

    def test_it_is_left_blank_when_there_is_not(self):
        for name in ("Club One", "Club Two"):
            ClubMember.objects.create(club=Club.objects.create(name=name), user=self.admin_user)
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_create"), self._species_form_data())
        self.assertIsNone(Species.objects.get(scientific_name="Corydoras habrosus").club)

    def test_an_auction_admin_only_attaches_it_to_their_own_auctions_lots(self):
        other_user = User.objects.create_user(username="other_club", password="testpassword")
        other_auction = Auction.objects.create(
            created_by=other_user,
            title="Somebody elses auction",
            use_scientific_name=True,
            date_start=timezone.now(),
        )
        other_lot = Lot.objects.create(lot_name="Mystery fish", auction=other_auction, quantity=1)
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        mine = Lot.objects.create(lot_name="Mystery fish", auction=self.online_auction, quantity=1)
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(
            reverse("species_create") + "?lot_name=Mystery+fish",
            {
                "scientific_name_input": "Corydoras habrosus",
                "common_name": "Mystery fish",
                "other_names": "",
                "variety": "",
                "parent": "",
                "category": "",
                "freshwater": "on",
                "breeder_points": "on",
                "lot_name": "Mystery fish",
                "attach_to_lots": "on",
            },
        )
        mine.refresh_from_db()
        other_lot.refresh_from_db()
        self.assertIsNotNone(mine.species)
        self.assertIsNone(other_lot.species, "another club's lots are not this admin's to change")


class CategoryChosenByAPersonSticksTests(StandardTestCase):
    """A hand-picked category clears ``category_automatically_added`` so the next save keeps it."""

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
        from auctions.forms import note_category_chosen_by_person

        lot = Lot.objects.get(pk=self.lot.pk)
        note_category_chosen_by_person(lot, {"species_category": self.plants})
        lot.species_category = self.plants
        lot.save()
        lot.refresh_from_db()
        self.assertEqual(lot.species_category, self.plants)

    def test_re_saving_without_touching_the_category_is_not_a_decision(self):
        from auctions.forms import note_category_chosen_by_person

        lot = Lot.objects.get(pk=self.lot.pk)
        note_category_chosen_by_person(lot, {"species_category": self.cichlids})
        self.assertTrue(lot.category_automatically_added)

    def test_a_machine_set_category_still_follows_the_species(self):
        livebearers = Category.objects.create(name="Livebearers")
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species_category = livebearers
        lot.category_automatically_added = True
        lot.save()
        self.assertEqual(lot.species_category, self.cichlids)


class TurningTheFieldOffKeepsTheSpeciesTests(StandardTestCase):
    """Turning ``use_scientific_name`` off hides the field without deleting stored species."""

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
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.species = self.guppy
        lot.save()
        self.assertEqual(lot.scientific_name, "Poecilia reticulata")
        self.online_auction.use_scientific_name = False
        self.online_auction.save()
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.scientific_name, "")
        self.assertEqual(lot.scientific_name_line, "")
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.scientific_name, "Poecilia reticulata")


class BapCategoryNamesResolveTests(StandardTestCase):
    """The category names the BAP rules match on by string must be reachable."""

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
        from auctions.species_categories import assign_categories

        shrimp = Species.objects.create(genus="Neocaridina", species="davidi", source="aquarium")
        assign_categories()
        shrimp.refresh_from_db()
        self.assertEqual(shrimp.category, self.inverts)


class SpeciesBreederPointsFlagTests(StandardTestCase):
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
        Species.objects.filter(pk=self.species.pk).update(breeder_points=False)
        lot = Lot.objects.get(pk=self.lot.pk)
        lot.auto_award_bap_points()
        self.assertFalse(BapAward.objects.filter(lot=lot).exists())
        self.assertEqual(lot.bap_auto_reason, "species_not_eligible")

    def test_a_strain_answers_for_its_parent(self):
        Species.objects.filter(pk=self.species.pk).update(breeder_points=False)
        Species.objects.create(
            genus="Tropheus", species="duboisi", variety="Maswa", parent=self.species, source="admin"
        )
        strain = Species.objects.get(variety="Maswa")
        self.assertFalse(strain.earns_breeder_points)


@isolated_cache("species-quantity")
class QuantityInLotNamesTests(StandardTestCase):
    """A leading count ("6 guppies") doesn't stop a name matching."""

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
        self.assertEqual(strip_quantity("blue dream shrimp"), "blue dream shrimp")
        self.assertEqual(strip_quantity("mickey mouse platy"), "mickey mouse platy")

    def test_hardware_still_answers_nothing(self):
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
    """Freshwater species outrank marine ones sharing a common name."""

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


@isolated_cache("species-single-word")
class SingleWordCommonNameTests(StandardTestCase):
    """One word of a longer lot name can name a species, within three bounds read off the data."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy", aquarium_use="commercial")
        self.ramirezi = make_species("Mikrogeophagus", "ramirezi", "Ram cichlid", ["Ram"], aquarium_use="commercial")
        self.altispinosus = make_species(
            "Mikrogeophagus", "altispinosus", extra_names=["Bolivian ram"], aquarium_use="commercial"
        )

    def test_a_common_name_inside_a_longer_lot_name(self):
        found, source = suggest_species("male guppy", use_llm=False)
        self.assertEqual(list(found), [self.guppy])
        self.assertEqual(source, "search")

    def test_a_phrase_still_beats_a_single_word(self):
        found, _ = suggest_species("wild caught bolivian ram", use_llm=False)
        self.assertEqual(list(found), [self.altispinosus])

    def test_an_ambiguous_word_answers_nothing(self):
        for index in range(MAX_SINGLE_WORD_MATCHES + 1):
            make_species(f"Genus{index}", f"sp{index}", "Tetra", aquarium_use="commercial")
        self.assertEqual(list(suggest_species("assorted tetras", use_llm=False)[0]), [])

    def test_a_word_naming_a_fish_nobody_keeps_answers_nothing(self):
        make_species("Carcharhinus", "brachyurus", "Copper shark", ["Bronze"])
        self.assertEqual(list(suggest_species("bronze cory", use_llm=False)[0]), [])

    def test_a_word_naming_a_kind_of_fish_answers_nothing(self):
        make_species("Pethia", "ticto", "Ticto barb", ["Barb"], aquarium_use="commercial")
        for index in range(MAX_NAMES_USING_A_WORD + 1):
            make_species(f"Barbus{index}", f"sp{index}", f"Kind{index} barb")
        self.assertEqual(list(suggest_species("odessa barb", use_llm=False)[0]), [])

    def test_our_own_vocabulary_does_not_need_the_trade_flag(self):
        betta = make_species("Betta", "splendens", "Siamese fighting fish")
        SpeciesCommonName.objects.create(species=betta, name="betta", source="aquarium")
        # Enough siblings that the genus alone is too broad; see search_matches' fall-through.
        for index in range(MAX_GENUS_MATCHES + 1):
            make_species("Betta", f"sp{index}", aquarium_use="commercial")
        found, source = suggest_species("male bettas", use_llm=False)
        self.assertEqual(list(found), [betta])
        self.assertEqual(source, "search")

    def test_equipment_is_still_not_a_species(self):
        make_species("Ostracion", "meleagris", "Spotted boxfish", ["Box"], aquarium_use="commercial")
        for name in ("breeder box", "box of misc", "sponge filter", "bag of gravel"):
            with self.subTest(name=name):
                self.assertEqual(list(suggest_species(name, use_llm=False)[0]), [], name)

    def test_the_most_specific_word_wins(self):
        make_species("Xiphophorus", "maculatus", "Southern platyfish", ["Platy"], aquarium_use="commercial")
        for index in range(MAX_SINGLE_WORD_MATCHES):
            make_species(f"Other{index}", f"sp{index}", f"Fish{index}", ["Sunburst"], aquarium_use="commercial")
        found, _ = suggest_species("sunburst platy", use_llm=False)
        self.assertEqual([species.scientific_name for species in found], ["Xiphophorus maculatus"])


@isolated_cache("species-mixed-bags")
class MixedBagsAreNotOneSpeciesTests(StandardTestCase):
    """ "Assorted" and "mixed" are not quantity words: the lot is not one species."""

    def test_a_mixed_bag_of_one_species_still_resolves(self):
        guppy = make_species("Poecilia", "reticulata", "Guppy", aquarium_use="commercial")
        for name in ("assorted guppies", "6 assorted guppies", "mixed guppies"):
            with self.subTest(name=name):
                self.assertEqual(list(suggest_species(name, use_llm=False)[0]), [guppy], name)

    def test_a_mixed_bag_of_a_whole_group_resolves_to_nothing(self):
        for index in range(MAX_SUGGESTIONS + 3):
            make_species(f"Genus{index}", f"sp{index}", "Tetra", aquarium_use="commercial")
        for name in ("assorted tetras", "mixed tetras", "assorted plants"):
            with self.subTest(name=name):
                self.assertEqual(list(suggest_species(name, use_llm=False)[0]), [], name)

    def test_they_are_not_stripped_from_the_name(self):
        self.assertEqual(strip_quantity("assorted tetras"), "assorted tetras")
        self.assertEqual(strip_quantity("6 assorted tetras"), "assorted tetras")


@isolated_cache("species-hobby-codes")
class HobbyCodeNamesTests(StandardTestCase):
    """L-numbers and CW numbers survive tokenising."""

    def test_a_code_survives_tokenising(self):
        self.assertIn("l046", base_words("L046 pleco"))
        self.assertIn("cw11", base_words("CW11 corydoras"))

    def test_a_bare_count_is_still_not_a_word(self):
        self.assertEqual(base_words("6 guppies"), ["guppies"])
        self.assertEqual(base_words("10 gallon tank"), ["gallon", "tank"])

    def test_a_code_in_the_curated_list_finds_its_species(self):
        species = make_species("Hypancistrus", "zebra", "Zebra pleco", aquarium_use="commercial")
        SpeciesCommonName.objects.create(species=species, name="l046", source="aquarium")
        for name in ("L046", "l046 pleco", "3 L046"):
            with self.subTest(name=name):
                self.assertEqual(list(suggest_species(name, use_llm=False)[0]), [species], name)


class NormalizedCommonNameTests(StandardTestCase):
    def test_a_name_with_an_apostrophe_is_reachable(self):
        species = make_species("Corydoras", "adolfoi", "Adolf's catfish")
        self.assertEqual(list(exact_matches("adolfs catfish")), [species])
        self.assertEqual(list(exact_matches("Adolf's catfish")), [species])

    def test_the_species_own_common_name_is_normalized_too(self):
        species = Species.objects.create(genus="Corydoras", species="agassizii", common_name="Agassiz's corydoras")
        self.assertEqual(species.common_name_normalized, "agassizs corydoras")
        self.assertEqual(list(exact_matches("Agassiz's corydoras")), [species])

    def test_a_hyphenated_name_with_a_count_is_reached_exactly(self):
        """A count plus a hyphenated name is matched exactly after strip_quantity() and normalize()."""
        species = make_species("Leporinus", "fasciatus", "Black-banded leporinus")
        found, source = suggest_species("3 black-banded leporinus", use_llm=False)
        self.assertEqual(list(found), [species])
        self.assertEqual(source, "exact")

    def test_a_hyphenated_name_is_reachable_as_a_phrase(self):
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


class SpeciesCategoryOnASplitCategoryListTests(StandardTestCase):
    """Category mapping against this site's real, finely split category list."""

    REAL_CATEGORIES = (
        "Cichlids - Rift Lake",
        "Cichlids - Old World",
        "Cichlids - Central American",
        "Cichlids - South American",
        "Corydoras",
        "Plecostomus",
        "Other Catfish",
        "Characins - Tetras, Pencilfish, Hatchetfish",
        "Cyprinids - Barbs, Danios, Rasboras",
        "Bettas and labyrinth fish",
        "Misc and oddball fish",
        "Saltwater fish",
        "Shrimp",
        "Snails and other inverts",
        "Aquatic plants",
        "Live food cultures",
        "Koi",
        "Goldfish",
        "Uncategorized",
    )

    def setUp(self):
        super().setUp()
        from auctions.species_categories import CategoryResolver

        Category.objects.exclude(name__in=self.REAL_CATEGORIES).delete()
        for name in self.REAL_CATEGORIES:
            Category.objects.get_or_create(name=name)
        self.resolver = CategoryResolver()

    def _species(self, genus, epithet, family="", order="", **fields):
        species = make_species(genus, epithet, f"{genus} {epithet}")
        Species.objects.filter(pk=species.pk).update(family=family, order=order, **fields)
        return Species.objects.get(pk=species.pk)

    def _category_for(self, species):
        from auctions.species_categories import hint_for

        return self.resolver.resolve(hint_for(species))

    def test_a_dash_in_the_category_name_is_not_a_different_category(self):
        self.assertEqual(self.resolver.resolve("characins").name, "Characins - Tetras, Pencilfish, Hatchetfish")

    def test_the_words_can_be_in_the_other_order(self):
        Category.objects.filter(name="Cichlids - Rift Lake").update(name="Rift Lake Cichlids")
        from auctions.species_categories import CategoryResolver

        self.assertEqual(CategoryResolver().resolve("cichlids rift").name, "Rift Lake Cichlids")

    def test_a_malawi_cichlid_lands_in_the_rift_lake_category(self):
        fish = self._species("Labidochromis", "caeruleus", family="Cichlidae", order="Cichliformes")
        self.assertEqual(self._category_for(fish).name, "Cichlids - Rift Lake")

    def test_a_south_american_cichlid_does_not(self):
        fish = self._species("Apistogramma", "cacatuoides", family="Cichlidae", order="Cichliformes")
        self.assertEqual(self._category_for(fish).name, "Cichlids - South American")

    def test_a_central_american_cichlid_does_not_either(self):
        fish = self._species("Thorichthys", "meeki", family="Cichlidae", order="Cichliformes")
        self.assertEqual(self._category_for(fish).name, "Cichlids - Central American")

    def test_a_krib_is_an_old_world_cichlid(self):
        fish = self._species("Pelvicachromis", "pulcher", family="Cichlidae", order="Cichliformes")
        self.assertEqual(self._category_for(fish).name, "Cichlids - Old World")

    def test_a_cichlid_genus_nobody_sells_gets_no_category_rather_than_a_guess(self):
        fish = self._species("Konia", "eisentrauti", family="Cichlidae", order="Cichliformes")
        Species.objects.filter(pk=fish.pk).update(genus="Notarealgenus")
        self.assertIsNone(self._category_for(Species.objects.get(pk=fish.pk)))

    def test_a_pleco_is_not_a_corydoras(self):
        fish = self._species("Ancistrus", "cirrhosus", family="Loricariidae", order="Siluriformes")
        self.assertEqual(self._category_for(fish).name, "Plecostomus")

    def test_a_synodontis_is_not_a_corydoras_either(self):
        fish = self._species("Synodontis", "petricola", family="Mochokidae", order="Siluriformes")
        self.assertEqual(self._category_for(fish).name, "Other Catfish")

    def test_a_corydoras_is_a_corydoras(self):
        fish = self._species("Corydoras", "aeneus", family="Callichthyidae", order="Siluriformes")
        self.assertEqual(self._category_for(fish).name, "Corydoras")

    def test_a_cory_falls_back_to_the_catfish_category_when_there_is_no_cory_one(self):
        Category.objects.filter(name="Corydoras").delete()
        from auctions.species_categories import CategoryResolver

        fish = self._species("Corydoras", "aeneus", family="Callichthyidae", order="Siluriformes")
        from auctions.species_categories import hint_for

        self.assertEqual(CategoryResolver().resolve(hint_for(fish)).name, "Other Catfish")

    def test_a_betta_lands_with_the_labyrinth_fish(self):
        fish = self._species("Betta", "splendens", family="Osphronemidae", order="Anabantiformes")
        self.assertEqual(self._category_for(fish).name, "Bettas and labyrinth fish")

    def test_a_snakehead_does_not(self):
        fish = self._species("Channa", "andrao", family="Channidae", order="Anabantiformes")
        self.assertEqual(self._category_for(fish).name, "Misc and oddball fish")

    def test_koi_and_goldfish_are_told_apart(self):
        koi = self._species("Cyprinus", "carpio", family="Cyprinidae", order="Cypriniformes")
        goldfish = self._species("Carassius", "auratus", family="Cyprinidae", order="Cypriniformes")
        self.assertEqual(self._category_for(koi).name, "Koi")
        self.assertEqual(self._category_for(goldfish).name, "Goldfish")

    def test_a_marine_only_fish_lands_in_saltwater(self):
        fish = self._species("Amphiprion", "ocellaris", family="Pomacentridae", order="Ovalentaria/misc")
        Species.objects.filter(pk=fish.pk).update(saltwater=True, freshwater=False)
        self.assertEqual(self._category_for(Species.objects.get(pk=fish.pk)).name, "Saltwater fish")

    def test_a_cherry_shrimp_lands_in_shrimp_rather_than_with_the_snails(self):
        from auctions.species_categories import assign_categories

        shrimp = Species.objects.create(genus="Neocaridina", species="davidi", source="aquarium")
        assign_categories()
        shrimp.refresh_from_db()
        self.assertEqual(shrimp.category.name, "Shrimp")

    def test_and_falls_back_to_the_inverts_when_there_is_no_shrimp_category(self):
        from auctions.species_categories import assign_categories

        Category.objects.filter(name="Shrimp").delete()
        shrimp = Species.objects.create(genus="Neocaridina", species="davidi", source="aquarium")
        assign_categories()
        shrimp.refresh_from_db()
        self.assertEqual(shrimp.category.name, "Snails and other inverts")


class BackfillLotSpeciesCommandTests(StandardTestCase):
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


class BackfillReviewPassTests(StandardTestCase):
    """The backfill command's review pass, run end to end."""

    def setUp(self):
        super().setUp()
        self.chindongo = make_species("Chindongo", "saulosi", "Saulosi cichlid")
        self.aulonocara = make_species("Aulonocara", "saulosi", "Sunshine peacock")
        self.lot.lot_name = "6 saulosi"
        self.lot.species = None
        self.lot.save()
        self.second = Lot.objects.create(
            lot_name="saulosi pair",
            auction=self.online_auction,
            user=self.user,
            auctiontos_seller=self.online_tos,
            quantity=1,
            reserve_price=5,
            date_end=self.lot.date_end,
        )
        Lot.objects.filter(pk=self.second.pk).update(species=None)

    def _run(self, *args, answers=None):
        from io import StringIO

        from django.core.management import call_command

        from auctions.management.commands.backfill_lot_species import Command

        out = StringIO()
        replies = list(answers or [])
        command = Command()
        command._ask = lambda prompt: replies.pop(0) if replies else "q"  # noqa: SLF001 - the seam is the point
        call_command(command, *args, stdout=out)
        return out.getvalue()

    def test_group_key_makes_one_question_out_of_every_spelling(self):
        from auctions.management.commands.backfill_lot_species import group_key

        self.assertEqual(group_key("6 male guppies"), "guppy")
        self.assertEqual(group_key("Guppies (pair)"), "guppy")
        self.assertEqual(group_key("3 bags"), "")

    def test_a_dry_run_lists_the_questions_and_asks_none(self):
        output = self._run("--review", "--dry-run", "--min-lots", "1")
        self.assertIn("saulosi", output)
        self.assertIn("Chindongo saulosi", output)
        self.assertIn("Aulonocara saulosi", output)
        self.assertIn("Dry run", output)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)

    def test_one_decision_covers_every_spelling_of_the_name(self):
        self._run("--review", "--min-lots", "1", answers=["1", "y", "q"])
        self.lot.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.lot.species, self.aulonocara)
        self.assertEqual(self.second.species, self.aulonocara)

    def test_a_decision_teaches_the_matcher(self):
        self._run("--review", "--min-lots", "1", answers=["1", "y", "q"])
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="6 saulosi").species, self.aulonocara)

    def test_answering_no_covers_only_that_spelling(self):
        self._run("--review", "--min-lots", "1", answers=["1", "n", "q"])
        self.lot.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.lot.species, self.aulonocara)
        self.assertIsNone(self.second.species)

    def test_not_a_species_is_remembered_so_it_stops_coming_back(self):
        self._run("--review", "--min-lots", "1", answers=["n", "q"])
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)
        cached = SpeciesSearchCache.objects.get(search_text="6 saulosi")
        self.assertIsNone(cached.species)

    def test_skipping_writes_nothing(self):
        self._run("--review", "--min-lots", "1", answers=["", "q"])
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="6 saulosi").exists())

    def test_a_name_the_matcher_can_place_is_not_asked_about(self):
        Lot.objects.filter(pk=self.second.pk).update(lot_name="Chindongo saulosi")
        output = self._run("--review", "--dry-run", "--min-lots", "1")
        self.assertNotIn("'chindongo saulosi'", output)

    def test_names_that_match_nothing_are_only_offered_when_asked_for(self):
        Lot.objects.filter(pk=self.lot.pk).update(lot_name="Sponge filter")
        Lot.objects.filter(pk=self.second.pk).update(lot_name="Sponge filters")
        self.assertNotIn("Sponge filter", self._run("--review", "--dry-run", "--min-lots", "1"))
        self.assertIn("Sponge filter", self._run("--review", "--dry-run", "--min-lots", "1", "--include-unmatched"))

    def test_a_species_can_be_added_without_leaving_the_review(self):
        Lot.objects.filter(pk=self.lot.pk).update(lot_name="Blue dream shrimp")
        Lot.objects.filter(pk=self.second.pk).update(lot_name="Blue dream shrimps")
        self._run(
            "--review",
            "--min-lots",
            "1",
            "--include-unmatched",
            answers=["a", "Neocaridina davidi", "", "Blue dream shrimp", "y", "q"],
        )
        added = Species.objects.get(scientific_name="Neocaridina davidi")
        self.assertEqual(added.source, "admin")
        self.assertTrue(added.approved)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, added)

    def test_a_cross_is_added_by_leaving_the_scientific_name_blank(self):
        Lot.objects.filter(pk=self.lot.pk).update(lot_name="Tibee shrimp")
        Lot.objects.filter(pk=self.second.pk).update(lot_name="Tibee shrimps")
        self._run(
            "--review",
            "--min-lots",
            "1",
            "--include-unmatched",
            answers=["a", "", "Tibee", "Tibee shrimp", "y", "q"],
        )
        added = Species.objects.get(variety="Tibee")
        self.assertTrue(added.is_hybrid)
        self.assertEqual(added.genus, "")
        self.assertIsNone(added.parent)
        self.assertEqual(added.full_scientific_name, "Hybrid 'Tibee'")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, added)

    def test_blank_on_both_prompts_adds_nothing(self):
        Lot.objects.filter(pk=self.lot.pk).update(lot_name="Sponge filter")
        Lot.objects.filter(pk=self.second.pk).update(lot_name="Sponge filters")
        before = Species.objects.count()
        self._run("--review", "--min-lots", "1", "--include-unmatched", answers=["a", "", "", "q"])
        self.assertEqual(Species.objects.count(), before)

    def test_a_strain_needs_its_parent_to_exist_first(self):
        Lot.objects.filter(pk=self.lot.pk).update(lot_name="Blue dream shrimp")
        Lot.objects.filter(pk=self.second.pk).update(lot_name="Blue dream shrimps")
        self._run(
            "--review",
            "--min-lots",
            "1",
            "--include-unmatched",
            answers=["a", "Neocaridina davidi", "Blue Dream", "q"],
        )
        self.assertFalse(Species.objects.filter(variety="Blue Dream").exists())

    def test_status_says_whether_the_curated_list_is_loaded(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("backfill_lot_species", "--status", stdout=out)
        output = out.getvalue()
        self.assertIn("Species list", output)
        self.assertIn("curated", output.lower())
        self.assertIn("distinct names", output)


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
        # The "not a species" table only has the normalised key.
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
        from django.template.loader import get_template

        source = get_template("auctions/bulk_add_lots.html").template.source
        self.assertIn("newElement.find('input[name$=\"-species\"]')", source)
        self.assertIn("newElement.find('.species-hint').html('')", source)


class CopyingLotsToANewAuctionTests(StandardTestCase):
    """Copying lots to a new auction keeps their species."""

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
    def test_a_brand_new_auction_has_it_on(self):
        fresh = Auction.objects.create(title="Brand new", created_by=self.user, date_start=timezone.now())
        self.assertTrue(fresh.use_scientific_name)

    def test_a_clone_of_an_auction_that_has_it_keeps_it(self):
        self.assertTrue(self.online_auction.use_scientific_name)
        give_contact_info(self.user)  # AuctionCreateView refuses somebody with no contact info
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
        self.online_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=5)
        self.online_auction.save()
        self.online_auction.refresh_from_db()
        self.assertTrue(self.online_auction.use_scientific_name)


class UnconfiguredProvider(FakeProvider):
    """A deployment with no API key: ``assist_enabled()``, not a missing provider, says the model is off."""

    def is_configured(self):
        return False


@isolated_cache("species-lookup-api")
class ClubSpeciesLookupAPITests(StandardTestCase):
    """/api/v1/clubs/<slug>/species-lookup/: permissions, response shape and LLM budget."""

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
        # Another club's key with the permission, to check slug scoping.
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
        self.other_url = reverse("api_club_species_lookup", kwargs={"slug": self.other_club.slug})
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
        # "Bolivian ram" is only reachable through its genus sibling "Ram".
        self.ramirezi = make_species("Mikrogeophagus", "ramirezi", "Ram cichlid", ["Ram"])
        self.altispinosus = make_species("Mikrogeophagus", "altispinosus")

    def get(self, **params):
        return self.client.get(self.url, params, HTTP_X_API_KEY=self.raw_key)

    def test_a_key_is_required(self):
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

    def test_nothing_but_get_and_post_is_answered(self):
        response = self.client.delete(self.url, HTTP_X_API_KEY=self.raw_key)
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
        data = self.get(q="Yellow lab").json()
        self.assertEqual(data["query"], "Yellow lab")
        self.assertEqual(data["source"], "exact")
        self.assertTrue(data["unambiguous"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["full_scientific_name"], "Labidochromis caeruleus")

    def test_the_whole_record_travels_with_the_match(self):
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
        # The common name is its own field in this payload.
        self.assertEqual(result["label"], "Labidochromis caeruleus")
        self.assertTrue(result["approved"])
        self.assertEqual(sorted(name["name"] for name in result["common_names"]), ["Electric yellow", "Yellow lab"])

    def test_a_cultivar_comes_back_under_its_full_name(self):
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
        make_species("Labidochromis", "gigas")
        data = self.get(q="Labidochromis").json()
        self.assertGreater(data["total_matches"], 1)
        self.assertFalse(data["unambiguous"])

    def test_a_handful_comes_back_and_total_matches_says_how_many_there_were(self):
        """Five results at most; total_matches says how many there were."""
        for index in range(MAX_GENUS_MATCHES - 1):
            make_species("Labidochromis", f"testspecies{index}")
        data = self.get(q="Labidochromis").json()
        self.assertEqual(data["count"], MAX_SUGGESTIONS)
        self.assertEqual(len(data["results"]), MAX_SUGGESTIONS)
        self.assertEqual(data["total_matches"], MAX_GENUS_MATCHES)

    def _two_angelfish(self):
        """The pair only a category can separate: same name, one marine, one not."""
        marine = Category.objects.create(name="Saltwater fish")
        freshwater = make_species("Pterophyllum", "scalare", "Angelfish", aquarium_use="commercial")
        emperor = make_species("Pomacanthus", "imperator", "Angelfish", aquarium_use="commercial")
        Species.objects.filter(pk=freshwater.pk).update(category=self.cichlids, freshwater=True)
        Species.objects.filter(pk=emperor.pk).update(category=marine, freshwater=False, saltwater=True)
        return marine, freshwater, emperor

    def test_a_category_name_matches_case_insensitively(self):
        marine, _freshwater, emperor = self._two_angelfish()
        data = self.get(q="angelfish", category="saltwater FISH").json()
        self.assertEqual(data["results"][0]["id"], emperor.pk)
        self.assertEqual(data["results"][0]["category"]["id"], marine.pk)

    def test_a_category_id_matches_on_the_id(self):
        marine, _freshwater, emperor = self._two_angelfish()
        data = self.get(q="angelfish", category_id=marine.pk).json()
        self.assertEqual(data["results"][0]["id"], emperor.pk)

    def test_a_category_only_reorders_and_never_filters(self):
        marine, freshwater, _emperor = self._two_angelfish()
        data = self.get(q="angelfish", category_id=marine.pk).json()
        self.assertIn(freshwater.pk, [result["id"] for result in data["results"]])

    def test_a_category_nobody_has_is_an_error_rather_than_a_shrug(self):
        for params in ({"category": "chiclids"}, {"category_id": "999999"}, {"category_id": "cichlids"}):
            response = self.get(q="Yellow lab", **params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn("categor", response.json()["error"])

    def test_passing_both_kinds_of_category_is_an_error(self):
        response = self.get(q="Yellow lab", category="Cichlids", category_id=self.cichlids.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not both", response.json()["error"])

    def test_the_model_runs_without_being_asked_for(self):
        provider = FakeProvider([{"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"}])
        llm.set_provider_override(provider)
        try:
            data = self.get(q="Bolivian ram").json()
            self.assertTrue(data["llm"])
            self.assertEqual(data["source"], "llm")
            self.assertEqual(data["results"][0]["id"], self.altispinosus.pk)
            self.assertEqual(provider.call_count, 1)
        finally:
            llm.set_provider_override(None)

    def test_a_database_answer_never_reaches_the_model(self):
        provider = FakeProvider([{"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"}])
        llm.set_provider_override(provider)
        try:
            data = self.get(q="Yellow lab").json()
            self.assertFalse(data["llm"])
            self.assertEqual(provider.call_count, 0)
        finally:
            llm.set_provider_override(None)

    def test_the_model_answer_is_remembered_so_the_next_caller_is_free(self):
        provider = FakeProvider([{"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"}])
        llm.set_provider_override(provider)
        try:
            self.get(q="Bolivian ram")
            data = self.get(q="BOLIVIAN RAM!").json()
            self.assertEqual(data["source"], "cache")
            self.assertEqual(provider.call_count, 1)
        finally:
            llm.set_provider_override(None)

    def test_every_response_says_what_is_left_of_the_budget(self):
        """Every response carries the remaining budget, including refusals."""
        provider = FakeProvider([{"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"}])
        llm.set_provider_override(provider)
        try:
            limit = views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY
            free = self.get(q="Yellow lab")
            self.assertEqual(free["X-Species-LLM-Limit"], str(limit))
            self.assertEqual(free["X-Species-LLM-Remaining"], str(limit))
            self.assertTrue(free["X-Species-LLM-Reset"])
            spent = self.get(q="Bolivian ram")
            self.assertEqual(spent["X-Species-LLM-Remaining"], str(limit - 1))
            self.assertEqual(self.get(q="").status_code, 400)
            self.assertEqual(self.get(q="")["X-Species-LLM-Remaining"], str(limit - 1))
        finally:
            llm.set_provider_override(None)

    def test_out_of_budget_with_nothing_to_show_is_a_rate_limit_error(self):
        provider = FakeProvider([{"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"}])
        llm.set_provider_override(provider)
        original = views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY
        views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = 0
        try:
            response = self.get(q="Bolivian ram")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(provider.call_count, 0)
            self.assertEqual(response["X-Species-LLM-Remaining"], "0")
            self.assertTrue(int(response["Retry-After"]) > 0)
        finally:
            views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = original
            llm.set_provider_override(None)

    def test_out_of_budget_still_answers_everything_the_database_knows(self):
        original = views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY
        views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = 0
        try:
            response = self.get(q="Yellow lab")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["results"][0]["id"], self.yellow_lab.pk)
        finally:
            views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = original

    def test_out_of_budget_never_teaches_the_site_that_a_name_is_not_a_species(self):
        """Running out of budget never caches "not a species"."""
        provider = FakeProvider([{"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"}])
        llm.set_provider_override(provider)
        original = views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY
        views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = 0
        try:
            self.get(q="Bolivian ram")
        finally:
            views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = original
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="bolivian ram").exists())
        try:
            self.assertEqual(self.get(q="Bolivian ram").json()["results"][0]["id"], self.altispinosus.pk)
        finally:
            llm.set_provider_override(None)

    def test_the_budget_is_one_clubs_and_not_the_sites(self):
        provider = FakeProvider(
            [
                {"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"},
                {"kind": "species", "scientific_name": "Mikrogeophagus altispinosus"},
            ]
        )
        llm.set_provider_override(provider)
        original = views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY
        views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = 1
        try:
            self.assertEqual(self.get(q="Bolivian ram").status_code, 200)
            self.assertEqual(self.get(q="something else entirely").status_code, 429)
            other = self.client.get(
                self.other_url, {"q": "another name nobody has looked up"}, HTTP_X_API_KEY=self.other_raw_key
            )
            self.assertEqual(other.status_code, 200)
            self.assertEqual(provider.call_count, 2)
        finally:
            views.club_api.SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = original
            llm.set_provider_override(None)

    def test_a_site_with_no_model_answers_from_the_database_and_remembers_nothing(self):
        llm.set_provider_override(UnconfiguredProvider())
        try:
            data = self.get(q="Bolivian ram").json()
            self.assertFalse(data["llm"])
            self.assertEqual(data["results"], [])
        finally:
            llm.set_provider_override(None)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="bolivian ram").exists())

    def test_a_club_sees_the_species_it_added_itself(self):
        mine = Species.objects.create(
            genus="Ancistrus",
            species="sp. l183",
            common_name="Starlight bristlenose",
            source="admin",
            approved=False,
            club=self.club,
        )
        SpeciesCommonName.objects.create(species=mine, name="Starlight bristlenose", source="admin")
        data = self.get(q="Starlight bristlenose").json()
        self.assertEqual(data["results"][0]["id"], mine.pk)
        self.assertFalse(data["results"][0]["approved"])

    def test_it_does_not_see_another_clubs_unapproved_species(self):
        theirs = Species.objects.create(
            genus="Ancistrus",
            species="sp. l184",
            common_name="Somebody elses guess",
            source="admin",
            approved=False,
            club=self.other_club,
        )
        SpeciesCommonName.objects.create(species=theirs, name="Somebody elses guess", source="admin")
        self.assertEqual(self.get(q="Somebody elses guess").json()["results"], [])

    def test_using_the_key_stamps_it(self):
        self.assertIsNone(self.api_key.last_used_at)
        self.get(q="Yellow lab")
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)

    def _log_in_as_a_club_admin(self):
        ClubMember.objects.create(club=self.club, user=self.user, permission_edit_club=True)
        self.client.login(username="my_lot", password="testpassword")

    def test_the_permission_can_be_granted_from_the_key_page(self):
        self._log_in_as_a_club_admin()
        create_url = reverse("club_api_key_create", kwargs={"slug": self.club.slug})
        self.assertContains(self.client.get(create_url), "can_look_up_species")
        self.client.post(
            create_url, {"name": "Website", "can_look_up_species_present": "1", "can_look_up_species": "on"}
        )
        self.assertTrue(ClubAPIKey.objects.get(club=self.club, name="Website").can_look_up_species)

    def test_the_key_page_documents_every_species_endpoint_once_it_is_granted(self):
        self._log_in_as_a_club_admin()
        url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": self.api_key.pk})
        page = self.client.get(url)
        self.assertContains(page, f"/api/v1/clubs/{self.club.slug}/species-lookup/")
        self.assertContains(page, "X-Species-LLM-Remaining")
        self.assertContains(page, "common-names/")
        self.api_key.can_look_up_species = False
        self.api_key.save(update_fields=["can_look_up_species"])
        self.assertNotContains(self.client.get(url), "species-lookup")

    def test_field_mappings_are_only_offered_to_a_key_that_writes_members(self):
        self._log_in_as_a_club_admin()
        url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": self.api_key.pk})
        self.api_key.can_add_club_members = False
        self.api_key.save(update_fields=["can_add_club_members"])
        self.assertNotContains(self.client.get(url), "Field mappings")
        self.api_key.can_add_club_members = True
        self.api_key.save(update_fields=["can_add_club_members"])
        self.assertContains(self.client.get(url), "Field mappings")


@isolated_cache("species-create-api")
class ClubSpeciesCreateAPITests(StandardTestCase):
    """POST /api/v1/clubs/<slug>/species-lookup/: create a species, unapproved and de-duplicated."""

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
        self.url = reverse("api_club_species_lookup", kwargs={"slug": self.club.slug})
        self.plecos = Category.objects.create(name="Plecos")
        self.shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", source="aquarium")

    def post(self, payload, key=None):
        return self.client.post(self.url, payload, content_type="application/json", HTTP_X_API_KEY=key or self.raw_key)

    def test_it_adds_a_species_and_hands_back_the_whole_record(self):
        response = self.post(
            {
                "scientific_name": "Ancistrus sp. L183",
                "common_name": "Starlight bristlenose",
                "other_names": ["L183", "White seam bristlenose"],
                "category": "plecos",
            }
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        species = Species.objects.get(pk=data["id"])
        self.assertEqual(species.genus, "Ancistrus")
        self.assertEqual(species.species, "sp. l183")
        self.assertEqual(species.common_name, "Starlight bristlenose")
        self.assertEqual(species.category, self.plecos)
        self.assertEqual(data["category"], {"id": self.plecos.pk, "name": "Plecos"})
        self.assertEqual(
            sorted(name["name"] for name in data["common_names"]),
            ["L183", "Starlight bristlenose", "White seam bristlenose"],
        )

    def test_a_club_can_add_a_hybrid(self):
        response = self.post({"is_hybrid": True, "variety": "Tibee", "common_name": "Tibee shrimp"})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        species = Species.objects.get(pk=data["id"])
        self.assertTrue(species.is_hybrid)
        self.assertEqual((species.genus, species.species, species.scientific_name), ("", "", ""))
        self.assertEqual(data["full_scientific_name"], "Hybrid 'Tibee'")

    def test_a_hybrid_with_a_scientific_name_or_a_parent_is_refused(self):
        response = self.post({"is_hybrid": True, "variety": "Tibee", "scientific_name": "Caridina cantonensis"})
        self.assertEqual(response.status_code, 400, response.content)
        response = self.post({"is_hybrid": True, "variety": "Tibee", "parent": self.shrimp.pk})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Species.objects.filter(variety="Tibee").exists())

    def test_a_hybrid_has_to_be_called_something(self):
        self.assertEqual(self.post({"is_hybrid": True, "common_name": "Some cross"}).status_code, 400)

    def test_what_a_key_adds_is_this_clubs_and_not_approved(self):
        data = self.post({"scientific_name": "Ancistrus sp. L183", "common_name": "Starlight bristlenose"}).json()
        species = Species.objects.get(pk=data["id"])
        self.assertFalse(species.approved)
        self.assertFalse(data["approved"])
        self.assertEqual(species.club, self.club)
        self.assertIsNone(species.added_by)
        # Not "manual": import_fishbase folds manual rows into the imported list.
        self.assertEqual(species.source, "admin")
        self.assertTrue(species.in_trade_override)

    def test_the_club_can_then_look_up_what_it_added_and_nobody_else_can(self):
        self.post(
            {
                "scientific_name": "Ancistrus sp. L183",
                "common_name": "Starlight bristlenose",
                "other_names": "L183",
            }
        )
        mine = self.client.get(self.url, {"q": "l183"}, HTTP_X_API_KEY=self.raw_key).json()
        self.assertEqual(mine["source"], "exact")
        self.assertTrue(mine["unambiguous"])
        self.assertEqual(mine["results"][0]["common_name"], "Starlight bristlenose")
        other_raw_key, other_prefix, other_key_hash = ClubAPIKey.generate()
        ClubAPIKey.objects.create(
            club=self.other_club,
            name="Someone else's website",
            prefix=other_prefix,
            key_hash=other_key_hash,
            can_look_up_species=True,
        )
        theirs = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": self.other_club.slug}),
            {"q": "l183"},
            HTTP_X_API_KEY=other_raw_key,
        )
        self.assertEqual(theirs.json()["results"], [])

    def test_other_names_may_be_one_comma_separated_string(self):
        data = self.post(
            {"scientific_name": "Ancistrus sp. L183", "common_name": "Starlight", "other_names": "L183, LDA08"}
        ).json()
        self.assertEqual(sorted(name["name"] for name in data["common_names"]), ["L183", "LDA08", "Starlight"])

    def test_a_strain_carries_its_parents_name(self):
        response = self.post({"variety": "Blue Dream", "parent": self.shrimp.pk, "common_name": "Blue dream shrimp"})
        self.assertEqual(response.status_code, 201, response.content)
        species = Species.objects.get(pk=response.json()["id"])
        self.assertEqual(species.genus, "Neocaridina")
        self.assertEqual(species.species, "davidi")
        self.assertEqual(species.variety, "Blue Dream")
        self.assertEqual(species.parent, self.shrimp)
        self.assertEqual(response.json()["full_scientific_name"], "Neocaridina davidi 'Blue Dream'")

    def test_a_strain_and_its_parent_go_together(self):
        variety_only = self.post({"variety": "Blue Dream", "common_name": "Blue dream shrimp"})
        self.assertEqual(variety_only.status_code, 400)
        self.assertIn("parent", variety_only.json())
        parent_only = self.post({"parent": self.shrimp.pk, "common_name": "Some shrimp"})
        self.assertEqual(parent_only.status_code, 400)
        self.assertIn("variety", parent_only.json())

    def test_a_strain_of_a_strain_is_refused(self):
        blue_dream = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=self.shrimp, source="aquarium"
        )
        response = self.post({"variety": "Blue Dream Deep", "parent": blue_dream.pk})
        self.assertEqual(response.status_code, 400)
        self.assertIn("parent", response.json())

    def test_a_parent_this_club_cannot_see_is_refused(self):
        theirs = Species.objects.create(
            genus="Ancistrus", species="sp. l184", source="admin", approved=False, club=self.other_club
        )
        response = self.post({"variety": "Gold", "parent": theirs.pk})
        self.assertEqual(response.status_code, 400)
        self.assertIn("parent", response.json())

    def test_a_scientific_name_is_required_unless_it_is_a_strain(self):
        response = self.post({"common_name": "Some fish somebody sold"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("scientific_name", response.json())

    def test_a_species_that_is_already_here_comes_back_instead_of_being_copied(self):
        response = self.post({"scientific_name": "Neocaridina davidi", "common_name": "Red cherry"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["species"]["id"], self.shrimp.pk)
        self.assertEqual(Species.objects.filter(genus="Neocaridina", species="davidi").count(), 1)

    def test_the_clash_check_ignores_case_the_way_the_form_does(self):
        self.assertEqual(self.post({"scientific_name": "neocaridina DAVIDI"}).status_code, 409)

    def test_a_common_name_that_belongs_to_another_species_is_refused(self):
        response = self.post({"scientific_name": "Ancistrus sp. L183", "common_name": "Cherry shrimp"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("common_name", response.json())
        self.assertFalse(Species.objects.filter(genus="Ancistrus").exists())

    def test_an_other_name_that_belongs_to_another_species_is_refused(self):
        response = self.post(
            {"scientific_name": "Ancistrus sp. L183", "common_name": "Starlight", "other_names": ["Cherry shrimp"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("other_names", response.json())

    def test_the_names_it_writes_are_scoped_like_the_species(self):
        data = self.post(
            {"scientific_name": "Ancistrus sp. L183", "common_name": "Starlight", "other_names": ["L183"]}
        ).json()
        names = SpeciesCommonName.objects.filter(species_id=data["id"])
        self.assertEqual(names.count(), 2)
        for name in names:
            self.assertFalse(name.approved)
            self.assertEqual(name.club, self.club)

    def test_a_category_nobody_has_is_refused_before_anything_is_written(self):
        response = self.post({"scientific_name": "Ancistrus sp. L183", "category": "Plecoz"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Species.objects.filter(genus="Ancistrus").exists())

    def test_a_category_id_works_too(self):
        data = self.post({"scientific_name": "Ancistrus sp. L183", "category_id": self.plecos.pk}).json()
        self.assertEqual(data["category"]["id"], self.plecos.pk)

    def test_the_species_permission_is_required(self):
        self.api_key.can_look_up_species = False
        self.api_key.save(update_fields=["can_look_up_species"])
        self.assertEqual(self.post({"scientific_name": "Ancistrus sp. L183"}).status_code, 403)
        self.assertFalse(Species.objects.filter(genus="Ancistrus").exists())

    def test_a_key_is_required(self):
        response = self.client.post(
            self.url, {"scientific_name": "Ancistrus sp. L183"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)

    def test_a_body_that_is_not_an_object_is_a_400_rather_than_a_500(self):
        for body in ("[1, 2, 3]", '"Ancistrus"'):
            response = self.client.post(self.url, body, content_type="application/json", HTTP_X_API_KEY=self.raw_key)
            self.assertEqual(response.status_code, 400, body)

    def test_a_signed_in_club_admin_is_credited_for_what_they_add(self):
        ClubMember.objects.create(club=self.club, user=self.user, permission_add_edit=True)
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            self.url,
            {"scientific_name": "Ancistrus sp. L183", "common_name": "Starlight"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        species = Species.objects.get(pk=response.json()["id"])
        self.assertEqual(species.added_by, self.user)
        self.assertEqual(species.club, self.club)
        self.assertFalse(species.approved)

    def test_a_club_member_with_no_standing_may_not_add_one(self):
        ClubMember.objects.create(club=self.club, user=self.user)
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            self.url, {"scientific_name": "Ancistrus sp. L183"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)


@isolated_cache("species-common-name-api")
class ClubSpeciesCommonNameAPITests(StandardTestCase):
    """POST /api/v1/clubs/<slug>/species-lookup/<id>/common-names/"""

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
        self.lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        self.url = reverse("api_club_species_common_names", kwargs={"slug": self.club.slug, "identifier": self.lab.pk})

    def post(self, payload, url=None):
        return self.client.post(url or self.url, payload, content_type="application/json", HTTP_X_API_KEY=self.raw_key)

    def test_it_attaches_the_name_and_the_matcher_finds_the_species_by_it(self):
        response = self.post({"name": "Yellow lab"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["created"])
        self.assertEqual(response.json()["species"]["id"], self.lab.pk)
        lookup = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": self.club.slug}),
            {"q": "yellow lab"},
            HTTP_X_API_KEY=self.raw_key,
        ).json()
        self.assertEqual(lookup["source"], "exact")
        self.assertEqual(lookup["results"][0]["id"], self.lab.pk)

    def test_the_name_is_stamped_so_a_re_import_cannot_delete_it(self):
        self.post({"name": "Yellow lab"})
        name = SpeciesCommonName.objects.get(species=self.lab, name="Yellow lab")
        self.assertEqual(name.source, "admin")
        self.assertEqual(name.name_normalized, "yellow lab")

    def test_it_never_edits_what_is_already_there(self):
        self.post({"name": "Yellow lab"})
        self.lab.refresh_from_db()
        self.assertEqual(self.lab.common_name, "Blue streak hap")
        self.assertFalse(SpeciesCommonName.objects.get(species=self.lab, name="Yellow lab").is_preferred)
        self.assertTrue(SpeciesCommonName.objects.get(species=self.lab, name="Blue streak hap").is_preferred)

    def test_sending_a_name_it_already_has_is_not_an_error(self):
        first = self.post({"name": "Yellow lab"})
        second = self.post({"name": "yellow  lab!"})
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(SpeciesCommonName.objects.filter(species=self.lab, source="admin").count(), 1)

    def test_a_name_with_nothing_in_it_is_refused(self):
        for name in ("", "   ", "!!!"):
            response = self.post({"name": name})
            self.assertEqual(response.status_code, 400, name)

    def test_another_clubs_unapproved_species_is_not_there_to_name(self):
        theirs = Species.objects.create(
            genus="Ancistrus", species="sp. l184", source="admin", approved=False, club=self.other_club
        )
        url = reverse("api_club_species_common_names", kwargs={"slug": self.club.slug, "identifier": theirs.pk})
        self.assertEqual(self.post({"name": "Something"}, url=url).status_code, 404)

    def test_the_club_may_name_a_species_it_added_itself(self):
        mine = Species.objects.create(
            genus="Ancistrus", species="sp. l183", source="admin", approved=False, club=self.club
        )
        url = reverse("api_club_species_common_names", kwargs={"slug": self.club.slug, "identifier": mine.pk})
        self.assertEqual(self.post({"name": "Starlight bristlenose"}, url=url).status_code, 201)

    def test_the_species_permission_is_required(self):
        self.api_key.can_look_up_species = False
        self.api_key.save(update_fields=["can_look_up_species"])
        self.assertEqual(self.post({"name": "Yellow lab"}).status_code, 403)
        self.assertFalse(SpeciesCommonName.objects.filter(name="Yellow lab").exists())

    def _name_url(self, identifier):
        return reverse("api_club_species_common_names", kwargs={"slug": self.club.slug, "identifier": identifier})

    def test_a_scientific_name_works_in_place_of_an_id(self):
        response = self.post({"name": "Yellow lab"}, url=self._name_url("labidochromis CAERULEUS"))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["species"]["id"], self.lab.pk)

    def test_a_strain_is_named_by_its_full_name(self):
        shrimp = make_species("Neocaridina", "davidi", "Cherry shrimp", source="aquarium")
        blue_dream = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=shrimp, source="aquarium"
        )
        response = self.post({"name": "Blue dreams"}, url=self._name_url("Neocaridina davidi 'Blue Dream'"))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["species"]["id"], blue_dream.pk)
        plain = self.post({"name": "Cherries"}, url=self._name_url("Neocaridina davidi"))
        self.assertEqual(plain.json()["species"]["id"], shrimp.pk)

    def test_a_name_that_matches_nothing_is_a_404(self):
        self.assertEqual(self.post({"name": "Whatever"}, url=self._name_url("Betta splendens")).status_code, 404)

    def test_a_name_that_already_names_another_species_is_refused(self):
        guppy = make_species("Poecilia", "reticulata", "Guppy")
        response = self.post({"name": "guppy"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["species"]["id"], guppy.pk)
        self.assertFalse(SpeciesCommonName.objects.filter(species=self.lab, name="guppy").exists())

    def test_a_name_another_club_added_privately_does_not_block_this_one(self):
        theirs = make_species("Betta", "splendens", "Siamese fighting fish")
        SpeciesCommonName.objects.create(
            species=theirs, name="House fish", source="admin", approved=False, club=self.other_club
        )
        self.assertEqual(self.post({"name": "House fish"}).status_code, 201)

    def test_the_name_is_this_clubs_until_it_is_approved(self):
        self.post({"name": "Yellow lab"})
        name = SpeciesCommonName.objects.get(species=self.lab, name="Yellow lab")
        self.assertFalse(name.approved)
        self.assertEqual(name.club, self.club)
        self.assertIsNone(name.added_by)

    def test_another_club_is_not_answered_with_it(self):
        self.post({"name": "Yellow lab"})
        other_raw_key, other_prefix, other_key_hash = ClubAPIKey.generate()
        ClubAPIKey.objects.create(
            club=self.other_club,
            name="Someone else's website",
            prefix=other_prefix,
            key_hash=other_key_hash,
            can_look_up_species=True,
        )
        theirs = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": self.other_club.slug}),
            {"q": "yellow lab"},
            HTTP_X_API_KEY=other_raw_key,
        ).json()
        self.assertEqual(theirs["results"], [])
        mine = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": self.club.slug}),
            {"q": "yellow lab"},
            HTTP_X_API_KEY=self.raw_key,
        ).json()
        self.assertEqual(mine["results"][0]["id"], self.lab.pk)

    def test_a_remembered_answer_does_not_hand_it_to_another_club_either(self):
        """A cached answer must not hand a club-scoped name to another club either."""
        self.post({"name": "Yellow lab"})
        SpeciesSearchCache.objects.create(search_text="yellow lab", species=self.lab, source="llm")
        other_raw_key, other_prefix, other_key_hash = ClubAPIKey.generate()
        ClubAPIKey.objects.create(
            club=self.other_club,
            name="Someone else's website",
            prefix=other_prefix,
            key_hash=other_key_hash,
            can_look_up_species=True,
        )
        theirs = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": self.other_club.slug}),
            {"q": "yellow lab"},
            HTTP_X_API_KEY=other_raw_key,
        ).json()
        self.assertEqual(theirs["results"], [])
        mine = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": self.club.slug}),
            {"q": "yellow lab"},
            HTTP_X_API_KEY=self.raw_key,
        ).json()
        self.assertEqual(mine["results"][0]["id"], self.lab.pk)

    def test_approving_it_is_what_makes_it_everybodys(self):
        self.post({"name": "Yellow lab"})
        self.assertEqual(exact_matches("yellow lab", club=self.other_club), [])
        SpeciesCommonName.objects.filter(species=self.lab, name="Yellow lab").update(approved=True)
        self.assertEqual(exact_matches("yellow lab", club=self.other_club), [self.lab])


@isolated_cache("species-session-auth")
class ClubSpeciesAPISessionAuthTests(StandardTestCase):
    """Session auth on the club API checks the same permissions as a key, against ClubMember."""

    def setUp(self):
        super().setUp()
        llm.set_provider_override(None)
        self.club = Club.objects.create(name="Cichlid Keepers Club")
        self.other_club = Club.objects.create(name="Some Other Club")
        self.lab = make_species("Labidochromis", "caeruleus", "Yellow lab")
        self.url = reverse("api_club_species_lookup", kwargs={"slug": self.club.slug})

    def get(self):
        return self.client.get(self.url, {"q": "yellow lab"})

    def post(self):
        return self.client.post(self.url, {"scientific_name": "Ancistrus sp. L183"}, content_type="application/json")

    def _sign_in(self, **permissions):
        if permissions.pop("member", True):
            ClubMember.objects.create(club=self.club, user=self.user, **permissions)
        self.client.login(username="my_lot", password="testpassword")

    def test_signed_out_is_refused(self):
        self.assertEqual(self.get().status_code, 401)

    def test_signed_in_with_no_club_at_all_is_refused(self):
        self._sign_in(member=False)
        self.assertEqual(self.get().status_code, 403)

    def test_a_club_member_with_no_permissions_is_refused(self):
        self._sign_in()
        self.assertEqual(self.get().status_code, 403)

    def test_belonging_to_another_club_is_worth_nothing_here(self):
        ClubMember.objects.create(club=self.other_club, user=self.user, permission_admin=True)
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.get().status_code, 403)

    def test_permission_view_reads_but_does_not_write(self):
        self._sign_in(permission_view=True)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], self.lab.pk)
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(Species.objects.filter(genus="Ancistrus").exists())

    def test_permission_add_edit_writes(self):
        self._sign_in(permission_add_edit=True)
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)
        species = Species.objects.get(pk=response.json()["id"])
        self.assertEqual(species.added_by, self.user)
        self.assertEqual(species.club, self.club)
        self.assertFalse(species.approved)

    def test_permission_admin_is_the_wildcard_it_is_everywhere_else(self):
        self._sign_in(permission_admin=True)
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.post().status_code, 201)

    def test_a_site_superuser_is_let_through_as_everywhere_else_on_the_site(self):
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        self.assertEqual(self.get().status_code, 200)

    def test_naming_a_species_needs_the_same_standing_as_adding_one(self):
        self._sign_in(permission_view=True)
        url = reverse("api_club_species_common_names", kwargs={"slug": self.club.slug, "identifier": self.lab.pk})
        self.assertEqual(
            self.client.post(url, {"name": "Electric yellow"}, content_type="application/json").status_code, 403
        )
        ClubMember.objects.filter(club=self.club, user=self.user).update(permission_add_edit=True)
        response = self.client.post(url, {"name": "Electric yellow"}, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        name = SpeciesCommonName.objects.get(name="Electric yellow")
        self.assertEqual(name.added_by, self.user)
        self.assertEqual(name.club, self.club)
        self.assertFalse(name.approved, "a club admin is not a site admin")

    def test_a_superuser_naming_a_species_names_it_for_everybody(self):
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        url = reverse("api_club_species_common_names", kwargs={"slug": self.club.slug, "identifier": self.lab.pk})
        self.client.post(url, {"name": "Electric yellow"}, content_type="application/json")
        self.assertTrue(SpeciesCommonName.objects.get(name="Electric yellow").approved)


class BrowsableAPITests(StandardTestCase):
    """The browsable API is off outside DEBUG, so API URLs never render docstrings as HTML."""

    def test_json_only_unless_this_is_a_debug_box(self):
        from django.conf import settings

        from fishauctions import settings as settings_module

        browsable = "rest_framework.renderers.BrowsableAPIRenderer"
        renderers = settings.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]
        # Tests force DEBUG off, so read the settings file's own value.
        if settings_module.DEBUG:
            self.assertIn(browsable, renderers)
        else:
            self.assertEqual(renderers, ["rest_framework.renderers.JSONRenderer"])

    def test_the_views_are_built_with_that_set(self):
        from rest_framework.renderers import BrowsableAPIRenderer

        from fishauctions import settings as settings_module

        if settings_module.DEBUG:
            self.skipTest("a dev box keeps the browsable API on purpose")
        self.assertNotIn(BrowsableAPIRenderer, views.ClubSpeciesLookupAPIView.renderer_classes)
        self.assertNotIn(BrowsableAPIRenderer, views.ClubMemberListCreateAPIView.renderer_classes)

    def test_a_browser_still_gets_an_answer_rather_than_a_406(self):
        from rest_framework.renderers import BrowsableAPIRenderer

        if BrowsableAPIRenderer in views.ClubSpeciesLookupAPIView.renderer_classes:
            self.skipTest("a dev box keeps the browsable API on purpose, and it answers in HTML")
        club = Club.objects.create(name="Cichlid Keepers Club")
        response = self.client.get(
            reverse("api_club_species_lookup", kwargs={"slug": club.slug}),
            {"q": "guppy"},
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")


@isolated_cache("species-feedback")
class RememberedAnswersCanBeUnlearnedTests(StandardTestCase):
    """Cached answers written by sellers can be unlearned. See species_matching.record_choice."""

    def setUp(self):
        super().setUp()
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy", aquarium_use="commercial")
        self.betta = make_species("Betta", "splendens", "Siamese fighting fish", aquarium_use="commercial")
        remember("sponge filter", self.guppy, source="user", user=self.user)

    def _row(self):
        return SpeciesSearchCache.objects.get(search_text="sponge filter")

    def test_leaving_the_answer_alone_counts_once_on_the_first_save(self):
        record_choice("Sponge filter", self.guppy, first_save=True)
        self.assertEqual(self._row().accepts, 1)
        self.assertEqual(self._row().rejects, 0)

    def test_re_saving_a_lot_is_not_a_second_vote(self):
        record_choice("Sponge filter", self.guppy, first_save=True)
        for _ in range(5):
            record_choice("Sponge filter", self.guppy, first_save=False)
        self.assertEqual(self._row().accepts, 1)

    def test_one_person_clearing_it_once_does_not_throw_the_answer_away(self):
        """One person clearing a cached answer once doesn't delete it."""
        record_choice("Sponge filter", None, first_save=True)
        self.assertTrue(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())
        self.assertEqual(self._row().rejects, 1)
        self.assertFalse(SpeciesNameRejection.objects.exists())

    def test_three_lots_disagreeing_does(self):
        for _ in range(SpeciesSearchCache.MIN_REJECTS_TO_RETIRE):
            record_choice("Sponge filter", None, first_save=True)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())
        self.assertTrue(SpeciesNameRejection.objects.filter(search_text="sponge filter", species=self.guppy).exists())

    def test_a_negative_is_scored_when_somebody_picks_a_species(self):
        """Picking a species scores a remembered negative."""
        remember("box of gravel", None, source="llm")
        record_choice("box of gravel", self.guppy, first_save=True, user=self.user)
        row = SpeciesSearchCache.objects.get(search_text="box of gravel")
        self.assertIsNone(row.species)
        self.assertEqual(row.rejects, 1)

    def test_one_seller_picking_a_species_is_not_the_sites_answer(self):
        """One seller picking a species doesn't replace the cached answer."""
        remember("box of gravel", None, source="llm")
        record_choice("box of gravel", self.guppy, first_save=True, user=self.user)
        self.assertIsNone(SpeciesSearchCache.objects.get(search_text="box of gravel").species)

    def test_three_of_them_replace_the_answer(self):
        """Enough rejections replace the answer."""
        remember("box of gravel", None, source="llm")
        for _ in range(SpeciesSearchCache.MIN_REJECTS_TO_RETIRE):
            record_choice("box of gravel", self.guppy, first_save=True, user=self.user)
        row = SpeciesSearchCache.objects.get(search_text="box of gravel")
        self.assertEqual(row.species, self.guppy)
        self.assertEqual(row.created_by, self.user)
        self.assertEqual(row.rejects, 0)
        self.assertEqual(row.accepts, 0)

    def test_a_lot_saved_with_no_species_is_not_a_vote_against_a_negative(self):
        """A lot saved with no species is not counted as agreement."""
        remember("box of gravel", None, source="llm")
        for _ in range(5):
            record_choice("box of gravel", None, first_save=True, user=self.user)
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="box of gravel").rejects, 0)

    def test_re_saving_one_lot_is_not_three_people_disagreeing_with_a_negative(self):
        remember("box of gravel", None, source="llm")
        record_choice("box of gravel", self.guppy, first_save=True, user=self.user)
        for _ in range(5):
            record_choice("box of gravel", self.guppy, first_save=False, changed=False, user=self.user)
        self.assertIsNone(SpeciesSearchCache.objects.get(search_text="box of gravel").species)

    def test_a_replacement_still_refuses_an_unapproved_species(self):
        """A replacement still refuses an unapproved species."""
        remember("box of gravel", None, source="llm")
        private = Species.objects.create(genus="Yssichromis", species="piceatus", approved=False, source="admin")
        for _ in range(SpeciesSearchCache.MIN_REJECTS_TO_RETIRE):
            record_choice("box of gravel", private, first_save=True, user=self.user)
        self.assertIsNone(SpeciesSearchCache.objects.get(search_text="box of gravel").species)

    def test_replacing_an_answer_does_not_inherit_its_votes(self):
        record_choice("Sponge filter", None, first_save=True)
        self.assertEqual(self._row().rejects, 1)
        remember("sponge filter", self.betta, source="user", user=self.user)
        self.assertEqual(self._row().rejects, 0)
        self.assertEqual(self._row().accepts, 0)

    def test_re_saving_one_cleared_lot_is_not_three_lots_disagreeing(self):
        """Re-saving one cleared lot doesn't count as more rejections."""
        record_choice("Sponge filter", None, first_save=True)
        for _ in range(10):
            record_choice("Sponge filter", None)
        self.assertEqual(self._row().rejects, 1)
        self.assertTrue(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())

    def test_a_well_supported_answer_survives_the_floor_being_reached(self):
        SpeciesSearchCache.objects.filter(search_text="sponge filter").update(accepts=90)
        for _ in range(SpeciesSearchCache.MIN_REJECTS_TO_RETIRE):
            record_choice("Sponge filter", None, first_save=True)
        self.assertTrue(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())
        self.assertEqual(self._row().rejects, 3)

    def test_and_stops_surviving_once_the_rejections_pass_a_tenth(self):
        SpeciesSearchCache.objects.filter(search_text="sponge filter").update(accepts=9, rejects=2)
        record_choice("Sponge filter", None, first_save=True)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())

    def test_picking_a_different_species_is_a_rejection_too(self):
        SpeciesSearchCache.objects.filter(search_text="sponge filter").update(rejects=2)
        record_choice("Sponge filter", self.betta, changed=True)
        self.assertEqual(list(SpeciesNameRejection.objects.values_list("species_id", flat=True)), [self.guppy.pk])

    def test_a_name_with_no_remembered_answer_is_not_scored(self):
        record_choice("Some other lot", None, first_save=True)
        self.assertFalse(SpeciesNameRejection.objects.exists())

    def test_a_remembered_no_is_never_scored(self):
        remember("box of gravel", None, source="llm")
        record_choice("box of gravel", None, first_save=True)
        row = SpeciesSearchCache.objects.get(search_text="box of gravel")
        self.assertEqual((row.accepts, row.rejects), (0, 0))

    def _retire_it(self):
        SpeciesSearchCache.objects.filter(search_text="sponge filter").update(
            rejects=SpeciesSearchCache.MIN_REJECTS_TO_RETIRE - 1
        )
        record_choice("Sponge filter", None, first_save=True)

    def test_a_retired_pairing_cannot_be_learned_again(self):
        """A retired pairing cannot be learned again."""
        self._retire_it()
        remember("sponge filter", self.guppy, source="user", user=self.user)
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())

    def test_but_another_species_can_still_be_learned_for_that_name(self):
        self._retire_it()
        remember("sponge filter", self.betta, source="user", user=self.user)
        self.assertEqual(self._row().species, self.betta)

    def test_a_rejection_does_not_overrule_the_species_list(self):
        """A rejection never overrules the species list itself."""
        record_choice("guppy", self.guppy)
        SpeciesNameRejection.objects.create(search_text="guppy", species=self.guppy)
        self.assertEqual(suggest_species("guppy", use_llm=False), ([self.guppy], "exact"))

    def test_the_model_is_never_offered_a_retired_pairing(self):
        """The model is never offered a retired pairing."""
        SpeciesNameRejection.objects.create(search_text="gup bag", species=self.guppy)
        provider = FakeProvider([{"id": self.guppy.pk}])
        llm.set_provider_override(provider)
        try:
            found, source = suggest_species("gup bag", user=self.user)
        finally:
            llm.set_provider_override(None)
        offered = provider.calls[-1]["messages"][-1]["content"]
        # Anchored to a line start: "12: " is also a substring of "112: ".
        self.assertNotIn(f"\n{self.guppy.pk}: ", offered)
        self.assertEqual(found, [])

    def test_and_naming_one_anyway_is_not_written_down_as_no_species(self):
        """A discarded model answer is not cached as "not a species"."""
        SpeciesNameRejection.objects.create(search_text="mystery bag", species=self.guppy)
        provider = FakeProvider([{"id": self.guppy.pk}])
        llm.set_provider_override(provider)
        try:
            found, source = suggest_species("mystery bag", user=self.user)
        finally:
            llm.set_provider_override(None)
        self.assertEqual(found, [])
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="mystery bag").exists())

    def _clear_it_on_a_new_bulk_row(self):
        self.client.login(username="my_lot", password="testpassword")
        return self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.online_auction.slug}),
            data={"lot_name": "Sponge filter", "quantity": 1, "reserve_price": 2, "species": ""},
            content_type="application/json",
        )

    def test_clearing_a_species_on_the_bulk_page_reports_it(self):
        SpeciesSearchCache.objects.filter(search_text="sponge filter").update(
            rejects=SpeciesSearchCache.MIN_REJECTS_TO_RETIRE - 1
        )
        response = self._clear_it_on_a_new_bulk_row()
        self.assertTrue(response.json()["success"], response.json())
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="sponge filter").exists())
        self.assertTrue(SpeciesNameRejection.objects.filter(species=self.guppy).exists())

    def test_but_one_seller_clearing_it_once_only_counts_it(self):
        """One seller clearing it once only counts it."""
        response = self._clear_it_on_a_new_bulk_row()
        self.assertTrue(response.json()["success"], response.json())
        self.assertEqual(self._row().rejects, 1)
        self.assertFalse(SpeciesNameRejection.objects.exists())

    def test_saving_a_bulk_row_with_the_answer_left_alone_counts_it(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.online_auction.slug}),
            data={"lot_name": "Sponge filter", "quantity": 1, "reserve_price": 2, "species": self.guppy.pk},
            content_type="application/json",
        )
        self.assertEqual(self._row().accepts, 1)

    def test_an_admin_moving_a_lot_off_a_species_rejects_the_pairing(self):
        SpeciesSearchCache.objects.filter(search_text="sponge filter").update(
            rejects=SpeciesSearchCache.MIN_REJECTS_TO_RETIRE - 1
        )
        self.lot.lot_name = "Sponge filter"
        self.lot.species = self.guppy
        self.lot.save()
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(
            reverse("auctionlotadmin", kwargs={"pk": self.lot.pk}),
            {
                "lot_name": "Sponge filter",
                "auction": self.online_auction.pk,
                "species": self.betta.pk,
                "species_category": "",
                "summernote_description": "",
                "quantity": 1,
                "reserve_price": 5,
            },
        )
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.betta)
        self.assertTrue(SpeciesNameRejection.objects.filter(search_text="sponge filter", species=self.guppy).exists())


class RetiredPairingsCanBeUndoneTests(StandardTestCase):
    """A site admin's way back, on the gaps page.  "For good" needs an escape hatch."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.rejection = SpeciesNameRejection.objects.create(search_text="fancy guppy", species=self.guppy)

    def test_the_gaps_page_lists_them(self):
        self.client.login(username="admin_user", password="testpassword")
        self.admin_user.is_superuser = True
        self.admin_user.save()
        body = self.client.get(reverse("species_gaps")).content.decode()
        self.assertIn("fancy guppy", body)
        self.assertIn("retired", body.lower())

    def test_a_superuser_can_allow_the_pairing_again(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_rejection_delete", kwargs={"pk": self.rejection.pk}))
        self.assertFalse(SpeciesNameRejection.objects.exists())

    def test_nobody_else_can(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(reverse("species_rejection_delete", kwargs={"pk": self.rejection.pk}))
        self.assertTrue(SpeciesNameRejection.objects.exists())


class DuplicateSpeciesTests(StandardTestCase):
    """Duplicate species rows, flagged like duplicate AuctionTOS."""

    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")

    def test_the_same_scientific_name_flags_both_rows(self):
        twin = Species.objects.create(genus="Poecilia", species="reticulata", source="admin")
        twin.refresh_from_db()
        self.assertEqual(twin.possible_duplicate, self.guppy)
        self.assertEqual(Species.objects.get(pk=self.guppy.pk).possible_duplicate_id, twin.pk)

    def test_the_same_common_name_flags_it_too(self):
        other = Species.objects.create(genus="Xiphophorus", species="hellerii", common_name="Guppy", source="admin")
        other.refresh_from_db()
        self.assertEqual(other.possible_duplicate, self.guppy)

    def test_a_strain_is_not_a_duplicate_of_its_species(self):
        strain = Species.objects.create(
            genus="Poecilia", species="reticulata", variety="Full Red", parent=self.guppy, source="admin"
        )
        strain.refresh_from_db()
        self.assertIsNone(strain.possible_duplicate)

    def test_the_imported_lists_are_not_scanned(self):
        """Imported rows are not scanned for duplicates."""
        twin = Species.objects.create(genus="Poecilia", species="reticulata", source="fishbase")
        twin.refresh_from_db()
        self.assertIsNone(twin.possible_duplicate)

    def test_a_flag_that_no_longer_holds_is_cleared(self):
        twin = Species.objects.create(genus="Poecilia", species="reticulata", source="admin")
        twin.genus = "Girardinus"
        twin.save()
        twin.refresh_from_db()
        self.assertIsNone(twin.possible_duplicate)
        self.assertIsNone(Species.objects.get(pk=self.guppy.pk).possible_duplicate)

    def test_merging_moves_everything_that_points_at_the_loser(self):
        twin = Species.objects.create(genus="Poecilia", species="reticulata", common_name="Millionfish", source="admin")
        SpeciesCommonName.objects.create(species=twin, name="Fancy guppy", source="admin")
        strain = Species.objects.create(
            genus="Poecilia", species="reticulata", variety="Cobra", parent=twin, source="admin"
        )
        Lot.objects.filter(pk=self.lot.pk).update(species=twin)
        remember("fancy guppy", twin, source="user", user=self.user)
        moved = self.guppy.merge_duplicate(twin)
        self.assertFalse(Species.objects.filter(pk=twin.pk).exists())
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).species, self.guppy)
        self.assertEqual(Species.objects.get(pk=strain.pk).parent, self.guppy)
        self.assertEqual(SpeciesSearchCache.objects.get(search_text="fancy guppy").species, self.guppy)
        self.assertEqual(moved["lots"], 1)
        names = set(self.guppy.common_names.values_list("name", flat=True))
        self.assertIn("Fancy guppy", names, "the hobby names on the losing row are the point of merging")
        self.assertIn("Millionfish", names, "its designated name is a name too")

    def test_merging_keeps_one_copy_of_a_shared_name(self):
        twin = Species.objects.create(genus="Poecilia", species="reticulata", source="admin")
        SpeciesCommonName.objects.create(species=twin, name="Guppy", source="admin")
        self.guppy.merge_duplicate(twin)
        self.assertEqual(self.guppy.common_names.filter(name_normalized="guppy").count(), 1)


class DuplicateSpeciesOnTheGapsPageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.guppy = make_species("Poecilia", "reticulata", "Guppy")
        self.twin = Species.objects.create(genus="Poecilia", species="reticulata", source="admin")
        self.admin_user.is_superuser = True
        self.admin_user.save()

    def test_the_page_lists_the_pair_once(self):
        self.client.login(username="admin_user", password="testpassword")
        body = self.client.get(reverse("species_gaps")).content.decode()
        self.assertIn("Possible duplicate species", body)
        self.assertEqual(body.count("Keep both"), 1, "the pair is flagged on both rows, listed once")
        self.assertEqual(body.count("Merge into this one"), 2, "either row can be the one that survives")

    def test_a_superuser_can_merge_them(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_merge", kwargs={"pk": self.twin.pk}), {"keep": self.guppy.pk})
        self.assertFalse(Species.objects.filter(pk=self.twin.pk).exists())
        self.assertTrue(Species.objects.filter(pk=self.guppy.pk).exists())

    def test_nobody_else_can(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(reverse("species_merge", kwargs={"pk": self.twin.pk}), {"keep": self.guppy.pk})
        self.assertTrue(Species.objects.filter(pk=self.twin.pk).exists())

    def test_a_pair_can_be_dismissed_instead(self):
        """A duplicate pair can be dismissed instead of merged."""
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_duplicate_dismiss", kwargs={"pk": self.twin.pk}))
        self.assertIsNone(Species.objects.get(pk=self.twin.pk).possible_duplicate)
        self.assertIsNone(Species.objects.get(pk=self.guppy.pk).possible_duplicate)

    def test_a_strain_and_its_parent_are_never_merged(self):
        strain = Species.objects.create(
            genus="Poecilia", species="reticulata", variety="Cobra", parent=self.guppy, source="admin"
        )
        Species.objects.filter(pk=strain.pk).update(possible_duplicate=self.guppy)
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("species_merge", kwargs={"pk": strain.pk}), {"keep": self.guppy.pk})
        self.assertTrue(Species.objects.filter(pk=strain.pk).exists())


class TheOtherNameUnderTheLotNameTests(StandardTestCase):
    """Show the name the seller didn't type under the lot name: lot page, label and AR overlay."""

    def setUp(self):
        super().setUp()
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        self.saulosi = make_species("Chindongo", "saulosi", "Saulosi cichlid")
        self.lot.auction = self.online_auction
        self.lot.species = self.saulosi
        self.lot.lot_name = "Yellow mbuna"
        self.lot.save()

    def test_a_common_name_lot_gets_the_scientific_name(self):
        self.assertEqual(self.lot.scientific_name_line, "Chindongo saulosi")
        self.assertEqual(self.lot.common_name_line, "")

    def test_a_scientific_name_lot_gets_the_common_name(self):
        self.lot.lot_name = "Chindongo saulosi F1"
        self.lot.save()
        self.assertTrue(self.lot.lot_name_says_the_species)
        self.assertEqual(self.lot.scientific_name_line, "")
        self.assertEqual(self.lot.common_name_line, "Saulosi cichlid")

    def test_the_match_is_on_whole_words(self):
        self.lot.lot_name = "Chindongo saulosii"
        self.lot.save()
        self.assertFalse(self.lot.lot_name_says_the_species)

    def test_a_strain_named_after_its_species_falls_back_to_the_parent(self):
        parent = make_species("Neocaridina", "davidi", "Cherry shrimp")
        strain = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=parent, source="aquarium"
        )
        self.lot.species = strain
        self.lot.lot_name = "Neocaridina davidi blue dream"
        self.lot.save()
        self.assertEqual(self.lot.common_name_line, "Cherry shrimp")

    def test_nothing_is_repeated_when_both_names_are_in_the_lot_name(self):
        self.lot.lot_name = "Chindongo saulosi saulosi cichlid"
        self.lot.save()
        self.assertEqual(self.lot.scientific_name_line, "")
        self.assertEqual(self.lot.common_name_line, "")

    def test_the_data_columns_are_untouched(self):
        self.lot.lot_name = "Chindongo saulosi"
        self.lot.save()
        self.assertEqual(self.lot.scientific_name, "Chindongo saulosi")

    def test_the_lot_page_prints_the_common_name(self):
        self.lot.lot_name = "Chindongo saulosi"
        self.lot.save()
        self.client.login(username="my_lot", password="testpassword")
        body = self.client.get(self.lot.lot_link).content.decode()
        self.assertIn("Saulosi cichlid", body)
        self.assertNotIn("<i>Chindongo saulosi</i>", body)

    def test_the_ar_overlay_gets_both_halves(self):
        from auctions.mobile.services import ar as ar_service

        self.lot.lot_name = "Chindongo saulosi"
        self.lot.save()
        rows = ar_service.build_lot_metadata(self.online_auction, [self.lot.pk], self.user, _FakeRequest())
        self.assertEqual(rows[0]["scientific_name"], "")
        self.assertEqual(rows[0]["common_name"], "Saulosi cichlid")


class _FakeRequest:
    def build_absolute_uri(self, url):
        return f"https://example.com{url}"


class SharedCommonNameTests(StandardTestCase):
    """FishBase hands the same synonym to two different fish on purpose."""

    def setUp(self):
        super().setUp()
        self.paleatus = make_species(
            "Corydoras", "paleatus", "Peppered corydoras", extra_names=["Peppered cory"], aquarium_use="commercial"
        )
        self.julii = make_species(
            "Corydoras", "julii", "Leopard corydoras", extra_names=["Peppered cory"], aquarium_use="commercial"
        )

    def test_the_species_really_called_that_wins(self):
        """The tie-break picks the one candidate whose own name shares a word with the query."""
        self.assertEqual(exact_matches("Peppered cory"), [self.paleatus])

    def test_a_shared_name_with_nothing_to_choose_between_them_stays_a_picklist(self):
        """The tie-break only narrows to one; a genuine tie stays a picklist."""
        both = make_species("Corydoras", "aeneus", "Peppered something", extra_names=["Peppered cory"])
        found = exact_matches("Peppered cory")
        self.assertEqual(len(found), 3)
        self.assertIn(both, found)


class NamesTheHobbyActuallyTypesTests(StandardTestCase):
    """Reported misses pinned so they can't return; four checked against the shipped CSV."""

    def _rows_by_name(self):
        from auctions import aquarium_species

        rows = {}
        for row in aquarium_species.read_rows():
            for name in row.common_names:
                rows[name.lower()] = row
        return rows

    def test_the_curated_list_carries_the_names_people_type(self):
        rows = self._rows_by_name()
        for typed, expected in (
            ("neo shrimp", "Neocaridina davidi"),
            ("neos", "Neocaridina davidi"),
            ("cherry shrimp", "Neocaridina davidi"),
            ("longfin bristlenose", "Ancistrus cirrhosus"),
            ("red wendtii", "Cryptocoryne wendtii"),
        ):
            self.assertIn(typed, rows, f"{typed!r} is what gets written on the bag")
            self.assertEqual(rows[typed].scientific_name, expected)

    def test_a_species_plus_the_kind_of_fish_it_is_still_answers(self):
        """A species plus its group name ("Saulosi cichlid") still answers."""
        saulosi = make_species("Chindongo", "saulosi", "Saulosi", source="aquarium", aquarium_use="commercial")
        make_species("Maylandia", "estherae", "Red zebra", extra_names=["Cichlid"])
        self.assertEqual(suggest_species("Saulosi cichlid", use_llm=False), ([saulosi], "search"))


class HybridSpeciesTests(StandardTestCase):
    """Hybrids carry only a trade name, no binomial. See Species.is_hybrid."""

    def setUp(self):
        super().setUp()
        self.url = reverse("species_create")
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")

    def _post(self, **overrides):
        data = {
            "scientific_name_input": "",
            "common_name": "Tibee shrimp",
            "other_names": "tibee, tibees",
            "variety": "Tibee",
            "parent": "",
            "is_hybrid": "on",
            "category": "",
            "freshwater": "on",
            "breeder_points": "on",
            "lot_name": "",
            "attach_to_lots": "",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def _tibee(self):
        return Species.objects.get(variety="Tibee")

    def test_a_hybrid_is_added_with_no_scientific_name(self):
        self._post()
        tibee = self._tibee()
        self.assertTrue(tibee.is_hybrid)
        self.assertEqual((tibee.genus, tibee.species, tibee.scientific_name), ("", "", ""))
        self.assertIsNone(tibee.parent)

    def test_it_reads_as_a_hybrid_everywhere_a_name_is_shown(self):
        """A hybrid reads as "Hybrid 'Name'" everywhere a name is shown."""
        self._post()
        self.assertEqual(self._tibee().full_scientific_name, "Hybrid 'Tibee'")
        self.assertEqual(self._tibee().label, "Hybrid 'Tibee'")

    def test_the_model_clears_a_genus_somebody_puts_on_one_anyway(self):
        """save() clears any genus put on a hybrid."""
        parent = make_species("Caridina", "cantonensis", "Bee shrimp")
        rogue = Species.objects.create(
            genus="Caridina", species="cantonensis", variety="Tangtai", parent=parent, is_hybrid=True
        )
        rogue.refresh_from_db()
        self.assertEqual((rogue.genus, rogue.species, rogue.scientific_name), ("", "", ""))
        self.assertIsNone(rogue.parent)

    def test_a_hybrid_may_not_claim_a_parent_species(self):
        parent = make_species("Caridina", "cantonensis", "Bee shrimp")
        response = self._post(parent=parent.pk)
        self.assertContains(response, "that is what makes it a hybrid")
        self.assertFalse(Species.objects.filter(variety="Tibee").exists())

    def test_a_hybrid_may_not_claim_a_scientific_name(self):
        response = self._post(scientific_name_input="Caridina cantonensis")
        self.assertContains(response, "a cross has no scientific name")
        self.assertFalse(Species.objects.filter(variety="Tibee").exists())

    def test_a_hybrid_has_to_be_called_something(self):
        response = self._post(variety="")
        self.assertContains(response, "the name the trade uses")

    def test_a_strain_with_no_parent_is_still_an_error_and_now_says_why(self):
        """A strain with no parent is an error that says why."""
        response = self._post(is_hybrid="", variety="Blue Dream")
        self.assertContains(response, "which species it is a strain of")
        self.assertContains(response, "this is a hybrid")

    def test_the_same_cross_added_twice_is_flagged_as_a_duplicate(self):
        self._post()
        first = self._tibee()
        second = Species.objects.create(variety="Tibee", is_hybrid=True, source="admin", common_name="Tibees")
        first.refresh_from_db()
        self.assertEqual(second.possible_duplicate, first)
        self.assertEqual(first.possible_duplicate, second)

    def test_two_different_crosses_are_not(self):
        self._post()
        tangtai = Species.objects.create(variety="Tangtai", is_hybrid=True, source="admin", common_name="Tangtai")
        self.assertIsNone(tangtai.possible_duplicate)

    def test_a_cross_and_a_strain_of_the_same_name_are_not_each_other(self):
        """A cross and a strain with the same name are different rows."""
        neocaridina = make_species("Neocaridina", "davidi", "Cherry shrimp")
        strain = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=neocaridina, source="aquarium"
        )
        cross = Species.objects.create(variety="Blue Dream", is_hybrid=True, source="admin")
        strain.refresh_from_db()
        self.assertIsNone(cross.possible_duplicate)
        self.assertIsNone(strain.possible_duplicate)

    def test_a_hybrid_is_never_offered_as_the_species_to_be_a_strain_of(self):
        self._post()
        parents = self.client.get(reverse("species-autocomplete"), {"q": "Tibee"}).content.decode()
        self.assertNotIn("Tibee", parents)
        everything = self.client.get(reverse("species-autocomplete"), {"q": "Tibee", "varieties": "1"})
        self.assertIn("Tibee", everything.content.decode())

    def test_the_name_the_trade_uses_finds_it(self):
        """A cross added with no common name is still reachable by its trade name."""
        self._post(common_name="", other_names="")
        tibee = self._tibee()
        self.assertEqual(suggest_species("Tibee", use_llm=False), ([tibee], "exact"))

    def test_a_hybrid_earns_breeder_points_like_anything_else(self):
        self._post()
        self.assertTrue(self._tibee().earns_breeder_points)

    def test_a_hybrid_in_the_hobby_does_not_promote_every_other_nameless_row(self):
        self._post()
        legacy = Species.objects.create(common_name="Some old hand-typed row", source="manual")
        Species.recompute_trade_ranks()
        legacy.refresh_from_db()
        self.assertEqual(legacy.trade_rank, Species.TRADE_RANK_NONE)

    def test_the_lot_page_prints_the_hybrid_line_under_the_lot_name(self):
        self._post()
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        self.lot.lot_name = "Tibee shrimp x10"
        self.lot.species = self._tibee()
        self.lot.save()
        self.assertEqual(self.lot.scientific_name_line, "Hybrid 'Tibee'")


class NamingASpeciesThatIsAlreadyThereTests(StandardTestCase):
    """Adding a common name to an existing species, for auction admins."""

    def setUp(self):
        super().setUp()
        self.url = reverse("species_name_create")
        self.yellow_lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        self.client.login(username="admin_user", password="testpassword")

    def _post(self, **overrides):
        data = {"species": self.yellow_lab.pk, "names": "yellow lab, electric yellow", "lot_name": ""}
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_somebody_who_runs_no_auction_is_turned_away(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_an_auction_admin_may_name_one(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_name_is_written_and_the_matcher_learns_it(self):
        self._post()
        self.assertEqual(
            suggest_species("yellow lab", user=self.admin_user, use_llm=False), ([self.yellow_lab], "exact")
        )

    def test_what_an_auction_admin_writes_is_theirs_until_it_is_approved(self):
        """Names an auction admin adds are scoped until approved."""
        self._post()
        for row in SpeciesCommonName.objects.filter(name__in=["yellow lab", "electric yellow"]):
            self.assertFalse(row.approved)
            self.assertEqual(row.added_by, self.admin_user)
        self.assertEqual(suggest_species("yellow lab", use_llm=False), ([], "none"))

    def test_a_superuser_writes_one_everybody_gets(self):
        User.objects.create_superuser("species_admin", "species_admin@example.com", "testpassword")
        self.client.login(username="species_admin", password="testpassword")
        self._post()
        self.assertTrue(SpeciesCommonName.objects.get(name="yellow lab").approved)

    def test_a_name_that_already_names_another_species_is_refused(self):
        make_species("Poecilia", "reticulata", "Guppy")
        response = self._post(names="guppy")
        self.assertContains(response, "already the name for")
        self.assertFalse(SpeciesCommonName.objects.filter(name="guppy", species=self.yellow_lab).exists())

    def test_naming_it_sets_the_species_on_the_lots_that_needed_it(self):
        self.online_auction.use_scientific_name = True
        self.online_auction.save()
        lot = Lot.objects.create(
            lot_name="Yellow lab",
            auction=self.online_auction,
            user=self.admin_user,
            auctiontos_seller=self.admin_online_tos,
            quantity=1,
            reserve_price=5,
        )
        self._post(lot_name="Yellow lab", attach_to_lots="on")
        lot.refresh_from_db()
        self.assertEqual(lot.species, self.yellow_lab)

    def test_it_does_not_write_to_the_cache_every_club_is_served_from(self):
        """Adding a name doesn't write the global cache."""
        self._post(lot_name="Yellow lab", attach_to_lots="on")
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text="yellow lab").exists())

    def test_it_sends_you_back_where_you_came_from(self):
        response = self._post(**{"names": "yellow lab"})
        self.assertEqual(response.status_code, 302)
        following = f"{self.url}?next=/lots/"
        response = self.client.post(following, {"species": self.yellow_lab.pk, "names": "yellow labs"})
        self.assertEqual(response["Location"], "/lots/")
