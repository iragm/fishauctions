"""The test runner, which exists to swap the password hasher out.

PBKDF2 is deliberately expensive -- about 200ms a call in this container -- and that is the
right setting everywhere a real password is stored. The test suite is the one place it buys
nothing: fixtures create roughly 17,000 passwords and API keys a run (StandardTestCase alone
makes five users per test class, and models.HashedAPIKey hashes with the same machinery), so
the default hashers were most of a 55-minute run.

This is a runner rather than a settings_test.py because only ``manage.py test`` instantiates
it: CI, a local full run and a single-module run all get it with no flag to forget, and
production cannot reach it however settings are loaded.
"""

import django
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def use_fast_hashers(*args):
    """Swap the hashers in *this* process, for good.

    ``--parallel`` workers are forked on Linux and inherit the swap, but under the ``spawn``
    start method (the default on macOS, and where CPython is heading elsewhere) a worker is a
    fresh interpreter that re-reads settings and never calls the runner's own
    ``setup_test_environment``. Django's hook for that is ``ParallelTestSuite.process_setup``,
    which ``_init_worker`` does call, so the same function serves both. Nothing disables it:
    the worker exits with the run.

    ``django.setup()`` first because this hook runs *before* the one ``_init_worker`` does, in a
    process whose settings are still unconfigured -- and override_settings on unconfigured
    settings wraps the empty sentinel, so the next read of any setting (django.setup()'s own
    LOGGING_CONFIG) raises and the pool respawns the worker forever. It is idempotent.
    """
    django.setup()
    override_settings(PASSWORD_HASHERS=FAST_HASHERS).enable()


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

    def teardown_test_environment(self, **kwargs):
        self._fast_hashers.disable()
        super().teardown_test_environment(**kwargs)
