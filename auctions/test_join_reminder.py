"""The "you looked but didn't join" reminder in ``auctiontos_notifications``: no distance to compare, and one
bad campaign, must not stop the run."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from auctions.management.commands.auctiontos_notifications import Command
from auctions.models import Auction, AuctionCampaign, PickupLocation

MAIL_SEND = "auctions.management.commands.auctiontos_notifications.mail.send"


@override_settings(FIREBASE_CREDENTIALS_JSON="")
class JoinReminderTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.creator = User.objects.create_user(username="reminder_creator", password="x", email="c@example.com")
        self.auction = Auction.objects.create(
            created_by=self.creator,
            title="In person, mail only",
            is_online=False,
            date_start=now + datetime.timedelta(days=2),
            date_end=now + datetime.timedelta(days=3),
        )
        PickupLocation.objects.create(
            name="by mail", auction=self.auction, pickup_by_mail=True, pickup_time=now + datetime.timedelta(days=2)
        )
        self.viewer = self.viewer_named("reminder_viewer")

    def viewer_named(self, username):
        user = User.objects.create_user(username=username, password="x", email=f"{username}@example.com")
        userdata = user.userdata
        userdata.send_reminder_emails_about_joining_auctions = True
        userdata.latitude = 40.1
        userdata.longitude = -80.1
        userdata.save()
        campaign = AuctionCampaign.objects.create(auction=self.auction, user=user, email=user.email)
        AuctionCampaign.objects.filter(pk=campaign.pk).update(timestamp=timezone.now() - datetime.timedelta(hours=48))
        return user

    def test_an_auction_with_no_distance_is_not_checked_against_one(self):
        with patch(MAIL_SEND) as send:
            call_command("auctiontos_notifications")
        emailed = [call.args[0] for call in send.call_args_list if call.args]
        self.assertIn(self.viewer.email, emailed)
        self.assertTrue(AuctionCampaign.objects.get(user=self.viewer).email_sent)

    def test_one_bad_campaign_does_not_stop_the_rest(self):
        second = self.viewer_named("second_viewer")
        with (
            patch.object(Command, "remind_to_join", side_effect=[RuntimeError("boom"), None]) as remind,
            self.assertLogs("auctions.management.commands.auctiontos_notifications", "ERROR"),
        ):
            call_command("auctiontos_notifications")
        self.assertEqual(remind.call_count, 2)
        self.assertEqual(AuctionCampaign.objects.filter(user__in=[self.viewer, second], email_sent=True).count(), 1)
