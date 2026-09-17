"""Tests for the MCP tool catalogue."""

import datetime
import json
import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from auctions import palette_actions
from auctions.mcp import icons, prompts, protocol, resources, tools
from auctions.models import UserAPIKey, UserData
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase

#: Description patterns a connector review rejects: telling the model how to behave. Sibling
#: disambiguation ("use describe_lot instead") is allowed.
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
            # Top-level ``title`` wins, so sending both repeats it.
            self.assertNotIn("title", descriptor["annotations"], f"{name} sends its title twice")

    def test_annotations_declare_the_read_write_split(self):
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            action = palette_actions.ACTIONS[name]
            self.assertIsInstance(annotations["readOnlyHint"], bool)
            self.assertEqual(annotations["readOnlyHint"], action.danger != palette_actions.DANGER_CONFIRM)
            self.assertEqual(annotations["openWorldHint"], action.open_world, name)

    def test_only_the_source_reader_reaches_outside_this_site(self):
        """Only ``read_source`` sets ``openWorldHint``: it fetches the published source code."""
        reaching = {name for name, built in self.by_name.items() if built["annotations"]["openWorldHint"]}
        self.assertEqual(reaching, {"read_source"})

    def test_a_write_says_whether_it_destroys_and_whether_it_repeats(self):
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            if annotations["readOnlyHint"]:
                continue
            self.assertIsInstance(annotations["destructiveHint"], bool, f"{name} does not say if it destroys")
            # Only sent when true, since false is the default.
            if tools.idempotent(palette_actions.ACTIONS[name]):
                self.assertIs(annotations["idempotentHint"], True, f"{name} repeats but does not say so")
            else:
                self.assertNotIn("idempotentHint", annotations, f"{name} says the default out loud")

    def test_a_read_carries_neither_hint(self):
        """Reads carry neither destructive nor idempotent hint; tools/list costs context every session."""
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            if not annotations["readOnlyHint"]:
                continue
            self.assertNotIn("destructiveHint", annotations, f"{name} reads; the hint means nothing")
            self.assertNotIn("idempotentHint", annotations, f"{name} reads; the hint means nothing")

    def test_every_parameter_declares_its_type(self):
        for name, action in palette_actions.ACTIONS.items():
            for param, description in action.params.items():
                schema, _ = tools.param_schema(description)
                self.assertIn(
                    "type",
                    schema,
                    f"{name}.{param} does not open with '<type>, required|optional' — "
                    f"got {description[:60]!r}. Without it the parameter has no JSON Schema type.",
                )

    def test_no_tool_advertises_a_lots_primary_key(self):
        """No tool advertises a lot's primary key; lots are named by ``lot_number_display``.

        ``lot_id`` stays a resolver alias and is stripped from results (``mcp.tools._INTERNAL_RESULT_KEYS``).
        ``image_id`` is the exception: a photo has no printed number.
        """
        for name, action in palette_actions.ACTIONS.items():
            self.assertNotIn("lot_id", action.params, f"{name} advertises a lot's primary key; take the lot number")
            for param in action.params:
                self.assertNotIn(param, {"pk", "id"}, f"{name}.{param} is a raw primary key")

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
        for name, action in palette_actions.ACTIONS.items():
            properties = self.by_name[name]["inputSchema"]["properties"]
            for param, prose in action.params.items():
                match = tools._PARAM_PREFIX.match(prose)
                remainder = prose[match.end() :].lstrip(" ,.").strip() if match else prose
                if not remainder:
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
        # Actionable: says what to send instead.
        self.assertIn("lot number", self._text(result))

    def test_finding_nothing_is_not_an_error(self):
        result = tools.call_tool(self._request_for(self.user), "describe_lot", {"lot": "no such lot anywhere"})
        self.assertFalse(result["isError"])
        self.assertIs(json.loads(self._text(result))["found"], False)

    def test_an_ambiguous_answer_asks_the_caller_to_narrow_it(self):
        result = tools.call_tool(self._request_for(self.user), "go_to_page", {})
        self.assertTrue(result["isError"])
        self.assertTrue(self._text(result).strip())

    def _keys_naming_a_primary_key(self, node, found=None):
        """Every key at any depth naming a primary key.

        Structural, because a low pk collides with lot numbers and prices in a JSON substring search.
        """
        found = [] if found is None else found
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"id", "pk", "lot_id", "lot_pk"} or key.endswith("_pk"):
                    found.append(key)
                self._keys_naming_a_primary_key(value, found)
        elif isinstance(node, list):
            for item in node:
                self._keys_naming_a_primary_key(item, found)
        return found

    def test_no_result_hands_out_a_lots_primary_key(self):
        """Primary keys are stripped at any depth, including in list rows."""
        for tool, arguments in (
            ("find_lot", {"query": self.lot.lot_name}),
            ("describe_lot", {"lot": self.lot.lot_name}),
            ("watch_lot", {"lot": self.lot.lot_name}),
        ):
            result = tools.call_tool(self._request_for(self.user), tool, arguments)
            body = self._text(result)
            self.assertNotIn("lot_id", body, f"{tool} handed out a lot's primary key")
            self.assertEqual(
                self._keys_naming_a_primary_key(result.get("structuredContent") or {}),
                [],
                f"{tool}'s structured answer names a primary key",
            )

    def test_a_lot_is_still_named_by_the_number_on_its_label(self):
        result = tools.call_tool(self._request_for(self.user), "find_lot", {"query": self.lot.lot_name})
        payload = json.loads(self._text(result))
        self.assertEqual(payload["lots"][0]["lot_number"], self.lot.lot_number_display)

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
        result = tools.call_tool(self._request_for(self.user), "my_context", {})
        self.assertEqual(result["structuredContent"], json.loads(self._text(result)))
        self.assertEqual(result["structuredContent"]["username"], self.user.username)

    def test_the_structure_and_the_text_are_always_the_same_answer(self):
        request = self._request_for(self.user)
        with patch.object(tools, "MAX_RESULT_CHARS", 200):
            result = tools.call_tool(request, "my_context", {})
        self.assertEqual(result["structuredContent"], json.loads(self._text(result)))
        self.assertIn("too big", result["structuredContent"]["error"])

    def test_a_plain_sentence_error_carries_no_structure(self):
        result = tools.call_tool(self._request_for(self.user), "add_lot", {"name": "guppies"}, writes=False)
        self.assertTrue(result["isError"])
        self.assertNotIn("structuredContent", result)

    def test_a_disambiguation_carries_structure_too(self):
        result = tools.call_tool(self._request_for(self.user), "watch_lot", {})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "needs_more_information")
        self.assertEqual(result["structuredContent"], json.loads(self._text(result)))

    def test_everything_in_a_result_survives_json(self):
        result = tools.call_tool(self._request_for(self.user), "describe_lot", {"lot": str(self.lot.lot_name)})
        json.dumps(result)

    def test_a_result_is_bounded(self):
        long_result = {"ok": True, "summary": "x" * (tools.MAX_RESULT_CHARS * 2)}
        text = tools._text(tools._payload(long_result))
        self.assertLess(len(text), tools.MAX_RESULT_CHARS)
        self.assertIn("too big", text)

    def test_a_result_that_does_not_fit_is_still_valid_json(self):
        """A truncated result is still valid JSON, with the "narrow the query" note intact."""
        long_result = {"ok": True, "summary": "fine", "rows": ["x" * 200] * 500}
        parsed = json.loads(tools._text(tools._payload(long_result)))
        self.assertEqual(parsed["summary"], "fine")
        self.assertIn("limit and offset", parsed["what_to_do"])


