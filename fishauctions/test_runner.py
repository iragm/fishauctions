"""The test runner: the cheap password hasher, mail kept in memory, and the timezone reset between tests.

PBKDF2 is deliberately expensive -- about 200ms a call here -- which is right everywhere a real
password is stored and buys nothing in the suite: fixtures create roughly 17,000 passwords and API
keys a run, which was most of a 55-minute run.

The second job is putting the active timezone back between tests, because four forms activate one
and none deactivate. Both are in a runner rather than a settings_test.py because only
``manage.py test`` instantiates it: CI, a full run and a single-module run all get it with no flag to
forget, and production cannot reach it.
"""

import django
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def test_settings():
    """The settings the whole suite runs under.

    Django swaps ``EMAIL_BACKEND`` for locmem but not post_office's own ``POST_OFFICE["BACKENDS"]``, which is
    whatever the environment configured, so a test that built a queued email's message logged in to the
    real SMTP server.
    """
    from django.conf import settings

    post_office = {
        **getattr(settings, "POST_OFFICE", {}),
        "BACKENDS": {"default": "django.core.mail.backends.locmem.EmailBackend"},
    }
    return override_settings(PASSWORD_HASHERS=FAST_HASHERS, POST_OFFICE=post_office)


def reset_timezone_between_tests():
    """Start every test in the site's timezone, whatever the last one left activated.

    ``PickupLocationForm``, ``CreateAuctionForm``, ``AuctionEditForm`` and ``ClubEventForm`` each call
    ``timezone.activate()`` in ``__init__`` and none deactivate -- correct for a request, which has one
    browser's zone for its life, but the zone is thread-local and Django doesn't reset it between tests.
    So a test that builds one leaves its zone active for whatever runs next, and ``--parallel`` decides
    which pairings happen.

    ``_pre_setup`` rather than ``setUp``, because it is Django's own per-test hook and runs for every
    test class, not only ones built on a base of ours.
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


def apply_test_settings(*args):
    """Apply :func:`test_settings` in *this* process, for good.

    ``--parallel`` workers are forked on Linux and inherit the swap, but under ``spawn`` a worker is a
    fresh interpreter that never calls the runner's ``setup_test_environment``. Django's hook for that is
    ``ParallelTestSuite.process_setup``, which ``_init_worker`` does call, so one function serves both.

    The timezone reset is installed from here too: a ``spawn`` worker patches its own copy of
    ``SimpleTestCase``, and a forked one skips it as already installed.

    ``django.setup()`` first, because this hook runs before the one ``_init_worker`` does, in a process
    whose settings are unconfigured -- and override_settings there wraps the empty sentinel, so the next
    setting read raises and the pool respawns the worker forever. It is idempotent.
    """
    django.setup()
    test_settings().enable()
    reset_timezone_between_tests()


class FastParallelTestSuite(ParallelTestSuite):
    # A plain function, not a staticmethod: Django passes it on as ``self.process_setup.__func__``.
    process_setup = apply_test_settings


class FastTestRunner(DiscoverRunner):
    parallel_test_suite = FastParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # override_settings rather than assigning to settings.PASSWORD_HASHERS, so the
        # setting_changed receivers fire -- one of them clears the memoized hashers.
        self._test_settings = test_settings()
        self._test_settings.enable()
        reset_timezone_between_tests()

    def teardown_test_environment(self, **kwargs):
        self._test_settings.disable()
        super().teardown_test_environment(**kwargs)
