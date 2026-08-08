"""Voice-driven set winners.

VOICE-1 — the first implementation is gone: no Vosklet, no cross-origin isolation, and the
set-winners page gets its analytics/ads/CDN assets back.
VOICE-2 — the per-auction vocabulary the app matches against, and the rules that make it useful:
strings verbatim, unsold only, auction-scoped, ETagged.
VOICE-3 — the grammar block in mobile config, and its kill switch.
VOICE-4 — the page: a mic button that stays hidden until the app says voice is supported.
VOICE-5 — the tuning log, which is the thing v1 never had.
"""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions import voice
from auctions.mobile.services import voice as voice_service
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubMember,
    Lot,
    PickupLocation,
    VoiceCommandLog,
    VoiceGrammar,
)
from auctions.tests import StandardTestCase

APP_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


# ---------------------------------------------------------------------------
# VOICE-1 — v1 is gone
# ---------------------------------------------------------------------------


class VoiceV1RemovedTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def test_set_winners_page_is_no_longer_cross_origin_isolated(self):
        """COEP existed only so SharedArrayBuffer would work for the speech WASM. Nothing else on
        the site uses SharedArrayBuffer, and the isolation cost this page its ads and analytics.

        COOP is not part of this: it's set site-wide in settings so OAuth popups work, so the check
        is that this page is now no more special than any other."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Cross-Origin-Embedder-Policy", response)
        self.assertNotIn("Cross-Origin-Resource-Policy", response)
        control = self.client.get(reverse("home"))
        self.assertEqual(
            response.get("Cross-Origin-Opener-Policy"),
            control.get("Cross-Origin-Opener-Policy"),
        )

    def test_middleware_class_is_gone(self):
        from auctions import middleware

        self.assertFalse(hasattr(middleware, "CrossOriginIsolationMiddleware"))

    def test_page_loads_no_speech_wasm_and_no_v1_handlers(self):
        response = self.client.get(self.url)
        page = response.content.decode()
        for gone in (
            "Vosklet",
            "vosk-model",
            "startVoiceRecognition",
            "startWebSpeechRecognition",
            "parseSpokenNumber",
            "tryAutoSubmit",
        ):
            self.assertNotIn(gone, page, f"{gone} should have been removed with voice v1")


# ---------------------------------------------------------------------------
# VOICE-2 — the vocabulary endpoint
# ---------------------------------------------------------------------------


class VoiceVocabularyTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-voice-vocabulary", kwargs={"slug": self.in_person_auction.slug})

    def _get(self, user=None, **extra):
        headers = _bearer(user) if user else {}
        headers.update(extra)
        return self.client.get(self.url, **headers)

    def test_requires_jwt(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_web_session_is_not_enough(self):
        """Mobile endpoints deliberately refuse session auth, so a logged-in browser can't call one
        (403 rather than 401: the request is authenticated, it just isn't a JWT)."""
        self.client.login(username="admin_user", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_non_admin_gets_403(self):
        self.assertEqual(self._get(self.user_with_no_lots).status_code, 403)

    def test_unknown_auction_gets_404(self):
        url = reverse("mobile-voice-vocabulary", kwargs={"slug": "no-such-auction"})
        self.assertEqual(self.client.get(url, **_bearer(self.admin_user)).status_code, 404)

    def test_admin_gets_the_auction_settings(self):
        response = self._get(self.admin_user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["only_whole_dollar_bids"], self.in_person_auction.only_whole_dollar_bids)
        self.assertTrue(response.data["use_seller_dash_lot_numbering"])
        self.assertEqual(response.data["currency_symbol"], self.in_person_auction.currency_symbol)

    def test_lot_numbers_are_strings_kept_verbatim(self):
        """Seller-dash numbering puts the seller's bidder number into the lot number, so `BOB-1` is
        an ordinary lot number. Normalizing to digits would make it unmatchable."""
        lot = self.in_person_auction.lots_qs.filter(winning_price__isnull=True).first()
        lot.custom_lot_number = "BOB-1"
        lot.save()
        numbers = self._get(self.admin_user).data["lot_numbers"]
        self.assertIn("BOB-1", numbers)
        for number in numbers:
            self.assertIsInstance(number, str)

    def test_sold_lots_are_left_out(self):
        """A sold lot is refused by validate_lot, so offering it can only produce a rejected command
        -- and leaving it out sharpens every other match."""
        lot = Lot.objects.create(
            lot_name="already sold",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="SOLD-1",
            winning_price=5,
            auctiontos_winner=self.in_person_buyer,
            active=False,
        )
        self.assertNotIn(lot.lot_number_display, self._get(self.admin_user).data["lot_numbers"])

    def test_lots_ended_unsold_are_still_offered(self):
        """The page will happily sell a lot that ended with no winner, so voice has to be able to
        name one."""
        lot = Lot.objects.create(
            lot_name="ended unsold",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="OPEN-1",
            date_end=timezone.now(),
            active=False,
        )
        self.assertIn(lot.lot_number_display, self._get(self.admin_user).data["lot_numbers"])

    def test_banned_and_deleted_lots_are_left_out(self):
        banned = Lot.objects.create(
            lot_name="banned",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="BAN-1",
            banned=True,
        )
        deleted = Lot.objects.create(
            lot_name="deleted",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="DEL-1",
            is_deleted=True,
        )
        numbers = self._get(self.admin_user).data["lot_numbers"]
        self.assertNotIn(banned.lot_number_display, numbers)
        self.assertNotIn(deleted.lot_number_display, numbers)

    def test_bidder_numbers_come_from_this_auction_only(self):
        """A bidder number that isn't legal here is a wrong answer the matcher would produce with
        full confidence, so the online auction's bidders must not leak in."""
        numbers = self._get(self.admin_user).data["bidder_numbers"]
        self.assertIn("555", numbers)
        online_only = AuctionTOS.objects.filter(auction=self.online_auction).exclude(bidder_number="")
        for tos in online_only:
            if not AuctionTOS.objects.filter(auction=self.in_person_auction, bidder_number=tos.bidder_number).exists():
                self.assertNotIn(tos.bidder_number, numbers)

    def test_blank_and_error_bidder_numbers_are_skipped(self):
        """ "ERROR" is what AuctionTOS.save() writes when it can't generate a number: a broken row,
        not a bidder, and a word an auctioneer might well say out loud."""
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(bidder_number="ERROR")
        numbers = self._get(self.admin_user).data["bidder_numbers"]
        self.assertNotIn("ERROR", numbers)
        self.assertNotIn("", numbers)

    def test_etag_round_trip(self):
        first = self._get(self.admin_user)
        etag = first["ETag"]
        self.assertTrue(etag)
        again = self._get(self.admin_user, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)

    def test_etag_changes_when_a_bidder_is_added(self):
        """Bidders get added at the check-in desk while selling runs; a vocabulary that kept its
        ETag through that would be stale within minutes."""
        etag = self._get(self.admin_user)["ETag"]
        AuctionTOS.objects.create(
            user=self.userB,
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            bidder_number="777",
        )
        self.assertEqual(self._get(self.admin_user, HTTP_IF_NONE_MATCH=etag).status_code, 200)
        self.assertIn("777", self._get(self.admin_user).data["bidder_numbers"])


class VoiceVocabularyClubManagedTests(TestCase):
    """In club-managed auctions the source of truth for bidder numbers is ClubMember, and
    validate_winner falls back to it (creating a shadow AuctionTOS on the spot). Voice has to be
    able to fill a bidder the page would accept."""

    def setUp(self):
        now = timezone.now()
        self.creator = User.objects.create_user(username="club_creator", password="x")
        self.club = Club.objects.create(name="Test club")
        self.auction = Auction.objects.create(
            created_by=self.creator,
            title="Club managed auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=10),
            club=self.club,
        )
        self.location = PickupLocation.objects.create(
            name="loc", auction=self.auction, pickup_time=now + datetime.timedelta(days=5)
        )
        self.auction.manage_users_through_club = "all"
        self.auction.save()
        self.member = ClubMember.objects.create(club=self.club, name="Bob", bidder_number="BOB")
        self.url = reverse("mobile-voice-vocabulary", kwargs={"slug": self.auction.slug})

    def _drop_shadow_tos(self):
        """Creating a member in a club-managed auction also creates its shadow AuctionTOS. Removing
        those rows leaves ClubMember as the only place "BOB" could come from, which is what these
        tests are actually about."""
        AuctionTOS.objects.filter(auction=self.auction).delete()

    def test_club_members_are_included(self):
        self._drop_shadow_tos()
        response = self.client.get(self.url, **_bearer(self.creator))
        self.assertEqual(response.status_code, 200)
        self.assertIn("BOB", response.data["bidder_numbers"])

    def test_club_members_are_not_included_for_a_normal_auction(self):
        self._drop_shadow_tos()
        self.auction.manage_users_through_club = ""
        self.auction.save()
        response = self.client.get(self.url, **_bearer(self.creator))
        self.assertNotIn("BOB", response.data["bidder_numbers"])

    def test_deleted_members_are_skipped(self):
        self._drop_shadow_tos()
        ClubMember.objects.filter(pk=self.member.pk).update(is_deleted=True)
        response = self.client.get(self.url, **_bearer(self.creator))
        self.assertNotIn("BOB", response.data["bidder_numbers"])

    def test_duplicate_numbers_appear_once(self):
        """The shadow AuctionTOS and the ClubMember it came from carry the same number; the app
        would treat two identical entries as two bidders to disambiguate between."""
        self.assertTrue(AuctionTOS.objects.filter(auction=self.auction, bidder_number="BOB").exists())
        self.assertEqual(voice_service.bidder_numbers(self.auction).count("BOB"), 1)


# ---------------------------------------------------------------------------
# VOICE-3 — the grammar block in mobile config
# ---------------------------------------------------------------------------


class VoiceConfigBlockTests(TestCase):
    def _config(self):
        return self.client.get(reverse("mobile-config")).data

    def test_block_is_absent_until_an_admin_configures_one(self):
        """Absent means "use the grammar you shipped with", which is the state every deployment
        starts in."""
        self.assertNotIn("voice", self._config())

    def test_configured_grammar_is_served_whole(self):
        VoiceGrammar.objects.create()
        block = self._config()["voice"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["backend"], voice.BACKEND_PLATFORM)
        self.assertEqual(block["locale"], "en_US")
        self.assertEqual(block["thresholds"], {"confident": 0.85, "unsure": 0.5})
        self.assertEqual(block["weights"]["snap"], 1.0)
        self.assertIn("lot", block["anchors"])
        self.assertEqual(block["number_words"]["seventeen"], 17)
        self.assertIn(["15", "50"], block["homophones"])
        self.assertTrue(block["auto_submit_on_sold"])
        self.assertTrue(block["block_auto_submit_when_unsure"])

    def test_admin_edits_reach_the_app_without_a_release(self):
        grammar = VoiceGrammar.objects.create()
        grammar.anchors = dict(grammar.anchors, sold=["sold", "hammer", "gone"])
        grammar.save()
        self.assertIn("gone", self._config()["voice"]["anchors"]["sold"])

    def test_disabled_is_the_kill_switch(self):
        VoiceGrammar.objects.create(enabled=False)
        self.assertFalse(self._config()["voice"]["enabled"])

    def test_grammar_is_a_singleton(self):
        first = VoiceGrammar.objects.create(locale="en_US")
        VoiceGrammar.objects.create(locale="en_GB")
        self.assertEqual(VoiceGrammar.objects.count(), 1)
        self.assertEqual(VoiceGrammar.load().pk, first.pk)
        self.assertEqual(VoiceGrammar.load().locale, "en_GB")

    def test_config_stays_public(self):
        """The app reads config before sign-in; the grammar is word lists, not secrets."""
        VoiceGrammar.objects.create()
        response = self.client.get(reverse("mobile-config"))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# VOICE-4 — the page
# ---------------------------------------------------------------------------


class VoicePageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def test_web_gets_no_voice_controls(self):
        page = self.client.get(self.url).content.decode()
        self.assertNotIn('id="voice-btn"', page)
        self.assertNotIn("fishauctionsVoice", page)

    def test_web_does_not_even_build_the_config(self):
        """Nothing on the web page can use it, and this page is the busiest one in a live auction."""
        self.assertNotIn("voice_config", self.client.get(self.url).context)

    def test_app_gets_the_bridge_and_a_hidden_button(self):
        """is_mobile_app alone isn't enough to *show* the button: an app build with no voice
        handlers, or a phone with no recognizer, would get a dead control. It's revealed by
        voiceGetState(), which reports the capability rather than whether the microphone permission
        is already held -- so a first visit (permission: false) still gets a button to tap."""
        page = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()
        self.assertIn('id="voice-btn"', page)
        self.assertIn('class="btn btn-sm btn-primary ms-2 d-none"', page)
        self.assertIn("voiceGetState", page)
        self.assertIn("voiceStart", page)
        self.assertIn("voiceStop", page)
        self.assertIn("window.fishauctionsVoice", page)

    def test_app_page_carries_the_thresholds_so_green_means_the_same_thing(self):
        page = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()
        self.assertIn('id="voice-config"', page)
        self.assertIn("0.85", page)

    def test_thresholds_follow_the_admin_grammar(self):
        VoiceGrammar.objects.create(thresholds={"confident": 0.7, "unsure": 0.4})
        response = self.client.get(self.url, HTTP_USER_AGENT=APP_UA)
        self.assertEqual(response.context["voice_config"]["confident"], 0.7)
        self.assertEqual(response.context["voice_config"]["unsure"], 0.4)

    def test_kill_switch_reaches_the_page(self):
        VoiceGrammar.objects.create(enabled=False)
        response = self.client.get(self.url, HTTP_USER_AGENT=APP_UA)
        self.assertFalse(response.context["voice_config"]["enabled"])

    def test_first_run_help_is_on_the_page(self):
        """A phone in a pocket 20 ft from the podium can't hear an auctioneer and no software fixes
        that, so say it the first time rather than letting someone conclude voice is broken."""
        page = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()
        self.assertIn("voice-first-run", page)
        self.assertIn("Bluetooth headset", page)


# ---------------------------------------------------------------------------
# VOICE-5 — tuning telemetry
# ---------------------------------------------------------------------------


class VoiceCommandLogTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_voice_command_log", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="admin_user", password="testpassword")

    def test_records_an_accepted_command(self):
        response = self.client.post(
            self.url,
            {"slot": "bidder", "heard": "bidder seventeen", "chosen": "17", "confidence": "0.93"},
        )
        self.assertEqual(response.status_code, 200)
        row = VoiceCommandLog.objects.get(pk=response.json()["id"])
        self.assertEqual(row.auction, self.in_person_auction)
        self.assertEqual(row.user, self.admin_user)
        self.assertEqual(row.slot, "bidder")
        self.assertEqual(row.heard, "bidder seventeen")
        self.assertEqual(row.chosen, "17")
        self.assertAlmostEqual(row.confidence, 0.93)
        self.assertFalse(row.was_corrected)

    def test_a_correction_lands_on_the_same_row(self):
        """This is the whole point: "we heard X, filled in Y, and the operator changed it to Z" is
        one fact, and split across two rows nobody can pair it back up."""
        log_id = self.client.post(
            self.url,
            {"slot": "bidder", "heard": "bidder fifty", "chosen": "50", "confidence": "0.6"},
        ).json()["id"]
        again = self.client.post(self.url, {"slot": "bidder", "id": log_id, "corrected_to": "15"})
        self.assertEqual(again.json()["id"], log_id)
        self.assertEqual(VoiceCommandLog.objects.count(), 1)
        row = VoiceCommandLog.objects.get(pk=log_id)
        self.assertEqual(row.corrected_to, "15")
        self.assertEqual(row.heard, "bidder fifty")
        self.assertEqual(row.chosen, "50")
        self.assertTrue(row.was_corrected)

    def test_unknown_slot_is_ignored(self):
        response = self.client.post(self.url, {"slot": "reserve_price", "heard": "whatever"})
        self.assertIsNone(response.json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_garbage_confidence_does_not_lose_the_row(self):
        """Telemetry never gets to interrupt a sale, so bad input degrades instead of 500ing."""
        response = self.client.post(self.url, {"slot": "lot", "heard": "lot four", "confidence": "banana"})
        row = VoiceCommandLog.objects.get(pk=response.json()["id"])
        self.assertIsNone(row.confidence)

    def test_non_admin_cannot_write(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.post(self.url, {"slot": "lot"}).status_code, 403)
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"slot": "lot"}).status_code, 302)

    def test_cannot_amend_someone_elses_row(self):
        row = VoiceCommandLog.objects.create(
            auction=self.in_person_auction, user=self.user, slot="bidder", heard="x", chosen="1"
        )
        response = self.client.post(self.url, {"slot": "bidder", "id": row.pk, "corrected_to": "9"})
        self.assertNotEqual(response.json()["id"], row.pk)
        row.refresh_from_db()
        self.assertEqual(row.corrected_to, "")

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
