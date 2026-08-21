"""Tests for club announcements, the website-integration snippets, and the embeds behind them.

The announcement feature is three deliveries with three failure modes, so the tests below are
mostly about keeping them apart: a Discord outage must not cost the club the push, a push must not
turn into a surprise email, and only the announcements ticked "show on website" may ever reach the
public page or the embed.
"""

import datetime
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions import announcements, club_events
from auctions.forms import ClubAnnouncementForm
from auctions.models import (
    Auction,
    Club,
    ClubAnnouncement,
    ClubEvent,
    ClubHistory,
    ClubMember,
    MobileDevice,
)
from auctions.views import DiscordInteractionsView


class ClubEventWhenDisplayTests(TestCase):
    """An online auction runs for days; printing only the end *time* said it ran for an evening."""

    def setUp(self):
        self.club = Club.objects.create(name="When Club")
        self.tz = timezone.get_current_timezone()

    def _event(self, **kwargs):
        return ClubEvent(club=self.club, title="Thing", **kwargs)

    def test_no_end_time_shows_only_the_start(self):
        start = datetime.datetime(2026, 8, 1, 19, 0, tzinfo=self.tz)
        text = self._event(date_start=start).when_display
        self.assertIn("Aug. 1, 2026", text)
        self.assertIn("7:00 PM", text)
        self.assertNotIn("–", text)

    def test_an_end_on_the_same_day_shows_only_the_end_time(self):
        start = datetime.datetime(2026, 8, 1, 19, 0, tzinfo=self.tz)
        text = self._event(date_start=start, date_end=start + datetime.timedelta(hours=2)).when_display
        self.assertIn("7:00 PM – 9:00 PM", text)
        self.assertEqual(text.count("Aug. 1, 2026"), 1)

    def test_an_end_on_another_day_repeats_the_date(self):
        start = datetime.datetime(2026, 8, 1, 19, 0, tzinfo=self.tz)
        text = self._event(date_start=start, date_end=start + datetime.timedelta(days=3)).when_display
        self.assertIn("Aug. 1, 2026", text)
        self.assertIn("Aug. 4, 2026", text)

    def test_an_end_before_the_start_is_ignored(self):
        start = datetime.datetime(2026, 8, 1, 19, 0, tzinfo=self.tz)
        text = self._event(date_start=start, date_end=start - datetime.timedelta(hours=1)).when_display
        self.assertNotIn("–", text)

    def test_an_all_day_event_says_all_day(self):
        day = datetime.datetime(2026, 8, 1, tzinfo=self.tz)
        text = self._event(date_start=day, date_end=day + datetime.timedelta(days=1), all_day=True).when_display
        self.assertIn("all day", text)
        self.assertEqual(text.count("Aug. 1"), 1)

    def test_a_multi_day_all_day_event_shows_the_last_day_not_the_exclusive_end(self):
        """Google and iCal write the end of an all-day event as the day *after* it finishes."""
        day = datetime.datetime(2026, 8, 1, tzinfo=self.tz)
        text = self._event(date_start=day, date_end=day + datetime.timedelta(days=3), all_day=True).when_display
        self.assertIn("Aug. 1", text)
        self.assertIn("Aug. 3, 2026", text)
        self.assertNotIn("Aug. 4", text)

    def test_a_multi_day_online_auction_reads_as_multi_day_on_the_club_page(self):
        start = timezone.now() + datetime.timedelta(days=2)
        auction = Auction.objects.create(
            title="Long Online Auction",
            date_start=start,
            date_end=start + datetime.timedelta(days=5),
            club=self.club,
            is_online=True,
            promote_this_auction=True,
        )
        event = ClubEvent.objects.filter(auction=auction).first()
        self.assertIsNotNone(event)
        end_local = timezone.localtime(event.date_end)
        self.assertIn(str(end_local.day), event.when_display)


class AuctionCalendarDescriptionTests(TestCase):
    """The lot-submission deadline used to land in every member's Google Calendar and Discord."""

    def test_the_description_no_longer_carries_the_lot_submission_deadline(self):
        club = Club.objects.create(name="Description Club")
        start = timezone.now() + datetime.timedelta(days=5)
        auction = Auction.objects.create(
            title="Auction",
            date_start=start,
            date_end=start + datetime.timedelta(days=1),
            club=club,
            is_online=True,
            lot_submission_end_date=start - datetime.timedelta(days=1),
        )
        description = club_events._auction_description(auction)
        self.assertEqual(description, "Online auction with in-person pickup.")
        self.assertNotIn("Lot submission", description)


class AnnouncementFormTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Form Club")

    def test_an_announcement_with_no_channel_is_refused(self):
        form = ClubAnnouncementForm(
            {"text": "Hello", "send_to_discord": False, "send_to_push": False, "show_on_website": False},
            club=self.club,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("at least one place", str(form.errors))

    def test_a_very_long_announcement_is_refused_server_side(self):
        """The maxlength attribute is the browser's cap; nothing enforces it on a POST."""
        form = ClubAnnouncementForm(
            {"text": "x" * (announcements.MAX_LENGTH + 1), "show_on_website": True}, club=self.club
        )
        self.assertFalse(form.is_valid())
        self.assertIn("characters", str(form.errors))

    def test_text_is_stripped(self):
        form = ClubAnnouncementForm({"text": "  spaced out  ", "show_on_website": True}, club=self.club)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["text"], "spaced out")

    def test_discord_is_switched_off_with_a_reason_when_no_server_is_connected(self):
        form = ClubAnnouncementForm(club=self.club)
        self.assertTrue(form.fields["send_to_discord"].disabled)
        self.assertIn("No Discord server", str(form.fields["send_to_discord"].help_text))

    def test_discord_is_switched_off_when_the_server_is_there_but_no_channel_is_set(self):
        self.club.discord_server_id = "guild-1"
        self.club.save()
        form = ClubAnnouncementForm(club=self.club)
        self.assertTrue(form.fields["send_to_discord"].disabled)
        self.assertIn("/announcements_here", str(form.fields["send_to_discord"].help_text))

    def test_discord_is_offered_once_a_channel_is_set(self):
        self.club.discord_server_id = "guild-1"
        self.club.announcement_channel_id = "chan-1"
        self.club.save()
        form = ClubAnnouncementForm(club=self.club)
        self.assertFalse(form.fields["send_to_discord"].disabled)

    def test_the_push_box_says_how_many_of_how_many_members_it_reaches(self):
        reachable = User.objects.create_user(username="reach", password="pw", email="r@example.com")
        MobileDevice.objects.create(user=reachable, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        ClubMember.objects.create(club=self.club, user=reachable, email="r@example.com")
        ClubMember.objects.create(club=self.club, email="paper@example.com")
        with patch("auctions.notifications.push_configured", return_value=True):
            form = ClubAnnouncementForm(club=self.club)
        self.assertIn("1 of your 2 members", str(form.fields["send_to_push"].help_text))
        self.assertFalse(form.fields["send_to_push"].disabled)

    def test_the_push_box_is_switched_off_when_nobody_has_the_app(self):
        ClubMember.objects.create(club=self.club, email="paper@example.com")
        form = ClubAnnouncementForm(club=self.club)
        self.assertTrue(form.fields["send_to_push"].disabled)
        self.assertIn("Nobody in your club has the app", str(form.fields["send_to_push"].help_text))


class AnnouncementReachTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Reach Club")

    def _member_with_app(self, username, **kwargs):
        user = User.objects.create_user(username=username, password="pw", email=f"{username}@example.com")
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        return ClubMember.objects.create(club=self.club, user=user, email=user.email, **kwargs)

    def test_do_not_contact_members_are_never_pushed_to(self):
        self._member_with_app("ok")
        self._member_with_app("nope", contact_status="do_not_contact")
        self.assertEqual([m.user.username for m in announcements.reachable_members(self.club)], ["ok"])

    def test_no_non_essential_emails_does_not_block_a_push(self):
        """That flag is about email; installing the app and leaving notifications on is opting in."""
        self._member_with_app("quiet", contact_status="non_essential")
        self.assertEqual(announcements.reachable_members(self.club).count(), 1)

    def test_a_member_with_no_account_or_no_token_is_not_reachable(self):
        ClubMember.objects.create(club=self.club, email="paper@example.com")
        user = User.objects.create_user(username="notoken", password="pw", email="n@example.com")
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="", push_enabled=True)
        ClubMember.objects.create(club=self.club, user=user, email=user.email)
        self.assertEqual(announcements.reachable_members(self.club).count(), 0)

    def test_a_member_with_two_phones_is_counted_once(self):
        member = self._member_with_app("twophones")
        MobileDevice.objects.create(user=member.user, device_uuid=uuid.uuid4(), fcm_token="tok2", push_enabled=True)
        self.assertEqual(announcements.reachable_members(self.club).count(), 1)

    def test_the_count_is_zero_when_push_is_not_configured_at_all(self):
        self._member_with_app("ok")
        with patch("auctions.notifications.push_configured", return_value=False):
            self.assertEqual(announcements.member_counts(self.club), (0, 1))


@override_settings(FIREBASE_CREDENTIALS_JSON="test-firebase-key")
class AnnouncementDeliveryTests(TestCase):
    """Push has to be *configured* for the form to accept the ticked box at all.

    With no FCM credentials ``member_counts()`` reports nobody reachable, ``ClubAnnouncementForm``
    disables ``send_to_push``, and a disabled field drops the submitted value -- so an undecorated
    test here measures whatever ``FIREBASE_CREDENTIALS_JSON`` happens to be in the environment, or
    whatever the last ``override_settings`` in the run left behind. It belongs on the class rather
    than on the two methods that happened to have it: every test in here posts the form.
    """

    def setUp(self):
        self.club = Club.objects.create(
            name="Delivery Club",
            enable_club_page=True,
            discord_server_id="guild-1",
            announcement_channel_id="chan-1",
        )
        self.admin = User.objects.create_user(username="ann_admin", password="pw", email="aa@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.client = Client()
        self.url = reverse("club_announcements", kwargs={"slug": self.club.slug})

    def _reachable_member(self, username="phone"):
        user = User.objects.create_user(username=username, password="pw", email=f"{username}@example.com")
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        return ClubMember.objects.create(club=self.club, user=user, email=user.email)

    def _grace_expires(self):
        """Run the send the way the queued task does, once the retract window has passed.

        Posting the form no longer delivers anything -- every announcement waits GRACE_SECONDS so
        it can be retracted -- so a test that wants to see what came out has to let the clock run.
        """
        return announcements.send_due(now=timezone.now() + datetime.timedelta(seconds=announcements.GRACE_SECONDS + 5))

    def test_nothing_leaves_the_building_during_the_retract_window(self):
        self._reachable_member()
        self.client.force_login(self.admin)
        with (
            patch("auctions.discord_events.send_channel_message") as discord,
            patch("auctions.tasks.send_push_to_user.delay") as push,
        ):
            self.client.post(
                self.url,
                {"text": "Wait for it", "send_to_discord": "on", "send_to_push": "on", "show_on_website": "on"},
            )
        discord.assert_not_called()
        push.assert_not_called()
        announcement = ClubAnnouncement.objects.get(club=self.club)
        self.assertIsNone(announcement.sent_at)
        self.assertTrue(announcement.is_in_grace_period)
        # ...and nothing public can see it yet either.
        self.assertEqual(announcements.latest_for_website(self.club), [])

    @override_settings(FIREBASE_CREDENTIALS_JSON="test-firebase-key")
    def test_posting_sends_to_every_ticked_channel_and_records_what_happened(self):
        # Without FCM credentials the form disables send_to_push and drops the ticked box, so this
        # would measure the machine's .env rather than the delivery. See the failed-Discord test.
        member = self._reachable_member()
        self.client.force_login(self.admin)
        with (
            patch("auctions.discord_events.send_channel_message", return_value="msg-1") as discord,
            patch("auctions.tasks.send_push_to_user.delay") as push,
        ):
            response = self.client.post(
                self.url,
                {"text": "Bring a plant", "send_to_discord": "on", "send_to_push": "on", "show_on_website": "on"},
            )
            self._grace_expires()
        self.assertEqual(response.status_code, 302)
        announcement = ClubAnnouncement.objects.get(club=self.club)
        self.assertEqual(announcement.text, "Bring a plant")
        self.assertTrue(announcement.discord_sent)
        self.assertEqual(announcement.discord_message_id, "msg-1")
        self.assertEqual(announcement.push_recipients, 1)
        discord.assert_called_once()
        self.assertIn("Bring a plant", discord.call_args[0][1])
        push.assert_called_once()
        self.assertEqual(push.call_args[0][0], member.user_id)

    def test_a_failed_discord_post_is_recorded_and_never_costs_the_push(self):
        self._reachable_member()
        self.client.force_login(self.admin)
        with (
            patch("auctions.discord_events.send_channel_message", return_value=""),
            patch("auctions.tasks.send_push_to_user.delay") as push,
        ):
            self.client.post(
                self.url,
                {"text": "Still going out", "send_to_discord": "on", "send_to_push": "on", "show_on_website": "on"},
            )
            self._grace_expires()
        announcement = ClubAnnouncement.objects.get(club=self.club)
        self.assertFalse(announcement.discord_sent)
        self.assertEqual(announcement.push_recipients, 1)
        push.assert_called_once()

    def test_an_unticked_channel_is_not_used(self):
        self._reachable_member()
        self.client.force_login(self.admin)
        with (
            patch("auctions.discord_events.send_channel_message") as discord,
            patch("auctions.tasks.send_push_to_user.delay") as push,
        ):
            self.client.post(self.url, {"text": "Website only", "show_on_website": "on"})
            self._grace_expires()
        discord.assert_not_called()
        push.assert_not_called()

    def test_the_push_has_no_email_fallback(self):
        """Nobody ticked an email box, so an undeliverable push must be dropped, not mailed."""
        from auctions import notifications

        self.assertIn(notifications.CATEGORY_CLUB_ANNOUNCEMENT, notifications.PUSH_ONLY_CATEGORIES)

    def test_sending_writes_a_club_history_row(self):
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.send_channel_message", return_value="m"):
            self.client.post(self.url, {"text": "For the record", "show_on_website": "on"})
            self._grace_expires()
        history = ClubHistory.objects.get(club=self.club, action__contains="For the record")
        # Written by the send, not by the request, and still owned by whoever wrote it.
        self.assertEqual(history.user, self.admin)
        self.assertEqual(history.applies_to, "ANNOUNCEMENTS")

    def test_a_member_without_permission_cannot_open_or_post(self):
        plain = User.objects.create_user(username="plain", password="pw", email="p@example.com")
        ClubMember.objects.create(club=self.club, user=plain)
        self.client.force_login(plain)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"text": "no", "show_on_website": "on"}).status_code, 403)
        self.assertFalse(ClubAnnouncement.objects.exists())

    def test_the_discord_post_is_the_club_name_and_the_text_and_nothing_else(self):
        """No link. The whole announcement is in the message, so there is nowhere to send anybody."""
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.send_channel_message", return_value="m") as discord:
            self.client.post(self.url, {"text": "Discord only", "send_to_discord": "on"})
            self._grace_expires()
        body = discord.call_args[0][1]
        # No link, and no club name: the whole server belongs to the club.
        self.assertEqual(body, "Discord only")


class AnnouncementsEmbedTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Embed News Club", enable_club_page=True)
        self.url = reverse("club_announcements_embed", kwargs={"slug": self.club.slug})

    def _announce(self, text, **kwargs):
        kwargs.setdefault("show_on_website", True)
        return ClubAnnouncement.objects.create(club=self.club, text=text, **kwargs)

    def test_json_is_the_default_and_returns_the_newest_first(self):
        self._announce("Older")
        self._announce("Newer")
        payload = self.client.get(self.url, {"count": 3}).json()
        self.assertEqual([a["text"] for a in payload["announcements"]], ["Newer", "Older"])

    def test_the_default_is_one(self):
        self._announce("Older")
        self._announce("Newer")
        self.assertEqual(len(self.client.get(self.url).json()["announcements"]), 1)

    def test_the_count_is_capped(self):
        for i in range(6):
            self._announce(f"News {i}")
        self.assertEqual(len(self.client.get(self.url, {"count": 99}).json()["announcements"]), 3)

    def test_announcements_not_marked_for_the_website_never_appear(self):
        self._announce("Discord only", show_on_website=False, send_to_discord=True)
        self._announce("Public")
        payload = self.client.get(self.url, {"count": 3}).json()
        self.assertEqual([a["text"] for a in payload["announcements"]], ["Public"])

    def test_deleted_announcements_never_appear(self):
        self._announce("Gone", is_deleted=True)
        self.assertEqual(self.client.get(self.url).json()["announcements"], [])

    def test_the_iframe_formats_render_a_themed_page(self):
        self._announce("Themed")
        for fmt, theme in (("iframelight", "light"), ("iframedark", "dark")):
            with self.subTest(fmt=fmt):
                body = self.client.get(self.url, {"format": fmt}).content.decode()
                self.assertIn(f'data-theme="{theme}"', body)
                self.assertIn("Themed", body)

    def test_the_unstyled_format_carries_no_css(self):
        self._announce("Bare")
        body = self.client.get(self.url, {"format": "unstyledhtml"}).content.decode()
        self.assertIn("club-announcements", body)
        self.assertNotIn("<style", body)

    def test_an_unknown_format_falls_back_to_json(self):
        self._announce("Whatever")
        self.assertEqual(self.client.get(self.url, {"format": "iframelite"})["Content-Type"], "application/json")

    def test_the_embed_can_be_framed_and_fetched_cross_origin(self):
        self._announce("CORS")
        for fmt in ("json", "iframelight"):
            with self.subTest(fmt=fmt):
                response = self.client.get(self.url, {"format": fmt})
                self.assertEqual(response["Access-Control-Allow-Origin"], "*")
                self.assertNotIn("X-Frame-Options", response)

    def test_an_empty_club_says_so_rather_than_erroring(self):
        self.assertContains(self.client.get(self.url, {"format": "iframelight"}), "Nothing new")

    def test_a_club_with_its_page_disabled_has_no_embed(self):
        self.club.enable_club_page = False
        self.club.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_every_render_is_counted_whatever_the_format(self):
        """A render count, not a read count -- it answers "is my snippet showing this at all".
        JSON counts too: a club rendering the JSON itself has put it on a page just the same."""
        announcement = self._announce("Counted")
        for fmt in ("json", "iframelight", "unstyledhtml"):
            self.client.get(self.url, {"format": fmt})
        announcement.refresh_from_db()
        self.assertEqual(announcement.website_views, 3)

    def test_an_announcement_that_is_not_on_the_website_is_never_counted(self):
        hidden = self._announce("Discord only", show_on_website=False, send_to_discord=True)
        shown = self._announce("Public")
        self.client.get(self.url, {"count": 3})
        hidden.refresh_from_db()
        shown.refresh_from_db()
        self.assertEqual(hidden.website_views, 0)
        self.assertEqual(shown.website_views, 1)


class CurrentAuctionEmbedTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Auction Embed Club", enable_club_page=True)
        self.url = reverse("club_auction_embed", kwargs={"slug": self.club.slug})
        self.start = timezone.now() + datetime.timedelta(days=7)

    def _auction(self, title, **kwargs):
        kwargs.setdefault("promote_this_auction", True)
        kwargs.setdefault("date_start", self.start)
        return Auction.objects.create(title=title, club=self.club, **kwargs)

    def test_the_pinned_current_auction_wins(self):
        soonest = self._auction("Soonest", date_start=timezone.now() + datetime.timedelta(days=1))
        pinned = self._auction("Pinned")
        self.club.current_auction = pinned
        self.club.save()
        self.assertEqual(self.client.get(self.url).json()["auction"]["title"], "Pinned")
        self.assertTrue(soonest.pk)

    def test_without_a_pin_the_soonest_upcoming_promoted_auction_is_used(self):
        self._auction("Later", date_start=self.start + datetime.timedelta(days=30))
        self._auction("Sooner")
        self.assertEqual(self.client.get(self.url).json()["auction"]["title"], "Sooner")

    def test_an_unpromoted_auction_is_never_advertised(self):
        self._auction("Secret", promote_this_auction=False)
        self.assertIsNone(self.client.get(self.url).json()["auction"])

    def test_no_auction_says_so_rather_than_erroring(self):
        self.assertContains(self.client.get(self.url, {"format": "iframelight"}), "No auction running")

    def test_an_online_auction_shows_both_ends_of_its_window(self):
        self._auction("Online", is_online=True, date_end=self.start + datetime.timedelta(days=4))
        when = self.client.get(self.url).json()["auction"]["when"]
        self.assertIn("–", when)

    def test_the_iframe_format_renders_a_themed_page(self):
        self._auction("Themed")
        body = self.client.get(self.url, {"format": "iframedark"}).content.decode()
        self.assertIn('data-theme="dark"', body)
        self.assertIn("Themed", body)

    def test_a_club_with_its_page_disabled_has_no_embed(self):
        self.club.enable_club_page = False
        self.club.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)


class WebsiteIntegrationPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Integration Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="wi_admin", password="pw", email="wa@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.url = reverse("club_website_integration", kwargs={"slug": self.club.slug})

    def test_every_snippet_is_listed(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        for heading in ("Upcoming events", "Current auction", "Latest announcement", "Breeder Award leaderboard"):
            self.assertContains(response, heading)

    def test_the_bap_snippet_is_still_offered_with_the_program_switched_off(self):
        """Somebody choosing what to put on the club website is exactly who should learn it exists."""
        self.club.enable_breeder_award_program = False
        self.club.save()
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, "Breeder Award leaderboard")
        self.assertContains(response, "Breeder Award Program is turned off")
        self.assertContains(response, reverse("bap_embed", kwargs={"slug": self.club.slug}))

    def test_it_offers_both_themes_and_the_developer_formats(self):
        self.client.force_login(self.admin)
        body = self.client.get(self.url).content.decode()
        for fmt in ("iframelight", "iframedark", "unstyledhtml", "format=json"):
            self.assertIn(fmt, body)


class AnnouncementSidebarTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.club = Club.objects.create(name="Sidebar Club", enable_club_page=True)
        self.url = reverse("club_detail", kwargs={"slug": self.club.slug})

    def test_someone_with_the_announcements_permission_gets_the_link(self):
        user = User.objects.create_user(username="sb_mgr", password="pw", email="sb@example.com")
        ClubMember.objects.create(club=self.club, user=user, permission_send_announcements=True)
        self.client.force_login(user)
        self.assertContains(self.client.get(self.url), reverse("club_announcements", kwargs={"slug": self.club.slug}))

    def test_managing_auctions_is_not_enough_on_its_own(self):
        """The permission was split out of auction management: one press reaches every member."""
        user = User.objects.create_user(username="sb_auctions", password="pw", email="sba@example.com")
        ClubMember.objects.create(club=self.club, user=user, permission_manage_auctions=True)
        self.client.force_login(user)
        self.assertNotContains(
            self.client.get(self.url), reverse("club_announcements", kwargs={"slug": self.club.slug})
        )
        self.assertEqual(
            self.client.get(reverse("club_announcements", kwargs={"slug": self.club.slug})).status_code, 403
        )

    def test_an_ordinary_member_does_not(self):
        user = User.objects.create_user(username="sb_member", password="pw", email="sm@example.com")
        ClubMember.objects.create(club=self.club, user=user)
        self.client.force_login(user)
        self.assertNotContains(
            self.client.get(self.url), reverse("club_announcements", kwargs={"slug": self.club.slug})
        )

    def test_the_latest_website_announcement_is_shown_on_the_club_page(self):
        ClubAnnouncement.objects.create(club=self.club, text="Hidden", show_on_website=False)
        ClubAnnouncement.objects.create(club=self.club, text="On the page", show_on_website=True)
        response = self.client.get(self.url)
        self.assertContains(response, "On the page")
        self.assertNotContains(response, "Hidden")


class AnnouncementsHereCommandTests(TestCase):
    """`/announcements_here`, tested at the handler rather than through signature verification."""

    MANAGE_GUILD = str(1 << 5)

    def setUp(self):
        self.club = Club.objects.create(name="Slash Club", discord_server_id="guild-1")
        self.view = DiscordInteractionsView()

    def _payload(self, **overrides):
        data = {
            "guild_id": "guild-1",
            "channel_id": "chan-9",
            "member": {"permissions": self.MANAGE_GUILD, "user": {"id": "42", "username": "boss"}},
        }
        data.update(overrides)
        return data

    def _content(self, response):
        return json.loads(response.content)["data"]["content"]

    def test_it_sets_the_channel_and_writes_history(self):
        response = self.view._handle_announcements_here_command(self._payload())
        self.club.refresh_from_db()
        self.assertEqual(self.club.announcement_channel_id, "chan-9")
        self.assertIn("✅", self._content(response))
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="announcement channel").exists())

    def test_it_does_not_touch_the_auction_channel(self):
        self.club.auction_channel_id = "auction-chan"
        self.club.save()
        self.view._handle_announcements_here_command(self._payload())
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_channel_id, "auction-chan")
        self.assertEqual(self.club.announcement_channel_id, "chan-9")

    def test_it_needs_manage_server(self):
        data = self._payload()
        data["member"]["permissions"] = "0"
        response = self.view._handle_announcements_here_command(data)
        self.club.refresh_from_db()
        self.assertIsNone(self.club.announcement_channel_id)
        self.assertIn("Manage Server", self._content(response))

    def test_an_unconnected_server_is_told_to_run_connect(self):
        response = self.view._handle_announcements_here_command(self._payload(guild_id="somewhere-else"))
        self.assertIn("/connect", self._content(response))


