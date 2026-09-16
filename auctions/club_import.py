"""Getting a list of aquarium clubs onto this site from a curated CSV.

**This replaced a web crawler.** Phase 8 originally found clubs by fetching umbrella directories and
crawling club links pages, with a language model extracting names. The run on 2026-09-09 killed it:
five of seven directories were gone (one federation had folded and lost its domain to a gambling
site), the two that survived couldn't be read by ``requests`` (one list lives inside a hashed JS
bundle, another 403s any honest User-Agent), and deciding which names were really clubs took four
rounds of widening a regex, each finding real clubs the last had discarded.

Those are judgement calls a person with a browser makes in an afternoon, and the population is
nearly static, so this is a one-off with a long tail rather than a feed.

So acquisition is a person, and this module is everything after it -- the half that is hard by hand:

* :func:`find_existing` -- domain first, then a fuzzy name (:mod:`auctions.club_matching`), since a
  list of three hundred clubs overlaps what is already here.
* :func:`ingest` -- creates at ``PROSPECT``, which is the map gate, and only ever fills in blanks.
  What a person typed is never overwritten by a CSV.

**A row is a claim, and nothing here checks it.** The link verifier went the same way as the
crawler: this site does not make outbound requests to other people's servers. A confident-looking
URL that 404s is caught by the person who has to look at the website before a club leaves
``PROSPECT``.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.utils import timezone

from . import club_matching

logger = logging.getLogger(__name__)

#: Hosts that are never a club's own site. A CSV is curated, but "the club's website" and "its
#: Facebook page" are easy to put in the wrong column.
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

#: The columns :func:`read_csv` reads. Only ``name`` is required: a club with nothing but a name is
#: still a lead, and demanding more would drop the hardest clubs to find.
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
    #: How the club can be reached (``Club.CONTACT_METHOD_CHOICES``): outreach is a person working a
    #: queue, so the queue has to say which door to knock on.
    contact_method: str = ""
    contact_email: str = ""
    #: Which file and row this came from, so a bad import can be traced.
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
    """The registrable-ish host of a URL, lowercased and without ``www.``; ``""`` if there is none.

    Not a public-suffix implementation: this decides whether two rows are the same club, and "the host
    without www" is sufficient and easier to reason about than special-casing ``co.uk``.
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
    """Read a curated club list; returns the rows and complaints about the ones it skipped.

    Complaints are returned rather than raised: a three-hundred-row file with four bad rows should
    import 296 clubs and say so, since failing the whole file means the typo gets fixed by deleting the
    row.
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
            # Overwhelmingly a Facebook URL in the homepage column: move it rather than drop it.
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

    Domain first, name second: two rows pointing at the same host are the same club, whatever decade's
    spelling they use. The name fallback's threshold is high enough that "Boston" and "Bristol Aquarium
    Society" stay two clubs.
    """
    domain = domain_of(found.homepage)
    if domain:
        for club in clubs:
            if domain_of(club.homepage or "") == domain:
                return club
    match, _score = club_matching.best_match(found.name, clubs)
    return match


def ingest(found_clubs, *, source: str, update_existing: bool = True) -> IngestReport:
    """Turn what a CSV claims into ``Club`` rows, without publishing one.

    New clubs are created at ``PROSPECT``, the map gate, so nothing imported appears until a person
    moves it to ``LISTED``. An existing club is only ever filled in, never overwritten: what is here was
    typed by somebody who knew.
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
