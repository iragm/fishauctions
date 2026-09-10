"""Getting a list of aquarium clubs onto this site from a CSV somebody curated.

**This replaced a web crawler, and the reason is worth keeping.**  Phase 8 originally found clubs by
fetching umbrella directories, crawling out from club links pages and reading search results, with a
language model extracting names from each page.  It was built, tested and then run against the real
internet on 2026-09-09, and the run is what killed it:

* Five of seven umbrella directories were gone.  One federation had folded and lost its domain to a
  gambling site; its successor is a mailing list with no club list; another's rebuilt site has no
  affiliates page at all.  Working out what any of that meant needed a person, not a retry.
* The two sources that still existed could not be read by ``requests``: one club list lives only
  inside a hashed JavaScript bundle, and another returns 403 to any honest User-Agent.
* Deciding which extracted names were really clubs turned into four rounds of widening a regex,
  each round finding more real clubs -- "Enthusiasts", "Exchange", "Aquaria", Spanish and French
  names -- that the previous round had silently discarded.

Every one of those is a judgement call, and a person with a browser makes them in an afternoon.  The
population is also nearly static: aquarium societies are mostly decades old and new ones are rare,
so this is a one-off with a long tail rather than a feed, and an unattended pipeline had nothing to
be unattended *for*.

**So the acquisition half is a human with a research tool, and this module is everything after it.**
That is the half that was always worth having, because it is the half that is hard to do by hand:

* :func:`find_existing` -- **domain first, then a fuzzy name** (:mod:`auctions.club_matching`).  A
  list of three hundred clubs overlaps whatever is already here, and typing that comparison out by
  hand is how a club ends up in the table twice.
* :func:`ingest` -- creates at ``PROSPECT``, which is the map gate, and only ever *fills in* blanks
  on a club that already exists.  What a person typed is never overwritten by a CSV.

A row in the file is a claim, not a fact.  :mod:`auctions.club_verification` fetches each club's
links afterwards and records what answered, which is the check that catches the confident-looking
URL that 404s -- the characteristic failure of research done by a machine.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.utils import timezone

from . import club_matching

logger = logging.getLogger(__name__)

#: Hosts that are never a club's own site, so never worth storing as a homepage.  A CSV is curated,
#: but "the club's website" and "the club's Facebook page" are easy to put in the wrong column.
_NOT_A_CLUB_HOST = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "google.com",
    "wikipedia.org",
    "meetup.com",
    "eventbrite.com",
    "linkedin.com",
    "pinterest.com",
    "reddit.com",
    "tiktok.com",
)

#: The columns :func:`read_csv` will read.  ``name`` is the only one that has to be there; a club
#: with nothing but a name is still a lead, and demanding more would mean dropping the hardest
#: clubs to find -- which are exactly the ones worth having.
CSV_COLUMNS = (
    "name",
    "homepage",
    "location",
    "facebook_page",
    "contact_method",
    "contact_email",
)


@dataclass
class ImportedClub:
    """One club a CSV claims exists.  Nothing has been checked at this point."""

    name: str
    homepage: str = ""
    location: str = ""
    facebook_page: str = ""
    #: How this club can be reached: see ``Club.CONTACT_METHOD_CHOICES``.  A club with only a
    #: Facebook page is reached differently from one with a contact form, and outreach is a person
    #: working a queue, so the queue has to say which door to knock on.
    contact_method: str = ""
    contact_email: str = ""
    #: Which file and which row this came from, recorded on the club so a bad import can be traced.
    source: str = ""


@dataclass
class IngestReport:
    """What one import did, in the four outcomes a row can have."""

    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def __str__(self):
        return (
            f"{len(self.created)} created, {len(self.updated)} filled in, "
            f"{len(self.matched)} already complete, {len(self.skipped)} skipped"
        )


def domain_of(url: str) -> str:
    """The registrable-ish host of a URL, lowercased and without ``www.``.  ``""`` if there is none.

    Not a public-suffix implementation on purpose: this is used to decide whether two rows are the
    same club, and for that "the host without www" is both sufficient and much easier to reason
    about than a rule that treats ``co.uk`` specially.
    """
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def is_a_club_host(url: str) -> bool:
    """Whether a URL could be a club's own website rather than a page about it somewhere else."""
    host = domain_of(url)
    return bool(host) and not any(host == bad or host.endswith(f".{bad}") for bad in _NOT_A_CLUB_HOST)


