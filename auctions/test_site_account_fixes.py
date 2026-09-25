"""Regression tests for a sweep of site and account fixes.

The unauthenticated beacons' per-address budget and the ``src`` length; the anonymous session lifetime
surviving a non-password sign-in; full-row ``UserData`` saves from stale instances; location cookies
and coordinates that aren't numbers; the Discord join message posting into any channel the bot can
see; the traffic charts' ``?days`` and row-by-row counting; what account deletion misses and where it
calls Apple; feedback ratings; a Square refund on a club invoice; JSON bodies that aren't objects; the
signups chart's start date; the data export's chat lines; and plain-http CSRF origins outside DEBUG.
"""

import datetime
import json
import os
import re
import subprocess
import sys
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from auctions import account_export
from auctions.account_deletion import delete_account
from auctions.context_processors import add_location, dismissed_cookies_tos
from auctions.models import (
    AssistantSkillRequest,
    Category,
    Club,
    ClubMember,
    FormFailure,
    Invoice,
    InvoicePayment,
    LLMUsage,
    LotHistory,
    PageView,
    RemotePrintJob,
    SignInStitch,
    Speaker,
    SpeakerComment,
    SpeakerTag,
    UserAPIKey,
    UserData,
    VoiceCommandLog,
)
from auctions.tests import StandardTestCase


def userdata_columns_written(queries):
    """The columns every ``UPDATE auctions_userdata`` in ``queries`` set."""
    columns = set()
    for query in queries.captured_queries:
        sql = query["sql"]
        if sql.startswith("UPDATE `auctions_userdata` SET"):
            set_clause = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            columns.update(re.findall(r"`(\w+)` =", set_clause))
    return columns


class BeaconBudgetTests(StandardTestCase):
    """A cookieless caller made a session row and a PageView -- kept forever -- per POST."""

    def _beacon(self, address="10.0.0.1", **data):
        self.client.cookies.clear()
        return self.client.post(
            reverse("pageview"), {"first_view": "true", "url": "/lots/", **data}, REMOTE_ADDR=address
        )

    @patch("auctions.views.ajax.PAGE_VIEWS_PER_ADDRESS_PER_MINUTE", 3)
    def test_an_address_past_its_budget_writes_nothing(self):
        for _ in range(3):
            self.assertEqual(self._beacon().status_code, 200)
        views, sessions = PageView.objects.count(), Session.objects.count()
        response = self._beacon()
        self.assertEqual(response.status_code, 204)
        self.assertEqual(PageView.objects.count(), views)
        self.assertEqual(Session.objects.count(), sessions)

    @patch("auctions.views.ajax.PAGE_VIEWS_PER_ADDRESS_PER_MINUTE", 1)
    def test_each_address_has_its_own_budget(self):
        self._beacon("10.0.0.1")
        self.assertEqual(self._beacon("10.0.0.1").status_code, 204)
        self.assertEqual(self._beacon("10.0.0.2").status_code, 200)
        self.assertEqual(PageView.objects.count(), 2)

    @patch("auctions.views.ajax.ABANDONED_FORMS_PER_ADDRESS_PER_MINUTE", 1)
    def test_the_abandonment_beacon_has_a_budget_too(self):
        from auctions import form_friction

        for name in ("AuctionEditForm", "ClubEditForm"):
            self.client.cookies.clear()
            self.client.post(
                reverse("form_abandoned"),
                {"token": form_friction.abandon_token(name), "fields": "tax", "seconds": "5"},
                REMOTE_ADDR="10.0.0.3",
            )
        self.assertEqual(FormFailure.objects.count(), 1)

    def test_a_long_src_is_cut_to_the_column(self):
        """PageView.source is 200 characters, and STRICT mode refuses a longer one."""
        self.assertEqual(self._beacon(src="x" * 300).status_code, 200)
        self.assertEqual(PageView.objects.get().source, "x" * 200)

    def test_a_uid_only_touches_last_activity(self):
        with CaptureQueriesContext(connection) as queries:
            self._beacon(uid=self.userB.userdata.unsubscribe_link)
        self.assertEqual(userdata_columns_written(queries), {"last_activity"})


class SignedInSessionLifetimeTests(StandardTestCase):
    """login() keeps the anonymous session's 14-day expiry; only allauth's password form reset it."""

    def test_a_sign_in_drops_the_anonymous_expiry(self):
        request = RequestFactory().get("/")
        SessionMiddleware(lambda request: None).process_request(request)
        request.session.set_expiry(settings.ANONYMOUS_SESSION_COOKIE_AGE)
        request.session.save()
        request.user = AnonymousUser()
        login(request, self.userB, backend="django.contrib.auth.backends.ModelBackend")
        self.assertNotIn("_session_expiry", request.session)
        self.assertEqual(request.session.get_expiry_age(), settings.SESSION_COOKIE_AGE)


