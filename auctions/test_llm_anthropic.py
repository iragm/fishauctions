"""Claude as the site's model: what :class:`auctions.llm.AnthropicProvider` sends and how it reads replies.

Every test goes through the real ``anthropic`` SDK with only the network replaced, so a request the SDK
or the Messages API shape would not accept fails here rather than on the first command somebody types.
"""

import json

import anthropic
import httpx2
from django.test import SimpleTestCase, override_settings

from auctions import llm

TOOL = {
    "name": "check_in",
    "description": "Check somebody in.",
    "inputSchema": {"type": "object", "properties": {"person": {"type": "string"}}, "required": ["person"]},
}


def reply(content, stop_reason="end_turn", usage=None):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def tool_use(call_id="toolu_1", name="check_in", arguments=None):
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments or {"person": "555"}}


class AnthropicProviderTests(SimpleTestCase):
    def provider(self, *answers, effort="minimal"):
        """A provider whose requests are recorded in ``self.sent`` and answered with ``answers`` in turn."""
        self.sent = []
        queue = list(answers)

        def handler(request):
            self.sent.append(json.loads(request.content))
            answer = queue.pop(0)
            if isinstance(answer, httpx2.Response):
                return answer
            return httpx2.Response(200, json=answer)

        provider = llm.AnthropicProvider(model="claude-haiku-5-5", api_key="sk-ant-test", reasoning_effort=effort)
        provider.http_client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
        return provider

    def test_a_tool_call_is_read(self):
        provider = self.provider(reply([tool_use()], "tool_use"))
        result = provider.complete(
            "system", [{"role": "user", "content": "check in 555"}], [TOOL], tool_choice="required"
        )
        self.assertEqual(
            [(call.id, call.name, call.arguments) for call in result.tool_calls],
            [("toolu_1", "check_in", {"person": "555"})],
        )
        sent = self.sent[0]
        self.assertEqual(sent["tool_choice"], {"type": "any", "disable_parallel_tool_use": True})
        self.assertEqual(sent["max_tokens"], llm.MIN_ANTHROPIC_MAX_TOKENS)
        self.assertEqual(sent["tools"][0]["input_schema"], TOOL["inputSchema"])
        self.assertEqual(sent["model"], "claude-haiku-5-5")
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_minimal_effort_is_sent_as_low(self):
        provider = self.provider(reply([{"type": "text", "text": "hello"}]), reply([{"type": "text", "text": "hello"}]))
        provider.complete("system", [{"role": "user", "content": "hi"}])
        self.assertEqual(self.sent[0]["output_config"], {"effort": "low"})
        provider.reasoning_effort = "high"
        provider.complete("system", [{"role": "user", "content": "hi"}])
        self.assertEqual(self.sent[1]["output_config"], {"effort": "high"})

    def test_no_effort_configured_sends_none(self):
        provider = self.provider(reply([{"type": "text", "text": "hello"}]), effort="")
        provider.complete("system", [{"role": "user", "content": "hi"}])
        self.assertNotIn("output_config", self.sent[0])
        self.assertNotIn("temperature", self.sent[0])

    def test_a_tool_result_goes_back_with_the_turn_that_asked_for_it(self):
        thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
        provider = self.provider(reply([thinking, tool_use()], "tool_use"), reply([{"type": "text", "text": "Done."}]))
        messages = [{"role": "user", "content": "check in 555"}]
        first = provider.complete("system", messages, [TOOL])
        messages.append(llm.tool_call_message(first.tool_calls))
        messages.append(llm.tool_result_message(first.tool_calls[0], "checked in"))
        second = provider.complete("system", messages, [TOOL])
        self.assertEqual(second.text, "Done.")
        _user, assistant, results = self.sent[1]["messages"]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual([block["type"] for block in assistant["content"]], ["thinking", "tool_use"])
        self.assertEqual(assistant["content"][0]["signature"], "sig")
        self.assertEqual(
            results,
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "checked in"}]},
        )

    def test_only_the_calls_that_were_answered_go_back(self):
        """The palette runs the first of several calls, and a ``tool_use`` with no result is a 400."""
        provider = self.provider(
            reply([tool_use("toolu_1"), tool_use("toolu_2")], "tool_use"), reply([{"type": "text", "text": "Done."}])
        )
        messages = [{"role": "user", "content": "check in 555 and 556"}]
        first = provider.complete("system", messages, [TOOL])
        messages.append(llm.tool_call_message([first.tool_calls[0]]))
        messages.append(llm.tool_result_message(first.tool_calls[0], "checked in"))
        provider.complete("system", messages, [TOOL])
        assistant = self.sent[1]["messages"][1]
        self.assertEqual([block["id"] for block in assistant["content"]], ["toolu_1"])

    def test_several_results_share_one_user_turn(self):
        provider = self.provider(
            reply([tool_use("toolu_1"), tool_use("toolu_2")], "tool_use"), reply([{"type": "text", "text": "Done."}])
        )
        messages = [{"role": "user", "content": "check in 555 and 556"}]
        first = provider.complete("system", messages, [TOOL])
        messages.append(llm.tool_call_message(first.tool_calls))
        for call in first.tool_calls:
            messages.append(llm.tool_result_message(call, "checked in"))
        provider.complete("system", messages, [TOOL])
        sent = self.sent[1]["messages"]
        self.assertEqual([message["role"] for message in sent], ["user", "assistant", "user"])
        self.assertEqual([block["tool_use_id"] for block in sent[2]["content"]], ["toolu_1", "toolu_2"])

    def test_a_tool_turn_from_another_request_is_rebuilt(self):
        """The provider that made the call is gone by the next request; the turn is rebuilt from its parts."""
        call = llm.ToolCall(id="toolu_9", name="check_in", arguments={"person": "555"})
        provider = self.provider(reply([{"type": "text", "text": "Done."}]))
        provider.complete(
            "system",
            [
                {"role": "user", "content": "check in 555"},
                llm.tool_call_message([call]),
                llm.tool_result_message(call, "checked in"),
            ],
            [TOOL],
        )
        self.assertEqual(self.sent[0]["messages"][1]["content"], [tool_use("toolu_9")])

    def test_consecutive_user_messages_are_one_turn(self):
        provider = self.provider(reply([{"type": "text", "text": "hello"}]))
        provider.complete(
            "system", [{"role": "user", "content": "About this user: {}"}, {"role": "user", "content": "hi"}]
        )
        self.assertEqual(
            self.sent[0]["messages"],
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "About this user: {}"}, {"type": "text", "text": "hi"}],
                }
            ],
        )

    def test_a_picture_is_sent_as_an_image_block(self):
        provider = self.provider(reply([{"type": "text", "text": "A page."}]))
        content = [
            {"type": "text", "text": "Read this."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        ]
        provider.complete("system", [{"role": "user", "content": content}])
        self.assertEqual(
            self.sent[0]["messages"][0]["content"][1],
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
        )

    def test_json_is_read_out_of_a_fenced_reply(self):
        provider = self.provider(reply([{"type": "text", "text": '```json\n{"species": "Corydoras"}\n```'}]))
        result = provider.complete_json("Name the species.", [{"role": "user", "content": "Lot name: cory"}])
        self.assertEqual(result.data, {"species": "Corydoras"})
        self.assertIn("one JSON object", self.sent[0]["system"][0]["text"])

    def test_a_reply_that_is_not_json_is_an_error(self):
        provider = self.provider(reply([{"type": "text", "text": "I think it is a Corydoras."}]))
        with self.assertRaises(llm.LLMError):
            provider.complete_json("Name the species.", [{"role": "user", "content": "Lot name: cory"}])

    def test_cached_tokens_are_counted_into_the_prompt(self):
        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 200,
        }
        provider = self.provider(reply([{"type": "text", "text": "hello"}], usage=usage))
        result = provider.complete("system", [{"role": "user", "content": "hi"}])
        self.assertEqual((result.prompt_tokens, result.cached_prompt_tokens, result.completion_tokens), (3210, 3000, 5))
        self.assertEqual(result.total_tokens, 3215)

    def test_a_rate_limit_carries_the_wait(self):
        limited = httpx2.Response(
            429,
            headers={"retry-after": "3"},
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )
        provider = self.provider(limited)
        with self.assertRaises(llm.RateLimited) as raised:
            provider.complete("system", [{"role": "user", "content": "hi"}])
        self.assertEqual(raised.exception.retry_after, 3.0)
        self.assertEqual(len(self.sent), 1, "the palette waits out a rate limit itself; the SDK must not retry")

    def test_an_outage_is_an_llm_error_and_is_not_retried(self):
        provider = self.provider(
            httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        )
        with self.assertRaises(llm.LLMError) as raised:
            provider.complete("system", [{"role": "user", "content": "hi"}])
        self.assertNotIsInstance(raised.exception, llm.RateLimited)
        self.assertEqual(len(self.sent), 1)

    def test_a_model_that_refuses_a_forced_tool_call_is_asked_again_without_one(self):
        refused = httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
                },
            },
        )
        provider = self.provider(refused, reply([tool_use()], "tool_use"))
        result = provider.complete(
            "system", [{"role": "user", "content": "check in 555"}], [TOOL], tool_choice="required"
        )
        self.assertEqual(result.tool_calls[0].name, "check_in")
        self.assertEqual(
            [sent["tool_choice"] for sent in self.sent],
            [{"type": "any", "disable_parallel_tool_use": True}, {"type": "auto", "disable_parallel_tool_use": True}],
        )
        # Remembered: the next round of the same request does not pay for the 400 again.
        self.sent.clear()
        provider.http_client = anthropic.DefaultHttpxClient(
            transport=httpx2.MockTransport(
                lambda request: (
                    self.sent.append(json.loads(request.content))
                    or httpx2.Response(200, json=reply([tool_use()], "tool_use"))
                )
            )
        )
        provider.complete("system", [{"role": "user", "content": "check in 556"}], [TOOL], tool_choice="required")
        self.assertEqual([sent["tool_choice"]["type"] for sent in self.sent], ["auto"])

    def test_a_model_that_takes_no_effort_level_is_asked_again_without_one(self):
        refused = httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "output_config.effort: not supported"},
            },
        )
        provider = self.provider(refused, reply([{"type": "text", "text": "hello"}]))
        self.assertEqual(provider.complete("system", [{"role": "user", "content": "hi"}]).text, "hello")
        self.assertIn("output_config", self.sent[0])
        self.assertNotIn("output_config", self.sent[1])

    def test_a_tool_call_cut_off_by_the_token_limit_is_not_run(self):
        provider = self.provider(reply([tool_use(arguments={"person": "5"})], "max_tokens"))
        with self.assertRaisesMessage(llm.LLMError, "whole completion budget"):
            provider.complete("system", [{"role": "user", "content": "check in 555"}], [TOOL])

    def test_a_body_that_is_not_a_reply_is_an_llm_error(self):
        provider = self.provider(httpx2.Response(200, text="<html>gateway</html>"))
        with self.assertRaises(llm.LLMError):
            provider.complete("system", [{"role": "user", "content": "hi"}])

    def test_another_bad_request_is_not_retried(self):
        bad = httpx2.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: too large"}}
        )
        provider = self.provider(bad)
        with self.assertRaises(llm.LLMError):
            provider.complete("system", [{"role": "user", "content": "hi"}], [TOOL], tool_choice="required")
        self.assertEqual(len(self.sent), 1)

    def test_a_refusal_is_an_error_not_an_empty_answer(self):
        provider = self.provider(reply([], "refusal"))
        with self.assertRaises(llm.Declined):
            provider.complete("system", [{"role": "user", "content": "hi"}])

    def test_running_out_of_tokens_says_so(self):
        provider = self.provider(reply([], "max_tokens"))
        with self.assertRaisesMessage(llm.LLMError, "whole completion budget"):
            provider.complete("system", [{"role": "user", "content": "hi"}])

    def test_no_key_makes_no_call(self):
        provider = llm.AnthropicProvider(model="claude-haiku-5-5")
        self.assertFalse(provider.is_configured())
        with self.assertRaises(llm.LLMError):
            provider.complete("system", [{"role": "user", "content": "hi"}])


