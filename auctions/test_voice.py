"""Voice-driven set winners.

VOICE-1: the Vosklet implementation and cross-origin isolation are gone.
VOICE-2: the per-auction vocabulary endpoint.
VOICE-3: the grammar block in mobile config.
VOICE-4: the set-winners page.
VOICE-5: the tuning log.
VOICE-6: logging utterances that matched nothing, rate-limited.
VOICE-7: the in-app settings panel; the app stores the settings, Django stores nothing.
"""

import datetime

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Count
from django.test import Client, TestCase
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
from auctions.test_support import isolated_cache
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
        """COEP is gone from the set-winners page; COOP is site-wide for OAuth popups."""
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
        """Session auth is refused with 403: mobile endpoints need a JWT."""
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
        """Lot numbers stay verbatim strings, since seller-dash numbers like `BOB-1` exist."""
        lot = self.in_person_auction.lots_qs.filter(winning_price__isnull=True).first()
        lot.custom_lot_number = "BOB-1"
        lot.save()
        numbers = self._get(self.admin_user).data["lot_numbers"]
        self.assertIn("BOB-1", numbers)
        for number in numbers:
            self.assertIsInstance(number, str)

    def test_sold_lots_are_left_out(self):
        """Sold lots are left out; validate_lot would refuse them."""
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
        """Lots that ended unsold are still offered."""
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
        """Bidder numbers come only from this auction."""
        numbers = self._get(self.admin_user).data["bidder_numbers"]
        self.assertIn("555", numbers)
        online_only = AuctionTOS.objects.filter(auction=self.online_auction).exclude(bidder_number="")
        for tos in online_only:
            if not AuctionTOS.objects.filter(auction=self.in_person_auction, bidder_number=tos.bidder_number).exists():
                self.assertNotIn(tos.bidder_number, numbers)

    def test_blank_and_error_bidder_numbers_are_skipped(self):
        """Blank and "ERROR" bidder numbers are skipped."""
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
        """The ETag changes when a bidder is added at check-in."""
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
    """Club-managed auctions take bidder numbers from ClubMember too, which validate_winner accepts."""

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
        """Delete the shadow AuctionTOS rows, leaving ClubMember as the only source."""
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
        """A number on both the shadow AuctionTOS and ClubMember appears once."""
        self.assertTrue(AuctionTOS.objects.filter(auction=self.auction, bidder_number="BOB").exists())
        self.assertEqual(voice_service.bidder_numbers(self.auction).count("BOB"), 1)


# ---------------------------------------------------------------------------
# VOICE-3 — the grammar block in mobile config
# ---------------------------------------------------------------------------