@isolated_cache("mcp-endpoint")
class EndpointTests(StandardTestCase):
    """The HTTP statuses the transport spec requires, tested through the URL."""

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        # Per-user opt-in; OptInTests covers the flag itself.
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

    def test_no_credential_is_a_401_that_says_where_to_authenticate(self):
        response = self.rpc("initialize", key="")
        self.assertEqual(response.status_code, 401)
        challenge = response["WWW-Authenticate"]
        self.assertTrue(challenge.startswith("Bearer "))
        # The pointer saves the client guessing well-known paths.
        self.assertIn("resource_metadata=", challenge)
        self.assertIn("/.well-known/oauth-protected-resource", challenge)

    def test_a_session_cookie_is_not_a_credential(self):
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

    def test_initialize_negotiates_and_advertises_only_what_exists(self):
        result = self.result(self.rpc("initialize", {"protocolVersion": protocol.LATEST_PROTOCOL_VERSION}))
        self.assertEqual(result["protocolVersion"], protocol.LATEST_PROTOCOL_VERSION)
        # No logging, sampling or elicitation: those need the server to speak first. See
        # docs/mcp_next.md.
        self.assertEqual(set(result["capabilities"]), {"tools", "resources", "prompts", "completions"})
        self.assertTrue(result["serverInfo"]["name"])
        self.assertTrue(result["instructions"].strip())
        self.assertTrue(result["serverInfo"]["icons"])
        self.assertTrue(result["serverInfo"]["websiteUrl"].startswith("https://"))

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
        # Something this server will never implement.
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
        """A key never exceeds its owner's permissions, even for a tool not in their tools/list."""
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
    """OAuth 2.1 tokens from this site's authorization server, which is how Claude's apps connect.

    Tests the token authenticates, scopes are a ceiling, and discovery documents say what Claude needs.
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
    """What Claude reads before starting an OAuth flow; failures look like "couldn't reach the MCP server".

    See https://claude.com/docs/connectors/building/authentication.
    """

    def metadata(self, path):
        response = self.client.get(path, secure=True)
        self.assertEqual(response.status_code, 200, path)
        return json.loads(response.content)

    def test_the_401_points_at_the_resource_metadata(self):
        response = self.client.post("/mcp/", data="{}", content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 401)
        challenge = response["WWW-Authenticate"]
        self.assertIn('resource_metadata="', challenge)
        # The RFC 9728 path form, which names this endpoint.
        self.assertIn("/.well-known/oauth-protected-resource/mcp", challenge)

    def test_the_resource_matches_the_endpoint_the_user_types_in(self):
        document = self.metadata("/.well-known/oauth-protected-resource/mcp")
        self.assertTrue(document["resource"].endswith("/mcp"))
        self.assertTrue(document["authorization_servers"])

    def test_cimd_is_advertised_in_the_two_places_claude_reads(self):
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
    """No per-user opt-in on this endpoint, but ``is_active`` is still checked on every credential."""

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

    def test_a_key_works_without_the_command_palette_flag(self):
        self._opt_in(self.user, False)
        self.assertEqual(self.rpc().status_code, 200)

    def test_no_credential_at_all_is_still_a_401_with_a_challenge(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response)

    def test_the_flag_makes_no_difference_either_way(self):
        self._opt_in(self.user, True)
        self.assertEqual(self.rpc().status_code, 200)
        self._opt_in(self.user, False)
        self.assertEqual(self.rpc().status_code, 200)

    def test_a_deactivated_account_is_still_a_403_and_not_a_reauth_loop(self):
        """A deactivated account gets a 403: a 401 would start an endless re-auth loop."""
        self.user.is_active = False
        self.user.save()
        response = self.rpc()
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("WWW-Authenticate", response)
        self.assertIn("no longer active", json.loads(response.content)["error"]["message"])


class ConnectPageTests(StandardTestCase):
    """The page that explains how to connect. Open to everybody signed in."""

    url = "/ai/"

    def test_the_command_palette_flag_does_not_gate_this_page(self):
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Create key", body)
        self.assertNotIn("isn't switched on for your account yet", body)

    def test_creating_a_key_works_without_the_flag_too(self):
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.client.post(self.url, {"name": "no-flag-needed"})
        self.assertTrue(UserAPIKey.objects.filter(user=self.user, name="no-flag-needed").exists())

    def test_signing_in_is_still_required(self):
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_it_renders_the_connection_instructions(self):
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
    """The same rule for OAuth tokens, where a 401 would loop."""

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

    def test_a_token_works_without_the_command_palette_flag(self):
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        self.assertEqual(self.ping(self.token_for(self.user)).status_code, 200)

    def test_a_token_for_a_deactivated_account_is_a_403_not_a_reauth_loop(self):
        token = self.token_for(self.user)
        self.user.is_active = False
        self.user.save()
        response = self.ping(token)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("WWW-Authenticate", response)


class ConnectedAppsTests(StandardTestCase):
    """The list of connected apps on /ai/, with a way to disconnect."""

    url = "/ai/"

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
    """Two things django-oauth-toolkit leaves open that a public signup site can't."""

    def test_the_application_pages_are_not_open_to_every_signed_in_member(self):
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
    """CIMD, the only way claude.ai connects.

    Claude's metadata document names more than one grant type, which the toolkit refused with
    ``Invalid client_id parameter value``.
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
        # This used to raise.
        self.assertEqual(_resolve_grant_type(narrowed["grant_types"]), "authorization-code")

    def test_it_narrows_rather_than_widens(self):
        from auctions.mcp.cimd import narrow_grant_types

        narrowed = narrow_grant_types({"grant_types": ["implicit", "password"]})
        self.assertEqual(narrowed["grant_types"], [])

    def test_a_document_with_nothing_to_drop_is_passed_through_untouched(self):
        from auctions.mcp.cimd import narrow_grant_types

        document = {"grant_types": ["authorization_code", "refresh_token"], "client_name": "Fine"}
        self.assertIs(narrow_grant_types(document), document)

    def test_the_supported_set_is_read_off_the_discovery_document(self):
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
        self.assertEqual(
            settings.OAUTH2_PROVIDER["CIMD_METADATA_FETCHER"],
            "auctions.mcp.cimd.ClientMetadataFetcher",
        )


class InactiveAccountTests(StandardTestCase):
    """Credentials stop working when the account is inactive."""

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
        # 403, not 401: no credential would work.
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

    def test_a_key_that_ends_in_url_is_a_url_too(self):
        """Keys ending in ``url`` are made absolute too, not just ``url`` itself."""
        from auctions.mcp import tools as mcp_tools

        payload = {"membership": {"renew_url": "/clubs/x/pay/", "barcode_url": "https://auction.test/b.svg"}}
        absolute = mcp_tools._absolute(payload, lambda path: "https://auction.test" + path)
        self.assertEqual(absolute["membership"]["renew_url"], "https://auction.test/clubs/x/pay/")
        self.assertEqual(absolute["membership"]["barcode_url"], "https://auction.test/b.svg", "already absolute")

    def test_the_membership_card_is_where_that_actually_bit(self):
        from auctions.mcp import tools as mcp_tools

        self.assertTrue(mcp_tools._is_url_key("renew_url"))
        self.assertTrue(mcp_tools._is_url_key("url"))
        self.assertFalse(mcp_tools._is_url_key("urls"))
        self.assertFalse(mcp_tools._is_url_key("summary"))


class IconTests(SimpleTestCase):
    """Every primitive that may carry an icon has one. See :mod:`auctions.mcp.icons`."""

    def setUp(self):
        self.descriptors = tools.tool_descriptors(None)

    def icon_file(self, name):
        """The icon's filename, hashed where statics are collected (see fishauctions/static_storage.py)."""
        return staticfiles_storage.url(f"mcp/{name}.svg").rsplit("/", 1)[-1]

    def test_every_tool_carries_exactly_one_icon(self):
        for descriptor in self.descriptors:
            found = descriptor.get("icons")
            self.assertTrue(found, f"{descriptor['name']} has no icon")
            self.assertEqual(len(found), 1, f"{descriptor['name']} sends more than one")

    def test_an_icon_is_an_absolute_url_a_host_can_fetch(self):
        for descriptor in self.descriptors:
            src = descriptor["icons"][0]["src"]
            self.assertTrue(src.startswith("https://"), f"{descriptor['name']}: {src} is not fetchable")
            self.assertIn("/static/mcp/", src)
            self.assertEqual(descriptor["icons"][0]["mimeType"], icons.SVG)

    def test_no_sizes_on_a_scalable_icon(self):
        """No ``sizes`` on SVG icons; it's paid for in context every session."""
        for descriptor in self.descriptors:
            self.assertNotIn("sizes", descriptor["icons"][0], descriptor["name"])

    def test_the_five_are_all_that_are_used(self):
        used = {descriptor["icons"][0]["src"].rsplit("/", 1)[-1] for descriptor in self.descriptors}
        self.assertEqual(
            used, {self.icon_file(name) for name in (icons.READ, icons.GO, icons.AUCTION, icons.CLUB, icons.EDIT)}
        )

    def test_a_read_is_a_magnifier_and_a_write_is_not(self):
        by_name = {descriptor["name"]: descriptor["icons"][0]["src"] for descriptor in self.descriptors}
        self.assertIn(self.icon_file(icons.READ), by_name["list_lots"])
        self.assertIn(self.icon_file(icons.GO), by_name["go_to_page"])
        self.assertIn(self.icon_file(icons.AUCTION), by_name["check_in"])
        self.assertIn(self.icon_file(icons.CLUB), by_name["add_club_member"])

    def test_the_icon_files_are_really_there(self):
        from pathlib import Path

        from django.conf import settings

        root = Path(settings.BASE_DIR) / "auctions" / "static" / "mcp"
        for name in (icons.READ, icons.GO, icons.AUCTION, icons.CLUB, icons.EDIT):
            self.assertTrue((root / f"{name}.svg").exists(), f"{name}.svg is missing")

    def test_the_prompts_and_the_resource_templates_carry_them_too(self):
        for descriptor in prompts.descriptors():
            self.assertTrue(descriptor.get("icons"), f"prompt {descriptor['name']} has no icon")
        for descriptor in resources.template_descriptors() + resources.fixed_descriptors():
            self.assertTrue(descriptor.get("icons"), f"resource {descriptor['name']} has no icon")

    def test_a_widget_document_deliberately_has_none(self):
        """Widget documents have no icon."""
        from auctions.mcp import widgets

        for descriptor in widgets.resource_descriptors():
            self.assertNotIn("icons", descriptor, f"{descriptor['name']} grew an icon")

    def test_they_are_a_small_fraction_of_the_catalogue(self):
        with_icons = len(json.dumps({"tools": self.descriptors}))
        without = len(json.dumps({"tools": [{k: v for k, v in t.items() if k != "icons"} for t in self.descriptors]}))
        self.assertLess(with_icons - without, without * 0.15, "icons are more than 15% of tools/list")


