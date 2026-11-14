"""The monthly-auction and club-tools emails (``auctions/club_emails.py``), the page that takes a club's
answer, and ``outreach_queue(lapsed=true)``."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from post_office.models import Email

from auctions import club_emails
from auctions.club_health import ClubHealth
from auctions.mcp import admin_reports
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubEmail,
    ClubHistory,
    ClubMember,
    FeatureEmail,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase

SEND = "auctions.club_emails.mail.send"
DAY = datetime.timedelta(days=1)


class Helpers(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.creator = User.objects.create_user("ce_creator", "ce_creator@example.com", "pw")
        cls.helper = User.objects.create_user("ce_helper", "ce_helper@example.com", "pw")
        cls.owner = User.objects.create_superuser("ce_owner", "ce_owner@example.com", "pw")

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Cichlid society")
        # Club staff only get club emails (club_emails._send_to_club), so the fixtures are members.
        ClubMember.objects.create(club=self.club, user=self.creator, name="Ce Creator", permission_admin=True)
        ClubMember.objects.create(club=self.club, user=self.helper, name="Ce Helper", permission_edit_club=True)

    def auction(self, title="Spring auction", ended_days_ago=30, posted_days_ago=None, creator=None):
        now = timezone.now()
        auction = Auction.objects.create(
            title=title,
            created_by=creator or self.creator,
            club=self.club,
            is_online=True,
            date_start=now - (ended_days_ago + 1) * DAY,
            date_end=now - ended_days_ago * DAY,
        )
        if posted_days_ago is not None:
            Auction.objects.filter(pk=auction.pk).update(date_posted=now - posted_days_ago * DAY)
        return auction

    def sent(self, send):
        return [(call.args[0], call.kwargs["template"]) for call in send.call_args_list]


@patch(SEND)
class MonthlyInviteTests(Helpers):
    def test_goes_to_the_creator_three_weeks_after_an_annual_auction(self, send):
        self.auction("Annual auction 2026", ended_days_ago=30)
        self.assertEqual(club_emails.send_club_emails(), 1)
        self.assertEqual(self.sent(send), [(self.creator.email, "club_email_monthly_invite")])
        self.assertTrue(ClubEmail.objects.filter(club=self.club, kind="monthly_invite").exists())
        self.assertTrue(FeatureEmail.objects.filter(user=self.creator, kind="monthly_invite").exists())
        send.reset_mock()
        self.assertEqual(club_emails.send_club_emails(), 0)

    def test_waits_three_weeks(self, send):
        self.auction("Annual auction", ended_days_ago=10)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_a_big_gross_counts_without_annual_in_the_title(self, send):
        self.auction("Fall swap", ended_days_ago=30)
        with patch.object(Auction, "gross", 3500):
            club_emails.send_club_emails()
        self.assertEqual(len(self.sent(send)), 1)

    def test_a_small_auction_without_annual_doesnt(self, send):
        self.auction("Fall swap", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_annual_is_a_whole_word(self, send):
        self.auction("Semiannualish swap", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_a_club_that_answered_isnt_asked(self, send):
        Club.objects.filter(pk=self.club.pk).update(auction_cadence="no_monthly")
        self.auction("Annual auction", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_nobody_who_opted_out(self, send):
        UserData.objects.filter(user=self.creator).update(email_me_about_new_features=False)
        self.auction("Annual auction", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])
        self.assertFalse(ClubEmail.objects.filter(club=self.club).exists(), "the club can still be asked later")

    def test_someone_who_just_had_a_feature_email_waits(self, send):
        FeatureEmail.objects.create(user=self.creator, kind="dues")
        self.auction("Annual auction", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])
        FeatureEmail.objects.filter(user=self.creator).update(sent_on=timezone.now() - 8 * DAY)
        club_emails.send_club_emails()
        self.assertEqual(len(self.sent(send)), 1)

    @override_settings(CLUB_EMAILS_START="2099-01-01")
    def test_nothing_before_the_start(self, send):
        self.auction("Annual auction", ended_days_ago=30)
        self.assertEqual(club_emails.send_club_emails(), 0)

    def test_a_failed_send_frees_the_person_and_the_club(self, send):
        send.side_effect = RuntimeError("smtp down")
        self.auction("Annual auction", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertFalse(FeatureEmail.objects.filter(user=self.creator, kind="monthly_invite").exists())
        self.assertFalse(ClubEmail.objects.filter(club=self.club).exists())

    def test_the_buttons_carry_the_answer_and_the_source(self, send):
        auction = self.auction("Annual auction", ended_days_ago=30)
        context = club_emails.monthly_invite_context(self.club, auction, self.creator, "example.com")
        urls = [button["url"] for button in context["buttons"]]
        self.assertEqual(len(urls), 3)
        self.assertIn("?answer=monthly&src=email_monthly_invite", urls[0])
        self.assertIn("?answer=not_interested&src=email_monthly_invite", urls[2])
        self.assertNotIn("add_club_url", context)


@patch(SEND)
class ClubToolsTests(Helpers):
    def monthly_club(self):
        for n in range(3):
            latest = self.auction(f"Meeting auction {n}", ended_days_ago=40 - 30 * n // 2, posted_days_ago=80 - 30 * n)
        location = PickupLocation.objects.create(name="Hall", auction=latest)
        AuctionTOS.objects.create(auction=latest, pickup_location=location, user=self.helper, is_admin=True)
        return latest

    def test_three_auctions_in_three_months_get_the_list(self, send):
        self.monthly_club()
        club_emails.send_club_emails()
        self.assertEqual(
            sorted(self.sent(send)),
            sorted([(self.creator.email, "club_email_club_tools"), (self.helper.email, "club_email_club_tools")]),
        )
        send.reset_mock()
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_a_monthly_club_isnt_asked_about_monthly_auctions(self, send):
        self.monthly_club()
        self.auction("Annual auction", ended_days_ago=30)
        club_emails.send_club_emails()
        self.assertNotIn("club_email_monthly_invite", [template for _to, template in self.sent(send)])

    def test_a_club_that_said_no_monthly_still_gets_it_once_it_runs_three(self, send):
        Club.objects.filter(pk=self.club.pk).update(auction_cadence="no_monthly")
        self.monthly_club()
        club_emails.send_club_emails()
        self.assertIn("club_email_club_tools", [template for _to, template in self.sent(send)])

    def test_three_auctions_spread_over_a_year_dont_count(self, send):
        for n in range(3):
            self.auction(f"Meeting auction {n}", ended_days_ago=30, posted_days_ago=40 + 120 * n)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_every_feature_links_its_help_and_the_clubs_setup(self, send):
        latest = self.monthly_club()
        context = club_emails.club_tools_context(self.club, latest, self.creator, "example.com")
        self.assertEqual(len(context["features"]), len(club_emails.FEATURES))
        for feature in context["features"]:
            self.assertIn("/help/", feature["help_url"])
            self.assertIn(f"/clubs/{self.club.slug}/", feature["setup_url"])
            self.assertTrue(feature["setup_url"].endswith("src=email_club_tools"))


class TemplateTests(Helpers):
    def test_both_templates_render(self):
        auction = self.auction("Annual auction", ended_days_ago=30)
        club_emails.send_club_emails()
        email = Email.objects.filter(to=self.creator.email).latest("id")
        self.assertIn("Monthly auctions for Cichlid society", email.subject)
        self.assertIn("doesn&#x27;t have monthly auctions", email.html_message)
        self.assertNotIn("Know another club", email.message)
        self.assertNotIn("{%", email.message)
        FeatureEmail.objects.filter(user=self.creator).update(sent_on=timezone.now() - 8 * DAY)
        for n in range(3):
            self.auction(f"Meeting {n}", ended_days_ago=20, posted_days_ago=10 + 20 * n)
        club_emails.send_club_emails()
        self.assertIsNotNone(auction)
        self.assertTrue(Email.objects.filter(to=self.creator.email, subject__startswith="Club tools").exists())


class AnswerPageTests(Helpers):
    def url(self, token=None, answer="", auction=None):
        token = token or club_emails.answer_token(self.club, self.creator, auction)
        path = reverse("club_cadence_answer", kwargs={"slug": self.club.slug, "token": token})
        return f"{path}?answer={answer}" if answer else path

    def test_get_shows_the_answers_and_changes_nothing(self):
        response = self.client.get(self.url(answer="no_monthly"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My club doesn&#x27;t have monthly auctions")
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_cadence, "")

    def test_post_stores_the_answer(self):
        response = self.client.post(self.url(), {"answer": "no_monthly"})
        self.assertContains(response, "saved")
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_cadence, "no_monthly")
        self.assertTrue(ClubHistory.objects.filter(club=self.club, user=self.creator).exists())

    def test_im_in_goes_on_to_copy_the_latest_auction(self):
        auction = self.auction("Annual auction")
        self.auction("test auction", ended_days_ago=1)
        response = self.client.post(self.url(auction=auction), {"answer": "monthly"})
        self.assertRedirects(
            response, f"{reverse('create_auction')}?copy={auction.slug}", fetch_redirect_response=False
        )
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_cadence, "monthly")

    def test_another_clubs_token_is_refused(self):
        other = Club.objects.create(name="Killifish club")
        token = club_emails.answer_token(other, self.creator)
        self.assertEqual(self.client.get(self.url(token=token)).status_code, 404)
        self.assertEqual(self.client.get(self.url(token="nonsense")).status_code, 404)

    def test_a_made_up_answer_stores_nothing(self):
        self.client.post(self.url(), {"answer": "weekly"})
        self.club.refresh_from_db()
        self.assertEqual(self.club.auction_cadence, "")


class LapsedQueueTests(Helpers):
    def run_report(self, params):
        request = RequestFactory().get("/mcp/admin/")
        request.user = self.owner
        return admin_reports.outreach_queue(request, params)

    def test_lapsed_lists_clubs_that_stopped(self):
        ClubHealth.objects.update_or_create(
            club=self.club, defaults={"stage": "slipping", "real_auctions": 4, "days_since_last_auction": 90}
        )
        never = Club.objects.create(name="Never started", contact_email="x@example.com")
        ClubHealth.objects.update_or_create(club=never, defaults={"stage": "aware"})
        lapsed = [row["club"] for row in self.run_report({"lapsed": True})["clubs"]]
        cold = [row["club"] for row in self.run_report({})["clubs"]]
        self.assertIn(self.club.pk, lapsed)
        self.assertNotIn(never.pk, lapsed)
        self.assertNotIn(self.club.pk, cold)


class AwaitingReplyTests(LapsedQueueTests):
    def test_emailed_clubs_wait_for_a_reply_until_one_is_recorded(self):
        from auctions.mcp import admin

        Club.objects.filter(pk=self.club.pk).update(date_contacted=timezone.now() - 10 * DAY)
        waiting = [row["club"] for row in self.run_report({"awaiting_reply": True})["clubs"]]
        self.assertEqual(waiting, [self.club.pk])
        request = RequestFactory().post("/mcp/admin/")
        request.user = self.owner
        admin.record_outreach(request, {"club": str(self.club.pk), "replied": True, "stall_reason": "cost"})
        self.club.refresh_from_db()
        self.assertEqual(self.club.stall_reason, "cost")
        self.assertIsNotNone(self.club.outreach_replied_on)
        self.assertEqual(self.run_report({"awaiting_reply": True})["clubs"], [])


@patch(SEND)
class ThanksTests(Helpers):
    def setUp(self):
        super().setUp()
        # Monthly auctions earn the club-tools email first; that one is tested above.
        ClubEmail.objects.create(club=self.club, kind="club_tools")

    def test_ten_auctions_get_one_thank_you(self, send):
        for n in range(club_emails.THANKS_AFTER):
            self.auction(f"Meeting {n}", ended_days_ago=10 + 30 * n, posted_days_ago=10 + 30 * n)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [(self.creator.email, "club_email_thanks")])
        send.reset_mock()
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_test_auctions_dont_count(self, send):
        for n in range(club_emails.THANKS_AFTER - 1):
            self.auction(f"Meeting {n}", ended_days_ago=10 + 30 * n, posted_days_ago=10 + 30 * n)
        self.auction("test auction", ended_days_ago=5)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_a_club_that_stopped_years_ago_isnt_thanked(self, send):
        for n in range(club_emails.THANKS_AFTER):
            self.auction(f"Meeting {n}", ended_days_ago=400 + 40 * n, posted_days_ago=400 + 40 * n)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])

    def test_an_unfinished_auction_doesnt_count(self, send):
        for n in range(club_emails.THANKS_AFTER - 1):
            self.auction(f"Meeting {n}", ended_days_ago=10 + 30 * n, posted_days_ago=10 + 30 * n)
        self.auction("Next month", ended_days_ago=-30)
        club_emails.send_club_emails()
        self.assertEqual(self.sent(send), [])


class ThanksTemplateTests(Helpers):
    def setUp(self):
        super().setUp()
        # Monthly auctions earn the club-tools email first; that one is tested above.
        ClubEmail.objects.create(club=self.club, kind="club_tools")

    def test_it_renders(self):
        for n in range(club_emails.THANKS_AFTER):
            self.auction(f"Meeting {n}", ended_days_ago=10 + 30 * n, posted_days_ago=10 + 30 * n)
        club_emails.send_club_emails()
        email = Email.objects.get(to=self.creator.email, subject="Thanks, Cichlid society")
        self.assertIn("Know of another club", email.message)
        self.assertNotIn("{%", email.message)
