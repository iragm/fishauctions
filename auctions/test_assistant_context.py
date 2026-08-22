"""What the assistant does when nobody is looking at a page.

The command palette runs inside a browser, so "which auction?" was always answered by the URL the
person was standing on. An agent connected over MCP has no URL, and every one of these tests is a
thing that went wrong -- or would have -- the first time a club used it that way.

The MCP transport, its authentication and the OAuth server are in ``test_mcp``. This file is about
the resolvers underneath, which both surfaces share.
"""

import datetime
from unittest.mock import patch

from django.test import RequestFactory
from django.utils import timezone

from auctions import palette_actions
from auctions.models import Auction, AuctionTOS, Club, ClubEvent, ClubMember, Lot
from auctions.test_palette_assist import PaletteAssistTestCase


class NoPageTestCase(PaletteAssistTestCase):
    """Every action run the way an agent runs it: no page context at all."""

    def _run(self, action, params=None, user=None):
        request = RequestFactory().post("/")
        request.user = user or self.user
        # What ``mcp.tools.call_tool`` sets, explicitly. An agent is not looking at anything.
        request.palette_page = {}
        return palette_actions.run_action(request, action, params or {})

    def _make_auction(self, title, *, days_ahead=1, creator=None, promoted=False):
        return Auction.objects.create(
            title=title,
            created_by=creator or self.user,
            date_start=timezone.now() + datetime.timedelta(days=days_ahead),
            date_end=timezone.now() + datetime.timedelta(days=days_ahead + 2),
            is_online=True,
            promote_this_auction=promoted,
        )

    def _join(self, auction, user=None):
        from auctions.models import PickupLocation

        location = auction.location_qs.first() or PickupLocation.objects.create(
            name=f"{auction.slug} pickup", auction=auction, pickup_time=timezone.now()
        )
        return AuctionTOS.objects.create(
            auction=auction,
            user=user or self.user,
            email=(user or self.user).email,
            pickup_location=location,
        )


class WhichAuctionTests(NoPageTestCase):
    """The central hole: with no page, ``last_auction_used`` was the only answer."""

    def test_one_running_auction_needs_no_hint(self):
        auction, problem = palette_actions.resolve_auction(self.user, "", {})
        self.assertIsNone(problem)
        self.assertIsNotNone(auction)

    def test_a_stale_pointer_never_beats_a_running_auction(self):
        """The failure this whole change exists for: spring setup morning, autumn's auction acted on."""
        old = self._make_auction("Last Autumn", days_ahead=-400)
        self._join(old)
        self.user.userdata.last_auction_used = old
        self.user.userdata.save()
        auction, problem = palette_actions.resolve_auction(self.user, "", {})
        # Either it picks a running one or it asks which; what it must never do is quietly pick the
        # one that finished a year ago because the browser pointer still names it.
        self.assertNotEqual(getattr(auction, "pk", None), old.pk)
        if problem:
            self.assertNotIn(old.title, [option["label"] for option in problem["options"]])

    def test_several_running_auctions_ask_rather_than_guess(self):
        second = self._make_auction("Spring Swap")
        self._join(second)
        self.user.userdata.last_auction_used = None
        self.user.userdata.save()
        _auction, problem = palette_actions.resolve_auction(self.user, "", {})
        self.assertIn("more_info_needed", problem)
        titles = [option["label"] for option in problem["options"]]
        self.assertTrue(any("Spring Swap" in title for title in titles), titles)

    def test_the_question_reaches_an_action_as_a_question(self):
        second = self._make_auction("Spring Swap")
        self._join(second)
        self.user.userdata.last_auction_used = None
        self.user.userdata.save()
        result = self._run("list_lots", {"status": "all"})
        self.assertIn("more_info_needed", result)
        self.assertNotIn("lots", result)

    def test_a_running_auction_they_last_used_wins_the_tie(self):
        second = self._make_auction("Spring Swap")
        self._join(second)
        self.user.userdata.last_auction_used = second
        self.user.userdata.save()
        auction, problem = palette_actions.resolve_auction(self.user, "", {})
        self.assertIsNone(problem)
        self.assertEqual(auction.pk, second.pk)

    def test_a_club_officer_is_in_their_clubs_auctions(self):
        """They never joined it as a bidder, which used to mean they had no relationship with it."""
        club = Club.objects.create(name="Officer Club", abbreviation="OC")
        auction = self._make_auction("Club Run Auction", creator=self.userB)
        auction.club = club
        auction.save()
        ClubMember.objects.create(club=club, user=self.user, name="An officer", permission_admin=True)
        joined = palette_actions.command_palette._joined_auctions(self.user)
        self.assertIn(auction.pk, list(joined.values_list("pk", flat=True)))

    def test_a_promoted_auction_can_be_reached_by_name(self):
        """Asking about one before joining is a fair question; writing to it still is not."""
        stranger = self._make_auction("Public Swap Meet", creator=self.userB, promoted=True)
        auction, problem = palette_actions.resolve_auction(self.user, "Public Swap Meet", {})
        self.assertIsNone(problem)
        self.assertEqual(auction.pk, stranger.pk)

    def test_a_private_auction_of_somebody_elses_stays_invisible(self):
        self._make_auction("Their Private Auction", creator=self.userB, promoted=False)
        _auction, problem = palette_actions.resolve_auction(self.user, "Their Private Auction", {})
        self.assertIsInstance(problem, str)
        self.assertIn("couldn't find", problem)


