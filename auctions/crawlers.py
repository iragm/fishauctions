"""Telling a crawler from a visitor by its User-Agent.

``user_agents`` misses meta-externalagent and most AI crawlers, so this is a pattern of its own.
The lookbehind spares CUBOT phones, whose model names end in "BOT".

``is_automated_browser`` is narrower in where it applies and wider in what it catches: scrapers that
run a real browser, execute the page view beacon, and say nothing about being a bot. It is used only
to keep them out of ``PageView``, never to withhold a session, since a person wrongly caught by it
must still be able to sign in.
"""

import re
from datetime import date

from django.utils import timezone

CRAWLER_PATTERN = re.compile(
    r"(?<!cu)bot\b|crawl|spider|slurp|externalagent|externalhit|bingpreview",
    re.IGNORECASE,
)

HEADLESS_PATTERN = re.compile(r"headlesschrome|lightpanda", re.IGNORECASE)

# The untouched desktop Chrome (or Edge) string, the one a scraper copies. Anything with another
# product token (Opera, Yandex, an app's webview) is left alone, as is every phone: an old phone
# cannot update its browser, but desktop Chrome on Windows 10+ and on macOS updates itself.
STOCK_DESKTOP_CHROME = re.compile(
    r"^Mozilla/5\.0 \((?P<os>Windows NT 10\.0; Win64; x64|Macintosh; Intel Mac OS X 10_15_7)\) "
    r"AppleWebKit/537\.36 \(KHTML, like Gecko\) Chrome/(?P<major>\d+)\.[\d.]+ Safari/537\.36"
    r"(?: Edg/(?P=major)\.[\d.]+)?$"
)

# Chrome ships a major version every four weeks; 131 was stable on this date. Before it, real
# releases came a little slower than this clock counts back, so an old row is judged against a
# version a few lower than was current: it errs toward keeping a view, never toward deleting one.
CHROME_RELEASE = (131, date(2024, 11, 12))
CHROME_RELEASE_DAYS = 28
# About eleven months behind. No desktop Chrome that updates itself gets this far back.
STALE_CHROME_VERSIONS = 12
# A Mac always says 10_15_7, but these are the last versions for macOS 10.13-10.14 and 10.15.
LAST_CHROME_FOR_OLD_MACS = {116, 128}


def is_crawler(request):
    return bool(CRAWLER_PATTERN.search(request.META.get("HTTP_USER_AGENT", "")))


def current_chrome_major(today=None):
    version, released = CHROME_RELEASE
    today = today or timezone.now().date()
    return version + (today - released).days // CHROME_RELEASE_DAYS


def is_automated_browser(user_agent, today=None):
    """A headless browser, or stock desktop Chrome a year or more out of date."""
    if HEADLESS_PATTERN.search(user_agent):
        return True
    match = STOCK_DESKTOP_CHROME.match(user_agent)
    if not match:
        return False
    major = int(match["major"])
    if match["os"].startswith("Macintosh") and major in LAST_CHROME_FOR_OLD_MACS:
        return False
    return major <= current_chrome_major(today) - STALE_CHROME_VERSIONS


def is_bot_page_view(user_agent, written):
    """Whether a stored anonymous page view came from a bot, judged as of the moment it was written."""
    user_agent = user_agent or ""
    return bool(CRAWLER_PATTERN.search(user_agent)) or is_automated_browser(user_agent, written.date())
