"""The addressable reads and the recipes: ``resources/templates/list``, ``prompts/*``, completions.

Written against the URL like ``test_mcp``, and for the same reason: these are protocol surfaces,
and the point of testing them is that a host gets what the spec says it gets.

The load-bearing tests here are the two audits. Every resource template must name a **read-only**
action, because a URI a host may fetch on somebody's behalf must never be a write; and nothing
concrete may ever appear in ``resources/list``, because a list of ``auction://spring-2027`` is a
list of which auctions exist handed to whoever asked.
"""

import json

from django.test import SimpleTestCase
from django.utils import timezone

from auctions import palette_actions
from auctions.mcp import prompts, protocol, resources, tools
from auctions.models import Auction, PickupLocation, UserAPIKey, UserData
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


class ResourceCatalogueTests(SimpleTestCase):
    """The catalogue on its own, with no request in the loop."""

    def test_every_template_names_a_read_only_action(self):
        """A URI a host may fetch on a person's behalf must never be a write."""
        for template in resources.ALL:
            action = palette_actions.ACTIONS[template.action]
            self.assertTrue(
                tools.read_only(action),
                f"{template.uri} resolves to {template.action}, which is not a read",
            )

    def test_every_placeholder_is_a_parameter_that_action_takes(self):
        """run_action refuses a parameter an action never advertised, so a typo here is a 500."""
        for template in resources.ALL:
            action = palette_actions.ACTIONS[template.action]
            for field in template.fields:
                self.assertTrue(action.accepts(field), f"{template.uri}: {template.action} has no {field}")
            for key in template.extra:
                self.assertTrue(action.accepts(key), f"{template.uri}: {template.action} has no {key}")

    def test_a_uri_resolves_to_its_template_and_its_arguments(self):
        matched = resources.match("lot://spring-auction/14")
        self.assertIsNotNone(matched)
        template, arguments = matched
        self.assertEqual(template.action, "describe_lot")
        self.assertEqual(arguments, {"auction": "spring-auction", "lot": "14"})

    def test_a_seller_dash_lot_number_survives(self):
        """BOB-1 is an ordinary lot number, so nothing here may be tightened into a digits regex."""
        _template, arguments = resources.match("lot://spring-auction/BOB-1")
        self.assertEqual(arguments["lot"], "BOB-1")

    def test_a_percent_encoded_slug_is_decoded_once(self):
        _template, arguments = resources.match("auction://spring%20auction")
        self.assertEqual(arguments["auction"], "spring auction")

    def test_the_wrong_number_of_parts_matches_nothing(self):
        self.assertIsNone(resources.match("lot://spring-auction"))
        self.assertIsNone(resources.match("auction://a/b/c"))

    def test_a_scheme_nobody_publishes_matches_nothing(self):
        self.assertIsNone(resources.match("file:///etc/passwd"))
        self.assertIsNone(resources.match("https://example.com/"))

    def test_a_literal_path_part_has_to_match(self):
        self.assertIsNotNone(resources.match("auction://spring/lots"))
        self.assertIsNone(resources.match("auction://spring/invoices"))

    def test_the_fixed_resources_are_about_the_caller_and_carry_no_slug(self):
        for template in resources.FIXED:
            self.assertEqual(template.fields, ())
            self.assertTrue(template.uri.startswith("me://"), template.uri)