class ProviderChoiceTests(SimpleTestCase):
    def setUp(self):
        llm.set_provider_override(None)

    @override_settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY="", LLM_MODEL="")
    def test_anthropic_reads_its_own_key_and_defaults_to_haiku(self):
        provider = llm.get_provider()
        self.assertIsInstance(provider, llm.AnthropicProvider)
        self.assertTrue(provider.is_configured())
        self.assertEqual(provider.model, "claude-haiku-5-5")
        self.assertTrue(llm.assist_enabled())

    @override_settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="", OPENAI_API_KEY="sk-test")
    def test_an_openai_key_does_not_configure_anthropic(self):
        self.assertFalse(llm.get_provider().is_configured())

    @override_settings(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test", ANTHROPIC_API_KEY="sk-ant-test", LLM_MODEL="")
    def test_openai_stays_the_default(self):
        provider = llm.get_provider()
        self.assertIsInstance(provider, llm.OpenAIProvider)
        self.assertEqual(provider.model, "gpt-5-nano")

    @override_settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY="sk-test")
    def test_vectors_still_come_from_openai(self):
        self.assertIsInstance(llm.get_embedding_provider(), llm.OpenAIProvider)

    @override_settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY="")
    def test_without_an_openai_key_there_are_no_vectors(self):
        with self.assertRaises(llm.LLMError):
            llm.get_embedding_provider().embed(["a"], "text-embedding-3-small")

    @override_settings(
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-ant-test",
        OPENAI_API_KEY="sk-test",
        LLM_BASE_URL="https://proxy.example/v1",
    )
    def test_an_openai_compatible_base_url_is_never_handed_to_anthropic(self):
        self.assertEqual(llm.get_provider().base_url, "")
        self.assertEqual(llm.get_embedding_provider().base_url, "https://proxy.example/v1")

    @override_settings(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    def test_openai_embeds_for_itself(self):
        self.assertIsInstance(llm.get_embedding_provider(), llm.OpenAIProvider)
