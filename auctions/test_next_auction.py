"""The emails that ask an auction's creator to run the next one (``auctions/next_auction.py``), the ended
auction's pointer to the club's next one, the invoice email's line about it, and the selling page's
"Promote my lots" link."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from auctions import next_auction
from auctions.models import Auction, Club, Invoice, Lot, PickupLocation
from auctions.tests import StandardTestCase
from auctions.views.browse import _share_my_lots_url

SEND = "auctions.next_auction.mail.send"


class Helpers(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.organizer = User.objects.create_user("organizer_na", "organizer_na@example.com", "pw")
        cls.club = Club.objects.create(name="Next Auction Test Club")

    def make(self, title, *, days_ago=0, posted_days_ago=None, club=True, online=False, **extra):
        now = timezone.now()
        start = now - datetime.timedelta(days=days_ago)
        auction = Auction.objects.create(
            title=title,
            created_by=self.organizer,
            club=self.club if club else None,
            is_online=online,
            date_start=start,
            date_end=start + datetime.timedelta(days=1) if online else None,
            **extra,
        )
        posted = now - datetime.timedelta(days=days_ago if posted_days_ago is None else posted_days_ago)
        Auction.objects.filter(pk=auction.pk).update(date_posted=posted)
        auction.refresh_from_db()
        return auction


class StemTests(Helpers):
    def test_dates_and_numbers_are_dropped(self):
        self.assertEqual(next_auction.title_stem("TFCB 2026 annual auction"), "tfcb annual auction")
        self.assertEqual(next_auction.title_stem("The SCTFS 5TH Annual Fall Auction"), "the sctfs annual auction")
        self.assertEqual(
            next_auction.title_stem("PVAS Monthly Meeting Auction - October 2026"), "pvas monthly meeting auction"
        )


class NextAuctionDueTests(Helpers):
    def test_unknown_cadence_is_three_weeks_less_half(self):
        auction = self.make("Spring club auction", days_ago=30)
        due = next_auction.next_auction_due(auction)
        expected = auction.wind_down_time + next_auction.DEFAULT_GAP - next_auction.DEFAULT_GAP / 2
        self.assertEqual(due, expected)

    def test_club_cadence_is_used(self):
        for months in (3, 2, 1):
            self.make(f"Club auction {months}", days_ago=30 * months + 1)
        latest = self.make("Club auction now", days_ago=1)
        self.assertEqual(next_auction.expected_gap(latest), datetime.timedelta(days=30))

    def test_a_test_auction_is_due_a_week_after_it_was_made(self):
        auction = self.make("test auction", days_ago=0, posted_days_ago=0)
        self.assertEqual(next_auction.next_auction_due(auction), auction.date_posted + next_auction.TEST_NUDGE_AFTER)


class SendNextAuctionTests(Helpers):
    @patch(SEND)
    def test_sends_once_when_due(self, send):
        auction = self.make("Fall club auction", days_ago=12)
        self.assertEqual(next_auction.send_next_auction_emails(), 1)
        self.assertEqual(send.call_args.kwargs["template"], "next_auction")
        self.assertEqual(send.call_args.args[0], self.organizer.email)
        self.assertFalse(send.call_args.kwargs["context"]["is_test"])
        auction.refresh_from_db()
        self.assertTrue(auction.next_auction_email_sent)
        self.assertTrue(auction.anniversary_email_sent)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)

    @patch(SEND)
    def test_not_before_it_is_due(self, send):
        self.make("Fall club auction", days_ago=3)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)
        send.assert_not_called()

    @patch(SEND)
    def test_skipped_when_the_club_made_another_auction(self, send):
        auction = self.make("Fall club auction", days_ago=12)
        self.make("Winter club auction", days_ago=-20, posted_days_ago=2)
        next_auction.send_next_auction_emails()
        sent_for = [call.kwargs["context"]["auction"].pk for call in send.call_args_list]
        self.assertNotIn(auction.pk, sent_for)
        auction.refresh_from_db()
        self.assertTrue(auction.next_auction_email_sent)

    @patch(SEND)
    def test_a_skipped_ask_keeps_the_anniversary(self, send):
        auction = self.make("Fall club auction", days_ago=12)
        self.make("Winter club auction", days_ago=-20, posted_days_ago=2)
        next_auction.send_next_auction_emails()
        auction.refresh_from_db()
        self.assertTrue(auction.next_auction_email_sent)
        self.assertFalse(auction.anniversary_email_sent)

    @patch(SEND)
    def test_clubless_test_then_a_club_auction_is_not_nudged(self, send):
        self.make("test auction", days_ago=8, club=False)
        self.make("Real club auction", days_ago=-5, posted_days_ago=7)
        next_auction.send_next_auction_emails()
        sent_for = [call.kwargs["context"]["auction"].title for call in send.call_args_list]
        self.assertNotIn("test auction", sent_for)

    @patch(SEND)
    def test_inactive_creator_gets_nothing(self, send):
        self.organizer.is_active = False
        self.organizer.save()
        self.make("Fall club auction", days_ago=12)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)

    @patch(SEND)
    def test_auctions_marked_sent_never_send(self, send):
        self.make("Fall club auction", days_ago=12, next_auction_email_sent=True)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)

    @patch(SEND)
    def test_too_late_is_dropped(self, send):
        self.make("Old club auction", days_ago=60)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)
        send.assert_not_called()

    @patch(SEND)
    def test_unsubscribed_creator_gets_nothing(self, send):
        self.organizer.userdata.has_unsubscribed = True
        self.organizer.userdata.save()
        self.make("Fall club auction", days_ago=12)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)
        send.assert_not_called()

    @patch(SEND)
    def test_test_auction_nudge(self, send):
        self.make("test auction", days_ago=8)
        self.assertEqual(next_auction.send_next_auction_emails(), 1)
        self.assertTrue(send.call_args.kwargs["context"]["is_test"])

    @patch(SEND)
    def test_no_nudge_for_a_club_that_already_runs_real_auctions(self, send):
        self.make("Summer club auction", days_ago=200, next_auction_email_sent=True)
        self.make("test auction", days_ago=8)
        self.assertEqual(next_auction.send_next_auction_emails(), 0)


class AnniversaryTests(Helpers):
    def year_ago(self):
        return 365 - next_auction.ANNIVERSARY_LEAD.days + 3

    @patch(SEND)
    def test_sends_ten_weeks_before_the_anniversary(self, send):
        auction = self.make("TFCB 2025 annual auction", days_ago=self.year_ago(), next_auction_email_sent=True)
        self.assertEqual(next_auction.send_anniversary_emails(), 1)
        self.assertEqual(send.call_args.kwargs["template"], "auction_anniversary")
        auction.refresh_from_db()
        self.assertTrue(auction.anniversary_email_sent)
        self.assertEqual(next_auction.send_anniversary_emails(), 0)

    @patch(SEND)
    def test_skipped_when_this_years_exists(self, send):
        self.make("TFCB 2025 annual auction", days_ago=self.year_ago(), next_auction_email_sent=True)
        self.make("TFCB 2026 annual auction", days_ago=-30, posted_days_ago=5, next_auction_email_sent=True)
        self.assertEqual(next_auction.send_anniversary_emails(), 0)

    @patch(SEND)
    def test_outside_the_window_nothing(self, send):
        self.make("TFCB 2024 annual auction", days_ago=500, next_auction_email_sent=True)
        self.make("TFCB 2025 annual auction", days_ago=100, next_auction_email_sent=True)
        self.assertEqual(next_auction.send_anniversary_emails(), 0)

    @patch(SEND)
    def test_one_per_club_per_run(self, send):
        days = self.year_ago()
        self.make("Club spring swap", days_ago=days, next_auction_email_sent=True)
        self.make("Club plant auction", days_ago=days - 1, next_auction_email_sent=True)
        self.assertEqual(next_auction.send_anniversary_emails(), 1)

    @patch(SEND)
    def test_a_close_neighbour_a_day_later_is_not_a_second_email(self, send):
        days = self.year_ago()
        self.make("Club spring swap", days_ago=days, next_auction_email_sent=True)
        self.assertEqual(next_auction.send_anniversary_emails(), 1)
        self.make("Club plant auction", days_ago=days - 3, next_auction_email_sent=True)
        later = timezone.now() + datetime.timedelta(days=3)
        self.assertEqual(next_auction.send_anniversary_emails(now=later), 0)

    @patch(SEND)
    def test_test_auctions_are_ignored(self, send):
        self.make("test auction", days_ago=self.year_ago(), next_auction_email_sent=True)
        self.assertEqual(next_auction.send_anniversary_emails(), 0)


class NextClubAuctionTests(Helpers):
    def test_only_the_same_clubs_promoted_upcoming_auction(self):
        ended = self.make("Fall club auction", days_ago=10)
        self.assertIsNone(ended.next_club_auction)
        self.make("Unpromoted winter auction", days_ago=-10)
        other_club = Club.objects.create(name="Some Other Club")
        Auction.objects.create(
            title="Other club auction",
            created_by=self.organizer,
            club=other_club,
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=5),
            promote_this_auction=True,
        )
        del ended.next_club_auction
        self.assertIsNone(ended.next_club_auction)
        following = self.make("Winter club auction", days_ago=-20, promote_this_auction=True)
        del ended.next_club_auction
        self.assertEqual(ended.next_club_auction, following)

    def test_ended_auction_page_links_it(self):
        ended = self.make("Fall club auction", days_ago=10, online=True)
        following = self.make("Winter club auction", days_ago=-20, promote_this_auction=True)
        response = self.client.get(ended.get_absolute_url(), follow=True)
        self.assertContains(response, following.get_absolute_url())

    def test_invoice_email_line(self):
        ended = self.make("Fall club auction", days_ago=10, online=True)
        location = PickupLocation.objects.create(name="hall", auction=ended)
        tos = ended.auctiontos_set.create(name="Sample Person", pickup_location=location, manually_added=True)
        invoice, _created = Invoice.objects.get_or_create(auctiontos_user=tos, auction=ended)
        template = Template("{% load email_tags %}{% email_next_auction %}|{% email_next_auction_text %}")
        self.assertEqual(template.render(Context({"invoice": invoice, "domain": "example.com"})), "|")
        following = self.make("Winter club auction", days_ago=-20, promote_this_auction=True)
        invoice = Invoice.objects.get(pk=invoice.pk)
        rendered = template.render(Context({"invoice": invoice, "domain": "example.com"}))
        self.assertIn(f"https://example.com{following.get_absolute_url()}?src=invoice_next", rendered)
        self.assertIn("Next up:", rendered)


class ShareMyLotsTests(StandardTestCase):
    def request_for(self, user):
        request = RequestFactory().get("/selling/")
        request.user = user
        return request

    def test_link_for_an_open_auction(self):
        auction = self.online_auction
        auction.date_end = timezone.now() + datetime.timedelta(days=3)
        auction.save()
        Lot.objects.create(lot_name="Shared lot", auction=auction, user=self.user, auctiontos_seller=self.online_tos)
        url = _share_my_lots_url(self.request_for(self.user))
        self.assertIn(reverse("user_lots"), url)
        self.assertIn("src=seller_share", url)
        self.assertIn(f"auction={auction.slug}", url)

    def test_no_link_without_lots(self):
        self.assertIsNone(_share_my_lots_url(self.request_for(self.user_who_does_not_join)))

    def test_selling_page_shows_the_button(self):
        auction = self.online_auction
        auction.date_end = timezone.now() + datetime.timedelta(days=3)
        auction.save()
        Lot.objects.create(lot_name="Shared lot", auction=auction, user=self.user, auctiontos_seller=self.online_tos)
        self.client.force_login(self.user)
        response = self.client.get(reverse("selling"))
        self.assertContains(response, "Promote my lots")
