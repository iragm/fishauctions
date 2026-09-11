"""Tests for the club lifecycle rollup and the outreach queue that comes out of it.

The rule the whole thing turns on is that a club is judged against its **own** cadence, so most of
these are about a club with an unusual schedule not being called dormant, and a club with a fast
one being caught quickly. See auctions/club_health.py.
"""

import datetime

from django.conf import settings
from django.test import TestCase, override_settings
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
from auctions.models import Auction, Club, ClubLadderSnapshot, ClubMember
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

    def test_removed_members_are_not_counted_as_members(self):
        """A club that emptied out must not read as staffed, or as using the members feature."""
        from auctions.models import ClubMember

        club = self._club()
        member = ClubMember.objects.create(club=club, user=self.user_with_no_lots)
        self.assertEqual(compute_club_health(club).members, 1)
        member.is_deleted = True
        member.save()
        health = compute_club_health(club)
        self.assertEqual(health.members, 0)
        self.assertNotIn("members", health.tools_used)

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

    def test_the_recoverable_cases_survive_the_limit(self):
        """Ordering has to happen before the cut, not after.

        Meta.ordering is "-overdue_ratio" and a trial or empty club has no ratio at all. NULLs sort
        last under DESC on MariaDB, so slicing the queryset and sorting the slice throws away
        exactly the two stages this queue is meant to lead with.
        """
        for index in range(30):
            ClubHealth.objects.create(
                club=Club.objects.create(name=f"Stopped {index}"),
                stage="dormant",
                due_for_checkin=True,
                overdue_ratio=10 + index,
            )
        ClubHealth.objects.create(club=Club.objects.create(name="Tried it once"), stage="trial", due_for_checkin=True)
        queue = due_for_checkin(limit=5)
        self.assertEqual(len(queue), 5)
        self.assertEqual(queue[0].club.name, "Tried it once")

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


class LadderTests(StandardTestCase):
    """The two halves of a club's stage, on one order, with the furthest-along one winning."""

    def test_a_club_nobody_has_approved_starts_at_the_bottom(self):
        club = Club.objects.create(name="Found on a directory page")
        self.assertEqual(club.outreach_stage, Club.PROSPECT)
        self.assertEqual(club_health.ladder_position(club)["stage"], "unaware")

    def test_the_hand_set_half_answers_when_nothing_is_derived(self):
        club = Club.objects.create(name="Approved, done nothing", outreach_stage=Club.LISTED)
        compute_club_health(club)
        club.refresh_from_db()
        position = club_health.ladder_position(club)
        self.assertEqual(position["stage"], "listed")
        self.assertEqual(position["source"], "hand")

    def test_the_derived_half_wins_when_it_is_further_along(self):
        """A club we only ever emailed can be further along than we think, and usually is."""
        club = Club.objects.create(name="Quietly running auctions", outreach_stage=Club.CONTACTED)
        health = ClubHealth.objects.create(club=club, stage="active")
        position = club_health.ladder_position(club, health)
        self.assertEqual(position["stage"], "active")
        self.assertEqual(position["source"], "derived")

    def test_an_empty_rollup_never_pushes_a_club_up_the_ladder(self):
        """Every prospect derives "empty", so ranking it would report a club nobody has heard of as
        further along than one somebody just wrote to."""
        club = Club.objects.create(name="A name and a postcode")
        health = ClubHealth.objects.create(club=club, stage="empty")
        self.assertEqual(club_health.ladder_position(club, health)["stage"], "unaware")

    def test_the_rungs_are_in_order(self):
        order = [key for key, _label, _half in club_health.LADDER]
        self.assertEqual(order[:3], ["unaware", "contacted", "listed"])
        self.assertLess(club_health.LADDER_RANK["new"], club_health.LADDER_RANK["dormant"])
        self.assertLess(club_health.LADDER_RANK["dormant"], club_health.LADDER_RANK["slipping"])
        self.assertLess(club_health.LADDER_RANK["slipping"], club_health.LADDER_RANK["active"])

    def test_the_nightly_rollup_never_writes_the_hand_set_half(self):
        """It is rebuilt from scratch every night; anything a person decided has to survive that."""
        club = Club.objects.create(name="Hand set", outreach_stage=Club.CONTACTED)
        compute_club_health(club)
        club.refresh_from_db()
        self.assertEqual(club.outreach_stage, Club.CONTACTED)

    def test_counts_add_up_to_every_club(self):
        Club.objects.create(name="One", outreach_stage=Club.PROSPECT)
        Club.objects.create(name="Two", outreach_stage=Club.LISTED)
        rows = club_health.ladder_counts()
        self.assertEqual(sum(row["clubs"] for row in rows), Club.objects.count())

    def test_counting_the_ladder_is_two_queries_however_many_clubs_have_no_rollup(self):
        """Clubs with no rollup are the ones club discovery adds in bulk, so a fetch per club here
        is a page that gets slower every time the campaign works."""
        for number in range(5):
            Club.objects.create(name=f"No rollup {number}")
        with self.assertNumQueries(2):
            club_health.ladder_counts()
        compute_club_health(Club.objects.create(name="With a rollup"))
        with self.assertNumQueries(2):
            club_health.ladder_counts()

    def test_a_club_asked_about_on_its_own_still_looks_its_rollup_up(self):
        """The sentinel default is what keeps that convenience without costing the loop above."""
        club = Club.objects.create(name="Asked about alone", outreach_stage=Club.CONTACTED)
        compute_club_health(club)
        ClubHealth.objects.filter(club=club).update(stage="active")
        self.assertEqual(club_health.ladder_position(Club.objects.get(pk=club.pk))["stage"], "active")


