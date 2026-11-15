"""Connector regressions: honest navigation, safe oversized writes, search diagnostics and client evals."""

import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase

from auctions import palette_actions
from auctions.mcp import auth, evaluation, tools, usage, widgets
from auctions.test_support import isolated_cache


@isolated_cache("mcp-reliability")
class ResultContractTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/mcp/")
        self.request.user = SimpleNamespace(pk=912, is_authenticated=True)
        self.addCleanup(patch.stopall)
        patch.object(palette_actions, "remember_undo").start()

    def call(self, tool, payload, arguments=None):
        with patch.object(palette_actions, "run_action", return_value=payload):
            return tools.call_tool(self.request, tool, arguments or {})

    def test_navigation_resolves_a_destination_without_claiming_to_open_it(self):
        result = self.call(
            "go_to_page", {"ok": True, "title": "Clubs", "url": "/clubs/", "summary": "Taking you there."}
        )
        data = result["structuredContent"]
        self.assertEqual(data["status"], "destination_resolved")
        self.assertFalse(data["navigation_performed"])
        self.assertEqual(data["url"], "http://testserver/clubs/")
        self.assertTrue(data["summary"].startswith("Not opened yet"))
        self.assertIn("Taking you", data["summary"])  # which page it found is kept
        self.assertFalse(result["isError"])
        self.assertIn("go_to_page", widgets.TOOL_WIDGETS)

    def test_chatgpt_gets_a_clickable_navigation_link_without_a_widget(self):
        with patch.object(auth, "reads_structured_content", return_value=True):
            result = self.call("go_to_page", {"ok": True, "url": "/clubs/", "summary": "Taking you there."})
        self.assertIn("[Open page](http://testserver/clubs/)", result["content"][0]["text"])

    def test_navigation_needing_clarification_has_no_destination(self):
        result = self.call("go_to_page", {"more_info_needed": "Which auction?"})
        self.assertEqual(result["structuredContent"]["status"], "needs_more_information")
        self.assertNotIn("url", result["structuredContent"])

    def test_oversized_read_is_an_error_and_counted_as_one(self):
        with patch.object(tools, "MAX_RESULT_CHARS", 200):
            result = self.call("my_context", {"summary": "Context", "rows": ["x" * 500]})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error_code"], "result_too_large")
        self.assertFalse(result["structuredContent"]["operation_completed"])
        self.assertEqual(usage.outcome_of("tools/call", {"result": result})[0], "error")

    def test_completed_write_is_not_repeated_when_its_receipt_is_too_large(self):
        payload = {"ok": True, "summary": "Lot created", "rows": ["x" * 500]}
        with (
            patch.object(tools, "MAX_RESULT_CHARS", 200),
            patch.object(palette_actions, "run_action", return_value=payload) as run,
        ):
            first = tools.call_tool(self.request, "add_lot", {"name": "overflow test"})
            second = tools.call_tool(self.request, "add_lot", {"name": "overflow test"})
        self.assertEqual(run.call_count, 1)
        # It went through, so the host must not be told it failed.
        self.assertFalse(first["isError"])
        self.assertTrue(first["structuredContent"]["operation_completed"])
        self.assertFalse(first["structuredContent"]["retryable"])
        self.assertEqual(first["structuredContent"], second["structuredContent"])

    def test_feature_request_old_name_is_an_unlisted_alias(self):
        self.assertIs(palette_actions.get_action("request_a_skill"), palette_actions.get_action("request_feature"))
        listed = {item["name"] for item in tools.tool_descriptors(None)}
        self.assertIn("request_feature", listed)
        self.assertNotIn("request_a_skill", listed)

    def test_form_intents_differ_from_the_palettes_only_lot_entry_point(self):
        form = palette_actions.get_action("add_a_lot_via_webform")
        self.assertIn("add a lot of blue shrimp", form.examples)
        self.assertIn("Anything about adding", form.description)
        self.assertNotIn("Anything about adding", tools.describe(form))
        self.assertIn("nothing is saved", tools.describe(form))

    def test_the_palette_keeps_the_website_wording(self):
        # The palette navigates by itself and offers only the lot form; the connector wording says otherwise.
        for name in ("add_a_lot_via_webform", "go_to_page"):
            action = palette_actions.get_action(name)
            self.assertTrue(action.mcp_description)
            self.assertNotEqual(tools.describe(action, palette=True), tools.describe(action))
            self.assertTrue(tools.describe(action, palette=True).startswith(action.description.strip()))

    def test_batch_schema_defines_per_lot_fields_and_copy_limits(self):
        schema = tools.input_schema(palette_actions.get_action("add_lots"))
        entries = schema["properties"]["lots"]
        item = entries["items"]["anyOf"][1]
        self.assertEqual(set(item["properties"]), {*palette_actions._PER_LOT_KEYS, "count"})
        # The resolver ignores keys it doesn't know, so the schema doesn't refuse them either.
        self.assertNotIn("additionalProperties", item)
        self.assertNotIn("bidder", item["properties"])
        self.assertEqual(item["properties"]["count"]["maximum"], palette_actions.MAX_LOTS_PER_BATCH)
        self.assertEqual(entries["maxItems"], palette_actions.MAX_LOTS_PER_BATCH)


