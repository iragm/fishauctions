"""Voice-driven set winners.

VOICE-1: the Vosklet implementation and cross-origin isolation are gone.
VOICE-2: the per-auction vocabulary endpoint the app biases its recognizer with.
VOICE-3: the grammar block in mobile config.
VOICE-4: the set-winners page.
VOICE-5: the log, and the page recording corrections on it.
VOICE-6: sales heard but not recorded, rate-limited.
VOICE-7: the in-app settings panel; the app stores the settings, Django stores nothing.
The server reading what was heard (``voice_interpreter``, tested in test_voice_interpreter), and a
browser listening through OpenAI.
"""

import datetime
import io
import json
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.db.models import Count
from django.test import TestCase, override_settings
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
    UserData,
    VoiceCommandLog,
    VoiceGrammar,
)
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase
from auctions.views import voice as voice_views

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


@override_settings(OPENAI_API_KEY="")
class VoicePageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        UserData.objects.filter(user=self.admin_user).update(voice_cloud_enabled=True)
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def test_web_gets_no_voice_controls_when_it_cant_listen(self):
        response = self.client.get(self.url)
        page = response.content.decode()
        self.assertNotIn('id="voice-btn"', page)
        self.assertNotIn("fishauctionsVoice", page)
        self.assertNotIn("voice_config", response.context)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_web_listens_through_openai(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn('id="voice-btn"', page)
        self.assertIn(reverse("auction_voice_cloud_session", kwargs={"slug": self.in_person_auction.slug}), page)
        self.assertIn("api.openai.com/v1/realtime/calls", page)
        self.assertNotIn("sk-test", page)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_openai_off_in_the_grammar_keeps_it_off_the_web(self):
        VoiceGrammar.objects.create(cloud_model="")
        page = self.client.get(self.url).content.decode()
        self.assertNotIn('id="voice-btn"', page)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_only_accounts_it_is_on_for_listen_through_openai(self):
        UserData.objects.filter(user=self.admin_user).update(voice_cloud_enabled=False)
        self.assertNotIn('id="voice-btn"', self.client.get(self.url).content.decode())
        config = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).context["voice_config"]
        self.assertFalse(config["cloud"], "the app still listens its own way")

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

    def test_the_server_reads_what_was_heard(self):
        """The page posts transcripts and acts on the answer; the app's own commands are ignored."""
        page = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()
        self.assertIn(reverse("auction_voice_interpret", kwargs={"slug": self.in_person_auction.slug}), page)
        self.assertNotIn("event.type === 'command'", page)
        for gone in ("voiceParse", "voiceMatchLocally", "voiceAnchorPhrases", "voice-vocabulary"):
            self.assertNotIn(gone, page)

    def test_the_page_gets_no_vocabulary(self):
        """The server reads against the live lot and bidder lists; the page has none to go stale."""
        config = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).context["voice_config"]
        self.assertNotIn("bidder_numbers", config)
        self.assertNotIn("lot_numbers", config)

    def test_first_run_help_is_on_the_page(self):
        """First-run help is on the page."""
        page = self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()
        self.assertIn("voice-first-run", page)
        self.assertIn("Bluetooth headset", page)


# ---------------------------------------------------------------------------
# The server reading what was heard
# ---------------------------------------------------------------------------


