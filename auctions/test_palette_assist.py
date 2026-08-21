"""Tests for the command palette's natural-language assist.

Everything runs against a :class:`FakeProvider` installed with ``llm.set_provider_override`` --
no network, and every test can script exactly what the model "says", including malformed replies.

The things worth guarding here are the ones that would be expensive to get wrong: that an obvious
query never costs a model call, that nothing the model returns can widen what a user is allowed to
do, and that the execute endpoint is a real gate rather than a rubber stamp on the countdown.
"""

import datetime
import json
import re
from io import StringIO
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import llm, palette_actions, palette_assist, palette_routes
from auctions.llm import LLMError, LLMProvider, LLMResult, ToolCall
from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    Club,
    ClubMember,
    CommandPalettePage,
    LLMUsage,
    Lot,
    LotImage,
    UserData,
)
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


def as_result(reply):
    """One scripted reply, as the :class:`LLMResult` a tool-calling provider would return.

    Tests script what the model "says" as a small dict — ``{"action": ..., "params": ...}``,
    ``{"answer": ...}``, ``{"clarify": ..., "options": [...]}`` — and have since long before the
    palette used tool calls. That shorthand is still the clearest way to write a test, so it is
    translated here rather than rewritten several hundred times: an action or a lookup becomes a
    tool call, a clarify becomes a call to ``ask_the_user``, an error becomes ``cannot_do_this``,
    and an answer is plain text.

    A test that wants to be explicit about the wire shape can script an ``LLMResult`` directly and
    it is passed straight through.
    """
    if isinstance(reply, LLMResult):
        return reply
    reply = dict(reply or {})
    text = ""
    call = None
    if isinstance(reply.get("action"), str):
        call = ToolCall(id="call_1", name=reply["action"], arguments=reply.get("params") or {})
        text = str(reply.get("summary") or "")
    elif isinstance(reply.get("lookup"), str):
        call = ToolCall(id="call_1", name=reply["lookup"], arguments=reply.get("params") or {})
    elif isinstance(reply.get("clarify"), str):
        arguments = {"question": reply["clarify"]}
        if reply.get("options") is not None:
            arguments["options"] = reply["options"]
        call = ToolCall(id="call_1", name=palette_assist.ASK_THE_USER, arguments=arguments)
    elif isinstance(reply.get("error"), str):
        call = ToolCall(id="call_1", name=palette_assist.CANNOT_DO_THIS, arguments={"reason": reply["error"]})
    elif isinstance(reply.get("answer"), str):
        text = reply["answer"]
    return LLMResult(
        text=text,
        tool_calls=[call] if call else [],
        model="fake-model",
        prompt_tokens=11,
        completion_tokens=7,
    )


class FakeProvider(LLMProvider):
    """A scripted provider. Hand it the replies you want, in order."""

    name = "fake"

    def __init__(self, replies=None):
        super().__init__(model="fake-model", api_key="fake-key")
        self.replies = list(replies or [])
        self.calls = []

    def is_configured(self):
        return True

    def _next(self, system, messages, tools=None):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self.replies:
            msg = "FakeProvider ran out of scripted replies"
            raise LLMError(msg)
        return self.replies.pop(0)

    def complete_json(self, system, messages, max_tokens=800):
        return LLMResult(data=self._next(system, messages), model="fake-model", prompt_tokens=11, completion_tokens=7)

    def complete(self, system, messages, tools=None, max_tokens=800):
        return as_result(self._next(system, messages, tools))

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
        self.enable_assist_for_everyone()

    def enable_assist_for_everyone(self):
        """Opt every user in the fixture into the assistant.

        ``UserData.use_llm_search`` is off by default (the feature is per-user opt-in, flipped in
        the admin), so without this the whole suite would only ever exercise the disabled fallback.
        :class:`AssistDisabledTests` turns it back off to test that path deliberately.
        """
        UserData.objects.update(use_llm_search=True)
        for user in (self.user, self.admin_user, self.userB, self.member):
            # A UserData already cached on the in-memory user predates the UPDATE above, and a
            # later .save() on it would quietly write the old value back.
            user.userdata.refresh_from_db(fields=["use_llm_search"])

    def tearDown(self):
        llm.set_provider_override(None)
        super().tearDown()

    # -- helpers ----------------------------------------------------------

    def _clear_throttles(self, user):
        cache.delete(f"palette_assist_cooldown_{user.pk}")
        cache.delete(f"palette_assist_calls_{user.pk}")

    def _tools(self, user=None):
        """The tool list this user's palette would be handed."""
        return palette_assist.tools_for(user or self.user)

    def _tool(self, name, user=None):
        for tool in self._tools(user):
            if tool["name"] == name:
                return tool
        self.fail(f"{name} is not offered to this user")
        return None

    def _tool_names(self, user=None):
        return {tool["name"] for tool in self._tools(user)}

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

    def _request_for(self, user):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user
        request.palette_page = {}
        return request

    def _reply(self, name, arguments=None, text=""):
        """One tool call, as the provider would hand it over."""
        return LLMResult(text=text, tool_calls=[ToolCall(id="c1", name=name, arguments=arguments or {})])

    def test_a_tool_name_that_is_not_registered_is_refused(self):
        """The provider is supposed to make this impossible; a local model behind LLM_BASE_URL may not."""
        reply = palette_assist.read_reply(self._reply("read_all_invoices"))
        self.assertEqual(reply["kind"], "invalid")

    def test_a_read_only_tool_is_a_lookup_and_a_write_is_an_action(self):
        """The one distinction the palette still has to draw for itself.

        There is a single namespace of tools now, so nothing tells the loop whether a call is
        something to run and feed back or something that finishes the request — except the
        registry's own ``lookup`` flag, which is what the danger tier is derived from.
        """
        self.assertEqual(palette_assist.read_reply(self._reply("find_person", {"name": "bob"}))["kind"], "lookup")
        self.assertEqual(palette_assist.read_reply(self._reply("add_lot", {"name": "shrimp"}))["kind"], "action")

    def test_a_sentence_written_alongside_a_call_becomes_the_countdown_summary(self):
        reply = palette_assist.read_reply(self._reply("add_lot", {"name": "shrimp"}, text="Adding blue shrimp."))
        self.assertEqual(reply["summary"], "Adding blue shrimp.")

    def test_plain_text_with_no_call_is_an_answer(self):
        reply = palette_assist.read_reply(LLMResult(text="It started an hour ago."))
        self.assertEqual(reply["kind"], "answer")
        self.assertEqual(reply["message"], "It started an hour ago.")

    def test_nothing_at_all_is_invalid(self):
        self.assertEqual(palette_assist.read_reply(LLMResult())["kind"], "invalid")

    def test_only_the_first_call_is_acted_on(self):
        """A model asking for three things at once is asking us to guess an order to write them in."""
        reply = palette_assist.read_reply(
            LLMResult(
                tool_calls=[
                    ToolCall(id="c1", name="find_person", arguments={"name": "bob"}),
                    ToolCall(id="c2", name="add_lot", arguments={"name": "shrimp"}),
                ]
            )
        )
        self.assertEqual(reply["kind"], "lookup")
        self.assertEqual(reply["action"].name, "find_person")
        self.assertEqual(Lot.objects.filter(lot_name="shrimp").count(), 0)

    def test_the_palettes_own_two_tools_are_not_registry_actions(self):
        """``ask_the_user`` and ``cannot_do_this`` exist for the palette and never reach run_action."""
        for name in (palette_assist.ASK_THE_USER, palette_assist.CANNOT_DO_THIS):
            self.assertIsNone(palette_actions.get_action(name), name)
        asked = palette_assist.read_reply(self._reply(palette_assist.ASK_THE_USER, {"question": "Which bob?"}))
        self.assertEqual(asked["kind"], "clarify")
        refused = palette_assist.read_reply(self._reply(palette_assist.CANNOT_DO_THIS, {"reason": "no"}))
        self.assertEqual(refused["kind"], "error")

    def test_an_empty_question_is_not_a_question(self):
        self.assertEqual(palette_assist.read_reply(self._reply(palette_assist.ASK_THE_USER, {}))["kind"], "invalid")


