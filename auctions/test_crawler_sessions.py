import re
import time
from datetime import date
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.core.cache import caches
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from redis.exceptions import TimeoutError as RedisTimeoutError

from auctions.crawlers import SQL_PREFILTER, current_chrome_major, is_automated_browser, is_bot_page_view, is_crawler
from auctions.models import PageView
from auctions.session_store import CACHE_MAX_AGE, SessionStore
from auctions.tests import StandardTestCase

# Kept current: a stock desktop Chrome a year out of date is a scraper (is_automated_browser).
BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{current_chrome_major()}.0.0.0 Safari/537.36"
)
META = (
    BROWSER + " (compatible; meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler))"
)
AMAZON = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Amazonbot/0.1; "
    "+https://developer.amazon.com/support/amazonbot) Chrome/119.0.6045.214 Safari/537.36"
)


class IsCrawlerTests(StandardTestCase):
    def _ua(self, user_agent):
        return is_crawler(RequestFactory().get("/", HTTP_USER_AGENT=user_agent))

    def test_crawlers(self):
        for user_agent in (
            META,
            AMAZON,
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
            "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)",
            "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        ):
            with self.subTest(user_agent=user_agent):
                self.assertTrue(self._ua(user_agent))

    def test_people(self):
        for user_agent in (
            BROWSER,
            "Mozilla/5.0 (Linux; Android 11; CUBOT KINGKONG 5 Pro) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
            "",
        ):
            with self.subTest(user_agent=user_agent):
                self.assertFalse(self._ua(user_agent))


def desktop_chrome(major, os="Windows NT 10.0; Win64; x64", edge=False):
    tail = f" Edg/{major}.0.0.0" if edge else ""
    return f"Mozilla/5.0 ({os}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36{tail}"


class AutomatedBrowserTests(StandardTestCase):
    TODAY = date(2026, 11, 15)
    MAC = "Macintosh; Intel Mac OS X 10_15_7"

    def test_the_release_clock(self):
        self.assertEqual(current_chrome_major(self.TODAY), 157)
        # Counting forward runs slow: a year on is 168 at four weeks, 168 or 169 with a winter pause.
        self.assertLessEqual(current_chrome_major(date(2027, 11, 15)), 168)
        # Counting back runs low (Chrome 120 shipped 2023-12-05), so an old row is kept, not deleted.
        self.assertLessEqual(current_chrome_major(date(2023, 12, 5)), 120)

    def test_scrapers(self):
        for user_agent in (
            desktop_chrome(145),
            desktop_chrome(116),
            desktop_chrome(120, os=self.MAC),
            desktop_chrome(119, edge=True),
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/99.0.4844.51 Safari/537.36",
            "Lightpanda/1.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/157.0.0.0 "
            "Safari/537.36",
        ):
            with self.subTest(user_agent=user_agent):
                self.assertTrue(is_automated_browser(user_agent, self.TODAY))

    def test_people(self):
        for user_agent in (
            desktop_chrome(157),
            desktop_chrome(146),
            desktop_chrome(150, edge=True),
            # The last Chrome for macOS 10.13-10.14 and for 10.15: those Macs cannot update.
            desktop_chrome(116, os=self.MAC),
            desktop_chrome(128, os=self.MAC),
            # Windows 7 stops at Chrome 109, and says so.
            desktop_chrome(109, os="Windows NT 6.1; Win64; x64"),
            # Old phones stop updating too.
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 "
            "Mobile Safari/537.36",
            # Another product token: Opera.
            desktop_chrome(120) + " OPR/106.0.0.0",
            # Edge that does not match its Chrome.
            desktop_chrome(120) + " Edg/157.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Firefox/115.0",
            "",
        ):
            with self.subTest(user_agent=user_agent):
                self.assertFalse(is_automated_browser(user_agent, self.TODAY))

    def test_the_beacon_records_nothing_for_one(self):
        response = self.client.post(
            reverse("pageview"), {"first_view": "true", "url": "/lots/"}, HTTP_USER_AGENT=desktop_chrome(99)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PageView.objects.exists())

    def test_the_sql_prefilter_lets_through_everything_the_patterns_catch(self):
        """The cleanup job's REGEXP is a hand copy of the patterns; it must never be narrower."""
        for user_agent in (
            desktop_chrome(99),
            desktop_chrome(120, os=self.MAC),
            desktop_chrome(119, edge=True),
            "Lightpanda/1.0",
            "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)",
            "Mozilla/5.0 (compatible; Yahoo! Slurp; http://help.yahoo.com/help/us/ysearch/slurp)",
            "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Mozilla/5.0 (compatible; meta-externalagent/1.1)",
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) BingPreview/1.0b",
            "Some Crawler/1.0",
        ):
            with self.subTest(user_agent=user_agent):
                self.assertTrue(is_bot_page_view(user_agent, timezone.now()))
                self.assertRegex(user_agent, re.compile(SQL_PREFILTER, re.IGNORECASE))

    def test_the_last_chrome_for_big_sur_is_a_person(self):
        self.assertFalse(is_automated_browser(desktop_chrome(138, os=self.MAC), self.TODAY))

    def test_a_signed_in_member_on_an_old_browser_is_still_counted(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("pageview"), {"first_view": "true", "url": "/lots/"}, HTTP_USER_AGENT=desktop_chrome(99)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PageView.objects.filter(user=self.user).exists())

    def test_a_scraper_still_gets_a_session(self):
        """Only page views are filtered: a person caught by mistake can still sign in."""
        response = self.client.get(reverse("allLots"), HTTP_USER_AGENT=desktop_chrome(99))
        self.assertEqual(response.status_code, 200)
        self.assertIn("sessionid", response.cookies)


