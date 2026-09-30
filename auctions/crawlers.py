"""Telling a crawler from a visitor by its User-Agent.

``user_agents`` misses meta-externalagent and most AI crawlers, so this is a pattern of its own.
The lookbehind spares CUBOT phones, whose model names end in "BOT".
"""

import re

CRAWLER_PATTERN = re.compile(
    r"(?<!cu)bot\b|crawl|spider|slurp|externalagent|externalhit|bingpreview",
    re.IGNORECASE,
)


def is_crawler(request):
    return bool(CRAWLER_PATTERN.search(request.META.get("HTTP_USER_AGENT", "")))
