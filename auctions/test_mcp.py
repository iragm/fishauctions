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
            self.assertEqual(descriptor["annotations"]["title"], descriptor["title"])

    def test_annotations_declare_the_read_write_split(self):
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            action = palette_actions.ACTIONS[name]
            self.assertIsInstance(annotations["readOnlyHint"], bool)
            self.assertIsInstance(annotations["destructiveHint"], bool)
            self.assertEqual(annotations["readOnlyHint"], action.danger != palette_actions.DANGER_CONFIRM)
            self.assertFalse(annotations["openWorldHint"], f"{name} reaches outside this site?")

    def test_nothing_is_read_only_and_destructive_at_once(self):
        for name, descriptor in self.by_name.items():
            annotations = descriptor["annotations"]
            if annotations["readOnlyHint"]:
                self.assertFalse(annotations["destructiveHint"], f"{name} claims to both read only and destroy")

    def test_reads_are_idempotent(self):
        for name, descriptor in self.by_name.items():
            if descriptor["annotations"]["readOnlyHint"]:
                self.assertTrue(descriptor["annotations"]["idempotentHint"], f"{name} reads but is not idempotent")

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

    def test_a_result_is_bounded(self):
        long_result = {"ok": True, "summary": "x" * (tools.MAX_RESULT_CHARS * 2)}
        text = tools._text(tools._payload(long_result))
        self.assertLess(len(text), tools.MAX_RESULT_CHARS + 200)
        self.assertIn("truncated", text)


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
        self.assertEqual(set(result["capabilities"]), {"tools"})
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
        response = self.rpc("resources/list")
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

    def test_a_key_belonging_to_somebody_not_opted_in_does_not_work(self):
        self._opt_in(self.user, False)
        self.assertEqual(self.rpc().status_code, 401)

    def test_the_same_key_works_once_they_are_opted_in(self):
        self._opt_in(self.user, True)
        self.assertEqual(self.rpc().status_code, 200)

    def test_turning_the_flag_back_off_disconnects_them(self):
        self._opt_in(self.user, True)
        self.assertEqual(self.rpc().status_code, 200)
        self._opt_in(self.user, False)
        self.assertEqual(self.rpc().status_code, 401)


class ConnectPageTests(StandardTestCase):
    """The page that explains how to connect, and the same gate on it."""

    url = "/account/api-keys/"

    def test_it_is_refused_to_somebody_not_opted_in(self):
        self.user.userdata.use_llm_search = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_creating_a_key_is_refused_too(self):
        """The gate is on dispatch, so it covers the POST and not just the page."""
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
        # The address, and both ways of connecting with it.
        self.assertIn("/mcp", body)
        self.assertIn("Add custom connector", body)
        self.assertIn("claude mcp add --transport http", body)

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
