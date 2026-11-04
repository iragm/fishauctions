"""A saved or cookie timezone of UTC is a browser that didn't know where it was, and must not stick."""

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase
from django.urls import reverse

from auctions.context_processors import DEFAULT_USER_TIMEZONE, add_location, add_tz, resolve_timezone
from auctions.helper_functions import placed_timezone
from auctions.models import Auction
from auctions.views.base import browser_timezone


class TimezoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tz", password="x")

    def request(self, cookie=None, user=None):
        request = RequestFactory().get("/")
        request.user = user or self.user
        request.COOKIES = {"user_timezone": cookie} if cookie else {}
        SessionMiddleware(lambda r: r).process_request(request)
        return request

    def save_zone(self, name):
        self.user.userdata.timezone = name
        self.user.userdata.save()
        self.user.refresh_from_db()

    def test_placed_timezone(self):
        self.assertEqual(placed_timezone("America/New_York"), "America/New_York")
        for name in ["UTC", "Etc/UTC", "GMT", "Factory", "Not/AZone", "", None]:
            self.assertIsNone(placed_timezone(name), name)

    def test_a_stale_saved_utc_is_replaced_by_the_browser(self):
        self.save_zone("UTC")
        add_location(self.request("America/New_York"))
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.timezone, "America/New_York")

    def test_a_utc_browser_never_overwrites_a_real_saved_zone(self):
        self.save_zone("America/Chicago")
        add_location(self.request("UTC"))
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.timezone, "America/Chicago")

    def test_a_utc_browser_shows_the_saved_zone(self):
        self.save_zone("America/Chicago")
        request = self.request("Etc/UTC")
        self.assertEqual(resolve_timezone(request, "X"), "America/Chicago")
        self.assertEqual(browser_timezone(request), "America/Chicago")

    def test_a_placed_browser_wins_over_the_saved_zone(self):
        self.save_zone("America/Chicago")
        self.assertEqual(add_tz(self.request("America/New_York"))["user_timezone"], "America/New_York")

    def test_a_utc_browser_with_nothing_better_is_shown_utc(self):
        self.assertEqual(add_tz(self.request("UTC", AnonymousUser()))["user_timezone"], "UTC")

    def test_a_saved_utc_alone_falls_back_to_the_default(self):
        self.save_zone("UTC")
        self.assertEqual(add_tz(self.request())["user_timezone"], DEFAULT_USER_TIMEZONE)

    def test_every_page_rechecks_the_browser_zone_against_the_cookie(self):
        self.client.cookies["user_timezone"] = "UTC"
        response = self.client.get(reverse("auctions"))
        self.assertContains(response, 'tz !== "UTC"')
        self.assertContains(response, 'setSiteCookie("user_timezone", tz)')

    def test_an_auction_by_a_creator_saved_as_utc_uses_the_site_zone(self):
        self.save_zone("UTC")
        auction = Auction(created_by=self.user, title="t")
        self.assertEqual(str(auction.timezone), settings.TIME_ZONE)

    def test_a_hostile_cookie_never_reaches_the_page(self):
        self.client.cookies["user_timezone"] = '"</script><script>alert(1)//'
        response = self.client.get(reverse("auctions"))
        self.assertNotContains(response, "alert(1)")
        self.assertContains(response, 'tz !== ""')

    def test_forms_survive_a_user_without_userdata(self):
        self.user.userdata.delete()
        user = User.objects.get(pk=self.user.pk)
        self.assertEqual(browser_timezone(self.request("UTC", user)), "UTC")
