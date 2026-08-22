"""Tests for the MCP tool catalogue.

The bulk of this file is an **audit**, in the spirit of the two the palette already has:
``test_palette_routes`` guarantees every page can be reached, ``test_palette_skills`` guarantees
every write the UI can do is either covered by an action or written down as deliberately not
covered, and this one guarantees every action that exists turns into a tool an MCP host will
accept. All three fail the build rather than waiting for somebody to notice.

The most load-bearing test here is :meth:`RegistryConformance.test_every_parameter_declares_its_type`.
The whole schema is derived from the prose in ``Action.params`` -- there is no second table of
types -- so the day somebody writes a parameter description that doesn't open with
``"<type>, required|optional"`` is the day that parameter silently loses its type. This catches it
in CI instead.
"""

import datetime
import json
import re
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from auctions import palette_actions
from auctions.mcp import protocol, tools
from auctions.models import UserAPIKey, UserData
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase

#: Description patterns a connector review rejects: text that tells the model how to behave rather
#: than what the tool does. Deliberately narrow. Sibling disambiguation ("to find out about a lot
#: instead of changing it, use describe_lot") is *recommended* practice, not injection, so the
#: patterns here target instruction shapes and nothing else.
INSTRUCTION_SHAPED = [
    r"\balways\b",
    r"\byou must\b",
    r"\bnever call\b",
    r"\bignore\b",
    r"\bsystem prompt\b",
    r"\bdo not use (any )?other\b",
    r"\bbefore (calling|using) any\b",
    r"\bfirst call\b",
    r"\bregardless of\b",
]

#: Connector review criteria: tool names must be 64 characters or fewer.
MAX_TOOL_NAME = 64

JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}


class ParamSchemaTests(SimpleTestCase):
    """The prose-to-JSON-Schema reader, on its own."""

    def test_simple_required_string(self):
        schema, required = tools.param_schema("string, required. The lot number as called out.")
        self.assertEqual(schema["type"], "string")
        self.assertTrue(required)
        # The type prefix moved into the schema; the prose after it stayed.
        self.assertEqual(schema["description"], "The lot number as called out.")

    def test_optional_with_a_default(self):
        schema, required = tools.param_schema("integer, optional, default 1.")
        self.assertEqual(schema["type"], "integer")
        self.assertFalse(required)
        # The tail of the sentence is what carries the default, and it stays.
        self.assertIn("efault 1", schema["description"])

    def test_a_parameter_with_nothing_but_a_type_gets_no_description(self):
        schema, required = tools.param_schema("boolean, optional.")
        self.assertEqual(schema, {"type": "boolean"})
        self.assertFalse(required)

    def test_either_of_two_types(self):
        schema, _ = tools.param_schema("string or boolean, required. true/false, or a value.")
        self.assertEqual(schema["type"], ["string", "boolean"])

    def test_array_carries_its_item_type(self):
        schema, required = tools.param_schema("array of string or object, required. The things to add.")
        self.assertEqual(schema["type"], "array")
        self.assertEqual(schema["items"]["type"], ["string", "object"])
        self.assertTrue(required)

    def test_undocumented_shape_keeps_the_prose_and_is_optional(self):
        schema, required = tools.param_schema("whatever you like")
        self.assertEqual(schema, {"description": "whatever you like"})
        self.assertFalse(required)

    def test_the_prose_after_the_prefix_is_never_lost(self):
        """The point of moving the prefix rather than dropping the sentence."""
        schema, _ = tools.param_schema("string, optional, ADMINS ONLY. Bidder number or name to add the lot for.")
        self.assertIn("ADMINS ONLY", schema["description"])
        self.assertIn("Bidder number", schema["description"])


