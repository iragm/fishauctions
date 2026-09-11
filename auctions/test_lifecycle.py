"""Tests for phase 9: the milestones, the lapsing definition, the session replay and the cohorts.

Three of these have teeth beyond "the function returns a number".

**The lapsing definition** is the one most likely to be got wrong by a later edit, and it is wrong
in a way that reads as good news: counting a right-censored person as retained makes every
retention number look better than it is.  ``test_the_last_auction_cannot_report_lapsing`` and
``test_somebody_who_came_back_is_not_lapsed`` are the ratchet on that.

**The sign-in stitch** has one non-obvious mechanic and the whole feature rests on it:
``django.contrib.auth.login`` cycles the session key *before* it sends ``user_logged_in``, so a
receiver that reads ``request.session.session_key`` records the key issued a moment ago and stitches
a sign-in to itself.  ``test_the_stitch_records_the_key_the_browser_sent`` fails if anybody
"simplifies" the cookie read back to the session.

**The median member has to be a real person.**  A later edit to a mean, or to the top of the
distribution, would be invisible on a fixture where everybody did the same thing -- so the fixture
here has one power user in it, which is the case the median exists to survive.
"""

from datetime import timedelta

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions import lifecycle
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    Invoice,
    Lot,
    PageView,
    PickupLocation,
    SignInStitch,
)
from auctions.tests import StandardTestCase
from auctions.usability_report import route_name


class ClubHistoryFixture(TestCase):
    """A club with four auctions and people who came and went between them."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="cohort_owner", password="x")
        cls.club = Club.objects.create(name="Cohort Aquarium Society")
        cls.auctions = []
        for index in range(4):
            auction = Auction.objects.create(
                created_by=cls.owner,
                title=f"Cohort auction {index}",
                is_online=False,
                club=cls.club,
                date_start=timezone.now() - timedelta(days=400 - index * 90),
            )
            PickupLocation.objects.create(
                name=f"cohort location {index}", auction=auction, pickup_time=auction.date_start
            )
            cls.auctions.append(auction)

    def join(self, auction, *, user=None, email=None, number="1"):
        return AuctionTOS.objects.create(
            user=user,
            email=email,
            auction=auction,
            pickup_location=auction.location_qs.first(),
            bidder_number=number,
        )


class LapsingTests(ClubHistoryFixture):
    """Lapsing is counted in the club's own auctions, and the last one cannot report it."""

    def test_somebody_who_stopped_coming_is_lapsed(self):
        person = User.objects.create_user(username="lapser", password="x")
        self.join(self.auctions[0], user=person, number="10")
        lapsed, measurable = lifecycle.lapsed_participants(self.club, self.auctions[0])
        self.assertTrue(measurable)
        self.assertEqual(lapsed, {f"u{person.pk}"})

    def test_somebody_who_came_back_is_not_lapsed(self):
        person = User.objects.create_user(username="returner", password="x")
        self.join(self.auctions[0], user=person, number="11")
        self.join(self.auctions[2], user=person, number="11")
        lapsed, measurable = lifecycle.lapsed_participants(self.club, self.auctions[0])
        self.assertTrue(measurable)
        self.assertEqual(lapsed, set())

    def test_the_last_auction_cannot_report_lapsing(self):
        """Right-censoring, which is the mistake that makes every retention number look good.

        Nobody at the most recent auction has been *asked* to come back yet, so they are neither
        lapsed nor retained.  The honest answer is that the question does not have one.
        """
        person = User.objects.create_user(username="censored", password="x")
        self.join(self.auctions[-1], user=person, number="12")
        lapsed, measurable = lifecycle.lapsed_participants(self.club, self.auctions[-1])
        self.assertFalse(measurable)
        self.assertEqual(lapsed, set())

    def test_the_second_to_last_auction_is_censored_too(self):
        """Two auctions have to have passed, not one -- ``LAPSED_AFTER_AUCTIONS`` is 2."""
        _, measurable = lifecycle.lapsed_participants(self.club, self.auctions[-2])
        self.assertFalse(measurable)

    def test_a_person_with_no_account_is_still_the_same_person_next_time(self):
        """The non-user persona: an organizer typed them in, and the email is all there is.

        Without this the seller who brings lots on a piece of paper is a new person at every
        auction, and a club that keeps the same members forever reads as one that replaces all of
        them every time.
        """
        self.join(self.auctions[0], email="Paper@Example.com", number="20")
        self.join(self.auctions[1], email="paper@example.com", number="21")
        lapsed, measurable = lifecycle.lapsed_participants(self.club, self.auctions[0])
        self.assertTrue(measurable)
        self.assertEqual(lapsed, set())