class DiagnosticsTests(SimpleTestCase):
    def tearDown(self):
        usage.catalog_version.cache_clear()

    def test_text_that_is_the_structured_json_is_counted_once(self):
        payload = {"summary": "Done", "lots": [{"lot_number": 5}]}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = {"content": [{"type": "text", "text": text}], "structuredContent": payload}
        self.assertEqual(usage.outcome_of("tools/call", {"result": result})[2], len(text))

    def test_people_found_are_counted_not_identified(self):
        payload = {"people": [{"id": 4, "bidder_number": "45", "name": "Someone"}]}
        found = usage.result_metadata({"result": {"structuredContent": payload}})
        self.assertEqual(found["results"]["people"], {"returned": 1})

    def test_summary_only_text_still_counts_structured_content(self):
        payload = {"summary": "Done", "lots": [{"lot_number": 5}]}
        result = {"content": [{"type": "text", "text": "Done"}], "structuredContent": payload}
        expected = 4 + len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(usage.outcome_of("tools/call", {"result": result})[2], expected)

    def test_search_metadata_preserves_order_without_storing_result_text(self):
        payload = {
            "total": 2,
            "lots": [{"id": 8, "name": "Private", "email": "a@example.com"}, {"id": 3}],
            "summary": "private text",
        }
        found = usage.result_metadata({"result": {"structuredContent": payload}})
        self.assertEqual(found["results"]["lots"]["first_results"], [{"id": 8}, {"id": 3}])
        self.assertEqual(found["total"], 2)
        self.assertNotIn("Private", json.dumps(found))
        self.assertNotIn("example.com", json.dumps(found))

    def test_metadata_is_bounded_and_drops_arbitrary_identifiers(self):
        rows = [{"id": "user-written", "page": "not-a-route", "name": "private"}] * 100
        found = usage.result_metadata({"result": {"structuredContent": {"results": rows}}})
        self.assertEqual(found["results"]["results"]["returned"], 100)
        self.assertEqual(found["results"]["results"]["first_results"], [{}] * 20)

    def test_catalog_fingerprint_changes_with_a_description(self):
        usage.catalog_version.cache_clear()
        before = usage.catalog_version()
        action = palette_actions.get_action("read_source")
        with patch.object(action, "description", action.description + " Extra purpose."):
            usage.catalog_version.cache_clear()
            self.assertNotEqual(before, usage.catalog_version())


class EvaluationTests(SimpleTestCase):
    def test_cases_reference_available_tools_and_have_unique_ids(self):
        cases = evaluation.cases()
        self.assertGreaterEqual(len(cases), 30)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            for name in [*case["expected_tools"], *case["forbidden_tools"]]:
                self.assertIn(name, palette_actions.ACTIONS)

    def test_no_call_failure_and_unknown_completion_are_kept(self):
        result = evaluation.score([{"case_id": "source_debug", "client": "ChatGPT", "tools": []}])
        (client,) = result["clients"]
        self.assertEqual(client["first_tool_correct"], 0)
        self.assertEqual(client["completion_labels"], 0)
        self.assertIsNone(client["runs_detail"][0]["completed"])

    def test_wrong_connector_before_correct_tool_is_a_selection_failure(self):
        result = evaluation.score(
            [{"case_id": "source_debug", "tools": ["github_search", "read_source"], "completed": True}]
        )
        row = result["clients"][0]["runs_detail"][0]
        self.assertFalse(row["first_tool_correct"])
        self.assertTrue(row["expected_tools_called"])
        self.assertTrue(row["completed"])

    def test_context_is_not_skipped_when_it_is_the_requested_tool(self):
        scored = evaluation.score([{"case_id": "own_context", "tools": ["my_context"]}])
        self.assertTrue(scored["clients"][0]["runs_detail"][0]["first_tool_correct"])

    def test_before_and_after_variants_are_not_combined(self):
        scored = evaluation.score(
            [
                {"case_id": "source_debug", "client": "ChatGPT", "variant": "before", "tools": []},
                {"case_id": "source_debug", "client": "ChatGPT", "variant": "after", "tools": ["read_source"]},
            ]
        )
        self.assertEqual(len(scored["clients"]), 2)
        self.assertEqual([row["first_tool_correct"] for row in scored["clients"]], [0, 1])

    def test_repetitions_are_not_deduplicated(self):
        run = {"case_id": "source_debug", "tools": ["read_source"], "completed": True}
        result = evaluation.score([run, copy.deepcopy(run)])
        self.assertEqual(result["clients"][0]["runs"], 2)
        self.assertEqual(result["clients"][0]["cases_covered"], 1)

    def test_export_never_executes_an_action(self):
        out = io.StringIO()
        with patch.object(palette_actions, "run_action") as run:
            call_command("mcp_eval", stdout=out)
        run.assert_not_called()
        self.assertEqual(len(json.loads(out.getvalue())["cases"]), len(evaluation.cases()))