class AwareStageTests(StandardTestCase):
    """A club with members here and no auctions is not the same club as a name on a list."""

    def test_a_club_with_a_member_here_is_aware_rather_than_empty(self):
        club = Club.objects.create(name="Has a member")
        ClubMember.objects.create(club=club, user=self.user, name="A member")
        health = compute_club_health(club)
        self.assertEqual(health.stage, "aware")
        self.assertEqual(health.people_here, 1)

    def test_somebody_naming_the_club_on_their_own_page_counts(self):
        """The other link, and the one the club itself never sees: UserData.club."""
        club = Club.objects.create(name="Named by a user")
        userdata = self.user.userdata
        userdata.club = club
        userdata.save()
        health = compute_club_health(club)
        self.assertEqual(health.stage, "aware")

    def test_one_person_with_both_links_is_one_person(self):
        club = Club.objects.create(name="Both links")
        ClubMember.objects.create(club=club, user=self.user, name="A member")
        userdata = self.user.userdata
        userdata.club = club
        userdata.save()
        self.assertEqual(compute_club_health(club).people_here, 1)

    def test_an_aware_club_is_still_on_the_outreach_queue(self):
        """It is the most recoverable club on the list: the members are already here."""
        club = Club.objects.create(name="Aware and idle")
        ClubMember.objects.create(club=club, user=self.user, name="A member")
        self.assertTrue(compute_club_health(club).due_for_checkin)

    def test_a_deleted_member_is_not_a_member(self):
        club = Club.objects.create(name="Emptied out")
        ClubMember.objects.create(club=club, user=self.user, name="Gone", is_deleted=True)
        self.assertEqual(compute_club_health(club).stage, "empty")