class CohortTests(ClubHistoryFixture):
    def test_a_returning_member_is_new_once_and_returning_after(self):
        person = User.objects.create_user(username="cohort_member", password="x")
        self.join(self.auctions[1], user=person, number="30")
        self.join(self.auctions[2], user=person, number="30")
        cohorts = {row.auction.pk: row for row in lifecycle.club_cohorts(self.club)}
        self.assertEqual(cohorts[self.auctions[1].pk].new_people, 1)
        self.assertEqual(cohorts[self.auctions[1].pk].returning, 0)
        self.assertEqual(cohorts[self.auctions[2].pk].new_people, 0)
        self.assertEqual(cohorts[self.auctions[2].pk].returning, 1)

    def test_joining_is_not_participating(self):
        """ "Participated" is bought or sold -- never "logged in", and never merely joined."""
        joiner = User.objects.create_user(username="joiner_only", password="x")
        buyer = User.objects.create_user(username="cohort_buyer", password="x")
        self.join(self.auctions[1], user=joiner, number="31")
        seller_tos = self.join(self.auctions[1], user=self.owner, number="32")
        buyer_tos = self.join(self.auctions[1], user=buyer, number="33")
        Lot.objects.create(
            lot_name="A cohort lot",
            auction=self.auctions[1],
            auctiontos_seller=seller_tos,
            auctiontos_winner=buyer_tos,
            quantity=1,
            winning_price=5,
        )
        cohorts = {row.auction.pk: row for row in lifecycle.club_cohorts(self.club)}
        row = cohorts[self.auctions[1].pk]
        self.assertEqual(row.new_people, 3)
        # The seller and the buyer, not the person who only signed the terms.
        self.assertEqual(row.new_who_participated, 2)

    def test_cohorts_are_oldest_first(self):
        """A trend read backwards is a different trend."""
        rows = lifecycle.club_cohorts(self.club)
        dates = [row.auction.date_start for row in rows]
        self.assertEqual(dates, sorted(dates))


class SignInStitchTests(TestCase):
    """The one row phase 9 adds, and the mechanic the whole thing rests on."""

    def setUp(self):
        self.password = "a-long-enough-password"
        self.person = User.objects.create_user(
            username="stitcher", password=self.password, email="stitcher@example.com"
        )
        # allauth refuses the sign-in and redirects to /confirm-email/ without this, so
        # ``user_logged_in`` never fires and the test would pass or fail for the wrong reason.
        EmailAddress.objects.create(user=self.person, email=self.person.email, verified=True, primary=True)

    def test_the_stitch_records_the_key_the_browser_sent(self):
        """Not the key ``login()`` cycled to a moment before the signal fired.

        ``django.contrib.auth.login`` calls ``session.cycle_key()`` *before* sending
        ``user_logged_in``, so a receiver reading ``request.session.session_key`` gets the new key
        and stitches the sign-in to itself -- recording a row that joins nothing.  The cookie is
        what the browser sent, which is the key every anonymous ``PageView`` in the visit carries.
        """
        # An anonymous visit first, so the client is holding a session key when it signs in.
        self.client.get(reverse("allLots"))
        before = self.client.session.session_key
        self.assertTrue(before)
        self.client.post(
            reverse("account_login"),
            {"login": self.person.username, "password": self.password},
        )
        after = self.client.session.session_key
        stitched = lifecycle.stitched_sessions(self.person)
        self.assertEqual(stitched, [before])
        self.assertNotIn(after, stitched, "the stitch recorded the post-login key, which joins nothing")

    def test_signing_in_twice_from_one_browser_is_one_stitch(self):
        self.client.get(reverse("allLots"))
        key = self.client.session.session_key
        for _ in range(2):
            SignInStitch.objects.get_or_create(user=self.person, session_id=key)
        self.assertEqual(SignInStitch.objects.filter(user=self.person).count(), 1)

    def test_stitching_began_is_none_when_nothing_is_stitched(self):
        self.assertIsNone(lifecycle.stitching_began())


class SessionTimelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = User.objects.create_user(username="timeline_person", password="x")
        cls.start = timezone.now() - timedelta(hours=4)

    def setUp(self):
        route_name.cache_clear()

    def _view(self, minutes, **kwargs):
        view = PageView.objects.create(url=kwargs.pop("url", "/lots/"), title="t", **kwargs)
        # date_start is auto_now_add, so the only way to place a row in time is to write it back.
        PageView.objects.filter(pk=view.pk).update(date_start=self.start + timedelta(minutes=minutes))
        return view

    def test_the_timeline_is_in_order_with_the_gaps(self):
        self._view(0, session_id="tl-session", url="/lots/")
        self._view(3, session_id="tl-session", url="/auctions/")
        steps = lifecycle.session_timeline(session_id="tl-session")
        self.assertEqual([step.view.url for step in steps], ["/lots/", "/auctions/"])
        self.assertIsNone(steps[0].gap)
        self.assertEqual(steps[1].gap, timedelta(minutes=3))

    def test_a_long_gap_marks_a_new_visit(self):
        self._view(0, session_id="tl-gap")
        self._view(90, session_id="tl-gap")
        steps = lifecycle.session_timeline(session_id="tl-gap")
        self.assertFalse(steps[0].new_visit)
        self.assertTrue(steps[1].new_visit)

    def test_a_users_timeline_includes_the_anonymous_half_once_it_is_stitched(self):
        """The seam this phase exists to close, and the only honest key across it."""
        self._view(0, session_id="tl-anon")
        self._view(10, user=self.person)
        unstitched = lifecycle.session_timeline(user=self.person)
        self.assertEqual(len(unstitched), 1)
        SignInStitch.objects.create(user=self.person, session_id="tl-anon")
        stitched = lifecycle.session_timeline(user=self.person)
        self.assertEqual(len(stitched), 2)
        self.assertEqual(stitched[0].view.session_id, "tl-anon")

    def test_no_subject_returns_nothing_rather_than_the_whole_table(self):
        self._view(0, session_id="tl-anon")
        self.assertEqual(lifecycle.session_timeline(), [])


class MedianMemberTests(ClubHistoryFixture):
    def test_the_median_is_a_real_person_and_not_the_power_user(self):
        """The mean of this distribution is meaningless: one power user moves it past everybody.

        Five members: three did one lot each, one did nothing, one did twenty.  The mean is five,
        which is more than four of the five people managed.  The median is one, which is a person.
        """
        auction = self.auctions[1]
        people = []
        for index in range(5):
            tos = self.join(
                auction, user=User.objects.create_user(username=f"median_{index}", password="x"), number=f"4{index}"
            )
            people.append(tos)
        counts = [0, 1, 1, 1, 20]
        for tos, count in zip(people, counts, strict=True):
            for lot_index in range(count):
                Lot.objects.create(
                    lot_name=f"lot {tos.pk}-{lot_index}",
                    auction=auction,
                    auctiontos_seller=tos,
                    quantity=1,
                )
        median = lifecycle.median_member(auction)
        self.assertIsNotNone(median)
        self.assertEqual(median.lots_qs.count(), 1)

    def test_an_auction_nobody_joined_has_no_median_member(self):
        self.assertIsNone(lifecycle.median_member(self.auctions[3]))

    def test_the_story_of_somebody_with_no_account_has_an_empty_timeline(self):
        """The non-user persona.  The emptiness is the finding, not a gap in the data."""
        auction = self.auctions[2]
        self.join(auction, email="nobody@example.com", number="50")
        story = lifecycle.median_member_story(auction)
        self.assertIsNotNone(story)
        self.assertEqual(story["timeline"], [])


class UnreachedShareTests(ClubHistoryFixture):
    def test_the_people_the_site_never_spoke_to_are_counted(self):
        auction = self.auctions[1]
        self.join(auction, email="silent@example.com", number="60")
        with_account = self.join(auction, user=User.objects.create_user(username="reached", password="x"), number="61")
        invoice = Invoice.objects.get_or_create(auctiontos_user=with_account)[0]
        invoice.opened = True
        invoice.save()
        share = lifecycle.unreached_share(auction)
        self.assertEqual(share["joined"], 2)
        self.assertEqual(share["silent"], 1)
        self.assertEqual(share["percent"], 50.0)

    def test_an_empty_auction_has_no_share_rather_than_zero(self):
        self.assertIsNone(lifecycle.unreached_share(self.auctions[3]))


