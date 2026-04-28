"""Helpers for parsing environment variables in settings.

Kept in its own module (instead of inline in settings.py) so it can be
unit-tested without importing the full Django settings module.
"""

_TRUTHY = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSY = frozenset({"0", "false", "no", "off", "f", "n", ""})


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
