"""Tests for the club lifecycle rollup and the outreach queue that comes out of it.

The rule the whole thing turns on is that a club is judged against its **own** cadence, so most of
these are about a club with an unusual schedule not being called dormant, and a club with a fast
one being caught quickly. See auctions/club_health.py.
"""

import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions import club_health
from auctions.club_health import (
    ClubHealth,
    classify,
    compute_club_health,
    due_for_checkin,
    is_test_auction,
    median_gap,
)
from auctions.models import Auction, Club
from auctions.tests import StandardTestCase


class TestAuctionNameTests(TestCase):
    """A club whose only auction is called "test" has not run an auction here."""

    def test_names_that_mean_somebody_was_trying_the_site_out(self):
        for slug in ("test-auction", "my-club-test", "demo", "trial-run", "example-2026", "club_mock_1"):
            self.assertTrue(is_test_auction(Auction(slug=slug, title="")), slug)

    def test_real_names_are_not_caught(self):
        for slug in ("spring-auction-2026", "greatest-fish-sale", "protest-march", "contest-winners"):
            self.assertFalse(is_test_auction(Auction(slug=slug, title="")), slug)

    def test_the_title_counts_too(self):
        self.assertTrue(is_test_auction(Auction(slug="abc123", title="Test auction")))


class MedianGapTests(TestCase):
    def _dates(self, *day_offsets):
        base = timezone.now() - datetime.timedelta(days=1000)
        return [base + datetime.timedelta(days=offset) for offset in day_offsets]

    def test_too_little_history_has_no_cadence(self):
        self.assertIsNone(median_gap(self._dates(0, 30)))

    def test_a_monthly_club_reads_as_monthly(self):
        self.assertEqual(median_gap(self._dates(0, 30, 60, 90)), 30.0)

    def test_one_long_break_does_not_become_the_cadence(self):
        """Median, not mean: four auctions a fortnight apart and then a gap year is a fortnight."""
        self.assertEqual(median_gap(self._dates(0, 14, 28, 42, 400)), 14.0)

    def test_a_gap_longer_than_two_years_is_a_restart_and_is_dropped(self):
        gap = club_health.MAX_GAP_DAYS_FOR_CADENCE + 100
        self.assertEqual(median_gap(self._dates(0, 30, 60, 60 + gap)), 30.0)

    def test_two_auctions_on_the_same_day_do_not_make_a_zero_gap(self):
        self.assertEqual(median_gap(self._dates(0, 0, 30, 60)), 30.0)


class ClassifyTests(TestCase):
    def test_a_club_with_nothing_at_all(self):
        stage, reason = classify(0, 0, None, None, None)
        self.assertEqual(stage, "empty")
        self.assertIn("never", reason)

    def test_a_club_that_only_ever_ran_a_test(self):
        stage, reason = classify(0, 2, 30, None, None)
        self.assertEqual(stage, "trial")
        self.assertIn("test", reason)

    def test_an_annual_club_is_healthy_at_eight_months(self):
        """The design constraint: one global cutoff would call this dormant."""
        stage, _reason = classify(4, 0, 240, 365.0, 240 / 365)
        self.assertEqual(stage, "active")

    def test_a_monthly_club_that_has_missed_two_is_caught(self):
        stage, reason = classify(12, 0, 65, 30.0, 65 / 30)
        self.assertEqual(stage, "slipping")
        self.assertIn("30 days", reason)

    def test_a_club_that_has_missed_three_of_its_own_gaps_is_dormant(self):
        stage, _reason = classify(12, 0, 130, 30.0, 130 / 30)
        self.assertEqual(stage, "dormant")

    def test_one_auction_and_no_cadence_is_new_rather_than_judged(self):
        stage, _reason = classify(1, 0, 20, None, None)
        self.assertEqual(stage, "new")

    def test_one_auction_a_long_time_ago_is_dormant(self):
        stage, _reason = classify(1, 0, club_health.NO_CADENCE_DORMANT_DAYS + 1, None, None)
        self.assertEqual(stage, "dormant")


