"""``/ai/`` (auctions/ai_page.py): the question box, the reads-only tool loop, the daily allowance."""

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from auctions import ai_page, command_palette, llm
from auctions.documents import search as library_search
from auctions.llm import LLMResult, ToolCall
from auctions.models import LLMUsage, UserData
from auctions.palette_actions import DANGER_CONFIRM, get_action
from auctions.test_palette_assist import FakeProvider
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


def answer(text, prompt=100, completion=20, cached=0):
    return LLMResult(
        text=text, model="fake-model", prompt_tokens=prompt, completion_tokens=completion, cached_prompt_tokens=cached
    )


def lookup(name, **arguments):
    return LLMResult(
        tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)],
        model="fake-model",
        prompt_tokens=100,
        completion_tokens=10,
    )


@isolated_cache("ai-page")
class AIPageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        llm.set_provider_override(self.provider)
        self.addCleanup(llm.set_provider_override, None)
        UserData.objects.filter(user=self.user).update(use_llm_search=True)
        self.user.userdata.refresh_from_db()
        self.client.force_login(self.user)

    def ask(self, question, conversation="c1"):
        return self.client.post(reverse("ai_ask"), {"q": question, "conversation": conversation})

    def test_signed_out_is_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("ai")).status_code, 302)
        self.assertEqual(self.ask("hi").status_code, 302)

    def test_the_query_is_filled_in_and_not_sent(self):
        response = self.client.get(reverse("ai") + "?q=when+is+my+next+auction")
        self.assertContains(response, "when is my next auction</textarea>")
        self.assertEqual(self.provider.call_count, 0)
        self.assertFalse(LLMUsage.objects.filter(user=self.user).exists())

    def test_the_page_links_to_connecting_an_agent_and_shows_the_bar(self):
        response = self.client.get(reverse("ai"))
        self.assertContains(response, reverse("user_api_keys"))
        self.assertContains(response, 'role="progressbar"')

    def test_an_answer_is_shown_as_markdown_and_counted(self):
        self.provider.replies = [answer("Your next auction is **Saturday**.")]
        response = self.ask("when is my next auction?")
        self.assertContains(response, "<strong>Saturday</strong>")
        self.assertContains(response, 'hx-swap-oob="true"')
        row = LLMUsage.objects.get(user=self.user)
        self.assertEqual(row.tools_offered, ai_page.SURFACE)
        self.assertEqual(ai_page.tokens_used(self.user), 120)

    def test_a_reply_cannot_put_html_on_the_page(self):
        self.provider.replies = [answer('<script>alert(1)</script><img src="https://example.com/x.png">')]
        response = self.ask("hi")
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "example.com/x.png")

    def test_a_lookup_runs_through_the_mcp_tools_and_the_result_reaches_the_model(self):
        self.provider.replies = [lookup("my_context"), answer("Done.")]
        self.assertContains(self.ask("what auction am I in?"), "Done.")
        second = self.provider.calls[1]["messages"]
        self.assertTrue(any(message.get("role") == "tool" for message in second))
        self.assertEqual(LLMUsage.objects.filter(user=self.user, tools_offered=ai_page.SURFACE).count(), 2)

    def test_no_write_is_offered(self):
        self.provider.replies = [answer("Hello.")]
        self.ask("hi")
        names = [tool["name"] for tool in self.provider.calls[0]["tools"]]
        self.assertIn("my_context", names)
        for name in names:
            action = get_action(name)
            self.assertFalse(action and action.danger == DANGER_CONFIRM, name)

    def test_the_conversation_carries_on(self):
        self.provider.replies = [answer("First."), answer("Second.")]
        self.ask("one", conversation="abc")
        self.ask("two", conversation="abc")
        # The fake keeps the list it was handed, which the answer is appended to afterwards.
        contents = [message.get("content") for message in self.provider.calls[1]["messages"]]
        self.assertEqual(contents[:3], ["one", "First.", "two"])

    def test_a_new_conversation_starts_empty(self):
        self.provider.replies = [answer("First."), answer("Second.")]
        self.ask("one", conversation="abc")
        self.ask("two", conversation="xyz")
        self.assertEqual(self.provider.calls[1]["messages"][0]["content"], "two")

    def test_lookups_are_not_kept_for_the_next_question(self):
        """A lookup's result would be sent again with every later round, spending the allowance."""
        self.provider.replies = [lookup("my_context"), answer("First."), answer("Second.")]
        self.ask("one", conversation="abc")
        self.ask("two", conversation="abc")
        roles = [message.get("role") for message in self.provider.calls[2]["messages"]][:3]
        self.assertEqual(roles, ["user", "assistant", "user"])

    def test_someone_cut_off_from_the_model_is_cut_off_here(self):
        UserData.objects.filter(user=self.user).update(use_llm_search=False)
        self.assertContains(self.client.get(reverse("ai")), "turned on for you here")
        self.assertContains(self.ask("hi"), "turned on for you here")
        self.assertEqual(self.provider.call_count, 0)
        groups = self.client.get(reverse("command_palette"), {"q": "pickup times"}).json()["groups"]
        self.assertNotIn("Ask AI", [group["label"] for group in groups])

    def test_one_question_at_a_time(self):
        cache.set(f"ai-page-asking:{self.user.pk}", 1)
        self.assertContains(self.ask("hi"), "Still answering")
        self.assertEqual(self.provider.call_count, 0)

    @override_settings(AI_PAGE_TOKENS_PER_DAY=150)
    def test_running_out_partway_says_so(self):
        self.provider.replies = [lookup("my_context"), answer("never")]
        self.provider.replies[0].prompt_tokens = 200
        self.assertContains(self.ask("hi"), "allowance used up")

    def test_too_many_lookups_stop(self):
        self.provider.replies = [lookup("my_context") for _ in range(ai_page.MAX_ROUNDS)]
        self.assertContains(self.ask("loop"), "more looking up than one question gets")
        self.assertEqual(self.provider.call_count, ai_page.MAX_ROUNDS)
        self.assertIsNone(cache.get(f"ai-page-asking:{self.user.pk}"))

    @override_settings(AI_PAGE_TOKENS_PER_DAY=100)
    def test_the_allowance_used_up_stops_asking(self):
        self.provider.replies = [answer("First.", prompt=100), answer("never")]
        self.ask("one")
        self.assertTrue(ai_page.usage(self.user)["over"])
        response = self.ask("two")
        self.assertContains(response, "allowance used up")
        self.assertEqual(self.provider.call_count, 1)
        self.assertContains(self.client.get(reverse("ai")), 'disabled><i class="bi bi-send">')

    def test_cached_prompt_tokens_count_at_a_tenth(self):
        self.provider.replies = [answer("Hi.", prompt=1000, completion=0, cached=1000)]
        self.ask("hi")
        self.assertEqual(ai_page.tokens_used(self.user), 100)

    def test_other_surfaces_do_not_count(self):
        LLMUsage.objects.create(user=self.user, model="m", prompt_tokens=5000, completion_tokens=0, tools_offered="")
        self.assertEqual(ai_page.tokens_used(self.user), 0)

    def test_the_palette_offers_to_ask(self):
        groups = self.client.get(reverse("command_palette"), {"q": "pickup times"}).json()["groups"]
        (group,) = [group for group in groups if group["label"] == "Ask AI"]
        self.assertEqual(group["items"][0]["url"], reverse("ai") + "?q=pickup+times")

    def test_the_offer_is_not_a_search_result(self):
        """Inside search() it would match everything, and the assist would never reach the model."""
        request = self.client.get("/").wsgi_request
        request.user = self.user
        labels = [group["label"] for group in command_palette.search(request, "pickup times")]
        self.assertNotIn("Ask AI", labels)


