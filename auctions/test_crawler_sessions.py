import time
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.core.cache import caches
from django.test import RequestFactory
from django.urls import reverse
from redis.exceptions import TimeoutError as RedisTimeoutError

from auctions.crawlers import is_crawler
from auctions.models import PageView
from auctions.session_store import CACHE_MAX_AGE, SessionStore
from auctions.tests import StandardTestCase

BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
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
