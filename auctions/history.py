"""What an edit changed, in a form a query can answer.

``AuctionHistory.action`` and ``ClubHistory.action`` are prose -- verbose names joined with commas
into an 800-character field -- which is right to show an organizer and wrong to query: it is keyed
on the label, so rewording one breaks every historical row; it truncates in form-field order, so the
loss is biased toward the bottom of the layout; and it records *that* a field changed, never what it
became.

``changed_fields`` is the queryable half, written alongside ``action`` by the same call, keyed on the
model field name and carrying the before and after. The prose column is untouched.

The question it exists for is "has anybody ever changed this setting" -- behind every argument about
whether a field belongs on the first screen, behind *Advanced*, or gone.
:func:`auctions.field_adoption` answers it for the past; this answers it exactly for the future,
including a field changed and changed back.

Values are stored as readable JSON scalars, not a restorable serialization: long text is truncated,
model instances become their ``str()``, and anything that looks like a credential becomes
``"[redacted]"`` -- ``ClubPayPalForm`` posts a secret through this path.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

# One stored value: long enough for a pickup-location description, short enough that a 90-field edit
# is still a sensible row.
MAX_VALUE_LENGTH = 300
# A form with more changed fields than this is not an edit anybody made by hand.
MAX_FIELDS = 250
# Substrings of a field name meaning the value is a credential. Matched on the name, because the
# value is exactly what must not be looked at.
SECRET_FIELD_MARKERS = ("password", "secret", "api_key", "apikey", "token", "private_key", "client_id")
REDACTED = "[redacted]"


def is_secret_field(field_name: str) -> bool:
    """Whether a field's value must never be written to a changelog."""
    name = (field_name or "").lower()
    return any(marker in name for marker in SECRET_FIELD_MARKERS)


def jsonable(value: Any, _depth: int = 0) -> Any:
    """A JSON-storable, human-readable stand-in for a form value.

    ``JSONField`` raises at ``save()`` time on anything ``json.dumps`` can't encode, inside the edit's
    own transaction -- so a changelog write could roll back the edit it describes. Nothing reaches the
    field without passing through here, and no branch raises.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        # A float JSON cannot encode (inf, nan) would raise at save time.
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time | datetime.timedelta | uuid.UUID):
        return str(value)
    if isinstance(value, str):
        return truncate(value)
    if _depth < 2 and isinstance(value, list | tuple | set | frozenset):
        return [jsonable(item, _depth + 1) for item in list(value)[:20]]
    if _depth < 2 and isinstance(value, dict):
        return {str(key)[:100]: jsonable(item, _depth + 1) for key, item in list(value.items())[:20]}
    if _depth < 2 and hasattr(value, "all") and callable(value.all):
        # A ManyToMany queryset: the m2m form field's cleaned_data.
        try:
            return [truncate(str(item)) for item in value.all()[:20]]
        except Exception:
            return truncate(str(value))
    try:
        return truncate(str(value))
    except Exception:
        # A __str__ that raises. Losing the value beats losing the edit.
        return "[unreadable]"


def truncate(value: str) -> str:
    """A string short enough to store, marked when it has lost its tail."""
    if len(value) <= MAX_VALUE_LENGTH:
        return value
    return value[: MAX_VALUE_LENGTH - 1] + "…"


def changed_field_summary(form) -> dict[str, dict[str, Any]]:
    """``{field_name: {"from": old, "to": new}}`` for everything ``form`` changed.

    Keyed on the form field name, which for a ``ModelForm`` is the model field name. ``from`` comes from
    ``form.initial``, captured when the form was built, so this is correct whether the caller records
    history before or after ``save()`` -- both orders exist here.

    An empty dict for a form that changed nothing or isn't a form; no row is written unless something
    changed, so the two need not be distinguishable.
    """
    if form is None:
        return {}
    try:
        changed = list(form.changed_data)
    except Exception:
        # changed_data validates, and a form that can't say what changed still has an edit behind
        # it. Prose without a summary beats an exception.
        return {}
    initial = getattr(form, "initial", None) or {}
    cleaned = getattr(form, "cleaned_data", None) or {}
    summary: dict[str, dict[str, Any]] = {}
    for field_name in changed[:MAX_FIELDS]:
        if is_secret_field(field_name):
            summary[field_name] = {"from": REDACTED, "to": REDACTED}
            continue
        summary[field_name] = {
            "from": jsonable(initial.get(field_name)),
            "to": jsonable(cleaned.get(field_name)),
        }
    return summary


def record_club_history(club, applies_to, action="Edited", user=None, form=None):
    """``Club``'s half of :meth:`Auction.create_history`, including ``changed_fields``.

    ``ClubHistory`` rows are written from thirty-odd places that name their own change ("Added member
    X"); this is for the form-backed ones -- the settings pages, where "did anybody ever turn this on"
    had no answer at all, since those views wrote the constant "Updated club settings".
    """
    from auctions.models import ClubHistory

    changed_fields = changed_field_summary(form)
    if form is not None and changed_fields:
        action = f"{action} {', '.join(field_label(form, name) for name in changed_fields)}"
    return ClubHistory.objects.create(
        club=club,
        user=user,
        action=action[:800],
        applies_to=applies_to,
        changed_fields=changed_fields,
    )


def field_label(form, field_name: str) -> str:
    """The human name of a form field, for the prose half of a history row."""
    try:
        return str(form.instance._meta.get_field(field_name).verbose_name)
    except Exception:
        return field_name.replace("_", " ").title()