@isolated_cache("library-allowance")
class LibraryAllowanceTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    @override_settings(LIBRARY_SEARCHES_PER_DAY=1)
    def test_searches_past_the_allowance_skip_the_vector(self):
        calls = []
        original = library_search._nearest
        library_search._nearest = lambda chunks, query: calls.append(query) or []
        self.addCleanup(setattr, library_search, "_nearest", original)
        library_search.search(self.user, "betta")
        library_search.search(self.user, "betta")
        self.assertEqual(calls, ["betta"])

    def test_the_answer_under_a_search_is_not_a_second_search(self):
        self.client.force_login(self.user)
        library_search.search(self.user, "betta")
        library_search.search(self.user, "betta", counted=False)
        self.assertEqual(library_search.used_today(self.user, "searches"), 1)

    def test_a_cache_failure_never_breaks_a_search(self):
        from unittest.mock import patch

        with patch("django.core.cache.cache.add", side_effect=ConnectionError):
            self.assertEqual(library_search.used_today(self.user, "searches", add=1), 0)

    def test_counts_are_per_person(self):
        library_search.used_today(self.user, "answers", add=1)
        self.assertEqual(library_search.used_today(self.user, "answers"), 1)
        self.assertEqual(library_search.used_today(self.userB, "answers"), 0)