class RememberAuctionTests(NoPageTestCase):
    """Engaging with an auction is engaging with it, whichever surface it came through."""

    def test_acting_on_an_auction_records_it(self):
        other = self._make_auction("Named Auction")
        self._join(other)
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self._run("list_lots", {"status": "all", "auction": other.slug})
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.last_auction_used_id, other.pk)

    def test_it_does_not_write_when_nothing_changed(self):
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        with patch.object(type(self.user.userdata), "save") as saved:
            palette_actions.remember_auction(self._request_for(self.user), self.in_person_auction)
        saved.assert_not_called()

    def _request_for(self, user):
        request = RequestFactory().post("/")
        request.user = user
        request.palette_page = {}
        return request


class WhichClubTests(NoPageTestCase):
    """Same hole, and here the wrong answer writes to somebody else's club."""

    def setUp(self):
        super().setUp()
        self.club_a = Club.objects.create(name="Alpha Aquarists", abbreviation="AA")
        self.club_b = Club.objects.create(name="Beta Bettas", abbreviation="BB")
        for club in (self.club_a, self.club_b):
            ClubMember.objects.create(
                club=club, user=self.user, name="Member", permission_admin=True, permission_add_edit=True
            )
        self.user.userdata.last_club_used = None
        self.user.userdata.save()

    def test_two_clubs_and_no_hint_is_a_question(self):
        result = self._run("club_numbers", {})
        self.assertIn("more_info_needed", result)
        labels = [option["label"] for option in result["options"]]
        self.assertIn("Alpha Aquarists", labels)
        self.assertIn("Beta Bettas", labels)

    def test_a_named_club_is_answered(self):
        result = self._run("club_numbers", {"club": "Beta Bettas"})
        self.assertEqual(result["club_numbers"]["club"], "Beta Bettas")

    def test_the_club_it_chose_is_echoed_back(self):
        """An agent that omitted the club has no page to check the answer against."""
        self.user.userdata.last_club_used = self.club_a
        self.user.userdata.save()
        result = self._run("add_club_member", {"name": "New Person"})
        self.assertEqual(result["club"], "Alpha Aquarists")