class AnnouncementEmailChannelTests(TestCase):
    """The two email providers, which are two channels rather than one.

    A club with both connected has two lists with two different sets of people on them, so "email"
    as a single channel would mail whoever is on both of them twice. Everything below is about
    keeping the pair independent: either one alone, both, or neither; one failing must not take the
    other with it; and neither may ever be sent through this site's own mail server, because the
    provider is what owns the unsubscribe list.
    """

    def setUp(self):
        self.club = Club.objects.create(name="Email Club", enable_club_page=True)
        self.admin = User.objects.create_user(username="em_admin", password="pw", email="em@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.client = Client()
        self.url = reverse("club_announcements", kwargs={"slug": self.club.slug})

    def _connect_mailchimp(self):
        self.club.mailchimp_access_token = "tok"
        self.club.mailchimp_server_prefix = "us1"
        self.club.mailchimp_audience_id = "aud-1"
        self.club.save()

    def _connect_brevo(self):
        self.club.brevo_api_key = "key"
        self.club.brevo_list_id = "7"
        self.club.save()

    # --- what the form offers ---------------------------------------------------------------

    def test_an_unconnected_provider_is_offered_but_disabled_with_the_fix_in_it(self):
        form = ClubAnnouncementForm(None, club=self.club)
        for field_name, urlname in (
            ("send_to_mailchimp", "club_mailchimp_config"),
            ("send_to_brevo", "club_brevo_config"),
        ):
            self.assertTrue(form.fields[field_name].disabled)
            self.assertIn(reverse(urlname, kwargs={"slug": self.club.slug}), str(form.fields[field_name].help_text))

    def test_connected_but_no_list_chosen_says_so_rather_than_offering_a_dead_box(self):
        self.club.brevo_api_key = "key"
        self.club.save()
        form = ClubAnnouncementForm(None, club=self.club)
        self.assertTrue(form.fields["send_to_brevo"].disabled)
        self.assertIn("no list is chosen", str(form.fields["send_to_brevo"].help_text))

    def test_both_connected_offers_both(self):
        """Both boxes are live; clean() is what refuses ticking the pair -- see below."""
        self._connect_mailchimp()
        self._connect_brevo()
        form = ClubAnnouncementForm(None, club=self.club)
        self.assertFalse(form.fields["send_to_mailchimp"].disabled)
        self.assertFalse(form.fields["send_to_brevo"].disabled)

    def test_an_email_provider_alone_is_enough_to_send(self):
        """Email is a channel like the others: it satisfies "pick at least one place"."""
        self._connect_mailchimp()
        form = ClubAnnouncementForm(
            {"text": "Meeting moved", "send_to_mailchimp": "on", "show_on_website": ""}, club=self.club
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_there_is_no_subject_box_at_all(self):
        """The emailed subject is always "<Club> announcement" -- see ClubAnnouncement.email_subject."""
        self.assertNotIn("subject", ClubAnnouncementForm(None, club=self.club).fields)
        self._connect_brevo()
        self.assertNotIn("subject", ClubAnnouncementForm(None, club=self.club).fields)

    def test_the_other_provider_is_hidden_once_one_is_connected(self):
        """A permanently disabled "Connect Brevo" box beside a working Mailchimp one can only ever
        be wrong. Both are offered while neither is connected, because then it is a menu."""
        both = ClubAnnouncementForm(None, club=self.club).fields
        self.assertIn("send_to_mailchimp", both)
        self.assertIn("send_to_brevo", both)

        self._connect_mailchimp()
        fields = ClubAnnouncementForm(None, club=self.club).fields
        self.assertIn("send_to_mailchimp", fields)
        self.assertNotIn("send_to_brevo", fields)

        self._connect_brevo()
        fields = ClubAnnouncementForm(None, club=self.club).fields
        self.assertIn("send_to_mailchimp", fields)
        self.assertIn("send_to_brevo", fields)

    def test_nothing_is_ticked_when_the_form_opens(self):
        """Including the website box, whose model default is True: a pre-ticked channel is one
        nobody chose, and an empty form is refused rather than published quietly."""
        form = ClubAnnouncementForm(None, club=self.club)
        for field_name in ("send_to_discord", "send_to_push", "send_to_mailchimp", "send_to_brevo"):
            self.assertFalse(form.fields[field_name].initial, field_name)
        self.assertFalse(form.fields["show_on_website"].initial)
        self.assertFalse(ClubAnnouncementForm({"text": "Nowhere"}, club=self.club).is_valid())

    # --- what gets sent ------------------------------------------------------------------------

    def test_the_email_is_queued_once_the_retract_window_has_passed(self):
        self._connect_mailchimp()
        self.client.force_login(self.admin)
        with patch("auctions.tasks.send_announcement_emails.delay") as task:
            self.client.post(self.url, {"text": "Swap night", "send_to_mailchimp": "on"})
            # Not during the window: an email cannot be retracted, so it must not go out while
            # the club still thinks it can stop it.
            task.assert_not_called()
            announcements.send_due(now=timezone.now() + datetime.timedelta(seconds=announcements.GRACE_SECONDS + 5))
        announcement = ClubAnnouncement.objects.get()
        task.assert_called_once_with(announcement.pk)

    def test_nothing_is_queued_when_no_email_provider_was_ticked(self):
        self.client.force_login(self.admin)
        with patch("auctions.tasks.send_announcement_emails.delay") as task:
            self.client.post(self.url, {"text": "Website only", "show_on_website": "on"})
            announcements.send_due(now=timezone.now() + datetime.timedelta(seconds=announcements.GRACE_SECONDS + 5))
        task.assert_not_called()

    def test_each_provider_gets_its_own_merge_tag_in_the_greeting(self):
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Hello everyone")
        mailchimp_html, _ = announcements.render_email(announcement, greeting=announcements.MAILCHIMP_GREETING)
        brevo_html, _ = announcements.render_email(announcement, greeting=announcements.BREVO_GREETING)
        # Marked safe on the way in, so the provider sees its own syntax rather than escaped text.
        self.assertIn("*|IF:FNAME|*", mailchimp_html)
        self.assertIn("{{ contact.FIRSTNAME", brevo_html)
        self.assertNotIn("&quot;", brevo_html)

    def test_the_email_carries_the_announcement_and_no_link_anywhere(self):
        """The whole announcement is in the email, so there is nothing to click through to."""
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Come along")
        html, text = announcements.render_email(announcement, greeting="")
        self.assertIn("Come along", html)
        self.assertIn("Come along", text)
        self.assertNotIn("<a href", html)
        self.assertNotIn("http", text)

    def test_sending_stores_a_campaign_id_for_whichever_provider_carried_it(self):
        self._connect_mailchimp()
        self._connect_brevo()
        through_mailchimp = ClubAnnouncement.objects.create(club=self.club, text="MC", send_to_mailchimp=True)
        through_brevo = ClubAnnouncement.objects.create(club=self.club, text="BV", send_to_brevo=True)
        with (
            patch("auctions.mailchimp.send_announcement_campaign", return_value="mc-9") as mc,
            patch("auctions.brevo.send_announcement_campaign", return_value="bv-4") as bv,
        ):
            announcements.send_emails(through_mailchimp)
            announcements.send_emails(through_brevo)
        through_mailchimp.refresh_from_db()
        through_brevo.refresh_from_db()
        self.assertEqual(through_mailchimp.mailchimp_campaign_id, "mc-9")
        self.assertEqual(through_mailchimp.brevo_campaign_id, "")
        self.assertEqual(through_brevo.brevo_campaign_id, "bv-4")
        self.assertEqual(through_brevo.mailchimp_campaign_id, "")
        self.assertEqual(mc.call_count, 1)
        self.assertEqual(bv.call_count, 1)

    def test_a_provider_that_fails_leaves_the_reason_on_the_row(self):
        """It is sent from a task, so the row is the only place the admin will ever find out."""
        self._connect_mailchimp()
        announcement = ClubAnnouncement.objects.create(club=self.club, text="MC", send_to_mailchimp=True)
        with patch("auctions.mailchimp.send_announcement_campaign", side_effect=RuntimeError("mailchimp is down")):
            announcements.send_emails(announcement)
        announcement.refresh_from_db()
        self.assertEqual(announcement.mailchimp_campaign_id, "")
        self.assertIn("mailchimp is down", announcement.email_error)

    def test_both_providers_at_once_is_refused(self):
        """Members are synced to every connected provider, so both lists are the same people."""
        self._connect_mailchimp()
        self._connect_brevo()
        form = ClubAnnouncementForm({"text": "Twice", "send_to_mailchimp": "on", "send_to_brevo": "on"}, club=self.club)
        self.assertFalse(form.is_valid())
        self.assertIn("Pick one email provider", " ".join(form.errors["__all__"]))

    def test_either_one_on_its_own_is_fine(self):
        self._connect_mailchimp()
        self._connect_brevo()
        for field in ("send_to_mailchimp", "send_to_brevo"):
            form = ClubAnnouncementForm({"text": "Once", field: "on"}, club=self.club)
            self.assertTrue(form.is_valid(), form.errors)

    def test_the_subject_is_always_the_club_and_the_word_announcement(self):
        """Not the first line, and not anything the club typed: a one-sentence announcement in the
        subject *and* the body shows the same words twice in an inbox."""
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Bring plants\nand buckets")
        self.assertEqual(announcement.email_subject, f"{self.club.name} announcement")
        announcement.subject = "Saturday"
        self.assertEqual(announcement.email_subject, f"{self.club.name} announcement")

    def test_opens_are_summed_across_providers_and_a_silent_provider_leaves_them_alone(self):
        announcement = ClubAnnouncement.objects.create(
            club=self.club, text="x", mailchimp_campaign_id="mc-1", brevo_campaign_id="bv-1", email_opens=5
        )
        with (
            patch("auctions.mailchimp.campaign_opens", return_value=7),
            patch("auctions.brevo.campaign_opens", return_value=2),
        ):
            self.assertEqual(announcements.refresh_email_opens(announcement), 9)
        # "No report yet" is not "nobody opened it": the stored number survives.
        with (
            patch("auctions.mailchimp.campaign_opens", return_value=None),
            patch("auctions.brevo.campaign_opens", return_value=None),
        ):
            self.assertEqual(announcements.refresh_email_opens(announcement), 9)


class ProviderAddressPrefillTests(TestCase):
    """The club address on a donation letter, taken from the provider that already has it."""

    def setUp(self):
        self.club = Club.objects.create(name="Address Club")

    def test_mailchimp_contact_becomes_a_letter_address(self):
        from auctions import mailchimp as mc

        formatted = mc.format_mailing_address(
            {
                "company": "Address Club",
                "addr1": "1 Fish St",
                "city": "Boston",
                "state": "MA",
                "zip": "02101",
                "country": "US",
            }
        )
        self.assertEqual(formatted, "1 Fish St\nBoston MA 02101")

    def test_brevo_address_becomes_a_letter_address(self):
        from auctions import brevo

        formatted = brevo.format_mailing_address(
            {"street": "2 Reef Rd", "city": "Leeds", "zipCode": "LS1", "country": "UK"}
        )
        self.assertEqual(formatted, "2 Reef Rd\nLeeds LS1\nUK")

    def test_an_address_the_club_typed_itself_is_never_overwritten(self):
        from auctions.views import _prefill_donation_address

        self.club.donation_mailing_address = "PO Box 9"
        self.club.save()
        self.assertFalse(_prefill_donation_address(self.club, "1 Fish St\nBoston MA", "Mailchimp"))
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_mailing_address, "PO Box 9")

    def test_a_blank_address_is_filled_in_and_recorded(self):
        from auctions.views import _prefill_donation_address

        self.assertTrue(_prefill_donation_address(self.club, "1 Fish St\nBoston MA", "Mailchimp"))
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_mailing_address, "1 Fish St\nBoston MA")
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="Mailchimp").exists())