class RegistryConformance(SimpleTestCase):
    """Every registered action, as a tool an MCP host will accept."""

    def setUp(self):
        self.descriptors = tools.tool_descriptors(None)
        self.by_name = {descriptor["name"]: descriptor for descriptor in self.descriptors}

    def test_every_action_becomes_a_tool(self):
        self.assertEqual(set(self.by_name), set(palette_actions.ACTIONS))
        self.assertTrue(self.descriptors, "the registry is empty")

    def test_names_are_short_enough(self):
        for name in self.by_name:
            self.assertLessEqual(len(name), MAX_TOOL_NAME, f"{name} is too long to be a tool name")

    def test_every_tool_has_a_title_and_a_description(self):
        for name, descriptor in self.by_name.items():
            self.assertTrue(descriptor["title"].strip(), f"{name} has no title")
            self.assertTrue(descriptor["description"].strip(), f"{name} has no description")
            # Only once. The spec says the top-level ``title`` wins over ``annotations.title``, so
            # sending both is the same string twice on every tool on every session.
            self.assertNotIn("title", descriptor["annotations"], f"{name} sends its title twice")

    def test_annotations_declare_the_read_write_split(self):
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            action = palette_actions.ACTIONS[name]
            self.assertIsInstance(annotations["readOnlyHint"], bool)
            self.assertEqual(annotations["readOnlyHint"], action.danger != palette_actions.DANGER_CONFIRM)
            self.assertFalse(annotations["openWorldHint"], f"{name} reaches outside this site?")

    def test_a_write_says_whether_it_destroys_and_whether_it_repeats(self):
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            if annotations["readOnlyHint"]:
                continue
            self.assertIsInstance(annotations["destructiveHint"], bool, f"{name} does not say if it destroys")
            # ``idempotentHint`` is only sent when it is true: false is the spec's own default for
            # it, and a write that repeats is the exception rather than the rule here.
            if tools.idempotent(palette_actions.ACTIONS[name]):
                self.assertIs(annotations["idempotentHint"], True, f"{name} repeats but does not say so")
            else:
                self.assertNotIn("idempotentHint", annotations, f"{name} says the default out loud")

    def test_a_read_carries_neither_hint(self):
        """Both are defined only when readOnlyHint is false, so on a read they are two dead keys.

        tools/list is paid for in full, in context, by every host on every session -- fifty-odd
        tools times two keys that say nothing is not free.
        """
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            if not annotations["readOnlyHint"]:
                continue
            self.assertNotIn("destructiveHint", annotations, f"{name} reads; the hint means nothing")
            self.assertNotIn("idempotentHint", annotations, f"{name} reads; the hint means nothing")

    def test_every_parameter_declares_its_type(self):
        """The convention the whole schema is derived from. See this module's docstring."""
        for name, action in palette_actions.ACTIONS.items():
            for param, description in action.params.items():
                schema, _ = tools.param_schema(description)
                self.assertIn(
                    "type",
                    schema,
                    f"{name}.{param} does not open with '<type>, required|optional' — "
                    f"got {description[:60]!r}. Without it the parameter has no JSON Schema type.",
                )

    def test_parameter_types_are_real_json_schema_types(self):
        for descriptor in self.descriptors:
            for param, schema in descriptor["inputSchema"]["properties"].items():
                declared = schema.get("type")
                names = declared if isinstance(declared, list) else [declared]
                for one in names:
                    self.assertIn(one, JSON_SCHEMA_TYPES, f"{descriptor['name']}.{param}: {one!r}")

    def test_input_schemas_are_closed_objects(self):
        for descriptor in self.descriptors:
            schema = descriptor["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            for param in schema.get("required", []):
                self.assertIn(param, schema["properties"])

    def test_no_parameter_prose_is_lost_on_the_way_into_the_schema(self):
        """A parameter documented with more than its type keeps every word of it."""
        for name, action in palette_actions.ACTIONS.items():
            properties = self.by_name[name]["inputSchema"]["properties"]
            for param, prose in action.params.items():
                match = tools._PARAM_PREFIX.match(prose)
                remainder = prose[match.end() :].lstrip(" ,.").strip() if match else prose
                if not remainder:
                    # Nothing but the type. The name and the schema say all there is to say.
                    self.assertNotIn("description", properties[param], f"{name}.{param}")
                    continue
                self.assertEqual(
                    properties[param]["description"].lower(),
                    remainder.lower(),
                    f"{name}.{param} lost or gained words on the way into the schema",
                )

    def test_descriptions_do_not_instruct_the_model(self):
        for name, descriptor in self.by_name.items():
            for pattern in INSTRUCTION_SHAPED:
                match = re.search(pattern, descriptor["description"], re.IGNORECASE)
                self.assertIsNone(
                    match,
                    f"{name}: {pattern!r} matched — a tool description says what the tool does, "
                    f"not how the model should behave. Context: "
                    f"{descriptor['description'][max(0, match.start() - 60) : match.end() + 40] if match else ''}",
                )

    def test_descriptions_do_not_point_at_the_palette_prompt(self):
        """A description has to stand alone: an agent has no 'context below' to look in."""
        for name, descriptor in self.by_name.items():
            haystack = descriptor["description"] + json.dumps(descriptor["inputSchema"])
            for phrase in ("context below", "list below", "listed under", "the prompt"):
                self.assertNotIn(phrase, haystack.lower(), f"{name} refers to {phrase!r}")

    def test_the_whole_catalogue_serialises(self):
        json.dumps(self.descriptors)

    def test_read_only_credentials_get_no_write_tools(self):
        reads = tools.tool_descriptors(None, writes=False)
        self.assertTrue(reads)
        self.assertLess(len(reads), len(self.descriptors))
        for descriptor in reads:
            self.assertTrue(descriptor["annotations"]["readOnlyHint"], descriptor["name"])
        # And it drops exactly the write tools, not a tool more.
        dropped = set(self.by_name) - {descriptor["name"] for descriptor in reads}
        self.assertEqual(
            dropped,
            {
                name
                for name, action in palette_actions.ACTIONS.items()
                if action.danger == palette_actions.DANGER_CONFIRM
            },
        )


@isolated_cache("mcp-tools")
class CallToolTests(StandardTestCase):
    """The dispatcher: the three shapes a resolver can return, as MCP results."""

    def setUp(self):
        super().setUp()
        UserData.objects.update(use_llm_search=True)

    def _request_for(self, user):
        request = RequestFactory().post("/mcp/")
        request.user = user
        return request

    def _text(self, result):
        return result["content"][0]["text"]

    def test_unknown_tool_is_never_guessed_at(self):
        with self.assertRaises(tools.UnknownTool):
            tools.call_tool(self._request_for(self.user), "nonesuch", {})

    def test_a_read_comes_back_as_json(self):
        result = tools.call_tool(self._request_for(self.user), "my_context", {})
        self.assertFalse(result["isError"])
        payload = json.loads(self._text(result))
        self.assertEqual(payload["username"], self.user.username)

    def test_an_error_is_an_mcp_error_carrying_the_message(self):
        result = tools.call_tool(self._request_for(self.user), "describe_lot", {})
        self.assertTrue(result["isError"])
        # Actionable, per the review criteria: the message says what to send instead.
        self.assertIn("lot number", self._text(result))

    def test_finding_nothing_is_not_an_error(self):
        """A search that matched nothing succeeded. Only a failure to *run* is an MCP error."""
        result = tools.call_tool(self._request_for(self.user), "describe_lot", {"lot": "no such lot anywhere"})
        self.assertFalse(result["isError"])
        self.assertIs(json.loads(self._text(result))["found"], False)

    def test_an_ambiguous_answer_asks_the_caller_to_narrow_it(self):
        """``more_info_needed`` comes back as a recoverable error carrying the candidates."""
        result = tools.call_tool(self._request_for(self.user), "go_to_page", {})
        self.assertTrue(result["isError"])
        self.assertTrue(self._text(result).strip())

    def test_a_read_only_credential_cannot_reach_a_write_tool(self):
        result = tools.call_tool(self._request_for(self.user), "add_lot", {"name": "guppies"}, writes=False)
        self.assertTrue(result["isError"])
        self.assertIn("read-only", self._text(result))

    def test_a_read_only_credential_can_still_read(self):
        result = tools.call_tool(self._request_for(self.user), "my_context", {}, writes=False)
        self.assertFalse(result["isError"])

    def test_internal_bookkeeping_is_not_handed_to_the_caller(self):
        request = self._request_for(self.user)
        result = tools.call_tool(request, "watch_lot", {"lot": str(self.lot.pk)})
        if not result["isError"]:
            self.assertNotIn("undo", json.loads(self._text(result)))

    def test_a_read_carries_the_parsed_object_as_well_as_the_text(self):
        """MCP 2025-06-18's ``structuredContent``: the host gets an object, not a string to parse."""
        result = tools.call_tool(self._request_for(self.user), "my_context", {})
        self.assertEqual(result["structuredContent"], json.loads(self._text(result)))
        self.assertEqual(result["structuredContent"]["username"], self.user.username)

    def test_the_structure_and_the_text_are_always_the_same_answer(self):
        """Including when the text is a refusal: the structure must not carry what was withheld."""
        request = self._request_for(self.user)
        with patch.object(tools, "MAX_RESULT_CHARS", 200):
            result = tools.call_tool(request, "my_context", {})
        self.assertEqual(result["structuredContent"], json.loads(self._text(result)))
        self.assertIn("too big", result["structuredContent"]["error"])

    def test_a_plain_sentence_error_carries_no_structure(self):
        """``structuredContent`` has to be an object; a one-line refusal is not one."""
        result = tools.call_tool(self._request_for(self.user), "add_lot", {"name": "guppies"}, writes=False)
        self.assertTrue(result["isError"])
        self.assertNotIn("structuredContent", result)

    def test_a_disambiguation_carries_structure_too(self):
        """ "Which lot?" is a successful result that has not acted, and it is structured too."""
        result = tools.call_tool(self._request_for(self.user), "watch_lot", {})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "needs_more_information")
        self.assertEqual(result["structuredContent"], json.loads(self._text(result)))

    def test_everything_in_a_result_survives_json(self):
        """``structuredContent`` is serialised again by the transport, so a Decimal in it is a 500."""
        result = tools.call_tool(self._request_for(self.user), "describe_lot", {"lot": str(self.lot.lot_name)})
        json.dumps(result)

    def test_a_result_is_bounded(self):
        long_result = {"ok": True, "summary": "x" * (tools.MAX_RESULT_CHARS * 2)}
        text = tools._text(tools._payload(long_result))
        self.assertLess(len(text), tools.MAX_RESULT_CHARS)
        self.assertIn("too big", text)

    def test_a_result_that_does_not_fit_is_still_valid_json(self):
        """It used to be sliced at 20 000 characters, which lands mid-string.

        A host that parses tool output got a parse error where the answer should have been -- and
        the "narrow the query" note was appended after the break, so the one instruction the caller
        needed was the part that got cut off.
        """
        long_result = {"ok": True, "summary": "fine", "rows": ["x" * 200] * 500}
        parsed = json.loads(tools._text(tools._payload(long_result)))
        self.assertEqual(parsed["summary"], "fine")
        self.assertIn("limit and offset", parsed["what_to_do"])


