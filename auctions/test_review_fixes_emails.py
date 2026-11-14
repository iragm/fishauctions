"""Automatic emails, review fixes: bounced creators, feature-email spacing for the next-auction email, club
emails only to club staff, the cadence answer's permission, one re-engagement email per quiet spell, the
library email's link, and the cap on merge suggestions per run (WP6)."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from auctions import club_emails, duplicate_accounts, feature_emails, next_auction
from auctions.models import Auction, AuctionTOS, Club, ClubEmail, ClubMember, FeatureEmail, PickupLocation, UserData
from auctions.tests import StandardTestCase

NEXT_SEND = "auctions.next_auction.mail.send"
FEATURE_SEND = "auctions.feature_emails.mail.send"
CLUB_SEND = "auctions.club_emails.mail.send"
MERGE_SEND = "auctions.duplicate_accounts.mail.send"
DAY = datetime.timedelta(days=1)
STARTED = override_settings(FEATURE_EMAILS_START=(timezone.localdate() - datetime.timedelta(days=30)).isoformat())


def _seen(user, days_ago):
    UserData.objects.filter(user=user).update(last_activity=timezone.now() - datetime.timedelta(days=days_ago))


class Helpers(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.organizer = User.objects.create_user("rv_organizer", "rv_organizer@example.com", "pw")
        cls.club = Club.objects.create(name="Review Fixes Club")

    def make(self, title, *, days_ago=0, posted_days_ago=None, creator=None):
        now = timezone.now()
        start = now - datetime.timedelta(days=days_ago)
        auction = Auction.objects.create(
            title=title, created_by=creator or self.organizer, club=self.club, date_start=start
        )
        posted = now - datetime.timedelta(days=days_ago if posted_days_ago is None else posted_days_ago)
        Auction.objects.filter(pk=auction.pk).update(date_posted=posted)
        auction.refresh_from_db()
        return auction

    def annual(self, ended_days_ago=30):
        now = timezone.now()
        return Auction.objects.create(
            title="Annual auction 2026",
            created_by=self.organizer,
            club=self.club,
            is_online=True,
            date_start=now - (ended_days_ago + 1) * DAY,
            date_end=now - ended_days_ago * DAY,
        )

    def sent_to(self, send, address):
        return [call for call in send.call_args_list if call.args[0] == address]


class NextAuctionEmailTests(Helpers):
    def test_bounced_creator_gets_no_next_auction_email(self):
        UserData.objects.filter(user=self.organizer).update(email_bounced_on=timezone.now())
        self.make("Fall club auction", days_ago=12)
        with patch(NEXT_SEND) as send:
            self.assertEqual(next_auction.send_next_auction_emails(), 0)
        send.assert_not_called()

    def test_next_auction_email_blocks_feature_email_for_spacing(self):
        self.make("Fall club auction", days_ago=12)
        _seen(self.organizer, 200)
        with patch(NEXT_SEND) as next_send, patch(FEATURE_SEND) as feature_send, STARTED:
            self.assertEqual(next_auction.send_next_auction_emails(), 1)
            feature_emails.send_feature_emails()
        self.assertEqual(self.sent_to(next_send, self.organizer.email)[0].kwargs["template"], "next_auction")
        self.assertEqual(self.sent_to(feature_send, self.organizer.email), [])
        kinds = list(FeatureEmail.objects.filter(user=self.organizer).values_list("kind", flat=True))
        self.assertEqual(kinds, ["next_auction"])

    def test_second_next_auction_email_updates_row(self):
        with patch(NEXT_SEND):
            self.make("Fall club auction", days_ago=12)
            self.assertEqual(next_auction.send_next_auction_emails(), 1)
            second = self.make("Spring club auction", days_ago=12)
            self.assertEqual(next_auction.send_next_auction_emails(), 1)
        rows = FeatureEmail.objects.filter(user=self.organizer, kind="next_auction")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().about, second.slug[:100])


class ClubEmailRecipientTests(Helpers):
    def test_departed_creator_gets_no_club_email(self):
        ClubMember.objects.create(
            club=self.club, user=self.organizer, name="Bob Smith", permission_admin=True, is_deleted=True
        )
        self.annual()
        with patch(CLUB_SEND) as send:
            self.assertEqual(club_emails.send_club_emails(), 0)
        send.assert_not_called()
        self.assertFalse(ClubEmail.objects.filter(club=self.club).exists())
        # Back on the club's staff list, the same club is asked.
        ClubMember.objects.filter(club=self.club, user=self.organizer).update(is_deleted=False)
        with patch(CLUB_SEND) as send:
            self.assertEqual(club_emails.send_club_emails(), 1)
        self.assertEqual(send.call_args.args[0], self.organizer.email)

    def test_cadence_answer_needs_club_permission(self):
        self.assertFalse(club_emails.record_answer(self.club, self.organizer, "monthly"))
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_cadence, "")
        ClubMember.objects.create(club=self.club, user=self.organizer, name="Bob Smith", permission_view=True)
        self.assertTrue(club_emails.record_answer(self.club, self.organizer, "no_monthly"))
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_cadence, "no_monthly")

    def test_club_tools_only_to_edit_club_staff(self):
        auction = self.make("Meeting auction", days_ago=10)
        helper = User.objects.create_user("rv_helper", "rv_helper@example.com", "pw")
        outsider = User.objects.create_user("rv_outsider", "rv_outsider@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.organizer, name="Bob Smith", permission_view=True)
        ClubMember.objects.create(club=self.club, user=helper, name="Jane Doe", permission_edit_club=True)
        location = PickupLocation.objects.create(name="Hall", auction=auction)
        for admin in (helper, outsider):
            AuctionTOS.objects.create(auction=auction, pickup_location=location, user=admin, is_admin=True)
        self.assertEqual(club_emails.club_tools_recipients(auction), [helper])


class QuietSpellTests(Helpers):
    def test_one_reengagement_email_per_quiet_spell(self):
        reader = User.objects.create_user("rv_reader", "rv_reader@example.com", "pw")
        _seen(reader, 200)
        FeatureEmail.objects.create(user=reader, kind="library")
        FeatureEmail.objects.filter(user=reader).update(sent_on=timezone.now() - datetime.timedelta(days=100))
        with patch(FEATURE_SEND) as send, STARTED:
            feature_emails.send_feature_emails()
        self.assertEqual(self.sent_to(send, reader.email), [])
        kinds = set(FeatureEmail.objects.filter(user=reader).values_list("kind", flat=True))
        self.assertEqual(kinds, {"library"})

    def test_quiet_again_after_return_gets_next_kind(self):
        reader = User.objects.create_user("rv_returner", "rv_returner@example.com", "pw")
        _seen(reader, 30)
        FeatureEmail.objects.create(user=reader, kind="library")
        FeatureEmail.objects.filter(user=reader).update(sent_on=timezone.now() - datetime.timedelta(days=300))
        with patch(FEATURE_SEND) as send, STARTED:
            feature_emails.send_feature_emails()
        mails = self.sent_to(send, reader.email)
        self.assertEqual([call.kwargs["context"]["button"] for call in mails], ["Try it"])
        self.assertTrue(FeatureEmail.objects.filter(user=reader, kind="palette").exists())

    def test_library_email_links_search(self):
        reader = User.objects.create_user("rv_library", "rv_library@example.com", "pw")
        _seen(reader, 60)
        with patch(FEATURE_SEND) as send, STARTED:
            feature_emails.send_feature_emails()
        mails = self.sent_to(send, reader.email)
        self.assertEqual(len(mails), 1)
        context = mails[0].kwargs["context"]
        self.assertEqual(context["button"], "Search the library")
        self.assertIn("/library/search/?src=email_library", context["url"])


class MergeSuggestionCapTests(Helpers):
    def test_merge_suggestions_capped_per_run(self):
        for name in ("rv_pat1", "rv_pat2", "rv_pat3"):
            user = User.objects.create_user(name, f"{name}@example.com", "pw", first_name="Pat", last_name="Fishkeeper")
            UserData.objects.get_or_create(user=user)
            UserData.objects.filter(user=user).update(last_ip_address="10.4.4.4")
        with patch.object(duplicate_accounts, "MAX_PER_RUN", 2), patch(MERGE_SEND) as send:
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 2)
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 1)
        self.assertEqual(send.call_count, 3)
