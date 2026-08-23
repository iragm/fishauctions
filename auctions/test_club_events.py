"""Tests for club events, Google Calendar sync, and the Discord events built on top of them."""

import datetime
import zoneinfo
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions import club_events, discord_events, recurrence
from auctions import google_calendar as gcal
from auctions.models import Auction, Club, ClubEvent, ClubMember, PickupLocation


class ClubEventModelTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Event Model Club")
        self.start = timezone.now() + datetime.timedelta(days=3)

    def test_effective_end_falls_back_to_two_hours(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        self.assertEqual(event.effective_end, self.start + datetime.timedelta(hours=2))

    def test_effective_end_ignores_an_end_before_the_start(self):
        """A backwards end time would make Google and Discord reject the event outright."""
        event = ClubEvent.objects.create(
            club=self.club,
            title="Meeting",
            date_start=self.start,
            date_end=self.start - datetime.timedelta(hours=1),
        )
        self.assertEqual(event.effective_end, self.start + datetime.timedelta(hours=2))

    def test_auction_events_are_not_editable_by_hand(self):
        auction = Auction.objects.create(
            title="Auction", date_start=self.start, club=self.club, promote_this_auction=True
        )
        event = ClubEvent.objects.filter(auction=auction).first()
        self.assertIsNotNone(event)
        self.assertFalse(event.is_editable)
        self.assertTrue(ClubEvent.objects.create(club=self.club, title="M", date_start=self.start).is_editable)

    def test_public_calendar_urls_are_empty_until_connected_and_public(self):
        self.assertEqual(self.club.google_calendar_public_url, "")
        self.club.google_calendar_refresh_token = "token"
        self.club.google_calendar_id = "abc@group.calendar.google.com"
        self.club.google_calendar_is_public = False
        self.assertEqual(self.club.google_calendar_public_url, "")
        self.club.google_calendar_is_public = True
        self.assertIn("abc%40group.calendar.google.com", self.club.google_calendar_public_url)
        self.assertIn("basic.ics", self.club.google_calendar_ical_url)

    def test_the_subscribe_link_falls_back_to_our_own_feed(self):
        """Every club has one of these, connected to Google or not — webcal:// so a click
        subscribes instead of downloading a snapshot that never updates again.

        Asserted as the whole address rather than as a prefix: ``startswith("https://example.com")``
        is not a test that a URL points at that host, because ``https://example.com.evil.test/``
        passes it too. That is a real bug in a sanitiser and a false alarm in a test, and the way to
        settle both is to say which URL we expected.
        """
        path = reverse("club_events_ical", kwargs={"slug": self.club.slug})
        self.assertEqual(self.club.calendar_subscribe_url("example.com"), f"webcal://example.com{path}")
        self.assertEqual(self.club.calendar_feed_url("example.com"), f"https://example.com{path}")

    def test_a_shared_google_calendar_wins_both_links(self):
        self.club.google_calendar_refresh_token = "token"
        self.club.google_calendar_id = "abc@group.calendar.google.com"
        self.club.google_calendar_is_public = True
        self.assertIn("calendar.google.com", self.club.calendar_subscribe_url("example.com"))
        self.assertIn("basic.ics", self.club.calendar_feed_url("example.com"))

    def test_a_private_google_calendar_is_not_offered_to_anyone(self):
        """The whole reason the flag exists: these links 404 for members until it's shared."""
        self.club.google_calendar_refresh_token = "token"
        self.club.google_calendar_id = "abc@group.calendar.google.com"
        self.club.google_calendar_is_public = False
        self.assertTrue(self.club.calendar_subscribe_url("example.com").startswith("webcal://"))
        self.assertNotIn("google.com", self.club.calendar_feed_url("example.com"))


class AuctionMirroringTests(TestCase):
    """Promoted auctions become calendar events automatically, and stay in step."""

    def setUp(self):
        self.club = Club.objects.create(name="Mirror Club")
        self.start = timezone.now() + datetime.timedelta(days=5)

    def _auction(self, **kwargs):
        defaults = {
            "title": "Spring Auction",
            "date_start": self.start,
            "date_end": self.start + datetime.timedelta(days=2),
            "club": self.club,
            "is_online": True,
            # Everything in this file is about what a *promoted* auction puts on a club's calendar,
            # so the helper says so. The model default is False; tests about the unpromoted case
            # pass promote_this_auction=False for themselves.
            "promote_this_auction": True,
        }
        defaults.update(kwargs)
        return Auction.objects.create(**defaults)

    def test_saving_a_promoted_auction_creates_one_event(self):
        auction = self._auction()
        events = ClubEvent.objects.filter(auction=auction)
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertEqual(event.source, ClubEvent.SOURCE_AUCTION)
        self.assertEqual(event.title, "Spring Auction")
        self.assertEqual(event.club, self.club)

    def test_editing_an_auction_updates_its_event(self):
        auction = self._auction()
        auction.title = "Renamed Auction"
        auction.date_start = self.start + datetime.timedelta(days=1)
        auction.save()
        event = ClubEvent.objects.get(auction=auction)
        self.assertEqual(event.title, "Renamed Auction")
        self.assertEqual(event.date_start, self.start + datetime.timedelta(days=1))
        self.assertTrue(event.needs_google_sync)

    def test_a_hand_typed_title_survives_the_auction_being_renamed(self):
        """A club's monthly meeting often is the auction, and what members read on their phone is
        the club's to write. Before this, every save of the auction wiped it."""
        auction = self._auction()
        event = ClubEvent.objects.get(auction=auction)
        event.title = "Spring Auction — April meeting"
        event.description = "Doors at 6:30. Bring a dish."
        event.title_is_custom = True
        event.description_is_custom = True
        event.save()

        auction.title = "Renamed Auction"
        auction.date_start = self.start + datetime.timedelta(days=1)
        auction.save()

        event.refresh_from_db()
        self.assertEqual(event.title, "Spring Auction — April meeting")
        self.assertEqual(event.description, "Doors at 6:30. Bring a dish.")
        # Everything the auction still owns moved with it.
        self.assertEqual(event.date_start, self.start + datetime.timedelta(days=1))

    def test_one_custom_field_does_not_freeze_the_other(self):
        auction = self._auction()
        event = ClubEvent.objects.get(auction=auction)
        event.title = "April meeting"
        event.title_is_custom = True
        event.save()

        auction.is_online = False
        auction.save()

        event.refresh_from_db()
        self.assertEqual(event.title, "April meeting")
        self.assertEqual(event.description, "In-person auction.")

    def test_clearing_the_flag_lets_the_auction_take_it_back(self):
        auction = self._auction()
        event = ClubEvent.objects.get(auction=auction)
        event.title = "April meeting"
        event.title_is_custom = True
        event.save()

        event.title_is_custom = False
        event.save()
        auction.save()

        event.refresh_from_db()
        self.assertEqual(event.title, "Spring Auction")

    def test_the_periodic_backstop_respects_it_too(self):
        """The post_save signal is not the only writer — sync_auction_events runs over every
        auction every 15 minutes, and would have undone the edit within the hour."""
        auction = self._auction()
        event = ClubEvent.objects.get(auction=auction)
        event.title = "April meeting"
        event.title_is_custom = True
        event.save()

        club_events.sync_auction_events(self.club)

        event.refresh_from_db()
        self.assertEqual(event.title, "April meeting")

    def test_a_pickup_event_can_be_given_wording_of_its_own(self):
        auction = self._auction()
        PickupLocation.objects.create(auction=auction, name="Clubhouse", address="1 Fish Lane", pickup_time=self.start)
        event = ClubEvent.objects.get(source=ClubEvent.SOURCE_PICKUP, pickup_location__auction=auction)
        event.description = "Pick up your lots — swap table open too."
        event.description_is_custom = True
        event.save()

        club_events.sync_pickup_events(auction)

        event.refresh_from_db()
        self.assertEqual(event.description, "Pick up your lots — swap table open too.")

    def test_generated_wording_says_what_the_site_would_have_written(self):
        auction = self._auction()
        event = ClubEvent.objects.get(auction=auction)
        event.title = "April meeting"
        event.title_is_custom = True
        event.save()
        title, description = club_events.generated_wording(event)
        self.assertEqual(title, "Spring Auction")
        self.assertEqual(description, "Online auction with in-person pickup.")

    def test_a_typed_in_event_has_no_generated_wording(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        self.assertEqual(club_events.generated_wording(event), ("", ""))

    def test_unpromoting_an_auction_retires_its_event(self):
        auction = self._auction()
        auction.promote_this_auction = False
        auction.save()
        self.assertTrue(ClubEvent.objects.get(auction=auction).is_deleted)

    def test_repromoting_an_auction_brings_the_event_back(self):
        auction = self._auction()
        auction.promote_this_auction = False
        auction.save()
        auction.promote_this_auction = True
        auction.save()
        event = ClubEvent.objects.get(auction=auction)
        self.assertFalse(event.is_deleted)
        # Still exactly one — the unique index on `auction` is doing its job.
        self.assertEqual(ClubEvent.objects.filter(auction=auction).count(), 1)

    def test_deleting_an_auction_retires_its_event(self):
        auction = self._auction()
        auction.is_deleted = True
        auction.save()
        self.assertTrue(ClubEvent.objects.get(auction=auction).is_deleted)

    def test_an_auction_with_no_club_is_never_mirrored(self):
        auction = self._auction(club=None)
        self.assertEqual(ClubEvent.objects.filter(auction=auction).count(), 0)

    def test_the_feature_can_be_turned_off_per_club(self):
        self.club.add_auctions_to_calendar = False
        self.club.save()
        auction = self._auction()
        self.assertEqual(ClubEvent.objects.filter(auction=auction, is_deleted=False).count(), 0)

    def test_turning_the_feature_off_retires_existing_auction_events(self):
        auction = self._auction()
        self.club.add_auctions_to_calendar = False
        self.club.save()
        club_events.sync_auction_events(self.club)
        self.assertTrue(ClubEvent.objects.get(auction=auction).is_deleted)

    def test_online_auctions_span_start_to_end(self):
        auction = self._auction()
        start, end = club_events.auction_event_window(auction)
        self.assertEqual(start, auction.date_start)
        self.assertEqual(end, auction.date_end)

    def test_in_person_auctions_get_a_fixed_block(self):
        auction = self._auction(is_online=False, date_end=None)
        start, end = club_events.auction_event_window(auction)
        self.assertEqual(end - start, club_events.DEFAULT_AUCTION_LENGTH)

    def test_a_single_address_becomes_an_in_person_events_location(self):
        """Only in-person auctions carry a location — see PickupEventTests for the online case."""
        auction = self._auction(is_online=False, date_end=None)
        PickupLocation.objects.create(name="Clubhouse", auction=auction, address="1 Fish Lane", pickup_time=self.start)
        auction.save()
        self.assertEqual(ClubEvent.objects.get(auction=auction).location, "1 Fish Lane")

    def test_an_address_less_default_location_does_not_blank_the_real_one(self):
        """Switching an auction to in-person auto-creates a location with no address; counting
        rows rather than addresses would wrongly treat that as 'several locations'."""
        auction = self._auction()
        auction.is_online = False
        auction.save()
        PickupLocation.objects.create(name="Clubhouse", auction=auction, address="1 Fish Lane", pickup_time=self.start)
        auction.save()
        self.assertGreater(auction.physical_location_qs.count(), 1)
        self.assertEqual(ClubEvent.objects.get(auction=auction).location, "1 Fish Lane")

    def test_multiple_addresses_leave_the_location_blank(self):
        """With several real addresses no single one is the right one to advertise."""
        auction = self._auction(is_online=False, date_end=None)
        for name in ("North", "South"):
            PickupLocation.objects.create(name=name, auction=auction, address=f"{name} St", pickup_time=self.start)
        auction.save()
        self.assertEqual(ClubEvent.objects.get(auction=auction).location, "")

    def test_sync_auction_events_is_idempotent(self):
        self._auction()
        club_events.sync_auction_events(self.club)
        club_events.sync_auction_events(self.club)
        self.assertEqual(ClubEvent.objects.filter(club=self.club, is_deleted=False).count(), 1)


class PickupEventTests(TestCase):
    """Online auctions get a short event for each pickup time, so members know when to collect."""

    def setUp(self):
        self.club = Club.objects.create(name="Pickup Club")
        self.start = timezone.now() + datetime.timedelta(days=5)
        self.auction = Auction.objects.create(
            title="Spring Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(days=2),
            club=self.club,
            is_online=True,
            # Pickup events only exist for an auction the club is promoting -- see
            # ``club_events.sync_pickup_events``. Said out loud because the model default is False.
            promote_this_auction=True,
        )

    def _location(self, name, **kwargs):
        defaults = {"auction": self.auction, "name": name, "address": f"{name} St"}
        defaults.update(kwargs)
        return PickupLocation.objects.create(**defaults)

    def _pickups(self):
        return ClubEvent.objects.filter(club=self.club, source=ClubEvent.SOURCE_PICKUP, is_deleted=False).order_by(
            "date_start"
        )

    def test_one_pickup_time_makes_one_fifteen_minute_event(self):
        when = self.start + datetime.timedelta(days=3)
        self._location("Clubhouse", pickup_time=when)
        events = self._pickups()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertEqual(event.date_start, when)
        self.assertEqual(event.date_end - event.date_start, datetime.timedelta(minutes=15))
        self.assertEqual(event.location, "Clubhouse St")
        self.assertIn("Spring Auction pickup", event.title)

    def test_two_pickup_times_on_one_location_make_two_events(self):
        first = self.start + datetime.timedelta(days=3)
        second = self.start + datetime.timedelta(days=4)
        self._location("Clubhouse", pickup_time=first, second_pickup_time=second)
        events = self._pickups()
        self.assertEqual(events.count(), 2)
        self.assertEqual([e.date_start for e in events], [first, second])
        for event in events:
            self.assertEqual(event.date_end - event.date_start, datetime.timedelta(minutes=15))

    def test_several_locations_make_no_pickup_events(self):
        """A member goes to one location; the rest would just be noise in their calendar."""
        when = self.start + datetime.timedelta(days=3)
        self._location("North", pickup_time=when)
        self._location("South", pickup_time=when + datetime.timedelta(hours=2))
        self.assertEqual(self._pickups().count(), 0)

    def test_a_multi_location_auction_still_gets_its_own_event(self):
        when = self.start + datetime.timedelta(days=3)
        self._location("North", pickup_time=when)
        self._location("South", pickup_time=when + datetime.timedelta(hours=2))
        self.assertTrue(ClubEvent.objects.filter(auction=self.auction, is_deleted=False).exists())

    def test_adding_a_second_location_retires_the_first_ones_pickup_events(self):
        when = self.start + datetime.timedelta(days=3)
        self._location("North", pickup_time=when, second_pickup_time=when + datetime.timedelta(days=1))
        self.assertEqual(self._pickups().count(), 2)
        self._location("South", pickup_time=when + datetime.timedelta(hours=2))
        self.assertEqual(self._pickups().count(), 0)

    def test_dropping_back_to_one_location_brings_its_pickup_events_back(self):
        when = self.start + datetime.timedelta(days=3)
        self._location("North", pickup_time=when)
        south = self._location("South", pickup_time=when + datetime.timedelta(hours=2))
        self.assertEqual(self._pickups().count(), 0)
        south.delete()
        club_events.sync_pickup_events(self.auction)
        self.assertEqual(self._pickups().count(), 1)
        self.assertEqual(self._pickups().first().location, "North St")

    def test_a_second_location_with_no_pickup_time_does_not_suppress_the_real_one(self):
        """Half-filled locations are common; only ones that would make an event should count."""
        when = self.start + datetime.timedelta(days=3)
        self._location("Clubhouse", pickup_time=when)
        self._location("Undecided")
        self.assertEqual(self._pickups().count(), 1)

    def test_a_mail_location_alongside_a_real_one_does_not_suppress_it(self):
        when = self.start + datetime.timedelta(days=3)
        self._location("Clubhouse", pickup_time=when)
        self._location("By mail", pickup_by_mail=True, pickup_time=when)
        self.assertEqual(self._pickups().count(), 1)

    def test_in_person_auctions_get_no_pickup_events(self):
        """For an in-person auction the pickup is the auction, which already has its own event."""
        self.auction.is_online = False
        self.auction.save()
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        self.assertEqual(self._pickups().count(), 0)

    def test_mail_locations_get_no_pickup_event(self):
        self._location("By mail", pickup_by_mail=True, pickup_time=self.start + datetime.timedelta(days=3))
        self.assertEqual(self._pickups().count(), 0)

    def test_a_location_with_no_time_makes_no_event(self):
        self._location("Undecided")
        self.assertEqual(self._pickups().count(), 0)

    def test_online_auction_events_have_no_location(self):
        """The address belongs on the pickup event — the auction itself happens on the website."""
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        auction_event = ClubEvent.objects.get(auction=self.auction)
        self.assertEqual(auction_event.location, "")

    def test_in_person_auction_events_keep_their_location(self):
        self.auction.is_online = False
        self.auction.save()
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        self.auction.save()
        self.assertEqual(ClubEvent.objects.get(auction=self.auction).location, "Clubhouse St")

    def test_changing_a_pickup_time_moves_the_event(self):
        location = self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        moved = self.start + datetime.timedelta(days=4)
        location.pickup_time = moved
        location.save()
        event = self._pickups().first()
        self.assertEqual(event.date_start, moved)
        self.assertTrue(event.needs_google_sync)

    def test_clearing_a_pickup_time_retires_its_event(self):
        location = self._location(
            "Clubhouse",
            pickup_time=self.start + datetime.timedelta(days=3),
            second_pickup_time=self.start + datetime.timedelta(days=4),
        )
        self.assertEqual(self._pickups().count(), 2)
        location.second_pickup_time = None
        location.save()
        self.assertEqual(self._pickups().count(), 1)

    def test_unpromoting_the_auction_retires_its_pickup_events(self):
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        self.auction.promote_this_auction = False
        self.auction.save()
        self.assertEqual(self._pickups().count(), 0)

    def test_syncing_repeatedly_does_not_duplicate(self):
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        club_events.sync_pickup_events(self.auction)
        club_events.sync_auction_events(self.club)
        self.assertEqual(self._pickups().count(), 1)

    def test_pickup_events_are_not_editable_and_link_to_the_auction(self):
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        event = self._pickups().first()
        self.assertFalse(event.is_editable)
        self.assertTrue(event.is_automatic)
        self.assertEqual(event.related_auction, self.auction)
        self.assertEqual(event.get_absolute_url(), self.auction.get_absolute_url())

    def test_deleting_a_location_removes_its_events_from_google(self):
        """The rows cascade away, so the remote copies have to go first or they're orphaned."""
        location = self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        event = self._pickups().first()
        event.google_event_id = "g-1"
        event.save()
        with patch.object(club_events, "_remove_remote") as remove:
            location.delete()
        remove.assert_called_once()
        self.assertEqual(ClubEvent.objects.filter(pk=event.pk).count(), 0)

    def test_pickup_events_show_up_on_the_club_page(self):
        self._location("Clubhouse", pickup_time=self.start + datetime.timedelta(days=3))
        upcoming, _ = club_events.upcoming_events(self.club)
        titles = [e.title for e in upcoming]
        self.assertTrue([t for t in titles if "pickup" in t], titles)


class UpcomingEventsTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Upcoming Club")
        now = timezone.now()
        self.future = ClubEvent.objects.create(
            club=self.club, title="Future", date_start=now + datetime.timedelta(days=2)
        )
        self.past = ClubEvent.objects.create(
            club=self.club,
            title="Past",
            date_start=now - datetime.timedelta(days=5),
            date_end=now - datetime.timedelta(days=5, hours=-1),
        )
        self.deleted = ClubEvent.objects.create(
            club=self.club, title="Deleted", date_start=now + datetime.timedelta(days=1), is_deleted=True
        )

    def test_upcoming_excludes_past_and_deleted(self):
        upcoming, past = club_events.upcoming_events(self.club, include_past=True)
        self.assertEqual([e.title for e in upcoming], ["Future"])
        self.assertEqual([e.title for e in past], ["Past"])

    def test_an_event_in_progress_still_counts_as_upcoming(self):
        """Someone looking at the club page during a meeting should still see it listed."""
        now = timezone.now()
        ClubEvent.objects.create(
            club=self.club,
            title="Happening now",
            date_start=now - datetime.timedelta(hours=1),
            date_end=now + datetime.timedelta(hours=1),
        )
        upcoming, _ = club_events.upcoming_events(self.club)
        self.assertIn("Happening now", [e.title for e in upcoming])


class ClubEventViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="View Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="ev_admin", password="pw", email="a@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.outsider = User.objects.create_user(username="ev_out", password="pw", email="o@example.com")
        self.start = timezone.now() + datetime.timedelta(days=4)

    def _post_event(self, **overrides):
        data = {
            "title": "Monthly meeting",
            "date_start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": "",
            "location": "1 Main St",
            "description": "Bring fish",
        }
        data.update(overrides)
        return self.client.post(reverse("club_event_add", kwargs={"slug": self.club.slug}), data)

    def test_an_admin_can_add_an_event(self):
        self.client.force_login(self.admin)
        response = self._post_event()
        self.assertEqual(response.status_code, 302)
        event = ClubEvent.objects.get(club=self.club, title="Monthly meeting")
        self.assertEqual(event.source, ClubEvent.SOURCE_MANUAL)
        self.assertEqual(event.created_by, self.admin)
        self.assertEqual(event.location, "1 Main St")

    def test_an_outsider_cannot_add_an_event(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self._post_event().status_code, 403)
        self.assertFalse(ClubEvent.objects.filter(club=self.club).exists())

    def test_an_anonymous_user_is_sent_to_log_in(self):
        response = self.client.get(reverse("club_event_add", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_an_end_before_the_start_is_rejected(self):
        self.client.force_login(self.admin)
        response = self._post_event(date_end=(self.start - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "after the start time")
        self.assertFalse(ClubEvent.objects.filter(club=self.club).exists())

    def test_an_admin_can_edit_an_event(self):
        self.client.force_login(self.admin)
        event = ClubEvent.objects.create(club=self.club, title="Old", date_start=self.start)
        response = self.client.post(
            reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}),
            {
                "title": "New",
                "date_start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
                "date_end": "",
                "location": "",
                "description": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.title, "New")
        self.assertTrue(event.needs_google_sync)

    def test_an_admin_can_delete_an_event(self):
        self.client.force_login(self.admin)
        event = ClubEvent.objects.create(club=self.club, title="Doomed", date_start=self.start)
        response = self.client.post(
            reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}), {"action": "delete"}
        )
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertTrue(event.is_deleted)

    def _auction_event(self):
        auction = Auction.objects.create(
            title="Auction", date_start=self.start, club=self.club, promote_this_auction=True
        )
        return auction, ClubEvent.objects.get(auction=auction)

    def test_an_auction_events_wording_can_be_edited_but_nothing_else(self):
        """The form narrows itself: the date, the place and whether the event exists belong to the
        auction, and an event whose date disagrees with its auction is worse than no feature."""
        self.client.force_login(self.admin)
        _auction, event = self._auction_event()
        response = self.client.get(reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(sorted(form.fields), ["description", "reset_description", "reset_title", "title"])
        self.assertContains(response, "Edit event details")
        self.assertNotContains(response, "Delete event")

    def test_editing_the_wording_marks_it_as_the_clubs(self):
        self.client.force_login(self.admin)
        auction, event = self._auction_event()
        response = self.client.post(
            reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}),
            {"title": "Auction — April meeting", "description": "Doors at 6:30."},
        )
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.title, "Auction — April meeting")
        self.assertTrue(event.title_is_custom)
        self.assertTrue(event.description_is_custom)

        # And the whole point: the auction's next save leaves it alone.
        auction.save()
        event.refresh_from_db()
        self.assertEqual(event.title, "Auction — April meeting")

    def test_typing_the_auctions_own_words_back_in_is_not_a_custom_value(self):
        """Nothing for the flag to protect, and a flag set here would quietly stop the event
        following a later rename."""
        self.client.force_login(self.admin)
        _auction, event = self._auction_event()
        title, description = club_events.generated_wording(event)
        self.client.post(
            reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}),
            {"title": title, "description": description},
        )
        event.refresh_from_db()
        self.assertFalse(event.title_is_custom)
        self.assertFalse(event.description_is_custom)

    def test_the_reset_box_puts_the_auctions_wording_back(self):
        self.client.force_login(self.admin)
        _auction, event = self._auction_event()
        url = reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk})
        self.client.post(url, {"title": "April meeting", "description": "Doors at 6:30."})

        # Ticked alongside a typed value, reset wins — it is the only way back.
        self.client.post(url, {"title": "Something else", "description": "Doors at 6:30.", "reset_title": "on"})
        event.refresh_from_db()
        self.assertEqual(event.title, "Auction")
        self.assertFalse(event.title_is_custom)
        self.assertTrue(event.description_is_custom)

    def test_a_generated_event_cannot_be_deleted_through_the_form(self):
        """The auction is what put it here — deleting the row only means the next sync rebuilds
        it. Unpromote the auction instead."""
        self.client.force_login(self.admin)
        _auction, event = self._auction_event()
        response = self.client.post(
            reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}), {"action": "delete"}
        )
        self.assertEqual(response.status_code, 404)
        event.refresh_from_db()
        self.assertFalse(event.is_deleted)

    def test_the_club_page_offers_both_edit_buttons_on_a_generated_event(self):
        self.client.force_login(self.admin)
        auction, event = self._auction_event()
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Edit details")
        self.assertContains(response, "Edit auction")
        self.assertContains(response, reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}))
        self.assertContains(response, auction.get_edit_url())

    def test_an_event_from_another_club_is_not_reachable(self):
        self.client.force_login(self.admin)
        other = Club.objects.create(name="Other Club")
        event = ClubEvent.objects.create(club=other, title="Theirs", date_start=self.start)
        response = self.client.get(reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}))
        self.assertEqual(response.status_code, 404)

    def test_the_club_page_lists_events_and_offers_the_add_button_to_admins(self):
        ClubEvent.objects.create(club=self.club, title="Club Picnic", date_start=self.start)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Club Picnic")
        self.assertContains(response, reverse("club_event_add", kwargs={"slug": self.club.slug}))

    def test_the_club_page_hides_the_add_button_from_everyone_else(self):
        ClubEvent.objects.create(club=self.club, title="Club Picnic", date_start=self.start)
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Club Picnic")
        self.assertNotContains(response, reverse("club_event_add", kwargs={"slug": self.club.slug}))