@isolated_cache("mcp-endpoint")
class EndpointTests(StandardTestCase):
    """The HTTP end of it: the statuses the transport spec attaches to each case.

    Written against the URL rather than against ``transport``/``protocol`` internals, so these
    keep their meaning if the hand-written wire layer is ever swapped for a library.
    """

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        # The feature is per-user opt-in (UserData.use_llm_search, off by default while it is in
        # beta) and the endpoint enforces that, so the fixture has to opt in. OptInTests below is
        # where the flag itself is tested.
        UserData.objects.update(use_llm_search=True)
        raw, prefix, key_hash = UserAPIKey.generate()
        self.raw_key = raw
        self.key = UserAPIKey.objects.create(
            user=self.user, name="test key", prefix=prefix, key_hash=key_hash, allow_writes=True
        )
        raw_ro, prefix_ro, hash_ro = UserAPIKey.generate()
        self.raw_read_only_key = raw_ro
        self.read_only_key = UserAPIKey.objects.create(
            user=self.user, name="read only", prefix=prefix_ro, key_hash=hash_ro, allow_writes=False
        )

    def rpc(self, method, params=None, *, key=None, message_id=1, **extra):
        body = {"jsonrpc": "2.0", "method": method}
        if message_id is not None:
            body["id"] = message_id
        if params is not None:
            body["params"] = params
        headers = {"HTTP_MCP_PROTOCOL_VERSION": protocol.LATEST_PROTOCOL_VERSION}
        raw = self.raw_key if key is None else key
        if raw:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {raw}"
        headers.update(extra)
        return self.client.post(self.url, data=json.dumps(body), content_type="application/json", **headers)

    def result(self, response):
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertNotIn("error", payload, payload)
        return payload["result"]

    # --- authentication ---------------------------------------------------

    def test_no_credential_is_a_401_that_says_where_to_authenticate(self):
        response = self.rpc("initialize", key="")
        self.assertEqual(response.status_code, 401)
        challenge = response["WWW-Authenticate"]
        self.assertTrue(challenge.startswith("Bearer "))
        # Without the pointer a client has to guess at the well-known paths, or give up.
        self.assertIn("resource_metadata=", challenge)
        self.assertIn("/.well-known/oauth-protected-resource", challenge)

    def test_a_session_cookie_is_not_a_credential(self):
        """The rule the CSRF exemption rests on. See auctions/mcp/auth.py."""
        self.client.force_login(self.user)
        response = self.rpc("initialize", key="")
        self.assertEqual(response.status_code, 401)

    def test_a_wrong_key_is_a_401(self):
        self.assertEqual(self.rpc("initialize", key="ak_deadbeef.nope").status_code, 401)

    def test_a_revoked_key_stops_working(self):
        self.key.is_active = False
        self.key.save()
        self.assertEqual(self.rpc("initialize").status_code, 401)

    def test_an_expired_key_stops_working(self):
        self.key.expires_at = timezone.now() - datetime.timedelta(minutes=1)
        self.key.save()
        self.assertEqual(self.rpc("initialize").status_code, 401)

    def test_using_a_key_records_that_it_was_used(self):
        self.rpc("ping")
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.last_used_at)

    # --- the protocol -----------------------------------------------------

    def test_initialize_negotiates_and_advertises_only_what_exists(self):
        result = self.result(self.rpc("initialize", {"protocolVersion": protocol.LATEST_PROTOCOL_VERSION}))
        self.assertEqual(result["protocolVersion"], protocol.LATEST_PROTOCOL_VERSION)
        # Everything implemented and nothing else: tools, resources (the ui:// widget documents
        # and the addressable reads), prompts, and completion for the prompts' arguments. Logging,
        # sampling and elicitation stay out -- all three need the server to speak first, which this
        # transport cannot do. See docs/mcp_next.md.
        self.assertEqual(set(result["capabilities"]), {"tools", "resources", "prompts", "completions"})
        self.assertTrue(result["serverInfo"]["name"])
        self.assertTrue(result["instructions"].strip())

    def test_an_unknown_protocol_version_in_the_body_falls_back_to_ours(self):
        result = self.result(self.rpc("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(result["protocolVersion"], protocol.LATEST_PROTOCOL_VERSION)

    def test_an_unknown_protocol_version_in_the_header_is_a_400(self):
        response = self.rpc("initialize", HTTP_MCP_PROTOCOL_VERSION="1999-01-01")
        self.assertEqual(response.status_code, 400)

    def test_a_missing_protocol_version_header_is_allowed(self):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}
        response = self.client.post(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(response.status_code, 200)

    def test_ping(self):
        self.assertEqual(self.result(self.rpc("ping")), {})

    def test_a_notification_is_accepted_with_no_body(self):
        response = self.rpc("notifications/initialized", message_id=None)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.content)

    def test_an_unknown_method_is_a_jsonrpc_error_not_a_crash(self):
        # ``logging/setLevel``, because log level is a deployment decision and this server will
        # never implement it. This test has now been round the houses twice -- it named
        # ``resources/list`` until the widgets shipped and ``prompts/list`` until the recipes did --
        # so it is pointed at something on the "not worth doing" half of docs/mcp_next.md.
        response = self.rpc("logging/setLevel", {"level": "debug"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["error"]["code"], protocol.METHOD_NOT_FOUND)

    def test_an_unknown_tool_is_invalid_params(self):
        response = self.rpc("tools/call", {"name": "nonesuch", "arguments": {}})
        self.assertEqual(json.loads(response.content)["error"]["code"], protocol.INVALID_PARAMS)

    def test_a_body_that_is_not_json_is_a_400(self):
        response = self.client.post(
            self.url,
            data="{not json",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
        )
        self.assertEqual(response.status_code, 400)

    def test_a_batch_says_so_rather_than_half_working(self):
        response = self.client.post(
            self.url,
            data=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
        )
        self.assertEqual(response.status_code, 400)

    # --- transport rules --------------------------------------------------

    def test_get_is_refused_because_no_stream_is_offered(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_delete_is_refused_because_there_are_no_sessions(self):
        self.assertEqual(self.client.delete(self.url).status_code, 405)

    def test_a_foreign_origin_is_rejected(self):
        response = self.rpc("ping", HTTP_ORIGIN="https://evil.example.com")
        self.assertEqual(response.status_code, 403)

    def test_our_own_origin_is_fine(self):
        response = self.rpc("ping", HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 200)

    # --- tools ------------------------------------------------------------

    def test_tools_list_is_scoped_to_the_caller(self):
        listed = self.result(self.rpc("tools/list"))["tools"]
        self.assertTrue(listed)
        expected = {descriptor["name"] for descriptor in tools.tool_descriptors(self.user)}
        self.assertEqual({descriptor["name"] for descriptor in listed}, expected)

    def test_a_read_only_key_is_offered_no_write_tools(self):
        listed = self.result(self.rpc("tools/list", key=self.raw_read_only_key))["tools"]
        self.assertTrue(listed)
        for descriptor in listed:
            self.assertTrue(descriptor["annotations"]["readOnlyHint"], descriptor["name"])

    def test_a_read_only_key_is_refused_a_write_even_if_it_asks_by_name(self):
        result = self.result(
            self.rpc("tools/call", {"name": "add_lot", "arguments": {"name": "guppies"}}, key=self.raw_read_only_key)
        )
        self.assertTrue(result["isError"])
        self.assertIn("read-only", result["content"][0]["text"])

    def test_calling_a_read_tool(self):
        result = self.result(self.rpc("tools/call", {"name": "my_context", "arguments": {}}))
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["username"], self.user.username)

    def test_a_tool_call_with_no_name_is_invalid_params(self):
        response = self.rpc("tools/call", {"arguments": {}})
        self.assertEqual(json.loads(response.content)["error"]["code"], protocol.INVALID_PARAMS)

    def test_a_key_cannot_reach_what_its_owner_cannot(self):
        """A key is a ceiling on its owner's permissions, never a way around them.

        ``user_with_no_lots`` is an ordinary participant in ``online_auction`` — not an admin, and
        not its creator — so an admin-only tool has to refuse. The key here allows writes, which is
        the case that would be a privilege escalation if the credential were doing the deciding
        instead of the resolver. It also proves the point about ``actions_for`` being a relevance
        filter and not the boundary: this tool is not in that user's ``tools/list`` at all, and
        naming it anyway still gets them nowhere.
        """
        raw, prefix, key_hash = UserAPIKey.generate()
        UserAPIKey.objects.create(
            user=self.user_with_no_lots, name="bidder key", prefix=prefix, key_hash=key_hash, allow_writes=True
        )
        listed = {t["name"] for t in self.result(self.rpc("tools/list", key=raw))["tools"]}
        self.assertNotIn("list_people", listed)
        result = self.result(
            self.rpc(
                "tools/call",
                {"name": "list_people", "arguments": {"auction": self.online_auction.slug}},
                key=raw,
            )
        )
        self.assertTrue(result["isError"], result["content"][0]["text"])
        self.assertIn("admins", result["content"][0]["text"])

    def test_the_same_tool_works_for_somebody_who_does_run_the_auction(self):
        """The other half of the previous test: the refusal is about permissions, not about MCP."""
        raw, prefix, key_hash = UserAPIKey.generate()
        UserAPIKey.objects.create(user=self.admin_user, name="admin key", prefix=prefix, key_hash=key_hash)
        result = self.result(
            self.rpc(
                "tools/call",
                {"name": "list_people", "arguments": {"auction": self.online_auction.slug}},
                key=raw,
            )
        )
        self.assertFalse(result["isError"], result["content"][0]["text"])


@isolated_cache("mcp-oauth")
class OAuthTests(StandardTestCase):
    """The other way in: an OAuth 2.1 access token from this site's own authorization server.

    Claude.ai, Claude Desktop, Claude mobile and Claude Code can only connect this way — they run
    an authorization-code flow and have nowhere to paste an API key. The flow itself is
    django-oauth-toolkit's and is its own project's to test; what is tested here is the part this
    codebase wrote: that a token authenticates, that its scopes are a ceiling, and that the
    discovery documents say the specific things Claude reads before it will even start.
    """

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        UserData.objects.update(use_llm_search=True)
        from oauth2_provider.models import get_access_token_model, get_application_model

        self.application = get_application_model().objects.create(
            name="Test client",
            client_type="public",
            authorization_grant_type="authorization-code",
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
            user=self.user,
        )
        self.AccessToken = get_access_token_model()

    def token_for(self, user, scope="read"):
        import secrets

        token = self.AccessToken.objects.create(
            user=user,
            application=self.application,
            token=secrets.token_hex(20),
            expires=timezone.now() + datetime.timedelta(hours=1),
            scope=scope,
        )
        return token.token

    def rpc(self, method, params=None, *, token, message_id=1):
        body = {"jsonrpc": "2.0", "method": method}
        if message_id is not None:
            body["id"] = message_id
        if params is not None:
            body["params"] = params
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )

    def result(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        self.assertNotIn("error", payload, payload)
        return payload["result"]

    def test_a_read_token_authenticates(self):
        result = self.result(self.rpc("initialize", {}, token=self.token_for(self.user)))
        self.assertEqual(result["protocolVersion"], protocol.LATEST_PROTOCOL_VERSION)

    def test_a_read_token_is_offered_no_write_tools(self):
        listed = self.result(self.rpc("tools/list", token=self.token_for(self.user)))["tools"]
        self.assertTrue(listed)
        for descriptor in listed:
            self.assertTrue(descriptor["annotations"]["readOnlyHint"], descriptor["name"])

    def test_the_write_scope_unlocks_the_write_tools(self):
        listed = self.result(self.rpc("tools/list", token=self.token_for(self.user, "read write")))["tools"]
        self.assertTrue(any(not d["annotations"]["readOnlyHint"] for d in listed))

    def test_a_token_with_no_read_scope_is_refused(self):
        """Reading is the floor. A token granted neither scope has nothing here it may do."""
        response = self.rpc("initialize", {}, token=self.token_for(self.user, "offline_access"))
        self.assertEqual(response.status_code, 401)

    def test_an_expired_token_is_refused(self):
        import secrets

        token = self.AccessToken.objects.create(
            user=self.user,
            application=self.application,
            token=secrets.token_hex(20),
            expires=timezone.now() - datetime.timedelta(minutes=1),
            scope="read write",
        )
        self.assertEqual(self.rpc("initialize", {}, token=token.token).status_code, 401)

    def test_a_token_with_nobody_behind_it_is_refused(self):
        """A client-credentials token has no user to act as, and every tool here acts as a person."""
        import secrets

        token = self.AccessToken.objects.create(
            user=None,
            application=self.application,
            token=secrets.token_hex(20),
            expires=timezone.now() + datetime.timedelta(hours=1),
            scope="read write",
        )
        self.assertEqual(self.rpc("initialize", {}, token=token.token).status_code, 401)

    def test_permissions_are_still_the_users_own(self):
        token = self.token_for(self.user_with_no_lots, "read write")
        result = self.result(
            self.rpc(
                "tools/call",
                {"name": "list_people", "arguments": {"auction": self.online_auction.slug}},
                token=token,
            )
        )
        self.assertTrue(result["isError"], result["content"][0]["text"])


class DiscoveryDocumentTests(StandardTestCase):
    """What Claude reads before it will start an OAuth flow at all.

    Every assertion here is a documented Claude requirement, and each one fails in a way that is
    hard to diagnose from the outside — the symptom is "couldn't reach the MCP server", with the
    authorization server seeing no traffic. See
    https://claude.com/docs/connectors/building/authentication.
    """

    def metadata(self, path):
        response = self.client.get(path, secure=True)
        self.assertEqual(response.status_code, 200, path)
        return json.loads(response.content)

    def test_the_401_points_at_the_resource_metadata(self):
        """Without this header a client has to guess at the well-known paths, or give up."""
        response = self.client.post("/mcp/", data="{}", content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 401)
        challenge = response["WWW-Authenticate"]
        self.assertIn('resource_metadata="', challenge)
        # The path-component form (RFC 9728), not the bare origin: the document it points at has
        # to name this endpoint, and the bare origin's names the whole site.
        self.assertIn("/.well-known/oauth-protected-resource/mcp", challenge)

    def test_the_resource_matches_the_endpoint_the_user_types_in(self):
        document = self.metadata("/.well-known/oauth-protected-resource/mcp")
        self.assertTrue(document["resource"].endswith("/mcp"))
        self.assertTrue(document["authorization_servers"])

    def test_cimd_is_advertised_in_the_two_places_claude_reads(self):
        """Claude picks CIMD only when *both* are present; miss one and it falls back to DCR."""
        document = self.metadata("/.well-known/oauth-authorization-server")
        self.assertIs(document["client_id_metadata_document_supported"], True)
        self.assertIn("none", document["token_endpoint_auth_methods_supported"])

    def test_pkce_s256_is_advertised_and_plain_is_not(self):
        document = self.metadata("/.well-known/oauth-authorization-server")
        self.assertEqual(document["code_challenge_methods_supported"], ["S256"])

    def test_dcr_is_offered_as_the_fallback(self):
        document = self.metadata("/.well-known/oauth-authorization-server")
        self.assertIn("registration_endpoint", document)

    def test_only_the_grants_this_server_exists_for_are_advertised(self):
        document = self.metadata("/.well-known/oauth-authorization-server")
        self.assertEqual(set(document["grant_types_supported"]), {"authorization_code", "refresh_token"})
        for retired in ("implicit", "password", "client_credentials"):
            self.assertNotIn(retired, document["grant_types_supported"])

    def test_registration_is_open_because_it_happens_before_anyone_signs_in(self):
        """DCR is the first call a client makes, with no user in the loop."""
        response = self.client.post(
            "/o/register/",
            data=json.dumps(
                {
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "client_name": "A test client",
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(json.loads(response.content)["client_id"])


@isolated_cache("mcp-opt-in")
class OptInTests(StandardTestCase):
    """``UserData.use_llm_search`` gates the whole feature, not just the page that explains it.

    The flag is off by default and flipped per user while this is in beta. A rollout control that
    covered only the instructions page would be decorative: a person could skip it, run whatever
    OAuth flow their client offers, and be connected.
    """

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        raw, prefix, key_hash = UserAPIKey.generate()
        self.raw_key = raw
        UserAPIKey.objects.create(user=self.user, name="k", prefix=prefix, key_hash=key_hash, allow_writes=True)

    def _opt_in(self, user, enabled=True):
        user.userdata.use_llm_search = enabled
        user.userdata.save()

    def rpc(self):
        return self.client.post(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )

    def test_a_key_belonging_to_somebody_not_opted_in_is_refused_with_a_403(self):
        """A 403, deliberately, and it is the status that matters more than the refusal.

        A 401 is an instruction to authenticate. A client that gets one here runs the whole OAuth
        flow again, is issued another perfectly valid token, presents it, and is refused again --
        a loop with no message in it anywhere and nothing on screen that says why. The 403 ends it
        and carries the sentence that says what to do about it.
        """
        self._opt_in(self.user, False)
        response = self.rpc()
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("WWW-Authenticate", response)
        self.assertIn("administrator", json.loads(response.content)["error"]["message"])

    def test_no_credential_at_all_is_still_a_401_with_a_challenge(self):
        """The other half: a 401 is what *starts* an OAuth flow, so it has to survive the change."""
        response = self.client.post(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response)

    def test_the_same_key_works_once_they_are_opted_in(self):
        self._opt_in(self.user, True)
        self.assertEqual(self.rpc().status_code, 200)

    def test_turning_the_flag_back_off_disconnects_them(self):
        self._opt_in(self.user, True)
        self.assertEqual(self.rpc().status_code, 200)
        self._opt_in(self.user, False)
        self.assertEqual(self.rpc().status_code, 403)


class ConnectPageTests(StandardTestCase):
    """The page that explains how to connect, and the same gate on it."""

    url = "/account/api-keys/"

    def test_somebody_not_opted_in_gets_the_explanation_not_a_403(self):
        """This is the one page that says what connecting an assistant is.

        A permission-denied page hands the person nothing to read and nothing to forward, which
        is the opposite of what somebody who has just found out they can't have it yet needs.
        """
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("isn't switched on for your account yet", body)
        # ...and nothing that issues or ends a credential is on the page.
        self.assertNotIn("Create key", body)
        self.assertNotIn("Disconnect", body)

    def test_creating_a_key_is_refused_too(self):
        """The explanation degrades; the credential form refuses."""
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url, {"name": "sneaky"}).status_code, 403)
        self.assertFalse(UserAPIKey.objects.filter(name="sneaky").exists())

    def test_it_renders_for_somebody_opted_in(self):
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # The address, and the two assistants the steps are written for.
        self.assertIn("/mcp", body)
        self.assertIn("Add a custom connector", body)
        self.assertIn("developer mode", body)
        # The key form is behind a collapse until somebody asks for it, but it is on the page.
        self.assertIn("Create key", body)

    def test_creating_a_key_shows_it_exactly_once(self):
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.client.post(self.url, {"name": "My script"})
        key = UserAPIKey.objects.get(name="My script")
        self.assertFalse(key.allow_writes, "a new key is read-only unless asked otherwise")
        first = self.client.get(self.url).content.decode()
        self.assertIn(key.prefix, first)
        self.assertIn("only time it will ever be shown", first)
        # Reloading must not put the secret back on screen.
        self.assertNotIn("only time it will ever be shown", self.client.get(self.url).content.decode())

    def test_revoking_a_key(self):
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.client.post(self.url, {"name": "Doomed"})
        key = UserAPIKey.objects.get(name="Doomed")
        self.client.post(self.url, {"revoke": key.pk})
        key.refresh_from_db()
        self.assertFalse(key.is_active)

    def test_somebody_elses_key_cannot_be_revoked(self):
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        raw, prefix, key_hash = UserAPIKey.generate()
        theirs = UserAPIKey.objects.create(user=self.admin_user, name="Theirs", prefix=prefix, key_hash=key_hash)
        self.client.force_login(self.user)
        self.client.post(self.url, {"revoke": theirs.pk})
        theirs.refresh_from_db()
        self.assertTrue(theirs.is_active)


@isolated_cache("mcp-oauth-optin")
class OAuthOptInTests(StandardTestCase):
    """The same rule, on the credential Claude's own surfaces actually use.

    The key path and the token path have to agree here, and the token path is the one that loops:
    an OAuth client answered with a 401 goes and gets another token.
    """

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        from oauth2_provider.models import get_access_token_model, get_application_model

        self.application = get_application_model().objects.create(
            name="Test client",
            client_type="public",
            authorization_grant_type="authorization-code",
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
        )
        self.AccessToken = get_access_token_model()

    def token_for(self, user, scope="read"):
        import secrets

        return self.AccessToken.objects.create(
            user=user,
            application=self.application,
            token=secrets.token_hex(20),
            expires=timezone.now() + datetime.timedelta(hours=1),
            scope=scope,
        ).token

    def ping(self, token):
        return self.client.post(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )

    def test_a_token_for_somebody_not_opted_in_gets_a_403_not_a_reauth_loop(self):
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        response = self.ping(self.token_for(self.user))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("WWW-Authenticate", response)

    def test_a_token_for_somebody_opted_in_still_works(self):
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        self.assertEqual(self.ping(self.token_for(self.user)).status_code, 200)


class ConnectedAppsTests(StandardTestCase):
    """The list of what is signed in, on the page that explains signing in.

    Signing in is how almost everybody connects, and until this list existed the page described a
    connection it could not show and offered no way to end: "revoke your key" is no help to
    somebody who never made a key, and the only other route was the Django admin.
    """

    url = "/account/api-keys/"

    def setUp(self):
        super().setUp()
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        from oauth2_provider.models import get_access_token_model, get_application_model

        self.application = get_application_model().objects.create(
            name="Claude",
            client_type="public",
            authorization_grant_type="authorization-code",
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
        )
        self.AccessToken = get_access_token_model()
        self.client.force_login(self.user)

    def token_for(self, user, scope="read write"):
        import secrets

        return self.AccessToken.objects.create(
            user=user,
            application=self.application,
            token=secrets.token_hex(20),
            expires=timezone.now() + datetime.timedelta(hours=1),
            scope=scope,
        )

    def test_a_connected_assistant_is_listed(self):
        self.token_for(self.user)
        body = self.client.get(self.url).content.decode()
        self.assertIn("Claude", body)
        self.assertIn("Disconnect", body)

    def test_disconnecting_removes_every_token(self):
        self.token_for(self.user)
        self.client.post(self.url, {"disconnect": self.application.pk})
        self.assertEqual(self.AccessToken.objects.filter(user=self.user).count(), 0)

    def test_disconnecting_does_not_touch_anybody_else(self):
        theirs = self.token_for(self.admin_user)
        self.token_for(self.user)
        self.client.post(self.url, {"disconnect": self.application.pk})
        self.assertTrue(self.AccessToken.objects.filter(pk=theirs.pk).exists())

    def test_a_hand_written_application_id_is_answered_not_crashed(self):
        response = self.client.post(self.url, {"disconnect": "not-a-number"}, follow=True)
        self.assertEqual(response.status_code, 200)

    def test_a_key_can_be_given_an_end_date(self):
        self.client.post(self.url, {"name": "Ninety days", "expires_in": "90"})
        key = UserAPIKey.objects.get(name="Ninety days")
        self.assertIsNotNone(key.expires_at)
        self.assertTrue(key.is_usable)

    def test_a_key_with_no_end_date_still_never_expires(self):
        self.client.post(self.url, {"name": "Forever"})
        self.assertIsNone(UserAPIKey.objects.get(name="Forever").expires_at)

    def test_an_expired_key_stops_working(self):
        raw, prefix, key_hash = UserAPIKey.generate()
        UserAPIKey.objects.create(
            user=self.user,
            name="Lapsed",
            prefix=prefix,
            key_hash=key_hash,
            expires_at=timezone.now() - datetime.timedelta(days=1),
        )
        response = self.client.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )
        self.assertEqual(response.status_code, 401)


@isolated_cache("mcp-dcr")
class AuthorizationServerHardeningTests(StandardTestCase):
    """Two things django-oauth-toolkit leaves open that a public site cannot.

    Both are the toolkit behaving correctly for the deployment it assumes -- a service whose only
    users are its own developers -- and wrongly for one where anybody can sign up.
    """

    def test_the_application_pages_are_not_open_to_every_signed_in_member(self):
        """``/o/applications/`` registers OAuth clients. It belongs to whoever runs the server."""
        self.client.force_login(self.user)
        for path in ("/o/applications/", "/o/applications/register/"):
            response = self.client.get(path)
            self.assertIn(response.status_code, (302, 403), path)

    def test_a_superuser_can_still_reach_them(self):
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save()
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get("/o/applications/").status_code, 200)

    def test_dynamic_registration_is_rate_limited_per_address(self):
        """DCR has to stay open to anonymous callers, which makes the table writable by strangers."""
        from auctions.mcp import auth as mcp_auth

        body = json.dumps(
            {
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "client_name": "A test client",
                "token_endpoint_auth_method": "none",
            }
        )
        statuses = []
        for _ in range(mcp_auth.DCR_REGISTRATIONS_PER_HOUR + 2):
            statuses.append(
                self.client.post("/o/register/", data=body, content_type="application/json", secure=True).status_code
            )
        self.assertIn(201, statuses, "a real client must still be able to register")
        self.assertEqual(statuses[-1], 429, "an unbounded registration endpoint is an unbounded table")


class ClientMetadataDocumentTests(SimpleTestCase):
    """CIMD, which is the only way claude.ai can connect.

    The bug these cover was found on staging, not here: every attempt to connect died on
    ``invalid_request: Invalid client_id parameter value`` with nothing in the message to act on,
    because Claude's metadata document names a grant type this server doesn't offer and the
    toolkit refuses any document that names more than one.
    """

    #: What claude.ai actually serves, fetched from the live document.
    CLAUDE_DOCUMENT = {
        "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        "client_name": "Claude",
        "client_uri": "https://claude.ai",
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    def test_claudes_document_maps_to_a_single_grant(self):
        from oauth2_provider.cimd import _resolve_grant_type

        from auctions.mcp.cimd import narrow_grant_types

        narrowed = narrow_grant_types(self.CLAUDE_DOCUMENT)
        self.assertEqual(narrowed["grant_types"], ["authorization_code", "refresh_token"])
        # This is the call that used to raise, which is what made Claude look like an unknown client.
        self.assertEqual(_resolve_grant_type(narrowed["grant_types"]), "authorization-code")

    def test_it_narrows_rather_than_widens(self):
        """A document that asks for nothing we support still fails, which is the right answer."""
        from auctions.mcp.cimd import narrow_grant_types

        narrowed = narrow_grant_types({"grant_types": ["implicit", "password"]})
        self.assertEqual(narrowed["grant_types"], [])

    def test_a_document_with_nothing_to_drop_is_passed_through_untouched(self):
        from auctions.mcp.cimd import narrow_grant_types

        document = {"grant_types": ["authorization_code", "refresh_token"], "client_name": "Fine"}
        self.assertIs(narrow_grant_types(document), document)

    def test_the_supported_set_is_read_off_the_discovery_document(self):
        """Two lists that have to agree are one list, or they drift."""
        from auctions.mcp.cimd import supported_grant_types

        advertised = set(settings.OAUTH2_PROVIDER["OAUTH2_GRANT_TYPES_SUPPORTED"])
        self.assertTrue(advertised.issubset(supported_grant_types()))
        self.assertNotIn("urn:ietf:params:oauth:grant-type:jwt-bearer", supported_grant_types())

    def test_the_fetcher_narrows_what_it_fetched(self):
        from auctions.mcp.cimd import ClientMetadataFetcher

        with patch("oauth2_provider.cimd.SafeMetadataFetcher.fetch", return_value=(self.CLAUDE_DOCUMENT, 300)):
            metadata, max_age = ClientMetadataFetcher().fetch("https://claude.ai/oauth/mcp-oauth-client-metadata")
        self.assertEqual(max_age, 300)
        self.assertEqual(metadata["grant_types"], ["authorization_code", "refresh_token"])

    def test_the_deployment_actually_uses_it(self):
        """Writing the class is half of it; the setting is the half that fails silently."""
        self.assertEqual(
            settings.OAUTH2_PROVIDER["CIMD_METADATA_FETCHER"],
            "auctions.mcp.cimd.ClientMetadataFetcher",
        )


class InactiveAccountTests(StandardTestCase):
    """A credential outliving the account behind it.

    On the web ``is_active=False`` stops somebody at the login form. Over ``/mcp/`` nothing looked
    at the user at all, so deleting an account or banning somebody left whatever they had connected
    still acting as them.
    """

    def setUp(self):
        super().setUp()
        self.user.userdata.use_llm_search = True
        self.user.userdata.save()
        raw, prefix, key_hash = UserAPIKey.generate()
        self.raw_key = raw
        UserAPIKey.objects.create(user=self.user, name="a key", prefix=prefix, key_hash=key_hash, allow_writes=True)

    def _rpc(self):
        return self.client.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
        )

    def test_a_live_account_works(self):
        self.assertEqual(self._rpc().status_code, 200)

    def test_a_deactivated_account_is_refused(self):
        self.user.is_active = False
        self.user.save()
        response = self._rpc()
        self.assertEqual(response.status_code, 403)
        # A 403 and not a 401: there is no credential they could go and fetch that would work.
        self.assertNotIn("WWW-Authenticate", response)


class ResultUrlTests(StandardTestCase):
    """A link an agent hands to a person has to be a link they can follow."""

    def test_relative_urls_are_made_absolute(self):
        from auctions.mcp import tools as mcp_tools

        payload = {
            "url": "/lots/all/?q=shrimp",
            "followups": [{"label": "A lot", "url": "/lots/1/"}, {"label": "Elsewhere", "url": "https://example.com/"}],
            "count": 3,
        }
        absolute = mcp_tools._absolute(payload, lambda path: "https://auction.test" + path)
        self.assertEqual(absolute["url"], "https://auction.test/lots/all/?q=shrimp")
        self.assertEqual(absolute["followups"][0]["url"], "https://auction.test/lots/1/")
        self.assertEqual(absolute["followups"][1]["url"], "https://example.com/", "already absolute")
        self.assertEqual(absolute["count"], 3)