class StaleUserDataTests(StandardTestCase):
    """Saves from a UserData read at the start of the request write only what they changed."""

    def _request(self, user, cookies=None):
        request = RequestFactory().get("/")
        SessionMiddleware(lambda request: None).process_request(request)
        # A fresh read, so request.user.userdata is this request's own (soon stale) copy.
        request.user = User.objects.get(pk=user.pk)
        request.user.userdata  # noqa: B018 - load it before the concurrent write below
        request.COOKIES.update(cookies or {})
        return request

    def test_add_location_leaves_other_fields_alone(self):
        request = self._request(self.userB, {"latitude": "40.5", "longitude": "-75.25"})
        UserData.objects.filter(user=self.userB).update(use_dark_theme=False)
        add_location(request)
        userdata = UserData.objects.get(user=self.userB)
        self.assertFalse(userdata.use_dark_theme)
        self.assertEqual(userdata.last_ip_address, "127.0.0.1")

    def test_dismissing_the_cookie_banner_leaves_other_fields_alone(self):
        request = self._request(self.userB, {"hide_tos_banner": "1"})
        UserData.objects.filter(user=self.userB).update(use_dark_theme=False)
        dismissed_cookies_tos(request)
        userdata = UserData.objects.get(user=self.userB)
        self.assertTrue(userdata.dismissed_cookies_tos)
        self.assertFalse(userdata.use_dark_theme)

    def test_the_landing_redirect_only_writes_last_activity(self):
        self.client.force_login(self.userB)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("home"))
        columns = userdata_columns_written(queries)
        self.assertIn("last_activity", columns)
        # The page's context processors record the address too; a full-row save writes every column.
        self.assertLessEqual(columns, {"last_activity", "last_ip_address"})

    def test_enabling_sale_notifications_only_writes_that(self):
        self.client.force_login(self.userB)
        with CaptureQueriesContext(connection) as queries:
            self.client.post(reverse("enable_notifications"))
        self.assertEqual(userdata_columns_written(queries), {"push_notifications_when_lots_sell"})
        self.assertTrue(UserData.objects.get(user=self.userB).push_notifications_when_lots_sell)


class LocationCookieTests(StandardTestCase):
    def _run(self, latitude, longitude):
        request = RequestFactory().get("/")
        SessionMiddleware(lambda request: None).process_request(request)
        request.user = User.objects.get(pk=self.userB.pk)
        request.COOKIES.update({"latitude": latitude, "longitude": longitude})
        return add_location(request)

    def test_a_cookie_that_is_not_a_coordinate_is_ignored(self):
        for latitude, longitude in (("nan", "1"), ("95", "10"), ("abc", "10"), ("1e400", "10")):
            with self.subTest(latitude):
                self.assertEqual(self._run(latitude, longitude), {"has_user_location": False})
                self.assertEqual(UserData.objects.get(user=self.userB).latitude, 0)

    def test_a_good_cookie_is_saved(self):
        self.assertEqual(self._run("40.5", "-75.25"), {"has_user_location": True})
        userdata = UserData.objects.get(user=self.userB)
        self.assertEqual((userdata.latitude, userdata.longitude), (40.5, -75.25))


