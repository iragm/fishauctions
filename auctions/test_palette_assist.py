"""Tests for the command palette's natural-language assist.

Everything runs against a :class:`FakeProvider` installed with ``llm.set_provider_override`` --
no network, and every test can script exactly what the model "says", including malformed replies.

The things worth guarding here are the ones that would be expensive to get wrong: that an obvious
query never costs a model call, that nothing the model returns can widen what a user is allowed to
do, and that the execute endpoint is a real gate rather than a rubber stamp on the countdown.
"""

import datetime
import json
from io import StringIO
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import llm, palette_actions, palette_assist, palette_routes
from auctions.llm import LLMError, LLMProvider, LLMResult
from auctions.models import Auction, AuctionTOS, Club, CommandPalettePage, LLMUsage, Lot, LotImage
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

    def complete_json(self, system, messages, max_tokens=800):
        self.calls.append({"system": system, "messages": messages})
        if not self.replies:
            msg = "FakeProvider ran out of scripted replies"
            raise LLMError(msg)
        return LLMResult(data=self.replies.pop(0), model="fake-model", prompt_tokens=11, completion_tokens=7)

    @property
    def call_count(self):
        return len(self.calls)


async def drain_async_stream(response):
    """Every chunk of an async streaming response, as a list of bytes."""
    return [chunk async for chunk in response]


class AssistResponse:
    """A drained NDJSON assist response.

    The endpoint streams, so a test can't just call ``.json()`` on it. This collects every event
    and exposes the final one as ``.json()``, so assertions read the same as they did before the
    endpoint streamed, plus ``.progress`` for the narration itself.

    The body is an async generator (it has to be -- see ``CommandPaletteAssistView``), and the test
    client is synchronous, so it gets drained through ``async_to_sync`` rather than plain
    iteration.
    """

    def __init__(self, response):
        self.status_code = response.status_code
        self.raw = response
        if response.streaming:
            chunks = async_to_sync(drain_async_stream)(response) if response.is_async else response.streaming_content
            body = b"".join(chunks).decode("utf-8")
            self.events = [json.loads(line) for line in body.splitlines() if line.strip()]
        else:
            # Throttled requests (429) and the non-streaming opt-out come back as plain JSON.
            self.events = [json.loads(response.content.decode("utf-8"))]

    @property
    def progress(self):
        return [event for event in self.events if event.get("kind") == "progress"]

    @property
    def progress_messages(self):
        return [event.get("message", "") for event in self.progress]

    def json(self):
        finals = [event for event in self.events if event.get("kind") != "progress"]
        return finals[-1] if finals else {}


@override_settings(SINGLE_CLUB_MODE=False)
class PaletteAssistTestCase(StandardTestCase):
    """Shared setup: a scripted provider, an open in-person auction, and no leftover throttles."""

    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        llm.set_provider_override(self.provider)
        self._clear_throttles(self.user)
        self._clear_throttles(self.admin_user)
        self._clear_throttles(self.userB)
        # An in-person auction that is genuinely open for lot submission, so add_lot has somewhere
        # legitimate to go. StandardTestCase's auctions are mostly in the past.
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.in_person_auction.lot_submission_start_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.max_lots_per_user = None
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        # self.user created both auctions, so they are always an admin. A plain participant is
        # needed for the permission tests: in_person_buyer is user_with_no_lots' TOS (bidder 555).
        self.member = self.user_with_no_lots
        self.member.userdata.last_auction_used = self.in_person_auction
        self.member.userdata.save()
        self._clear_throttles(self.member)
        self.assertFalse(self.in_person_auction.permission_check(self.member))

    def tearDown(self):
        llm.set_provider_override(None)
        super().tearDown()

    # -- helpers ----------------------------------------------------------

    def _clear_throttles(self, user):
        cache.delete(f"palette_assist_cooldown_{user.pk}")
        cache.delete(f"palette_assist_calls_{user.pk}")

    def _script(self, *replies):
        self.provider.replies = list(replies)
        self.provider.calls = []

    def _assist(self, query, context=None, user=None, skip_throttle_reset=False, path=""):
        """POST to the assist endpoint as ``user`` (defaults to self.user).

        The endpoint streams NDJSON, so this returns an :class:`AssistResponse` that drains the
        stream. ``.json()`` still gives the final answer, which keeps every assertion below reading
        the way it did before streaming existed; ``.progress`` exposes the narration.
        """
        user = user or self.user
        if not skip_throttle_reset:
            cache.delete(f"palette_assist_cooldown_{user.pk}")
        self.client.force_login(user)
        return AssistResponse(
            self.client.post(
                reverse("command_palette_assist"),
                data=json.dumps({"q": query, "context": context or [], "path": path}),
                content_type="application/json",
            )
        )

    def _execute(self, action, params, user=None, path=""):
        user = user or self.user
        cache.delete(f"palette_assist_cooldown_{user.pk}")
        self.client.force_login(user)
        return self.client.post(
            reverse("command_palette_execute"),
            data=json.dumps({"action": action, "params": params, "path": path}),
            content_type="application/json",
        )


class HeuristicTests(PaletteAssistTestCase):
    """Obvious queries must never reach the model."""

    def test_short_query_with_a_match_skips_the_llm(self):
        self._script({"action": "go_to_page", "params": {"page": "nope"}})
        response = self._assist("This auction is in-person")
        data = response.json()
        self.assertEqual(data["kind"], "results")
        self.assertEqual(self.provider.call_count, 0, "a short query with an obvious match must not call the LLM")

    def test_command_phrasing_reaches_the_llm_even_when_short(self):
        self._script({"error": "nope"})
        self._assist("add a lot")
        self.assertEqual(self.provider.call_count, 1)

    def test_long_query_reaches_the_llm(self):
        self._script({"error": "nope"})
        self._assist("please add a lot of blue shrimp to my most recent auction for me")
        self.assertEqual(self.provider.call_count, 1)

    def test_empty_query_returns_default_results(self):
        self._script({"error": "nope"})
        response = self._assist("")
        self.assertEqual(response.json()["kind"], "results")
        self.assertEqual(self.provider.call_count, 0)