def read_csv(handle, *, source: str = "") -> tuple[list[ImportedClub], list[str]]:
    """Read a curated club list.  Returns the rows and a list of complaints about the ones it didn't.

    The complaints are returned rather than raised because a three-hundred-row file with four bad
    rows should import two hundred and ninety-six clubs and *tell* somebody about the four.  Failing
    the whole file on one typo means the typo gets fixed by deleting the row.
    """
    from .models import Club

    valid_methods = {choice for choice, _label in Club.CONTACT_METHOD_CHOICES if choice}
    reader = csv.DictReader(handle)
    missing = [column for column in ("name",) if column not in (reader.fieldnames or [])]
    if missing:
        return [], [f"The file has no {', '.join(missing)} column. Expected: {', '.join(CSV_COLUMNS)}"]

    rows, complaints = [], []
    for number, raw in enumerate(reader, start=2):  # 2: row 1 is the header, as a spreadsheet counts
        values = {column: (raw.get(column) or "").strip() for column in CSV_COLUMNS}
        if not values["name"]:
            complaints.append(f"Row {number}: no name, skipped")
            continue
        method = values["contact_method"].lower()
        if method and method not in valid_methods:
            complaints.append(
                f"Row {number} ({values['name']}): contact_method '{method}' is not one of "
                f"{', '.join(sorted(valid_methods))}; left blank"
            )
            method = ""
        if values["homepage"] and not is_a_club_host(values["homepage"]):
            # Overwhelmingly a Facebook URL in the homepage column.  Move it rather than drop it.
            if not values["facebook_page"]:
                values["facebook_page"] = values["homepage"]
            complaints.append(f"Row {number} ({values['name']}): homepage is not a club's own site, moved")
            values["homepage"] = ""
        rows.append(
            ImportedClub(
                name=values["name"][:255],
                homepage=values["homepage"][:255],
                location=values["location"][:500],
                facebook_page=values["facebook_page"][:255],
                contact_method=method,
                contact_email=values["contact_email"][:255],
                source=f"{source} row {number}" if source else f"row {number}",
            )
        )
    return rows, complaints


def find_existing(found: ImportedClub, clubs):
    """The club already on this site that ``found`` is, or ``None``.

    **Domain first, name second, and the order is the whole point.**  Two clubs can be called
    "Aquarium Society of Virginia" in two different decades' spellings, but a domain is a fact: if
    two rows point at the same host they are the same club.  Only when there is no domain to
    compare does this fall back to comparing names, and that comparison has a threshold high enough
    that "Boston Aquarium Society" and "Bristol Aquarium Society" are two clubs.
    """
    domain = domain_of(found.homepage)
    if domain:
        for club in clubs:
            if domain_of(club.homepage or "") == domain:
                return club
    match, _score = club_matching.best_match(found.name, clubs)
    return match


def ingest(found_clubs, *, source: str, update_existing: bool = True) -> IngestReport:
    """Turn what a CSV claims into ``Club`` rows, without ever publishing one.

    New clubs are created at ``PROSPECT``, which is the map gate: nothing imported appears anywhere
    on this site until a person moves it to ``LISTED``.  An existing club is only ever *filled in*
    -- a homepage or an email it did not have -- and never overwritten, because what is already here
    was typed by somebody who knew and what is arriving was researched from outside.
    """
    from .models import Club

    report = IngestReport()
    clubs = list(Club.objects.all())
    for found in found_clubs:
        existing = find_existing(found, clubs)
        if existing is None:
            club = Club.objects.create(
                name=found.name,
                homepage=found.homepage or None,
                facebook_page=found.facebook_page or None,
                location=found.location or None,
                contact_email=found.contact_email or None,
                contact_method=found.contact_method,
                outreach_stage=Club.PROSPECT,
                notes=f"Imported from {source or found.source} on {timezone.now():%Y-%m-%d}"[:300],
            )
            clubs.append(club)
            report.created.append(club)
            continue
        filled = {}
        if update_existing:
            for attribute, value in (
                ("homepage", found.homepage),
                ("facebook_page", found.facebook_page),
                ("location", found.location),
                ("contact_email", found.contact_email),
                ("contact_method", found.contact_method),
            ):
                if value and not (getattr(existing, attribute, "") or "").strip():
                    filled[attribute] = value
        if filled:
            for attribute, value in filled.items():
                setattr(existing, attribute, value)
            existing.save(update_fields=list(filled))
            report.updated.append(existing)
        else:
            report.matched.append(existing)
    return report
