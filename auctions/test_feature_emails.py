"""Feature emails (``auctions/feature_emails.py``), the outreach queue and ``record_outreach`` on
``/mcp/admin/``, and the club-website embed's ``?src=``."""

import datetime
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, override_settings
from django.utils import timezone

from auctions import feature_emails
from auctions.club_health import ClubHealth
from auctions.mcp import admin, admin_reports
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubHistory,
    ClubMember,
    FeatureEmail,
    FeatureEmailPause,
    Invoice,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase

SEND = "auctions.feature_emails.mail.send"
STARTED = override_settings(FEATURE_EMAILS_START=(timezone.localdate() - datetime.timedelta(days=30)).isoformat())


def _seen(user, days_ago):
    UserData.objects.filter(user=user).update(last_activity=timezone.now() - datetime.timedelta(days=days_ago))


class Helpers(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.organizer = User.objects.create_user("fe_organizer", "fe_organizer@example.com", "pw")
        cls.owner = User.objects.create_superuser("fe_owner", "fe_owner@example.com", "pw")

    def setUp(self):
        super().setUp()
        feature_emails._quiet_cache.clear()

    def ended_auction(self, **extra):
        now = timezone.now()
        auction = Auction.objects.create(
            title="Card checkout swap",
            created_by=self.organizer,
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now - datetime.timedelta(days=1),
            **extra,
        )
        location = PickupLocation.objects.create(name="Hall", auction=auction)
        tos = AuctionTOS.objects.create(
            auction=auction, pickup_location=location, manually_added=True, email="b@x.test"
        )
        Invoice.objects.get_or_create(auctiontos_user=tos, auction=auction)
        return auction

    def sent_to(self, send, user):
        return [call for call in send.call_args_list if call.args[0] == user.email]

    def run_report(self, name, params=None):
        request = RequestFactory().get("/mcp/admin/")
        request.user = self.owner
        return admin_reports.__dict__[name](request, params or {})


class CapTests(Helpers):
    @override_settings(FEATURE_EMAILS_START="2027-01-04")
    def test_ramp(self):
        start = datetime.date(2027, 1, 4)
        self.assertEqual(feature_emails.daily_cap(start - datetime.timedelta(days=1)), 0)
        self.assertEqual(feature_emails.daily_cap(start), 200)
        self.assertEqual(feature_emails.daily_cap(start + datetime.timedelta(days=13)), 200)
        self.assertEqual(feature_emails.daily_cap(start + datetime.timedelta(days=14)), 400)
        self.assertEqual(feature_emails.daily_cap(start + datetime.timedelta(days=21)), 800)
        self.assertEqual(feature_emails.daily_cap(start + datetime.timedelta(days=400)), feature_emails.RAMP_MOST)

    @override_settings(FEATURE_EMAILS_START="")
    def test_blank_turns_them_off(self):
        self.assertEqual(feature_emails.daily_cap(timezone.localdate()), 0)

    @override_settings(FEATURE_EMAILS_START="2099-01-01")
    @patch(SEND)
    def test_nothing_goes_before_the_start(self, send):
        self.ended_auction()
        self.assertEqual(feature_emails.send_feature_emails(), 0)
        send.assert_not_called()


@STARTED
class SendTests(Helpers):
    @patch(SEND)
    def test_card_checkout_goes_once_with_a_tracked_link(self, send):
        self.ended_auction()
        feature_emails.send_feature_emails()
        calls = self.sent_to(send, self.organizer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["template"], "feature_email")
        self.assertIn("src=email_card_checkout", calls[0].kwargs["context"]["url"])
        self.assertTrue(FeatureEmail.objects.filter(user=self.organizer, kind="card_checkout").exists())
        send.reset_mock()
        feature_emails.send_feature_emails()
        self.assertEqual(self.sent_to(send, self.organizer), [])

    @patch(SEND)
    def test_an_auction_taking_cards_gets_nothing(self, send):
        self.ended_auction(enable_square_payments=True)
        feature_emails.send_feature_emails()
        self.assertFalse(FeatureEmail.objects.filter(user=self.organizer, kind="card_checkout").exists())

    @patch(SEND)
    def test_the_preference_and_unsubscribe_stop_them(self, send):
        self.ended_auction()
        UserData.objects.filter(user=self.organizer).update(email_me_about_new_features=False)
        feature_emails.send_feature_emails()
        self.assertEqual(self.sent_to(send, self.organizer), [])
        data = UserData.objects.get(user=self.organizer)
        data.email_me_about_new_features = True
        data.save()
        data.unsubscribe_from_all()
        self.assertFalse(UserData.objects.get(user=self.organizer).email_me_about_new_features)

    @patch(SEND)
    def test_one_per_person_per_spacing(self, send):
        FeatureEmail.objects.create(user=self.organizer, kind="speakers")
        self.ended_auction()
        feature_emails.send_feature_emails()
        self.assertEqual(self.sent_to(send, self.organizer), [])

    @patch(SEND)
    def test_quiet_people_get_the_palette_email(self, send):
        quiet = User.objects.create_user("fe_quiet", "fe_quiet@example.com", "pw")
        busy = User.objects.create_user("fe_busy", "fe_busy@example.com", "pw")
        gone = User.objects.create_user("fe_gone", "fe_gone@example.com", "pw")
        _seen(quiet, 60)
        _seen(busy, 1)
        _seen(gone, 900)
        feature_emails.send_feature_emails()
        kinds = dict(FeatureEmail.objects.filter(user__in=[quiet, busy, gone]).values_list("user__username", "kind"))
        self.assertEqual(kinds, {"fe_quiet": "palette"})

    @patch(SEND)
    def test_the_library_only_goes_to_people_who_have_it(self, send):
        reader = User.objects.create_user("fe_reader", "fe_reader@example.com", "pw")
        UserData.objects.filter(user=reader).update(library_enabled=True)
        _seen(reader, 60)
        feature_emails.send_feature_emails()
        self.assertEqual(FeatureEmail.objects.get(user=reader).kind, "library")

    @patch(SEND)
    def test_dues_go_to_staff_who_can_take_money(self, send):
        club = Club.objects.create(name="Dues club")
        treasurer = User.objects.create_user("fe_treasurer", "fe_treasurer@example.com", "pw")
        ClubMember.objects.create(club=club, user=treasurer, permission_admin=True)
        for n in range(feature_emails.DUES_MIN_MEMBERS):
            member = User.objects.create_user(f"fe_member{n}", f"fe_member{n}@example.com", "pw")
            ClubMember.objects.create(club=club, user=member)
        feature_emails.send_feature_emails()
        self.assertEqual(FeatureEmail.objects.get(user=treasurer).kind, "dues")
        self.assertFalse(FeatureEmail.objects.filter(user__username="fe_member0", kind="dues").exists())

    @patch(SEND)
    def test_the_cap_stops_a_run(self, send):
        for n in range(3):
            _seen(User.objects.create_user(f"fe_cap{n}", f"fe_cap{n}@example.com", "pw"), 60)
        with patch.object(feature_emails, "daily_cap", return_value=1):
            self.assertEqual(feature_emails.send_feature_emails(), 1)

    @patch(SEND)
    def test_opt_outs_pause_everything(self, send):
        for n in range(feature_emails.WATCH_MIN_SENT):
            user = User.objects.create_user(f"fe_watch{n}", f"fe_watch{n}@example.com", "pw")
            FeatureEmail.objects.create(user=user, kind="palette")
        UserData.objects.filter(user__username__in=["fe_watch0", "fe_watch1"]).update(has_unsubscribed=True)
        self.ended_auction()
        self.assertEqual(feature_emails.send_feature_emails(), 0)
        self.assertTrue(FeatureEmailPause.objects.exists())
        self.assertEqual(self.sent_to(send, self.organizer), [])

    def test_the_template_renders(self):
        from post_office.models import EmailTemplate

        template = EmailTemplate.objects.get(name="feature_email", language="")
        self.assertIn("{{ sentence }}", template.content)
        self.assertIn("/notifications/", template.html_content)


class OutreachQueueTests(Helpers):
    def test_who_is_on_it_and_which_email(self):
        old = timezone.now() - datetime.timedelta(days=900)
        cold = Club.objects.create(name="Cold club", contact_email="sec@cold.test", contact_method="email")
        mga = Club.objects.create(name="MGA club", homepage="https://mga.test", other_software="mygroupauctions")
        stale = Club.objects.create(name="Stale club", contact_method="webform", date_contacted=old)
        Club.objects.create(name="Recent club", contact_method="email", date_contacted=timezone.now())
        Club.objects.create(name="Folded club", contact_method="email", stall_reason="folded")
        Club.objects.create(name="Unreachable club")
        busy = Club.objects.create(name="Busy club", contact_method="email")
        ClubHealth.objects.create(club=busy, stage="active", real_auctions=5)
        aware = Club.objects.create(name="Aware club", contact_method="facebook")
        ClubHealth.objects.create(club=aware, stage="aware")
        result = self.run_report("outreach_queue", {"limit": 50})
        rows = {row["name"]: row for row in result["clubs"]}
        for name in ("«Cold club»", "«MGA club»", "«Stale club»", "«Aware club»"):
            self.assertIn(name, rows)
        for name in ("«Recent club»", "«Folded club»", "«Unreachable club»", "«Busy club»"):
            self.assertNotIn(name, rows)
        self.assertEqual(rows["«Cold club»"]["email_version"], "a")
        self.assertEqual(rows["«MGA club»"]["email_version"], "b")
        self.assertTrue(rows["«MGA club»"]["possible_mygroupauctions"])
        self.assertEqual(rows["«Stale club»"]["last_contacted"], old.date().isoformat())
        names = [row["name"] for row in result["clubs"]]
        self.assertLess(names.index("«Aware club»"), names.index("«Cold club»"))
        self.assertTrue(rows["«Cold club»"]["has_contact_email"])
        self.assertNotIn("sec@cold.test", json.dumps(admin.private_result(result)))
        self.assertTrue(rows["«Cold club»"]["admin_url"].endswith(f"/admin/auctions/club/{cold.pk}/change/"))
        self.assertIsNotNone(mga.pk and stale.pk)

    def test_record_outreach_takes_it_off_and_keeps_a_listed_club_listed(self):
        club = Club.objects.create(name="Listed club", contact_method="email", outreach_stage=Club.LISTED)
        request = RequestFactory().post("/")
        request.user = self.owner
        result = admin.record_outreach(request, {"club": str(club.pk), "version": "b", "stall_reason": "uses_other"})
        self.assertNotIn("error", result)
        club.refresh_from_db()
        self.assertEqual(club.outreach_stage, Club.LISTED)
        self.assertEqual(club.stall_reason, "uses_other")
        self.assertIsNotNone(club.date_contacted)
        self.assertTrue(ClubHistory.objects.filter(club=club, action__contains="Outreach email B").exists())
        names = [row["name"] for row in self.run_report("outreach_queue", {"limit": 50})["clubs"]]
        self.assertNotIn("«Listed club»", names)

    def test_record_outreach_moves_a_prospect_to_contacted(self):
        club = Club.objects.create(name="Prospect club", contact_method="email")
        request = RequestFactory().post("/")
        request.user = self.owner
        admin.record_outreach(request, {"club": str(club.pk)})
        club.refresh_from_db()
        self.assertEqual(club.outreach_stage, Club.CONTACTED)

    def test_update_club_sets_and_checks_other_software(self):
        club = Club.objects.create(name="Software club")
        request = RequestFactory().post("/")
        request.user = self.owner
        self.assertIn("error", json.dumps(admin.update_club(request, {"club": str(club.pk), "other_software": "x"})))
        admin.update_club(request, {"club": str(club.pk), "other_software": "MyGroupAuctions"})
        club.refresh_from_db()
        self.assertEqual(club.other_software, "mygroupauctions")
        admin.update_club(request, {"club": str(club.pk), "other_software": ""})
        club.refresh_from_db()
        self.assertEqual(club.other_software, "")


class ResultsTests(Helpers):
    @STARTED
    def test_counts_per_kind(self):
        clicker = User.objects.create_user("fe_clicker", "fe_clicker@example.com", "pw")
        quitter = User.objects.create_user("fe_quitter", "fe_quitter@example.com", "pw")
        FeatureEmail.objects.create(user=clicker, kind="palette")
        FeatureEmail.objects.create(user=quitter, kind="palette")
        UserData.objects.filter(user=quitter).update(email_me_about_new_features=False)
        from auctions.models import PageView

        PageView.objects.create(user=clicker, source="email_palette", url="/")
        result = self.run_report("feature_email_results")
        row = next(row for row in result["by_kind"] if row["kind"] == "palette")
        self.assertEqual((row["sent"], row["clicked"], row["opted_out"]), (2, 1, 1))
        self.assertEqual(result["cap_today"], 1600)