class AuthAndThrottleTests(PaletteAssistTestCase):
    """Both endpoints are login-only, and both are throttled before any model call."""

    def test_endpoints_require_login(self):
        client = Client()
        self._script({"error": "nope"})
        resp = client.post(
            reverse("command_palette_assist"), data=json.dumps({"q": "add a lot"}), content_type="application/json"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url.lower())
        resp = client.post(
            reverse("command_palette_execute"),
            data=json.dumps({"action": "add_lot", "params": {}}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.provider.call_count, 0, "an anonymous request must never reach the LLM")

    def test_rapid_second_assist_is_throttled(self):
        self._script({"error": "first"}, {"error": "second"})
        first = self._assist("add a lot of blue shrimp for someone")
        self.assertEqual(first.status_code, 200)
        calls_after_first = self.provider.call_count
        second = self._assist("add another lot of blue shrimp", skip_throttle_reset=True)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["kind"], "error")
        self.assertTrue(second.json()["message"])
        self.assertEqual(self.provider.call_count, calls_after_first, "a throttled request must not reach the provider")

    def test_execute_is_throttled_too(self):
        self._execute("add_lot", {"name": "x"})
        self.client.force_login(self.user)
        second = self.client.post(
            reverse("command_palette_execute"),
            data=json.dumps({"action": "add_lot", "params": {"name": "x"}}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 429)

    def test_sustained_call_budget(self):
        cache.set(f"palette_assist_calls_{self.user.pk}", palette_assist.WINDOW_MAX_CALLS, timeout=300)
        self._script({"error": "nope"})
        response = self._assist("add a lot of blue shrimp please")
        self.assertEqual(response.json()["kind"], "error")
        self.assertEqual(self.provider.call_count, 0, "over the window cap, no model call should happen")


class UntrustedOutputTests(PaletteAssistTestCase):
    """Whatever the model returns is input, not instruction."""

    def test_malformed_reply_is_rejected(self):
        """Four off-contract replies must never be acted on -- but must not dead-end either.

        The old behaviour here was a flat "I couldn't work out how to do that". Now the loop gives
        up and hands over to the fallback ladder, so the answer is search results, a guessed page,
        or an error, and never an action.
        """
        self._script({"nonsense": True}, {"also": "wrong"}, [], "not even a dict")
        response = self._assist("do something impossible with several words")
        self.assertIn(response.json()["kind"], {"error", "results", "clarify", "navigate"})
        self.assertEqual(Lot.objects.filter(lot_name="do something impossible").count(), 0)
        self.assertTrue(LLMUsage.objects.filter(response_kind=palette_assist.FAIL_INVALID).exists())

    def test_unknown_action_is_rejected(self):
        self._script({"action": "delete_everything", "params": {}}, {"error": "gave up"})
        response = self._assist("please delete the entire database now")
        self.assertIn(response.json()["kind"], {"error", "clarify"})
        self.assertFalse(Lot.objects.filter(is_deleted=True, lot_name="delete_everything").exists())

    def test_unknown_param_is_rejected(self):
        result = palette_actions.run_action(self._request_for(self.user), "add_lot", {"name": "x", "sudo": True})
        self.assertIn("error", result)

    def test_unknown_lookup_is_rejected(self):
        parsed = palette_assist.parse_reply({"lookup": "read_all_invoices", "params": {}})
        self.assertEqual(parsed["kind"], "invalid")

    def test_non_lookup_action_cannot_be_used_as_a_lookup(self):
        parsed = palette_assist.parse_reply({"lookup": "add_lot", "params": {}})
        self.assertEqual(parsed["kind"], "invalid")

    def test_a_page_key_in_the_action_slot_becomes_a_navigation(self):
        """gpt-5-nano's most common near-miss: a real destination, in the wrong slot.

        Discarding these is what made the palette answer "I'm not sure what you meant" to requests
        it had in fact understood. ``print_my_labels`` is a page key; ``print_labels`` is the action.
        """
        parsed = palette_assist.parse_reply({"action": "print_my_labels", "params": {"scope": "mine"}})
        self.assertEqual(parsed["kind"], "action")
        self.assertEqual(parsed["action"].name, "go_to_page")
        self.assertEqual(parsed["params"], {"page": "print_my_labels"})

    def test_a_page_key_in_the_lookup_slot_becomes_a_navigation(self):
        parsed = palette_assist.parse_reply({"lookup": "watched"})
        self.assertEqual(parsed["kind"], "action")
        self.assertEqual(parsed["action"].name, "go_to_page")
        self.assertEqual(parsed["params"], {"page": "watched"})

    def test_a_misplaced_page_key_keeps_only_parameters_go_to_page_accepts(self):
        """``run_action`` refuses parameters an action never advertised, so drop the leftovers."""
        parsed = palette_assist.parse_reply(
            {"action": "auction_tos_list", "params": {"target": "spring", "bidder_number": "14"}}
        )
        self.assertEqual(parsed["params"], {"target": "spring", "page": "auction_tos_list"})

    def test_a_name_that_is_neither_an_action_nor_a_page_is_still_rejected(self):
        parsed = palette_assist.parse_reply({"action": "delete_everything", "params": {}})
        self.assertEqual(parsed["kind"], "invalid")

    def test_an_action_named_by_its_own_key_is_understood(self):
        """``{"go_to_page": {...}}`` -- the other JSON convention for writing a call."""
        parsed = palette_assist.parse_reply({"go_to_page": {"page": "club_membership_pay", "target": None}})
        self.assertEqual(parsed["kind"], "action")
        self.assertEqual(parsed["action"].name, "go_to_page")
        # "target": null means no target, not the string "None".
        self.assertEqual(parsed["params"], {"page": "club_membership_pay"})

    def test_a_lookup_named_by_its_own_key_is_understood(self):
        parsed = palette_assist.parse_reply({"find_person": {"name": "bob"}})
        self.assertEqual(parsed["kind"], "lookup")
        self.assertEqual(parsed["action"].name, "find_person")

    def test_a_key_that_names_nothing_is_still_rejected(self):
        self.assertEqual(palette_assist.parse_reply({"drop_all_tables": {}})["kind"], "invalid")
        self.assertEqual(palette_assist.parse_reply({"a": {}, "b": {}})["kind"], "invalid")
        self.assertEqual(palette_assist.parse_reply({"go_to_page": "not an object"})["kind"], "invalid")

    def test_a_named_key_cannot_smuggle_in_parameters_an_action_never_advertised(self):
        """The envelope is what gets fixed up. Everything downstream is unchanged."""
        parsed = palette_assist.parse_reply({"add_lot": {"name": "shrimp", "sudo": True}})
        self.assertEqual(parsed["kind"], "action")
        result = palette_actions.run_action(self._request_for(self.user), "add_lot", parsed["params"])
        self.assertIn("error", result)

    def test_a_misplaced_page_key_does_not_skip_the_permission_check(self):
        """The rewrite changes which key the name arrived under, not what the user may reach."""
        self._script({"action": "admin_setup_checklist", "params": {}}, {"error": "gave up"})
        response = self._assist("open the site setup checklist for me please", user=self.member)
        self.assertNotEqual(response.json().get("kind"), "navigate")

    def _request_for(self, user):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user
        return request


class DangerTierTests(PaletteAssistTestCase):
    """safe executes now, confirm counts down, navigate goes to the page."""

    def test_safe_action_runs_during_assist(self):
        self._script({"action": "my_context", "params": {}, "summary": "Look you up"})
        response = self._assist("tell me everything about my current situation")
        data = response.json()
        self.assertEqual(data["kind"], "done")

    def test_confirm_action_returns_a_countdown_and_writes_nothing(self):
        before = Lot.objects.filter(lot_name="blue shrimp").count()
        self._script(
            {
                "action": "add_lot",
                "params": {"name": "blue shrimp", "quantity": 1},
                "summary": "Add a lot of blue shrimp",
            }
        )
        response = self._assist("add a lot of blue shrimp to my auction")
        data = response.json()
        self.assertEqual(data["kind"], "countdown")
        self.assertEqual(data["action"], "add_lot")
        self.assertEqual(data["delay_ms"], palette_assist.COUNTDOWN_MS)
        self.assertEqual(
            Lot.objects.filter(lot_name="blue shrimp").count(), before, "assist must not write; execute does"
        )

    def test_execute_actually_adds_the_lot(self):
        response = self._execute("add_lot", {"name": "blue shrimp", "quantity": 2})
        data = response.json()
        self.assertEqual(data["kind"], "done", data)
        lot = Lot.objects.filter(lot_name="blue shrimp", auction=self.in_person_auction).first()
        self.assertIsNotNone(lot)
        self.assertEqual(lot.quantity, 2)
        self.assertEqual(lot.auctiontos_seller, self.in_person_tos)

    def test_navigate_action_returns_a_url_and_does_not_act(self):
        self._script({"action": "print_labels", "params": {"scope": "mine"}, "summary": "Open labels"})
        response = self._assist("I would like to print all of my labels now")
        data = response.json()
        self.assertEqual(data["kind"], "navigate")
        self.assertIn("print-my-labels", data["url"])

    def test_execute_refuses_non_confirm_actions(self):
        response = self._execute("go_to_page", {"page": "my_invoices"})
        self.assertEqual(response.json()["kind"], "error")


class PermissionTests(PaletteAssistTestCase):
    """The model can ask for anything; the resolvers decide what actually happens."""

    def test_non_admin_cannot_add_a_lot_for_someone_else(self):
        response = self._execute("add_lot", {"name": "sneaky lot", "bidder": "555"}, user=self.member)
        data = response.json()
        self.assertEqual(data["kind"], "error")
        self.assertIn("admin", data["message"].lower())
        self.assertFalse(Lot.objects.filter(lot_name="sneaky lot").exists())

    def test_admin_can_add_a_lot_for_a_bidder(self):
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(bidder_number="555")
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()
        response = self._execute("add_lot", {"name": "admin added lot", "bidder": "555"}, user=self.admin_user)
        data = response.json()
        self.assertEqual(data["kind"], "done", data)
        lot = Lot.objects.filter(lot_name="admin added lot").first()
        self.assertIsNotNone(lot)
        self.assertEqual(lot.auctiontos_seller.bidder_number, "555")

    def test_non_admin_cannot_set_a_lot_winner(self):
        response = self._execute("set_lot_winner", {"lot": "101-1", "winner": "555", "price": "10"}, user=self.member)
        self.assertEqual(response.json()["kind"], "error")

    def test_non_admin_cannot_check_people_in(self):
        response = self._execute("check_in", {"person": "555"}, user=self.member)
        self.assertEqual(response.json()["kind"], "error")

    def test_action_on_an_auction_the_user_has_not_joined_fails(self):
        # user_who_does_not_join has no AuctionTOS anywhere.
        stranger = self.user_who_does_not_join
        self._clear_throttles(stranger)
        response = self._execute(
            "add_lot", {"name": "trespassing lot", "auction": self.in_person_auction.slug}, user=stranger
        )
        data = response.json()
        self.assertEqual(data["kind"], "error")
        self.assertFalse(Lot.objects.filter(lot_name="trespassing lot").exists())

    def test_execute_revalidates_independently_of_assist(self):
        """The countdown params from an admin's assist are worthless in someone else's hands."""
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(bidder_number="555")
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()
        self._script({"action": "add_lot", "params": {"name": "borrowed lot", "bidder": "555"}, "summary": "Add a lot"})
        assisted = self._assist("add a lot called borrowed lot for bidder 555", user=self.admin_user)
        self.assertEqual(assisted.json()["kind"], "countdown")
        params = assisted.json()["params"]
        # Replay the admin's exact countdown params as a plain participant. The countdown carries
        # no authority of its own: execute re-runs the resolver, which refuses.
        response = self._execute("add_lot", params, user=self.member)
        data = response.json()
        self.assertEqual(data["kind"], "error", data)
        self.assertIn("admin", data["message"].lower())
        self.assertFalse(Lot.objects.filter(lot_name="borrowed lot").exists())


class LookupScopeTests(PaletteAssistTestCase):
    """Read-only lookups must not become a way to enumerate people."""

    def _run(self, user, params):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user
        return palette_actions.run_action(request, "find_person", params)

    def test_participant_cannot_enumerate_the_room(self):
        AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Secret Attendee",
            bidder_number="901",
        )
        result = self._run(self.member, {"name": "Secret Attendee"})
        names = [person.get("name", "") for person in result.get("people", [])]
        self.assertNotIn("Secret Attendee", names, "a plain participant must not be able to look up other attendees")

    def test_admin_can_look_up_a_participant(self):
        AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Visible Attendee",
            bidder_number="902",
        )
        result = self._run(self.user, {"name": "Visible Attendee"})
        names = [person.get("name", "") for person in result.get("people", [])]
        self.assertIn("Visible Attendee", names)

    def test_my_context_only_describes_the_caller(self):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = self.member
        context = palette_actions.user_context(self.member)
        self.assertEqual(context["username"], self.member.username)
        self.assertFalse(context["last_auction"]["is_admin"])


