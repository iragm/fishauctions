"""Per-address limits on the public forms and beacons, counted in Redis.

A Redis stall lets the write through: a limit guards against floods, and a stall lasts seconds, so a
page view or a message lost to one, or a 503 emailed to the admins for it, costs more than one write
nobody counted. Only an outage fails open; a misconfiguration still raises.
"""

import logging
import time

import redis
from django.core.cache import cache

from auctions.client_ip import client_ip

logger = logging.getLogger(__name__)

OUTAGE_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)

#: An outage is logged once a minute per worker, not once per request.
_WARN_EVERY_SECONDS = 60
_last_warning = 0.0


def over_the_limit(request, bucket, limit, seconds):
    """Count one write against the caller's address; True once ``limit`` are spent in ``seconds``."""
    global _last_warning
    key = f"{bucket}:{client_ip(request) or 'unknown'}"
    try:
        # add-then-incr, so parallel requests each see their own count.
        if cache.add(key, 1, timeout=seconds):
            return limit < 1
        try:
            return cache.incr(key) > limit
        except ValueError:  # the window expired between the add and the increment
            cache.set(key, 1, timeout=seconds)
            return limit < 1
    except OUTAGE_ERRORS as e:
        now = time.monotonic()
        if now - _last_warning >= _WARN_EVERY_SECONDS:
            _last_warning = now
            logger.warning("Rate limit unreachable, letting %s through: %r", bucket, e)
        return False