class ResourceLinkTests(StandardTestCase):
    """``resource_link`` blocks, built from ``palette_actions.KEY_ABOUT`` rather than sniffed from results."""

    def setUp(self):
        super().setUp()
        UserData.objects.update(use_llm_search=True)

    def _request_for(self, user):
        request = RequestFactory().post("/mcp/")
        request.user = user
        return request

    def _links(self, result):
        return [block for block in result["content"] if block.get("type") == "resource_link"]

    def test_a_read_about_an_auction_links_to_the_auction(self):
        result = tools.call_tool(self._request_for(self.user), "list_lots", {"auction": self.online_auction.slug})
        links = self._links(result)
        self.assertIn(f"auction://{self.online_auction.slug}", [link["uri"] for link in links])

    def test_a_link_is_a_uri_this_server_really_publishes(self):
        result = tools.call_tool(self._request_for(self.user), "my_context", {})
        for link in self._links(result):
            self.assertIsNotNone(resources.match(link["uri"]), f"{link['uri']} matches no template")
            self.assertEqual(link["type"], "resource_link")
            self.assertTrue(link["name"])
            self.assertTrue(link["title"])

    def test_a_tool_never_links_to_its_own_answer(self):
        links = resources.links_for("describe_lot", {"auction": "spring", "lot": "14"})
        uris = [link["uri"] for link in links]
        self.assertNotIn("lot://spring/14", uris)
        self.assertIn("auction://spring", uris, "the auction it is in is the one worth having")

    def test_what_goes_in_place_of_a_dropped_self_link_is_what_sits_underneath(self):
        uris = [link["uri"] for link in resources.links_for("describe_auction", {"auction": "spring"})]
        self.assertEqual(uris, ["auction://spring/lots", "auction://spring/people", "auction://spring/history"])
        self.assertEqual(
            [link["uri"] for link in resources.links_for("describe_club", {"club": "nec"})],
            ["club://nec/events", "club://nec/history"],
        )

    def test_a_tool_that_did_not_answer_the_top_level_thing_gets_only_that(self):
        uris = [link["uri"] for link in resources.links_for("list_lots", {"auction": "spring"})]
        self.assertEqual(uris, ["auction://spring"])

    def test_a_lot_result_links_to_the_lot_and_the_auction(self):
        links = resources.links_for("edit_lot", {"auction": "spring", "lot": "14"})
        self.assertEqual([link["uri"] for link in links], ["lot://spring/14", "auction://spring"])

    def test_nothing_to_link_is_no_blocks_rather_than_an_empty_one(self):
        self.assertEqual(resources.links_for("my_context", {}), [])
        self.assertEqual(resources.links_for("my_context", None), [])

    def test_the_number_of_links_is_bounded(self):
        many = {"auctions": [f"auction-{index}" for index in range(50)]}
        self.assertEqual(len(resources.links_for("my_context", many)), resources.MAX_LINKS)

    def test_a_uri_this_server_cannot_build_is_dropped_rather_than_sent(self):
        self.assertEqual(resources.links_for("my_context", {"auction": "one/two/three"}), [])

    def test_the_bookkeeping_key_is_never_in_the_answer(self):
        result = tools.call_tool(self._request_for(self.user), "my_context", {})
        self.assertNotIn(palette_actions.KEY_ABOUT, json.loads(result["content"][0]["text"]))
        self.assertNotIn(palette_actions.KEY_ABOUT, result["structuredContent"])