class ConversationTests(PaletteAssistTestCase):
    """Lookups, clarification, and remembering what just happened."""

    def test_lookup_round_then_action(self):
        self._script(
            {"lookup": "find_person", "params": {"name": "no_lots"}},
            {"action": "my_context", "params": {}, "summary": "Here's your situation"},
        )
        response = self._assist("who is the person called no_lots and what am I working on")
        self.assertEqual(response.json()["kind"], "done")
        self.assertEqual(self.provider.call_count, 2, "the lookup result should be fed back for a second round")

    def test_clarify_is_passed_through(self):
        self._script({"clarify": "Which Bob did you mean?", "options": ["Bob Smith", "Bob Jones"]})
        response = self._assist("add a lot of blue shrimp for bob please")
        data = response.json()
        self.assertEqual(data["kind"], "clarify")
        self.assertEqual(data["message"], "Which Bob did you mean?")
        self.assertEqual(data["options"], ["Bob Smith", "Bob Jones"])

    def test_ambiguous_person_becomes_more_info_needed(self):
        AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, name="Bob Smith"
        )
        AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, name="Bob Jones"
        )
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()
        response = self._execute("add_lot", {"name": "shrimp", "bidder": "bob"}, user=self.admin_user)
        data = response.json()
        self.assertEqual(data["kind"], "clarify")
        self.assertIn("bob", data["message"].lower())

    def test_context_chaining_resolves_that_label(self):
        """ "print that label" resolves the lot from the previous exchange."""
        lot = Lot.objects.create(
            lot_name="context lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            quantity=1,
        )
        self._script({"action": "print_labels", "params": {"lot_id": lot.pk}, "summary": "Print that lot's label"})
        context = [{"query": "add a lot of blue shrimp", "result": "Added context lot", "data": {"lot_id": lot.pk}}]
        response = self._assist("print that label", context=context)
        data = response.json()
        self.assertEqual(data["kind"], "navigate")
        self.assertEqual(data["url"], reverse("single_lot_label", kwargs={"pk": lot.pk}))
        # The context really was handed to the model.
        sent = json.dumps(self.provider.calls[0]["messages"])
        self.assertIn(str(lot.pk), sent)

    def test_context_is_capped_and_sanitized(self):
        raw = [{"query": f"q{i}", "result": f"r{i}"} for i in range(20)]
        raw.append({"query": "x", "result": "y", "data": {"lot_id": 5, "evil": "drop table"}})
        cleaned = palette_assist.sanitize_context(raw)
        self.assertLessEqual(len(cleaned), palette_assist.MAX_CONTEXT_ENTRIES)
        self.assertNotIn("evil", cleaned[-1].get("data", {}))

    def test_context_rejects_junk(self):
        self.assertEqual(palette_assist.sanitize_context("nope"), [])
        self.assertEqual(palette_assist.sanitize_context([1, 2, "three"]), [])


class BusinessRuleTests(PaletteAssistTestCase):
    """The action layer must inherit the site's rules, not restate them."""

    def test_lot_submission_closed_is_reported(self):
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.save()
        response = self._execute("add_lot", {"name": "too late"}, user=self.member)
        data = response.json()
        self.assertEqual(data["kind"], "error")
        self.assertIn("submission has ended", data["message"].lower())
        self.assertFalse(Lot.objects.filter(lot_name="too late").exists())

    def test_missing_lot_name_asks_for_it(self):
        response = self._execute("add_lot", {"quantity": 1}, user=self.member)
        self.assertEqual(response.json()["kind"], "clarify")

    def test_selling_not_allowed_is_reported(self):
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(selling_allowed=False)
        response = self._execute("add_lot", {"name": "not allowed"}, user=self.member)
        self.assertEqual(response.json()["kind"], "error")
        self.assertFalse(Lot.objects.filter(lot_name="not allowed").exists())

    def test_check_in_marks_the_person_checked_in(self):
        # use_check_in_mode is a property: club-managed, with users managed through check-in.
        from auctions.models import Club

        club = Club.objects.create(name="Check In Club", abbreviation="CIC")
        self.in_person_auction.club = club
        self.in_person_auction.manage_users_through_club = "checkin"
        self.in_person_auction.save()
        self.assertTrue(self.in_person_auction.use_check_in_mode)
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()
        tos = AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Arriving Person",
            bidder_number="777",
        )
        response = self._execute("check_in", {"person": "777"}, user=self.admin_user)
        self.assertEqual(response.json()["kind"], "done", response.json())
        tos.refresh_from_db()
        self.assertIsNotNone(tos.checked_in)
        self.assertTrue(tos.bidding_allowed)


class SharedLotAddPathTests(PaletteAssistTestCase):
    """The bulk-add page and the palette action must stay on one code path.

    ``save_new_lot`` / ``recalculate_seller_invoice`` / ``lot_add_block`` were extracted out of
    ``BulkAddLots`` for the palette to reuse, and the page's save path had no test of its own, so
    this covers both sides of the split.
    """

    def _bulk_post(self, lot_name):
        """POST one lot through the real bulk-add page as its own formset."""
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.client.force_login(self.user)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        formset = page.context["formset"]
        data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": str(formset.initial_form_count()),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-lot_name": lot_name,
            "form-0-quantity": "1",
            "form-0-reserve_price": str(self.in_person_auction.minimum_bid),
            "form-0-summernote_description": "",
            "form-0-custom_field_1": "",
            "form-0-custom_dropdown": "",
        }
        return self.client.post(url, data)

    def test_bulk_add_page_still_saves_lots(self):
        response = self._bulk_post("page added lot")
        self.assertEqual(response.status_code, 302)
        lot = Lot.objects.filter(lot_name="page added lot", auction=self.in_person_auction).first()
        self.assertIsNotNone(lot, "the bulk add page must still create lots after the refactor")
        self.assertEqual(lot.auctiontos_seller, self.in_person_tos)
        self.assertEqual(lot.added_by, self.user)

    def test_page_and_palette_produce_equivalent_lots(self):
        self._bulk_post("via the page")
        self._execute("add_lot", {"name": "via the palette"})
        page_lot = Lot.objects.filter(lot_name="via the page").first()
        palette_lot = Lot.objects.filter(lot_name="via the palette").first()
        self.assertIsNotNone(page_lot)
        self.assertIsNotNone(palette_lot)
        for attribute in ("auction_id", "auctiontos_seller_id", "user_id", "added_by_id"):
            self.assertEqual(
                getattr(page_lot, attribute),
                getattr(palette_lot, attribute),
                f"{attribute} differs between the page and the palette",
            )