class PagingTests(NoPageTestCase):
    """Fifteen of forty-three, read out as the whole answer."""

    def test_a_truncated_list_says_so_and_says_how_to_go_on(self):
        for index in range(20):
            AuctionTOS.objects.create(
                auction=self.in_person_auction,
                pickup_location=self.in_person_location,
                name=f"Person {index}",
                email=f"paging{index}@example.com",
                bidder_number=f"9{index:02d}",
            )
        result = self._run("list_people", {"status": "all", "auction": self.in_person_auction.slug})
        self.assertEqual(result["showing"], palette_actions.LIST_LIMIT)
        self.assertIn("offset=", result["summary"])
        self.assertGreater(result["count"], result["showing"])

    def test_offset_reaches_the_rest(self):
        for index in range(20):
            AuctionTOS.objects.create(
                auction=self.in_person_auction,
                pickup_location=self.in_person_location,
                name=f"Person {index}",
                email=f"offset{index}@example.com",
                bidder_number=f"8{index:02d}",
            )
        first = self._run("list_people", {"status": "all", "auction": self.in_person_auction.slug})
        second = self._run(
            "list_people",
            {"status": "all", "auction": self.in_person_auction.slug, "offset": palette_actions.LIST_LIMIT},
        )
        self.assertEqual(second["offset"], palette_actions.LIST_LIMIT)
        names = {row["name"] for row in first["people"]} & {row["name"] for row in second["people"]}
        self.assertFalse(names, "the second page repeated the first")

    def test_the_limit_is_capped(self):
        result = self._run("list_people", {"status": "all", "auction": self.in_person_auction.slug, "limit": 10_000})
        self.assertLessEqual(result["showing"], palette_actions.MAX_LIST_LIMIT)


class UndoCheckInTests(NoPageTestCase):
    """Two Bobs and one microphone."""

    def setUp(self):
        super().setUp()
        # ``use_check_in_mode`` is a property over these two.
        self.in_person_auction.manage_users_through_club = "checkin"
        if not self.in_person_auction.club:
            self.in_person_auction.club = Club.objects.create(name="Check In Club", abbreviation="CIC")
        self.in_person_auction.save()
        self.tos = AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Bob Wrong",
            email="bobwrong@example.com",
            bidder_number="321",
        )

    def test_a_check_in_describes_its_own_reversal(self):
        result = self._run("check_in", {"person": "321", "auction": self.in_person_auction.slug})
        self.assertEqual(result["undo"]["action"], "undo_check_in")

    def test_the_reversal_works(self):
        self._run("check_in", {"person": "321", "auction": self.in_person_auction.slug})
        self.tos.refresh_from_db()
        self.assertIsNotNone(self.tos.checked_in)
        self._run("undo_check_in", {"person": "321", "auction": self.in_person_auction.slug})
        self.tos.refresh_from_db()
        self.assertIsNone(self.tos.checked_in)

    def test_it_leaves_bidding_alone(self):
        """Half a dozen things turn bidding on; an undo must not quietly turn it off."""
        self._run("check_in", {"person": "321", "auction": self.in_person_auction.slug})
        self._run("undo_check_in", {"person": "321", "auction": self.in_person_auction.slug})
        self.tos.refresh_from_db()
        self.assertTrue(self.tos.bidding_allowed)

    def test_a_participant_cannot_do_it(self):
        result = self._run(
            "undo_check_in",
            {"person": "321", "auction": self.in_person_auction.slug},
            user=self.member,
        )
        self.assertIn("permission", result["error"])


