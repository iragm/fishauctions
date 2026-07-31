"""Tests for club events, Google Calendar sync, and the Discord events built on top of them."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions import club_events, discord_events
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

    def test_add_to_calendar_url_has_utc_times_and_the_title(self):
        event = ClubEvent.objects.create(club=self.club, title="Swap Meet", date_start=self.start)
        url = event.add_to_calendar_url
        self.assertIn("calendar.google.com", url)
        self.assertIn("Swap+Meet", url)
        self.assertIn(f"{self.start.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}", url)

    def test_auction_events_are_not_editable_by_hand(self):
        auction = Auction.objects.create(title="Auction", date_start=self.start, club=self.club)
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

    def test_two_locations_make_two_events(self):
        when = self.start + datetime.timedelta(days=3)
        self._location("North", pickup_time=when)
        self._location("South", pickup_time=when + datetime.timedelta(hours=2))
        self.assertEqual(self._pickups().count(), 2)
        self.assertEqual({e.location for e in self._pickups()}, {"North St", "South St"})

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

    def test_auction_events_cannot_be_edited_through_the_event_form(self):
        self.client.force_login(self.admin)
        auction = Auction.objects.create(title="Auction", date_start=self.start, club=self.club)
        event = ClubEvent.objects.get(auction=auction)
        response = self.client.get(reverse("club_event_edit", kwargs={"slug": self.club.slug, "pk": event.pk}))
        self.assertEqual(response.status_code, 404)

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
        auction = Auction.objects.create(title="Auction", date_start=self.start, club=self.club)
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
        auction = Auction.objects.create(title="Real Title", date_start=self.start, club=self.club)
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

    def test_disconnecting_clears_the_tokens_and_unlinks_events(self):
        event = ClubEvent.objects.create(club=self.club, title="Meeting", date_start=self.start, google_event_id="g-1")
        gcal.disconnect(self.club)
        self.club.refresh_from_db()
        event.refresh_from_db()
        self.assertFalse(self.club.google_calendar_connected)
        self.assertEqual(self.club.google_calendar_id, "")
        self.assertEqual(event.google_event_id, "")
        # The event itself survives — only the Google link is gone.
        self.assertFalse(event.is_deleted)

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
        self.assertFalse(self.club.google_calendar_is_public)

    def test_toggling_public_makes_no_google_call(self):
        """google_calendar_is_public is the admin telling us what they did in Google Calendar —
        it's a display flag, not something we can act on."""
        self.client.force_login(self.admin)
        with patch.object(gcal, "_request") as request:
            self.client.post(self.url, {"google_calendar_is_public": "on"})
        request.assert_not_called()
        self.club.refresh_from_db()
        self.assertTrue(self.club.google_calendar_is_public)

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
        session.save()
        with patch.object(gcal, "exchange_code") as exchange:
            response = self.client.get(reverse("google_calendar_callback"), {"code": "abc", "state": "wrong"})
        exchange.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertFalse(self.club.google_calendar_connected)

    def test_a_successful_callback_stores_the_tokens(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session["google_calendar_oauth_club_slug"] = self.club.slug
        session.save()
        state = self.admin.userdata.unsubscribe_link
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
        self.assertTrue(event.discord_event_attempted)

    def test_auction_events_are_skipped_so_they_are_never_doubled_up(self):
        """auction_emails owns Discord events for auctions; this path must stay out of the way."""
        Auction.objects.create(title="Auction", date_start=self.start, club=self.club)
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            self.assertEqual(discord_events.sync_club_events(self.club), 0)
        create.assert_not_called()

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
        self.assertTrue(event.discord_event_attempted)
        self.assertEqual(event.discord_event_id, "")
        with patch.object(discord_events, "create_scheduled_event", return_value="d-1") as create:
            discord_events.sync_club_events(self.club)
        create.assert_not_called()

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
        with patch.object(discord_events.requests, "post", return_value=FakeResponse()) as post:
            discord_events.create_scheduled_event("guild-1", "x" * 250, start, start, "somewhere")
        self.assertEqual(len(post.call_args.kwargs["json"]["name"]), 100)


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
        )
        gather = (self.start + datetime.timedelta(days=1)).replace(hour=19, minute=30)
        PickupLocation.objects.create(auction=auction, name="Clubhouse", address="1 Fish Lane", pickup_time=gather)
        text, _ = self._fragment()
        self.assertIn("Fall Auction", text)
        self.assertIn(f"{gather:%-I:%M %p}", text)
        self.assertIn(f"{gather:%B %-d, %Y}", text)

    def test_an_in_person_auction_with_several_locations_falls_back_to_its_own_time(self):
        auction = Auction.objects.create(
            title="Fall Auction", date_start=self.start, date_end=None, club=self.club, is_online=False
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

    def test_the_celery_task_runs(self):
        from auctions.tasks import sync_club_calendars

        Club.objects.create(name="Task Club")
        with patch.object(club_events, "sync_all", return_value=0) as sync_all:
            sync_club_calendars()
        sync_all.assert_called_once()