class AnnouncementRetractTests(TestCase):
    """Unsending, and being honest about how much of it can actually be unsent."""

    def setUp(self):
        self.club = Club.objects.create(
            name="Retract Club",
            enable_club_page=True,
            discord_server_id="guild-9",
            announcement_channel_id="chan-9",
        )
        self.admin = User.objects.create_user(username="rt_admin", password="pw", email="rt@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_send_announcements=True)
        self.client = Client()

    def _retract_url(self, announcement):
        return reverse("club_announcement_retract", kwargs={"slug": self.club.slug, "uuid": announcement.uuid})

    def test_a_retracted_one_stays_on_the_club_list_struck_through(self):
        """Retract is not Delete: the admin who pressed it needs to see that it worked."""
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Wrong date", show_on_website=True)
        self.client.force_login(self.admin)
        self.client.post(self._retract_url(announcement))
        page = self.client.get(reverse("club_announcements", kwargs={"slug": self.club.slug}))
        self.assertContains(page, "Wrong date")
        self.assertContains(page, "Retracted")
        # ...and the button is gone, because there is nothing left to take back.
        self.assertNotContains(page, self._retract_url(announcement))

    def test_it_writes_the_one_record_that_survives(self):
        """The row goes off the club page and off the website; club history is what is left."""
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Called off", show_on_website=True)
        self.client.force_login(self.admin)
        self.client.post(self._retract_url(announcement))
        history = ClubHistory.objects.get(club=self.club, applies_to="ANNOUNCEMENTS")
        self.assertIn("retracted", history.action)
        self.assertIn("Called off", history.action)
        self.assertEqual(history.user, self.admin)

    def test_it_deletes_the_discord_post_and_takes_it_off_the_website(self):
        announcement = ClubAnnouncement.objects.create(
            club=self.club,
            text="Wrong date",
            send_to_discord=True,
            discord_sent=True,
            discord_message_id="m-1",
            show_on_website=True,
        )
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.delete_channel_message", return_value=True) as delete:
            self.client.post(self._retract_url(announcement))
        delete.assert_called_once_with("chan-9", "m-1")
        announcement.refresh_from_db()
        self.assertTrue(announcement.is_deleted)
        self.assertEqual(announcements.latest_for_website(self.club), [])

    def test_it_says_what_is_still_out_there(self):
        announcement = ClubAnnouncement.objects.create(
            club=self.club, text="Oops", send_to_push=True, push_recipients=12, mailchimp_campaign_id="mc-1"
        )
        self.client.force_login(self.admin)
        response = self.client.post(self._retract_url(announcement), follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("12 phones already got the notification", text)
        self.assertIn("email has already been sent", text)

    def test_a_retracted_announcement_leaves_the_website_and_the_embed(self):
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Gone", show_on_website=True)
        self.client.force_login(self.admin)
        self.client.post(self._retract_url(announcement))
        self.assertEqual(announcements.latest_for_website(self.club), [])

    def test_a_member_without_the_permission_cannot_retract(self):
        announcement = ClubAnnouncement.objects.create(club=self.club, text="Mine")
        plain = User.objects.create_user(username="rt_plain", password="pw", email="rtp@example.com")
        ClubMember.objects.create(club=self.club, user=plain, permission_manage_auctions=True)
        self.client.force_login(plain)
        self.assertEqual(self.client.post(self._retract_url(announcement)).status_code, 403)
        announcement.refresh_from_db()
        self.assertFalse(announcement.is_deleted)

    def test_a_discord_post_that_cannot_be_deleted_is_reported_not_swallowed(self):
        announcement = ClubAnnouncement.objects.create(
            club=self.club, text="Stuck", send_to_discord=True, discord_sent=True, discord_message_id="m-2"
        )
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.delete_channel_message", return_value=False):
            response = self.client.post(self._retract_url(announcement), follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("remove it by hand", text)


class AnnouncementUnsubscribeTests(TestCase):
    """Who an announcement can reach, per channel, and why the answers differ.

    The email half is not ours to decide: a campaign goes to the provider's list, and this site's
    contribution is having kept that list right. "No non-essential emails" makes a member
    unsubscribed at the provider, so the campaign skips them -- while push still reaches them,
    because that preference is about email and installing the app was its own opt-in.
    """

    def setUp(self):
        self.club = Club.objects.create(name="Unsub Club")

    def _member(self, username, contact_status):
        user = User.objects.create_user(username=username, password="pw", email=f"{username}@example.com")
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        return ClubMember.objects.create(club=self.club, user=user, email=user.email, contact_status=contact_status)

    def test_no_non_essential_email_is_an_email_preference_not_a_push_one(self):
        from auctions import brevo
        from auctions import mailchimp as mc

        member = self._member("quiet", "non_essential")
        self.assertEqual(mc._desired_status(member), "unsubscribed")
        self.assertEqual(brevo._desired_status(member), "unsubscribed")
        self.assertIn(member, announcements.reachable_members(self.club))

    def test_do_not_contact_is_off_every_channel(self):
        from auctions import brevo
        from auctions import mailchimp as mc

        member = self._member("gone", "do_not_contact")
        self.assertEqual(mc._desired_status(member), "archived")
        self.assertEqual(brevo._desired_status(member), "archived")
        self.assertNotIn(member, announcements.reachable_members(self.club))

    def test_the_email_count_on_the_form_only_counts_subscribed_contacts(self):
        self.club.brevo_api_key = "key"
        self.club.brevo_list_id = "3"
        self.club.save()
        subscribed = self._member("in", "contact")
        ClubMember.objects.filter(pk=subscribed.pk).update(brevo_status="subscribed")
        left = self._member("out", "non_essential")
        ClubMember.objects.filter(pk=left.pk).update(brevo_status="unsubscribed")
        _mailchimp, brevo_count = announcements.email_recipient_counts(self.club)
        self.assertEqual(brevo_count, 1)


class AnnouncementSchedulingTests(TestCase):
    """ "Send at 9am Friday", and the row that exists in between.

    A scheduled announcement is written now and delivered later, which means there is a window
    where the row exists and nobody may see it: not on the club's website, not at its own URL, and
    not counted as sent. Most of the tests below are about that window.
    """

    def setUp(self):
        self.club = Club.objects.create(
            name="Later Club",
            enable_club_page=True,
            discord_server_id="guild-2",
            announcement_channel_id="chan-2",
        )
        self.admin = User.objects.create_user(username="sc_admin", password="pw", email="sc@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_send_announcements=True)
        self.client = Client()
        self.url = reverse("club_announcements", kwargs={"slug": self.club.slug})
        self.later = timezone.now() + datetime.timedelta(days=2)

    def _schedule(self, **extra):
        data = {
            "text": "Meeting on Friday",
            "show_on_website": "on",
            "scheduled_for": timezone.localtime(self.later).strftime("%Y-%m-%dT%H:%M"),
        }
        data.update(extra)
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.send_channel_message") as discord:
            self.client.post(self.url, data)
        return ClubAnnouncement.objects.get(), discord

    def test_scheduling_delivers_nothing_yet(self):
        announcement, discord = self._schedule(send_to_discord="on")
        discord.assert_not_called()
        self.assertIsNone(announcement.sent_at)
        self.assertTrue(announcement.is_scheduled)

    def test_it_stays_off_the_website_until_it_is_sent(self):
        announcement, _discord = self._schedule()
        self.assertEqual(announcements.latest_for_website(self.club), [])
        announcements.deliver(announcement)
        self.assertEqual(announcements.latest_for_website(self.club), [announcement])

    def test_the_embed_does_not_carry_it_until_it_is_sent(self):
        """The row and its text exist from the moment it is written; nothing public may show them."""
        announcement, _discord = self._schedule()
        embed = self.client.get(reverse("club_announcements_embed", kwargs={"slug": self.club.slug}))
        self.assertNotContains(embed, announcement.text)
        announcements.deliver(announcement)
        embed = self.client.get(reverse("club_announcements_embed", kwargs={"slug": self.club.slug}))
        self.assertContains(embed, announcement.text)

    def test_the_beat_sends_it_once_its_time_has_come(self):
        announcement, _discord = self._schedule(send_to_discord="on")
        with patch("auctions.discord_events.send_channel_message", return_value="m") as discord:
            self.assertEqual(announcements.send_due(now=self.later - datetime.timedelta(minutes=1)), 0)
            discord.assert_not_called()
            self.assertEqual(announcements.send_due(now=self.later + datetime.timedelta(minutes=1)), 1)
            discord.assert_called_once()
        announcement.refresh_from_db()
        self.assertIsNotNone(announcement.sent_at)

    def test_the_send_is_recorded_in_club_history_when_it_actually_happens(self):
        """The club got "Announcement scheduled" days ago; this is the half that says it went."""
        announcement, _discord = self._schedule(send_to_discord="on")
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, action__startswith="Announcement scheduled").exists()
        )
        with patch("auctions.discord_events.send_channel_message", return_value="m"):
            announcements.send_due(now=self.later + datetime.timedelta(minutes=1))
        history = ClubHistory.objects.filter(club=self.club, action__startswith="Announcement sent").get()
        self.assertIn(announcement.short_text, history.action)
        self.assertEqual(history.applies_to, "ANNOUNCEMENTS")
        # The beat ran it, but the person who wrote it owns it.
        self.assertEqual(history.user, self.admin)

    def test_a_second_pass_does_not_send_it_again(self):
        announcement, _discord = self._schedule(send_to_discord="on")
        after = self.later + datetime.timedelta(minutes=1)
        with patch("auctions.discord_events.send_channel_message", return_value="m") as discord:
            announcements.send_due(now=after)
            announcements.send_due(now=after)
            self.assertEqual(discord.call_count, 1)

    def test_cancelling_before_it_goes_says_nobody_saw_it(self):
        announcement, _discord = self._schedule()
        response = self.client.post(
            reverse("club_announcement_retract", kwargs={"slug": self.club.slug, "uuid": announcement.uuid}),
            follow=True,
        )
        self.assertIn("never sent", " ".join(str(m) for m in response.context["messages"]))
        announcement.refresh_from_db()
        self.assertTrue(announcement.is_deleted)
        with patch("auctions.discord_events.send_channel_message") as discord:
            announcements.send_due(now=self.later + datetime.timedelta(minutes=1))
        discord.assert_not_called()

    def test_a_time_in_the_past_is_refused_rather_than_sent_immediately(self):
        form = ClubAnnouncementForm(
            {
                "text": "Yesterday",
                "show_on_website": "on",
                "scheduled_for": timezone.localtime(timezone.now() - datetime.timedelta(days=1)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
            },
            club=self.club,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("already passed", str(form.errors["scheduled_for"]))

    def test_leaving_it_blank_gives_it_the_retract_window_and_nothing_more(self):
        """Blank is not "send later"; it is "send in half a minute, in case you got it wrong"."""
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.send_channel_message", return_value="m") as discord:
            self.client.post(self.url, {"text": "Now", "send_to_discord": "on"})
            discord.assert_not_called()
            announcement = ClubAnnouncement.objects.get()
            self.assertTrue(announcement.is_in_grace_period)
            self.assertFalse(
                ClubAnnouncement.objects.get(pk=announcement.pk).scheduled_for
                > timezone.now() + datetime.timedelta(minutes=1)
            )
            announcements.send_due(now=timezone.now() + datetime.timedelta(seconds=announcements.GRACE_SECONDS + 5))
        discord.assert_called_once()
        self.assertIsNotNone(ClubAnnouncement.objects.get().sent_at)

    def test_a_date_the_club_picked_is_not_a_retract_window(self):
        """Both are scheduled_for; only one of them reads as "going out in a moment"."""
        announcement, _discord = self._schedule()
        self.assertTrue(announcement.is_scheduled)
        self.assertFalse(announcement.is_in_grace_period)

    def test_retracting_inside_the_window_means_it_never_goes(self):
        self.client.force_login(self.admin)
        with patch("auctions.discord_events.send_channel_message", return_value="m") as discord:
            self.client.post(self.url, {"text": "Wrong date", "send_to_discord": "on"})
            announcement = ClubAnnouncement.objects.get()
            self.client.post(
                reverse(
                    "club_announcement_retract",
                    kwargs={"slug": self.club.slug, "uuid": announcement.uuid},
                )
            )
            announcements.send_due(now=timezone.now() + datetime.timedelta(seconds=announcements.GRACE_SECONDS + 5))
        discord.assert_not_called()
        announcement.refresh_from_db()
        self.assertIsNone(announcement.sent_at)
        self.assertTrue(announcement.is_deleted)