class SettledInvoiceTests(NoPageTestCase):
    """Undoing a sale after the money changed hands."""

    def setUp(self):
        super().setUp()
        from auctions.models import Invoice

        self.lot = Lot.objects.filter(auction=self.in_person_auction, is_deleted=False).first()
        self.winner = self.in_person_buyer
        self.lot.auctiontos_winner = self.winner
        self.lot.winning_price = 12
        self.lot.active = False
        self.lot.date_end = timezone.now()
        self.lot.save()
        self.invoice, _created = Invoice.objects.get_or_create(
            auctiontos_user=self.winner, defaults={"auction": self.in_person_auction}
        )

    def test_it_refuses_and_says_how_to_insist(self):
        lot = self.lot
        self.invoice.status = "PAID"
        self.invoice.save()
        result = self._run("undo_sale", {"lot": lot.lot_number_display, "auction": self.in_person_auction.slug})
        self.assertIn("settled up", result["error"])
        self.assertIn("ignore_errors", result["error"])
        lot.refresh_from_db()
        self.assertIsNotNone(lot.auctiontos_winner)

    def test_ignore_errors_gets_past_it(self):
        """The web has a button for this; over MCP the button has to be a sentence."""
        self.invoice.status = "PAID"
        self.invoice.save()
        result = self._run(
            "undo_sale",
            {
                "lot": self.lot.lot_number_display,
                "auction": self.in_person_auction.slug,
                "ignore_errors": True,
            },
        )
        self.assertNotIn("error", result)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.auctiontos_winner)

    def test_an_open_invoice_is_no_obstacle(self):
        result = self._run("undo_sale", {"lot": self.lot.lot_number_display, "auction": self.in_person_auction.slug})
        self.assertNotIn("error", result)


class ClubCalendarTests(NoPageTestCase):
    """A club's other three jobs, which lived on the web and nowhere else."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Calendar Club", abbreviation="CC")
        ClubMember.objects.create(
            club=self.club, user=self.user, name="An admin", permission_admin=True, permission_edit_club=True
        )
        self.user.userdata.last_club_used = self.club
        self.user.userdata.save()

    def test_adding_an_event_puts_it_on_the_calendar(self):
        when = (timezone.now() + datetime.timedelta(days=10)).replace(microsecond=0)
        with patch("auctions.views._push_event_to_integrations"):
            result = self._run(
                "add_club_event",
                {
                    "club": self.club.slug,
                    "title": "October meeting",
                    "starts": when.isoformat(),
                    "location": "The usual place",
                },
            )
        self.assertIn("October meeting", result["summary"])
        self.assertTrue(ClubEvent.objects.filter(club=self.club, title="October meeting").exists())

    def test_events_are_reported_in_the_users_own_timezone(self):
        """An 8pm Friday meeting used to read back as the small hours of Saturday."""
        self.user.userdata.timezone = "America/New_York"
        self.user.userdata.save()
        evening = timezone.datetime(2027, 5, 21, 23, 30, tzinfo=datetime.timezone.utc)
        ClubEvent.objects.create(club=self.club, title="Evening meeting", date_start=evening)
        result = self._run("list_club_events", {"club": self.club.slug})
        row = next(row for row in result["events"] if row["title"] == "Evening meeting")
        self.assertIn("Friday", row["starts"])
        self.assertIn("7:30 PM", row["starts"])

    def test_a_generated_event_keeps_its_dates(self):
        event = ClubEvent.objects.create(
            club=self.club,
            title="Auto event",
            date_start=timezone.now() + datetime.timedelta(days=5),
            source=ClubEvent.SOURCE_AUCTION,
        )
        result = self._run(
            "update_club_event",
            {"club": self.club.slug, "event": "Auto event", "starts": "2027-01-01T10:00"},
        )
        self.assertIn("belongs to the auction", result["error"])
        event.refresh_from_db()
        self.assertNotEqual(event.date_start.year, 2027)

    def test_a_generated_events_wording_is_still_the_clubs(self):
        ClubEvent.objects.create(
            club=self.club,
            title="Auto event",
            date_start=timezone.now() + datetime.timedelta(days=5),
            source=ClubEvent.SOURCE_AUCTION,
        )
        with patch("auctions.views._push_event_to_integrations"):
            result = self._run(
                "update_club_event",
                {"club": self.club.slug, "event": "Auto event", "new_title": "Monthly meeting"},
            )
        self.assertIn("Updated", result["summary"])

    def test_changing_a_setting_goes_through_the_settings_form(self):
        result = self._run(
            "update_club_setting", {"club": self.club.slug, "setting": "breeder award program", "value": "on"}
        )
        self.club.refresh_from_db()
        self.assertTrue(self.club.enable_breeder_award_program)
        self.assertIn("on", result["summary"])

    def test_an_unknown_setting_lists_what_it_can_change(self):
        result = self._run("update_club_setting", {"club": self.club.slug, "setting": "nonsense", "value": "on"})
        self.assertIn("more_info_needed", result)
        self.assertIn("description", result["more_info_needed"])


class ClubAnnouncementTests(NoPageTestCase):
    """One press reaches every channel at once, so it goes through the grace window."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Loud Club", abbreviation="LC")
        ClubMember.objects.create(
            club=self.club,
            user=self.user,
            name="An admin",
            permission_admin=True,
            permission_send_announcements=True,
        )
        self.user.userdata.last_club_used = self.club
        self.user.userdata.save()

    def test_it_refuses_to_send_to_nowhere(self):
        result = self._run("send_club_announcement", {"club": self.club.slug, "text": "Hello everyone"})
        self.assertIn("more_info_needed", result)

    def test_it_schedules_rather_than_sending(self):
        from auctions.models import ClubAnnouncement

        result = self._run(
            "send_club_announcement",
            {"club": self.club.slug, "text": "The meeting moved to the 21st", "website": True},
        )
        self.assertIn("seconds", result["summary"])
        announcement = ClubAnnouncement.objects.get(club=self.club)
        self.assertIsNone(announcement.sent_at, "nothing is delivered inside the request")
        self.assertIsNotNone(announcement.scheduled_for)

    def test_retracting_takes_back_the_most_recent_one(self):
        from auctions.models import ClubAnnouncement

        self._run(
            "send_club_announcement",
            {"club": self.club.slug, "text": "Ignore this one", "website": True},
        )
        result = self._run("retract_announcement", {"club": self.club.slug})
        self.assertIn("before it went anywhere", result["summary"])
        self.assertTrue(ClubAnnouncement.objects.get(club=self.club).is_deleted)

    def test_somebody_without_the_permission_is_refused(self):
        result = self._run(
            "send_club_announcement",
            {"club": self.club.slug, "text": "Not allowed", "website": True},
            user=self.member,
        )
        self.assertIn("error", result)


