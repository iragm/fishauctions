"""/requests/: the web form for bug reports and feature requests, writing the same queue as request_a_skill."""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from auctions.models import AssistantSkillRequest


class FeatureRequestsPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("asker", "asker@example.com", "pw")
        cls.other = User.objects.create_user("someone", "someone@example.com", "pw")

    def setUp(self):
        self.client.force_login(self.user)

    def _send(self, **data):
        payload = {"request": "Sort lots by price\nI wanted the cheap ones first."}
        payload.update(data)
        return self.client.post(reverse("feature_requests"), payload, follow=True)

    def test_signed_out_is_sent_to_log_in(self):
        self.client.logout()
        response = self.client.get(reverse("feature_requests"))
        self.assertEqual(response.status_code, 302)

    def test_a_request_joins_the_owners_queue_as_new(self):
        self._send()
        row = AssistantSkillRequest.objects.get(user=self.user)
        self.assertEqual(row.skill, "Sort lots by price")
        self.assertEqual(row.reason, "Sort lots by price\nI wanted the cheap ones first.")
        self.assertEqual(row.status, AssistantSkillRequest.STATUS_NEW)
        self.assertEqual(row.surface, "requests page")

    def test_a_long_first_line_is_cut_at_a_word_for_the_name(self):
        self._send(request="word " * 40)
        row = AssistantSkillRequest.objects.get(user=self.user)
        self.assertLessEqual(len(row.skill), 81)
        self.assertTrue(row.skill.endswith("word…"))
        self.assertEqual(row.reason, ("word " * 40).strip())

    def test_you_see_your_own_requests_and_their_status_never_anyone_elses(self):
        AssistantSkillRequest.objects.create(
            user=self.user, skill="Dark mode", reason="x", status="planned", notes="owner only"
        )
        AssistantSkillRequest.objects.create(user=self.other, skill="Someone else's idea", reason="y")
        html = self.client.get(reverse("feature_requests")).content.decode()
        self.assertIn("Dark mode", html)
        self.assertIn("Planned", html)
        self.assertNotIn("owner only", html)
        self.assertNotIn("Someone else", html)

    def test_a_decided_request_is_not_changed_by_sending_it_again(self):
        AssistantSkillRequest.objects.create(user=self.user, skill="Sort lots by price", reason="old", status="planned")
        self._send(request="Sort lots by price\nnew words")
        row = AssistantSkillRequest.objects.get(user=self.user)
        self.assertEqual(row.reason, "old")

    def test_a_day_has_a_limit(self):
        for number in range(10):
            AssistantSkillRequest.objects.create(user=self.user, skill=f"idea {number}", reason="r")
        self._send()
        self.assertFalse(AssistantSkillRequest.objects.filter(skill="Sort lots by price").exists())

    def test_the_support_page_links_here(self):
        self.assertIn(reverse("feature_requests"), self.client.get(reverse("support")).content.decode())