class DangerTierTests(PaletteAssistTestCase):
    """safe executes now, confirm counts down, navigate goes to the page."""

    def test_a_lookup_in_the_action_slot_is_read_as_a_lookup(self):
        """Every safe action is a lookup, so one named in the ``action`` slot is still a lookup.

        This used to run as an action that was the end of the conversation, which meant the model's
        own note-to-self came back as the answer: asked "what's the split", the user was told
        "Fetching the auction details to see how the fee split is configured." and nothing else. The
        loop has to come back with a real answer instead.
        """
        self._script(
            {"action": "describe_auction", "params": {}, "summary": "Fetch the auction to explain the fees"},
            {"answer": "The club takes 25% of the winning bid."},
        )
        response = self._assist("what is the split in this auction")
        data = response.json()
        self.assertEqual(data["kind"], "answer")
        self.assertEqual(data["message"], "The club takes 25% of the winning bid.")
        self.assertEqual(self.provider.call_count, 2)

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
            {"action": "print_labels", "params": {"scope": "mine"}},
        )
        response = self._assist("who is the person called no_lots and print their labels")
        self.assertEqual(response.json()["kind"], "navigate")
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
        self._script({"action": "go_to_page", "params": {"page": "watched"}})
        self._assist("tell me about my current auction situation please")
        usage = LLMUsage.objects.all()
        self.assertEqual(usage.count(), 1)
        row = usage.first()
        self.assertEqual(row.user, self.user)
        self.assertEqual(row.model, "fake-model")
        self.assertEqual(row.total_tokens, 18)
        self.assertEqual(row.action, "go_to_page")
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

    def test_an_unusable_reply_is_not_retried(self):
        """A reply with nothing in it costs exactly one call.

        There used to be a correction round here, because JSON mode could not stop the model
        answering in the wrong shape and a nudge sometimes fixed it. The provider enforces the tool
        schemas now, so a reply that still gets here is one the endpoint didn't validate or one the
        model didn't make — and neither is fixed by asking again. The search ladder is faster and
        more use to the person waiting.
        """
        self._script(*[{"nonsense": True}] * 4)
        self._assist("something long enough to need the model and get nowhere at all")
        self.assertEqual(self.provider.call_count, 1)

    def test_a_repeated_lookup_is_not_run_again(self):
        """The model asking for the same thing twice will not get a different answer.

        It is told so, once, rather than being cut off: the result it is asking for again is already
        in the conversation, and ending the loop here threw away a lookup that had already been paid
        for. The lookup itself is not re-run -- the nudge costs a model call and no database work.
        """
        same = {"lookup": "find_person", "params": {"name": "nobody at all"}}
        self._script(same, same, same, same)
        with patch.object(palette_actions, "run_action", wraps=palette_actions.run_action) as run:
            self._assist("who on earth is nobody at all and what did they buy")
        self.assertEqual(run.call_count, 1, "the repeat must not hit the database again")
        self.assertEqual(self.provider.call_count, 3, "one nudge, then stop")

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
        """A lookup buys the round that uses it, and nothing buys a fourth."""
        self._script(*[{"lookup": "find_person", "params": {"name": f"person {i}"}} for i in range(10)])
        self._assist("find me somebody, anybody, and keep looking until you do")
        self.assertLessEqual(self.provider.call_count, palette_assist.MAX_ROUNDS_AFTER_LOOKUP)


class TokenAccountingTests(PaletteAssistTestCase):
    """What the analytics page reports it costs."""

    def test_cached_prompt_tokens_are_recorded(self):
        """The system prompt is identical on every call, so most of it is a cache hit.

        Billed at a fraction of the normal input rate -- a total that ignores this reads as several
        times the real bill and makes the feature look unaffordable when it isn't.
        """

        class CachingProvider(FakeProvider):
            def complete(self, system, messages, tools=None, max_tokens=800):
                self.calls.append({"system": system, "messages": messages, "tools": tools})
                return LLMResult(
                    tool_calls=[ToolCall(id="c1", name="go_to_page", arguments={"page": "watched"})],
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


class OptInTests(PaletteAssistTestCase):
    """The assistant is per-user opt-in on top of being site-configured."""

    def _opt_out(self, user):
        UserData.objects.filter(user=user).update(use_llm_search=False)
        user.userdata.refresh_from_db(fields=["use_llm_search"])

    def test_both_gates_are_required(self):
        self.assertTrue(palette_assist.assist_enabled_for(self.user))
        self._opt_out(self.user)
        self.assertFalse(palette_assist.assist_enabled_for(self.user))
        self.enable_assist_for_everyone()
        llm.set_provider_override(None)
        with override_settings(OPENAI_API_KEY="", LLM_BASE_URL=""):
            self.assertFalse(palette_assist.assist_enabled_for(self.user))

    def test_anonymous_users_never_get_it(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(palette_assist.assist_enabled_for(AnonymousUser()))
        self.assertFalse(palette_assist.assist_enabled_for(None))

    def test_an_opted_out_user_gets_plain_search_and_no_model_call(self):
        self._opt_out(self.user)
        self._script({"action": "go_to_page", "params": {"page": "nope"}})
        data = self._assist("add a lot of blue shrimp for bob").json()
        self.assertEqual(data["kind"], "results")
        self.assertEqual(self.provider.calls, [], "an opted-out user must never reach the model")

    def test_an_opted_out_user_cannot_execute_a_confirm_action(self):
        """A countdown started before the preference was turned off must not still run."""
        self._opt_out(self.user)
        response = self._execute("add_lot", {"name": "blue shrimp", "quantity": 2})
        self.assertEqual(response.json()["kind"], "error")
        self.assertFalse(Lot.objects.filter(lot_name="blue shrimp").exists())

    def test_the_template_only_offers_the_mic_to_opted_in_users(self):
        """The context processor and the endpoints must agree, or the mic lies about what works."""
        self.client.force_login(self.user)
        # "home" redirects to a landing page, so follow it to reach one that actually renders.
        self.assertTrue(self.client.get(reverse("home"), follow=True).context["palette_assist_enabled"])
        self._opt_out(self.user)
        self.assertFalse(self.client.get(reverse("home"), follow=True).context["palette_assist_enabled"])


class RegistryTests(PaletteAssistTestCase):
    """The prompt is generated from the registry, so the two can't drift apart."""

    def test_the_tool_list_is_every_action_the_user_could_use(self):
        offered = self._tool_names(self.user)
        for action in palette_actions.actions_for(self.user):
            self.assertIn(action.name, offered)

    def test_an_auction_admin_is_offered_the_auction_skills(self):
        """self.user created both auctions, so every admin skill is theirs to use."""
        offered = self._tool_names(self.user)
        for name in ("set_lot_winner", "check_in", "add_person", "set_invoice_status"):
            self.assertIn(name, offered)

    def test_a_plain_bidder_is_not_offered_club_administration(self):
        offered = self._tool_names(self.member)
        # Exact names now, not substrings of a JSON blob: "renew_member" used to have to be matched
        # as '"skill": "renew_member"' because it is a prefix of renew_membership, which this user
        # *does* get.
        for name in ("award_points", "renew_member", "set_invoice_status"):
            self.assertNotIn(name, offered)
        for name in ("watch_lot", "add_lot", "renew_membership"):
            self.assertIn(name, offered)

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

    def test_the_prompt_names_an_auction_the_user_has_not_joined(self):
        """The commonest reader of an auction's page is somebody deciding whether to join it."""
        path = reverse("auction_main", kwargs={"slug": self.online_auction.slug})
        page = palette_routes.page_context_from_path(self.user_who_does_not_join, path)
        prompt = palette_assist.build_system_prompt(self.user_who_does_not_join, page)
        self.assertIn(self.online_auction.title, prompt)
        self.assertIn("has NOT joined", prompt)

    def test_a_forged_path_still_cannot_act_on_an_auction_the_user_is_not_in(self):
        """Naming the auction on screen is context; writing to it is still membership-gated."""
        path = reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug})
        response = self._execute("add_lot", {"name": "trespassing shrimp"}, user=self.user_who_does_not_join, path=path)
        data = response.json()
        self.assertEqual(data["kind"], "error")
        self.assertIn(self.online_auction.title, data["message"])
        self.assertFalse(Lot.objects.filter(lot_name="trespassing shrimp").exists())

    def test_a_question_about_the_auction_on_screen_is_answered_without_joining(self):
        """The gap this closes: 'when does this start' on an auction you haven't joined."""
        path = reverse("auction_main", kwargs={"slug": self.online_auction.slug})
        from django.test import RequestFactory

        page = palette_routes.page_context_from_path(self.user_who_does_not_join, path)
        request = RequestFactory().get(path)
        request.user = self.user_who_does_not_join
        request.palette_page = page
        result = palette_actions.describe_auction(request, {})
        self.assertTrue(result["found"])
        self.assertEqual(result["auction"]["title"], self.online_auction.title)
        self.assertFalse(result["auction"]["you_have_joined"])


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
        self.assertIn(self.in_person_auction.title, palette_assist.humanize(text, self.user))
        self.assertNotIn(self.in_person_auction.slug, palette_assist.humanize(text, self.user))

    def test_a_route_key_becomes_its_label(self):
        self.assertIn("all lots in an auction", palette_assist.humanize("Try auction_lot_list next."))

    def test_ordinary_hyphenated_english_is_left_alone(self):
        """Nothing is replaced on the strength of its shape — only real slugs and real route keys."""
        for text in ("Use check-in mode.", "That is a sign-up page.", "e-mail them", "a well-known no-show"):
            self.assertEqual(palette_assist.humanize(text, self.user), text)

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
        self.assertEqual(palette_assist.humanize("Look in spring-sale.", self.user), "Look in Spring Sale.")

    def test_a_title_is_never_treated_as_a_regex_template(self):
        """Titles are arbitrary user text; one containing backreference syntax must survive intact."""
        auction = self._auction(r"Spring \1 Sale")
        self.assertIn(r"\1", palette_assist.humanize(f"Look in {auction.slug}.", self.user))

    def test_a_club_slug_becomes_its_name(self):
        club = Club.objects.create(name="Humanized Aquarium Society", active=True)
        ClubMember.objects.create(club=club, user=self.user, permission_admin=True)
        self.assertIn("Humanized Aquarium Society", palette_assist.humanize(f"Try {club.slug}.", self.user))

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

    def test_the_prompt_says_a_question_is_answered_in_words(self):
        """A question wants an answer, not a page — and the describe_* tool that produces one."""
        prompt = palette_assist.build_system_prompt(self.user)
        self.assertIn("plain words", prompt)
        self.assertIn("describe_", prompt)
        self.assertIn("describe_auction", self._tool_names())


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

    def test_add_lot_warns_the_model_off_making_a_person_into_a_lot(self):
        """ "add mike smith" is a person, not a lot called Mike Smith. Said where the model reads it."""
        description = self._tool("add_lot")["description"]
        self.assertIn("add_person", description)
        self.assertIn("PERSON", description)


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

    def test_asking_a_choice_requires_the_choices(self):
        """A question with a choice in it and no options is a dead end for anyone using voice."""
        tool = self._tool(palette_assist.ASK_THE_USER)
        self.assertIn("must put each choice in 'options'", tool["description"])
        self.assertIn("options", tool["inputSchema"]["properties"])