@isolated_cache("voice-interpret")
class VoiceInterpretTests(StandardTestCase):
    """The in-person auction's lot is 101-1 and its bidders include 555."""

    def setUp(self):
        super().setUp()
        self.url = reverse("auction_voice_interpret", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="admin_user", password="testpassword")

    def _post(self, heard, **form):
        body = {"heard": heard, "lot": "101-1", "winner": "", "price": ""}
        body.update(form)
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def test_a_close_comes_back_as_commands_for_the_form(self):
        reading = self._post(["going once, going twice, sold to bidder five five five for ten dollars"]).json()
        self.assertEqual(
            [(command["slot"], command["value"]) for command in reading["commands"]],
            [("bidder", "555"), ("price", "10"), ("sold", "")],
        )
        self.assertEqual(reading["carry"], [])

    def test_what_it_did_is_logged_and_the_rows_come_back(self):
        reading = self._post(["sold to bidder five five five for ten dollars"]).json()
        rows = {row.slot: row for row in VoiceCommandLog.objects.filter(auction=self.in_person_auction)}
        self.assertEqual(set(rows), {"bidder", "price", "sold"})
        self.assertEqual(rows["bidder"].chosen, "555")
        self.assertEqual(rows["sold"].chosen, "save")
        self.assertEqual(reading["commands"][0]["log_id"], rows["bidder"].pk)
        self.assertEqual(rows["bidder"].user, self.admin_user)

    def test_a_field_already_holding_the_value_is_not_logged_again(self):
        self._post(["sold to bidder five five five"], winner="555")
        self.assertFalse(VoiceCommandLog.objects.filter(slot="bidder").exists())

    def test_a_sale_still_waiting_is_not_logged_as_sold(self):
        reading = self._post(["sold for ten dollars"]).json()
        self.assertEqual(reading["note"], "Waiting for the bidder")
        self.assertIsNone(reading["carry"])
        self.assertFalse(VoiceCommandLog.objects.filter(slot="sold").exists())

    def test_a_missed_sale_is_logged_as_not_recorded(self):
        self._post(
            ["sold for ten. java moss, who'll give two, two dollars, three, four, five, sold to 555 for 5 dollars"]
        )
        row = VoiceCommandLog.objects.get(slot="")
        self.assertTrue(row.nothing_matched)
        self.assertIn("lot 101-1", row.heard)

    def test_the_kill_switch_reads_nothing(self):
        VoiceGrammar.objects.create(enabled=False)
        self.assertEqual(self._post(["sold to bidder five five five for ten dollars"]).json()["commands"], [])

    def test_not_json_is_refused(self):
        response = self.client.post(self.url, "heard=sold", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 400)

    def test_non_admin_is_refused(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self._post(["sold"]).status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self._post(["sold"]).status_code, 302)


