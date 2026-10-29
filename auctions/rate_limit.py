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
#: Subclasses of ConnectionError that mean a wrong password or ACL, not a stall.
NOT_AN_OUTAGE = (redis.exceptions.AuthenticationError, redis.exceptions.AuthorizationError)

#: An outage is logged once a minute per worker, not once per request.
_WARN_EVERY_SECONDS = 60
_last_warning = float("-inf")


def over_the_limit(request, bucket, limit, seconds):
    """Count one write against the caller's address; True once ``limit`` are spent in ``seconds``."""
    global _last_warning
    key = f"{bucket}:{client_ip(request) or 'unknown'}"
    try:
        # add-then-incr, so parallel requests each see their own count.
        if cache.add(key, 1, timeout=seconds):
            return limit < 1
        try:
            count = cache.incr(key)
        except ValueError:  # the window expired between the add and the increment
            cache.add(key, 1, timeout=seconds)
            return limit < 1
        if count == 1:
            # The window expired between incr's EXISTS and its INCR, which made the key with no expiry.
            cache.touch(key, seconds)
        return count > limit
    except NOT_AN_OUTAGE:
        raise
    except OUTAGE_ERRORS as e:
        now = time.monotonic()
        if now - _last_warning >= _WARN_EVERY_SECONDS:
            _last_warning = now
            logger.warning("Rate limit unreachable, letting %s through: %r", bucket, e)
        return False
