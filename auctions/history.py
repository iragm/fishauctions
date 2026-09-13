"""What an edit changed, in a form a query can answer.

``AuctionHistory.action`` and ``ClubHistory.action`` are prose: ``"Edited Buy now, Tax, Reserve
price"``, built by joining **verbose names** with commas into an 800-character ``CharField``.  That
string is the right thing to show an organizer and the wrong thing to ask a question of:

* it is keyed on the label, so rewording one silently breaks every historical row, and two fields
  sharing a verbose name are indistinguishable;
* it truncates, and truncation follows form-field order -- so on a ~90-field form the loss is
  systematically biased toward whatever sits at the bottom of the layout;
* it records *that* a field changed, never what it became.

``changed_fields`` is the queryable half, written alongside ``action`` by the same call.  It is
keyed on the **model field name**, which is what migrations rename and what code already refers to,
and it carries the before and after.  The prose column is untouched: it is still what the history
page renders, and rewriting three years of it was never worth it.

The question this exists to answer is "has anybody, ever, changed this setting" -- the one behind
every argument about whether a field should be on the first screen of a form, behind an *Advanced*
toggle, or gone.  :func:`auctions.field_adoption` answers it for the past by comparing live rows
against their defaults; this answers it for the future, exactly, including a field somebody changed
and changed back.

Values are stored as JSON scalars a human can read back, not as a serialization anybody could
restore from: long text is truncated, model instances become their ``str()``, and anything that
looks like a credential is replaced with ``"[redacted]"`` -- ``ClubPayPalForm`` posts a secret
through this same path, and a changelog is not a place to keep one.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

# One stored value. Long enough for a pickup-location description or a rules paragraph's opening,
# short enough that a 90-field edit is still a sensible row.
MAX_VALUE_LENGTH = 300
# A form with more changed fields than this is not an edit anybody made by hand.
MAX_FIELDS = 250
# Substrings of a field name that mean the value is a credential. Matched on the field name rather
# than the value because the value is exactly what must not be looked at.
SECRET_FIELD_MARKERS = ("password", "secret", "api_key", "apikey", "token", "private_key", "client_id")
REDACTED = "[redacted]"


def is_secret_field(field_name: str) -> bool:
    """Whether a field's value must never be written to a changelog."""
    name = (field_name or "").lower()
    return any(marker in name for marker in SECRET_FIELD_MARKERS)


def jsonable(value: Any, _depth: int = 0) -> Any:
    """A JSON-storable, human-readable stand-in for a form value.

    ``JSONField`` will happily accept anything ``json.dumps`` can encode and raise on everything
    else, at ``save()`` time, inside whatever transaction the edit is in -- so a changelog write
    could roll back the edit it was describing.  Nothing reaches the field without passing through
    here, and there is no branch that raises.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        # A float that JSON cannot encode (inf, nan) would raise at save time.
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

    Keyed on the form field name, which for a ``ModelForm`` is the model field name.  ``from``
    comes from ``form.initial`` -- captured when the form was built, so this is correct whether the
    caller records history before or after ``save()``, and both orders exist in this codebase
    (``AuctionUpdate.form_valid`` is before, ``palette_actions`` is after).

    An empty dict for a form that changed nothing, or for anything that is not a form; the field's
    default is the same empty dict, so "no summary" and "nothing changed" read alike.  They are not
    distinguishable and do not need to be: no row is written at all unless something changed.
    """
    if form is None:
        return {}
    try:
        changed = list(form.changed_data)
    except Exception:
        # changed_data validates, and a form that cannot say what changed still has an edit behind
        # it that is being recorded. Prose without a summary beats an exception here.
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

    ``ClubHistory`` rows are written from thirty-odd places directly, and all but the form-backed
    ones name their own change ("Added member X") with nothing a field summary could add.  This is
    for the ones that hand over a form -- the settings pages, where the question "did anybody ever
    turn this on" is the same question as on the auction side and had no answer at all: those views
    wrote the constant string "Updated club settings".
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