class CrawlerSessionTests(StandardTestCase):
    def test_a_crawler_page_load_stores_no_session(self):
        response = self.client.get(reverse("allLots"), HTTP_USER_AGENT=META)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sessionid", response.cookies)
        self.assertFalse(Session.objects.exists())

    def test_a_browser_page_load_still_gets_one(self):
        response = self.client.get(reverse("allLots"), HTTP_USER_AGENT=BROWSER)
        self.assertIn("sessionid", response.cookies)
        self.assertTrue(Session.objects.exists())

    def test_a_crawler_beacon_records_nothing(self):
        response = self.client.post(
            reverse("pageview"), {"first_view": "true", "url": "/lots/"}, HTTP_USER_AGENT=AMAZON
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(PageView.objects.exists())
        self.assertFalse(Session.objects.exists())


class SessionCacheOutageTests(StandardTestCase):
    """A Redis that doesn't answer costs the cache, not the page."""

    def _redis_down(self):
        backend = type(caches["default"])
        stalled = {"side_effect": RedisTimeoutError("Timeout reading from socket")}
        return (
            patch.object(backend, "has_key", **stalled),
            patch.object(backend, "get", **stalled),
            patch.object(backend, "set", **stalled),
        )

    def test_a_first_visit_gets_a_session(self):
        has_key, get, set_ = self._redis_down()
        with has_key, get, set_:
            response = self.client.get(reverse("allLots"), HTTP_USER_AGENT=BROWSER)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Session.objects.exists())

    def _page_view(self):
        return self.client.post(reverse("pageview"), {"first_view": "true", "url": "/lots/"}, HTTP_USER_AGENT=BROWSER)

    def test_the_page_view_beacon_records_the_view(self):
        """The beacon's per-address budget lives in the cache; a stall there skips the budget, not the view."""
        has_key, get, set_ = self._redis_down()
        add = patch.object(type(caches["default"]), "add", side_effect=RedisTimeoutError("Timeout reading from socket"))
        with has_key, get, set_, add:
            response = self._page_view()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PageView.objects.count(), 1)

    def test_a_stall_after_the_read_records_the_view_too(self):
        backend = type(caches["default"])
        with patch.object(backend, "incr", side_effect=RedisTimeoutError("Timeout reading from socket")):
            response = self._page_view()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PageView.objects.count(), 1)

    def test_an_existing_session_is_read_from_the_row(self):
        session = SessionStore()
        session["status"] = "started"
        session.create()
        has_key, get, set_ = self._redis_down()
        with has_key, get, set_:
            self.assertEqual(SessionStore(session.session_key).load(), {"status": "started"})
            self.assertTrue(SessionStore().exists(session.session_key))


class SessionCacheLifetimeTests(StandardTestCase):
    def _cached_ttl(self, session):
        # The test runner's locmem cache keeps an absolute expiry per key.
        cache = caches["default"]
        return cache._expire_info[cache.make_key(session.cache_key)] - time.time()

    def test_a_year_long_session_leaves_redis_after_two_weeks_idle(self):
        session = SessionStore()
        session["status"] = "started"
        session.create()
        self.assertGreater(session.get_expiry_age(), CACHE_MAX_AGE)
        self.assertTrue(0 < self._cached_ttl(session) <= CACHE_MAX_AGE)

    def test_a_refill_from_the_row_is_capped_too(self):
        session = SessionStore()
        session["status"] = "started"
        session.create()
        caches["default"].delete(session.cache_key)
        SessionStore(session.session_key).load()
        self.assertTrue(0 < self._cached_ttl(session) <= CACHE_MAX_AGE)
