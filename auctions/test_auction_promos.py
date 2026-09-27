"""``auction_promos``: when a promoted auction is announced, to whom, by which channel, and the sent log."""

import datetime
import io
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from auctions.management.commands import auction_promos
from auctions.management.commands.auction_promos import Command, is_send_time, promotion_window
from auctions.models import (
    Auction,
    AuctionCampaign,
    AuctionTOS,
    Lot,
    MobileDevice,
    PickupLocation,
    PushNotificationSent,
    UserBan,
    UserData,
)
from auctions.test_support import isolated_cache

FAKE_FIREBASE = '{"type": "service_account", "project_id": "x"}'
#: Today at 10:30 AM in UTC, the fixture user's time zone, so the send hour is open unless a test moves
#: it. Today rather than a fixed date: the join reminder and AuctionTOS read the real clock.
NOW = timezone.now().astimezone(datetime.UTC).replace(hour=10, minute=30, second=0, microsecond=0)
DAY = datetime.timedelta(days=1)
SEND = "auctions.management.commands.auction_promos.mail.send"
PUSH = "auctions.tasks.send_push_to_user.delay"


@isolated_cache("auction-promos")
class AuctionPromosTestCase(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="organizer", password="x", email="organizer@example.com")
        self.fan = User.objects.create_user(username="fan", password="x", email="fan@example.com", first_name="Fan")
        UserData.objects.filter(user=self.fan).update(
            latitude=40.1,
            longitude=-80.1,
            timezone="UTC",
            email_me_about_new_auctions=True,
            email_me_about_new_auctions_distance=100,
            email_me_about_new_in_person_auctions=True,
            email_me_about_new_in_person_auctions_distance=100,
            has_unsubscribed=False,
            last_activity=NOW - 10 * DAY,
        )
        self.fan.userdata.refresh_from_db()

    def make_auction(self, *, is_online=False, starts, ends=None, posted=NOW - 10 * DAY, promoted=True, **kwargs):
        auction = Auction.objects.create(
            created_by=self.seller,
            title="Spring Swap",
            is_online=is_online,
            promote_this_auction=promoted,
            date_start=starts,
            date_end=ends,
            **kwargs,
        )
        Auction.objects.filter(pk=auction.pk).update(date_posted=posted)
        auction.refresh_from_db()
        PickupLocation.objects.create(
            name="Fire hall", auction=auction, latitude=40.0, longitude=-80.0, pickup_time=starts
        )
        return auction

    def run_job(self, now=NOW):
        started = timezone.now()
        with patch(SEND) as send, patch(PUSH) as push, self.captureOnCommitCallbacks(execute=True):
            Command().promote_all(now)
        # Stamped by the real clock; move them to the pretend one, which the one-a-day rule reads.
        AuctionCampaign.objects.filter(timestamp__gte=started).update(timestamp=now)
        return send, push

    def emailed(self, send):
        return [call.args[0] for call in send.call_args_list]