@isolated_cache("voice-cloud")
@override_settings(OPENAI_API_KEY="sk-site-key")
class VoiceCloudSessionTests(StandardTestCase):
    """A browser's OpenAI session: the key it gets, and what OpenAI is asked for."""

    def setUp(self):
        super().setUp()
        UserData.objects.filter(user=self.admin_user).update(voice_cloud_enabled=True)
        self.url = reverse("auction_voice_cloud_session", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="admin_user", password="testpassword")

    def _answer(self, secret="ek_temporary"):
        response = mock.Mock()
        response.json.return_value = {"value": secret, "expires_at": 1}
        response.raise_for_status.return_value = None
        return response

    def test_the_page_gets_a_short_lived_key_and_never_the_sites(self):
        with mock.patch.object(voice_views.httpx, "post", return_value=self._answer()) as post:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(answer["key"], "ek_temporary")
        self.assertEqual(answer["model"], voice.CLOUD_LIVE)
        self.assertTrue(answer["commit"])
        self.assertNotIn("sk-site-key", response.content.decode())
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-site-key")

    def test_what_openai_is_asked_for(self):
        AuctionTOS.objects.filter(pk=self.in_person_tos.pk).update(bidder_number="NM")
        with mock.patch.object(voice_views.httpx, "post", return_value=self._answer()) as post:
            answer = self.client.post(self.url).json()
        session = post.call_args.kwargs["json"]["session"]
        audio = session["audio"]["input"]
        self.assertEqual(session["type"], "transcription")
        self.assertEqual(audio["noise_reduction"], {"type": "far_field"})
        self.assertIsNone(audio["turn_detection"])
        self.assertIn("NM", audio["transcription"]["keywords"])
        self.assertLessEqual(post.call_args.kwargs["json"]["expires_after"]["seconds"], 600)
        # The live model's keywords go again once connected.
        self.assertEqual(answer["update"]["type"], "session.update")
        self.assertIn("NM", answer["update"]["session"]["audio"]["input"]["transcription"]["keywords"])

    def test_a_model_that_waits_for_pauses_gets_server_turns(self):
        VoiceGrammar.objects.create(cloud_model="gpt-4o-transcribe")
        with mock.patch.object(voice_views.httpx, "post", return_value=self._answer()) as post:
            answer = self.client.post(self.url).json()
        self.assertEqual(
            post.call_args.kwargs["json"]["session"]["audio"]["input"]["turn_detection"]["type"], "server_vad"
        )
        self.assertFalse(answer["commit"])
        self.assertIsNone(answer["update"])

    def test_off_in_the_grammar(self):
        VoiceGrammar.objects.create(cloud_model="")
        with mock.patch.object(voice_views.httpx, "post") as post:
            self.assertEqual(self.client.post(self.url).status_code, 404)
        post.assert_not_called()

    @override_settings(OPENAI_API_KEY="")
    def test_off_without_a_key(self):
        self.assertEqual(self.client.post(self.url).status_code, 404)

    def test_voice_off_turns_it_off_too(self):
        VoiceGrammar.objects.create(enabled=False)
        self.assertEqual(self.client.post(self.url).status_code, 404)

    def test_off_for_this_account(self):
        UserData.objects.filter(user=self.admin_user).update(voice_cloud_enabled=False)
        with mock.patch.object(voice_views.httpx, "post") as post:
            self.assertEqual(self.client.post(self.url).status_code, 404)
        post.assert_not_called()

    def test_new_accounts_follow_the_setting_and_a_command_sets_everyone(self):
        for setting in (False, True):
            with override_settings(VOICE_CLOUD_ENABLED_FOR_USERS=setting):
                user = User.objects.create_user(f"new{setting}", f"new{setting}@example.com", "x")
            self.assertEqual(user.userdata.voice_cloud_enabled, setting)
        call_command("change_voice_cloud", "on", stdout=io.StringIO())
        self.assertFalse(UserData.objects.filter(voice_cloud_enabled=False).exists())
        call_command("change_voice_cloud", "off", stdout=io.StringIO())
        self.assertFalse(UserData.objects.filter(voice_cloud_enabled=True).exists())

    def test_a_page_stuck_reconnecting_is_stopped(self):
        with (
            mock.patch.object(voice_views, "CLOUD_SESSIONS_PER_HOUR", 2),
            mock.patch.object(voice_views.httpx, "post", return_value=self._answer()),
        ):
            codes = [self.client.post(self.url).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_openai_down(self):
        with (
            mock.patch.object(voice_views.httpx, "post", side_effect=httpx.ConnectError("down")),
            self.assertLogs("auctions.views.voice", level="ERROR"),
        ):
            self.assertEqual(self.client.post(self.url).status_code, 502)

    def test_non_admin_is_refused(self):
        self.client.login(username="no_lots", password="testpassword")
        with mock.patch.object(voice_views.httpx, "post") as post:
            self.assertEqual(self.client.post(self.url).status_code, 403)
        post.assert_not_called()

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


# ---------------------------------------------------------------------------
# VOICE-5 — the log
# ---------------------------------------------------------------------------


class VoiceCommandLogTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_voice_command_log", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="admin_user", password="testpassword")
        self.row_id = voice.log_command(
            self.admin_user, self.in_person_auction, slot="bidder", heard="bidder fifty", chosen="50", confidence=0.6
        )

    def test_a_correction_lands_on_the_row_voice_wrote(self):
        response = self.client.post(self.url, {"slot": "bidder", "id": self.row_id, "corrected_to": "15"})
        self.assertEqual(response.json()["id"], self.row_id)
        self.assertEqual(VoiceCommandLog.objects.count(), 1)
        row = VoiceCommandLog.objects.get(pk=self.row_id)
        self.assertEqual(row.corrected_to, "15")
        self.assertEqual(row.heard, "bidder fifty")
        self.assertEqual(row.chosen, "50")
        self.assertTrue(row.was_corrected)

    def test_nothing_to_correct_writes_nothing(self):
        self.assertIsNone(self.client.post(self.url, {"slot": "bidder", "corrected_to": "9"}).json()["id"])
        self.assertIsNone(self.client.post(self.url, {"id": "banana", "corrected_to": "9"}).json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 1)

    def test_garbage_confidence_does_not_lose_the_row(self):
        """Bad confidence input degrades instead of 500ing."""
        row_id = voice.log_command(
            self.admin_user, self.in_person_auction, slot="lot", heard="lot four", confidence="banana"
        )
        self.assertIsNone(VoiceCommandLog.objects.get(pk=row_id).confidence)

    def test_an_unknown_slot_is_not_logged(self):
        self.assertIsNone(voice.log_command(self.admin_user, self.in_person_auction, slot="reserve_price", heard="x"))

    def test_non_admin_cannot_write(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.post(self.url, {"id": self.row_id, "corrected_to": "9"}).status_code, 403)
        self.assertEqual(VoiceCommandLog.objects.get(pk=self.row_id).corrected_to, "")

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"id": self.row_id}).status_code, 302)

    def test_cannot_amend_someone_elses_row(self):
        row = VoiceCommandLog.objects.create(
            auction=self.in_person_auction, user=self.user, slot="bidder", heard="x", chosen="1"
        )
        response = self.client.post(self.url, {"slot": "bidder", "id": row.pk, "corrected_to": "9"})
        self.assertNotEqual(response.json()["id"], row.pk)
        row.refresh_from_db()
        self.assertEqual(row.corrected_to, "")

    def test_the_log_page_shows_this_auction(self):
        VoiceCommandLog.objects.create(auction=self.online_auction, slot="bidder", heard="somewhere else", chosen="1")
        voice.log_command(
            self.admin_user, self.in_person_auction, slot="sold", heard="sold to bidder fifty", chosen="save"
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("bidder fifty", page)
        self.assertIn("sold to bidder fifty", page)
        self.assertNotIn("somewhere else", page)

    def test_the_log_page_is_for_admins(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 403)


# ---------------------------------------------------------------------------
# VOICE-6 — sales heard but not recorded
# ---------------------------------------------------------------------------


@isolated_cache("voice-unmatched")
class VoiceUnmatchedLogTests(StandardTestCase):
    """``voice.log_unmatched``: a row with no slot. Rate-limited, since one stuck window would repeat."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def _log(self, heard, session_key="one"):
        return voice.log_unmatched(self.admin_user, self.in_person_auction, heard=heard, session_key=session_key)

    def test_records_what_was_heard(self):
        row = VoiceCommandLog.objects.get(pk=self._log("lot 101-1: sold for ten"))
        self.assertEqual(row.auction, self.in_person_auction)
        self.assertEqual(row.user, self.admin_user)
        self.assertEqual(row.heard, "lot 101-1: sold for ten")
        self.assertEqual(row.slot, "")
        self.assertTrue(row.nothing_matched)

    def test_one_word_is_not_worth_a_row(self):
        self.assertIsNone(self._log("yeah"))
        self.assertIsNone(self._log("   "))
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_rate_limited_per_session(self):
        self.assertIsNotNone(self._log("one for the money"))
        self.assertIsNone(self._log("two for the show"))
        self.assertIsNotNone(self._log("two for the show", session_key="two"))
        self.assertEqual(VoiceCommandLog.objects.count(), 2)

    def test_the_tuning_query_is_group_by_heard(self):
        for session in ("a", "b", "c"):
            self._log("bitter forty two", session_key=session)
        self._log("going once going twice", session_key="d")
        counts = VoiceCommandLog.objects.filter(slot="").values("heard").annotate(times=Count("id")).order_by("-times")
        self.assertEqual(counts[0]["heard"], "bitter forty two")
        self.assertEqual(counts[0]["times"], 3)


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


class AppVoiceSourceTests(StandardTestCase):
    """VOICE-APP: the page reads the app's phrase ids and finals, and can listen through OpenAI in the app.

    The behaviour runs in Chrome in auctions.tests_selenium.AppVoiceTests.
    """

    def setUp(self):
        super().setUp()
        UserData.objects.filter(user=self.admin_user).update(voice_cloud_enabled=True)
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def app_page(self):
        return self.client.get(self.url, HTTP_USER_AGENT=APP_UA).content.decode()

    def test_a_phrase_ends_on_its_final_and_its_id(self):
        page = self.app_page()
        self.assertIn("event.phrase_id", page)
        self.assertIn("typeof event.final === 'boolean'", page)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_listen_with_is_offered_in_the_app_when_openai_is_on(self):
        page = self.app_page()
        self.assertIn('id="voice-source"', page)
        self.assertIn('id="voice-source-openai"', page)
        self.assertIn("state.web_microphone", page)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_not_for_an_account_it_is_off_for(self):
        UserData.objects.filter(user=self.admin_user).update(voice_cloud_enabled=False)
        self.assertNotIn('id="voice-source"', self.app_page())

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_not_in_a_browser_which_has_only_openai(self):
        page = self.client.get(self.url).content.decode()
        self.assertNotIn('id="voice-source"', page)
        self.assertIn('id="voice-btn"', page)

    def test_a_refused_microphone_in_the_app_points_at_the_app(self):
        """The app has already pointed at the phone's settings; there's no site permission to allow."""
        page = self.app_page()
        self.assertIn("Allow the microphone for the app", page)
        self.assertIn("Allow it for this site", page)


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