class LookupRoundBudgetTests(PaletteAssistTestCase):
    """A lookup that is never used is the most expensive thing this loop can do."""

    def test_a_lookup_is_never_the_last_round(self):
        """Two lookups and then the answer -- three rounds, which a flat cap of two would cut off.

        Under a flat two-round cap a request like this paid for two model calls and a database read
        and then dropped what it found on the floor, which is what "when does this auction start?"
        looked like from the outside: a spinner, and then search results.
        """
        self._script(
            {"lookup": "my_context", "params": {}},
            {"lookup": "describe_auction", "params": {}},
            {"answer": "It started an hour ago."},
        )
        data = self._assist(
            "when exactly does this auction start", path=self.in_person_auction.get_absolute_url()
        ).json()
        self.assertEqual(data["kind"], "answer")
        self.assertEqual(data["message"], "It started an hour ago.")
        self.assertEqual(self.provider.call_count, 3)

    def test_a_request_that_never_looks_anything_up_still_stops_at_two(self):
        self._script({"nonsense": 1}, {"nonsense": 2}, {"nonsense": 3}, {"nonsense": 4})
        self._assist("do something impossible with several words")
        self.assertLessEqual(self.provider.call_count, palette_assist.MAX_ROUNDS)


class DescribeAuctionPayloadTests(PaletteAssistTestCase):
    """What a lookup sends back is paid for by the token, and truncated if it doesn't fit."""

    def _describe(self, user=None):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user or self.user
        request.palette_page = {}
        return palette_actions.describe_auction(request, {"auction": self.in_person_auction.title})

    def test_dates_are_local_and_readable(self):
        """The raw field is UTC with microseconds, and the model repeated it back verbatim."""
        starts = self._describe()["auction"]["starts"]
        self.assertNotIn("+00:00", starts)
        self.assertIn(str(self.in_person_auction.date_start.astimezone(self.in_person_auction.timezone).year), starts)

    def test_the_chart_blob_is_not_sent(self):
        """``cached_stats`` is the stats page's chart series: ~700 tokens, and no question needs it."""
        admin = self._describe()["auction"].get("_admin", {})
        self.assertNotIn("cached_stats", admin)

    def test_the_fee_settings_survive_truncation(self):
        """ "What's the split?" was answered with an invented one because this got cut off."""
        self.in_person_auction.summernote_description = "Rules. " * 400
        self.in_person_auction.save()
        payload = json.dumps(self._describe(), default=str)
        self.assertLessEqual(len(payload), palette_assist.MAX_LOOKUP_RESULT_CHARS)
        trimmed = payload[: palette_assist.MAX_LOOKUP_RESULT_CHARS]
        self.assertIn("winning bid percent to club", trimmed)

    def test_the_alternate_split_is_described(self):
        settings_block = self._describe()["auction"]["settings"]
        names = {row["setting"] for row in settings_block}
        self.assertIn("Alternate split", names)
        self.assertIn("Alternate winning bid percent to club", names)

    def test_rules_are_stripped_and_capped(self):
        self.in_person_auction.summernote_description = "<p><b>Be nice.</b></p>" + ("blah " * 900)
        self.in_person_auction.save()
        rules = self._describe()["auction"]["rules"]
        self.assertNotIn("<b>", rules)
        self.assertLessEqual(len(rules), palette_actions.RULES_LIMIT)
        self.assertIn("Be nice.", rules)


