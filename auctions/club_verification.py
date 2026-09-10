"""Is this club still out there?  Fetch its links, record what answered, and let dead ones die.

Phase 8 is about adding two or three hundred clubs to a list of sixty.  Doing that to a list nobody
has audited produces a list of three hundred clubs of unknown quality, so this runs first and keeps
running: **every club, found or already here, carries a date it was last verified and a way to
die**, or the outreach queue fills up with societies that folded in 2004 and the person working it
stops trusting the queue.

It is also the cheapest thing in phase 8 and the code every later source reuses.  A directory page,
a club's links page and a club's own homepage are all "fetch a URL politely and see what comes
back", so :func:`fetch` is here rather than in the importer that happens to need it first.

**What counts as dead is deliberately narrow.**  A club is a candidate for ``active=False`` only
when *every* signal is absent at once: no reachable homepage, no reachable Facebook page, no
auctions here, no members here.  Any one of those being present means somebody is still there.  The
rule never sets ``active`` itself -- ``active`` is hand-set, it takes a club off the map, and a
false positive is this site publicly forgetting a club that exists.  It nominates; a person agrees.

**Why a year.**  Nobody has a census of aquarium societies, so the interval is reasoned rather than
measured: the clubs on umbrella directories skew heavily to ones founded decades ago, and the
failures are front-loaded -- a club that survives its first few years usually runs on a small
committee until that committee ages out, which is not a thing that happens on a schedule.  So the
population is mostly long-lived with a long tail, checking more often than yearly buys almost
nothing, and a directory entry older than about three years is a coin flip.  Both numbers are
constants here so a better one replaces them in one place.

**Nothing here decides a club is dead because a fetch failed once.**  A timeout is a slow host, a
403 is a bot filter, and neither is evidence about a club.  Only a resolved "this host is not
serving anything" -- a DNS failure, a connection refusal, or a 404/410 -- is recorded as
unreachable; everything else is recorded as unknown and left for the next run.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from django.utils import timezone

logger = logging.getLogger(__name__)

#: How often a club's links are re-fetched.  See the module docstring for why this is a year.
CHECK_INTERVAL_DAYS = 365

#: A directory entry older than this is a coin flip and its clubs are worth re-verifying on sight.
DIRECTORY_STALE_DAYS = 365 * 3

#: Long enough for a slow club website on shared hosting, short enough that a run over three
#: hundred clubs finishes.
REQUEST_TIMEOUT_SECONDS = 15

#: Between two requests to the same host.  Club sites are small and often on shared hosting.
PER_HOST_DELAY_SECONDS = 2

#: Sent on every fetch this module makes.  A club webmaster reading their logs gets a name and a
#: page explaining what this is, which is the least a crawler owes them.
USER_AGENT = "auction.fish club directory (+https://auction.fish/blog/privacy/)"

#: Statuses that mean the host answered and said there is nothing here.  Everything else -- a
#: timeout, a 403 from a bot filter, a 500 -- is "unknown", because none of them is evidence about
#: whether a club exists.
GONE_STATUSES = frozenset({404, 410})


@dataclass(frozen=True)
class FetchResult:
    """What one HTTP fetch produced.  ``reachable`` is None when nothing was learned."""

    url: str
    reachable: bool | None
    note: str
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.reachable is True


def fetch(url: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS, want_text: bool = False) -> FetchResult:
    """Fetch one URL and say what happened, without ever raising.

    Shared with the directory readers and the link crawl in 8c/8d.  ``want_text`` is what separates
    "is anybody home" from "give me the page to extract from": a verification pass over three
    hundred clubs has no use for three hundred pages of HTML in memory.
    """
    import requests

    if not url:
        return FetchResult(url=url, reachable=None, note="no url")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.exceptions.SSLError as error:
        # A broken certificate is a site somebody stopped paying attention to, but the host is
        # answering, so this is not evidence the club is gone.
        return FetchResult(url=url, reachable=None, note=f"ssl error: {str(error)[:80]}")
    except (requests.exceptions.ConnectionError, socket.gaierror) as error:
        # The name does not resolve or nothing is listening.  This is the one network failure that
        # is real evidence: the club's domain has lapsed.
        return FetchResult(url=url, reachable=False, note=f"unreachable: {str(error)[:80]}")
    except requests.exceptions.RequestException as error:
        return FetchResult(url=url, reachable=None, note=f"failed: {str(error)[:80]}")
    if response.status_code in GONE_STATUSES:
        return FetchResult(url=url, reachable=False, note=f"http {response.status_code}")
    if response.status_code >= 400:
        return FetchResult(url=url, reachable=None, note=f"http {response.status_code}")
    return FetchResult(
        url=url,
        reachable=True,
        note=f"http {response.status_code}",
        text=response.text if want_text else "",
    )


def verify_club(club) -> dict:
    """Fetch this club's homepage and Facebook page and record what answered.

    Returns the fields it wrote, so a caller can report them.  A club with no links at all is still
    stamped as checked: "we looked and there was nothing to look at" is the answer that puts it in
    front of somebody, and leaving the date null would make it look like the run had skipped it.
    """
    notes = []
    fields = {"date_links_checked": timezone.now()}
    for field_name, url_attribute in (("homepage_reachable", "homepage"), ("facebook_reachable", "facebook_page")):
        url = (getattr(club, url_attribute, "") or "").strip()
        if not url:
            fields[field_name] = None
            continue
        result = fetch(url)
        fields[field_name] = result.reachable
        notes.append(f"{url_attribute}: {result.note}")
    fields["link_check_note"] = "; ".join(notes)[:200] or "no links on file"
    for field_name, value in fields.items():
        setattr(club, field_name, value)
    club.save(update_fields=list(fields))
    return fields


def clubs_due_for_verification(queryset=None, *, now=None):
    """Clubs never checked, or checked longer ago than :data:`CHECK_INTERVAL_DAYS`.

    Never-checked first, because a club nobody has ever verified is the one whose entry might be
    fiction.
    """
    from django.db.models import Q

    from .models import Club

    now = now or timezone.now()
    queryset = Club.objects.all() if queryset is None else queryset
    cutoff = now - timezone.timedelta(days=CHECK_INTERVAL_DAYS)
    return queryset.filter(Q(date_links_checked__isnull=True) | Q(date_links_checked__lt=cutoff)).order_by(
        "date_links_checked", "pk"
    )


def looks_dead(club) -> tuple[bool, str]:
    """Whether every signal that this club still exists is absent, and which ones were checked.

    Deliberately unanimous.  A club with a dead website that still ran an auction here last spring
    is a club with a dead website.  Returns ``(False, reason)`` when anything at all is alive, so
    the caller can show why a club it expected to see is not nominated.
    """
    from .models import Auction, ClubMember

    if club.date_links_checked is None:
        return False, "not verified yet"
    alive = []
    if club.homepage_reachable:
        alive.append("homepage answers")
    if club.facebook_reachable:
        alive.append("Facebook page answers")
    # An unknown result is not evidence of absence -- see the module docstring -- so a club whose
    # host timed out is never nominated on the strength of that.
    if club.homepage_reachable is None and (club.homepage or "").strip():
        alive.append("homepage could not be checked")
    if club.facebook_reachable is None and (club.facebook_page or "").strip():
        alive.append("Facebook page could not be checked")
    if Auction.objects.filter(club=club, is_deleted=False).exists():
        alive.append("has auctions here")
    if ClubMember.objects.filter(club=club, is_deleted=False).exists():
        alive.append("has members here")
    if alive:
        return False, ", ".join(alive)
    return True, "no reachable links, no auctions and no members"


def dead_candidates(queryset=None):
    """Every club :func:`looks_dead` nominates, as ``[(club, reason)]``.  Never writes anything."""
    from .models import Club

    queryset = Club.objects.filter(active=True) if queryset is None else queryset
    found = []
    for club in queryset:
        dead, reason = looks_dead(club)
        if dead:
            found.append((club, reason))
    return found
