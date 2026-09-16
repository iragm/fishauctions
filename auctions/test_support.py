"""Support code shared by the test modules. Holds no tests of its own.

``manage.py test --parallel`` (what CI runs) gives each worker its own *database*, but not its own
cache: every worker keeps the ``CACHES`` from settings, pointing at one shared Redis. Two things
follow, and both fail tests for reasons unrelated to the code under test:

* ``cache.clear()`` is a Redis ``FLUSHDB``, not a scoped delete, so a worker calling it in ``setUp``
  empties the cache out from under every *other* worker mid-assertion. That is what made
  ``test_the_jwks_is_not_refetched_for_every_notification`` fail CI with ``2 != 1``.
* Cache keys are global, so two workers writing one key read each other's values -- Apple's JWKS
  (each worker signs with its own throwaway key under the same ``kid``), or anything keyed on a
  primary key, which both worker databases hand out starting from 1.

:func:`isolated_cache` gives a test class a local-memory cache instead, living inside the one
process running the test.
"""

from django.test import override_settings


def isolated_cache(name):
    """Point a test class at a cache of its own instead of the Redis one all workers share.

    Applies to subclasses too, so decorating a base test case covers everything built on it::

        @isolated_cache("apple-notifications")
        class AppleNotificationTestCase(TestCase):
            ...
    """
    return override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"isolated-cache-{name}",
                # LocMemCache's default ceiling is 300 entries, past which it culls a third of
                # them — a cache that quietly forgets things mid-test is the bug being fixed here.
                "OPTIONS": {"MAX_ENTRIES": 100_000},
            }
        }
    )