@isolated_cache("mcp-resources")
class ResourceEndpointTests(StandardTestCase):
    """Reading one over the wire, with the caller's own permissions."""

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        UserData.objects.update(use_llm_search=True)
        raw, prefix, key_hash = UserAPIKey.generate()
        self.raw_key = raw
        UserAPIKey.objects.create(user=self.user, name="test key", prefix=prefix, key_hash=key_hash, allow_writes=True)
        raw_other, prefix_other, hash_other = UserAPIKey.generate()
        self.other_key = raw_other
        UserAPIKey.objects.create(
            user=self.userB, name="other", prefix=prefix_other, key_hash=hash_other, allow_writes=True
        )

    def rpc(self, method, params=None, *, key=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key if key is None else key}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )

    def result(self, response):
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertNotIn("error", payload, payload)
        return payload["result"]

    def test_initialize_advertises_what_is_now_implemented(self):
        capabilities = self.result(self.rpc("initialize", {"protocolVersion": protocol.LATEST_PROTOCOL_VERSION}))[
            "capabilities"
        ]
        self.assertEqual(set(capabilities), {"tools", "resources", "prompts", "completions"})

    def test_the_templates_are_listed(self):
        listed = self.result(self.rpc("resources/templates/list"))["resourceTemplates"]
        self.assertIn("auction://{auction}", {row["uriTemplate"] for row in listed})
        for row in listed:
            self.assertTrue(row["description"])

    def test_nothing_that_names_somebody_is_ever_listed(self):
        """A list of auction://spring-2027 is a list of which auctions exist.

        The rule is not "nothing concrete" -- ``help://faq`` is concrete and is listed, because it
        is the same document for every caller and names nobody. What may never appear is a URI
        carrying a slug, which is a fact about who is on this site handed to whoever asked.
        """
        listed = self.result(self.rpc("resources/list"))["resources"]
        for row in listed:
            self.assertTrue(
                row["uri"].startswith(("ui://", "me://", "help://")),
                f"{row['uri']} names something concrete and would enumerate the site",
            )

    def test_the_faq_is_listed_and_reads(self):
        listed = {row["uri"] for row in self.result(self.rpc("resources/list"))["resources"]}
        self.assertIn("help://faq", listed)
        contents = self.result(self.rpc("resources/read", {"uri": "help://faq"}))["contents"][0]
        self.assertIn("help", json.loads(contents["text"]))

    def test_reading_an_auction_answers_with_the_tools_own_json(self):
        uri = f"auction://{self.in_person_auction.slug}"
        contents = self.result(self.rpc("resources/read", {"uri": uri}))["contents"][0]
        payload = json.loads(contents["text"])
        self.assertIn(self.in_person_auction.title, json.dumps(payload))
        self.assertEqual(contents["mimeType"], resources.DATA_MIME_TYPE)

    def test_a_data_resource_is_permission_checked_like_the_tool_it_wraps(self):
        """list_people is admins only, and reading it by URI has to be the same refusal."""
        uri = f"auction://{self.in_person_auction.slug}/people"
        contents = self.result(self.rpc("resources/read", {"uri": uri}, key=self.other_key))["contents"][0]
        self.assertIn("error", json.loads(contents["text"]))

    def test_the_same_read_works_for_somebody_who_may(self):
        uri = f"auction://{self.in_person_auction.slug}/people"
        contents = self.result(self.rpc("resources/read", {"uri": uri}))["contents"][0]
        self.assertNotIn("error", json.loads(contents["text"]))

    def test_a_uri_nobody_publishes_is_a_protocol_error(self):
        response = self.rpc("resources/read", {"uri": "file:///etc/passwd"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", json.loads(response.content))

    def test_the_widget_documents_still_read(self):
        contents = self.result(self.rpc("resources/read", {"uri": "ui://auction.fish/lot"}))["contents"][0]
        self.assertTrue(contents["text"].lstrip().startswith("<!doctype html>"))

    def test_my_context_is_readable_by_uri(self):
        contents = self.result(self.rpc("resources/read", {"uri": "me://context"}))["contents"][0]
        self.assertIn("auctions", json.loads(contents["text"]))


class PromptTests(SimpleTestCase):
    """The recipes. Fixed text, so most of what matters can be checked without a database."""

    def test_every_prompt_renders_with_no_arguments_at_all(self):
        """A host may call prompts/get with nothing filled in, and it still has to be a sentence."""
        for prompt in prompts.PROMPTS:
            rendered = prompts.render(prompt.name, {})
            text = rendered["messages"][0]["content"]["text"]
            self.assertNotIn("{", text, f"{prompt.name} left a placeholder unfilled")
            self.assertGreater(len(text), 200)

    def test_an_argument_is_filled_in_when_it_is_given(self):
        rendered = prompts.render("chase_unpaid", {"auction": "Spring Auction"})
        self.assertIn("Spring Auction", rendered["messages"][0]["content"]["text"])

    def test_a_prompt_nobody_publishes_is_none(self):
        self.assertIsNone(prompts.render("drop_all_tables", {}))

    def test_every_argument_that_completes_something_names_a_kind_complete_knows(self):
        for prompt in prompts.PROMPTS:
            for argument in prompt.arguments:
                self.assertIn(argument.completes, ("", "auction", "club"), f"{prompt.name}.{argument.name}")

    def test_a_prompt_body_never_interpolates_anything_but_its_own_arguments(self):
        """A prompt that could carry a lot description would be an injection surface with a menu entry."""
        for prompt in prompts.PROMPTS:
            names = {argument.name for argument in prompt.arguments}
            for chunk in prompt.body.split("{")[1:]:
                placeholder = chunk.split("}")[0]
                self.assertIn(placeholder, names, f"{prompt.name} interpolates {placeholder}")


@isolated_cache("mcp-prompts")
class PromptEndpointTests(StandardTestCase):
    """prompts/list, prompts/get and completion/complete over the wire."""

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        UserData.objects.update(use_llm_search=True)
        raw, prefix, key_hash = UserAPIKey.generate()
        self.raw_key = raw
        UserAPIKey.objects.create(user=self.user, name="test key", prefix=prefix, key_hash=key_hash, allow_writes=True)

    def rpc(self, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )

    def result(self, response):
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertNotIn("error", payload, payload)
        return payload["result"]

    def test_the_recipes_are_listed_with_their_arguments(self):
        listed = self.result(self.rpc("prompts/list"))["prompts"]
        by_name = {row["name"]: row for row in listed}
        self.assertIn("run_check_in", by_name)
        self.assertEqual(by_name["run_check_in"]["arguments"][0]["name"], "auction")

    def test_getting_one_returns_a_user_message(self):
        result = self.result(self.rpc("prompts/get", {"name": "chase_unpaid", "arguments": {"auction": "Spring"}}))
        message = result["messages"][0]
        self.assertEqual(message["role"], "user")
        self.assertIn("Spring", message["content"]["text"])

    def test_a_prompt_nobody_publishes_is_a_protocol_error(self):
        payload = json.loads(self.rpc("prompts/get", {"name": "nonsense"}).content)
        self.assertIn("error", payload)

    def test_completion_offers_the_auctions_this_person_is_in(self):
        future = Auction.objects.create(
            created_by=self.user,
            title="Completable Auction",
            is_online=False,
            date_start=timezone.now() + timezone.timedelta(days=10),
            promote_this_auction=False,
        )
        PickupLocation.objects.create(
            name="somewhere", auction=future, pickup_time=timezone.now() + timezone.timedelta(days=11)
        )
        result = self.result(
            self.rpc(
                "completion/complete",
                {
                    "ref": {"type": "ref/prompt", "name": "chase_unpaid"},
                    "argument": {"name": "auction", "value": "Completable"},
                },
            )
        )
        self.assertIn("Completable Auction", result["completion"]["values"])

    def test_completion_never_answers_for_a_resource_template(self):
        """Completing auction://{auction} means listing this person's auctions from a URI pattern."""
        result = self.result(
            self.rpc(
                "completion/complete",
                {
                    "ref": {"type": "ref/resource", "uri": "auction://{auction}"},
                    "argument": {"name": "auction", "value": ""},
                },
            )
        )
        self.assertEqual(result["completion"]["values"], [])