class WindowTests(AuctionPromosTestCase):
    def test_in_person_opens_a_week_before_and_closes_at_the_start(self):
        auction = self.make_auction(starts=NOW + 10 * DAY)
        self.assertEqual(promotion_window(auction), (NOW + 3 * DAY, NOW + 10 * DAY))

    def test_a_late_auction_waits_a_day_after_it_was_created(self):
        auction = self.make_auction(starts=NOW + 3 * DAY, posted=NOW)
        self.assertEqual(promotion_window(auction)[0], NOW + DAY)

    def test_online_opens_a_day_after_bidding_starts_and_closes_when_bidding_ends(self):
        auction = self.make_auction(is_online=True, starts=NOW - 5 * DAY, ends=NOW + 5 * DAY)
        self.assertEqual(promotion_window(auction), (NOW - 4 * DAY, NOW + 5 * DAY))

    def test_in_person_auction_this_week_is_emailed_once(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        self.assertEqual(self.emailed(send), [self.fan.email])
        campaign = AuctionCampaign.objects.get(auction=auction, user=self.fan)
        self.assertEqual(campaign.kind, AuctionCampaign.KIND_PROMO)
        self.assertEqual(campaign.source, AuctionCampaign.SOURCE_PROMO_EMAIL)
        send, _ = self.run_job(NOW + DAY)
        send.assert_not_called()

    def test_in_person_auction_more_than_a_week_out_waits(self):
        self.make_auction(starts=NOW + 10 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_a_fresh_auction_waits_a_day(self):
        self.make_auction(starts=NOW + 5 * DAY, posted=NOW - datetime.timedelta(hours=2))
        send, _ = self.run_job()
        send.assert_not_called()

    def test_an_in_person_auction_that_started_is_never_announced(self):
        self.make_auction(starts=NOW - datetime.timedelta(hours=1))
        send, _ = self.run_job()
        send.assert_not_called()

    def test_online_auction_waits_for_a_day_of_bidding(self):
        self.make_auction(is_online=True, starts=NOW - datetime.timedelta(hours=2), ends=NOW + 7 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_online_auction_a_day_into_bidding_is_emailed(self):
        self.make_auction(is_online=True, starts=NOW - 2 * DAY, ends=NOW + 7 * DAY)
        send, _ = self.run_job()
        self.assertEqual(self.emailed(send), [self.fan.email])

    def test_an_online_auction_that_ended_is_not_announced(self):
        self.make_auction(is_online=True, starts=NOW - 9 * DAY, ends=NOW - DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_an_unpromoted_auction_is_not_announced(self):
        self.make_auction(starts=NOW + 5 * DAY, promoted=False)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_an_auction_without_categories_is_announced_like_any_other(self):
        self.make_auction(starts=NOW + 5 * DAY, use_categories=False)
        send, _ = self.run_job()
        self.assertEqual(self.emailed(send), [self.fan.email])


class SendHourTests(AuctionPromosTestCase):
    def test_waits_for_ten_in_the_morning(self):
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job(NOW + datetime.timedelta(hours=5))
        send.assert_not_called()

    def test_ten_in_the_morning_is_the_users_own(self):
        import zoneinfo

        UserData.objects.filter(user=self.fan).update(timezone="America/New_York")
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()  # early morning in New York
        send.assert_not_called()
        new_york = zoneinfo.ZoneInfo("America/New_York")
        ten_thirty_there = NOW.astimezone(new_york).replace(hour=10, minute=30).astimezone(datetime.UTC)
        send, _ = self.run_job(ten_thirty_there)
        self.assertEqual(self.emailed(send), [self.fan.email])

    def test_last_chance_goes_out_at_any_waking_hour(self):
        auction = self.make_auction(starts=NOW + datetime.timedelta(hours=10))
        send, _ = self.run_job(NOW + datetime.timedelta(hours=5))
        self.assertEqual(self.emailed(send), [self.fan.email])
        self.assertTrue(is_send_time(self.fan.userdata, NOW + datetime.timedelta(hours=5), auction.date_start))

    def test_last_chance_never_goes_out_overnight(self):
        # Created late, starting at 9 AM: its window opens at 2 AM.
        starts = NOW + DAY - datetime.timedelta(hours=1, minutes=30)
        self.make_auction(starts=starts, posted=NOW - datetime.timedelta(hours=9))
        send, _ = self.run_job(NOW + datetime.timedelta(hours=16))  # 2:30 AM
        send.assert_not_called()
        send, _ = self.run_job(NOW + datetime.timedelta(hours=22))  # 8:30 AM, before it starts
        self.assertEqual(self.emailed(send), [self.fan.email])

    def test_one_a_day_the_soonest_first(self):
        later = self.make_auction(starts=NOW + 6 * DAY)
        sooner = self.make_auction(starts=NOW + 3 * DAY)
        send, _ = self.run_job()
        self.assertEqual(len(send.call_args_list), 1)
        self.assertEqual(send.call_args.kwargs["context"]["auction"], sooner)
        send, _ = self.run_job(NOW + DAY)
        self.assertEqual(len(send.call_args_list), 1)
        self.assertEqual(send.call_args.kwargs["context"]["auction"], later)

    def test_an_unknown_time_zone_falls_back_to_the_sites(self):
        UserData.objects.filter(user=self.fan).update(timezone="Not/AZone")
        self.fan.userdata.refresh_from_db()
        is_send_time(self.fan.userdata, NOW, None)  # doesn't raise


class AudienceTests(AuctionPromosTestCase):
    def test_somebody_active_this_week_is_not_emailed(self):
        UserData.objects.filter(user=self.fan).update(last_activity=NOW - 2 * DAY)
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()
        self.assertFalse(AuctionCampaign.objects.exists())

    def test_somebody_who_goes_quiet_mid_window_hears_later(self):
        UserData.objects.filter(user=self.fan).update(last_activity=NOW - 2 * DAY)
        self.make_auction(starts=NOW + 6 * DAY)
        self.run_job()
        send, _ = self.run_job(NOW + 4 * DAY)
        self.assertEqual(self.emailed(send), [self.fan.email])

    def test_somebody_gone_for_over_a_year_is_not_emailed(self):
        UserData.objects.filter(user=self.fan).update(last_activity=NOW - 500 * DAY)
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_out_of_range(self):
        UserData.objects.filter(user=self.fan).update(email_me_about_new_in_person_auctions_distance=5)
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_the_nearest_of_several_locations_decides(self):
        UserData.objects.filter(user=self.fan).update(latitude=45.0, longitude=-75.0)
        auction = self.make_auction(is_online=True, starts=NOW - 2 * DAY, ends=NOW + 5 * DAY)
        near = PickupLocation.objects.create(
            name="Library", auction=auction, latitude=45.05, longitude=-75.05, pickup_time=NOW + 6 * DAY
        )
        send, _ = self.run_job()
        self.assertEqual(self.emailed(send), [self.fan.email])
        context = send.call_args.kwargs["context"]
        self.assertEqual(context["location"], near)
        self.assertTrue(context["multiple_locations"])

    def test_the_preference_is_per_kind_of_auction(self):
        UserData.objects.filter(user=self.fan).update(email_me_about_new_in_person_auctions=False)
        self.make_auction(starts=NOW + 5 * DAY)
        self.make_auction(is_online=True, starts=NOW - 2 * DAY, ends=NOW + 5 * DAY)
        send, _ = self.run_job()
        self.assertEqual(len(send.call_args_list), 1)
        self.assertEqual(send.call_args.kwargs["context"]["kind"], "online auction")

    def test_somebody_who_already_joined_is_not_told(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        AuctionTOS.objects.create(
            auction=auction, user=self.fan, pickup_location=auction.pickuplocation_set.first(), name="Fan"
        )
        send, _ = self.run_job()
        send.assert_not_called()

    def test_the_organizer_is_not_told_about_their_own_auction(self):
        UserData.objects.filter(user=self.seller).update(
            latitude=40.1, longitude=-80.1, timezone="UTC", last_activity=NOW - 10 * DAY
        )
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        self.assertEqual(self.emailed(send), [self.fan.email])

    def test_a_blank_distance_means_the_default_not_everywhere(self):
        UserData.objects.filter(user=self.fan).update(email_me_about_new_in_person_auctions_distance=None)
        self.make_auction(starts=NOW + 5 * DAY)
        far = self.make_auction(starts=NOW + 5 * DAY)
        PickupLocation.objects.filter(auction=far).update(latitude=30.0, longitude=-100.0)
        send, _ = self.run_job()
        self.assertEqual(len(send.call_args_list), 1)
        self.assertNotEqual(send.call_args.kwargs["context"]["auction"], far)

    def test_somebody_added_to_the_auction_by_email_is_not_told(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        AuctionTOS.objects.create(
            auction=auction, email="FAN@example.com", pickup_location=auction.pickuplocation_set.first(), name="Fan"
        )
        send, _ = self.run_job()
        send.assert_not_called()

    def test_somebody_the_organizer_banned_is_not_told(self):
        UserBan.objects.create(user=self.seller, banned_user=self.fan)
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_somebody_deleting_their_account_is_not_told(self):
        UserData.objects.filter(user=self.fan).update(account_deletion_requested=NOW - 8 * DAY)
        self.make_auction(starts=NOW + 5 * DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_an_online_auction_with_no_end_is_never_announced(self):
        auction = self.make_auction(is_online=True, starts=NOW - 2 * DAY, ends=None)
        # pre_save fills one in; only a direct write leaves it empty.
        Auction.objects.filter(pk=auction.pk).update(date_end=None)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_an_auction_the_weekly_email_already_listed_is_not_emailed_again(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        # The weekly email went after this auction's window opened.
        UserData.objects.filter(user=self.fan).update(last_weekly_promo_sent_at=promotion_window(auction)[0] + DAY)
        send, _ = self.run_job()
        send.assert_not_called()

    def test_one_the_weekly_email_went_out_before_is_emailed(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        UserData.objects.filter(user=self.fan).update(last_weekly_promo_sent_at=promotion_window(auction)[0] - DAY)
        send, _ = self.run_job()
        self.assertEqual(self.emailed(send), [self.fan.email])

    def test_an_auction_already_pushed_by_the_old_job_is_not_sent_again(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        PushNotificationSent.objects.create(user=self.fan, category="promo", auction=auction)
        send, _ = self.run_job()
        send.assert_not_called()


class PushTests(AuctionPromosTestCase):
    def setUp(self):
        super().setUp()
        UserData.objects.filter(user=self.fan).update(push_notifications_instead_of_email=True)
        MobileDevice.objects.create(user=self.fan, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_push_users_get_a_push_instead(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        send, push = self.run_job()
        send.assert_not_called()
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        campaign = AuctionCampaign.objects.get(auction=auction, user=self.fan)
        self.assertEqual(campaign.source, AuctionCampaign.SOURCE_PROMO_PUSH)
        self.assertEqual(kwargs["title"], "Auction coming up")
        self.assertIn(auction.title, kwargs["body"])
        self.assertIn("miles away", kwargs["body"])
        self.assertIn(f"src={campaign.uuid}", kwargs["url"])

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_online_push_says_bidding_is_open(self):
        self.make_auction(is_online=True, starts=NOW - 2 * DAY, ends=NOW + 5 * DAY)
        _, push = self.run_job()
        self.assertEqual(push.call_args.kwargs["title"], "Bidding is open")

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_push_is_not_held_back_for_recent_visitors(self):
        UserData.objects.filter(user=self.fan).update(last_activity=NOW)
        self.make_auction(starts=NOW + 5 * DAY)
        _, push = self.run_job()
        push.assert_called_once()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_a_push_still_in_the_queue_is_not_sent_again(self):
        self.make_auction(starts=NOW + 5 * DAY)
        with patch(PUSH) as push, self.captureOnCommitCallbacks(execute=True):
            Command().promote_all(NOW)
            Command().promote_all(NOW)
        push.assert_called_once()


class SentLogTests(AuctionPromosTestCase):
    def test_the_email_carries_the_auctions_lots_and_the_tracking_link(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        tos = AuctionTOS.objects.create(
            auction=auction, user=self.seller, pickup_location=auction.pickuplocation_set.first(), name="Organizer"
        )
        lot = Lot.objects.create(
            lot_name="Pair of Apistogramma", auction=auction, auctiontos_seller=tos, user=self.seller, quantity=1
        )
        send, _ = self.run_job()
        context = send.call_args.kwargs["context"]
        campaign = AuctionCampaign.objects.get(auction=auction, user=self.fan)
        self.assertEqual([found.pk for found in context["lots"]], [lot.pk])
        self.assertIn(f"src={campaign.uuid}", context["auction_url"])
        self.assertEqual(send.call_args.kwargs["template"], "auction_promo_email")

    def test_the_template_renders(self):
        """Unmocked: the template the migration made renders with what the job passes it."""
        from post_office.models import Email

        auction = self.make_auction(starts=NOW + 5 * DAY)
        Command().promote_all(NOW)
        email = Email.objects.get(template__name="auction_promo_email")
        message = email.email_message()
        self.assertIn(auction.title, message.subject)
        self.assertIn("Fire hall", message.body)
        self.assertIn(self.fan.userdata.unsubscribe_link, message.body)

    def test_joining_marks_the_promo_and_the_reminder_rows(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        self.run_job()
        AuctionCampaign.objects.create(auction=auction, user=self.fan, email=self.fan.email)
        AuctionTOS.objects.create(
            auction=auction, user=self.fan, pickup_location=auction.pickuplocation_set.first(), name="Fan"
        )
        self.assertEqual(
            set(AuctionCampaign.objects.filter(auction=auction, user=self.fan).values_list("result", flat=True)),
            {"JOINED"},
        )

    def test_a_promo_does_not_block_or_trigger_the_join_reminder(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        self.run_job()
        # Viewing afterwards still starts the join reminder's own row.
        AuctionCampaign.objects.create(auction=auction, user=self.fan, email=self.fan.email)
        self.assertEqual(AuctionCampaign.objects.filter(auction=auction, user=self.fan).count(), 2)
        AuctionCampaign.objects.update(timestamp=NOW - 2 * DAY, email_sent=False)
        with patch("auctions.management.commands.auctiontos_notifications.mail.send"):
            call_command("auctiontos_notifications")
        rows = dict(AuctionCampaign.objects.filter(auction=auction).values_list("kind", "email_sent"))
        # The reminder job picked up the view row and left the promo row alone.
        self.assertEqual(rows, {AuctionCampaign.KIND_VIEW: True, AuctionCampaign.KIND_PROMO: False})

    def test_the_database_stops_a_second_claim(self):
        """Two runs racing: the other one's claim is already in (as far as this run can tell, it isn't)."""
        auction = self.make_auction(starts=NOW + 5 * DAY)
        AuctionCampaign.objects.create(
            auction=auction, user=self.fan, promo_key=AuctionCampaign.promo_key_for(auction, self.fan)
        )
        send, _ = self.run_job()
        send.assert_not_called()

    def test_opting_in_again_undoes_stop_promotional_emails(self):
        from auctions.forms import ChangeUserNotificationsForm

        userdata = self.fan.userdata
        userdata.unsubscribe_from_all()
        userdata.refresh_from_db()
        self.assertTrue(userdata.has_unsubscribed)
        data = {"email_me_about_new_in_person_auctions": True, "email_me_about_new_in_person_auctions_distance": 100}
        form = ChangeUserNotificationsForm(self.fan, data, instance=userdata)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        userdata.refresh_from_db()
        self.assertFalse(userdata.has_unsubscribed)

    def test_saving_preferences_without_opting_in_keeps_it(self):
        from auctions.forms import ChangeUserNotificationsForm

        userdata = self.fan.userdata
        userdata.unsubscribe_from_all()
        userdata.refresh_from_db()
        form = ChangeUserNotificationsForm(
            self.fan, {"email_me_when_people_comment_on_my_lots": True}, instance=userdata
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        userdata.refresh_from_db()
        self.assertTrue(userdata.has_unsubscribed)

    def test_stats_count_the_promotion(self):
        auction = self.make_auction(starts=NOW + 5 * DAY)
        self.assertEqual(auction.promo_stats, {})
        self.run_job()
        AuctionTOS.objects.create(
            auction=auction, user=self.fan, pickup_location=auction.pickuplocation_set.first(), name="Fan"
        )
        auction = Auction.objects.get(pk=auction.pk)
        self.assertEqual(auction.promo_stats, {"emails": 1, "pushes": 0, "click_rate": 100.0, "join_rate": 100.0})
        # Join reminder numbers leave promo rows out.
        self.assertEqual(auction.number_of_reminder_emails, 0)

    def test_the_command_runs_and_takes_its_lock(self):
        from django.core.cache import cache

        cache.add(auction_promos.LOCK_KEY, 1)
        with patch.object(Command, "promote_all") as promote_all:
            call_command("auction_promos")
        promote_all.assert_not_called()
        cache.delete(auction_promos.LOCK_KEY)
        with patch.object(Command, "promote_all", return_value=(0, 0)) as promote_all:
            call_command("auction_promos", stdout=io.StringIO())
        promote_all.assert_called_once()