class VoiceConfigBlockTests(TestCase):
    def _config(self):
        return self.client.get(reverse("mobile-config")).data

    def test_the_defaults_are_served_when_nobody_has_configured_a_grammar(self):
        """The default grammar is served when none is configured, so app and page score alike."""
        block = self._config()["voice"]
        self.assertEqual(block["anchors"], voice.default_anchors())
        self.assertEqual(block["thresholds"], voice.default_thresholds())
        self.assertEqual(block["weights"], voice.default_weights())
        self.assertEqual(block["backend"], voice.BACKEND_BIASED)

    def test_configured_grammar_is_served_whole(self):
        VoiceGrammar.objects.create()
        block = self._config()["voice"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["backend"], voice.BACKEND_BIASED)
        self.assertEqual(block["locale"], "en_US")
        self.assertEqual(block["thresholds"], {"confident": 0.77, "unsure": 0.5})
        self.assertEqual(block["weights"]["match"], 1.0)
        self.assertIn("lot", block["anchors"])
        self.assertEqual(block["number_words"]["seventeen"], 17)
        self.assertIn(["15", "50"], block["homophones"])
        self.assertTrue(block["auto_submit_on_sold"])
        self.assertTrue(block["block_auto_submit_when_unsure"])
        self.assertEqual(block["commit_after_ms"], voice.DEFAULT_COMMIT_AFTER_MS)

    def test_how_long_a_value_waits_to_settle_is_a_row_edit(self):
        """VOICE-8: commit_after_ms (how long a partial must settle) is a row setting."""
        grammar = VoiceGrammar.objects.create()
        grammar.commit_after_ms = 450
        grammar.save()
        self.assertEqual(self._config()["voice"]["commit_after_ms"], 450)

    def test_zero_hands_the_app_back_to_final_results_only(self):
        """Zero restores final-results-only behaviour."""
        VoiceGrammar.objects.create(commit_after_ms=0)
        self.assertEqual(self._config()["voice"]["commit_after_ms"], 0)

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
        """Config stays public; the grammar is word lists."""
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
        """The web page doesn't build the voice config."""
        self.assertNotIn("voice_config", self.client.get(self.url).context)

    def test_app_gets_the_bridge_and_a_hidden_button(self):
        """The app gets the bridge and a hidden button, revealed by voiceGetState() capability."""
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
        self.assertIn("0.77", page)

    def test_thresholds_follow_the_admin_grammar(self):
        VoiceGrammar.objects.create(thresholds={"confident": 0.7, "unsure": 0.4})
        response = self.client.get(self.url, HTTP_USER_AGENT=APP_UA)
        self.assertEqual(response.context["voice_config"]["confident"], 0.7)
        self.assertEqual(response.context["voice_config"]["unsure"], 0.4)

    def test_kill_switch_reaches_the_page(self):
        VoiceGrammar.objects.create(enabled=False)
        response = self.client.get(self.url, HTTP_USER_AGENT=APP_UA)
        self.assertFalse(response.context["voice_config"]["enabled"])

    def test_the_page_can_match_a_transcript_on_its_own(self):
        """The page can match a transcript itself when the app sends no command."""
        page = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()
        self.assertIn("voiceMatchLocally", page)
        self.assertIn("voiceParse", page)
        self.assertIn("voiceAnchorPhrases", page)

    def test_the_page_gets_the_grammar_and_this_auctions_vocabulary(self):
        config = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).context["voice_config"]
        self.assertIn("lot", config["anchors"])
        self.assertEqual(config["number_words"]["one"], 1)
        self.assertIn("lot_numbers", config)
        self.assertIn("bidder_numbers", config)
        self.assertIn(self.in_person_buyer.bidder_number, config["bidder_numbers"])

    def test_an_admin_grammar_reaches_the_matcher_too(self):
        """An admin-configured grammar reaches the page matcher too."""
        VoiceGrammar.objects.create(anchors={"lot": ["lot", "item", "number"], "sold": ["sold"]})
        config = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).context["voice_config"]
        self.assertIn("number", config["anchors"]["lot"])

    def test_first_run_help_is_on_the_page(self):
        """First-run help is on the page."""
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
        """A correction lands on the same row as the original command."""
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
        """Bad confidence input degrades instead of 500ing."""
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


# ---------------------------------------------------------------------------
# VOICE-6 — the utterances that matched nothing
# ---------------------------------------------------------------------------