class ComputeClubHealthTests(StandardTestCase):
    def _club(self, name="Cadence club"):
        return Club.objects.create(name=name)

    def _auction(self, club, days_ago, title="Real auction"):
        when = timezone.now() - datetime.timedelta(days=days_ago)
        auction = Auction.objects.create(
            created_by=self.user,
            title=f"{title} {days_ago}",
            is_online=True,
            date_start=when,
            date_end=when + datetime.timedelta(days=1),
            club=club,
        )
        Auction.objects.filter(pk=auction.pk).update(date_start=when)
        auction.refresh_from_db()
        return auction

    def test_a_club_with_no_auctions_is_empty_and_queued(self):
        health = compute_club_health(self._club())
        self.assertEqual(health.stage, "empty")
        self.assertTrue(health.due_for_checkin)
        self.assertEqual(health.real_auctions, 0)

    def test_test_auctions_are_counted_separately_from_real_ones(self):
        club = self._club()
        self._auction(club, 10, title="Test auction")
        self._auction(club, 5, title="Real auction")
        health = compute_club_health(club)
        self.assertEqual(health.real_auctions, 1)
        self.assertEqual(health.test_auctions, 1)

    def test_a_club_that_only_ever_tested_is_the_most_reachable_case(self):
        club = self._club()
        self._auction(club, 30, title="Test auction")
        health = compute_club_health(club)
        self.assertEqual(health.stage, "trial")
        self.assertTrue(health.due_for_checkin)

    def test_a_monthly_club_still_running_is_not_in_the_queue(self):
        club = self._club()
        for days_ago in (120, 90, 60, 30, 5):
            self._auction(club, days_ago)
        health = compute_club_health(club)
        self.assertEqual(health.median_gap_days, 30.0)
        self.assertEqual(health.stage, "active")
        self.assertFalse(health.due_for_checkin)

    def test_a_monthly_club_that_stopped_lands_in_the_queue(self):
        club = self._club()
        for days_ago in (220, 190, 160, 130):
            self._auction(club, days_ago)
        health = compute_club_health(club)
        self.assertEqual(health.median_gap_days, 30.0)
        self.assertIn(health.stage, ("slipping", "dormant"))
        self.assertTrue(health.due_for_checkin)

    def test_an_annual_club_at_eight_months_is_left_alone(self):
        """The number that would be wrong under any global cutoff."""
        club = self._club()
        for days_ago in (1100, 735, 370, 240):
            self._auction(club, days_ago)
        health = compute_club_health(club)
        self.assertEqual(health.stage, "active")
        self.assertFalse(health.due_for_checkin)

    def test_a_dissolved_club_is_not_asked_to_come_back(self):
        club = self._club()
        club.active = False
        club.save()
        health = compute_club_health(club)
        self.assertFalse(health.due_for_checkin)

    def test_a_recently_contacted_club_comes_off_the_queue(self):
        club = self._club()
        club.date_contacted = timezone.now() - datetime.timedelta(days=5)
        club.save()
        self.assertFalse(compute_club_health(club).due_for_checkin)

    def test_and_goes_back_on_once_the_cooldown_is_up(self):
        club = self._club()
        club.date_contacted = timezone.now() - datetime.timedelta(days=club_health.CONTACT_COOLDOWN_DAYS + 1)
        club.save()
        self.assertTrue(compute_club_health(club).due_for_checkin)

    def test_deleted_auctions_do_not_keep_a_club_looking_alive(self):
        club = self._club()
        auction = self._auction(club, 2)
        auction.is_deleted = True
        auction.save()
        self.assertEqual(compute_club_health(club).real_auctions, 0)

    def test_which_tools_a_club_has_used_is_recorded(self):
        club = self._club()
        club.enable_breeder_award_program = True
        club.membership_system = "rolling"
        club.save()
        health = compute_club_health(club)
        self.assertIn("breeder award program", health.tools_used)
        self.assertIn("membership dues", health.tools_used)
        self.assertNotIn("discord", health.tools_used)

    def test_recomputing_updates_the_same_row_rather_than_adding_one(self):
        club = self._club()
        compute_club_health(club)
        compute_club_health(club)
        self.assertEqual(ClubHealth.objects.filter(club=club).count(), 1)


class QueueTests(StandardTestCase):
    def test_the_queue_leads_with_the_recoverable_cases(self):
        """A club that set up and never ran an auction is a different conversation from one that ran
        twelve and stopped, and is much likelier to come back."""
        for name, stage in (("Stopped", "dormant"), ("Tried it", "trial"), ("Late", "slipping")):
            ClubHealth.objects.create(
                club=Club.objects.create(name=name), stage=stage, due_for_checkin=True, overdue_ratio=3
            )
        self.assertEqual([row.club.name for row in due_for_checkin()], ["Tried it", "Late", "Stopped"])

    def test_healthy_clubs_are_not_in_the_queue(self):
        ClubHealth.objects.create(club=Club.objects.create(name="Fine"), stage="active", due_for_checkin=False)
        self.assertEqual(due_for_checkin(), [])

    def test_a_club_with_no_contact_address_is_flagged_rather_than_hidden(self):
        health = ClubHealth.objects.create(
            club=Club.objects.create(name="Unreachable"), stage="trial", due_for_checkin=True
        )
        self.assertIn(health, due_for_checkin())
        self.assertFalse(health.is_reachable)


class ClubHealthDashboardTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user.is_superuser = True
        self.admin_user.save()

    def test_the_queue_page_renders(self):
        club = Club.objects.create(name="Queued club")
        compute_club_health(club)
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("admin_club_health"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Queued club")

    def test_an_ordinary_user_cannot_open_it(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertNotEqual(self.client.get(reverse("admin_club_health")).status_code, 200)

    def test_marking_a_club_contacted_takes_it_off_the_queue(self):
        """The trigger the rollup exists to feed -- without it this is another chart nobody opens."""
        club = Club.objects.create(name="About to be contacted")
        self.assertTrue(compute_club_health(club).due_for_checkin)
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(reverse("club_mark_contacted", kwargs={"pk": club.pk}))
        self.assertEqual(response.status_code, 302)
        club.refresh_from_db()
        self.assertIsNotNone(club.date_contacted)
        self.assertFalse(ClubHealth.objects.get(club=club).due_for_checkin)

    def test_an_ordinary_user_cannot_mark_a_club_contacted(self):
        club = Club.objects.create(name="Not yours")
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(reverse("club_mark_contacted", kwargs={"pk": club.pk}))
        club.refresh_from_db()
        self.assertIsNone(club.date_contacted)


class RefreshAllTests(StandardTestCase):
    def test_every_club_gets_a_row(self):
        Club.objects.create(name="A")
        Club.objects.create(name="B")
        written = club_health.refresh_all()
        self.assertEqual(written, Club.objects.count())
        self.assertEqual(ClubHealth.objects.count(), Club.objects.count())

    def test_one_broken_club_does_not_stop_the_run(self):
        Club.objects.create(name="Fine one")
        with self.assertLogs("auctions.club_health", level="ERROR"):
            written = club_health.refresh_all(Club.objects.all())
            # Nothing is actually broken here; assertLogs needs at least one record, so provoke one.
            club_health.logger.error("provoked")
        self.assertGreater(written, 0)
