"""Voice-driven set winners.

VOICE-1 — the first implementation is gone: no Vosklet, no cross-origin isolation, and the
set-winners page gets its analytics/ads/CDN assets back.
VOICE-2 — the per-auction vocabulary the app matches against, and the rules that make it useful:
strings verbatim, unsold only, auction-scoped, ETagged.
VOICE-3 — the grammar block in mobile config, and its kill switch.
VOICE-4 — the page: a mic button that stays hidden until the app says voice is supported.
VOICE-5 — the tuning log, which is the thing v1 never had.
VOICE-6 — the other half of that log: the utterances that matched nothing, which is where the words
we don't know yet are, rate-limited so a room full of talking doesn't fill the table.
VOICE-7 — the settings panel, so voice is tuned during an auction on the phone in the operator's
hand. The app owns and stores those settings; Django stores nothing.
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


# ---------------------------------------------------------------------------
# VOICE-6 — the utterances that matched nothing
# ---------------------------------------------------------------------------


@isolated_cache("voice-unmatched")
class VoiceUnmatchedLogTests(StandardTestCase):
    """The rows a log of *accepted* commands can never hold.

    "Bitter" for "bidder" opens no slot, produces no command and reaches no table, so before this
    the tuning query could only ever return words that already worked. Grouping these by ``heard``
    is what turns "the word we get wrong most often" into a query.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("auction_voice_command_log", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="admin_user", password="testpassword")
        # The rate limit lives in the cache, and these tests write several rows in well under its
        # five seconds.
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
        """A command below the unsure cutoff is logged with the score it got: null means nothing
        matched at all, and the two are different findings."""
        response = self._post(heard="bitter forty two", confidence="0.31")
        row = VoiceCommandLog.objects.get(pk=response.json()["id"])
        self.assertAlmostEqual(row.confidence, 0.31)
        self.assertEqual(row.slot, "")

    def test_one_word_is_not_worth_a_row(self):
        """A continuous recognizer hears the room. One word is as likely to be someone walking past
        the phone as anything addressed to the app."""
        response = self._post(heard="yeah")
        self.assertIsNone(response.json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_silence_is_not_worth_a_row(self):
        self.assertIsNone(self._post(heard="").json()["id"])
        self.assertIsNone(self._post(heard="   ").json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_rate_limited_per_session(self):
        """Every phrase in a busy room must not become a row; the server decides, because the table
        is the thing being protected."""
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
        """The room sets the pace of unmatched utterances; the operator sets the pace of commands,
        and dropping one of those would lose the correction that pairs with it."""
        for index in range(5):
            response = self.client.post(self.url, {"slot": "bidder", "heard": f"bidder {index}", "chosen": str(index)})
            self.assertIsNotNone(response.json()["id"])
        self.assertEqual(VoiceCommandLog.objects.filter(slot="bidder").count(), 5)

    def test_an_unknown_slot_is_still_ignored(self):
        """Blank means "nothing matched". A slot we don't have is still a bug, not a finding."""
        self.assertIsNone(self._post(slot="reserve_price", heard="reserve is forty").json()["id"])
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_non_admin_cannot_write(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.post(self.url, {"heard": "who is this"}).status_code, 403)
        self.assertEqual(VoiceCommandLog.objects.count(), 0)

    def test_the_tuning_query_is_group_by_heard(self):
        """The whole point: what the auctioneer keeps saying that the grammar has never heard of."""
        for _ in range(3):
            self._post(heard="bitter forty two")
        self._post(heard="going once going twice")
        counts = VoiceCommandLog.objects.filter(slot="").values("heard").annotate(times=Count("id")).order_by("-times")
        self.assertEqual(counts[0]["heard"], "bitter forty two")
        self.assertEqual(counts[0]["times"], 3)

    def test_the_page_logs_what_matched_nothing(self):
        """No app change was needed for this -- the app already pushes every transcript to the page,
        so the page is the side that can tell "no command followed" from "a command did"."""
        page = self.client.get(
            reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug}),
            HTTP_USER_AGENT=APP_UA,
        ).content.decode()
        self.assertIn("voiceHeardTranscript", page)
        self.assertIn("voiceSettleTranscript", page)
        self.assertIn("voiceUnmatchedMinTokens", page)
        self.assertIn("voiceUnmatchedMinSeconds", page)


class VoiceLogAdminTests(StandardTestCase):
    """The admin end of VOICE-6: reaching the unmatched pile, and counting what's in it."""

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
        """Blank isn't one of the slot field's choices, so without its own filter the pile worth
        reading first would be the one pile the admin can't ask for."""
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
    """Tuning voice happens during an auction, on the phone in the operator's hand.

    The app owns and stores the settings, per device on purpose -- they describe this phone in this
    room. Django stores nothing here; the page is the whole feature.
    """

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
        """0.72 means nothing to anyone. The ends of the track ask the question the operator
        actually has, which is how often they want to retype a field."""
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
        """bias_supported is a note, not a gate: the half that works everywhere -- picking the
        smaller of two readings the recognizer already returned -- needs nothing from the platform.
        It is also false until Listen has been tapped once."""
        page = self.app_page()
        self.assertIn("bias_low_prices", page)
        self.assertIn("bias_supported", page)
        self.assertIn("voice-bias-note", page)

    def test_the_slider_sends_on_release(self):
        """Send on release, not on every input event: a drag is dozens of events and each one is a
        platform call."""
        page = self.app_page()
        self.assertIn("$(\"#voice-confident\").on('change'", page)
        self.assertNotIn("$(\"#voice-confident\").on('input'", page)

    def test_the_slider_moves_what_this_page_calls_sure(self):
        """Otherwise the operator drags the one control that matters and watches nothing change:
        the app would fill fields at the new cutoff while the amber flag here kept using the
        admin's. The site's grammar is the starting value, not the last word."""
        page = self.app_page()
        self.assertIn("voiceConfidentAt = voiceConfig.confident", page)
        self.assertIn("confidence >= voiceConfidentAt", page)
        self.assertNotIn("confidence >= voiceConfig.confident", page)

    def test_nothing_is_stored_server_side(self):
        """Settings describe this phone in this room, and syncing them to the account would fight
        an operator running two handsets."""
        self.assertNotIn("confident_at", self.client.get(self.url, HTTP_USER_AGENT=APP_UA).context["voice_config"])