class ClubPageCalendarButtonTests(TestCase):
    """Two buttons, both of which subscribe. Nothing that hands out a frozen copy."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Subscribe Club", enable_club_page=True)
        self.start = timezone.now() + datetime.timedelta(days=3)
        ClubEvent.objects.create(club=self.club, title="Club Picnic", date_start=self.start)

    def _page(self):
        return self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))

    def test_both_subscribe_buttons_are_offered(self):
        response = self._page()
        self.assertContains(response, "Google Calendar")
        self.assertContains(response, "Apple Calendar or Outlook")
        self.assertContains(response, "webcal://")

    def test_there_is_no_one_time_download(self):
        """A static copy of a calendar goes stale the moment it's imported.

        The feed's path is still all over the page — it's what both subscribe links point at.
        What must be gone is a bare *relative* link to it, which only downloads the file.
        """
        ical_path = reverse("club_events_ical", kwargs={"slug": self.club.slug})
        response = self._page()
        self.assertNotContains(response, f'href="{ical_path}"')
        self.assertNotContains(response, "Download once")

    def test_the_club_page_does_not_leak_template_comments(self):
        """Django's {# #} comment is single-line only — a two-line one renders onto the page."""
        body = self._page().content.decode()
        for phrase in ("Subscribing, not downloading", "dropdown-menu-end", "#}"):
            self.assertNotIn(phrase, body)