class UsageLoggingTests(PaletteAssistTestCase):
    """Every model call is accounted for."""

    def test_llm_usage_row_is_written(self):
        LLMUsage.objects.all().delete()
        self._script({"action": "my_context", "params": {}, "summary": "ok"})
        self._assist("tell me about my current auction situation please")
        usage = LLMUsage.objects.all()
        self.assertEqual(usage.count(), 1)
        row = usage.first()
        self.assertEqual(row.user, self.user)
        self.assertEqual(row.model, "fake-model")
        self.assertEqual(row.total_tokens, 18)
        self.assertEqual(row.action, "my_context")
        self.assertTrue(row.success)

    def test_failed_call_is_recorded_as_unsuccessful(self):
        LLMUsage.objects.all().delete()
        self._script()  # no replies -> the provider raises LLMError
        response = self._assist("do something that needs the model and several words")
        self.assertNotEqual(response.json()["kind"], "done")
        self.assertTrue(LLMUsage.objects.filter(success=False).exists())

    def test_provider_outage_is_recorded_separately_from_a_model_refusal(self):
        """These used to be indistinguishable, which made the analytics page useless for triage."""
        LLMUsage.objects.all().delete()
        self._script()  # no replies -> LLMError, i.e. the provider is unreachable
        self._assist("something the provider will never see because it is down")
        self.assertTrue(LLMUsage.objects.filter(response_kind=palette_assist.FAIL_PROVIDER).exists())

        LLMUsage.objects.all().delete()
        self._script({"error": "this site doesn't do that"})
        self._assist("please launch a rocket into orbit for me")
        self.assertTrue(LLMUsage.objects.filter(response_kind=palette_assist.FAIL_MODEL_ERROR).exists())

    def test_analytics_page_shows_usage(self):
        LLMUsage.objects.create(user=self.user, model="fake-model", total_tokens=42, response_kind="done")
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save()
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("command_palette_analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "42")


class OpenAIProviderTests(SimpleTestCase):
    """The wire format, against a stubbed endpoint.

    These guard the two things a reasoning model does differently from the chat models this
    provider was first written for: it charges hidden reasoning tokens against the completion
    budget, and it can spend all of them and reply with nothing at all.
    """

    def _provider(self, **kwargs):
        return llm.OpenAIProvider(model="gpt-5-nano", api_key="test-key", **kwargs)

    def _respond(self, status_code=200, body=None, text=""):
        """A stubbed ``httpx.Client`` that records the payload it was given."""
        sent = {}

        class Response:
            def __init__(self):
                self.status_code = status_code
                self.text = text or json.dumps(body or {})

            def json(self):
                return body or {}

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, headers=None, json=None):
                sent.update(json or {})
                return Response()

        return patch("auctions.llm.httpx.Client", Client), sent

    def _answer(self, content, finish_reason="stop"):
        return {
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "model": "gpt-5-nano",
            "usage": {"prompt_tokens": 3089, "completion_tokens": 23},
        }

    def test_reasoning_effort_is_sent(self):
        """Left off, gpt-5-nano spends 6-8s and several hundred tokens reasoning about a one-liner."""
        client, sent = self._respond(body=self._answer('{"action": "go_to_page"}'))
        with client:
            self._provider(reasoning_effort="minimal").complete_json("sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(sent["reasoning_effort"], "minimal")

    def test_reasoning_effort_is_omitted_when_blank(self):
        client, sent = self._respond(body=self._answer('{"action": "go_to_page"}'))
        with client:
            self._provider(reasoning_effort="").complete_json("sys", [{"role": "user", "content": "hi"}])
        self.assertNotIn("reasoning_effort", sent)

    def test_an_endpoint_that_rejects_a_parameter_is_retried_without_it(self):
        """LLM_BASE_URL can point at a server that has never heard of these parameters."""
        for rejected, expected_replacement in (
            ("reasoning_effort", None),
            ("max_completion_tokens", "max_tokens"),
        ):
            with self.subTest(rejected=rejected):
                attempts = self._stub_endpoint_rejecting(rejected)
                result = self._provider(reasoning_effort="minimal").complete_json("sys", [])
                self.assertEqual(result.data, {"action": "go_to_page"})
                self.assertEqual(len(attempts), 2, "expected exactly one retry")
                self.assertNotIn(rejected, attempts[1])
                if expected_replacement:
                    self.assertIn(expected_replacement, attempts[1])

    def _stub_endpoint_rejecting(self, unsupported):
        """Patch httpx so the endpoint 400s any request carrying ``unsupported``.

        Returns the list of payloads it was sent, which is what the assertions are about.
        """
        attempts = []
        answer = self._answer('{"action": "go_to_page"}')

        class Response:
            def __init__(self, payload):
                self.status_code = 400 if unsupported in payload else 200
                self.text = f"Unrecognized request argument supplied: {unsupported}"

            def json(self):
                return answer

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, headers=None, **kwargs):
                payload = kwargs["json"]
                attempts.append(dict(payload))
                return Response(payload)

        patcher = patch("auctions.llm.httpx.Client", Client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return attempts

    def test_a_reply_truncated_by_the_token_budget_is_an_error_not_an_empty_object(self):
        """The bug this exists for.

        A reasoning model that burns its whole budget thinking returns HTTP 200 with an empty
        string. That used to parse as ``{}``, which the assist loop read as "the model replied
        off-contract", so it explained the contract and asked again -- several more seconds, for
        the identical empty answer, until it ran out of rounds and told the user it wasn't sure
        what they meant.
        """
        client, _sent = self._respond(body=self._answer("", finish_reason="length"))
        with client, self.assertRaises(LLMError) as caught:
            self._provider().complete_json("sys", [{"role": "user", "content": "hi"}])
        self.assertIn("completion budget", str(caught.exception))

    def test_an_empty_reply_is_an_error(self):
        client, _sent = self._respond(body=self._answer("   "))
        with client, self.assertRaises(LLMError):
            self._provider().complete_json("sys", [{"role": "user", "content": "hi"}])

    def test_the_token_budget_leaves_room_for_reasoning(self):
        """800 was not enough: a moderately hard query spent all of it before writing any JSON."""
        self.assertGreaterEqual(llm.DEFAULT_MAX_TOKENS, 2000)


class RoundCostTests(PaletteAssistTestCase):
    """How many model calls one query is allowed to cost.

    Every round re-sends the whole ~3k-token system prompt, so rounds *are* the bill. These guard
    the two ways the loop used to spend four of them and arrive nowhere.
    """

    def test_an_off_contract_reply_is_corrected_once_and_then_given_up_on(self):
        """Four rubbish replies used to mean four full-price calls."""
        self._script(*[{"nonsense": True}] * 4)
        self._assist("something long enough to need the model and get nowhere at all")
        self.assertEqual(self.provider.call_count, palette_assist.MAX_CORRECTIONS + 1)

    def test_a_repeated_lookup_is_not_run_again(self):
        """The model asking for the same thing twice will not get a different answer."""
        same = {"lookup": "find_person", "params": {"name": "nobody at all"}}
        self._script(same, same, same)
        self._assist("who on earth is nobody at all and what did they buy")
        self.assertEqual(self.provider.call_count, 2, "the repeat should end the loop, not run again")

    def test_a_lookup_followed_by_the_action_it_enabled_still_works(self):
        """The guard is about repeats, not about lookups.

        This is the deep path that justifies a second round at all, and the only shape measured to
        need one: look a person up, then act on what came back.
        """
        self._script(
            {"lookup": "find_person", "params": {"name": "555"}},
            {"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Opening invoices"},
        )
        response = self._assist("who is bidder 555 and then take me to my invoices")
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(response.json()["kind"], "navigate")

    def test_the_round_cap_is_the_ceiling_on_what_one_query_can_cost(self):
        self._script(*[{"lookup": "find_person", "params": {"name": f"person {i}"}} for i in range(10)])
        self._assist("find me somebody, anybody, and keep looking until you do")
        self.assertLessEqual(self.provider.call_count, palette_assist.MAX_ROUNDS)


class TokenAccountingTests(PaletteAssistTestCase):
    """What the analytics page reports it costs."""

    def test_cached_prompt_tokens_are_recorded(self):
        """The system prompt is identical on every call, so most of it is a cache hit.

        Billed at a fraction of the normal input rate -- a total that ignores this reads as several
        times the real bill and makes the feature look unaffordable when it isn't.
        """

        class CachingProvider(FakeProvider):
            def complete_json(self, system, messages, max_tokens=800):
                self.calls.append({"system": system, "messages": messages})
                return LLMResult(
                    data={"action": "my_context", "params": {}, "summary": "ok"},
                    model="fake-model",
                    prompt_tokens=3100,
                    cached_prompt_tokens=2816,
                    completion_tokens=22,
                )

        LLMUsage.objects.all().delete()
        llm.set_provider_override(CachingProvider())
        self._assist("tell me about my current auction situation please")
        row = LLMUsage.objects.get()
        self.assertEqual(row.prompt_tokens, 3100)
        self.assertEqual(row.cached_prompt_tokens, 2816)

    def test_the_analytics_page_separates_cached_from_charged_tokens(self):
        LLMUsage.objects.all().delete()
        LLMUsage.objects.create(
            user=self.user, model="m", prompt_tokens=3100, cached_prompt_tokens=2816, total_tokens=3122
        )
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save()
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("command_palette_analytics"))
        self.assertEqual(response.context["llm_cached_prompt_tokens"], 2816)
        self.assertEqual(response.context["llm_uncached_prompt_tokens"], 284)
        self.assertEqual(response.context["llm_cached_percent"], 91)

    def test_the_provider_reads_cached_tokens_off_the_wire(self):
        provider = llm.OpenAIProvider(model="gpt-5-nano", api_key="k")
        result = provider._parse(
            {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "model": "gpt-5-nano",
                "usage": {
                    "prompt_tokens": 3089,
                    "completion_tokens": 26,
                    "prompt_tokens_details": {"cached_tokens": 2816},
                },
            }
        )
        self.assertEqual(result.cached_prompt_tokens, 2816)