class MilestoneReachTests(StandardTestCase):
    """Reach, not conversion -- and the shapes a funnel would report as a drop-out."""

    def setUp(self):
        super().setUp()
        route_name.cache_clear()

    def test_a_seller_who_never_opens_a_lot_page_still_reaches_added_lots(self):
        """The seller's whole path, and the reason this is not a funnel.

        In a strict funnel "added lots" sits under "viewed a first lot" and this person reads as
        somebody who dropped out at the lot page.  They did not: they never needed one.
        """
        reach = lifecycle.milestone_reach([self.online_auction])[self.online_auction.pk]
        self.assertGreaterEqual(reach["added_lots"], 1)

    def test_the_rules_page_is_the_auctions_own_page_and_not_its_lots(self):
        """Since 7a.2 a lot view carries the auction FK too, so the FK alone cannot answer this."""
        lot = self.online_auction.lots_qs.first()
        PageView.objects.create(
            url=f"/lots/{lot.pk}/whatever/",
            title="a lot",
            session_id="rules-a",
            lot_number=lot,
            auction=self.online_auction,
        )
        reach = lifecycle.milestone_reach([self.online_auction])[self.online_auction.pk]
        without_rules = reach["read_rules"]
        PageView.objects.create(
            url=f"/auctions/{self.online_auction.slug}/",
            title="the rules",
            session_id="rules-b",
            auction=self.online_auction,
        )
        reach = lifecycle.milestone_reach([self.online_auction])[self.online_auction.pk]
        self.assertEqual(reach["read_rules"], without_rules + 1)

    def test_every_milestone_key_is_reported(self):
        """A milestone declared in ``MILESTONES`` and never computed would render as a blank row."""
        reach = lifecycle.milestone_reach([self.online_auction])[self.online_auction.pk]
        self.assertEqual(sorted(reach), sorted(milestone.key for milestone in lifecycle.MILESTONES))

    def test_every_prerequisite_names_a_milestone_that_exists(self):
        keys = {milestone.key for milestone in lifecycle.MILESTONES}
        for milestone in lifecycle.MILESTONES:
            if milestone.after:
                self.assertIn(milestone.after, keys, f"{milestone.key} is after a milestone that does not exist")

    def test_no_auctions_is_an_empty_report_rather_than_an_unbounded_scan(self):
        self.assertEqual(lifecycle.milestone_reach([]), {})


class LifecyclePageTests(StandardTestCase):
    """Both pages are admin-only and both render on a site with data in it.

    ``StandardTestCase.admin_user`` is an *auction* admin, which is a different thing entirely:
    ``AdminOnlyViewMixin`` gates on ``is_superuser`` and says so in its own docstring.
    """

    def _as_site_admin(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.force_login(self.admin_user)

    def _with_a_club(self):
        """Attach the fixture's auctions to a club, because every panel groups by one.

        Without this the page renders its "no club has an auction yet" branch, which is a real
        state of the site and the reason ``club_coverage`` is on the page -- but it is not the
        state that proves the panels work.
        """
        club = Club.objects.create(name="Lifecycle page club")
        Auction.objects.filter(pk__in=[self.online_auction.pk, self.in_person_auction.pk]).update(club=club)
        return club

    def test_the_lifecycle_page_needs_an_admin(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin_lifecycle"))
        self.assertNotEqual(response.status_code, 200)

    def test_the_lifecycle_page_renders(self):
        self._with_a_club()
        self._as_site_admin()
        response = self.client.get(reverse("admin_lifecycle"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Milestones reached")
        self.assertContains(response, "Cohorts, one auction to the next")
        self.assertContains(response, "The median member")

    def test_the_lifecycle_page_says_what_share_of_the_site_it_can_see(self):
        """Not a footnote: every club number on it is computed from the linked fifth."""
        self._as_site_admin()
        response = self.client.get(reverse("admin_lifecycle"))
        self.assertContains(response, "auctions")
        self.assertIn("coverage", response.context)
        self.assertLessEqual(response.context["coverage"]["linked"], response.context["coverage"]["total"])

    def test_the_session_replay_page_renders_with_no_subject(self):
        self._as_site_admin()
        response = self.client.get(reverse("admin_session_replay"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Busiest sessions")

    def test_the_session_replay_page_reads_one_session(self):
        PageView.objects.create(url="/lots/", title="t", session_id="replay-me")
        self._as_site_admin()
        response = self.client.get(reverse("admin_session_replay"), {"session": "replay-me"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["timeline"]), 1)

    def test_the_session_replay_page_needs_an_admin(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin_session_replay"))
        self.assertNotEqual(response.status_code, 200)
