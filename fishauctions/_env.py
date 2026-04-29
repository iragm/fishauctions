"""Helpers for parsing environment variables in settings.

Kept in its own module (instead of inline in settings.py) so it can be
unit-tested without importing the full Django settings module.
"""

from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured

_TRUTHY = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSY = frozenset({"0", "false", "no", "off", "f", "n", ""})

INSECURE_SECRET_VALUES = frozenset({"", "unsecure"})


def parse_bool_env(value: str | None, *, default: bool) -> bool:
    """Parse a string env-var value into a bool, case-insensitively.

    - ``None`` (env var not set) returns ``default``.
    - Leading/trailing whitespace is stripped before matching, so a
      whitespace-only value (e.g. ``"   "``) normalizes to ``""`` and is
      treated as falsy.
    - Recognized truthy: 1, true, yes, on, t, y (any case).
    - Recognized falsy: 0, false, no, off, f, n, empty string (any case).
    - Anything else raises ``ValueError`` so a typo in the env var doesn't
      silently degrade to a wrong default.
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
    """Raise ``ImproperlyConfigured`` if any secret is unset or has a known-insecure default.

    Intended to be called from ``settings.py`` only when ``DEBUG`` is False.
    A value is considered insecure if it is ``None`` or in
    ``INSECURE_SECRET_VALUES`` (the literal placeholders shipped as defaults
    in this codebase). Every offender is reported in a single error message
    so the operator sees the full picture in one startup pass.
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
