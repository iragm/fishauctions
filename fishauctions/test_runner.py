"""The test runner: the cheap password hasher, and the timezone reset between tests.

PBKDF2 is deliberately expensive -- about 200ms a call in this container -- and that is the
right setting everywhere a real password is stored. The test suite is the one place it buys
nothing: fixtures create roughly 17,000 passwords and API keys a run (StandardTestCase alone
makes five users per test class, and models.HashedAPIKey hashes with the same machinery), so
the default hashers were most of a 55-minute run.

The second job is the one below it: putting the active timezone back between tests, because four
forms activate one and none of them deactivate. Both are here for the same reason -- this is a
runner rather than a settings_test.py because only ``manage.py test`` instantiates it: CI, a local
full run and a single-module run all get it with no flag to forget, and production cannot reach it
however settings are loaded.
"""

import django
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def reset_timezone_between_tests():
    """Start every test in the site's timezone, whatever the last one left activated.

    ``PickupLocationForm``, ``CreateAuctionForm``, ``AuctionEditForm`` and ``ClubEventForm`` each
    call ``timezone.activate()`` in ``__init__`` and none of them deactivate -- correct for a
    request, which has one browser's zone for its whole life, but that zone is thread-local and
    Django does not reset it between tests. So a test that builds one of those forms (or fetches a
    page that does) leaves its zone active for whatever runs next, and a test asserting on a
    rendered or parsed datetime passes alone and fails by whole hours after one of them. Which
    tests those are is not fixed: ``--parallel`` splits the suite by worker count, so a pairing
    that never happens here happens in CI. ``test_club_events`` hit exactly this and pinned one
    class with ``addCleanup(timezone.deactivate)``; this is the same fix for every other class,
    including the ones nobody has written yet.

    ``_pre_setup`` rather than ``setUp``, because it is Django's own per-test hook and runs for
    every test class in the suite, not only the ones built on a base of ours.
    """
    from django.test.testcases import SimpleTestCase
    from django.utils import timezone

    if getattr(SimpleTestCase, "_deactivates_timezone", False):
        return
    inner = SimpleTestCase._pre_setup.__func__

    @classmethod
    def _pre_setup(cls):
        timezone.deactivate()
        inner(cls)

    SimpleTestCase._pre_setup = _pre_setup
    SimpleTestCase._deactivates_timezone = True


def use_fast_hashers(*args):
    """Swap the hashers in *this* process, for good.

    ``--parallel`` workers are forked on Linux and inherit the swap, but under the ``spawn``
    start method (the default on macOS, and where CPython is heading elsewhere) a worker is a
    fresh interpreter that re-reads settings and never calls the runner's own
    ``setup_test_environment``. Django's hook for that is ``ParallelTestSuite.process_setup``,
    which ``_init_worker`` does call, so the same function serves both. Nothing disables it:
    the worker exits with the run.

    The timezone reset is installed from here as well: a ``spawn`` worker patches its own copy of
    ``SimpleTestCase``, and a forked one inherits the patch and skips it as already installed.

    ``django.setup()`` first because this hook runs *before* the one ``_init_worker`` does, in a
    process whose settings are still unconfigured -- and override_settings on unconfigured
    settings wraps the empty sentinel, so the next read of any setting (django.setup()'s own
    LOGGING_CONFIG) raises and the pool respawns the worker forever. It is idempotent.
    """
    django.setup()
    override_settings(PASSWORD_HASHERS=FAST_HASHERS).enable()
    reset_timezone_between_tests()


class FastParallelTestSuite(ParallelTestSuite):
    # A plain function, not a staticmethod: Django passes it on as ``self.process_setup.__func__``.
    process_setup = use_fast_hashers


class FastTestRunner(DiscoverRunner):
    parallel_test_suite = FastParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # override_settings rather than assigning to settings.PASSWORD_HASHERS, so that the
        # setting_changed receivers fire -- one of them is what clears the memoized hashers.
        self._fast_hashers = override_settings(PASSWORD_HASHERS=FAST_HASHERS)
        self._fast_hashers.enable()
        reset_timezone_between_tests()

    def teardown_test_environment(self, **kwargs):
        self._fast_hashers.disable()
        super().teardown_test_environment(**kwargs)