class ShortcutMiningTests(PaletteAssistTestCase):
    """Turning repeated assistant answers into shortcuts that cost nothing.

    The economics of the whole feature: a phrase the model has answered identically N times is one
    it never has to be asked about again.
    """

    def _usage(self, query, destination, count=1, success=True):
        for _ in range(count):
            LLMUsage.objects.create(
                user=self.user,
                model="fake-model",
                query=query,
                destination=destination,
                response_kind="navigate",
                action="go_to_page",
                success=success,
            )

    def _mine(self, *args):
        out = StringIO()
        call_command("mine_palette_shortcuts", *args, stdout=out)
        return out.getvalue()

    def test_a_navigation_records_where_it_landed(self):
        """Without this the miner has no ground truth and the whole thing is guesswork."""
        LLMUsage.objects.all().delete()
        self._script({"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Opening invoices"})
        self._assist("take me to where I can see what I owe")
        self.assertEqual(LLMUsage.objects.get(action="go_to_page").destination, "my_invoices")

    def test_a_consistently_answered_phrase_becomes_a_shortcut(self):
        LLMUsage.objects.all().delete()
        self._usage("Where do I see my watched lots?", "watched", count=5)
        self._mine("--apply")
        page = CommandPalettePage.objects.get(target="route:watched")
        self.assertEqual(page.search_term, "where do i see my watched lots")

    def test_a_phrase_that_resolves_two_ways_is_left_alone(self):
        """Ambiguity means context matters, and a fixed shortcut would be wrong some of the time."""
        LLMUsage.objects.all().delete()
        self._usage("show me the invoices", "my_invoices", count=4)
        self._usage("show me the invoices", "auction_invoices", count=4)
        output = self._mine("--apply")
        self.assertFalse(CommandPalettePage.objects.filter(target__startswith="route:").exists())
        self.assertIn("resolved inconsistently", output)

    def test_an_uncommon_phrase_is_left_alone(self):
        LLMUsage.objects.all().delete()
        self._usage("some one-off thing somebody typed once", "watched", count=2)
        self._mine("--apply")
        self.assertFalse(CommandPalettePage.objects.filter(target__startswith="route:").exists())

    def test_it_reports_without_writing_unless_asked(self):
        LLMUsage.objects.all().delete()
        self._usage("where do I see my watched lots", "watched", count=5)
        output = self._mine()
        self.assertIn("watched", output)
        self.assertIn("Re-run with --apply", output)
        self.assertFalse(CommandPalettePage.objects.filter(target__startswith="route:").exists())

    def test_a_mined_shortcut_then_answers_without_any_model_call(self):
        """The point of the exercise, end to end."""
        LLMUsage.objects.all().delete()
        self._usage("where do I see my watched lots", "watched", count=5)
        self._mine("--apply")

        self._script()  # no scripted replies: touching the provider at all would raise
        response = self._assist("where do I see my watched lots")
        self.assertEqual(response.json()["kind"], "results")
        self.assertEqual(self.provider.call_count, 0, "a mined shortcut must not cost a model call")
        self.assertEqual(response.progress, [], "and must not narrate work it isn't doing")

    def test_a_shortcut_is_matched_exactly_not_fuzzily(self):
        """Exactness is what makes a long-query short-circuit safe. Fuzzy matching measured badly."""
        CommandPalettePage.objects.create(search_term="my watched lots", target="route:watched")
        request = self._request_for(self.user)
        self.assertIsNotNone(palette_assist.shortcut_match(request, "My Watched Lots!"))
        self.assertIsNone(palette_assist.shortcut_match(request, "my watched lots for the spring auction"))

    def test_a_shortcut_still_resolves_per_user_and_re_checks_permissions(self):
        """A written-down phrase is not a written-down URL: the route resolves for whoever asks."""
        CommandPalettePage.objects.create(search_term="site setup", target="route:admin_setup_checklist")
        self.assertIsNone(palette_assist.shortcut_match(self._request_for(self.member), "site setup"))

    def test_an_unknown_route_target_resolves_to_nothing(self):
        CommandPalettePage.objects.create(search_term="nowhere at all", target="route:not_a_real_route")
        self.assertIsNone(palette_assist.shortcut_match(self._request_for(self.user), "nowhere at all"))

    def _request_for(self, user):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user
        return request


class AssistDisabledTests(PaletteAssistTestCase):
    """With no provider configured the palette is exactly what it was before."""

    def test_assist_falls_back_to_search(self):
        llm.set_provider_override(None)
        with override_settings(OPENAI_API_KEY="", LLM_BASE_URL=""):
            response = self._assist("add a lot of blue shrimp for bob")
            data = response.json()
            self.assertEqual(data["kind"], "results")
            self.assertIn("groups", data)

    def test_assist_enabled_reflects_the_key(self):
        llm.set_provider_override(None)
        with override_settings(OPENAI_API_KEY=""):
            self.assertFalse(llm.assist_enabled())
        with override_settings(OPENAI_API_KEY="sk-test"):
            self.assertTrue(llm.assist_enabled())


class RegistryTests(PaletteAssistTestCase):
    """The prompt is generated from the registry, so the two can't drift apart."""

    def test_prompt_lists_every_action(self):
        prompt = palette_assist.build_system_prompt(self.user)
        for name in palette_actions.ACTIONS:
            self.assertIn(name, prompt)

    def test_every_action_has_a_valid_danger_level(self):
        valid = {
            palette_actions.DANGER_SAFE,
            palette_actions.DANGER_CONFIRM,
            palette_actions.DANGER_NAVIGATE,
        }
        for action in palette_actions.ACTIONS.values():
            self.assertIn(action.danger, valid, action.name)

    def test_lookups_are_all_safe(self):
        for action in palette_actions.ACTIONS.values():
            if action.lookup:
                self.assertEqual(action.danger, palette_actions.DANGER_SAFE, action.name)

    def test_prompt_lists_every_page_the_user_can_reach(self):
        """The catalog in the prompt is generated, so a new route teaches the model automatically."""
        prompt = palette_assist.build_system_prompt(self.user)
        for key in ("my_invoices", "print_my_labels", "auction_lot_list", "watched"):
            self.assertIn(key, prompt)

    def test_prompt_does_not_offer_site_admin_pages_to_ordinary_users(self):
        prompt = palette_assist.build_system_prompt(self.member)
        self.assertNotIn("admin_setup_checklist", prompt)


class StreamingTests(PaletteAssistTestCase):
    """Progress narration. The feature is slow; saying nothing for 20s reads as broken."""

    def test_the_endpoint_streams_ndjson(self):
        self._script({"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Opening invoices"})
        response = self._assist("take me to where I can see what I owe")
        self.assertTrue(response.raw.streaming)
        self.assertEqual(response.raw["Content-Type"], "application/x-ndjson")
        # Without this nginx buffers the whole body and the streaming does nothing at all.
        self.assertEqual(response.raw["X-Accel-Buffering"], "no")

    def test_the_response_body_is_an_async_iterator(self):
        """The one property that decides whether any of this reaches the browser.

        Handed a *sync* iterator, Django's ASGI handler runs ``sync_to_async(list)`` over the whole
        thing before writing a byte, so every progress line arrives bundled with the answer at the
        end and the user watches a motionless box for twenty seconds. Everything else in this class
        still passed while that was happening, because the events are all present in the finished
        body either way -- this is the assertion that tells the two apart.
        """
        self._script({"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Opening invoices"})
        response = self._assist("take me to where I can see what I owe")
        self.assertTrue(response.raw.is_async)

    def test_the_first_progress_line_is_written_before_the_model_is_asked(self):
        """Progress must be *emitted* early, not merely present in the finished body."""
        seen = []

        class SlowProvider(FakeProvider):
            def complete_json(self, system, messages, max_tokens=800):
                seen.append("model called")
                return super().complete_json(system, messages, max_tokens)

        self.provider = SlowProvider([{"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Go"}])
        llm.set_provider_override(self.provider)
        self.client.force_login(self.user)
        raw = self.client.post(
            reverse("command_palette_assist"),
            data=json.dumps({"q": "take me to where I can see what I owe"}),
            content_type="application/json",
        )

        async def first_chunk(response):
            chunks = response.__aiter__()
            try:
                return await chunks.__anext__()
            finally:
                await chunks.aclose()

        chunk = async_to_sync(first_chunk)(raw)
        self.assertEqual(json.loads(chunk)["kind"], "progress")
        self.assertEqual(seen, [], "the opening line should be on screen before the slow part starts")

    def test_progress_arrives_before_the_answer(self):
        self._script({"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Opening invoices"})
        response = self._assist("take me to where I can see what I owe")
        self.assertTrue(response.progress, "expected at least one progress event")
        self.assertEqual(response.events[-1]["kind"], "navigate")
        self.assertEqual(response.events[0]["kind"], "progress")

    def test_a_lookup_round_is_narrated_by_name(self):
        """ "Searching for “555”…" — the point is that it names the thing being looked up."""
        self._script(
            {"lookup": "find_person", "params": {"name": "555"}},
            {"action": "go_to_page", "params": {"page": "auction_tos_list"}, "summary": "Opening people"},
        )
        response = self._assist("who is bidder 555 in this auction anyway")
        self.assertTrue(
            any("555" in message for message in response.progress_messages),
            f"expected the lookup to be narrated with its target, got {response.progress_messages}",
        )

    def test_the_opening_line_reflects_what_was_typed(self):
        self.assertEqual(palette_assist.opening_line("add a lot of blue shrimp"), "Adding that…")
        self.assertEqual(palette_assist.opening_line("lot 12 sold to bidder 4 for 25"), "Recording that sale…")
        self.assertEqual(palette_assist.opening_line("print my labels"), "Finding the right labels…")
        self.assertEqual(palette_assist.opening_line("take me to my account"), "Finding that page…")
        self.assertEqual(palette_assist.opening_line("qwerty asdf"), "Working out what you mean…")

    def test_obvious_matches_still_answer_without_any_progress(self):
        """A query answered by search alone shouldn't grow a fake thinking animation."""
        response = self._assist("This auction is in-person")
        self.assertEqual(response.progress, [])
        self.assertEqual(response.json()["kind"], "results")

    def test_non_streaming_clients_still_get_a_plain_json_answer(self):
        self._script({"action": "go_to_page", "params": {"page": "my_invoices"}, "summary": "Opening invoices"})
        self.client.force_login(self.user)
        raw = self.client.post(
            reverse("command_palette_assist"),
            data=json.dumps({"q": "take me to where I can see what I owe", "stream": False}),
            content_type="application/json",
        )
        self.assertFalse(raw.streaming)
        self.assertEqual(json.loads(raw.content)["kind"], "navigate")


