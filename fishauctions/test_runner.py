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

from django.conf import settings
from django.contrib.auth.hashers import reset_hashers
from django.test.runner import DiscoverRunner


class FastTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
        # get_hashers() memoizes, and the signal that clears it only fires for
        # override_settings, not for an assignment like the one above.
        reset_hashers(setting="PASSWORD_HASHERS")
        super().setup_test_environment(**kwargs)