class ClubEventsEmbedTests(TestCase):
    """The iframe/JSON feed clubs paste into WordPress, and the admin-only snippets for it."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Embed Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="em_admin", password="pw", email="ea@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.member = User.objects.create_user(username="em_member", password="pw", email="em@example.com")
        ClubMember.objects.create(club=self.club, user=self.member)
        self.start = timezone.now() + datetime.timedelta(days=1)
        self.url = reverse("club_events_embed", kwargs={"slug": self.club.slug})

    def _events(self, count):
        for i in range(count):
            ClubEvent.objects.create(
                club=self.club,
                title=f"Meeting {i}",
                date_start=self.start + datetime.timedelta(days=i),
                location=f"{i} Main St",
            )

    def test_json_is_the_default_and_lists_upcoming_events(self):
        self._events(2)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["club"], "Embed Club")
        self.assertEqual([e["title"] for e in payload["events"]], ["Meeting 0", "Meeting 1"])
        self.assertIn("0 Main St", payload["events"][0]["location"])

    def test_count_one_returns_only_the_next_event(self):
        self._events(3)
        payload = self.client.get(self.url, {"count": 1}).json()
        self.assertEqual([e["title"] for e in payload["events"]], ["Meeting 0"])

    def test_count_is_capped_at_ten(self):
        self._events(14)
        payload = self.client.get(self.url, {"count": 99}).json()
        self.assertEqual(len(payload["events"]), 10)

    def test_a_junk_count_falls_back_to_the_default(self):
        self._events(3)
        payload = self.client.get(self.url, {"count": "lots"}).json()
        self.assertEqual(len(payload["events"]), 3)

    def test_a_zero_or_negative_count_still_returns_one_event(self):
        self._events(3)
        self.assertEqual(len(self.client.get(self.url, {"count": 0}).json()["events"]), 1)
        self.assertEqual(len(self.client.get(self.url, {"count": -5}).json()["events"]), 1)

    def test_past_and_deleted_events_are_left_out(self):
        self._events(1)
        ClubEvent.objects.create(
            club=self.club, title="Last year", date_start=timezone.now() - datetime.timedelta(days=30)
        )
        ClubEvent.objects.create(club=self.club, title="Gone", date_start=self.start, is_deleted=True)
        titles = [e["title"] for e in self.client.get(self.url).json()["events"]]
        self.assertEqual(titles, ["Meeting 0"])

    def test_pickup_events_are_left_out(self):
        """Logistics for people who already won lots, not something to put on a club's website."""
        auction = Auction.objects.create(
            title="Embed Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(days=2),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        PickupLocation.objects.create(
            auction=auction,
            name="Clubhouse",
            address="1 Main St",
            pickup_time=self.start + datetime.timedelta(days=3),
        )
        self.assertTrue(ClubEvent.objects.filter(club=self.club, source=ClubEvent.SOURCE_PICKUP).exists())
        titles = [e["title"] for e in self.client.get(self.url).json()["events"]]
        self.assertEqual(titles, ["Embed Auction"])

    def test_the_limit_counts_real_events_not_ones_filtered_out(self):
        """Filtering after the slice would hand back fewer events than asked for."""
        auction = Auction.objects.create(
            title="Embed Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(hours=2),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        PickupLocation.objects.create(
            auction=auction,
            name="Clubhouse",
            address="1 Main St",
            pickup_time=self.start + datetime.timedelta(hours=3),
        )
        self._events(3)
        payload = self.client.get(self.url, {"count": 2}).json()
        self.assertEqual(len(payload["events"]), 2)
        self.assertNotIn("pickup", " ".join(e["title"] for e in payload["events"]).lower())

    def test_iframe_formats_render_a_themed_page(self):
        self._events(1)
        for fmt, theme in (("iframelight", "light"), ("iframedark", "dark")):
            with self.subTest(fmt=fmt):
                response = self.client.get(self.url, {"format": fmt})
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                self.assertIn(f'data-theme="{theme}"', body)
                self.assertIn("Meeting 0", body)

    def test_the_unstyled_format_is_a_bare_list(self):
        self._events(1)
        body = self.client.get(self.url, {"format": "unstyledhtml"}).content.decode()
        self.assertIn("club-events", body)
        self.assertNotIn("<style", body)

    def test_an_empty_calendar_says_so_rather_than_erroring(self):
        response = self.client.get(self.url, {"format": "iframelight"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing coming up")

    def test_an_unknown_format_falls_back_to_json(self):
        self._events(1)
        response = self.client.get(self.url, {"format": "iframelite"})
        self.assertEqual(response["Content-Type"], "application/json")

    def test_the_embed_can_be_framed_and_fetched_cross_origin(self):
        self._events(1)
        for fmt in ("json", "iframelight"):
            with self.subTest(fmt=fmt):
                response = self.client.get(self.url, {"format": fmt})
                self.assertEqual(response["Access-Control-Allow-Origin"], "*")
                self.assertNotIn("X-Frame-Options", response)

    def test_a_club_with_its_page_disabled_has_no_embed(self):
        self.club.enable_club_page = False
        self.club.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_embed_exposes_no_member_data(self):
        self._events(1)
        body = self.client.get(self.url).content.decode()
        for secret in ("em@example.com", "ea@example.com", "em_member", "em_admin"):
            self.assertNotIn(secret, body)

    def test_the_snippets_live_on_the_website_integration_page_not_the_calendar(self):
        """They used to be a collapsed panel on the club page; they are a page of their own now."""
        self._events(1)
        club_page = reverse("club_detail", kwargs={"slug": self.club.slug})
        integration = reverse("club_website_integration", kwargs={"slug": self.club.slug})

        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(club_page), "iframelight")
        self.assertContains(self.client.get(integration), self.url)

    def test_only_admins_can_open_the_website_integration_page(self):
        integration = reverse("club_website_integration", kwargs={"slug": self.club.slug})

        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(integration).status_code, 200)

        self.client.force_login(self.member)
        self.assertEqual(self.client.get(integration).status_code, 403)

        # Anonymous gets the same 403 as a signed-in non-admin: the view's own permission check
        # runs before LoginRequiredMixin, which is how every other club admin page behaves.
        self.client.logout()
        self.assertEqual(self.client.get(integration).status_code, 403)

    def test_the_snippets_offer_the_next_event_and_the_next_ten(self):
        self._events(1)
        self.client.force_login(self.admin)
        body = self.client.get(reverse("club_website_integration", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("count=1", body)
        self.assertIn("count=10", body)
        self.assertIn("iframelight", body)
        self.assertIn("iframedark", body)

    def test_the_calendar_links_are_offered_as_plain_addresses(self):
        """Not an embed on purpose — a club's own site already has somewhere to put a link, and
        an iframe is the wrong shape for "subscribe to our calendar"."""
        self._events(1)
        self.client.force_login(self.admin)
        body = self.client.get(reverse("club_website_integration", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("Calendar links", body)
        self.assertIn("webcal://", body)
        self.assertIn("events.ics", body)

    def test_a_shared_google_calendar_replaces_them(self):
        self.club.google_calendar_refresh_token = "token"
        self.club.google_calendar_id = "abc@group.calendar.google.com"
        self.club.google_calendar_is_public = True
        self.club.save()
        self.client.force_login(self.admin)
        body = self.client.get(reverse("club_website_integration", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("calendar.google.com", body)
        self.assertNotIn("webcal://", body)


class ClubPastEventsEmbedTests(TestCase):
    """The backwards half of the events embed: what the club has actually been doing."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="History Club", enable_club_page=True)
        self.url = reverse("club_past_events_embed", kwargs={"slug": self.club.slug})
        now = timezone.now()
        for days, title in ((30, "Long ago"), (7, "Last week"), (2, "Two days ago")):
            ClubEvent.objects.create(
                club=self.club,
                title=title,
                date_start=now - datetime.timedelta(days=days),
                date_end=now - datetime.timedelta(days=days) + datetime.timedelta(hours=2),
                location="1 Main St",
            )
        ClubEvent.objects.create(club=self.club, title="Next month", date_start=now + datetime.timedelta(days=30))

    def test_it_lists_what_has_already_happened_newest_first(self):
        payload = self.client.get(self.url).json()
        self.assertEqual(
            [e["title"] for e in payload["past_events"]],
            ["Two days ago", "Last week", "Long ago"],
        )

    def test_count_one_is_the_most_recent_event(self):
        payload = self.client.get(self.url, {"count": 1}).json()
        self.assertEqual([e["title"] for e in payload["past_events"]], ["Two days ago"])

    def test_upcoming_events_are_never_in_it(self):
        titles = [e["title"] for e in self.client.get(self.url).json()["past_events"]]
        self.assertNotIn("Next month", titles)

    def test_it_takes_the_same_count_and_format_parameters(self):
        self.assertEqual(len(self.client.get(self.url, {"count": 99}).json()["past_events"]), 3)
        self.assertEqual(len(self.client.get(self.url, {"count": 0}).json()["past_events"]), 1)
        self.assertEqual(len(self.client.get(self.url, {"count": "lots"}).json()["past_events"]), 3)
        for fmt, theme in (("iframelight", "light"), ("iframedark", "dark")):
            with self.subTest(fmt=fmt):
                body = self.client.get(self.url, {"format": fmt}).content.decode()
                self.assertIn(f'data-theme="{theme}"', body)
                self.assertIn("Two days ago", body)

    def test_a_row_is_formatted_exactly_like_an_upcoming_one(self):
        """One formatter, deliberately: two lists on one club website must not drift apart."""
        upcoming = self.client.get(reverse("club_events_embed", kwargs={"slug": self.club.slug})).json()
        past = self.client.get(self.url).json()
        self.assertEqual(sorted(upcoming["events"][0]), sorted(past["past_events"][0]))

    def test_an_empty_history_does_not_say_nothing_coming_up(self):
        ClubEvent.objects.filter(club=self.club).delete()
        body = self.client.get(self.url, {"format": "iframelight"}).content.decode()
        self.assertIn("Nothing here yet", body)
        self.assertNotIn("Nothing coming up", body)

    def test_it_is_framable_and_fetchable_like_the_others(self):
        response = self.client.get(self.url, {"format": "iframelight"})
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertNotIn("X-Frame-Options", response)

    def test_a_club_with_its_page_disabled_has_no_embed(self):
        self.club.enable_club_page = False
        self.club.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)


class EmbedSelfSizingTests(TestCase):
    """An iframe cannot size itself, so the embed measures itself and the snippet listens."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Sizing Club", enable_club_page=True, enable_breeder_award_program=True)
        self.admin = User.objects.create_user(username="sz_admin", password="pw", email="sz@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)

    def _embed(self, name):
        return self.client.get(reverse(name, kwargs={"slug": self.club.slug}), {"format": "iframelight"})

    def test_every_styled_embed_reports_its_height(self):
        for name in (
            "club_events_embed",
            "club_past_events_embed",
            "club_announcements_embed",
            "club_auction_embed",
            "bap_embed",
        ):
            with self.subTest(embed=name):
                body = self._embed(name).content.decode()
                self.assertIn("window.parent.postMessage", body)
                self.assertIn('clubEmbed: "height"', body)

    def test_it_stays_quiet_when_nothing_framed_it(self):
        """Opening the embed URL directly must not post a message to itself."""
        self.assertIn("if (window.parent === window) { return; }", self._embed("club_events_embed").content.decode())

    def test_nothing_is_fetched_from_outside(self):
        body = self._embed("club_events_embed").content.decode()
        self.assertNotIn("<script src", body)
        self.assertNotIn("://fonts.", body)

    def test_the_snippet_hands_over_a_listener_with_the_iframe(self):
        self.client.force_login(self.admin)
        body = self.client.get(reverse("club_website_integration", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn('&lt;script&gt;addEventListener("message"', body)
        self.assertIn("testserver", body)

    def test_the_listener_checks_where_the_message_came_from(self):
        """A club page carrying somebody else's iframe must not be resizable by it."""
        self.client.force_login(self.admin)
        body = self.client.get(reverse("club_website_integration", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("e.origin!==", body)
        self.assertIn("contentWindow===e.source", body)


class EventsEmbedUsageTrackingTests(TestCase):
    """Whether a club's own website is actually showing our calendar."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Tracked Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="tr_admin", password="pw", email="tr@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.member = User.objects.create_user(username="tr_member", password="pw", email="trm@example.com")
        ClubMember.objects.create(club=self.club, user=self.member)
        self.url = reverse("club_events_embed", kwargs={"slug": self.club.slug})

    def _views(self):
        self.club.refresh_from_db()
        return self.club.events_website_views

    def test_a_club_starts_with_nothing_recorded(self):
        self.assertEqual(self._views(), 0)
        self.assertFalse(self.club.embeds_events_on_website)

    def test_every_format_counts(self):
        for fmt in ("json", "iframelight", "iframedark", "unstyledhtml"):
            self.client.get(self.url, {"format": fmt})
        self.assertEqual(self._views(), 4)

    def test_an_empty_calendar_still_counts(self):
        """The snippet is installed either way, and that is the whole fact being collected."""
        self.assertFalse(ClubEvent.objects.filter(club=self.club).exists())
        self.client.get(self.url)
        self.assertEqual(self._views(), 1)

    def test_the_past_events_embed_counts_too(self):
        self.client.get(reverse("club_past_events_embed", kwargs={"slug": self.club.slug}))
        self.assertEqual(self._views(), 1)

    def test_an_admin_checking_their_own_snippet_does_not_count(self):
        self.client.force_login(self.admin)
        self.client.get(self.url)
        self.assertEqual(self._views(), 0)

    def test_an_ordinary_member_counts(self):
        """A member reading it on the club's website is exactly the thing worth counting."""
        self.client.force_login(self.member)
        self.client.get(self.url)
        self.assertEqual(self._views(), 1)

    def test_the_club_page_here_is_not_the_club_website(self):
        self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertEqual(self._views(), 0)

    def test_a_recent_render_means_the_embed_is_installed(self):
        self.client.get(self.url)
        self.club.refresh_from_db()
        self.assertTrue(self.club.embeds_events_on_website)

    def test_a_snippet_taken_down_long_ago_stops_counting_as_installed(self):
        self.client.get(self.url)
        self.club.refresh_from_db()
        stale = timezone.now() - datetime.timedelta(days=Club.EVENTS_EMBED_ACTIVE_DAYS + 1)
        Club.objects.filter(pk=self.club.pk).update(events_website_last_view=stale)
        self.club.refresh_from_db()
        self.assertFalse(self.club.embeds_events_on_website)


class CustomizeEventPromptTests(TestCase):
    """The auction-page nudge to write your own wording for the calendar entry members read."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Prompt Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="pr_admin", password="pw", email="pr@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.auction = Auction.objects.create(
            title="Prompt Auction",
            date_start=timezone.now() + datetime.timedelta(days=10),
            club=self.club,
            is_online=False,
            promote_this_auction=True,
            created_by=self.admin,
        )
        self.event = ClubEvent.objects.filter(auction=self.auction).first()
        self._mark_embedded()

    def _mark_embedded(self):
        Club.objects.filter(pk=self.club.pk).update(events_website_views=5, events_website_last_view=timezone.now())
        self.club.refresh_from_db()
        self.auction.refresh_from_db()

    def test_the_auction_has_a_generated_event_to_offer(self):
        self.assertIsNotNone(self.event)
        self.assertTrue(self.event.is_automatic)
        self.assertEqual(self.auction.event_needing_custom_wording, self.event)

    def test_no_prompt_when_the_club_does_not_embed_our_events(self):
        Club.objects.filter(pk=self.club.pk).update(events_website_views=0, events_website_last_view=None)
        self.auction.refresh_from_db()
        self.assertIsNone(self.auction.event_needing_custom_wording)

    def test_no_prompt_once_either_field_has_been_typed_by_hand(self):
        for field in ("title_is_custom", "description_is_custom"):
            with self.subTest(field=field):
                fields = {"title_is_custom": False, "description_is_custom": False, field: True}
                ClubEvent.objects.filter(pk=self.event.pk).update(**fields)
                self.auction.refresh_from_db()
                self.assertIsNone(self.auction.event_needing_custom_wording)

    def test_no_prompt_for_an_auction_that_is_over(self):
        long_ago = timezone.now() - datetime.timedelta(days=30)
        Auction.objects.filter(pk=self.auction.pk).update(
            date_start=long_ago,
            date_end=long_ago,
            date_online_bidding_ends=long_ago,
            lot_submission_end_date=long_ago,
        )
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.pretty_much_over)
        self.assertIsNone(self.auction.event_needing_custom_wording)

    def test_no_prompt_for_an_auction_with_no_club(self):
        Auction.objects.filter(pk=self.auction.pk).update(club=None)
        self.auction.refresh_from_db()
        self.assertIsNone(self.auction.event_needing_custom_wording)

    def test_the_banner_is_on_the_auction_page_for_an_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        self.assertContains(response, "website is showing this auction")
        self.assertContains(response, "Customize this event")
        self.assertContains(response, reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": self.event.pk}))

    def test_an_ordinary_user_never_sees_it(self):
        other = User.objects.create_user(username="pr_other", password="pw", email="pro@example.com")
        self.client.force_login(other)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        self.assertNotContains(response, "website is showing this auction")

    def test_dismissing_it_sticks(self):
        self.client.force_login(self.admin)
        url = reverse("auction_main", kwargs={"slug": self.auction.slug})
        self.client.get(url, {"dismissed_customize_event_banner": "true"})
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.dismissed_customize_event_banner)
        self.assertIsNone(self.auction.event_needing_custom_wording)
        self.assertNotContains(self.client.get(url), "website is showing this auction")

    def test_the_customize_link_works_for_an_auction_admin_with_no_club_role(self):
        """The banner is written for the auction's creator, who often holds no club permission."""
        creator = User.objects.create_user(username="pr_creator", password="pw", email="prc@example.com")
        Auction.objects.filter(pk=self.auction.pk).update(created_by=creator)
        self.client.force_login(creator)
        response = self.client.get(reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": self.event.pk}))
        self.assertEqual(response.status_code, 200)

    def test_an_unrelated_user_still_cannot_edit_the_event(self):
        stranger = User.objects.create_user(username="pr_stranger", password="pw", email="prs@example.com")
        self.client.force_login(stranger)
        response = self.client.get(reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": self.event.pk}))
        self.assertEqual(response.status_code, 403)


class ClubEventICalTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="iCal Club", enable_club_page=True)
        self.start = timezone.now() + datetime.timedelta(days=2)

    def test_the_feed_renders_events(self):
        ClubEvent.objects.create(club=self.club, title="Annual Show", date_start=self.start, location="1 Main St")
        response = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/calendar", response["Content-Type"])
        body = response.content.decode()
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("SUMMARY:Annual Show", body)
        self.assertIn("LOCATION:1 Main St", body)
        self.assertIn("END:VCALENDAR", body)

    def test_special_characters_are_escaped(self):
        """An unescaped comma or semicolon silently truncates the field in most calendar apps."""
        ClubEvent.objects.create(club=self.club, title="Fish, Plants; and Snails", date_start=self.start)
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn(r"SUMMARY:Fish\, Plants\; and Snails", body)

    def test_deleted_events_are_left_out(self):
        ClubEvent.objects.create(club=self.club, title="Gone", date_start=self.start, is_deleted=True)
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertNotIn("Gone", body)

    def test_a_club_with_its_page_disabled_has_no_feed(self):
        self.club.enable_club_page = False
        self.club.save()
        response = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 404)

    def test_each_event_carries_a_sequence_so_edits_reach_subscribers(self):
        """Most clients keep the copy they already imported unless the sequence goes up."""
        event = ClubEvent.objects.create(club=self.club, title="Annual Show", date_start=self.start)
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn(f"SEQUENCE:{int(event.updated_at.timestamp())}", body)

    def test_all_day_events_are_dates_not_midnight_appointments(self):
        day = datetime.datetime(2026, 8, 1, tzinfo=timezone.get_current_timezone())
        ClubEvent.objects.create(
            club=self.club,
            title="Show weekend",
            date_start=day,
            date_end=day + datetime.timedelta(days=2),
            all_day=True,
        )
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("DTSTART;VALUE=DATE:20260801", body)
        # Google and iCal both write the end of an all-day event as the day after it finishes.
        self.assertIn("DTEND;VALUE=DATE:20260803", body)

    def test_a_cancelled_event_says_so(self):
        ClubEvent.objects.create(club=self.club, title="Called off", date_start=self.start, cancelled=True)
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("STATUS:CANCELLED", body)

    def test_the_feed_names_its_timezone(self):
        ClubEvent.objects.create(club=self.club, title="Annual Show", date_start=self.start)
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("X-WR-TIMEZONE:", body)


@override_settings(GOOGLE_CALENDAR_CLIENT_ID="cid", GOOGLE_CALENDAR_CLIENT_SECRET="secret")
class GoogleCalendarSyncTests(TestCase):
    """Everything below mocks google_calendar._request, the module's single network entry point."""

    def setUp(self):
        self.club = Club.objects.create(name="Sync Club")
        self.club.google_calendar_refresh_token = "refresh"
        self.club.google_calendar_id = "cal-1"
        self.club.save()
        self.start = timezone.now() + datetime.timedelta(days=3)

    def test_ensure_calendar_never_touches_sharing(self):
        """Regression guard. Writing an ACL rule needs calendar.acls or calendar — both sensitive,
        both granting control over every calendar the admin owns. We deliberately ask for neither,
        so any /acl call here is a bug that breaks the club's syncing outright with
        'Request had insufficient authentication scopes'."""
        with patch.object(gcal, "_request", return_value={"id": "cal-1"}) as request:
            gcal.ensure_calendar(self.club)
        called = [f"{call[0][1]} {call[0][2]}" for call in request.call_args_list]
        self.assertFalse([path for path in called if "/acl" in path], f"ensure_calendar hit the ACL API: {called}")

    def test_ensure_calendar_reuses_an_existing_calendar(self):
        with patch.object(gcal, "_request", return_value={"id": "cal-1"}) as request:
            self.assertEqual(gcal.ensure_calendar(self.club), "cal-1")
        # One GET to confirm it's still there, and no POST creating a second one.
        self.assertEqual(len(request.call_args_list), 1)
        self.assertEqual(request.call_args_list[0][0][1], "GET")

    def test_a_calendar_deleted_in_google_is_recreated_and_events_requeued(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        with patch.object(gcal, "_request", side_effect=[404, {"id": "cal-new"}]):
            self.assertEqual(gcal.ensure_calendar(self.club), "cal-new")
        event.refresh_from_db()
        self.assertEqual(event.google_event_id, "")
        self.assertTrue(event.needs_google_sync)

    def test_is_configured_needs_both_halves_of_the_oauth_app(self):
        self.assertTrue(gcal.is_configured())
        with override_settings(GOOGLE_CALENDAR_CLIENT_SECRET=""):
            self.assertFalse(gcal.is_configured())

    def test_authorize_url_asks_for_offline_access(self):
        """Without access_type=offline Google never returns a refresh token and the
        integration silently stops working an hour after it's set up."""
        url = gcal.authorize_url("https://example.com/cb", "state123")
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("state=state123", url)

    def test_pushing_a_new_event_records_the_google_id(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(gcal, "_request", return_value={"id": "g-1"}) as request:
            self.assertTrue(gcal.push_event(event))
        event.refresh_from_db()
        self.assertEqual(event.google_event_id, "g-1")
        self.assertFalse(event.needs_google_sync)
        self.assertEqual(request.call_args[0][1], "POST")

    def test_pushing_an_existing_event_updates_rather_than_duplicating(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        with patch.object(gcal, "_request", return_value={"id": "g-1"}) as request:
            gcal.push_event(event)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args[0][1], "PUT")

    def test_an_event_deleted_in_google_is_recreated_on_the_next_push(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="gone")
        with patch.object(gcal, "_request", side_effect=[404, {"id": "g-new"}]) as request:
            gcal.push_event(event)
        event.refresh_from_db()
        self.assertEqual(event.google_event_id, "g-new")
        self.assertEqual(request.call_args[0][1], "POST")

    def test_the_event_body_carries_our_uuid(self):
        """The uuid is how a pull recognizes an event we pushed but failed to record."""
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        body = gcal._event_body(event)
        self.assertEqual(body["extendedProperties"]["private"]["auctionSiteEventUuid"], str(event.uuid))
        self.assertEqual(body["summary"], "Meeting")

    def test_pulling_creates_events_from_google(self):
        page = {
            "items": [
                {
                    "id": "g-100",
                    "status": "confirmed",
                    "summary": "Board meeting",
                    "description": "Upstairs",
                    "location": "2 Elm St",
                    "start": {"dateTime": self.start.isoformat()},
                    "end": {"dateTime": (self.start + datetime.timedelta(hours=1)).isoformat()},
                }
            ],
            "nextSyncToken": "token-1",
        }
        with patch.object(gcal, "_request", return_value=page):
            created, updated, deleted = gcal.pull_events(self.club)
        self.assertEqual((created, updated, deleted), (1, 0, 0))
        event = ClubEvent.objects.get(google_event_id="g-100")
        self.assertEqual(event.source, ClubEvent.SOURCE_GOOGLE)
        self.assertEqual(event.title, "Board meeting")
        self.assertEqual(event.location, "2 Elm St")
        # A pulled event must not be immediately pushed back to Google.
        self.assertFalse(event.needs_google_sync)
        self.club.refresh_from_db()
        self.assertEqual(self.club.google_calendar_sync_token, "token-1")

    def test_pulling_handles_all_day_events(self):
        page = {
            "items": [
                {
                    "id": "g-allday",
                    "status": "confirmed",
                    "summary": "Fish show",
                    "start": {"date": "2026-09-01"},
                    "end": {"date": "2026-09-02"},
                }
            ],
            "nextSyncToken": "t",
        }
        with patch.object(gcal, "_request", return_value=page):
            gcal.pull_events(self.club)
        event = ClubEvent.objects.get(google_event_id="g-allday")
        self.assertEqual(event.date_start.date(), datetime.date(2026, 9, 1))

    def test_a_cancelled_google_event_soft_deletes_ours(self):
        ClubEvent.objects.create(club=self.club, title="Doomed", date_start=self.start, google_event_id="g-200")
        page = {"items": [{"id": "g-200", "status": "cancelled"}], "nextSyncToken": "t"}
        with patch.object(gcal, "_request", return_value=page):
            _, _, deleted = gcal.pull_events(self.club)
        self.assertEqual(deleted, 1)
        self.assertTrue(ClubEvent.objects.get(google_event_id="g-200").is_deleted)

    def test_deleting_an_auction_event_in_google_puts_it_back(self):
        """The auction is still real, so the club page must keep showing it."""
        auction = Auction.objects.create(
            title="Auction", date_start=self.start, club=self.club, promote_this_auction=True
        )
        event = ClubEvent.objects.get(auction=auction)
        event.google_event_id = "g-300"
        event.save()
        page = {"items": [{"id": "g-300", "status": "cancelled"}], "nextSyncToken": "t"}
        with patch.object(gcal, "_request", return_value=page):
            gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertFalse(event.is_deleted)
        self.assertTrue(event.needs_google_sync)
        self.assertEqual(event.google_event_id, "")

    def test_a_google_edit_never_overwrites_an_auction_event(self):
        auction = Auction.objects.create(
            title="Real Title", date_start=self.start, club=self.club, promote_this_auction=True
        )
        event = ClubEvent.objects.get(auction=auction)
        event.google_event_id = "g-400"
        event.save()
        page = {
            "items": [
                {
                    "id": "g-400",
                    "status": "confirmed",
                    "summary": "Vandalized",
                    "start": {"dateTime": self.start.isoformat()},
                    "end": {"dateTime": (self.start + datetime.timedelta(hours=1)).isoformat()},
                }
            ],
            "nextSyncToken": "t",
        }
        with patch.object(gcal, "_request", return_value=page):
            gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertEqual(event.title, "Real Title")

    def test_a_pull_reclaims_an_event_we_pushed_but_did_not_record(self):
        """Guards the crash-between-POST-and-save case, which would otherwise duplicate."""
        event = ClubEvent.objects.create(club=self.club, title="Orphan", date_start=self.start)
        page = {
            "items": [
                {
                    "id": "g-500",
                    "status": "confirmed",
                    "summary": "Orphan",
                    "start": {"dateTime": self.start.isoformat()},
                    "end": {"dateTime": (self.start + datetime.timedelta(hours=1)).isoformat()},
                    "extendedProperties": {"private": {"auctionSiteEventUuid": str(event.uuid)}},
                }
            ],
            "nextSyncToken": "t",
        }
        with patch.object(gcal, "_request", return_value=page):
            created, _, _ = gcal.pull_events(self.club)
        self.assertEqual(created, 0)
        event.refresh_from_db()
        self.assertEqual(event.google_event_id, "g-500")
        self.assertEqual(ClubEvent.objects.filter(club=self.club, is_deleted=False).count(), 1)

    def test_an_expired_sync_token_is_dropped_so_the_next_run_is_a_full_pull(self):
        self.club.google_calendar_sync_token = "stale"
        self.club.save()
        with patch.object(gcal, "_request", return_value=gcal.SYNC_TOKEN_GONE):
            gcal.pull_events(self.club)
        self.club.refresh_from_db()
        self.assertEqual(self.club.google_calendar_sync_token, "")

    def test_sync_club_records_an_error_instead_of_raising(self):
        with patch.object(gcal, "ensure_calendar", side_effect=gcal.GoogleCalendarError("boom")):
            self.assertFalse(gcal.sync_club(self.club))
        self.club.refresh_from_db()
        self.assertIn("boom", self.club.google_calendar_last_error)

    def test_a_successful_sync_clears_a_previous_error(self):
        self.club.google_calendar_last_error = "old failure"
        self.club.save()
        with (
            patch.object(gcal, "ensure_calendar", return_value="cal-1"),
            patch.object(gcal, "push_pending", return_value=(0, None)),
            patch.object(gcal, "pull_events", return_value=(0, 0, 0)),
            patch.object(gcal, "refresh_public_flag"),
        ):
            self.assertTrue(gcal.sync_club(self.club))
        self.club.refresh_from_db()
        self.assertEqual(self.club.google_calendar_last_error, "")

    def test_one_rejected_event_does_not_stop_the_others(self):
        """A single event Google won't accept must not block the rest of the club's calendar."""
        bad = ClubEvent.objects.create(club=self.club, title="Bad", date_start=self.start)
        good = ClubEvent.objects.create(club=self.club, title="Good", date_start=self.start)

        def push(event):
            if event.pk == bad.pk:
                msg = "Google said no"
                raise gcal.GoogleCalendarError(msg)
            event.google_event_id = "g-ok"
            event.needs_google_sync = False
            event.save(update_fields=["google_event_id", "needs_google_sync"])
            return True

        with patch.object(gcal, "push_event", side_effect=push):
            pushed, error = gcal.push_pending(self.club)
        self.assertEqual(pushed, 1)
        self.assertIsNotNone(error)
        good.refresh_from_db()
        self.assertEqual(good.google_event_id, "g-ok")

    def test_a_rejected_event_still_lets_the_pull_run(self):
        """Otherwise one poisoned event cuts the club off from Google-side changes forever."""
        with (
            patch.object(gcal, "ensure_calendar", return_value="cal-1"),
            patch.object(gcal, "push_pending", return_value=(0, gcal.GoogleCalendarError("nope"))),
            patch.object(gcal, "pull_events", return_value=(1, 0, 0)) as pull,
        ):
            self.assertFalse(gcal.sync_club(self.club))
        pull.assert_called_once()
        self.club.refresh_from_db()
        self.assertIn("nope", self.club.google_calendar_last_error)

    def test_disconnecting_clears_the_tokens(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        gcal.disconnect(self.club)
        self.club.refresh_from_db()
        event.refresh_from_db()
        self.assertFalse(self.club.google_calendar_connected)
        self.assertEqual(self.club.google_calendar_refresh_token, "")
        # The event itself survives — only the Google link is gone.
        self.assertFalse(event.is_deleted)

    def test_disconnecting_keeps_the_calendar_so_reconnecting_resumes_it(self):
        """Making a second calendar would strand every member who subscribed to the first."""
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        gcal.disconnect(self.club)
        self.club.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(self.club.google_calendar_id, "cal-1")
        self.assertEqual(event.google_event_id, "g-1")
        # Everything is queued to go back out, so the calendar catches up on reconnect.
        self.assertTrue(event.needs_google_sync)

    def test_a_calendar_the_new_account_cannot_see_is_replaced(self):
        """Reconnecting a *different* Google account can't touch the old calendar, so we start
        a new one rather than failing every sync from then on."""
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        with patch.object(gcal, "_request", side_effect=[404, {"id": "cal-2"}]):
            self.assertEqual(gcal.ensure_calendar(self.club), "cal-2")
        event.refresh_from_db()
        self.assertEqual(event.google_event_id, "")
        self.assertTrue(event.needs_google_sync)

    def test_a_revoked_refresh_token_disconnects_the_club(self):
        """Otherwise every sync fails forever with no sign of what to do about it."""

        class FakeResponse:
            status_code = 400

            def json(self):
                return {"error": "invalid_grant"}

        with patch.object(gcal.requests, "post", return_value=FakeResponse()):
            with self.assertRaises(gcal.GoogleCalendarError):
                gcal.get_access_token(self.club)
        self.club.refresh_from_db()
        self.assertFalse(self.club.google_calendar_connected)
        self.assertIn("revoked", self.club.google_calendar_last_error)

    def test_a_cached_access_token_is_reused(self):
        self.club.google_calendar_access_token = "cached"
        self.club.google_calendar_token_expires = timezone.now() + datetime.timedelta(minutes=30)
        self.club.save()
        with patch.object(gcal.requests, "post") as post:
            self.assertEqual(gcal.get_access_token(self.club), "cached")
        post.assert_not_called()

    def test_an_expiring_access_token_is_refreshed(self):
        self.club.google_calendar_access_token = "old"
        self.club.google_calendar_token_expires = timezone.now() + datetime.timedelta(seconds=10)
        self.club.save()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"access_token": "fresh", "expires_in": 3600}

        with patch.object(gcal.requests, "post", return_value=FakeResponse()):
            self.assertEqual(gcal.get_access_token(self.club), "fresh")


@override_settings(GOOGLE_CALENDAR_CLIENT_ID="cid", GOOGLE_CALENDAR_CLIENT_SECRET="secret")
class GoogleCalendarConfigViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Config Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="gc_admin", password="pw", email="gc@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_edit_club=True)
        self.outsider = User.objects.create_user(username="gc_out", password="pw", email="gco@example.com")
        self.url = reverse("club_google_calendar_config", kwargs={"slug": self.club.slug})

    def test_an_admin_sees_the_connect_button(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("google_calendar_connect", kwargs={"slug": self.club.slug}))

    def test_an_outsider_is_refused(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_settings_can_be_saved(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {"add_auctions_to_calendar": "on"})
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertTrue(self.club.add_auctions_to_calendar)
        # Unchecked boxes are off.
        self.assertFalse(self.club.create_discord_events_for_club_events)

    def test_the_settings_form_cannot_declare_the_calendar_public(self):
        """Sharing is read from Google, never posted. A form that accepted it would be a way to
        put the Google links on the club page for a calendar nobody outside the club can open."""
        self.client.force_login(self.admin)
        self.client.post(self.url, {"google_calendar_is_public": "on"})
        self.club.refresh_from_db()
        self.assertFalse(self.club.google_calendar_is_public)

    def test_connect_redirects_to_google(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("google_calendar_connect", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.google.com", response.url)

    def test_connect_is_refused_to_an_outsider(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("google_calendar_connect", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 403)

    def test_the_callback_rejects_a_bad_state(self):
        """The state parameter is the only CSRF defense on the OAuth round trip."""
        self.client.force_login(self.admin)
        session = self.client.session
        session["google_calendar_oauth_club_slug"] = self.club.slug
        session["google_calendar_oauth_state"] = "expected"
        session.save()
        with patch.object(gcal, "exchange_code") as exchange:
            response = self.client.get(reverse("google_calendar_callback"), {"code": "abc", "state": "wrong"})
        exchange.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertFalse(self.club.google_calendar_connected)

    def test_the_state_is_a_fresh_nonce_not_the_users_unsubscribe_link(self):
        """That link is printed in the footer of every email we send, so anyone holding one
        could otherwise complete this flow against someone else's Google account."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse("google_calendar_connect", kwargs={"slug": self.club.slug}))
        state = self.client.session["google_calendar_oauth_state"]
        self.assertTrue(state)
        self.assertNotEqual(state, str(self.admin.userdata.unsubscribe_link))
        self.assertIn(f"state={state}", response.url)

    def test_the_callback_rejects_a_state_that_was_never_issued(self):
        """No connect step means no nonce in the session, so a link someone was handed is dead."""
        self.client.force_login(self.admin)
        session = self.client.session
        session["google_calendar_oauth_club_slug"] = self.club.slug
        session.save()
        with patch.object(gcal, "exchange_code") as exchange:
            self.client.get(reverse("google_calendar_callback"), {"code": "abc", "state": ""})
        exchange.assert_not_called()

    def test_a_successful_callback_stores_the_tokens(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session["google_calendar_oauth_club_slug"] = self.club.slug
        session["google_calendar_oauth_state"] = "nonce-1"
        session.save()
        state = "nonce-1"
        with (
            patch.object(gcal, "exchange_code", return_value=("refresh", "access", 3600, "club@example.com")),
            patch.object(gcal, "ensure_calendar", return_value="cal-1"),
            patch.object(gcal, "sync_club", return_value=True),
        ):
            response = self.client.get(reverse("google_calendar_callback"), {"code": "abc", "state": state})
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertEqual(self.club.google_calendar_refresh_token, "refresh")
        self.assertEqual(self.club.google_calendar_account_email, "club@example.com")
        self.assertIsNotNone(self.club.google_calendar_connected_on)


@override_settings(DISCORD_BOT_TOKEN="bot-token")
class DiscordClubEventTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="Discord Club", discord_server_id="guild-1", create_discord_events_for_club_events=True
        )
        self.start = timezone.now() + datetime.timedelta(days=2)

    def test_a_future_event_gets_a_discord_event(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            self.assertEqual(discord_events.sync_club_events(self.club), 1)
        create.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.discord_event_id, "d-1")
        self.assertFalse(event.needs_discord_sync)

    def test_auction_events_are_skipped_so_they_are_never_doubled_up(self):
        """auction_emails owns Discord events for auctions; this path must stay out of the way."""
        Auction.objects.create(title="Auction", date_start=self.start, club=self.club, promote_this_auction=True)
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            self.assertEqual(discord_events.sync_club_events(self.club), 0)
        create.assert_not_called()

    def test_pickup_events_are_skipped_too(self):
        """Four pickup slots on one auction would otherwise be four more Discord events, for
        logistics that only concern people who already won a lot."""
        auction = Auction.objects.create(
            title="Auction", date_start=self.start, club=self.club, is_online=True, promote_this_auction=True
        )
        location = PickupLocation.objects.create(
            name="Shop", auction=auction, pickup_time=self.start, address="1 Main St"
        )
        self.assertTrue(ClubEvent.objects.filter(pickup_location=location).exists())
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            self.assertEqual(discord_events.sync_club_events(self.club), 0)
        create.assert_not_called()

    def test_an_edited_event_is_moved_rather_than_left_at_its_old_time(self):
        event = ClubEvent.objects.create(
            club=self.club, title="Meeting", date_start=self.start, discord_event_id="d-1", needs_discord_sync=True
        )
        with patch.object(discord_events, "_patch_scheduled_event", return_value=200) as patched:
            self.assertEqual(discord_events.sync_club_events(self.club), 1)
        self.assertEqual(patched.call_args.args[1], "d-1")
        event.refresh_from_db()
        self.assertFalse(event.needs_discord_sync)

    def test_an_event_deleted_in_discord_is_made_again(self):
        event = ClubEvent.objects.create(
            club=self.club, title="Meeting", date_start=self.start, discord_event_id="d-1", needs_discord_sync=True
        )
        with (
            patch.object(discord_events, "_patch_scheduled_event", return_value=404),
            patch.object(discord_events, "create_scheduled_event", return_value="d-2") as create,
        ):
            discord_events.sync_club_events(self.club)
        create.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.discord_event_id, "d-2")

    def test_cancelling_an_event_takes_it_out_of_discord(self):
        event = ClubEvent.objects.create(
            club=self.club, title="Meeting", date_start=self.start, discord_event_id="d-1", cancelled=True
        )
        with patch.object(discord_events, "_delete_scheduled_event", return_value=204) as delete:
            self.assertEqual(discord_events.sync_club_events(self.club), 1)
        delete.assert_called_once_with("guild-1", "d-1")
        event.refresh_from_db()
        self.assertEqual(event.discord_event_id, "")

    def test_a_cancellation_discord_refused_is_not_retried_every_run(self):
        ClubEvent.objects.create(
            club=self.club, title="Meeting", date_start=self.start, discord_event_id="d-1", cancelled=True
        )
        with patch.object(discord_events, "_delete_scheduled_event", return_value=403):
            discord_events.sync_club_events(self.club)
        with patch.object(discord_events, "_delete_scheduled_event", return_value=403) as delete:
            discord_events.sync_club_events(self.club)
        delete.assert_not_called()

    def test_but_a_cancellation_discord_never_heard_is(self):
        ClubEvent.objects.create(
            club=self.club, title="Meeting", date_start=self.start, discord_event_id="d-1", cancelled=True
        )
        with patch.object(discord_events, "_delete_scheduled_event", return_value=0):
            discord_events.sync_club_events(self.club)
        with patch.object(discord_events, "_delete_scheduled_event", return_value=204) as delete:
            discord_events.sync_club_events(self.club)
        delete.assert_called_once()

    def test_turning_the_feature_off_takes_back_the_events_it_made(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, discord_event_id="d-1")
        self.club.create_discord_events_for_club_events = False
        self.club.save()
        with patch.object(discord_events, "_delete_scheduled_event", return_value=204) as delete:
            self.assertEqual(discord_events.sync_club_events(self.club), 1)
        delete.assert_called_once_with("guild-1", "d-1")
        event.refresh_from_db()
        self.assertEqual(event.discord_event_id, "")

    def test_past_events_are_skipped(self):
        """Discord rejects a scheduled event that starts in the past."""
        ClubEvent.objects.create(club=self.club, title="Old", date_start=timezone.now() - datetime.timedelta(days=1))
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            discord_events.sync_club_events(self.club)
        create.assert_not_called()

    def test_a_failure_is_not_retried_forever(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(discord_events, "create_scheduled_event", return_value=None):
            discord_events.sync_club_events(self.club)
        event.refresh_from_db()
        self.assertFalse(event.needs_discord_sync)
        self.assertEqual(event.discord_event_id, "")
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            discord_events.sync_club_events(self.club)
        create.assert_not_called()

    def test_but_editing_the_event_earns_it_another_try(self):
        """A permanent failure shouldn't retry every 15 minutes; a fixed one shouldn't be stuck."""
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(discord_events, "create_scheduled_event", return_value=None):
            discord_events.sync_club_events(self.club)
        event.title = "Meeting, moved"
        event.needs_discord_sync = True
        event.save()
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            discord_events.sync_club_events(self.club)
        create.assert_called_once()

    def test_the_feature_can_be_turned_off(self):
        self.club.create_discord_events_for_club_events = False
        self.club.save()
        ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(discord_events, "create_scheduled_event") as create:
            self.assertEqual(discord_events.sync_club_events(self.club), 0)
        create.assert_not_called()

    def test_a_club_with_no_discord_server_is_skipped(self):
        self.club.discord_server_id = ""
        self.club.save()
        ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(discord_events, "create_scheduled_event") as create:
            self.assertEqual(discord_events.sync_club_events(self.club), 0)
        create.assert_not_called()

    def test_an_event_with_no_location_still_gets_one(self):
        """Discord refuses an external event without a location string."""
        ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start)
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            discord_events.sync_club_events(self.club)
        self.assertTrue(create.call_args.kwargs["location"])


@override_settings(DISCORD_BOT_TOKEN="bot-token")
class AuctionDiscordEventTests(TestCase):
    """The auction_emails path still works after moving its Discord helper into discord_events."""

    def test_the_auction_helper_delegates_to_the_shared_module(self):
        from auctions.management.commands.auction_emails import _create_discord_scheduled_event

        start = timezone.now() + datetime.timedelta(days=2)
        with patch.object(discord_events, "create_scheduled_event", return_value="d-9") as create:
            ok = _create_discord_scheduled_event(
                guild_id="guild-1",
                name="Spring Auction",
                start_time=start,
                end_time=start + datetime.timedelta(hours=2),
                location_url="https://example.com/auction",
            )
        self.assertTrue(ok)
        self.assertEqual(create.call_args.kwargs["location"], "https://example.com/auction")
        self.assertEqual(create.call_args.kwargs["name"], "Spring Auction")

    def test_a_failed_creation_reports_false(self):
        from auctions.management.commands.auction_emails import _create_discord_scheduled_event

        start = timezone.now() + datetime.timedelta(days=2)
        with patch.object(discord_events, "create_scheduled_event", return_value=None):
            ok = _create_discord_scheduled_event("guild-1", "A", start, start, "url")
        self.assertFalse(ok)

    def test_a_long_title_is_truncated_to_discords_limit(self):
        """Discord rejects a scheduled event whose name is over 100 characters."""

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"id": "d-1"}

        start = timezone.now() + datetime.timedelta(days=2)
        with patch.object(discord_events.requests, "request", return_value=FakeResponse()) as request:
            discord_events.create_scheduled_event("guild-1", "x" * 250, start, start, "somewhere")
        self.assertEqual(len(request.call_args.kwargs["json"]["name"]), 100)


class NextEventInMemberEmailTests(TestCase):
    """Welcome/renewal/expiration emails advertise the club's next calendar event, not just
    its next auction."""

    def setUp(self):
        self.club = Club.objects.create(name="Email Club")
        self.site = Site.objects.get_current()
        self.start = timezone.now() + datetime.timedelta(days=5)

    def _fragment(self, **kwargs):
        from auctions.tasks import next_event_fragment

        return next_event_fragment(self.club, self.site, **kwargs)

    def test_no_events_produces_nothing(self):
        self.assertEqual(self._fragment(), ("", ""))

    def test_a_meeting_is_advertised(self):
        """The whole point of the change — a club with no auction still has something to say."""
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        text, html = self._fragment()
        self.assertIn("Our next event is Monthly Meeting", text)
        self.assertIn("Monthly Meeting", html)
        self.assertIn("See the details", text)

    def test_a_meeting_includes_its_start_time(self):
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        text, _ = self._fragment()
        self.assertIn(f"{self.start:%-I:%M %p}", text)

    def test_an_auction_shows_the_date_without_a_time(self):
        """An online auction spans days, so a start time next to the date is just noise."""
        Auction.objects.create(
            title="Spring Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(days=2),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        text, _ = self._fragment()
        self.assertIn("Our next event is Spring Auction", text)
        self.assertIn(f"{self.start:%B %-d, %Y}", text)
        self.assertNotIn(f"{self.start:%-I:%M %p}", text)
        self.assertIn("Read the auction's rules", text)

    def test_the_soonest_event_wins(self):
        ClubEvent.objects.create(
            club=self.club, title="Later Meeting", date_start=self.start + datetime.timedelta(days=10)
        )
        ClubEvent.objects.create(club=self.club, title="Sooner Meeting", date_start=self.start)
        text, _ = self._fragment()
        self.assertIn("Sooner Meeting", text)
        self.assertNotIn("Later Meeting", text)

    def test_pickup_events_are_never_advertised(self):
        """A pickup is logistics for people who already won lots, not an invitation."""
        auction = Auction.objects.create(
            title="Spring Auction",
            date_start=self.start + datetime.timedelta(days=10),
            date_end=self.start + datetime.timedelta(days=12),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        PickupLocation.objects.create(auction=auction, name="Clubhouse", address="1 Fish Lane", pickup_time=self.start)
        text, _ = self._fragment()
        self.assertNotIn("pickup", text.lower())
        self.assertIn("Spring Auction", text)

    def test_cancelled_and_deleted_events_are_skipped(self):
        ClubEvent.objects.create(club=self.club, title="Called Off", date_start=self.start, cancelled=True)
        ClubEvent.objects.create(
            club=self.club, title="Removed", date_start=self.start + datetime.timedelta(hours=1), is_deleted=True
        )
        ClubEvent.objects.create(club=self.club, title="Real One", date_start=self.start + datetime.timedelta(days=1))
        text, _ = self._fragment()
        self.assertIn("Real One", text)

    def test_past_events_are_skipped(self):
        ClubEvent.objects.create(
            club=self.club,
            title="Last Month",
            date_start=timezone.now() - datetime.timedelta(days=30),
            date_end=timezone.now() - datetime.timedelta(days=30) + datetime.timedelta(hours=1),
        )
        self.assertEqual(self._fragment(), ("", ""))

    def test_another_clubs_events_are_not_used(self):
        other = Club.objects.create(name="Other Email Club")
        ClubEvent.objects.create(club=other, title="Their Meeting", date_start=self.start)
        self.assertEqual(self._fragment(), ("", ""))

    def test_turning_it_off_produces_nothing(self):
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        self.assertEqual(self._fragment(include_event=False), ("", ""))

    def test_the_calendar_link_rides_on_the_event_line(self):
        """A welcome email goes out once, so it is the only chance to get somebody subscribed —
        but a club that switched the next event off is saying "don't advertise what we're doing",
        and a subscribe link is that."""
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        text, html = self._fragment()
        self.assertIn("Add our calendar", text)
        self.assertIn(f"webcal://{self.site.domain}", text)
        self.assertIn("Add our calendar", html)
        off_text, off_html = self._fragment(include_event=False)
        self.assertNotIn("Add our calendar", off_text)
        self.assertNotIn("Add our calendar", off_html)

    def test_a_shared_google_calendar_is_the_link_instead(self):
        """Same rule as the club page's buttons: the club's own Google calendar when there is one,
        because it holds whatever an admin typed straight into it."""
        self.club.google_calendar_refresh_token = "token"
        self.club.google_calendar_id = "abc@group.calendar.google.com"
        self.club.google_calendar_is_public = True
        self.club.save()
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        text, _ = self._fragment()
        self.assertIn("calendar.google.com", text)
        self.assertNotIn("webcal://", text)

    def test_the_preview_has_no_working_calendar_link(self):
        """as_links=False is the settings-page preview — nothing in it should be clickable."""
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        _, html = self._fragment(as_links=False)
        self.assertIn("Add our calendar", html)
        self.assertNotIn("<a href", html)

    def test_a_location_becomes_a_directions_link(self):
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start, location="1 Fish Lane")
        text, html = self._fragment()
        self.assertIn("Get directions", text)
        self.assertIn("google.com/maps", html)

    def test_an_online_auction_falls_back_to_its_single_pickup_address(self):
        """The auction event carries no location of its own, but members still want directions."""
        auction = Auction.objects.create(
            title="Spring Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(days=2),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        PickupLocation.objects.create(
            auction=auction,
            name="Clubhouse",
            address="1 Fish Lane",
            latitude=42.0,
            longitude=-71.0,
            pickup_time=self.start + datetime.timedelta(days=3),
        )
        text, _ = self._fragment()
        self.assertIn("Get directions", text)

    def test_no_directions_when_an_auction_has_several_locations(self):
        """One 'Get directions' link across two locations would send half the club to the wrong
        place. Only offer it when there is exactly one."""
        auction = Auction.objects.create(
            title="Spring Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(days=2),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        PickupLocation.objects.create(
            auction=auction,
            name="North",
            address="1 North St",
            latitude=42.0,
            longitude=-71.0,
            pickup_time=self.start + datetime.timedelta(days=3),
        )
        # Second location has an address but no coordinates, so no directions_link of its own.
        PickupLocation.objects.create(
            auction=auction,
            name="South",
            address="2 South St",
            pickup_time=self.start + datetime.timedelta(days=3),
        )
        text, _ = self._fragment()
        self.assertNotIn("Get directions", text)

    def test_an_in_person_auction_shows_its_pickup_time(self):
        """date_start is only 'when bidding opens'; the pickup location's time is when members
        are actually expected to turn up."""
        auction = Auction.objects.create(
            title="Fall Auction",
            date_start=self.start,
            date_end=None,
            club=self.club,
            is_online=False,
            promote_this_auction=True,
        )
        gather = (self.start + datetime.timedelta(days=1)).replace(hour=19, minute=30)
        PickupLocation.objects.create(auction=auction, name="Clubhouse", address="1 Fish Lane", pickup_time=gather)
        text, _ = self._fragment()
        self.assertIn("Fall Auction", text)
        self.assertIn(f"{gather:%-I:%M %p}", text)
        self.assertIn(f"{gather:%B %-d, %Y}", text)

    def test_an_in_person_auction_with_several_locations_falls_back_to_its_own_time(self):
        auction = Auction.objects.create(
            title="Fall Auction",
            date_start=self.start,
            date_end=None,
            club=self.club,
            is_online=False,
            promote_this_auction=True,
        )
        for name in ("North", "South"):
            PickupLocation.objects.create(
                auction=auction, name=name, address=f"{name} St", pickup_time=self.start + datetime.timedelta(days=1)
            )
        text, _ = self._fragment()
        self.assertIn(f"{self.start:%-I:%M %p}", text)

    def test_a_placeholder_location_does_not_suppress_directions(self):
        """Switching an auction to in-person auto-creates an address-less location; counting it
        would wrongly look like 'several locations'."""
        auction = Auction.objects.create(
            title="Fall Auction",
            date_start=self.start,
            date_end=self.start + datetime.timedelta(days=2),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        auction.is_online = False
        auction.save()
        PickupLocation.objects.create(
            auction=auction,
            name="Clubhouse",
            address="1 Fish Lane",
            latitude=42.0,
            longitude=-71.0,
            pickup_time=self.start + datetime.timedelta(days=1),
        )
        self.assertGreater(auction.physical_location_qs.count(), 1)
        text, _ = self._fragment()
        self.assertIn("Get directions", text)

    def test_the_preview_renders_spans_instead_of_working_links(self):
        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start, location="1 Fish Lane")
        _, html = self._fragment(as_links=False)
        self.assertNotIn("<a href", html)
        self.assertIn("<span class='text-info'>", html)

    def test_titles_are_escaped_in_the_html(self):
        ClubEvent.objects.create(club=self.club, title="Fish <script>alert(1)</script>", date_start=self.start)
        _, html = self._fragment()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_member_email_includes_the_event(self):
        from auctions.tasks import send_club_member_email

        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        self.club.welcome_include_auction = True
        self.club.save()
        member = ClubMember.objects.create(club=self.club, name="Pat", email="pat@example.com")
        with patch("auctions.tasks.mail.send") as send:
            send_club_member_email(member, "Welcome", "You're in.", email_type="welcome")
        self.assertTrue(send.called)
        body = send.call_args.kwargs.get("message", "") + send.call_args.kwargs.get("html_message", "")
        self.assertIn("Monthly Meeting", body)

    def test_the_member_email_omits_the_event_when_turned_off(self):
        from auctions.tasks import send_club_member_email

        ClubEvent.objects.create(club=self.club, title="Monthly Meeting", date_start=self.start)
        self.club.welcome_include_auction = False
        self.club.save()
        member = ClubMember.objects.create(club=self.club, name="Pat", email="pat@example.com")
        with patch("auctions.tasks.mail.send") as send:
            send_club_member_email(member, "Welcome", "You're in.", email_type="welcome")
        body = send.call_args.kwargs.get("message", "") + send.call_args.kwargs.get("html_message", "")
        self.assertNotIn("Monthly Meeting", body)


@override_settings(GOOGLE_CALENDAR_CLIENT_ID="cid", GOOGLE_CALENDAR_CLIENT_SECRET="secret")
class GoogleCalendarPullSafetyTests(TestCase):
    """The pull is the half that can quietly destroy data, so these are its guard rails."""

    def setUp(self):
        self.club = Club.objects.create(name="Pull Club")
        self.club.google_calendar_refresh_token = "refresh"
        self.club.google_calendar_id = "cal-1"
        self.club.save()
        self.start = timezone.now() + datetime.timedelta(days=3)

    def _item(self, **overrides):
        item = {
            "id": "g-1",
            "status": "confirmed",
            "summary": "Board meeting",
            "description": "",
            "location": "",
            "start": {"dateTime": self.start.isoformat()},
            "end": {"dateTime": (self.start + datetime.timedelta(hours=1)).isoformat()},
        }
        item.update(overrides)
        return item

    def test_the_first_pull_asks_for_a_bounded_window(self):
        """No timeMax means one never-ending weekly meeting expands into an instance per week,
        for ever, and every one of them becomes a club event."""
        with patch.object(gcal, "_request", return_value={"nextSyncToken": "t"}) as request:
            gcal.pull_events(self.club)
        params = request.call_args.kwargs["params"]
        self.assertIn("timeMin", params)
        self.assertIn("timeMax", params)

    def test_every_page_of_a_listing_carries_the_same_query(self):
        """Page two dropping the query reverts to Google's defaults — deletions hidden, and a
        window that no longer matches the first page's."""
        pages = [
            {"items": [], "nextPageToken": "page-2"},
            {"items": [], "nextSyncToken": "token-2"},
        ]
        with patch.object(gcal, "_request", side_effect=pages) as request:
            gcal.pull_events(self.club)
        first_params = request.call_args_list[0].kwargs["params"]
        second_params = request.call_args_list[1].kwargs["params"]
        self.assertEqual(second_params["pageToken"], "page-2")
        self.assertEqual(second_params["showDeleted"], "true")
        self.assertEqual({key: value for key, value in second_params.items() if key != "pageToken"}, first_params)

    def test_pagination_cannot_spin_forever(self):
        """A page token that never advances would otherwise be an endless loop."""
        page = {"items": [], "nextPageToken": "same-token-every-time"}
        with patch.object(gcal, "_request", return_value=page) as request:
            gcal.pull_events(self.club)
        self.assertEqual(request.call_count, gcal.MAX_PULL_PAGES)

    def test_an_unpushed_local_edit_is_not_overwritten_by_the_pull(self):
        """push_pending runs first but can fail; the pull that follows must not then replace the
        admin's edit with the stale copy from Google and clear the flag that would retry it."""
        event = ClubEvent.objects.create(
            club=self.club,
            title="Renamed here",
            date_start=self.start,
            google_event_id="g-1",
            needs_google_sync=True,
        )
        with patch.object(gcal, "_request", return_value={"items": [self._item()], "nextSyncToken": "t"}):
            gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertEqual(event.title, "Renamed here")
        self.assertTrue(event.needs_google_sync)

    def test_a_pickup_event_deleted_in_google_comes_back(self):
        """It's generated from the auction's pickup time, so dropping it would leave the club
        page disagreeing with the auction — and the next sync would recreate it anyway."""
        auction = Auction.objects.create(
            title="Spring Auction", date_start=self.start, club=self.club, is_online=True, promote_this_auction=True
        )
        PickupLocation.objects.create(name="Shop", auction=auction, pickup_time=self.start, address="1 Main St")
        event = ClubEvent.objects.get(source=ClubEvent.SOURCE_PICKUP)
        event.google_event_id = "g-1"
        event.save()
        with patch.object(
            gcal, "_request", return_value={"items": [self._item(status="cancelled")], "nextSyncToken": "t"}
        ):
            gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertFalse(event.is_deleted)
        self.assertEqual(event.google_event_id, "")
        self.assertTrue(event.needs_google_sync)

    def test_a_google_edit_never_overwrites_a_pickup_event(self):
        auction = Auction.objects.create(
            title="Spring Auction", date_start=self.start, club=self.club, is_online=True, promote_this_auction=True
        )
        PickupLocation.objects.create(name="Shop", auction=auction, pickup_time=self.start, address="1 Main St")
        event = ClubEvent.objects.get(source=ClubEvent.SOURCE_PICKUP)
        event.google_event_id = "g-1"
        event.needs_google_sync = False
        event.save()
        original_title = event.title
        with patch.object(gcal, "_request", return_value={"items": [self._item()], "nextSyncToken": "t"}):
            gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertEqual(event.title, original_title)

    def test_a_copy_of_one_of_our_events_does_not_steal_its_id(self):
        """Google copies extendedProperties into duplicates and into each instance of a series;
        claiming one would repoint us at the copy and orphan the original."""
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        item = self._item(
            id="g-1_20260801T000000Z", extendedProperties={"private": {"auctionSiteEventUuid": str(event.uuid)}}
        )
        with patch.object(gcal, "_request", return_value={"items": [item], "nextSyncToken": "t"}):
            created, _updated, _deleted = gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertEqual(event.google_event_id, "g-1")
        self.assertEqual(created, 0)
        self.assertEqual(ClubEvent.objects.filter(club=self.club).count(), 1)

    def test_a_pulled_event_tells_discord_about_the_change(self):
        """This is the only place that would ever hear about an edit made in Google Calendar."""
        event = ClubEvent.objects.create(
            club=self.club,
            title="Old name",
            date_start=self.start,
            google_event_id="g-1",
            needs_google_sync=False,
            needs_discord_sync=False,
        )
        with patch.object(gcal, "_request", return_value={"items": [self._item()], "nextSyncToken": "t"}):
            gcal.pull_events(self.club)
        event.refresh_from_db()
        self.assertEqual(event.title, "Board meeting")
        self.assertTrue(event.needs_discord_sync)

    def test_an_all_day_event_stays_all_day_when_it_goes_back(self):
        """Sending a datetime would turn the club's all-day event into a timed one."""
        item = self._item(start={"date": "2026-08-01"}, end={"date": "2026-08-02"})
        with patch.object(gcal, "_request", return_value={"items": [item], "nextSyncToken": "t"}):
            gcal.pull_events(self.club)
        event = ClubEvent.objects.get(google_event_id="g-1")
        self.assertTrue(event.all_day)
        body = gcal._event_body(event)
        self.assertEqual(body["start"], {"date": "2026-08-01"})
        self.assertEqual(body["end"], {"date": "2026-08-02"})

    def test_last_sync_means_a_round_trip_that_worked(self):
        with (
            patch.object(gcal, "_request", return_value={"nextSyncToken": "t"}),
            patch.object(gcal, "refresh_public_flag") as public,
        ):
            gcal.sync_club(self.club)
        self.club.refresh_from_db()
        self.assertIsNotNone(self.club.google_calendar_last_sync)
        # Every round trip that worked also re-reads whether the calendar is shared — that is the
        # only thing keeping the club page's Google links honest, and nothing else calls it.
        public.assert_called_once_with(self.club)

    def test_an_expired_token_does_not_look_like_a_successful_sync(self):
        self.club.google_calendar_sync_token = "stale"
        self.club.save()
        with patch.object(gcal, "_request", return_value=gcal.SYNC_TOKEN_GONE):
            gcal.pull_events(self.club)
        self.club.refresh_from_db()
        self.assertEqual(self.club.google_calendar_sync_token, "")
        self.assertIsNone(self.club.google_calendar_last_sync)


class RecurrenceRuleTests(TestCase):
    """The rule reader itself. Everything else trusts these answers."""

    def setUp(self):
        self.anchor = datetime.datetime(2026, 7, 7, 19, 0, tzinfo=datetime.timezone.utc)
        self.hour = datetime.timedelta(hours=1)

    def test_the_next_occurrence_of_a_weekly_series(self):
        nxt = recurrence.current_or_next(
            self.anchor,
            ["RRULE:FREQ=WEEKLY;BYDAY=TU"],
            self.hour,
            datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(nxt, datetime.datetime(2026, 7, 14, 19, 0, tzinfo=datetime.timezone.utc))

    def test_an_occurrence_under_way_beats_the_one_after_it(self):
        """ "Our next event" shouldn't skip past the meeting happening this evening."""
        during = datetime.datetime(2026, 7, 7, 19, 30, tzinfo=datetime.timezone.utc)
        nxt = recurrence.current_or_next(self.anchor, ["RRULE:FREQ=WEEKLY;BYDAY=TU"], self.hour, during)
        self.assertEqual(nxt, self.anchor)

    def test_a_finished_series_keeps_its_last_occurrence(self):
        """So it settles into the club page's recent history instead of vanishing."""
        lines = ["RRULE:FREQ=WEEKLY;COUNT=2;BYDAY=TU"]
        after = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
        nxt = recurrence.current_or_next(self.anchor, lines, self.hour, after)
        self.assertEqual(nxt, datetime.datetime(2026, 7, 14, 19, 0, tzinfo=datetime.timezone.utc))

    def test_an_excluded_occurrence_is_skipped(self):
        lines = recurrence.with_exdate(
            ["RRULE:FREQ=WEEKLY;BYDAY=TU"], datetime.datetime(2026, 7, 14, 19, 0, tzinfo=datetime.timezone.utc)
        )
        nxt = recurrence.current_or_next(
            self.anchor, lines, self.hour, datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        )
        self.assertEqual(nxt, datetime.datetime(2026, 7, 21, 19, 0, tzinfo=datetime.timezone.utc))

    def test_excluding_the_same_occurrence_twice_changes_nothing(self):
        moment = datetime.datetime(2026, 7, 14, 19, 0, tzinfo=datetime.timezone.utc)
        once = recurrence.with_exdate(["RRULE:FREQ=WEEKLY;BYDAY=TU"], moment)
        self.assertEqual(recurrence.with_exdate(once, moment), once)

    def test_a_rule_google_sends_that_dateutil_cannot_read_is_survivable(self):
        """One strange repeat must not stop a club's calendar syncing."""
        self.assertIsNone(recurrence.current_or_next(self.anchor, ["RRULE:FREQ=NONSENSE"], self.hour, self.anchor))

    def test_rules_are_described_in_english(self):
        cases = {
            "RRULE:FREQ=WEEKLY": "Repeats weekly",
            "RRULE:FREQ=WEEKLY;INTERVAL=2": "Repeats every 2 weeks",
            "RRULE:FREQ=WEEKLY;BYDAY=TU": "Repeats weekly on Tuesday",
            "RRULE:FREQ=WEEKLY;BYDAY=TU,TH": "Repeats weekly on Tuesday and Thursday",
            "RRULE:FREQ=MONTHLY;BYDAY=1TU": "Repeats monthly on the first Tuesday",
            "RRULE:FREQ=MONTHLY;BYDAY=-1FR": "Repeats monthly on the last Friday",
            "RRULE:FREQ=YEARLY": "Repeats yearly",
        }
        for rule, expected in cases.items():
            self.assertEqual(recurrence.describe([rule]), expected, rule)

    def test_lines_we_do_not_understand_are_dropped(self):
        kept = recurrence.clean_lines(["RRULE:FREQ=WEEKLY", "", "SUMMARY:not a rule", "EXDATE:20260714T190000Z"])
        self.assertEqual(kept, ["RRULE:FREQ=WEEKLY", "EXDATE:20260714T190000Z"])


@override_settings(GOOGLE_CALENDAR_CLIENT_ID="cid", GOOGLE_CALENDAR_CLIENT_SECRET="secret")
class RecurringEventPullTests(TestCase):
    """A repeating event is read in as one event with a rule, not as fifty-two copies."""

    def setUp(self):
        self.club = Club.objects.create(name="Repeat Club", enable_club_page=True)
        self.club.google_calendar_refresh_token = "refresh"
        self.club.google_calendar_id = "cal-1"
        self.club.save()
        # A week ago plus a few hours, so "the next one" is unambiguously later today.
        self.anchor = (timezone.now() - datetime.timedelta(days=7) + datetime.timedelta(hours=5)).replace(microsecond=0)

    def _master(self, **overrides):
        item = {
            "id": "g-series",
            "status": "confirmed",
            "summary": "Monthly meeting",
            "start": {"dateTime": self.anchor.isoformat()},
            "end": {"dateTime": (self.anchor + datetime.timedelta(hours=2)).isoformat()},
            "recurrence": ["RRULE:FREQ=WEEKLY"],
        }
        item.update(overrides)
        return item

    def _pull(self, items):
        with patch.object(gcal, "_request", return_value={"items": items, "nextSyncToken": "t"}):
            return gcal.pull_events(self.club)

    def test_the_pull_no_longer_asks_google_to_expand_series(self):
        with patch.object(gcal, "_request", return_value={"nextSyncToken": "t"}) as request:
            gcal.pull_events(self.club)
        self.assertNotIn("singleEvents", request.call_args.kwargs["params"])

    def test_a_series_becomes_one_event_holding_the_rule(self):
        created, _updated, _deleted = self._pull([self._master()])
        self.assertEqual(created, 1)
        event = ClubEvent.objects.get(google_event_id="g-series")
        self.assertTrue(event.is_recurring)
        self.assertEqual(event.recurrence, "RRULE:FREQ=WEEKLY")
        self.assertEqual(event.recurrence_start, self.anchor)

    def test_it_shows_the_next_occurrence_not_the_first_one(self):
        """date_start is what the club page, the emails and Discord all read."""
        self._pull([self._master()])
        event = ClubEvent.objects.get(google_event_id="g-series")
        self.assertGreater(event.date_start, timezone.now())
        self.assertEqual(event.date_start, self.anchor + datetime.timedelta(days=7))
        self.assertEqual(event.date_end - event.date_start, datetime.timedelta(hours=2))

    def test_a_weekly_series_does_not_fill_the_club_page(self):
        self._pull([self._master()])
        upcoming, _past = club_events.upcoming_events(self.club)
        self.assertEqual(upcoming.count(), 1)

    def test_one_occurrence_cancelled_in_google_is_excluded_from_the_rule(self):
        self._pull([self._master()])
        event = ClubEvent.objects.get(google_event_id="g-series")
        cancelled_start = event.date_start
        instance = {
            "id": "g-series_20260811T190000Z",
            "status": "cancelled",
            "recurringEventId": "g-series",
            "originalStartTime": {"dateTime": cancelled_start.isoformat()},
        }
        self._pull([instance])
        event.refresh_from_db()
        self.assertIn("EXDATE", event.recurrence)
        # It moved on to the occurrence after the one that was called off.
        self.assertEqual(event.date_start, cancelled_start + datetime.timedelta(days=7))

    def test_one_occurrence_moved_in_google_becomes_its_own_event(self):
        self._pull([self._master()])
        event = ClubEvent.objects.get(google_event_id="g-series")
        moved_from = event.date_start
        moved_to = moved_from + datetime.timedelta(days=1)
        instance = {
            "id": "g-series_moved",
            "status": "confirmed",
            "summary": "Monthly meeting (moved)",
            "recurringEventId": "g-series",
            "originalStartTime": {"dateTime": moved_from.isoformat()},
            "start": {"dateTime": moved_to.isoformat()},
            "end": {"dateTime": (moved_to + datetime.timedelta(hours=2)).isoformat()},
        }
        self._pull([instance])
        moved = ClubEvent.objects.get(google_event_id="g-series_moved")
        self.assertEqual(moved.date_start, moved_to)
        self.assertFalse(moved.is_recurring)
        # ...and the series no longer generates the slot it came from, so it isn't listed twice.
        event.refresh_from_db()
        self.assertNotEqual(event.date_start, moved_from)

    def test_a_series_arriving_after_its_own_exception_still_lines_up(self):
        """Google doesn't promise an order; an instance is useless without its series."""
        instance = {
            "id": "g-series_20260811T190000Z",
            "status": "cancelled",
            "recurringEventId": "g-series",
            "originalStartTime": {"dateTime": (self.anchor + datetime.timedelta(days=7)).isoformat()},
        }
        self._pull([instance, self._master()])
        event = ClubEvent.objects.get(google_event_id="g-series")
        self.assertIn("EXDATE", event.recurrence)

    def test_editing_the_series_in_google_keeps_the_occurrences_called_off(self):
        """Google records those as separate instances, so its rule never mentions them."""
        self._pull([self._master()])
        event = ClubEvent.objects.get(google_event_id="g-series")
        self._pull(
            [
                {
                    "id": "g-series_x",
                    "status": "cancelled",
                    "recurringEventId": "g-series",
                    "originalStartTime": {"dateTime": event.date_start.isoformat()},
                }
            ]
        )
        self._pull([self._master(summary="Monthly meeting, renamed")])
        event.refresh_from_db()
        self.assertEqual(event.title, "Monthly meeting, renamed")
        self.assertIn("EXDATE", event.recurrence)

    def test_a_series_goes_back_to_google_anchored_where_it_started(self):
        """Pushing the occurrence we're showing would walk the series forward a week each time."""
        self._pull([self._master()])
        event = ClubEvent.objects.get(google_event_id="g-series")
        body = gcal._event_body(event)
        self.assertEqual(body["start"]["dateTime"], self.anchor.isoformat())
        self.assertEqual(body["recurrence"], ["RRULE:FREQ=WEEKLY"])

    def test_a_rule_we_cannot_read_leaves_a_plain_event(self):
        created, _updated, _deleted = self._pull([self._master(recurrence=["RRULE:FREQ=NONSENSE"])])
        self.assertEqual(created, 1)
        event = ClubEvent.objects.get(google_event_id="g-series")
        self.assertFalse(event.is_recurring)
        self.assertEqual(event.date_start, self.anchor)


class RecurringEventUpkeepTests(TestCase):
    """Keeping the stored occurrence current, and what the rest of the site does with it."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Upkeep Club", enable_club_page=True)
        # Two weeks ago plus a few hours, so the next occurrence is unambiguously later today.
        self.anchor = (timezone.now() - datetime.timedelta(days=14) + datetime.timedelta(hours=5)).replace(
            microsecond=0
        )
        self.event = ClubEvent.objects.create(
            club=self.club,
            title="Weekly meeting",
            date_start=self.anchor,
            date_end=self.anchor + datetime.timedelta(hours=2),
            recurrence="RRULE:FREQ=WEEKLY",
            recurrence_start=self.anchor,
            source=ClubEvent.SOURCE_GOOGLE,
        )

    def test_the_sync_moves_it_on_to_the_next_occurrence(self):
        self.assertEqual(club_events.refresh_recurring_events(self.club), 1)
        self.event.refresh_from_db()
        self.assertGreater(self.event.date_start, timezone.now())
        # Discord holds one date, so it has to be told.
        self.assertTrue(self.event.needs_discord_sync)

    def test_moving_it_on_keeps_the_length_of_the_meeting(self):
        club_events.refresh_recurring_events(self.club)
        self.event.refresh_from_db()
        self.assertEqual(self.event.date_end - self.event.date_start, datetime.timedelta(hours=2))

    def test_a_second_sync_changes_nothing(self):
        club_events.refresh_recurring_events(self.club)
        self.assertEqual(club_events.refresh_recurring_events(self.club), 0)

    def test_a_repeating_event_is_advertised_in_membership_emails(self):
        club_events.refresh_recurring_events(self.club)
        self.assertEqual(club_events.next_member_facing_event(self.club), self.event)

    def test_the_feed_hands_over_the_rule_not_one_copy(self):
        club_events.refresh_recurring_events(self.club)
        body = self.client.get(reverse("club_events_ical", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("RRULE:FREQ=WEEKLY", body)
        # DTSTART is the series anchor: the rule is measured from it.
        self.assertIn(f"DTSTART:{self.anchor.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)

    def test_the_club_page_says_it_repeats(self):
        club_events.refresh_recurring_events(self.club)
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Repeats weekly")

    def test_moving_one_occurrence_on_the_site_moves_the_series(self):
        club_events.refresh_recurring_events(self.club)
        self.event.refresh_from_db()
        admin = User.objects.create_user(username="repeatadmin", password="x", email="r@example.com")
        ClubMember.objects.create(
            club=self.club, user=admin, name="Repeat Admin", email="r@example.com", permission_admin=True
        )
        self.client.force_login(admin)
        moved = self.event.date_start + datetime.timedelta(hours=1)
        # What a browser posts: the time as the admin sees it, in their own timezone. This admin
        # has no user_timezone cookie, so _browser_timezone falls back to the site's zone and the
        # view parses in that -- render it the same way. Not timezone.localtime(), which reads the
        # thread-local zone a previous test's form left activated (see ClubEventTimezoneTests).
        site_time = moved.astimezone(zoneinfo.ZoneInfo(settings.TIME_ZONE))
        self.client.post(
            reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": self.event.pk}),
            {"title": "Weekly meeting", "date_start": site_time.strftime("%Y-%m-%d %H:%M:%S")},
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.recurrence_start, self.anchor + datetime.timedelta(hours=1))


@override_settings(GOOGLE_CALENDAR_CLIENT_ID="cid", GOOGLE_CALENDAR_CLIENT_SECRET="secret")
class GoogleCalendarPublicCheckTests(TestCase):
    """Whether the calendar is shared is read from Google, not asked of the admin.

    It used to be a checkbox they ticked after following the instructions, checked once at that
    moment. Both halves failed: a club that shared the calendar and never came back never got its
    links, and a club that later un-shared it kept advertising links that 404 for every member."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Public Club")
        self.club.google_calendar_refresh_token = "refresh"
        self.club.google_calendar_id = "cal-1"
        self.club.save()
        self.admin = User.objects.create_user(username="pubadmin", password="x", email="pub@example.com")
        ClubMember.objects.create(
            club=self.club, user=self.admin, name="Pub Admin", email="pub@example.com", permission_admin=True
        )
        self.url = reverse("club_google_calendar_config", kwargs={"slug": self.club.slug})

    class _Response:
        def __init__(self, status_code):
            self.status_code = status_code

    def test_a_shared_calendar_answers_its_own_ical_url(self):
        with patch.object(gcal.requests, "get", return_value=self._Response(200)):
            self.assertTrue(gcal.is_calendar_public(self.club))

    def test_a_private_one_does_not(self):
        with patch.object(gcal.requests, "get", return_value=self._Response(404)):
            self.assertFalse(gcal.is_calendar_public(self.club))

    def test_sharing_it_in_google_is_noticed_without_being_told(self):
        """The whole point: a club follows the steps in Google Calendar and nothing else is asked
        of them. Before this they had to come back here and tick a box, and most never did."""
        with patch.object(gcal.requests, "get", return_value=self._Response(200)):
            self.assertTrue(gcal.refresh_public_flag(self.club))
        self.club.refresh_from_db()
        self.assertTrue(self.club.google_calendar_is_public)
        self.assertIsNotNone(self.club.google_calendar_public_checked)

    def test_un_sharing_it_takes_the_links_away_again(self):
        """Nothing used to notice this, so a club that un-shared its calendar went on advertising
        a link that 404s for every member."""
        self.club.google_calendar_is_public = True
        self.club.save()
        with patch.object(gcal.requests, "get", return_value=self._Response(404)):
            self.assertTrue(gcal.refresh_public_flag(self.club))
        self.club.refresh_from_db()
        self.assertFalse(self.club.google_calendar_is_public)
        self.assertEqual(self.club.google_calendar_public_url, "")

    def test_being_unable_to_reach_google_changes_nothing(self):
        """A timeout is not evidence the calendar was un-shared, and treating it as one would take
        the links off the club page for an hour every time Google hiccups."""
        self.club.google_calendar_is_public = True
        self.club.save()
        with patch.object(gcal, "is_calendar_public", side_effect=gcal.GoogleCalendarError("offline")):
            self.assertFalse(gcal.refresh_public_flag(self.club))
        self.club.refresh_from_db()
        self.assertTrue(self.club.google_calendar_is_public)
        self.assertIsNone(self.club.google_calendar_public_checked)

    def test_the_check_is_rate_limited_but_sync_now_forces_it(self):
        """One anonymous GET is cheap, but not every 15 minutes for every club for ever."""
        self.club.google_calendar_public_checked = timezone.now()
        self.club.save()
        with patch.object(gcal.requests, "get", return_value=self._Response(200)) as get:
            gcal.refresh_public_flag(self.club)
        get.assert_not_called()
        with patch.object(gcal.requests, "get", return_value=self._Response(200)) as get:
            gcal.refresh_public_flag(self.club, force=True)
        get.assert_called_once()
        self.club.refresh_from_db()
        self.assertTrue(self.club.google_calendar_is_public)

    def test_disconnecting_forgets_that_it_was_public(self):
        """Reconnecting a different Google account gets a new, private calendar — a leftover flag
        would advertise it in the window before the next probe."""
        self.club.google_calendar_is_public = True
        self.club.google_calendar_public_checked = timezone.now()
        self.club.save()
        gcal.disconnect(self.club)
        self.club.refresh_from_db()
        self.assertFalse(self.club.google_calendar_is_public)
        self.assertIsNone(self.club.google_calendar_public_checked)


@override_settings(DISCORD_BOT_TOKEN="bot-token")
class AuctionDiscordEventLifecycleTests(TestCase):
    """auction_emails creates an auction's Discord event; before this it could never be changed
    again, so an auction that moved — or was called off — kept its original entry for ever."""

    def setUp(self):
        self.club = Club.objects.create(
            name="Auction Club", discord_server_id="guild-1", create_events_for_auctions=True
        )
        self.start = timezone.now() + datetime.timedelta(days=5)
        self.auction = Auction.objects.create(
            title="Spring Auction", date_start=self.start, club=self.club, promote_this_auction=True
        )
        Auction.objects.filter(pk=self.auction.pk).update(discord_event_id="d-1")
        self.auction.refresh_from_db()

    def test_moving_an_auction_queues_a_discord_update(self):
        self.auction.date_start = self.start + datetime.timedelta(days=1)
        self.auction.save()
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.discord_event_needs_update)

    def test_an_unrelated_save_does_not(self):
        self.auction.invoiced = True
        self.auction.save()
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.discord_event_needs_update)

    def test_the_queued_update_reaches_discord(self):
        self.auction.title = "Spring Auction, moved"
        self.auction.save()
        with patch.object(discord_events, "_patch_scheduled_event", return_value=200) as patched:
            discord_events.sync_club_events(self.club)
        self.assertEqual(patched.call_args.args[2], "Spring Auction, moved")
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.discord_event_needs_update)

    def test_unpromoting_an_auction_takes_its_event_out_of_discord(self):
        self.auction.promote_this_auction = False
        self.auction.save()
        with patch.object(discord_events, "_delete_scheduled_event", return_value=204) as delete:
            discord_events.sync_club_events(self.club)
        delete.assert_called_once_with("guild-1", "d-1")
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.discord_event_id, "")

    def test_deleting_an_auction_takes_its_event_out_of_discord(self):
        self.auction.is_deleted = True
        self.auction.save()
        with patch.object(discord_events, "_delete_scheduled_event", return_value=204) as delete:
            discord_events.sync_club_events(self.club)
        delete.assert_called_once()

    def test_an_event_deleted_in_discord_is_made_again(self):
        self.auction.title = "Spring Auction, moved"
        self.auction.save()
        with (
            patch.object(discord_events, "_patch_scheduled_event", return_value=404),
            patch.object(discord_events, "create_scheduled_event", return_value="d-2") as create,
        ):
            discord_events.sync_club_events(self.club)
        create.assert_called_once()
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.discord_event_id, "d-2")

    def test_turning_auction_events_off_takes_back_what_it_made(self):
        self.club.create_events_for_auctions = False
        self.club.save()
        with patch.object(discord_events, "_delete_scheduled_event", return_value=204) as delete:
            discord_events.sync_club_events(self.club)
        delete.assert_called_once()
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.discord_event_id, "")

    def test_a_refusal_is_not_retried_every_run(self):
        self.auction.promote_this_auction = False
        self.auction.save()
        with patch.object(discord_events, "_delete_scheduled_event", return_value=403):
            discord_events.sync_club_events(self.club)
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.discord_event_needs_update)

    def test_but_an_unreachable_discord_is(self):
        self.auction.promote_this_auction = False
        self.auction.save()
        with patch.object(discord_events, "_delete_scheduled_event", return_value=0):
            discord_events.sync_club_events(self.club)
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.discord_event_needs_update)

    def test_auction_emails_records_the_id_it_creates(self):
        from auctions.management.commands.auction_emails import Command

        auction = Auction.objects.create(
            title="Summer Auction", date_start=self.start, club=self.club, promote_this_auction=True
        )
        Auction.objects.filter(pk=auction.pk).update(date_posted=timezone.now() - datetime.timedelta(days=2))
        with patch.object(discord_events, "create_scheduled_event", return_value="d-9"):
            Command()._create_discord_events(timezone.now(), "example.com")
        auction.refresh_from_db()
        self.assertEqual(auction.discord_event_id, "d-9")
        self.assertTrue(auction.discord_event_created)


@override_settings(DISCORD_BOT_TOKEN="bot-token", GOOGLE_CALENDAR_CLIENT_ID="cid", GOOGLE_CALENDAR_CLIENT_SECRET="s")
class CalendarCleanupOnDeleteTests(TestCase):
    """A row that cascades away takes no record of its Google and Discord copies with it, so
    they have to be removed while it's still here."""

    def setUp(self):
        self.club = Club.objects.create(name="Cleanup Club", discord_server_id="guild-1")
        self.club.google_calendar_refresh_token = "refresh"
        self.club.google_calendar_id = "cal-1"
        self.club.save()
        self.start = timezone.now() + datetime.timedelta(days=4)

    def test_hard_deleting_an_auction_removes_its_events_from_google(self):
        """Auction.delete() is a soft delete, but a queryset delete — which is what the Django
        admin does — really does cascade the calendar events away."""
        auction = Auction.objects.create(
            title="Doomed", date_start=self.start, club=self.club, promote_this_auction=True
        )
        event = ClubEvent.objects.get(auction=auction)
        event.google_event_id = "g-1"
        event.save()
        with patch.object(gcal, "delete_event", return_value=True) as delete:
            Auction.objects.filter(pk=auction.pk).delete()
        delete.assert_called()

    def test_hard_deleting_an_auction_removes_its_discord_event(self):
        auction = Auction.objects.create(
            title="Doomed", date_start=self.start, club=self.club, promote_this_auction=True
        )
        Auction.objects.filter(pk=auction.pk).update(discord_event_id="d-1")
        with patch.object(discord_events, "cancel_scheduled_event", return_value=True) as cancel:
            Auction.objects.filter(pk=auction.pk).delete()
        self.assertIn("d-1", [call.args[1] for call in cancel.call_args_list])

    def test_soft_deleting_an_auction_retires_its_event_instead(self):
        """The everyday path: the event is soft-deleted with the auction and its remote copies
        are cleaned up by the next sync, not by a cascade."""
        auction = Auction.objects.create(
            title="Doomed", date_start=self.start, club=self.club, promote_this_auction=True
        )
        event = ClubEvent.objects.get(auction=auction)
        event.google_event_id = "g-1"
        event.save()
        auction.delete()
        event.refresh_from_db()
        self.assertTrue(event.is_deleted)
        with patch.object(gcal, "delete_event", return_value=True) as delete:
            club_events.purge_retired(self.club)
        delete.assert_called()

    def test_deleting_a_club_takes_its_events_off_google(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        self.assertTrue(event.pk)
        with patch.object(gcal, "delete_event", return_value=True) as delete:
            self.club.delete()
        delete.assert_called()

    def test_an_event_that_comes_back_can_reach_discord_again(self):
        """Retiring it cancels the Discord event; leaving it marked "already tried" would mean a
        pickup time that's re-added never gets one."""
        event = ClubEvent.objects.create(
            club=self.club,
            title="Meeting",
            date_start=self.start,
            discord_event_id="d-1",
            needs_discord_sync=False,
        )
        with patch.object(discord_events, "cancel_scheduled_event", return_value=True):
            club_events.retire_event(event)
        event.refresh_from_db()
        self.assertEqual(event.discord_event_id, "")
        self.assertTrue(event.needs_discord_sync)


class ClubEventTimezoneTests(TestCase):
    """Every page renders inside {% timezone user_timezone %}, so a form shows an admin their own
    times. Parsing them back in the site's timezone shifted the event on every single save."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="TZ Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="tzadmin", password="x", email="tz@example.com")
        ClubMember.objects.create(
            club=self.club, user=self.admin, name="TZ Admin", email="tz@example.com", permission_admin=True
        )
        self.client.force_login(self.admin)
        self.client.cookies["user_timezone"] = "America/Los_Angeles"
        # ClubEventForm activates this zone and nothing deactivates it, so without this every test
        # that runs after one of these in the same process sees Los Angeles as the current zone.
        self.addCleanup(timezone.deactivate)

    def test_an_event_is_saved_at_the_time_the_admin_typed(self):
        import zoneinfo

        self.client.post(
            reverse("club_event_add", kwargs={"slug": self.club.slug}),
            {"title": "Evening meeting", "date_start": "2026-09-10 19:00:00"},
        )
        event = ClubEvent.objects.get(title="Evening meeting")
        local = event.date_start.astimezone(zoneinfo.ZoneInfo("America/Los_Angeles"))
        self.assertEqual((local.hour, local.minute), (19, 0))

    def test_saving_an_event_again_does_not_move_it(self):
        """The round trip is what actually bit: open, save, and the event slid by the offset."""
        self.client.post(
            reverse("club_event_add", kwargs={"slug": self.club.slug}),
            {"title": "Evening meeting", "date_start": "2026-09-10 19:00:00"},
        )
        event = ClubEvent.objects.get(title="Evening meeting")
        original = event.date_start
        edit_url = reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk})
        response = self.client.get(edit_url)
        shown = response.context["form"]["date_start"].value()
        self.client.post(edit_url, {"title": "Evening meeting", "date_start": shown})
        event.refresh_from_db()
        self.assertEqual(event.date_start, original)


class ClubEventCancellationTests(TestCase):
    """Deleting an event makes it vanish from every subscriber's calendar with no explanation;
    cancelling tells them."""

    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Cancel Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="canceladmin", password="x", email="c@example.com")
        ClubMember.objects.create(
            club=self.club, user=self.admin, name="Cancel Admin", email="c@example.com", permission_admin=True
        )
        self.client.force_login(self.admin)
        self.start = timezone.now() + datetime.timedelta(days=3)
        self.event = ClubEvent.objects.create(club=self.club, title="Monthly meeting", date_start=self.start)

    def test_an_admin_can_call_an_event_off(self):
        url = reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": self.event.pk})
        response = self.client.post(
            url,
            {
                "title": self.event.title,
                "date_start": self.event.date_start.strftime("%Y-%m-%d %H:%M:%S"),
                "cancelled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertTrue(self.event.cancelled)
        self.assertFalse(self.event.is_deleted)
        self.assertTrue(self.event.needs_google_sync)

    def test_the_add_form_has_no_cancelled_box(self):
        """You don't add an event in order to call it off."""
        response = self.client.get(reverse("club_event_add", kwargs={"slug": self.club.slug}))
        self.assertNotIn("cancelled", response.context["form"].fields)

    def test_google_is_told_it_is_off_rather_than_being_asked_to_delete_it(self):
        self.event.cancelled = True
        body = gcal._event_body(self.event)
        self.assertEqual(body["status"], "cancelled")

    def test_the_club_page_still_shows_it(self):
        self.event.cancelled = True
        self.event.save()
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Monthly meeting")
        self.assertContains(response, "Cancelled")


class SyncAllTests(TestCase):
    def test_one_broken_club_does_not_stop_the_others(self):
        good = Club.objects.create(name="Good Club", discord_server_id="g-1")
        bad = Club.objects.create(name="Bad Club", discord_server_id="g-2")
        start = timezone.now() + datetime.timedelta(days=1)
        ClubEvent.objects.create(club=good, title="Good", date_start=start)
        ClubEvent.objects.create(club=bad, title="Bad", date_start=start)
        calls = []

        def flaky(club):
            calls.append(club.pk)
            if club.pk == bad.pk:
                msg = "kaboom"
                raise RuntimeError(msg)

        with patch.object(club_events, "sync_club", side_effect=flaky):
            synced = club_events.sync_all()
        self.assertEqual(sorted(calls), sorted([good.pk, bad.pk]))
        self.assertEqual(synced, 1)

    def test_an_inactive_club_is_left_alone(self):
        club = Club.objects.create(name="Closed Club", active=False, discord_server_id="g-3")
        ClubEvent.objects.create(club=club, title="Old", date_start=timezone.now() + datetime.timedelta(days=1))
        with patch.object(club_events, "sync_club") as sync_club:
            club_events.sync_all()
        self.assertNotIn(club.pk, [call.args[0].pk for call in sync_club.call_args_list])

    def test_a_club_with_nothing_to_sync_is_skipped(self):
        """`discord_server_id__isnull=False` read like a filter but matched every club saved with
        the field left blank, because a blank CharField stores "" and not NULL."""
        Club.objects.create(name="Empty Club", discord_server_id="")
        with patch.object(club_events, "sync_club") as sync_club:
            club_events.sync_all()
        sync_club.assert_not_called()

    def test_the_celery_task_runs(self):
        from auctions.tasks import sync_club_calendars

        Club.objects.create(name="Task Club")
        with patch.object(club_events, "sync_all", return_value=0) as sync_all:
            sync_club_calendars()
        sync_all.assert_called_once()

    def test_two_runs_cannot_overlap(self):
        """Beat fires this every 15 minutes; a slow run would otherwise race the next one and
        push the same events twice, or provision two calendars for one club."""
        from django.core.cache import cache

        from auctions.tasks import CALENDAR_SYNC_LOCK_KEY, sync_club_calendars

        cache.delete(CALENDAR_SYNC_LOCK_KEY)
        started = []

        def slow():
            started.append(1)
            sync_club_calendars()  # the "second run", arriving while the first holds the lock
            return 0

        with patch.object(club_events, "sync_all", side_effect=slow):
            sync_club_calendars()
        self.assertEqual(len(started), 1)
        # The lock is released, so the next scheduled run still happens.
        self.assertIsNone(cache.get(CALENDAR_SYNC_LOCK_KEY))