class FallbackTests(PaletteAssistTestCase):
    """What happens when the assistant can't work it out. Never a dead end."""

    def test_running_out_of_rounds_falls_back_to_search(self):
        self._script(*[{"nonsense": True}] * 4)
        response = self._assist("This auction is in-person but phrased as a long command please")
        data = response.json()
        self.assertNotEqual(data["kind"], "done")
        if data["kind"] == "results":
            self.assertIn("wasn't sure", data.get("note", ""))

    def test_a_model_error_still_shows_the_user_something(self):
        self._script({"error": "I have no idea what that means"})
        response = self._assist("show me the treasurer report for the club please")
        self.assertIn(response.json()["kind"], {"results", "navigate", "clarify"})

    def test_giving_up_is_recorded_under_its_own_kind(self):
        LLMUsage.objects.all().delete()
        self._script(*[{"nonsense": True}] * 4)
        self._assist("some entirely unmatchable phrase zzzz qqqq")
        self.assertTrue(LLMUsage.objects.filter(response_kind=palette_assist.FAIL_GAVE_UP).exists())

    def test_a_genuinely_meaningless_query_still_ends_in_an_error(self):
        """The ladder has a bottom: don't invent a destination for gibberish."""
        self._script({"error": "no"})
        response = self._assist("zzzqqq wwwxxx yyyvvv uuuttt")
        self.assertEqual(response.json()["kind"], "error")

    def test_keyword_stripping_rescues_a_wordy_query(self):
        self.assertEqual(palette_assist._keywords("can you take me to where i pay my dues"), "pay dues")

    def test_the_analytics_page_lists_what_it_could_not_answer(self):
        LLMUsage.objects.create(
            user=self.user, query="book me a flight", response_kind=palette_assist.FAIL_GAVE_UP, success=False
        )
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save()
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("command_palette_analytics"))
        self.assertContains(response, "book me a flight")


class NavigationCoverageTests(PaletteAssistTestCase):
    """go_to_page is the one skill that stands in for every page on the site."""

    def _go(self, params):
        request = self.client.request().wsgi_request
        request.user = self.user
        request.palette_page = {}
        return palette_actions.run_action(request, "go_to_page", params)

    def test_a_route_key_resolves_straight_to_a_url(self):
        result = self._go({"page": "my_invoices"})
        self.assertEqual(result["url"], reverse("my_invoices"))

    def test_free_text_still_finds_the_page(self):
        result = self._go({"page": "lots I am watching"})
        self.assertEqual(result["url"], reverse("watched"))

    def test_an_auction_page_fills_in_the_slug_itself(self):
        result = self._go({"page": "auction_lot_list"})
        self.assertEqual(result["url"], reverse("auction_lot_list", kwargs={"slug": self.in_person_auction.slug}))

    def test_a_made_up_page_key_is_refused(self):
        result = self._go({"page": "delete_the_database"})
        self.assertIn("error", result)

    def test_a_non_admin_cannot_navigate_to_an_admin_page(self):
        request = self.client.request().wsgi_request
        request.user = self.member
        request.palette_page = {}
        result = palette_actions.run_action(request, "go_to_page", {"page": "auction_tos_list"})
        self.assertIn("error", result)
        self.assertIn("admin", result["error"].lower())

    def test_a_refusal_is_not_quietly_turned_into_a_different_page(self):
        """The fallback ladder must not run after a permission refusal.

        Guessing past "you aren't an admin" would drop the user on some other page without saying
        why, which reads as the request having worked.
        """
        request = self.client.request().wsgi_request
        request.user = self.member
        request.palette_page = {}
        result = palette_actions.run_action(request, "go_to_page", {"page": "auction_tos_list"})
        self.assertIn("error", result)
        self.assertNotIn("url", result)

    def test_navigation_never_leaves_the_site(self):
        """A URL from this action is always a path we generated with reverse()."""
        for page in ("my_invoices", "watched", "account", "faq"):
            result = self._go({"page": page})
            self.assertTrue(result["url"].startswith("/"), result)


class PageAwarenessTests(PaletteAssistTestCase):
    """The auction on screen beats the stickier 'last auction used'."""

    def test_add_lot_uses_the_auction_the_user_is_looking_at(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._script(
            {
                "action": "add_lot",
                "params": {"name": "context shrimp"},
                "summary": "Add a lot of context shrimp",
            }
        )
        path = reverse("auction_lot_list", kwargs={"slug": self.in_person_auction.slug})
        response = self._assist("add a lot of context shrimp for me please", path=path)
        data = response.json()
        self.assertEqual(data["kind"], "countdown")
        self._execute("add_lot", data["params"], path=path)
        lot = Lot.objects.filter(lot_name="context shrimp").first()
        self.assertIsNotNone(lot)
        self.assertEqual(lot.auction.pk, self.in_person_auction.pk)

    def test_the_prompt_tells_the_model_what_is_on_screen(self):
        path = reverse("auction_lot_list", kwargs={"slug": self.in_person_auction.slug})
        page = palette_routes.page_context_from_path(self.user, path)
        prompt = palette_assist.build_system_prompt(self.user, page)
        self.assertIn("looking_at_right_now", prompt)
        self.assertIn(self.in_person_auction.title, prompt)

    def test_a_forged_path_cannot_reach_an_auction_the_user_is_not_in(self):
        path = reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug})
        page = palette_routes.page_context_from_path(self.user_who_does_not_join, path)
        self.assertNotIn("auction", page)


class LotNamingTests(SimpleTestCase):
    """Casing, for lots that arrive as speech or as all-lowercase typing."""

    def test_an_all_lowercase_name_is_capitalised(self):
        self.assertEqual(palette_actions.tidy_lot_name("blue shrimp"), "Blue Shrimp")

    def test_small_words_stay_small_unless_they_lead(self):
        self.assertEqual(palette_actions.tidy_lot_name("a pair of angelfish"), "A Pair of Angelfish")

    def test_catfish_codes_are_uppercased(self):
        self.assertEqual(palette_actions.tidy_lot_name("l134 pleco"), "L134 Pleco")

    def test_a_name_the_user_capitalised_is_left_exactly_alone(self):
        """Any capital at all means somebody made a decision; re-casing would undo it."""
        for name in ("Blue Shrimp", "CPD", "Corydoras sp. CW010", "pH test kit"):
            self.assertEqual(palette_actions.tidy_lot_name(name), name)

    def test_empty_input_is_harmless(self):
        self.assertEqual(palette_actions.tidy_lot_name(""), "")
        self.assertEqual(palette_actions.tidy_lot_name(None), "")