class MapGateTests(StandardTestCase):
    """A club nobody has approved is not a claim this site makes anywhere."""

    def setUp(self):
        super().setUp()
        self.listed = Club.objects.create(
            name="Listed Aquarium Society", outreach_stage=Club.LISTED, latitude=42.0, longitude=-73.0
        )
        self.prospect = Club.objects.create(
            name="Prospect Aquarium Society", outreach_stage=Club.PROSPECT, latitude=42.1, longitude=-73.1
        )

    def test_listed_is_the_gate_and_active_is_the_other_half(self):
        self.assertIn(self.listed, Club.objects.listed())
        self.assertNotIn(self.prospect, Club.objects.listed())
        self.listed.active = False
        self.listed.save()
        self.assertNotIn(self.listed, Club.objects.listed())

    @override_settings(LOCATION_FIELD={**settings.LOCATION_FIELD, "provider.google.api_key": "test-key"})
    def test_a_prospect_is_not_on_the_map(self):
        """The key is pinned because the pins only exist when there is a map to put them on.

        clubs.html renders every club name inside ``{% if google_maps_api_key %}``, and CI runs with
        an empty ``GOOGLE_MAPS_API_KEY`` while a dev .env has a real one -- so without this the test
        asserts against an empty page in CI and a full one here.
        """
        response = self.client.get(reverse("clubs"))
        if response.status_code != 200:
            self.skipTest("the club finder is disabled in this environment")
        self.assertContains(response, "Listed Aquarium Society")
        self.assertNotContains(response, "Prospect Aquarium Society")

    def test_a_prospect_is_not_in_the_club_autocomplete(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post("/api/clubs/", {"search": "Aquarium Society"})
        names = [row["name"] for row in response.json()]
        self.assertIn("Listed Aquarium Society", names)
        self.assertNotIn("Prospect Aquarium Society", names)

    def test_a_prospect_is_not_in_the_command_palette(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("command_palette"), {"q": "Aquarium Society"})
        found = [item["title"] for group in response.json()["groups"] for item in group["items"]]
        self.assertIn("Listed Aquarium Society", found)
        self.assertNotIn("Prospect Aquarium Society", found)


class StallReasonTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")

    def test_the_queue_records_why_a_club_stopped(self):
        club = Club.objects.create(name="Answered the email")
        compute_club_health(club)
        self.client.post(reverse("club_mark_contacted", kwargs={"pk": club.pk}), {"stall_reason": "paper"})
        club.refresh_from_db()
        self.assertEqual(club.stall_reason, "paper")

    def test_a_post_that_says_nothing_about_the_reason_leaves_it_alone(self):
        """ "" is a legal value in this vocabulary ("Not known"), so an absent field must not read
        as one."""
        club = Club.objects.create(name="Already answered", stall_reason="cost")
        compute_club_health(club)
        self.client.post(reverse("club_mark_contacted", kwargs={"pk": club.pk}))
        club.refresh_from_db()
        self.assertEqual(club.stall_reason, "cost")
        self.assertIsNotNone(club.date_contacted)

    def test_the_reason_can_be_cleared_on_purpose(self):
        club = Club.objects.create(name="Answered then unanswered", stall_reason="cost")
        compute_club_health(club)
        self.client.post(reverse("club_mark_contacted", kwargs={"pk": club.pk}), {"stall_reason": ""})
        club.refresh_from_db()
        self.assertEqual(club.stall_reason, "")

    def test_a_reason_outside_the_vocabulary_is_ignored(self):
        """Free text here would be Club.notes again, which is the thing that cannot be counted."""
        club = Club.objects.create(name="Said something else")
        compute_club_health(club)
        self.client.post(reverse("club_mark_contacted", kwargs={"pk": club.pk}), {"stall_reason": "they hate blue"})
        club.refresh_from_db()
        self.assertEqual(club.stall_reason, "")
        self.assertIsNotNone(club.date_contacted)


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


class LadderSnapshotTests(TestCase):
    """The ladder as a trend, which is the only part of phase 8f that is code.

    ``ClubHealth`` is a ``OneToOneField`` rewritten nightly, so it holds only today: the moment a
    club moves up a rung, where it used to be is gone.  These tests are about the two properties
    that makes the snapshot worth a table at all -- it is idempotent within a month, and a month
    nobody recorded reads as a gap rather than as zero clubs.
    """

    def test_a_month_is_recorded_once_and_then_refreshed_in_place(self):
        """The nightly task calls this unconditionally; it must not write thirty rows a month."""
        Club.objects.create(name="Snapshot club one")
        club_health.snapshot_ladder()
        first = ClubLadderSnapshot.objects.count()
        self.assertGreater(first, 0)
        Club.objects.create(name="Snapshot club two")
        club_health.snapshot_ladder()
        self.assertEqual(ClubLadderSnapshot.objects.count(), first)
        self.assertEqual(sum(ClubLadderSnapshot.objects.values_list("clubs", flat=True)), 2)

    def test_two_months_are_two_columns(self):
        Club.objects.create(name="Snapshot club three")
        club_health.snapshot_ladder(when=timezone.now() - timezone.timedelta(days=40))
        club_health.snapshot_ladder()
        history = club_health.ladder_history()
        self.assertEqual(len(history["months"]), 2)
        self.assertEqual(history["months"], sorted(history["months"]))

    def test_a_month_nobody_recorded_is_a_gap_and_not_a_zero(self):
        """A zero would say every club left that rung.  The truth is that nobody was looking."""
        Club.objects.create(name="Snapshot club four")
        club_health.snapshot_ladder()
        ClubLadderSnapshot.objects.filter(stage="listed").delete()
        history = club_health.ladder_history()
        listed = next(row for row in history["rows"] if row["stage"] == "listed")
        self.assertEqual(listed["counts"], [None])

    def test_no_snapshots_is_an_empty_report_rather_than_a_row_of_zeroes(self):
        self.assertEqual(club_health.ladder_history(), {"months": [], "rows": []})
