"""``recent_changes`` and ``club_history``: the filters, and a superuser reading every auction or club.

A run of links from ``/admin-unlinked-auctions/`` writes one line per auction and one per club, so
checking that run for a misclick needs both histories readable site-wide, by who and by time.
"""

import datetime

from django.test import RequestFactory
from django.utils import timezone

from auctions import palette_actions
from auctions.models import Auction, AuctionHistory, Club, ClubHistory
from auctions.tests import StandardTestCase


class HistoryToolTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = self.user_who_does_not_join
        self.superuser.is_superuser = True
        self.superuser.first_name, self.superuser.last_name = "Site", "Owner"
        self.superuser.save()
        self.club = Club.objects.create(name="Faraway Aquarium Society")
        self.other_auction = Auction.objects.create(
            created_by=self.user_with_no_lots,
            title="Somebody else's swap",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=5),
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        self.linked = AuctionHistory.objects.create(
            auction=self.other_auction,
            user=self.superuser,
            action="Assigned to club 'Faraway Aquarium Society' from the unlinked auctions page.",
            applies_to="RULES",
        )
        self.old = AuctionHistory.objects.create(
            auction=self.online_auction, user=self.admin_user, action="Edited the rules.", applies_to="RULES"
        )
        AuctionHistory.objects.filter(pk=self.old.pk).update(timestamp=timezone.now() - datetime.timedelta(hours=5))
        self.granted = ClubHistory.objects.create(
            club=self.club,
            user=self.superuser,
            action="Granted admin permissions to Someone from the unlinked auctions page.",
            applies_to="MEMBERS",
        )

    def _run(self, user, name, params):
        request = RequestFactory().post("/")
        request.user = user
        request.palette_page = {}
        return palette_actions.run_action(request, name, params)

    def _whats(self, result):
        return [row["what"] for row in result.get("changes", [])]

    def test_a_superuser_reads_every_auction_at_once(self):
        result = self._run(self.superuser, "recent_changes", {"all_auctions": True, "hours": 1})
        self.assertEqual(result["count"], 1)
        self.assertIn("Assigned to club", self._whats(result)[0])
        self.assertEqual(result["changes"][0]["auction_slug"], self.other_auction.slug)

    def test_all_as_the_auction_name_means_every_auction(self):
        result = self._run(self.superuser, "recent_changes", {"auction": "all", "search": "edited the rules"})
        self.assertEqual(result["count"], 1)

    def test_an_auction_admin_cannot_read_every_auction(self):
        result = self._run(self.admin_user, "recent_changes", {"all_auctions": True})
        self.assertNotIn("Assigned to club", str(result))

    def test_filtering_by_who_made_the_change(self):
        result = self._run(self.superuser, "recent_changes", {"all_auctions": True, "by": "site owner"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            self._run(self.superuser, "recent_changes", {"all_auctions": True, "by": "admin_user"})["count"], 1
        )

    def test_a_time_range(self):
        since = (timezone.now() - datetime.timedelta(hours=6)).isoformat()
        until = (timezone.now() - datetime.timedelta(hours=4)).isoformat()
        result = self._run(self.superuser, "recent_changes", {"all_auctions": True, "since": since, "until": until})
        self.assertEqual(len(self._whats(result)), 1)
        self.assertIn("Edited the rules.", self._whats(result)[0])

    def test_an_unreadable_time_is_refused_not_ignored(self):
        result = self._run(self.superuser, "recent_changes", {"all_auctions": True, "since": "last tuesday"})
        self.assertNotIn("changes", result)

    def test_hours_filter_on_one_auction_for_its_admin(self):
        result = self._run(self.admin_user, "recent_changes", {"auction": self.online_auction.slug, "hours": 1})
        self.assertNotIn("Edited the rules.", self._whats(result))

    def test_a_superuser_reads_a_club_they_are_not_in(self):
        result = self._run(self.superuser, "club_history", {"club": "Faraway Aquarium Society"})
        self.assertEqual(result["club"], "Faraway Aquarium Society")
        self.assertIn("Granted admin permissions", self._whats(result)[0])

    def test_a_superuser_reads_every_club_at_once(self):
        result = self._run(self.superuser, "club_history", {"all_clubs": True, "search": "granted admin", "hours": 1})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["changes"][0]["club_slug"], self.club.slug)

    def test_an_ordinary_user_still_cannot_read_a_strangers_club(self):
        result = self._run(self.user, "club_history", {"club": "Faraway Aquarium Society"})
        self.assertNotIn("Granted admin permissions", str(result))
        result = self._run(self.user, "club_history", {"all_clubs": True})
        self.assertNotIn("Granted admin permissions", str(result))