class HumanizeTests(PaletteAssistTestCase):
    """Slugs, route keys and ids are for the model. Users get names."""

    def test_an_auction_slug_becomes_its_title(self):
        text = f"I found lots in {self.in_person_auction.slug} for you."
        self.assertIn(self.in_person_auction.title, palette_assist.humanize(text))
        self.assertNotIn(self.in_person_auction.slug, palette_assist.humanize(text))

    def test_a_route_key_becomes_its_label(self):
        self.assertIn("all lots in an auction", palette_assist.humanize("Try auction_lot_list next."))

    def test_ordinary_hyphenated_english_is_left_alone(self):
        """Nothing is replaced on the strength of its shape — only real slugs and real route keys."""
        for text in ("Use check-in mode.", "That is a sign-up page.", "e-mail them", "a well-known no-show"):
            self.assertEqual(palette_assist.humanize(text), text)

    def _auction(self, title):
        return Auction.objects.create(
            created_by=self.user,
            title=title,
            is_online=False,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

    def test_a_short_two_word_slug_is_caught_too(self):
        """One hyphen is still a slug — the check is whether it exists, not how it looks."""
        auction = self._auction("Spring Sale")
        self.assertEqual(auction.slug, "spring-sale")
        self.assertEqual(palette_assist.humanize("Look in spring-sale."), "Look in Spring Sale.")

    def test_a_title_is_never_treated_as_a_regex_template(self):
        """Titles are arbitrary user text; one containing backreference syntax must survive intact."""
        auction = self._auction(r"Spring \1 Sale")
        self.assertIn(r"\1", palette_assist.humanize(f"Look in {auction.slug}."))

    def test_a_club_slug_becomes_its_name(self):
        club = Club.objects.create(name="Humanized Aquarium Society", active=True)
        self.assertIn("Humanized Aquarium Society", palette_assist.humanize(f"Try {club.slug}."))

    def test_a_model_answer_is_scrubbed_on_the_way_out(self):
        self._script({"answer": f"The rules for {self.in_person_auction.slug} say no plants."})
        data = self._assist("what are the rules about plants in this auction").json()
        self.assertEqual(data["kind"], "answer")
        self.assertNotIn(self.in_person_auction.slug, data["message"])
        self.assertIn(self.in_person_auction.title, data["message"])


class AnswerTests(PaletteAssistTestCase):
    """Questions get answered, not navigated."""

    def test_an_answer_comes_back_as_its_own_kind(self):
        self._script({"answer": "Lot submission closes tomorrow."})
        data = self._assist("when does lot submission close for this auction").json()
        self.assertEqual(data["kind"], "answer")
        self.assertEqual(data["message"], "Lot submission closes tomorrow.")

    def test_an_answer_is_recorded_as_an_answer(self):
        self._script({"answer": "Twenty five percent."})
        self._assist("how much does the club take in this auction")
        usage = LLMUsage.objects.filter(response_kind="answer").first()
        self.assertIsNotNone(usage)
        self.assertTrue(usage.success)

    def test_the_prompt_offers_the_answer_shape(self):
        prompt = palette_assist.build_system_prompt(self.user)
        self.assertIn('"answer"', prompt)
        self.assertIn("describe_auction", prompt)


class DescribeTests(PaletteAssistTestCase):
    """The read-only lookups behind an answer, and their scoping."""

    def _run(self, name, params, user=None):
        request = self.client.request().wsgi_request
        request.user = user or self.user
        request.palette_page = {}
        return palette_actions.run_action(request, name, params)

    def test_describe_auction_includes_the_rules_as_plain_text(self):
        self.in_person_auction.summernote_description = "<p>No <b>plants</b> please.</p>"
        self.in_person_auction.save()
        result = self._run("describe_auction", {"auction": self.in_person_auction.title})
        self.assertEqual(result["auction"]["rules"], "No plants please.")
        self.assertNotIn("<p>", result["auction"]["rules"])

    def test_describe_auction_explains_its_settings(self):
        """Each setting arrives with the model's own help text, so the answer can't drift."""
        result = self._run("describe_auction", {"auction": self.in_person_auction.title})
        settings_block = {row["setting"]: row for row in result["auction"]["settings"]}
        self.assertIn("minimum bid", str(settings_block.keys()).lower())
        self.assertTrue(any(row["means"] for row in result["auction"]["settings"]))

    def test_describe_auction_hides_admin_stats_from_a_participant(self):
        self.in_person_auction.make_stats_public = False
        self.in_person_auction.save()
        result = self._run("describe_auction", {"auction": self.in_person_auction.title}, user=self.member)
        self.assertNotIn("_admin", result["auction"])
        self.assertFalse(result["auction"]["you_are_an_admin"])

    def test_describe_auction_gives_admins_their_stats(self):
        result = self._run("describe_auction", {"auction": self.in_person_auction.title})
        self.assertIn("_admin", result["auction"])
        self.assertIn("checked_in", result["auction"]["_admin"])

    def test_describe_person_refuses_a_participant(self):
        """The room's names, numbers and invoices are not public to the room."""
        result = self._run("describe_person", {"name": "555"}, user=self.member)
        self.assertIn("error", result)
        self.assertIn("admin", result["error"].lower())

    def test_describe_person_answers_an_admin(self):
        result = self._run("describe_person", {"name": "555", "auction": self.in_person_auction.title})
        self.assertTrue(result["found"])
        self.assertEqual(result["person"]["bidder_number"], "555")

    def test_describe_lot_is_scoped_like_find_lot(self):
        result = self._run("describe_lot", {"lot": self.lot.lot_name}, user=self.user_who_does_not_join)
        self.assertFalse(result.get("found"))

    def test_describe_club_explains_how_points_are_awarded(self):
        club = Club.objects.create(
            name="Describable Aquarium Society",
            active=True,
            enable_breeder_award_program=True,
            points_per_lot=5,
            min_quantity=6,
        )
        result = self._run("describe_club", {"club": club.name})
        settings_block = result["club"]["points_program"]
        self.assertTrue(settings_block)
        # The point of this lookup: every setting carries an explanation of what it does, taken
        # from the model field itself, so the answer can never drift from the rule.
        self.assertTrue(any("BAP" in (row["means"] or "") for row in settings_block))
        by_name = {row["setting"]: row["value"] for row in settings_block}
        self.assertIn(5, by_name.values())

    def test_describe_club_hides_member_counts_from_a_non_admin(self):
        club = Club.objects.create(name="Private Aquarium Society", active=True)
        result = self._run("describe_club", {"club": club.name}, user=self.member)
        self.assertNotIn("_admin", result["club"])


class SearchLotsTests(PaletteAssistTestCase):
    """'find shrimp in this auction' shows the shrimp."""

    def test_it_navigates_to_a_filtered_lot_list(self):
        self._script({"action": "search_lots", "params": {"query": "shrimp"}, "summary": "Search"})
        path = reverse("auction_lot_list", kwargs={"slug": self.in_person_auction.slug})
        data = self._assist("find shrimp in this auction", path=path).json()
        self.assertEqual(data["kind"], "navigate")
        self.assertIn("q=shrimp", data["url"])
        self.assertIn(f"auction={self.in_person_auction.slug}", data["url"])

    def test_it_says_where_it_is_taking_you(self):
        self._script({"action": "search_lots", "params": {"query": "shrimp"}, "summary": ""})
        path = reverse("auction_lot_list", kwargs={"slug": self.in_person_auction.slug})
        data = self._assist("find shrimp in this auction", path=path).json()
        self.assertIn("shrimp", data["message"])
        self.assertIn(self.in_person_auction.title, data["message"])

    def test_searching_everywhere_is_not_scoped_to_an_auction(self):
        self._script({"action": "search_lots", "params": {"query": "shrimp", "everywhere": True}, "summary": ""})
        data = self._assist("find every shrimp lot on the whole site").json()
        self.assertEqual(data["kind"], "navigate")
        self.assertNotIn("auction=", data["url"])


class LotReuseTests(PaletteAssistTestCase):
    """Re-listing something you've sold before keeps its photos and description."""

    def setUp(self):
        super().setUp()
        # A lot this user listed in a different auction, with a picture and a description.
        self.previous = Lot.objects.create(
            lot_name="Blue Dream Shrimp",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            user=self.user,
            quantity=12,
            summernote_description="<p>Home bred, three months old.</p>",
            i_bred_this_fish=True,
        )
        self.previous_image = LotImage.objects.create(
            lot_number=self.previous, url="https://example.com/shrimp.jpg", is_primary=True
        )

    def _add(self, params):
        self._script({"action": "add_lot", "params": params, "summary": "Add a lot"})
        data = self._assist(f"add {params.get('name', '')}").json()
        self.assertEqual(data["kind"], "countdown", data)
        self._execute("add_lot", data["params"])
        return data

    def test_a_relisting_copies_the_description_and_the_photo(self):
        self._add({"name": "blue dream shrimp", "auction": self.in_person_auction.title})
        lot = Lot.objects.filter(auction=self.in_person_auction, lot_name__iexact="Blue Dream Shrimp").first()
        self.assertIsNotNone(lot)
        self.assertIn("Home bred", lot.summernote_description)
        self.assertTrue(lot.i_bred_this_fish)
        self.assertEqual(LotImage.objects.filter(lot_number=lot).count(), 1)

    def test_a_relisting_takes_the_old_lots_capitalisation(self):
        """The user typed it properly once already; that is better than anything we'd guess."""
        self._add({"name": "blue dream shrimp", "auction": self.in_person_auction.title})
        lot = Lot.objects.filter(auction=self.in_person_auction).order_by("-lot_number").first()
        self.assertEqual(lot.lot_name, "Blue Dream Shrimp")

    def test_what_the_user_actually_said_still_wins(self):
        self._add({"name": "blue dream shrimp", "auction": self.in_person_auction.title, "quantity": 3})
        lot = Lot.objects.filter(auction=self.in_person_auction).order_by("-lot_number").first()
        self.assertEqual(lot.quantity, 3)

    def test_a_brand_new_lot_is_capitalised_and_has_no_photos(self):
        self._add({"name": "red cherry shrimp", "auction": self.in_person_auction.title})
        lot = Lot.objects.filter(auction=self.in_person_auction, lot_name="Red Cherry Shrimp").first()
        self.assertIsNotNone(lot)
        self.assertEqual(LotImage.objects.filter(lot_number=lot).count(), 0)

    def test_someone_elses_lot_is_never_copied(self):
        """The clone rule is the one the Copy button enforces: your lots only."""
        other = Lot.objects.create(
            lot_name="Secret Shrimp",
            auction=self.online_auction,
            auctiontos_seller=self.tosB,
            user=self.userB,
            summernote_description="<p>Not yours.</p>",
        )
        LotImage.objects.create(lot_number=other, url="https://example.com/secret.jpg")
        self._add({"name": "secret shrimp", "auction": self.in_person_auction.title})
        lot = Lot.objects.filter(auction=self.in_person_auction, lot_name="Secret Shrimp").first()
        self.assertIsNotNone(lot)
        self.assertNotIn("Not yours", lot.summernote_description or "")
        self.assertEqual(LotImage.objects.filter(lot_number=lot).count(), 0)

    def test_a_partial_match_reuses_the_content_but_not_the_name(self):
        """ "add shrimp" must not come back as a lot called "Blue Dream Shrimp"."""
        self._add({"name": "dream", "auction": self.in_person_auction.title})
        lot = Lot.objects.filter(auction=self.in_person_auction).order_by("-lot_number").first()
        self.assertEqual(lot.lot_name, "Dream")
        # The photo and description are still worth having, and the summary says where from.
        self.assertEqual(LotImage.objects.filter(lot_number=lot).count(), 1)

    def test_an_ambiguous_partial_match_copies_nothing(self):
        """Two past lots contain the words, so we can't tell which photo they meant."""
        Lot.objects.create(
            lot_name="Blue Dream Shrimp Juveniles",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            user=self.user,
            summernote_description="<p>Second one.</p>",
        )
        self._add({"name": "dream", "auction": self.in_person_auction.title})
        lot = Lot.objects.filter(auction=self.in_person_auction, lot_name="Dream").first()
        self.assertIsNotNone(lot)
        self.assertEqual(LotImage.objects.filter(lot_number=lot).count(), 0)

    def test_the_user_is_told_the_lot_was_reused(self):
        request = self.client.request().wsgi_request
        request.user = self.user
        request.palette_page = {}
        result = palette_actions.run_action(
            request, "add_lot", {"name": "blue dream shrimp", "auction": self.in_person_auction.title}
        )
        self.assertIn("Reused", result["summary"])


class AddPersonTests(PaletteAssistTestCase):
    """'add mike smith' is a person, not a lot called Mike Smith."""

    def _add_person(self, params, user=None):
        request = self.client.request().wsgi_request
        request.user = user or self.user
        request.palette_page = {}
        return palette_actions.run_action(request, "add_person", params)

    def test_an_admin_can_add_someone(self):
        result = self._add_person({"name": "Mike Smith", "auction": self.in_person_auction.title})
        self.assertTrue(result.get("ok"), result)
        tos = AuctionTOS.objects.filter(auction=self.in_person_auction, name="Mike Smith").first()
        self.assertIsNotNone(tos)
        self.assertTrue(tos.bidder_number)

    def test_a_participant_cannot_add_people(self):
        result = self._add_person({"name": "Mike Smith", "auction": self.in_person_auction.title}, user=self.member)
        self.assertIn("error", result)
        self.assertIn("permission", result["error"].lower())

    def test_adding_the_same_person_twice_is_refused_rather_than_duplicated(self):
        self._add_person({"name": "Mike Smith", "auction": self.in_person_auction.title})
        result = self._add_person({"name": "mike smith", "auction": self.in_person_auction.title})
        self.assertIn("error", result)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.in_person_auction, name="Mike Smith").count(), 1)

    def test_a_duplicate_bidder_number_is_the_forms_error_not_ours(self):
        result = self._add_person(
            {"name": "Mike Smith", "auction": self.in_person_auction.title, "bidder_number": "555"}
        )
        self.assertIn("error", result)
        self.assertIn("already in use", result["error"].lower())

    def test_it_writes_nothing_during_assist(self):
        """add_person is confirm-tier: the countdown comes back, the row does not exist yet."""
        self._script({"action": "add_person", "params": {"name": "Jane Doe"}, "summary": "Add Jane"})
        data = self._assist("add jane doe").json()
        self.assertEqual(data["kind"], "countdown")
        self.assertFalse(AuctionTOS.objects.filter(name="Jane Doe").exists())

    def test_the_prompt_warns_the_model_off_making_a_person_into_a_lot(self):
        prompt = palette_assist.build_system_prompt(self.user)
        self.assertIn("add_person", prompt)
        self.assertIn("PERSON", prompt)