@isolated_cache("voice-unmatched")
class VoiceUnmatchedLogTests(StandardTestCase):
    """Utterances that matched nothing, grouped by ``heard`` to find unknown words."""

    def setUp(self):
        super().setUp()
        self.url = reverse("auction_voice_command_log", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="admin_user", password="testpassword")
        # The rate limit is in the cache.
        cache.clear()

    def _post(self, **data):
        response = self.client.post(self.url, data)
        cache.clear()
        return response

    def test_records_an_utterance_that_matched_nothing(self):
        response = self.client.post(self.url, {"heard": "sold to bitter forty two"})
        self.assertEqual(response.status_code, 200)
        row = VoiceCommandLog.objects.get(pk=response.json()["id"])
        self.assertEqual(row.auction, self.in_person_auction)
        self.assertEqual(row.user, self.admin_user)
        self.assertEqual(row.heard, "sold to bitter forty two")
        self.assertEqual(row.slot, "")
        self.assertEqual(row.chosen, "")
        self.assertIsNone(row.confidence)
        self.assertTrue(row.nothing_matched)

    def test_a_near_miss_keeps_its_score(self):
        """A near miss keeps its score; null means nothing matched."""
        response = self._post(heard="bitter forty two", confidence="0.31")
        row = VoiceCommandLog.objects.get(pk=response.json()["id"])
        self.assertAlmostEqual(row.confidence, 0.31)
        self.assertEqual(row.slot, "")

    def test_one_word_is_not_worth_a_row(self):
        """Single words aren't logged."""
        response = self._post(heard="yeah")
        self.assertIsNone(response.json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_silence_is_not_worth_a_row(self):
        self.assertIsNone(self._post(heard="").json()["id"])
        self.assertIsNone(self._post(heard="   ").json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_rate_limited_per_session(self):
        """Unmatched utterances are rate-limited per session."""
        first = self.client.post(self.url, {"heard": "one for the money"})
        second = self.client.post(self.url, {"heard": "two for the show"})
        self.assertIsNotNone(first.json()["id"])
        self.assertIsNone(second.json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 1)

    def test_a_second_session_is_not_held_up_by_the_first(self):
        """Two handsets are two microphones in two parts of the room, not one."""
        self.assertIsNotNone(self.client.post(self.url, {"heard": "one for the money"}).json()["id"])
        other = Client()
        other.login(username="admin_user", password="testpassword")
        self.assertIsNotNone(other.post(self.url, {"heard": "two for the show"}).json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 2)

    def test_an_accepted_command_is_never_rate_limited(self):
        """Accepted commands are never rate-limited."""
        for index in range(5):
            response = self.client.post(self.url, {"slot": "bidder", "heard": f"bidder {index}", "chosen": str(index)})
            self.assertIsNotNone(response.json()["id"])
        self.assertEqual(VoiceCommandLog.objects.filter(slot="bidder").count(), 5)

    def test_an_unknown_slot_is_still_ignored(self):
        """An unknown slot is ignored."""
        self.assertIsNone(self._post(slot="reserve_price", heard="reserve is forty").json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_non_admin_cannot_write(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.post(self.url, {"heard": "who is this"}).status_code, 403)
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_the_tuning_query_is_group_by_heard(self):
        """The tuning query groups by heard."""
        for _ in range(3):
            self._post(heard="bitter forty two")
        self._post(heard="going once going twice")
        counts = VoiceCommandLog.objects.filter(slot="").values("heard").annotate(times=Count("id")).order_by("-times")
        self.assertEqual(counts[0]["heard"], "bitter forty two")
        self.assertEqual(counts[0]["times"], 3)

    def test_the_page_logs_what_matched_nothing(self):
        """The page logs transcripts that produced no command."""
        page = self.client.get(
            reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug}),
            HTTP_USER_AGENT=APP_UA,
        ).content.decode()
        self.assertIn("voiceHeardTranscript", page)
        self.assertIn("voiceSettleTranscript", page)
        self.assertIn("voiceUnmatchedMinTokens", page)
        self.assertIn("voiceUnmatchedMinSeconds", page)


class VoiceLogAdminTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser(username="voice_admin", password="testpassword", email="va@example.com")
        self.client.login(username="voice_admin", password="testpassword")
        self.url = reverse("admin:auctions_voicecommandlog_changelist")
        VoiceCommandLog.objects.create(auction=self.in_person_auction, heard="bitter forty two")
        VoiceCommandLog.objects.create(auction=self.in_person_auction, heard="bitter forty two")
        VoiceCommandLog.objects.create(auction=self.in_person_auction, slot="bidder", heard="bidder six", chosen="6")
        VoiceCommandLog.objects.create(
            auction=self.in_person_auction, slot="bidder", heard="bidder fifty", chosen="50", corrected_to="15"
        )

    def test_nothing_matched_is_reachable(self):
        """The admin can filter to unmatched rows."""
        response = self.client.get(self.url, {"outcome": "unmatched"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.heard for row in response.context["cl"].queryset], ["bitter forty two"] * 2)

    def test_the_other_two_piles_split_the_rest(self):
        corrected = self.client.get(self.url, {"outcome": "corrected"}).context["cl"].queryset
        self.assertEqual([row.chosen for row in corrected], ["50"])
        stood = self.client.get(self.url, {"outcome": "stood"}).context["cl"].queryset
        self.assertEqual([row.chosen for row in stood], ["6"])

    def test_counting_what_was_heard_ranks_the_phrases(self):
        response = self.client.post(
            self.url,
            {
                "action": "count_what_was_heard",
                "_selected_action": [str(row.pk) for row in VoiceCommandLog.objects.all()],
            },
            follow=True,
        )
        page = response.content.decode()
        self.assertIn("2 × “bitter forty two”", page)
        self.assertIn("1 × “bidder six”", page)
        self.assertLess(page.index("bitter forty two"), page.index("bidder six"))


# ---------------------------------------------------------------------------
# VOICE-7 — the voice settings panel
# ---------------------------------------------------------------------------


class VoiceSettingsPanelTests(StandardTestCase):
    """The in-app voice settings panel; settings are per device and stored by the app."""

    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def app_page(self):
        return self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()

    def test_web_gets_no_settings_panel(self):
        page = self.client.get(self.url).content.decode()
        self.assertNotIn('id="voice-settings"', page)
        self.assertNotIn("voiceSetSettings", page)

    def test_the_panel_hangs_off_the_microphone(self):
        page = self.app_page()
        self.assertIn('id="voice-settings-btn"', page)
        self.assertIn('id="voice-settings"', page)
        self.assertIn('aria-controls="voice-settings"', page)

    def test_all_three_handlers_are_used(self):
        page = self.app_page()
        self.assertIn("voiceGetSettings", page)
        self.assertIn("voiceSetSettings", page)
        self.assertIn("voiceGetState", page)

    def test_the_slider_shows_no_number(self):
        """The confidence slider shows no number, just labelled ends."""
        page = self.app_page()
        self.assertIn('type="range"', page)
        self.assertIn("Fill it in, I'll check", page)
        self.assertIn("Only when you're sure", page)
        self.assertNotIn('id="voice-confident-value"', page)

    def test_the_slider_takes_its_bounds_from_the_app(self):
        page = self.app_page()
        self.assertIn("confident_min", page)
        self.assertIn("confident_max", page)
        self.assertIn("confident_at", page)

    def test_both_checkboxes_are_there_with_their_help_text(self):
        page = self.app_page()
        self.assertIn("Process on this phone", page)
        self.assertIn("Faster and works without a connection.", page)
        self.assertIn("Bias towards lower numbers", page)
        self.assertIn("If in doubt, guess 17 instead of 70. Only for sell prices.", page)

    def test_bias_is_rendered_whatever_the_platform_says(self):
        """Bias settings render regardless of bias_supported."""
        page = self.app_page()
        self.assertIn("bias_low_prices", page)
        self.assertIn("bias_supported", page)
        self.assertIn("voice-bias-note", page)

    def test_the_slider_sends_on_release(self):
        """The slider sends on release, not on every input event."""
        page = self.app_page()
        self.assertIn("$(\"#voice-confident\").on('change'", page)
        self.assertNotIn("$(\"#voice-confident\").on('input'", page)

    def test_the_slider_moves_what_this_page_calls_sure(self):
        """The slider also moves the page's own confidence cutoff."""
        page = self.app_page()
        self.assertIn("voiceConfidentAt = voiceConfig.confident", page)
        self.assertIn("confidence >= voiceConfidentAt", page)
        self.assertNotIn("confidence >= voiceConfig.confident", page)

    def test_nothing_is_stored_server_side(self):
        """Nothing is stored server-side."""
        self.assertNotIn("confident_at", self.client.get(self.url, HTTP_USER_AGENT=APP_UA).context["voice_config"])


class PriceAnchorCanonicalWordTests(TestCase):
    """VOICE-8: the first word of ``anchors["price"]`` is canonical.

    Both platforms format "twenty five dollars" as ``$25``, so the app substitutes this word for a
    currency symbol. Reordering the list would silently break the app.
    """

    def test_dollars_is_the_canonical_price_anchor(self):
        self.assertEqual(voice.default_anchors()["price"][0], "dollars")

    def test_the_served_grammar_keeps_it_first(self):
        grammar = VoiceGrammar.objects.create()
        self.assertEqual(grammar.anchors["price"][0], "dollars")