class PageContextTests(PaletteAssistTestCase):
    """What the user is looking at, and the facts about it they are most likely to ask for."""

    def test_the_auction_on_screen_brings_its_own_facts(self):
        context = palette_actions.user_context(
            self.user, palette_routes.page_context_from_path(self.user, self.in_person_auction.get_absolute_url())
        )
        facts = context["looking_at_right_now"]["this_auction"]
        self.assertEqual(facts["title"], self.in_person_auction.title)
        self.assertFalse(facts["is_online"])
        self.assertEqual(facts["format"], "in-person auction")
        self.assertTrue(facts["starts"])

    def test_an_online_auction_says_so_in_words(self):
        """A bare ``is_online: false`` was read straight past; "Yes" came back about an in-person one."""
        context = palette_actions.user_context(
            self.user, palette_routes.page_context_from_path(self.user, self.online_auction.get_absolute_url())
        )
        self.assertEqual(context["looking_at_right_now"]["this_auction"]["format"], "online auction")

    def test_an_auction_the_user_has_not_joined_is_still_the_page_they_are_on(self):
        """Running an auction through its club is not the same as having joined it."""
        club = Club.objects.create(name="Runner Club", abbreviation="RC")
        run_not_joined = Auction.objects.create(
            created_by=self.userB,
            title="An auction run through its club",
            club=club,
            is_online=True,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        ClubMember.objects.create(club=club, user=self.member, permission_admin=True)
        self.assertFalse(AuctionTOS.objects.filter(auction=run_not_joined, user=self.member).exists())
        page = palette_routes.page_context_from_path(self.member, run_not_joined.get_absolute_url())
        self.assertEqual(page.get("auction"), run_not_joined.slug)

    def test_the_page_hint_still_cannot_write_to_an_unjoined_auction(self):
        """Naming an auction is not joining it: resolve_auction stays scoped to joined auctions."""
        stranger = Auction.objects.create(
            created_by=self.userB,
            title="Somebody else's auction",
            is_online=True,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        auction, _error = palette_actions.resolve_auction(self.member, "", {"auction": stranger.slug})
        self.assertNotEqual(getattr(auction, "pk", None), stranger.pk)


class PeopleTests(PaletteAssistTestCase):
    """Adding somebody, and then fixing what you got wrong about them."""

    def _run(self, action, params, user=None):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user or self.user
        request.palette_page = {"auction": self.in_person_auction.slug}
        return palette_actions.run_action(request, action, params)

    def test_adding_someone_says_their_details_are_blank(self):
        """Added by voice at the door, they have a name and nothing else -- and nobody was told."""
        result = self._run("add_person", {"name": "Doris Door"})
        self.assertIn("No email or phone number yet", result["summary"])
        self.assertTrue(any("Doris Door's details" in f["label"] for f in result["followups"]))

    def test_adding_someone_with_contact_details_does_not_nag(self):
        result = self._run("add_person", {"name": "Ed Email", "email": "ed@example.com", "phone_number": "5551212"})
        self.assertNotIn("No email", result["summary"])

    def test_the_countdown_names_who_it_is_about(self):
        """ "Add someone to the auction." over a five second timer describes the wrong half."""
        self._script({"action": "add_person", "params": {"name": "Nora New"}})
        data = self._assist("add nora new to this auction please").json()
        self.assertEqual(data["kind"], "countdown")
        self.assertIn("Nora New", data["summary"])

    def test_updating_an_email(self):
        self._run("add_person", {"name": "Fred Fix"})
        result = self._run("update_person", {"person": "Fred Fix", "email": "fred@example.com"})
        self.assertNotIn("error", result)
        tos = AuctionTOS.objects.get(auction=self.in_person_auction, name="Fred Fix")
        self.assertEqual(tos.email, "fred@example.com")

    def test_updating_a_phone_number(self):
        """The reported bug: a countdown ran, and the phone number did not change."""
        self._run("add_person", {"name": "Phil Phone"})
        self._run("update_person", {"person": "Phil Phone", "phone_number": "555-1212"})
        tos = AuctionTOS.objects.get(auction=self.in_person_auction, name="Phil Phone")
        self.assertEqual(tos.phone_number, "555-1212")

    def test_updating_uses_the_pages_duplicate_email_rule(self):
        self._run("add_person", {"name": "Ann A", "email": "ann@example.com"})
        self._run("add_person", {"name": "Ben B"})
        result = self._run("update_person", {"person": "Ben B", "email": "ann@example.com"})
        self.assertIn("error", result)

    def test_updating_nothing_asks_what_to_change(self):
        self._run("add_person", {"name": "Vic Vague"})
        result = self._run("update_person", {"person": "Vic Vague"})
        self.assertIn("more_info_needed", result)

    def test_a_name_is_only_renamed_when_a_new_name_is_given(self):
        """ "change bob's email" passes bob's name too; that must not be read as renaming bob to bob."""
        self._run("add_person", {"name": "Ray Rename"})
        self._run("update_person", {"person": "Ray Rename", "new_name": "Ray Renamed"})
        self.assertTrue(AuctionTOS.objects.filter(auction=self.in_person_auction, name="Ray Renamed").exists())

    def test_a_non_admin_cannot_change_someone(self):
        self._run("add_person", {"name": "Tim Target", "email": "tim@example.com"})
        result = self._run("update_person", {"person": "Tim Target", "email": "hacked@example.com"}, user=self.member)
        self.assertIn("error", result)
        tos = AuctionTOS.objects.get(auction=self.in_person_auction, name="Tim Target")
        self.assertEqual(tos.email, "tim@example.com")

    def test_updating_is_a_confirm_tier_action(self):
        """It writes to the database, so it gets the countdown and the execute endpoint's re-check."""
        self.assertEqual(palette_actions.get_action("update_person").danger, palette_actions.DANGER_CONFIRM)


class ClubManagedPeopleTests(PaletteAssistTestCase):
    """In a club-managed auction the club owns the bidder number, so it has to own the person."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Palette Club", abbreviation="PC")
        self.in_person_auction.club = self.club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()
        self.assertTrue(self.in_person_auction.is_club_managed)

    def _run(self, action, params):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = self.user
        request.palette_page = {"auction": self.in_person_auction.slug}
        return palette_actions.run_action(request, action, params)

    def test_adding_someone_creates_the_club_member(self):
        """Without this the participant had a bidder number the club had never heard of.

        Nothing in the club admin could find them, and the participant edit form hides the bidder
        number in club-managed mode -- so the number could not even be corrected afterwards.
        """
        self._run("add_person", {"name": "Cara Club"})
        tos = AuctionTOS.objects.get(auction=self.in_person_auction, name="Cara Club")
        self.assertIsNotNone(tos.clubmember_id)
        self.assertEqual(tos.clubmember.club, self.club)
        self.assertEqual(tos.clubmember.bidder_number, tos.bidder_number)

    def test_only_one_participant_row_is_created(self):
        """Creating the member also creates its shadow row; adding a second means two invoices."""
        self._run("add_person", {"name": "Solo Row"})
        self.assertEqual(AuctionTOS.objects.filter(auction=self.in_person_auction, name="Solo Row").count(), 1)

    def test_changing_contact_details_writes_them_to_the_club_member(self):
        """The club owns these fields; editing only the participant row leaves the two disagreeing."""
        self._run("add_person", {"name": "Mo Move"})
        self._run("update_person", {"person": "Mo Move", "email": "mo@example.com", "phone_number": "5550000"})
        tos = AuctionTOS.objects.get(auction=self.in_person_auction, name="Mo Move")
        self.assertEqual(tos.email, "mo@example.com")
        self.assertEqual(tos.clubmember.email, "mo@example.com")
        self.assertEqual(tos.clubmember.phone_number, "5550000")


class RequiredFieldTests(PaletteAssistTestCase):
    """An auction's own required fields are the auction's to enforce, whatever route a lot arrives by."""

    def setUp(self):
        super().setUp()
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Scientific name"
        self.in_person_auction.buy_now = "required"
        self.in_person_auction.use_custom_dropdown_field = "required"
        self.in_person_auction.custom_dropdown_name = "Table"
        self.in_person_auction.save()
        for value in ("A", "B"):
            AuctionDropdown.objects.create(auction=self.in_person_auction, value=value)
        self.walk_in = AuctionTOS.objects.create(
            auction=self.in_person_auction, name="Walk In", pickup_location=self.in_person_location
        )

    def _add(self, **params):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = self.user
        request.palette_page = {"auction": self.in_person_auction.slug}
        return palette_actions.run_action(
            request, "add_lot", {"name": "blue shrimp", "bidder": self.walk_in.bidder_number, **params}
        )

    def test_a_lot_missing_required_fields_is_refused_and_says_which(self):
        result = self._add()
        self.assertIn("more_info_needed", result)
        self.assertIn("Scientific name", result["more_info_needed"])
        self.assertIn("Table", result["more_info_needed"])
        self.assertEqual(Lot.objects.filter(lot_name="Blue Shrimp", auction=self.in_person_auction).count(), 0)

    def test_a_dropdown_value_the_auction_does_not_offer_is_refused(self):
        result = self._add(custom_field_1="Neocaridina", buy_now_price=20, custom_dropdown="not an option")
        self.assertIn("error", result)
        self.assertEqual(Lot.objects.filter(lot_name="Blue Shrimp", auction=self.in_person_auction).count(), 0)

    def test_a_complete_lot_is_accepted(self):
        result = self._add(custom_field_1="Neocaridina", buy_now_price=20, custom_dropdown="A")
        self.assertNotIn("error", result)
        self.assertEqual(Lot.objects.filter(lot_name="Blue Shrimp", auction=self.in_person_auction).count(), 1)

    def test_adding_a_lot_for_a_seller_with_no_account_does_not_crash(self):
        """Most sellers at an in-person auction were added at the door and have no login.

        ``find_lot_to_copy`` returned a bare ``None`` for them where every other exit returns a
        pair, so the unpack raised and every one of these came back as "Something went wrong".
        """
        self.assertIsNone(self.walk_in.user)
        self.assertEqual(palette_actions.find_lot_to_copy(self.walk_in.user, "blue shrimp"), (None, False))
        result = self._add(custom_field_1="Neocaridina", buy_now_price=20, custom_dropdown="A")
        self.assertNotIn("Something went wrong", str(result))


#: Fields whose name suggests the auction charges somebody money. See DriftTests.
MONEY_FIELD = re.compile(r"fee|percent|split|price|cost|tax")
#: Fields whose name suggests a club points rule. Same rule, same reason.
POINTS_FIELD = re.compile(r"point|bap|hap|cap|breeder")


class DriftTests(PaletteAssistTestCase):
    """The parts that go stale silently when the rest of the site moves on."""

    def test_every_registered_action_is_offered_to_the_model_as_a_tool(self):
        """One catalogue. A skill the palette has is a skill /mcp/ has, and the reverse."""
        from auctions.mcp import tools as mcp_tools

        offered = {tool["name"] for tool in mcp_tools.tool_descriptors(None)}
        self.assertEqual(offered, set(palette_actions.ACTIONS))

    def test_the_palette_offers_the_shared_catalogue_and_its_own_two_tools(self):
        from auctions.mcp import tools as mcp_tools

        shared = {tool["name"] for tool in mcp_tools.tool_descriptors(self.user)}
        offered = {tool["name"] for tool in palette_assist.tools_for(self.user)}
        self.assertEqual(offered - shared, {palette_assist.ASK_THE_USER, palette_assist.CANNOT_DO_THIS})

    def test_update_person_sends_every_field_its_form_asks_for(self):
        """The data dict is read off the form, so a new field on the modal can't break the palette."""
        from auctions.forms import CreateEditAuctionTOS

        tos = AuctionTOS.objects.create(
            auction=self.in_person_auction, name="Drift Check", pickup_location=self.in_person_location
        )
        from django.forms import model_to_dict

        data = model_to_dict(tos, fields=CreateEditAuctionTOS.Meta.fields)
        self.assertEqual(set(data), set(CreateEditAuctionTOS.Meta.fields))

    def test_a_truncated_lookup_says_it_was_truncated(self):
        """Silent truncation is how an answer about fees got invented: the fees were past the cut."""
        payload = palette_assist.lookup_payload("describe_auction", {"rules": "x" * 9000})
        self.assertIn("TRUNCATED", payload)
        self.assertIn("Do not fill in anything", payload)

    def test_a_payload_that_fits_is_sent_verbatim(self):
        payload = palette_assist.lookup_payload("my_context", {"username": "bob"})
        self.assertNotIn("TRUNCATED", payload)
        self.assertIn('"username": "bob"', payload)

    def test_every_money_field_on_an_auction_is_described_or_excused(self):
        """The rule that would have caught the reported bug before a user did.

        Asked "what's the split?" the assistant answered with a fee percentage nobody had
        configured, because the alternate-fee fields weren't in ``_AUCTION_SETTINGS`` and it filled
        the gap from the fees it could see. A fee the assistant can't see is worse than one it has
        never heard of, so a new one has to be described or written off on purpose.
        """
        described = set(palette_actions._AUCTION_SETTINGS)
        excused = set(palette_actions.SETTINGS_NOT_DESCRIBED)
        money = {
            field.name
            for field in Auction._meta.get_fields()
            if hasattr(field, "attname") and MONEY_FIELD.search(field.name)
        }
        self.assertEqual(
            sorted(money - described - excused),
            [],
            "These look like fees on Auction but the assistant can't see them. Add each to "
            "palette_actions._AUCTION_SETTINGS, or to SETTINGS_NOT_DESCRIBED with a reason.",
        )

    def test_every_points_rule_on_a_club_is_described_or_excused(self):
        """ "How do I earn points?" is answered entirely from this list, so a gap in it is a wrong answer."""
        described = set(palette_actions._CLUB_BAP_SETTINGS)
        excused = set(palette_actions.POINTS_NOT_DESCRIBED)
        rules = {
            field.name
            for field in Club._meta.get_fields()
            if hasattr(field, "attname") and POINTS_FIELD.search(field.name)
        }
        self.assertEqual(
            sorted(rules - described - excused),
            [],
            "These look like club points rules the assistant can't see. Add each to "
            "palette_actions._CLUB_BAP_SETTINGS, or to POINTS_NOT_DESCRIBED with a reason.",
        )

    def test_nothing_is_both_described_and_excused(self):
        for described, excused in (
            (palette_actions._AUCTION_SETTINGS, palette_actions.SETTINGS_NOT_DESCRIBED),
            (palette_actions._CLUB_BAP_SETTINGS, palette_actions.POINTS_NOT_DESCRIBED),
        ):
            self.assertEqual(set(described) & set(excused), set())

    def test_every_excused_setting_has_a_real_reason(self):
        excuses = {**palette_actions.SETTINGS_NOT_DESCRIBED, **palette_actions.POINTS_NOT_DESCRIBED}
        for name, reason in excuses.items():
            self.assertGreater(len(reason), 20, f"{name} needs a real reason, not '{reason}'")

    def test_every_described_setting_is_a_real_field(self):
        """A renamed field would otherwise vanish from the answer with nothing failing."""
        for name in palette_actions._AUCTION_SETTINGS:
            Auction._meta.get_field(name)
        for name in palette_actions._CLUB_BAP_SETTINGS:
            Club._meta.get_field(name)

    def test_every_describe_lookup_fits_without_truncation(self):
        """The guard for the day a new setting pushes one of these over the limit again."""
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = self.user
        request.palette_page = {}
        self.in_person_auction.summernote_description = "Rules and more rules. " * 300
        self.in_person_auction.save()
        club = Club.objects.create(name="Drift Club", abbreviation="DC", description="About us. " * 300)
        for name, params in (
            ("describe_auction", {"auction": self.in_person_auction.title}),
            ("describe_club", {"club": club.name}),
            ("my_context", {}),
        ):
            with self.subTest(lookup=name):
                result = palette_actions.run_action(request, name, params)
                self.assertNotIn("TRUNCATED", palette_assist.lookup_payload(name, result))


class DisclosureTests(PaletteAssistTestCase):
    """Messages that echo what the caller typed must not turn a guess into a confirmation."""

    def setUp(self):
        super().setUp()
        self.secret = Auction.objects.create(
            created_by=self.userB,
            title="The Secret Society Auction",
            is_online=True,
            promote_this_auction=False,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

    def test_a_guessed_slug_does_not_come_back_as_a_title(self):
        """The oracle: an error echoes the hint, and humanize used to look it up unscoped.

        Posting a guessed slug to the execute endpoint answered "I couldn't find an auction called
        “The Secret Society Auction”" — confirming it exists, and handing over its current title, to
        somebody with no relationship to it at all.
        """
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = self.member
        result = palette_assist.execute(request, "add_lot", {"auction": self.secret.slug, "name": "x"})
        self.assertNotIn(self.secret.title, result["message"])
        self.assertIn(self.secret.slug, result["message"])

    def test_a_slug_the_user_can_see_is_still_tidied_away(self):
        """The scoping must not break what humanize is for."""
        text = f"Opening {self.in_person_auction.slug} for you."
        self.assertIn(self.in_person_auction.title, palette_assist.humanize(text, self.user))

    def test_without_a_user_no_slug_resolves(self):
        text = f"Opening {self.in_person_auction.slug} for you."
        self.assertEqual(palette_assist.humanize(text), text)

    def test_route_keys_still_resolve_without_a_user(self):
        """A route label is a static catalog string and names no object, so it needs no scoping."""
        self.assertIn("all lots in an auction", palette_assist.humanize("Try auction_lot_list next."))

    def test_a_stale_last_auction_pointer_is_not_trusted(self):
        """``last_auction_used`` outlives the participant row it was set from."""
        self.member.userdata.last_auction_used = self.secret
        self.member.userdata.save()
        auction, error = palette_actions.resolve_auction(self.member, "")
        self.assertIsNone(auction)
        self.assertTrue(error)

    def test_a_stale_pointer_is_not_described_either(self):
        self.member.userdata.last_auction_used = self.secret
        self.member.userdata.save()
        auction, error = palette_actions._resolve_described_auction(self.member, "", {})
        self.assertIsNone(auction)
        self.assertTrue(error)

    def test_a_non_admin_is_not_offered_admin_destinations(self):
        """``match_routes`` took a user and ignored it, so find_page offered everyone everything."""
        matches = palette_routes.match_routes("treasurer report", self.member)
        self.assertEqual([route.key for route in matches if route.admin == palette_routes.ADMIN_AUCTION], [])

    def test_an_admin_still_is(self):
        matches = palette_routes.match_routes("treasurer report", self.user)
        self.assertTrue(matches)

    def test_the_prompt_catalog_and_the_matcher_agree(self):
        """These filtered differently, which is how one of them came to be forgotten."""
        catalog = palette_routes.catalog_for_prompt(self.member)
        for route in palette_routes.match_routes("report invoices lots users club", self.member):
            self.assertIn(route.key, catalog)


class AddPersonCollisionTests(PaletteAssistTestCase):
    """Adding a person must not quietly become editing a different one."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Collision Club", abbreviation="CC")
        self.in_person_auction.club = self.club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()

    def _add(self, **params):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = self.user
        request.palette_page = {"auction": self.in_person_auction.slug}
        return palette_actions.run_action(request, "add_person", params)

    def test_an_email_that_belongs_to_somebody_else_is_refused(self):
        """``ensure_club_member`` matches on email, so this used to rename the person it matched."""
        self._add(name="Bob Original", email="shared@example.com")
        member = ClubMember.objects.get(club=self.club, email="shared@example.com")
        # The member's address and the participant row's can differ, which is what slips past the
        # form's own duplicate-email rule.
        AuctionTOS.objects.filter(clubmember=member).update(email="")
        result = self._add(name="Jane Impostor", email="shared@example.com")
        self.assertIn("error", result)
        self.assertIn("Bob Original", result["error"])
        self.assertTrue(AuctionTOS.objects.filter(auction=self.in_person_auction, name="Bob Original").exists())
        self.assertFalse(AuctionTOS.objects.filter(auction=self.in_person_auction, name="Jane Impostor").exists())

    def test_a_shadow_row_with_no_name_of_its_own_is_still_adopted(self):
        """Refusing a collision must not refuse a row that nobody has claimed yet.

        ``AuctionTOS.save()`` fills a blank name with "Unknown", so both spellings of "not really
        anybody" have to be adoptable — otherwise a club member added by email only can never be
        given a name here.
        """
        self._add(name="Blank Row", email="blank@example.com")
        member = ClubMember.objects.get(club=self.club, email="blank@example.com")
        AuctionTOS.objects.filter(clubmember=member).delete()
        AuctionTOS.objects.create(
            auction=self.in_person_auction,
            clubmember=member,
            pickup_location=self.in_person_location,
            name="Unknown",
        )
        result = self._add(name="Now Named", email="blank@example.com")
        self.assertNotIn("error", result)
        self.assertTrue(AuctionTOS.objects.filter(auction=self.in_person_auction, name="Now Named").exists())


class RunActionTestCase(PaletteAssistTestCase):
    """Base for tests that call resolvers directly rather than through the model.

    Everything below is server behaviour -- what a resolver returns, who it refuses, what it writes.
    Scripting a model reply to reach it would test the prompt, not the thing under test.
    """

    def _run(self, action, params=None, user=None, page=None):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user or self.user
        request.palette_page = page if page is not None else {"auction": self.in_person_auction.slug}
        return palette_actions.run_action(request, action, params or {})


class AuctionNumbersTests(RunActionTestCase):
    """The running totals an auctioneer asks for mid-event."""

    def test_counts_come_from_the_auction_itself(self):
        result = self._run("auction_numbers", {"auction": self.online_auction.slug})
        numbers = result["numbers"]
        self.assertEqual(numbers["lots_sold"], self.online_auction.total_sold_lots)
        self.assertEqual(
            numbers["lots_total"], Lot.objects.filter(auction=self.online_auction, is_deleted=False).count()
        )
        # Counted, not subtracted: a removed lot is neither sold nor unsold.
        self.assertEqual(numbers["lots_unsold"], 1)

    def test_money_is_admin_only(self):
        mine = self._run("auction_numbers", {"auction": self.online_auction.slug})
        self.assertIn("_admin", mine["numbers"])
        theirs = self._run("auction_numbers", {"auction": self.online_auction.slug}, user=self.member)
        self.assertNotIn("_admin", theirs["numbers"])

    def test_a_private_stats_auction_gives_a_non_admin_only_the_clock(self):
        self.online_auction.make_stats_public = False
        self.online_auction.save()
        result = self._run("auction_numbers", {"auction": self.online_auction.slug}, user=self.member)
        self.assertNotIn("lots_sold", result["numbers"])
        self.assertIn("time", result["numbers"])

    def test_an_in_person_auction_with_no_online_bidding_does_not_invent_a_countdown(self):
        self.in_person_auction.online_bidding = "disable"
        self.in_person_auction.save()
        result = self._run("auction_numbers", {"auction": self.in_person_auction.slug})
        self.assertNotIn("time_left", result["numbers"]["time"])
        self.assertIn("doesn't count down", result["numbers"]["time"]["note"])


class MyActivityTests(RunActionTestCase):
    """The bidder-side answer path: what did I win, what do I owe, am I paid up."""

    def test_reports_the_users_own_lots_and_invoice(self):
        result = self._run("my_activity", {"auction": self.online_auction.slug})
        activity = result["activity"]
        self.assertEqual(activity["lots_submitted"], 4)
        self.assertEqual(activity["lots_sold"], 3)
        self.assertEqual(activity["your_bidder_number"], self.online_tos.bidder_number)

    def test_the_invoice_direction_is_stated_in_words(self):
        """A bare signed total was read back as "you owe" to somebody who was owed it."""
        result = self._run("my_activity", {"auction": self.online_auction.slug})
        invoice = result["activity"]["invoice"]
        self.assertEqual(invoice["the_club_owes_you"], bool(self.invoice.user_should_be_paid))
        self.assertNotEqual(invoice["you_owe_the_club"], invoice["the_club_owes_you"])
        # Never signed: the direction is carried by the booleans above.
        self.assertFalse(str(invoice["total"]).startswith("-"))

    def test_someone_who_has_not_joined_gets_told_so_rather_than_an_error(self):
        result = self._run("my_activity", {"auction": self.in_person_auction.slug}, user=self.userB)
        self.assertNotIn("error", result)
        self.assertIn("note", result["activity"])


class ListTests(RunActionTestCase):
    """The end-of-auction cleanup questions."""

    def test_listing_people_is_admin_only(self):
        result = self._run("list_people", {"status": "not_checked_in"}, user=self.member)
        self.assertIn("error", result)
        self.assertIn("only admins", result["error"].lower())

    def test_not_checked_in_filters_on_the_stored_timestamp(self):
        self.in_person_buyer.checked_in = timezone.now()
        self.in_person_buyer.save()
        result = self._run("list_people", {"status": "not_checked_in"})
        self.assertNotIn("555", [row["bidder_number"] for row in result["people"]])
        arrived = self._run("list_people", {"status": "checked_in"})
        self.assertIn("555", [row["bidder_number"] for row in arrived["people"]])

    def test_duplicates_reads_the_field_that_was_already_computed(self):
        other = AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Bob Twice",
        )
        # Set the way the merge tool sets it -- a queryset update, not save() -- because saving an
        # AuctionTOS re-runs the duplicate detection and would overwrite what this test is asserting.
        AuctionTOS.objects.filter(pk=other.pk).update(possible_duplicate=self.in_person_buyer.pk)
        result = self._run("list_people", {"status": "duplicates"})
        other.refresh_from_db()
        self.assertIn(other.bidder_number, [row["bidder_number"] for row in result["people"]])
        flagged = next(row for row in result["people"] if row["bidder_number"] == other.bidder_number)
        self.assertEqual(flagged["might_be_the_same_as"], self.in_person_buyer.name)

    def test_the_documented_spelling_of_duplicates_works(self):
        """``possible_duplicates`` is what the parameter docs tell the model to send."""
        other = AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, name="Bob Twice"
        )
        AuctionTOS.objects.filter(pk=other.pk).update(possible_duplicate=self.in_person_buyer.pk)
        result = self._run("list_people", {"status": "possible_duplicates"})
        flagged = [row for row in result["people"] if "might_be_the_same_as" in row]
        self.assertTrue(flagged)

    def test_an_unpaid_invoice_is_reported_unsigned_with_a_direction(self):
        """Some of the people who "haven't paid" are owed money, not owing it."""
        result = self._run("list_people", {"status": "unpaid", "auction": self.online_auction.slug})
        for row in result["people"]:
            self.assertFalse(str(row["invoice_total"]).startswith("-"))
            self.assertIn("the_club_owes_them", row)

    def test_a_participant_with_two_invoices_is_listed_once(self):
        from auctions.models import Invoice

        Invoice.objects.create(auctiontos_user=self.tosB, auction=self.online_auction)
        result = self._run("list_people", {"status": "unpaid", "auction": self.online_auction.slug})
        numbers = [row["bidder_number"] for row in result["people"]]
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_mine_needs_no_admin_rights(self):
        result = self._run("list_lots", {"status": "mine", "auction": self.online_auction.slug}, user=self.member)
        self.assertNotIn("error", result)

    def test_the_sellers_name_is_only_added_for_admins(self):
        mine = self._run("list_lots", {"status": "all", "auction": self.online_auction.slug})
        self.assertIn("seller", mine["lots"][0])
        theirs = self._run("list_lots", {"status": "all", "auction": self.online_auction.slug}, user=self.member)
        self.assertNotIn("seller", theirs["lots"][0])


class RecentChangesTests(RunActionTestCase):
    """Reading back the history the palette has been writing all along."""

    def test_palette_writes_are_flagged_as_the_assistants_own(self):
        self._run("add_lot", {"name": "blue shrimp", "auction": self.in_person_auction.slug})
        self.in_person_auction.create_history(applies_to="LOTS", action="Something a person did", user=self.user)
        result = self._run("recent_changes", {})
        self.assertTrue(any(row["by_the_assistant"] for row in result["changes"]))
        self.assertTrue(any(not row["by_the_assistant"] for row in result["changes"]))

    def test_it_is_admin_only(self):
        result = self._run("recent_changes", {}, user=self.member)
        self.assertIn("error", result)


class DescribeLotLiveStateTests(RunActionTestCase):
    """The three questions most asked about a live lot."""

    def setUp(self):
        super().setUp()
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.save()
        self.live_lot = Lot.objects.create(
            lot_name="Live shrimp lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            user=self.user,
            quantity=1,
            reserve_price=5,
            active=True,
        )

    def test_a_live_lot_reports_its_price_bids_and_close_time(self):
        result = self._run("describe_lot", {"lot": "Live shrimp lot"})
        lot = result["lot"]
        self.assertIn("current_price", lot)
        self.assertIn("bids", lot)
        self.assertIn("bidding_closes", lot)
        self.assertFalse(lot["you_are_the_high_bidder"])

    def test_a_sealed_bid_auction_never_reveals_the_price(self):
        self.online_auction.sealed_bid = True
        self.online_auction.save()
        result = self._run("describe_lot", {"lot": "Live shrimp lot"})
        self.assertIsNone(result["lot"]["current_price"])
        self.assertIn("sealed", result["lot"]["note"].lower())

    def test_the_top_proxy_bid_is_never_returned(self):
        """``max_bid`` is the one number on this site that must not reach a bidder."""
        result = self._run("describe_lot", {"lot": "Live shrimp lot"})
        self.assertNotIn("max_bid", json.dumps(result, default=str))


class NoSaleTests(RunActionTestCase):
    """ "Pass" — the ordinary outcome for a good fraction of lots."""

    def test_it_ends_the_lot_with_no_winner(self):
        result = self._run("no_sale", {"lot": "101-1"})
        self.assertNotIn("error", result)
        self.in_person_lot.refresh_from_db()
        self.assertIsNone(self.in_person_lot.auctiontos_winner)
        self.assertFalse(self.in_person_lot.active)

    def test_it_refuses_a_lot_that_already_sold(self):
        self.in_person_lot.auctiontos_winner = self.in_person_buyer
        self.in_person_lot.winning_price = 12
        self.in_person_lot.save()
        result = self._run("no_sale", {"lot": "101-1"})
        self.assertIn("error", result)
        self.assertIn("undo", result["error"])

    def test_it_is_admin_only(self):
        result = self._run("no_sale", {"lot": "101-1"}, user=self.member)
        self.assertIn("error", result)

    def test_undo_sale_puts_a_passed_lot_back_up(self):
        self._run("no_sale", {"lot": "101-1"})
        result = self._run("undo_sale", {"lot": "101-1"})
        self.assertNotIn("error", result)
        self.in_person_lot.refresh_from_db()
        self.assertTrue(self.in_person_lot.active)


class AddLotsTests(RunActionTestCase):
    """Batch entry, which is what a drop-off table actually does."""

    def test_a_list_becomes_several_lots(self):
        result = self._run("add_lots", {"lots": ["java fern", "a heater"], "auction": self.in_person_auction.slug})
        self.assertNotIn("error", result)
        self.assertEqual(Lot.objects.filter(auction=self.in_person_auction, lot_name="Java Fern").count(), 1)
        self.assertEqual(Lot.objects.filter(auction=self.in_person_auction, lot_name="A Heater").count(), 1)

    def test_a_comma_separated_string_is_accepted_rather_than_corrected(self):
        result = self._run("add_lots", {"lots": "java fern, a heater", "auction": self.in_person_auction.slug})
        self.assertNotIn("error", result)
        self.assertEqual(Lot.objects.filter(auction=self.in_person_auction, lot_name="Java Fern").count(), 1)

    def test_batch_level_flags_apply_to_every_lot(self):
        self._run(
            "add_lots",
            {"lots": ["guppies", "endlers"], "auction": self.in_person_auction.slug, "i_bred_this_fish": True},
        )
        lots = Lot.objects.filter(auction=self.in_person_auction, lot_name__in=["Guppies", "Endlers"])
        self.assertEqual(lots.count(), 2)
        self.assertTrue(all(lot.i_bred_this_fish for lot in lots))

    def test_one_bad_lot_does_not_lose_the_others(self):
        result = self._run("add_lots", {"lots": ["java fern", ""], "auction": self.in_person_auction.slug})
        self.assertTrue(result.get("ok"))
        self.assertEqual(Lot.objects.filter(auction=self.in_person_auction, lot_name="Java Fern").count(), 1)

    def test_a_whole_box_is_refused_and_sent_to_the_bulk_page(self):
        result = self._run(
            "add_lots",
            {"lots": [f"lot {n}" for n in range(30)], "auction": self.in_person_auction.slug},
        )
        self.assertIn("error", result)
        self.assertIn("bulk add", result["error"])


class LotCategoryTests(RunActionTestCase):
    """Item B: a palette-added lot must not be quietly worse than a form-added one."""

    def test_the_category_is_guessed_rather_than_defaulted(self):
        from auctions.models import Category

        wanted = Category.objects.create(name="Plants")
        Lot.objects.create(
            lot_name="Java Fern",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            species_category=wanted,
            category_automatically_added=False,
            quantity=1,
        )
        self.assertEqual(palette_actions._category_pk("java fern"), wanted.pk)

    def test_an_unguessable_name_still_gets_a_category(self):
        from auctions.models import Category

        Category.objects.get_or_create(name="Uncategorized")
        self.assertIsNotNone(palette_actions._category_pk("qqqqzzz"))


class CustomLotFieldTests(RunActionTestCase):
    """Item C: fields the auction uses have to reach the prompt and be asked for."""

    def test_the_clubs_own_label_is_what_the_model_is_told(self):
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Scientific name"
        self.in_person_auction.save()
        fields = palette_actions.lot_fields_in_use(self.in_person_auction)
        self.assertEqual(fields["custom_field_1"]["label"], "Scientific name")
        self.assertTrue(fields["custom_field_1"]["required"])

    def test_a_required_field_is_asked_about_by_its_label(self):
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Scientific name"
        self.in_person_auction.save()
        result = self._run("add_lot", {"name": "blue shrimp", "auction": self.in_person_auction.slug})
        self.assertIn("more_info_needed", result)
        self.assertIn("Scientific name", result["more_info_needed"])

    def test_the_breeder_flag_is_a_documented_parameter_now(self):
        """It was applied but never advertised, so the model had no reason to send it."""
        self.assertIn("i_bred_this_fish", palette_actions.ACTIONS["add_lot"].params)

    def test_the_dropdown_options_come_from_the_clubs_own_rows(self):
        self.in_person_auction.use_custom_dropdown_field = "allow"
        self.in_person_auction.custom_dropdown_name = "River"
        self.in_person_auction.save()
        AuctionDropdown.objects.create(auction=self.in_person_auction, value="Rio Negro")
        fields = palette_actions.lot_fields_in_use(self.in_person_auction)
        self.assertEqual(fields["custom_dropdown"]["options"], ["Rio Negro"])


class UpdatePreferencesTests(RunActionTestCase):
    """Changing one setting without a trip to a page of thirty checkboxes."""

    def test_a_spoken_setting_name_resolves_to_a_real_field(self):
        self.assertEqual(palette_actions._resolve_preference("new auction emails"), "email_me_about_new_auctions")
        self.assertEqual(palette_actions._resolve_preference("kilometers"), "distance_unit")

    def test_turning_something_off_saves_through_the_pages_own_form(self):
        self.user.userdata.email_me_about_new_auctions = True
        self.user.userdata.save()
        result = self._run("update_preferences", {"setting": "new auction emails", "value": False})
        self.assertNotIn("error", result)
        self.user.userdata.refresh_from_db()
        self.assertFalse(self.user.userdata.email_me_about_new_auctions)

    def test_switching_to_kilometres_does_not_shrink_the_search_radii(self):
        """The form converts km back to miles on save, so the data it is given must be in km."""
        self.user.userdata.distance_unit = "km"
        self.user.userdata.local_distance = 100
        self.user.userdata.save()
        self._run("update_preferences", {"setting": "email visible", "value": True})
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.local_distance, 100)

    def test_an_unknown_setting_asks_rather_than_guessing(self):
        result = self._run("update_preferences", {"setting": "the thing with the fish", "value": True})
        self.assertIn("more_info_needed", result)


class DoorPrizeTests(RunActionTestCase):
    """A live-event moment where saying it out loud beats finding a page."""

    def test_only_checked_in_people_are_drawn(self):
        self.in_person_buyer.checked_in = timezone.now()
        self.in_person_buyer.save()
        result = self._run("draw_door_prize", {})
        self.assertNotIn("error", result)
        self.in_person_buyer.refresh_from_db()
        self.assertIsNotNone(self.in_person_buyer.door_prize_called)

    def test_nobody_checked_in_says_so_plainly(self):
        AuctionTOS.objects.filter(auction=self.in_person_auction).update(checked_in=None)
        result = self._run("draw_door_prize", {})
        self.assertIn("error", result)
        self.assertIn("checked in", result["error"])

    def test_a_winner_is_never_drawn_twice(self):
        self.in_person_buyer.checked_in = timezone.now()
        self.in_person_buyer.save()
        AuctionTOS.objects.filter(auction=self.in_person_auction).exclude(pk=self.in_person_buyer.pk).update(
            checked_in=None
        )
        self._run("draw_door_prize", {})
        again = self._run("draw_door_prize", {})
        self.assertIn("error", again)
        self.assertIn("already won", again["error"])


class PrintByBidderTests(RunActionTestCase):
    """The front-desk scope that was missing even though the route existed."""

    def test_an_admin_gets_that_bidders_label_page(self):
        result = self._run("print_labels", {"bidder": "555"})
        self.assertIn("/print/bidder/555/", result["url"])

    def test_unprinted_narrows_it_further(self):
        result = self._run("print_labels", {"bidder": "555", "scope": "unprinted"})
        self.assertIn("/unprinted/", result["url"])

    def test_a_participant_cannot_print_somebody_elses(self):
        result = self._run("print_labels", {"bidder": "555"}, user=self.member)
        self.assertIn("error", result)


class JoinAuctionTests(RunActionTestCase):
    """Reaching an auction the user has NOT joined — the one thing every other resolver can't do."""

    def test_it_never_agrees_to_the_rules_on_somebodys_behalf(self):
        result = self._run("join_auction", {"auction": self.in_person_auction.slug}, user=self.userB, page={})
        self.assertIn("url", result)
        self.assertFalse(AuctionTOS.objects.filter(auction=self.in_person_auction, user=self.userB).exists())

    def test_it_says_when_a_pickup_location_has_to_be_chosen(self):
        from auctions.models import PickupLocation

        PickupLocation.objects.create(
            name="second location",
            auction=self.in_person_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        result = self._run("join_auction", {"auction": self.in_person_auction.slug}, user=self.userB, page={})
        self.assertIn("pickup location", result["summary"])

    def test_somebody_already_in_is_told_their_bidder_number(self):
        result = self._run("join_auction", {"auction": self.in_person_auction.slug}, user=self.member, page={})
        self.assertIn("already in", result["summary"])


class SearchHelpTests(RunActionTestCase):
    """Grounding platform how-do-I questions in text somebody here wrote."""

    def test_it_finds_a_matching_faq(self):
        from auctions.models import FAQ

        FAQ.objects.create(
            category_text="Bidding",
            question="What is a zorblatt lot?",
            answer="A zorblatt lot is one nobody has to pay for.",
        )
        # A term nothing else on the site uses, so this asserts the search rather than whatever the
        # seeded FAQ and blog rows happen to say about a real topic.
        result = self._run("search_help", {"query": "zorblatt"})
        self.assertTrue(result["found"])
        self.assertIn("zorblatt lot is one nobody", result["help"][0]["answer"])

    def test_finding_nothing_tells_the_model_not_to_improvise(self):
        result = self._run("search_help", {"query": "zzzqqq nonexistent topic"})
        self.assertFalse(result["found"])
        self.assertIn("general knowledge", result["summary"])


class UndoLastTests(RunActionTestCase):
    """A bounded undo, over actions that describe their own reversal."""

    def setUp(self):
        super().setUp()
        cache.delete(palette_actions._undo_key(self.user))

    def _do_and_remember(self, action, params):
        result = self._run(action, params)
        palette_actions.remember_undo(self.user, action, result)
        return result

    def test_a_sale_can_be_undone(self):
        self._do_and_remember("set_lot_winner", {"lot": "101-1", "winner": "555", "price": "12"})
        self.in_person_lot.refresh_from_db()
        self.assertIsNotNone(self.in_person_lot.auctiontos_winner)
        result = self._run("undo_last", {})
        self.assertNotIn("error", result)
        self.in_person_lot.refresh_from_db()
        self.assertIsNone(self.in_person_lot.auctiontos_winner)

    def test_a_person_edit_is_put_back_exactly(self):
        self.in_person_buyer.email = "before@example.com"
        self.in_person_buyer.save()
        self._do_and_remember("update_person", {"person": "555", "email": "after@example.com"})
        self.in_person_buyer.refresh_from_db()
        self.assertEqual(self.in_person_buyer.email, "after@example.com")
        self._run("undo_last", {})
        self.in_person_buyer.refresh_from_db()
        self.assertEqual(self.in_person_buyer.email, "before@example.com")

    def test_adding_something_is_not_undoable(self):
        """Undoing an add means deleting, and a delete stays a page."""
        result = self._run("add_lot", {"name": "blue shrimp", "auction": self.in_person_auction.slug})
        palette_actions.remember_undo(self.user, "add_lot", result)
        self.assertIn("error", self._run("undo_last", {}))

    def test_undoing_twice_does_not_apply_the_same_reversal_again(self):
        self._do_and_remember("watch_lot", {"lot_id": self.in_person_lot.pk})
        self._run("undo_last", {})
        again = self._run("undo_last", {})
        self.assertIn("error", again)

    def test_nothing_to_undo_says_so_usefully(self):
        result = self._run("undo_last", {})
        self.assertIn("error", result)
        self.assertIn("undo", result["error"])

    def test_the_window_is_not_extended_by_later_commands(self):
        """Every write resets the cache TTL, so the age has to be checked per entry."""
        self._do_and_remember("watch_lot", {"lot_id": self.in_person_lot.pk})
        stale = cache.get(palette_actions._undo_key(self.user))
        stale[0]["at"] = (
            timezone.now() - datetime.timedelta(seconds=palette_actions.UNDO_WINDOW_SECONDS + 60)
        ).isoformat()
        cache.set(palette_actions._undo_key(self.user), stale, timeout=palette_actions.UNDO_WINDOW_SECONDS)
        self.assertEqual(palette_actions._undo_stack(self.user), [])
        self.assertIn("error", self._run("undo_last", {}))


class TrustWindowTests(PaletteAssistTestCase):
    """The repeat-write countdown: shortened by use, spent by a cancel."""

    def setUp(self):
        super().setUp()
        self.action = palette_actions.ACTIONS["add_lot"]
        self.params = {"name": "blue shrimp", "auction": self.in_person_auction.slug}

    def _request(self, user=None):
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user or self.user
        request.palette_page = {}
        return request

    def test_the_first_countdown_is_the_full_five_seconds(self):
        request = self._request()
        palette_assist.forget_trust(request, self.action, self.params)
        response = palette_assist._countdown_response(request, self.action, self.params, "")
        self.assertEqual(response["delay_ms"], palette_assist.COUNTDOWN_MS)

    def test_it_shortens_once_the_same_thing_has_been_approved(self):
        request = self._request()
        palette_assist.remember_trust(request, self.action, self.params)
        response = palette_assist._countdown_response(request, self.action, self.params, "")
        self.assertEqual(response["delay_ms"], palette_assist.TRUSTED_COUNTDOWN_MS)

    def test_cancelling_spends_it(self):
        request = self._request()
        palette_assist.remember_trust(request, self.action, self.params)
        palette_assist.forget_trust(request, self.action, self.params)
        response = palette_assist._countdown_response(request, self.action, self.params, "")
        self.assertEqual(response["delay_ms"], palette_assist.COUNTDOWN_MS)

    def test_an_ordinary_bidder_never_gets_a_shortened_countdown(self):
        request = self._request(user=self.userB)
        palette_assist.remember_trust(request, self.action, self.params)
        response = palette_assist._countdown_response(request, self.action, self.params, "")
        self.assertEqual(response["delay_ms"], palette_assist.COUNTDOWN_MS)


@isolated_cache("palette-lookup-preload")
class LookupPreloadTests(PaletteAssistTestCase):
    """Item 26: a phrase always answered from one lookup costs one round, not two."""

    def setUp(self):
        super().setUp()
        # preloadable_lookup() caches its verdict per phrase for an hour, and these tests reuse one
        # phrase with different usage rows behind it. Safe to flush: the decorator above gives this
        # class a cache of its own instead of the Redis every --parallel worker shares.
        cache.clear()

    def _record(self, query, destination, times):
        for _ in range(times):
            LLMUsage.objects.create(user=self.user, query=query, destination=destination, success=True)

    def test_a_repeated_parameterless_lookup_becomes_a_preload(self):
        self._record("how is it going", "lookup:auction_numbers", palette_assist.PRELOAD_MIN_COUNT)
        self.assertEqual(palette_assist.preloadable_lookup("how is it going"), "auction_numbers")

    def test_one_disagreement_leaves_the_phrase_to_the_model(self):
        self._record("how is it going", "lookup:auction_numbers", palette_assist.PRELOAD_MIN_COUNT)
        self._record("how is it going", "lookup:my_activity", 1)
        self.assertIsNone(palette_assist.preloadable_lookup("how is it going"))

    def test_a_navigation_is_never_preloaded_as_a_lookup(self):
        self._record("take me to my invoice", "invoice", palette_assist.PRELOAD_MIN_COUNT)
        self.assertIsNone(palette_assist.preloadable_lookup("take me to my invoice"))

    def test_only_a_parameterless_lookup_is_ever_recorded(self):
        with_params = {("describe_auction", json.dumps({"auction": "x"}, sort_keys=True))}
        self.assertEqual(palette_assist._answered_from(with_params), "")
        without = {("auction_numbers", json.dumps({}, sort_keys=True))}
        self.assertEqual(palette_assist._answered_from(without), "lookup:auction_numbers")

    def test_the_miner_does_not_turn_a_lookup_into_a_page_shortcut(self):
        self._record("how is it going", "lookup:auction_numbers", palette_assist.PRELOAD_MIN_COUNT)
        out = StringIO()
        call_command("mine_palette_shortcuts", "--apply", stdout=out)
        self.assertFalse(CommandPalettePage.objects.filter(search_term="how is it going").exists())
        self.assertIn("answered from a single lookup", out.getvalue())


class FailureReportTests(PaletteAssistTestCase):
    """Item 25: turn the worst moments into a queue somebody can read."""

    def test_a_failure_carries_the_id_needed_to_report_it(self):
        self._script({"error": "I can't do that"})
        response = self._assist("please launch a rocket into orbit for me").json()
        self.assertIsNotNone(response.get("usage_id"))

    def test_reporting_flags_the_row(self):
        usage = LLMUsage.objects.create(user=self.user, query="something that failed", success=False)
        self.client.force_login(self.user)
        self.client.post(
            reverse("command_palette_report"),
            data=json.dumps({"usage_id": usage.pk}),
            content_type="application/json",
        )
        usage.refresh_from_db()
        self.assertTrue(usage.reported)

    def test_one_user_cannot_flag_anothers_row(self):
        usage = LLMUsage.objects.create(user=self.userB, query="not mine", success=False)
        self.client.force_login(self.user)
        self.client.post(
            reverse("command_palette_report"),
            data=json.dumps({"usage_id": usage.pk}),
            content_type="application/json",
        )
        usage.refresh_from_db()
        self.assertFalse(usage.reported)


class CarryOverTests(PaletteAssistTestCase):
    """Item 28: the second sentence of a conversation must keep the first one's subject."""

    def test_the_auction_is_carried_forward(self):
        self.assertIn("auction", palette_assist._CARRY_OVER_KEYS)
        carried = palette_assist._carry_over({"auction": "spring-2026", "lot_id": 3, "irrelevant": "x"})
        self.assertEqual(carried, {"auction": "spring-2026", "lot_id": 3})

    def test_the_client_will_accept_back_everything_a_resolver_hands_forward(self):
        """A carry-over key the sanitizer drops is one the next command silently loses."""
        entries = palette_assist.sanitize_context(
            [{"query": "q", "result": "r", "data": dict.fromkeys(palette_assist._CARRY_OVER_KEYS, "v")}]
        )
        self.assertEqual(set(entries[0]["data"]), set(palette_assist._CARRY_OVER_KEYS))