class CancelTrackingTests(PaletteAssistTestCase):
    """Cancelling the countdown is the only signal that we understood the wrong thing."""

    def _countdown(self):
        self._script({"action": "add_lot", "params": {"name": "cancel me"}, "summary": "Add a lot"})
        return self._assist("add a lot of cancel me").json()

    def test_the_countdown_carries_the_usage_row_id(self):
        data = self._countdown()
        self.assertEqual(data["kind"], "countdown")
        self.assertTrue(data["usage_id"])

    def test_cancelling_marks_the_row(self):
        data = self._countdown()
        response = self.client.post(
            reverse("command_palette_cancel"),
            data=json.dumps({"usage_id": data["usage_id"]}),
            content_type="application/json",
        )
        self.assertTrue(response.json()["recorded"])
        self.assertTrue(LLMUsage.objects.get(pk=data["usage_id"]).cancelled)

    def test_cancelling_writes_nothing_else(self):
        data = self._countdown()
        self.client.post(
            reverse("command_palette_cancel"),
            data=json.dumps({"usage_id": data["usage_id"]}),
            content_type="application/json",
        )
        self.assertFalse(Lot.objects.filter(lot_name__icontains="cancel me").exists())

    def test_one_user_cannot_mark_anothers_row(self):
        data = self._countdown()
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("command_palette_cancel"),
            data=json.dumps({"usage_id": data["usage_id"]}),
            content_type="application/json",
        )
        self.assertFalse(response.json()["recorded"])
        self.assertFalse(LLMUsage.objects.get(pk=data["usage_id"]).cancelled)

    def test_junk_is_ignored(self):
        self.client.force_login(self.user)
        for body in ({"usage_id": "nonsense"}, {"usage_id": None}, {}):
            response = self.client.post(
                reverse("command_palette_cancel"), data=json.dumps(body), content_type="application/json"
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["recorded"])

    def test_the_endpoint_requires_login(self):
        self.client.logout()
        response = self.client.post(
            reverse("command_palette_cancel"), data=json.dumps({"usage_id": 1}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 302)


class ContextInMessagesTests(PaletteAssistTestCase):
    """Say which auction, not just what."""

    def test_the_countdown_names_the_auction_it_will_write_to(self):
        self._script({"action": "add_lot", "params": {"name": "context shrimp"}, "summary": "Add a lot"})
        data = self._assist("add a lot of context shrimp").json()
        self.assertEqual(data["kind"], "countdown")
        self.assertEqual(data["context"], self.in_person_auction.title)

    def test_the_progress_line_names_the_auction_too(self):
        self._script({"action": "add_lot", "params": {"name": "context shrimp"}, "summary": "Add a lot"})
        response = self._assist("add a lot of context shrimp")
        self.assertTrue(
            any(self.in_person_auction.title in line for line in response.progress_messages),
            response.progress_messages,
        )

    def test_a_navigation_says_where_it_is_going(self):
        self._script({"action": "go_to_page", "params": {"page": "auction_lot_list"}, "summary": ""})
        data = self._assist("take me to the lot list for this auction").json()
        self.assertEqual(data["kind"], "navigate")
        self.assertIn("Taking you to", data["message"])
        self.assertIn(self.in_person_auction.title, data["message"])

    def test_a_page_with_no_object_still_says_where_it_is_going(self):
        self._script({"action": "go_to_page", "params": {"page": "watched"}, "summary": ""})
        data = self._assist("take me to the lots I am watching").json()
        self.assertEqual(data["kind"], "navigate")
        self.assertIn("Taking you to", data["message"])


class ClarifyOptionsTests(PaletteAssistTestCase):
    """A question the user can't click is a dead end, especially by voice."""

    def test_a_clarify_without_options_still_offers_something_to_click(self):
        """The reported bug: an either/or question arrived with an empty options list.

        The prompt now demands options, but a prompt is a request. When the model asks anyway, the
        question gets ordinary search results underneath it so there is still a way forward —
        which matters most by voice, where there is nothing to type into.
        """
        self._script({"clarify": "Did you want to print labels, or look at invoices?"})
        data = self._assist("sort out my labels or invoices for this auction").json()
        self.assertEqual(data["kind"], "clarify")
        self.assertEqual(data["options"], [])
        self.assertTrue(data.get("groups"), "a question with nothing to click is the bug")

    def test_a_question_about_nothing_at_all_is_still_a_clean_question(self):
        """When search has nothing either, the question stands on its own rather than erroring."""
        self._script({"clarify": "Which did you mean?"})
        data = self._assist("zzqqxx wibble frobnicate").json()
        self.assertEqual(data["kind"], "clarify")
        self.assertEqual(data["message"], "Which did you mean?")
        self.assertFalse(data.get("groups"))

    def test_options_are_passed_through_when_the_model_gives_them(self):
        self._script({"clarify": "Which one?", "options": ["Assign bidder 1", "Check them in"]})
        data = self._assist("give john bidder number 1").json()
        self.assertEqual(data["options"], ["Assign bidder 1", "Check them in"])

    def test_the_prompt_requires_options_for_a_choice(self):
        prompt = palette_assist.build_system_prompt(self.user)
        self.assertIn("MUST put each", prompt)
