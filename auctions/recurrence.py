"""Repeating club events.

A repeating event from Google Calendar is stored the way Google stores it: **one row for the
series**, holding the rule (the RRULE/EXDATE/RDATE lines, verbatim) and the moment the series is
anchored to. The row's ``date_start``/``date_end`` are kept pointing at the occurrence happening
now, or the next one — so everything that knows nothing about recurrence keeps working unchanged:
the club page sorts by ``date_start``, "upcoming" filters on ``date_end``, membership emails pick
the soonest event, and Discord gets one scheduled event at a time rather than fifty-two.

The alternative — asking Google to expand the series (``singleEvents=true``) and storing an event
per occurrence — is what this replaced. A single never-ending weekly meeting became a row, a club
page entry, a Discord event and an iCal VEVENT per week, for ever.

Only the feed and the push to Google deal in the whole series: both take ``recurrence_start`` as
DTSTART and hand over the rule itself, so a subscriber's calendar expands it the way Google does.
"""

from __future__ import annotations

import datetime
import logging

from dateutil.rrule import rrulestr

logger = logging.getLogger(__name__)

# Lines Google sends in an event's "recurrence" array that we know how to keep.
KNOWN_PREFIXES = ("RRULE", "EXDATE", "RDATE", "EXRULE")

WEEKDAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}

ORDINALS = {"1": "first", "2": "second", "3": "third", "4": "fourth", "-1": "last"}


def clean_lines(lines):
    """Keep the recurrence lines we understand, in order, with no blanks."""
    return [line.strip() for line in lines or [] if line and line.strip().upper().startswith(KNOWN_PREFIXES)]


def to_text(lines):
    return "\n".join(clean_lines(lines))


def from_text(text):
    return clean_lines((text or "").splitlines())


def _ruleset(anchor, lines):
    """A dateutil rule set for this series, or None when the rule is unusable.

    Google can send rules dateutil won't parse, and a club's calendar must not stop syncing over
    one strange repeat — the caller falls back to treating the event as a one-off.
    """
    text = to_text(lines)
    if not text or not anchor:
        return None
    try:
        return rrulestr(text, dtstart=anchor, forceset=True)
    except (ValueError, TypeError):
        logger.warning("Could not read recurrence rule %r", text)
        return None


def current_or_next(anchor, lines, duration, now):
    """The occurrence to show for this series right now, or None if it has no usable rule.

    An occurrence already under way wins over the one after it — "our next event" shouldn't skip
    past the meeting happening this evening. Once a finite series has run out, the last occurrence
    is kept, so the event settles into the club page's recent history instead of vanishing.
    """
    rules = _ruleset(anchor, lines)
    if rules is None:
        return None
    previous = rules.before(now, inc=True)
    if previous and previous + duration > now:
        return previous
    upcoming = rules.after(now)
    if upcoming:
        return upcoming
    return previous


def with_exdate(lines, moment):
    """The same rule with one occurrence excluded.

    Cancelling or moving a single occurrence in Google arrives as its own item rather than as a
    change to the series, so this is how that occurrence stops being generated here too. Written
    in UTC, which every parser accepts without needing to know the calendar's timezone.
    """
    exdate = f"EXDATE:{moment.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
    lines = clean_lines(lines)
    if exdate in lines:
        return lines
    return [*lines, exdate]


def describe(lines):
    """A short human-readable summary of the rule, for the club page. Empty when there isn't one."""
    rule = next((line for line in clean_lines(lines) if line.upper().startswith("RRULE")), "")
    if not rule:
        return ""
    # "RRULE:FREQ=WEEKLY;BYDAY=TU" -> {"FREQ": "WEEKLY", "BYDAY": "TU"}
    parts = dict(piece.split("=", 1) for piece in rule.split(":", 1)[-1].split(";") if "=" in piece)
    frequency = parts.get("FREQ", "").upper()
    interval = int(parts.get("INTERVAL", "1") or 1)
    every = {
        "DAILY": ("daily", "every {n} days"),
        "WEEKLY": ("weekly", "every {n} weeks"),
        "MONTHLY": ("monthly", "every {n} months"),
        "YEARLY": ("yearly", "every {n} years"),
    }.get(frequency)
    if not every:
        return "Repeats"
    summary = every[0] if interval == 1 else every[1].format(n=interval)
    days = _describe_days(parts.get("BYDAY", ""), frequency)
    if days:
        summary = f"{summary} on {days}"
    return f"Repeats {summary}"


def _describe_days(byday, frequency):
    """ "Tuesday", "the first Tuesday", "Tuesday and Thursday" — or nothing we can phrase."""
    names = []
    for raw in (byday or "").split(","):
        token = raw.strip().upper()
        if not token:
            continue
        # "TU" is every Tuesday; "1TU" is the first Tuesday of the month.
        ordinal, code = token[:-2], token[-2:]
        name = WEEKDAY_NAMES.get(code)
        if not name:
            return ""
        if ordinal and frequency == "MONTHLY":
            word = ORDINALS.get(ordinal)
            if not word:
                return ""
            name = f"the {word} {name}"
        names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"