class SetCoordinatesTests(StandardTestCase):
    URL = "/api/users/location/"

    def setUp(self):
        super().setUp()
        self.client.force_login(self.userB)

    def test_missing_or_junk_coordinates_are_a_400(self):
        for data in ({}, {"latitude": "40"}, {"latitude": "x", "longitude": "1"}, {"latitude": "91", "longitude": "1"}):
            with self.subTest(data):
                self.assertEqual(self.client.post(self.URL, data).status_code, 400)
        self.assertIsNone(UserData.objects.get(user=self.userB).location_coordinates)

    def test_good_coordinates_are_saved_and_nothing_else(self):
        UserData.objects.filter(user=self.userB).update(use_dark_theme=False)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(self.URL, {"latitude": "40.5", "longitude": "-75.25"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(userdata_columns_written(queries), {"location_coordinates", "latitude", "longitude"})
        userdata = UserData.objects.get(user=self.userB)
        self.assertEqual((userdata.latitude, userdata.longitude), (40.5, -75.25))
        self.assertFalse(userdata.use_dark_theme)


class RenderAdTests(StandardTestCase):
    def test_a_category_that_is_not_a_number_is_ignored(self):
        self.assertEqual(self.client.get(reverse("get_ad"), {"category": "abc"}).status_code, 200)

    def test_a_real_category_still_works(self):
        category = Category.objects.create(name="Ad fish")
        self.assertEqual(self.client.get(reverse("get_ad"), {"category": category.pk}).status_code, 200)


@override_settings(DISCORD_BOT_TOKEN="bot-token")
class DiscordJoinMessageChannelTests(StandardTestCase):
    """The bot sits in every club's server; the channel has to be this club's."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Join Message Club", discord_server_id="111")
        ClubMember.objects.create(club=self.club, user=self.userB, name="Admin", permission_admin=True)
        self.client.force_login(self.userB)

    def _send(self, channel_id):
        return self.client.post(
            reverse("club_discord_send_join_message", kwargs={"slug": self.club.slug}), {"channel_id": channel_id}
        )

    @staticmethod
    def _channel(guild_id, status_code=200):
        response = MagicMock(status_code=status_code)
        response.json.return_value = {"id": "222", "guild_id": guild_id}
        return response

    @patch("auctions.views.discord.requests.post")
    @patch("auctions.views.discord.requests.get")
    def test_a_channel_id_that_is_not_a_number_calls_nobody(self, get, post):
        for channel_id in ("chan", "123/../../guilds/9", "12 3", "١٢٣"):
            with self.subTest(channel_id):
                self.assertEqual(self._send(channel_id).status_code, 302)
        get.assert_not_called()
        post.assert_not_called()

    @patch("auctions.views.discord.requests.post")
    @patch("auctions.views.discord.requests.get")
    def test_a_channel_in_another_server_is_refused(self, get, post):
        get.return_value = self._channel("999")
        self._send("222")
        self.assertEqual(get.call_args.args[0], "https://discord.com/api/v10/channels/222")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bot bot-token")
        self.assertIn("timeout", get.call_args.kwargs)
        post.assert_not_called()

    @patch("auctions.views.discord.requests.post")
    @patch("auctions.views.discord.requests.get")
    def test_a_channel_the_bot_cannot_see_is_refused(self, get, post):
        get.return_value = self._channel(None, status_code=404)
        self._send("222")
        post.assert_not_called()

    @patch("auctions.views.discord.requests.post")
    @patch("auctions.views.discord.requests.get")
    def test_a_club_with_no_server_calls_nobody(self, get, post):
        Club.objects.filter(pk=self.club.pk).update(discord_server_id="")
        self._send("222")
        get.assert_not_called()
        post.assert_not_called()

    @patch("auctions.views.discord.requests.post")
    @patch("auctions.views.discord.requests.get")
    def test_a_channel_in_this_clubs_server_gets_the_message(self, get, post):
        get.return_value = self._channel("111")
        post.return_value = MagicMock(status_code=200)
        self._send(" 222 ")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://discord.com/api/v10/channels/222/messages")


class TrafficChartTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser("traffic_boss", "traffic@example.com", "x")
        self.client.force_login(self.superuser)

    def _view(self, days_ago=0, **fields):
        view = PageView.objects.create(url="/lots/", **fields)
        PageView.objects.filter(pk=view.pk).update(date_start=timezone.now() - datetime.timedelta(days=days_ago))
        return view

    def test_days_are_clamped(self):
        for days, expected in (("100000", 365), ("-5", 1), ("0", 1), ("30", 30)):
            with self.subTest(days):
                data = self.client.get(reverse("admin_traffic_json"), {"days": days}).json()
                self.assertEqual(len(data["labels"]), expected)
                self.assertEqual(len(data["datasets"][0]["data"]), expected)
        self.assertEqual(self.client.get(reverse("admin_traffic"), {"days": "100000"}).context["days"], 365)
        response = self.client.get(reverse("admin_traffic_time_of_day_json"), {"days": "-5"})
        self.assertEqual(response.status_code, 200)

    def test_views_are_counted_per_day_today_first(self):
        self._view(0)
        self._view(0)
        self._view(3)
        self._view(30)
        data = self.client.get(reverse("admin_traffic_json"), {"days": "7"}).json()["datasets"][0]["data"]
        self.assertEqual(data, [2, 0, 0, 1, 0, 0, 0])

    @patch("auctions.views.site_admin.MAX_HEAT_MAP_POINTS", 2)
    def test_the_heat_map_is_capped(self):
        for _ in range(3):
            self._view(0, latitude=40, longitude=-75)
        points = self.client.get(reverse("admin_traffic")).context["pageviews"]
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0], {"latitude": 40, "longitude": -75})

    @override_settings(TIME_ZONE="America/New_York")
    def test_the_signups_chart_starts_on_the_local_date(self):
        """10pm in New York is tomorrow in UTC; the start came from the UTC date and lost a day."""
        evening = datetime.datetime(2026, 1, 10, 3, 0, tzinfo=datetime.UTC)
        with patch("django.utils.timezone.now", return_value=evening):
            labels = self.client.get(reverse("admin_user_signups_json"), {"days": "3"}).json()["labels"]
        self.assertEqual(labels, ["Jan 6, 2026", "Jan 7, 2026", "Jan 8, 2026", "Jan 9, 2026"])


class UserChartTests(StandardTestCase):
    def test_counts_come_from_the_database_whatever_the_row_count(self):
        from auctions.models import Bid, Lot

        fish = Category.objects.create(name="Counted fish")
        Lot.objects.filter(pk=self.lot.pk).update(species_category=fish)
        superuser = User.objects.create_superuser("chart_counter", "counter@example.com", "x")
        self.client.force_login(superuser)
        PageView.objects.create(user=self.userB, lot_number=self.lot)
        Bid.objects.create(user=self.userB, lot_number=self.lot, amount=5)
        with CaptureQueriesContext(connection) as few:
            self.client.get(f"/api/chart/users/{self.userB.pk}/")
        for _ in range(20):
            PageView.objects.create(user=self.userB, lot_number=self.lot)
        with CaptureQueriesContext(connection) as many:
            data = self.client.get(f"/api/chart/users/{self.userB.pk}/").json()
        self.assertEqual(len(many), len(few))
        self.assertEqual(data, {"labels": ["Counted fish"], "bids": [1], "views": [21]})


class AccountDeletionCoversEverythingTests(StandardTestCase):
    def _oauth_rows(self, user):
        from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken

        application = Application.objects.create(
            name="an agent",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://example.com/callback",
        )
        expires = timezone.now() + datetime.timedelta(hours=1)
        access = AccessToken.objects.create(user=user, application=application, token="access-1", expires=expires)
        RefreshToken.objects.create(user=user, application=application, token="refresh-1", access_token=access)
        Grant.objects.create(
            user=user, application=application, code="grant-1", expires=expires, redirect_uri="https://example.com/"
        )
        return AccessToken, RefreshToken, Grant

    def test_sign_ins_api_keys_and_oauth_tokens_go(self):
        user = self.userB
        _raw, prefix, key_hash = UserAPIKey.generate()
        UserAPIKey.objects.create(user=user, name="laptop", prefix=prefix, key_hash=key_hash)
        SignInStitch.objects.create(user=user, session_id="an-anonymous-session")
        models = self._oauth_rows(user)
        delete_account(user)
        self.assertFalse(UserAPIKey.objects.filter(user=user).exists())
        self.assertFalse(SignInStitch.objects.filter(user=user).exists())
        for model in models:
            with self.subTest(model.__name__):
                self.assertFalse(model.objects.filter(user=user).exists())

    def test_their_own_rows_go_and_the_sites_records_lose_them(self):
        user = self.userB
        speaker = Speaker.objects.create(name="A speaker")
        SpeakerTag.objects.create(speaker=speaker, user=user, tag="funny")
        comment = SpeakerComment.objects.create(speaker=speaker, user=user, body="Great talk")
        RemotePrintJob.objects.create(user=user, lots=[], total_count=0)
        AssistantSkillRequest.objects.create(user=user, skill="refund everything", reason="my own words")
        usage = LLMUsage.objects.create(user=user, query="where is my order from bob@example.com", total_tokens=42)
        voice = VoiceCommandLog.objects.create(auction=self.online_auction, user=user, heard="lot five")

        delete_account(user)

        self.assertFalse(SpeakerTag.objects.filter(user=user).exists())
        self.assertFalse(RemotePrintJob.objects.filter(user=user).exists())
        self.assertFalse(AssistantSkillRequest.objects.filter(user=user).exists())
        usage.refresh_from_db()
        self.assertEqual((usage.user, usage.query, usage.total_tokens), (None, "", 42))
        voice.refresh_from_db()
        self.assertEqual((voice.user, voice.heard), (None, "lot five"))
        comment.refresh_from_db()
        self.assertEqual((comment.user, comment.body), (None, "Great talk"))

    def test_apple_is_called_before_the_transaction_opens(self):
        depth_when_called = []
        baseline = len(connection.savepoint_ids)

        def record(user):
            depth_when_called.append(len(connection.savepoint_ids))
            return 0

        with patch("auctions.apple_signin.revoke_all_for_user", side_effect=record):
            delete_account(self.userB)
        self.assertEqual(depth_when_called, [baseline])


class FeedbackRatingTests(StandardTestCase):
    def _rate(self, username, leave_as, rating):
        self.client.login(username=username, password="testpassword")
        return self.client.post(f"/api/feedback/{self.lot.pk}/{leave_as}/", {"rating": rating})

    def test_only_minus_one_zero_and_one_are_accepted(self):
        for rating in ("7", "abc", "-5", "1.5"):
            with self.subTest(rating):
                self.assertEqual(self._rate("no_tos", "winner", rating).status_code, 200)
                self.lot.refresh_from_db()
                self.assertEqual(self.lot.feedback_rating, 0)
        self._rate("no_tos", "winner", "-1")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.feedback_rating, -1)
        self._rate("no_tos", "winner", "0")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.feedback_rating, 0)

    def test_the_seller_side_is_checked_too(self):
        self._rate("my_lot", "seller", "9")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.winner_feedback_rating, 0)
        self._rate("my_lot", "seller", "1")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.winner_feedback_rating, 1)


class SquareRefundOnAClubInvoiceTests(StandardTestCase):
    """A club renewal invoice has no auction and no bidder, and the refund history line assumed both."""

    def test_the_refund_is_booked(self):
        from decimal import Decimal

        club = Club.objects.create(name="Refund Club")
        invoice = Invoice.objects.create(club=club, buyer=self.userB, status="UNPAID", renewal_needed=True)
        InvoicePayment.objects.create(
            invoice=invoice,
            external_id="SQ-CLUB-PAY",
            amount=Decimal("20.00"),
            amount_available_to_refund=Decimal("20.00"),
            currency="USD",
            payment_method="Square",
        )
        event = {
            "merchant_id": "M1",
            "type": "refund.updated",
            "data": {
                "object": {
                    "refund": {
                        "id": "SQ-CLUB-REFUND",
                        "status": "COMPLETED",
                        "payment_id": "SQ-CLUB-PAY",
                        "amount_money": {"amount": 500, "currency": "USD"},
                    }
                }
            },
        }
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True):
            response = self.client.post(
                reverse("square_webhook"), data=json.dumps(event), content_type="application/json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(InvoicePayment.objects.get(external_id="SQ-CLUB-REFUND").amount, Decimal("-5.00"))


class PaletteBodiesThatAreNotObjectsTests(StandardTestCase):
    def test_a_json_list_is_treated_as_empty(self):
        self.client.force_login(self.userB)
        for name in ("command_palette_cancel", "command_palette_report"):
            for body in ("[1, 2]", "7", '"text"', "null"):
                with self.subTest(name=name, body=body):
                    response = self.client.post(reverse(name), data=body, content_type="application/json")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"recorded": False})


class ExportChatMessagesTests(StandardTestCase):
    def test_only_what_they_said_is_exported(self):
        LotHistory.objects.create(lot=self.lot, user=self.user, message="Is this still available?")
        LotHistory.objects.create(
            lot=self.lot, user=self.user, message="my_lot removed no_tos as the winner", changed_price=True
        )
        messages = [row["message"] for row in account_export.export(self.user)["chat_messages"]]
        self.assertEqual(messages, ["Is this still available?"])


class CsrfTrustedOriginsTests(StandardTestCase):
    """Plain-http local origins only with DEBUG on. Settings are read at import, so import them fresh."""

    def _origins(self, debug):
        env = {
            **os.environ,
            "DEBUG": debug,
            "SECRET_KEY": "a-long-test-secret-that-is-not-a-default-0123456789",
            "DATABASE_PASSWORD": "not-a-default-database-password",
            "REDIS_PASSWORD": "not-a-default-redis-password",
        }
        script = "import json; from fishauctions import settings; print(json.dumps(settings.CSRF_TRUSTED_ORIGINS))"
        result = subprocess.run(  # noqa: S603 - our own interpreter and a fixed script
            [sys.executable, "-c", script],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_production_trusts_no_plain_http_origin(self):
        self.assertFalse([origin for origin in self._origins("False") if origin.startswith("http://")])

    def test_debug_trusts_localhost(self):
        origins = self._origins("True")
        self.assertIn("http://localhost", origins)
        self.assertIn("http://127.0.0.1", origins)
