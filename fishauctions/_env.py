"""Helpers for parsing environment variables in settings.

Its own module rather than inline in settings.py so it can be unit-tested without importing the full
settings.
"""

from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured

_TRUTHY = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSY = frozenset({"0", "false", "no", "off", "f", "n", ""})

INSECURE_SECRET_VALUES = frozenset({"", "unsecure"})
OPTIONAL_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "unsecure",
        "secret",
        "public-key",
        "private-key",
        "change-me-to-a-long-random-secret",
        "secret.apps.googleusercontent.com",
        "client-id",
        "ca-pub-abcde",
        "G-abcde",
        "GTM-abcde",
    }
)


def parse_bool_env(value: str | None, *, default: bool) -> bool:
    """Parse a string env-var value into a bool, case-insensitively.

    ``None`` (unset) returns ``default``, and whitespace is stripped first, so ``"   "`` is falsy.
    Truthy: 1, true, yes, on, t, y. Falsy: 0, false, no, off, f, n, "". Anything else raises
    ``ValueError``, so a typo doesn't silently become a wrong default.
    """
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    msg = f"Cannot parse {value!r} as a boolean env value"
    raise ValueError(msg)


def require_secure_prod_secrets(secrets: Mapping[str, str | None]) -> None:
    """Raise ``ImproperlyConfigured`` if any secret is unset or still at a known-insecure default.

    Called from ``settings.py`` when ``DEBUG`` is False. Insecure means ``None`` or one of
    ``INSECURE_SECRET_VALUES``, the literal placeholders shipped as defaults here. Every offender is
    named in one message, so the operator sees the whole picture in one startup pass.
    """
    bad = sorted(name for name, value in secrets.items() if value is None or value in INSECURE_SECRET_VALUES)
    if not bad:
        return
    joined = ", ".join(bad)
    msg = (
        f"The following environment variables are unset or set to a known-insecure "
        f"default but are required when DEBUG=False: {joined}. Set each one to a "
        f"secure value before starting the application in production."
    )
    raise ImproperlyConfigured(msg)


def env_has_real_value(value: str | None, *, placeholder_values: frozenset[str] = OPTIONAL_PLACEHOLDER_VALUES) -> bool:
    """Return True when an optional env value is present and not a known placeholder."""
    if value is None:
        return False
    normalized = value.strip()
    return bool(normalized) and normalized not in placeholder_values