class ConfirmationTierTests(SimpleTestCase):
    """``asks_first`` is the palette's countdown, and it is not the read/write split."""

    def test_checking_someone_in_does_not_ask_first(self):
        self.assertFalse(palette_actions.get_action("check_in").asks_first)

    def test_it_is_still_a_write_everywhere_that_matters(self):
        action = palette_actions.get_action("check_in")
        self.assertEqual(action.danger, palette_actions.DANGER_CONFIRM)
        self.assertFalse(tools.read_only(action))
        descriptor = tools.descriptor(action)
        self.assertFalse(descriptor["annotations"]["readOnlyHint"])
        read_only_catalogue = {one["name"] for one in tools.tool_descriptors(None, writes=False)}
        self.assertNotIn("check_in", read_only_catalogue, "a read-only credential must not be offered it")

    def test_only_a_reversible_write_may_skip_the_countdown(self):
        for action in palette_actions.ACTIONS.values():
            if action.asks_first:
                continue
            self.assertEqual(action.danger, palette_actions.DANGER_CONFIRM, f"{action.name} is not a write")
            self.assertFalse(action.destructive, f"{action.name} destroys something and must ask")
            self.assertTrue(tools.idempotent(action), f"{action.name} is not safe to repeat")

    def test_everything_else_still_asks(self):
        """Only a short, deliberate list of tools skips confirmation."""
        skipping = {name for name, action in palette_actions.ACTIONS.items() if not action.asks_first}
        self.assertEqual(
            skipping,
            {"check_in", "watch_lot", "review_points", "set_my_auction", "set_my_club"},
            "a new action opted out of the countdown",
        )

    def test_a_points_decision_can_always_be_taken_back_by_the_same_tool(self):
        action = palette_actions.get_action("review_points")
        self.assertIn("undo", action.params["decision"])
        self.assertFalse(action.destructive)
