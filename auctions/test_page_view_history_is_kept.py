"""Repeat views of a page are history, not duplicates.

A ``remove_duplicate_views`` beat job used to merge them every fifteen minutes.  It only ever
reached *anonymous* rows -- a signed-in view stores ``session_id=NULL`` and the matcher skipped
those -- and it had no time window, while ``SESSION_COOKIE_AGE`` is about 230 years: one anonymous
visitor's every visit to a page, however far apart, folded into a single row.  ``PageView``'s
docstring has the argument.

This is the ratchet for that decision.  Nothing here tests code that exists; it tests that the
beacon still leaves one row per view, which is what any reintroduced merger would break.
"""

from django.contrib.auth.models import User
from django.core.management import get_commands
from django.test import TestCase
from django.urls import reverse

from auctions.models import PageView


class RepeatViewsAreKeptTests(TestCase):
    def _view(self, path="/lots/1/"):
        return self.client.post(
            reverse("pageview"),
            {"url": path, "title": "A lot", "referrer": "", "first_view": "true"},
        )

    def test_one_anonymous_session_viewing_a_page_twice_leaves_two_rows(self):
        """The case the old job collapsed.  Both requests share a session, so both rows carry the
        same ``session_id`` -- which is exactly what it matched on."""
        self._view()
        self._view()
        rows = PageView.objects.filter(url="/lots/1/")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(len({row.session_id for row in rows}), 1)

    def test_a_signed_in_visitor_is_no_different(self):
        """The old job could not reach these at all, so the two halves of every raw-row count on
        the stats pages were being kept by different rules."""
        User.objects.create_user("repeatviewer", "repeat@example.com", "x")
        self.client.login(username="repeatviewer", password="x")
        self._view("/lots/2/")
        self._view("/lots/2/")
        self.assertEqual(PageView.objects.filter(url="/lots/2/").count(), 2)

    def test_there_is_no_deduplicating_command(self):
        self.assertNotIn("remove_duplicate_views", get_commands())