class LotSpeciesTests(NoPageTestCase):
    """A lot an agent adds still has to be a lot with a species on it."""

    def setUp(self):
        super().setUp()
        from auctions.models import Species

        self.species = Species.objects.create(
            genus="Zorblattus",
            species="testicus",
            scientific_name="Zorblattus testicus",
            approved=True,
        )
        self.in_person_auction.use_scientific_name = True
        self.in_person_auction.save()

    def _add(self, name):
        return self._run("add_lot", {"name": name, "auction": self.in_person_auction.slug})

    def test_an_obvious_name_gets_its_species(self):
        """The page's JavaScript fills this in, and an agent runs no JavaScript."""
        result = self._add("Zorblattus testicus")
        self.assertIn("lot_id", result)
        lot = Lot.objects.get(pk=result["lot_id"])
        self.assertEqual(lot.species_id, self.species.pk)

    def test_an_ambiguous_name_gets_nothing_rather_than_a_guess(self):
        from auctions.models import Species

        Species.objects.create(
            genus="Zorblattus", species="ambiguus", scientific_name="Zorblattus ambiguus", approved=True
        )
        result = self._add("Zorblattus")
        lot = Lot.objects.get(pk=result["lot_id"])
        self.assertIsNone(lot.species_id, "a wrong species ends up on a printed label")

    def test_it_never_spends_the_sites_model_budget(self):
        with patch("auctions.species_matching.llm_match") as called:
            self._add("something nobody has ever heard of")
        called.assert_not_called()
