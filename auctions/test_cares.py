"""The CARES list: the weekly sync from aquaristspecies.net, and where a CARES species shows.

The API is never called: :class:`FakeSession` serves the pages.
"""

import csv
import datetime
import io
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import cares
from auctions.forms import ClubBapSettingsForm
from auctions.mobile.services.label_pdf import build_label_view
from auctions.models import (
    Auction,
    AuctionTOS,
    Category,
    ChunkedJobState,
    Club,
    ClubMember,
    Lot,
    PickupLocation,
    Species,
    SpeciesCommonName,
)
from auctions.tests import StandardTestCase

API = "https://cares.example.com/api/partner/cares-species/"


def record(pk, name, code="CEN", alt=""):
    return {
        "id": pk,
        "name": name,
        "alt_name": alt,
        "url": f"https://www.aquaristspecies.net/species/{pk}/",
        "last_updated": "2026-11-01T00:00:00Z",
        "cares_classification": code,
        "cares_classification_display": code,
    }


class FakeResponse:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data or {}
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeSession:
    """Answers each GET with the next response; keeps what was asked."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "headers": headers, "params": params, "redirects": allow_redirects})
        return self.responses.pop(0)


def page(results, next_url=None):
    return FakeResponse(200, {"count": len(results), "next": next_url, "previous": None, "results": results})


@override_settings(CARES_PARTNER_KEY="test-key", CARES_API_URL=API)
class CaresSyncTests(TestCase):
    def setUp(self):
        cache.delete(cares.LOCK_KEY)
        ChunkedJobState.objects.filter(name=cares.STATE_NAME).delete()
        self.category = Category.objects.create(name="Livebearers")
        self.goodeid = Species.objects.create(
            genus="Xenotoca",
            species="eiseni",
            source="fishbase",
            speccode=1,
            family="Goodeidae",
            category=self.category,
        )

    def sync(self, *responses, **kwargs):
        session = FakeSession(*responses)
        counts = cares.sync(session=session, sleep=lambda seconds: None, **kwargs)
        return counts, session

    def test_off_without_a_key(self):
        with override_settings(CARES_PARTNER_KEY=""):
            self.assertEqual(cares.sync(session=FakeSession()), {"disabled": 1})

    def test_marks_a_species_already_on_the_list(self):
        counts, session = self.sync(page([record(10, "Xenotoca eiseni", "CVU")]))
        self.goodeid.refresh_from_db()
        self.assertEqual((self.goodeid.cares_id, self.goodeid.cares_classification), (10, "CVU"))
        self.assertEqual(counts["marked"], 1)
        self.assertEqual(Species.objects.count(), 1)
        self.assertEqual(session.calls[0]["headers"]["X-Partner-Api-Key"], "test-key")

    def test_adds_a_missing_species_with_its_genus_relatives(self):
        self.sync(page([record(11, "Xenotoca doadrioi", "CCR")]))
        added = Species.objects.get(cares_id=11)
        self.assertEqual((added.genus, added.species, added.source), ("Xenotoca", "doadrioi", "cares"))
        self.assertEqual((added.family, added.category), ("Goodeidae", self.category))
        self.assertTrue(added.approved)
        self.assertEqual(added.trade_rank, Species.TRADE_RANK_SPECIES)

    def test_a_synonym_finds_the_species_instead_of_cloning_it(self):
        self.sync(page([record(12, "Allotoca eiseni", "CEN", alt="Neoophorus eiseni, Xenotoca eiseni")]))
        self.goodeid.refresh_from_db()
        self.assertEqual(self.goodeid.cares_id, 12)
        self.assertFalse(Species.objects.filter(genus="Allotoca").exists())

    def test_a_name_another_record_holds_is_not_taken(self):
        self.goodeid.cares_id = 99
        self.goodeid.cares_classification = "CVU"
        self.goodeid.save()
        counts, _session = self.sync(page([record(13, "Xenotoca eiseni", "CEN")]))
        self.goodeid.refresh_from_db()
        self.assertEqual(self.goodeid.cares_id, 99)
        # Not cloned either: a second row of a listed name is worse than an unmarked record.
        self.assertEqual(Species.objects.count(), 1)
        self.assertEqual(counts["claimed"], 1)

    def test_a_synonym_never_takes_another_records_own_name(self):
        """Either order: the record named Xenotoca eiseni gets that row, the other is added."""
        for records in (
            [record(20, "Allotoca eiseni", alt="Xenotoca eiseni"), record(21, "Xenotoca eiseni")],
            [record(21, "Xenotoca eiseni"), record(20, "Allotoca eiseni", alt="Xenotoca eiseni")],
        ):
            with self.subTest(first=records[0]["id"]):
                Species.objects.exclude(pk=self.goodeid.pk).delete()
                Species.objects.filter(pk=self.goodeid.pk).update(cares_id=None, cares_classification="")
                self.sync(page(records))
                self.goodeid.refresh_from_db()
                self.assertEqual(self.goodeid.cares_id, 21)
                self.assertEqual(Species.objects.get(cares_id=20).genus, "Allotoca")

    def test_a_second_run_finds_its_own_row_by_id(self):
        self.sync(page([record(11, "Xenotoca doadrioi", "CCR")]))
        counts, _session = self.sync(page([record(11, "Xenotoca doadrioi", "CEN")]))
        self.assertEqual(Species.objects.filter(genus="Xenotoca", species="doadrioi").count(), 1)
        self.assertEqual(Species.objects.get(cares_id=11).cares_classification, "CEN")
        self.assertEqual(counts["marked"], 1)

    def test_old_spellings_of_codes_are_folded(self):
        self.sync(page([record(10, "Xenotoca eiseni", "VULN")]))
        self.goodeid.refresh_from_db()
        self.assertEqual(self.goodeid.cares_classification, "CVU")

    def test_an_unknown_code_adds_nothing_and_changes_nothing(self):
        self.goodeid.cares_classification = "CEN"
        self.goodeid.save()
        counts, _session = self.sync(page([record(10, "Xenotoca eiseni", "ZZZ"), record(14, "Ilyodon whitei", "ZZZ")]))
        self.goodeid.refresh_from_db()
        self.assertEqual(self.goodeid.cares_classification, "CEN")
        self.assertFalse(Species.objects.filter(genus="Ilyodon").exists())
        self.assertEqual(counts["unknown_code"], 2)

    def test_trade_names_become_common_names_and_synonyms_do_not(self):
        self.sync(page([record(15, "Hypancistrus seideli", "CEN", alt="L066, L236, Hypancistrus sp. L333")]))
        added = Species.objects.get(cares_id=15)
        names = set(SpeciesCommonName.objects.filter(species=added).values_list("name", flat=True))
        self.assertEqual(names, {"L066", "L236"})

    def test_a_trade_name_another_species_answers_to_is_skipped(self):
        other = Species.objects.create(genus="Hypancistrus", species="zebra", source="fishbase", speccode=2)
        SpeciesCommonName.objects.create(species=other, name="L046", source="fishbase")
        self.sync(page([record(15, "Hypancistrus seideli", "CEN", alt="L046")]))
        self.assertFalse(SpeciesCommonName.objects.filter(species__cares_id=15).exists())

    def test_a_page_without_a_results_list_stops_the_run(self):
        self.sync(page([record(10, "Xenotoca eiseni")]))
        with self.assertRaises(cares.CaresError):
            self.sync(FakeResponse(200, {"detail": "maintenance"}))
        self.goodeid.refresh_from_db()
        self.assertEqual(self.goodeid.cares_classification, "CEN")

    def test_malformed_records_are_skipped(self):
        bad = [None, "x", {"id": True, "name": "Xenotoca doadrioi"}, {"id": "5", "name": "Xenotoca doadrioi"}]
        counts, _session = self.sync(page([*bad, record(10, "Xenotoca eiseni")]))
        self.assertEqual(counts["skipped"], 4)
        self.assertEqual(Species.objects.count(), 1)

    def test_the_key_never_goes_to_another_host_or_through_a_redirect(self):
        session = FakeSession(page([record(10, "Xenotoca eiseni")], next_url="https://elsewhere.example.com/?page=2"))
        with self.assertRaises(cares.CaresError):
            cares.sync(session=session, sleep=lambda seconds: None)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0]["redirects"])

    def test_retry_after_as_a_date_or_nonsense_waits_a_minute_and_gives_up(self):
        waits = []
        throttled = [FakeResponse(429, headers={"Retry-After": "Wed, 11 Nov 2026 04:30:00 GMT"})] * 4
        with self.assertRaises(cares.CaresError):
            cares.sync(session=FakeSession(*throttled), sleep=waits.append)
        self.assertEqual(waits, [60, 60, 60])

    def test_pages_are_followed_over_https(self):
        _counts, session = self.sync(
            page([record(10, "Xenotoca eiseni")], next_url="http://cares.example.com/api/?page=2&page_size=1000"),
            page([record(11, "Xenotoca doadrioi")]),
        )
        self.assertEqual(session.calls[1]["url"], "https://cares.example.com/api/?page=2&page_size=1000")
        self.assertIsNone(session.calls[1]["params"])
        self.assertEqual(Species.objects.exclude(cares_classification="").count(), 2)

    def test_a_throttled_page_waits_and_asks_again(self):
        waits = []
        session = FakeSession(FakeResponse(429, headers={"Retry-After": "5"}), page([record(10, "Xenotoca eiseni")]))
        cares.sync(session=session, sleep=waits.append)
        self.assertEqual(waits, [5])
        self.assertEqual(len(session.calls), 2)

    def test_a_refusal_stops_the_run_without_recording_it(self):
        with self.assertRaises(cares.CaresError):
            self.sync(FakeResponse(401))
        self.assertFalse(ChunkedJobState.objects.filter(name=cares.STATE_NAME, finished__isnull=False).exists())
        self.assertIsNone(cache.get(cares.LOCK_KEY))

    def test_first_run_is_full_and_later_ones_ask_since_the_last_start(self):
        started = timezone.now() - datetime.timedelta(days=7)
        _counts, session = self.sync(page([]), now=started)
        self.assertNotIn("since", session.calls[0]["params"])
        _counts, session = self.sync(page([]), now=started + datetime.timedelta(days=7))
        self.assertEqual(session.calls[0]["params"]["since"], started.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_a_failed_record_keeps_the_last_start(self):
        started = timezone.now() - datetime.timedelta(days=7)
        self.sync(page([]), now=started)
        with mock.patch.object(cares, "apply", side_effect=RuntimeError):
            counts, _session = self.sync(page([record(10, "Xenotoca eiseni")]))
        self.assertEqual(counts["failed"], 1)
        _counts, session = self.sync(page([]))
        self.assertEqual(session.calls[0]["params"]["since"], started.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_an_unknown_code_is_asked_for_again(self):
        started = timezone.now() - datetime.timedelta(days=7)
        self.sync(page([]), now=started)
        self.sync(page([record(10, "Xenotoca eiseni", "ZZZ")]))
        _counts, session = self.sync(page([]))
        self.assertEqual(session.calls[0]["params"]["since"], started.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_one_run_at_a_time(self):
        cache.add(cares.LOCK_KEY, "1")
        self.assertEqual(cares.sync(session=FakeSession()), {"already_running": 1})

    def test_merging_keeps_the_cares_mark(self):
        self.sync(page([record(11, "Xenotoca doadrioi", "CCR")]))
        added = Species.objects.get(cares_id=11)
        keep = Species.objects.create(genus="Xenotoca", species="doadrioi", source="fishbase", speccode=3)
        keep.merge_duplicate(added)
        keep.refresh_from_db()
        self.assertEqual((keep.cares_id, keep.cares_classification), (11, "CCR"))


class CaresSpeciesTests(TestCase):
    def test_a_cultivar_answers_for_its_parent(self):
        parent = Species.objects.create(
            genus="Xenotoca", species="doadrioi", source="cares", cares_id=7, cares_classification="CCR"
        )
        strain = Species.objects.create(genus="Xenotoca", species="doadrioi", variety="Gold", parent=parent)
        self.assertEqual((strain.cares, strain.cares_display), ("CCR", "Critically Endangered"))
        plain = Species.objects.create(genus="Xenotoca", species="eiseni")
        self.assertEqual(plain.cares, "")


class CaresLotTests(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cares_species = Species.objects.create(
            genus="Xenotoca", species="doadrioi", source="cares", cares_id=7, cares_classification="CCR"
        )
        cls.in_person_lot.species = cls.cares_species
        cls.in_person_lot.save()

    def test_lot_page_shows_the_classification(self):
        response = self.client.get(self.in_person_lot.get_absolute_url())
        self.assertContains(response, "<b>CARES:</b>", html=False)
        self.assertContains(response, "Critically Endangered")
        self.assertNotContains(response, "aquaristspecies.net")

    def test_not_shown_when_the_auction_has_species_off(self):
        self.in_person_auction.use_scientific_name = False
        self.in_person_auction.save()
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertEqual(lot.cares, "")
        response = self.client.get(lot.get_absolute_url())
        self.assertNotContains(response, "<b>CARES:</b>", html=False)

    def test_searching_cares_finds_only_cares_lots(self):
        from auctions.filters import LotAdminFilter

        other = Lot.objects.create(
            lot_name="plain lot", auction=self.in_person_auction, auctiontos_seller=self.admin_in_person_tos
        )
        found = set(LotAdminFilter.generic(None, Lot.objects.all(), "CARES"))
        self.assertIn(self.in_person_lot, found)
        self.assertNotIn(other, found)
        self.assertEqual(set(Lot.objects.filter(Lot.cares_q())), {self.in_person_lot})

    def test_searching_cares_still_finds_lots_marked_by_hand(self):
        from auctions.filters import LotAdminFilter

        self.in_person_auction.use_custom_checkbox_field = True
        self.in_person_auction.custom_checkbox_name = "CARES"
        self.in_person_auction.save()
        ticked = Lot.objects.create(
            lot_name="ticked lot",
            custom_checkbox=True,
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
        )
        named = Lot.objects.create(
            lot_name="CARES goodeids", auction=self.in_person_auction, auctiontos_seller=self.admin_in_person_tos
        )
        found = set(LotAdminFilter.generic(None, Lot.objects.all(), "cares"))
        self.assertTrue({self.in_person_lot, ticked, named} <= found)

    def test_lot_list_search_and_badge(self):
        Lot.objects.create(lot_name="plain lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_tos)
        self.client.force_login(self.user)
        response = self.client.get(reverse("allLots"), {"q": "cares", "auction": self.in_person_auction.slug})
        self.assertContains(response, self.in_person_lot.lot_name)
        self.assertNotContains(response, "plain lot")
        self.assertContains(response, ">CARES</span>")

    def test_csv_export_has_a_cares_column(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("lot_list", kwargs={"slug": self.in_person_auction.slug}))
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        column = rows[0].index("CARES priority")
        values = [row[column] for row in rows[1:]]
        self.assertIn("Critically Endangered", values)

    def test_club_api_lot_carries_cares(self):
        from auctions.serializers import ClubApiLotSerializer, SpeciesMatchSerializer

        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertEqual(ClubApiLotSerializer(lot).data["species"]["cares"], "Critically Endangered")
        self.assertEqual(SpeciesMatchSerializer(self.cares_species).data["cares"], "Critically Endangered")

    def test_app_overlay_lists_cares_where_the_label_prints_species(self):
        from auctions.mobile.services.ar import _label_fields

        self.in_person_auction.label_print_fields = "lot_name,scientific_name"
        self.in_person_auction.save()
        lot = Lot.objects.get(pk=self.in_person_lot.pk)
        self.assertEqual(_label_fields(lot), [{"label": "CARES", "value": "Critically Endangered"}])
        self.in_person_auction.label_print_fields = "lot_name"
        self.in_person_auction.save()
        self.assertEqual(_label_fields(Lot.objects.get(pk=lot.pk)), [])

    def test_command_lists_creators_using_the_checkbox_for_cares(self):
        self.in_person_auction.use_custom_checkbox_field = True
        self.in_person_auction.custom_checkbox_name = "CARES species"
        self.in_person_auction.save()
        out = io.StringIO()
        call_command("cares_checkbox_auctions", stdout=out)
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual([row["latest_auction"] for row in rows], [self.in_person_auction.title])
        self.assertEqual(rows[0]["email"], self.in_person_auction.created_by.email)

    def _label(self, lot):
        self.in_person_auction.label_print_fields = "lot_name,scientific_name"
        self.in_person_auction.save()
        request = RequestFactory().get("/")
        request.user = self.admin_user
        view = build_label_view(Lot.objects.get(pk=lot.pk), request, mark_printed=False)
        context = view.get_context_data()
        return context["labels"][0], render_to_string(view.template_name, context)

    def test_label_prints_the_scientific_name_then_cares(self):
        label, html = self._label(self.in_person_lot)
        self.assertEqual(label.species_suffix, " (CARES)")
        self.assertIn("<i>Xenotoca doadrioi</i> (CARES)", html)

    def test_label_of_a_lot_named_for_the_species_says_cares_once(self):
        self.in_person_lot.lot_name = "Xenotoca doadrioi"
        self.in_person_lot.save()
        _label, html = self._label(self.in_person_lot)
        self.assertNotIn("<i>Xenotoca doadrioi</i>", html)
        self.assertIn('<div class="small">CARES</div>', html)

    def test_label_without_the_species_field_says_nothing(self):
        self.in_person_auction.label_print_fields = "lot_name"
        self.in_person_auction.save()
        request = RequestFactory().get("/")
        request.user = self.admin_user
        view = build_label_view(Lot.objects.get(pk=self.in_person_lot.pk), request, mark_printed=False)
        html = render_to_string(view.template_name, view.get_context_data())
        self.assertNotIn("(CARES)", html)


class CaresBapTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(username="cares_seller", password="testpass", email="c@example.com")
        self.club = Club.objects.create(
            name="CARES Club", enable_breeder_award_program=True, auto_add_points=True, min_quantity=1
        )
        self.category = Category.objects.create(name="CARES Livebearers", bap_points=5)
        self.auction = Auction.objects.create(
            title="CARES Auction",
            created_by=self.user,
            club=self.club,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        ClubMember.objects.create(club=self.club, user=self.user)
        location = PickupLocation.objects.create(
            name="Hall", auction=self.auction, pickup_time=timezone.now() + datetime.timedelta(days=7)
        )
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=location)
        self.species = Species.objects.create(
            genus="Xenotoca", species="doadrioi", source="cares", cares_id=7, cares_classification="CCR"
        )

    def _lot(self, species):
        return Lot.objects.create(
            lot_name="Doadrioi",
            auction=self.auction,
            auctiontos_seller=self.tos,
            quantity=5,
            i_bred_this_fish=True,
            species_category=self.category,
            species=species,
            active=False,
            winning_price=10,
            auctiontos_winner=self.tos,
        )

    def test_bonus_is_added_for_a_cares_species(self):
        self.club.points_for_cares_species = 4
        self.club.save()
        lot = self._lot(self.species)
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, 9)

    def test_no_bonus_for_other_species_or_when_off(self):
        self.club.points_for_cares_species = 4
        self.club.save()
        other = Species.objects.create(genus="Xenotoca", species="eiseni")
        self.assertEqual(self._lot(other).default_bap_points(self.club), 5)
        self.club.points_for_cares_species = 0
        self.assertEqual(self._lot(self.species).default_bap_points(self.club), 5)

    def test_settings_form_saves_it_and_leaving_it_off_means_zero(self):
        data = {"points_per_lot": "", "min_quantity": 1, "days_between_same_name_lots": 0}
        data["days_between_same_species_lots"] = 0
        form = ClubBapSettingsForm(data={**data, "points_for_custom_checkbox": 0}, instance=self.club)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["points_for_cares_species"], 0)
        form = ClubBapSettingsForm(
            data={**data, "points_for_custom_checkbox": 0, "points_for_cares_species": 3}, instance=self.club
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.club.refresh_from_db()
        self.assertEqual(self.club.points_for_cares_species, 3)
