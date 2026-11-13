"""Seventeen reports on ``/mcp/admin/`` that each settle one growth decision, read-only and with nobody named.

Each answers a question the owner would otherwise decide by feel, and says so in its description:

=======================  ==========================================================================
``signup_funnel``        signup page views by referrer, browser family, session depth and the page they
                         came from, then accounts made and verified: bot filter or form redesign first
``entry_page_outcomes``  signed-out sessions' first page by source, and how many reach sign-up or log-in
``club_retention``       clubs by the quarter of their first auction, and how many ran a 2nd and a 5th
``unclaimed_people``     people a club added who have no account, by club, and whether they opened an invoice
``dead_ends``            pages that 404'd and pages people were on when sent to log in, by referrer
``help_search_misses``   help and palette searches that found nothing, grouped, still unanswered first
``referrers_without_club``  sites sending visitors that match no club's homepage
``seller_reach``         sellers in two or more clubs, and the club-less auctions they sold in
``feature_adoption_by_club``  which integrations each active club uses
``auction_setup_time``   creation to first lot, copied or fresh, and what gets changed after a copy
``read_feedback``        post-auction survey comments newer than a cursor, oldest first, nobody named
``club_first_touch``     where each new club's creator first arrived from, by their first signed-in view
``outreach_outcomes``    clubs contacted 60+ days ago that then ran a real auction, by club health stage
``tools_vs_retention``   whether clubs using more club tools are still running auctions
``club_funnel``          clubs out there, probably active, tried the site, ran a real auction, ran one lately
``outreach_queue``       the next clubs to email, which email, and how to reach each (no address)
``feature_email_results``  feature emails sent, clicked, opted out and bounced, per kind
=======================  ==========================================================================

**Nobody is named.** A person is a user number at most, never a name, username or email; anything a
visitor's browser or a member typed (a referrer, a search, an auction title) comes back fenced. The
answers still go through :func:`auctions.mcp.admin.private_result` like every admin read.

**Every read is bounded.** ``PageView`` is never purged and is the biggest table on the site, so each
report over it takes ``days`` (capped) and stops at :data:`MAX_ROWS` rows, saying so when it does.

``read_feedback`` can't write, so "never read twice" is a cursor: the caller passes back ``next_since``
as ``since`` and ``next_seen`` as ``seen``: ``since`` is inclusive, so rows tied at the cursor come back once
and ``seen`` drops them. ``survey_answered_on`` is set at the *first* answer, so a
comment typed after the button click can sit before the cursor and be missed; ``seen`` only removes rows,
it can't bring those back. A weekly routine that wants them passes a ``since`` a day or two earlier than
``next_since`` and the ids it has handled.

A 404 is a ``PageView`` titled :data:`NOT_FOUND_TITLE` (``404.html`` sets it), so ``dead_ends`` only
sees 404s from the deploy that added that title onward.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from django.db.models import Count, Min, Q
from django.utils import timezone

from auctions import palette_actions
from auctions.palette_actions import DANGER_SAFE, Action, _int, _ok

#: The most ``PageView`` rows one report reads; past it the report says it stopped early.
MAX_ROWS = 300_000
#: Rows of any one breakdown a report returns.
TOP = 15
#: What ``404.html`` puts in ``<title>``, and so in ``PageView.title``.
NOT_FOUND_TITLE = "Page not found"
SIGNUP_PATHS = ("/signup/",)
LOGIN_PATHS = ("/login/",)
#: First path segments whose next segment is a slug, not a page.
SLUG_PREFIXES = {"auctions", "clubs", "club", "lots", "users", "speakers", "help", "library", "species", "invoices"}
#: Referrers that are search engines, social sites or mail, never a club's own site: matched against a
#: whole label of the host, so ``planet.com`` isn't ``t.co`` and ``tubing.com`` isn't Bing.
GENERIC_LABELS = {
    "google",
    "facebook",
    "fb",
    "messenger",
    "bing",
    "duckduckgo",
    "yahoo",
    "baidu",
    "yandex",
    "ecosia",
    "instagram",
    "outlook",
    "live",
    "chatgpt",
    "openai",
    "localhost",
    "android-app",
}
GENERIC_HOSTS = {"t.co", "lm.facebook.com"}
GENERIC_FIRST_LABELS = {"mail", "webmail"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

fence = palette_actions.untrusted_short


def _days(params: dict[str, Any], default: int = 30, most: int = 90) -> int:
    return max(1, min(_int(params, "days", default) or default, most))


def _since(days: int):
    return timezone.now() - timedelta(days=days)


def _share(part: int, whole: int) -> int:
    """A whole-number percent, 0 for nothing."""
    return round(100 * part / whole) if whole else 0


def path_family(path: str) -> str:
    """``/lots/123/blue-shrimp/`` -> ``/lots/<n>/*/``: which page, never which lot or whose."""
    path = (path or "").split("?")[0].split("#")[0]
    if not path.startswith("/"):
        return "(another site)" if path else "(none)"
    out: list[str] = []
    for index, part in enumerate(p for p in path.split("/") if p):
        if part.isdigit() or _UUID.match(part):
            out.append("<n>")
        elif index == 1 and out and out[0] in SLUG_PREFIXES:
            out.append("*")
        elif out and out[-1] == "<n>":
            out.append("*")
        else:
            out.append(part.lower()[:40])
        if len(out) >= 4:
            break
    return "/" + "/".join(out) + ("/" if out else "")


def site_of(referrer: str) -> str:
    """The host of a stored referrer (``clean_referrer`` already dropped the scheme and ``www.``)."""
    text = (referrer or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    host = urlsplit(text).hostname or ""
    return host.removeprefix("www.")


def _own_host() -> str:
    from django.contrib.sites.models import Site

    return Site.objects.get_current().domain.lower().removeprefix("www.")


def referrer_label(referrer: str, own: str) -> str:
    """Where a view came from: ``(direct)``, ``(this site) /path/``, ``Google``, or another site's host."""
    text = (referrer or "").strip()
    if not text:
        return "(direct)"
    if text in ("Google", "Facebook"):
        return text
    host = site_of(text)
    if host == own or host.endswith("." + own):
        rest = text.split("/", 1)[1] if "/" in text else ""
        return "(this site) " + path_family("/" + rest)
    return fence(host or text[:60])


@lru_cache(maxsize=4096)
def browser_family(user_agent: str) -> str:
    """``Chrome Mobile``, ``Safari``, ``(crawler)``, ``(none)``: the browser, never the device."""
    from user_agents import parse

    from auctions.crawlers import CRAWLER_PATTERN

    text = user_agent or ""
    if not text:
        return "(none)"
    if CRAWLER_PATTERN.search(text):
        return "(crawler)"
    parsed = parse(text)
    if parsed.is_bot:
        return "(crawler)"
    return parsed.browser.family or "(unknown)"


def _top(counter: Counter, n: int = TOP, key: str = "name") -> list[dict[str, Any]]:
    return [{key: name, "views": count} for name, count in counter.most_common(n)]


def _views(since, **filters):
    from auctions.models import PageView

    return PageView.objects.filter(date_start__gte=since, **filters)


def _capped(queryset, fields: tuple[str, ...]):
    """``(rows, stopped_early)``, oldest first: the newest :data:`MAX_ROWS` when there are more, so a
    report that stops early is missing the start of its window, never the latest days."""
    rows = list(queryset.order_by("-date_start").values_list(*fields)[: MAX_ROWS + 1])
    stopped = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    rows.reverse()
    return rows, stopped


def _stopped_note(found: dict[str, Any], summary: str, stopped: bool) -> str:
    """Say in the summary, not only in a key, that the window was cut short."""
    if not stopped:
        return summary
    found["stopped_after_rows"] = MAX_ROWS
    return summary + f" Only the newest {MAX_ROWS:,} views were read: ask for fewer days."


def is_generic_referrer(host: str) -> bool:
    labels = host.split(".")
    return (
        host in GENERIC_HOSTS or labels[0] in GENERIC_FIRST_LABELS or any(label in GENERIC_LABELS for label in labels)
    )


def _depth_bucket(views: int) -> str:
    if views <= 1:
        return "1 page"
    if views <= 3:
        return "2-3 pages"
    if views <= 10:
        return "4-10 pages"
    return "11+ pages"


# --- E1 ---------------------------------------------------------------------------------------------


def signup_funnel(request, params: dict[str, Any]) -> dict[str, Any]:
    """Who opens the signup page and how many of them become a verified account."""
    from allauth.account.models import EmailAddress
    from django.contrib.auth.models import User

    from auctions.models import PageView

    days = _days(params)
    since = _since(days)
    own = _own_host()
    path_q = Q()
    for path in SIGNUP_PATHS:
        path_q |= Q(url__startswith=path)
    rows, stopped = _capped(
        _views(since).filter(path_q), ("session_id", "user_id", "referrer", "user_agent", "date_start")
    )
    by_referrer: Counter = Counter()
    by_browser: Counter = Counter()
    sessions: set[str] = set()
    signed_in = 0
    for session_id, user_id, referrer, user_agent, _when in rows:
        by_referrer[referrer_label(referrer, own)] += 1
        by_browser[browser_family(user_agent)] += 1
        if user_id:
            signed_in += 1
        elif session_id:
            sessions.add(session_id)
    depth: Counter = Counter()
    ids = list(sessions)
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        counts = (
            PageView.objects.filter(session_id__in=chunk, date_start__gte=since)
            .values("session_id")
            .annotate(n=Count("pk"))
            .values_list("n", flat=True)
        )
        for count in counts:
            depth[_depth_bucket(count)] += 1
    joined = User.objects.filter(date_joined__gte=since)
    made = joined.count()
    verified = EmailAddress.objects.filter(user__in=joined, verified=True).values("user").distinct().count()
    social = joined.filter(socialaccount__isnull=False).distinct().count()
    views = len(rows)
    summary = (
        f"{views:,} signup page views in {days} days from {len(sessions):,} signed-out sessions; "
        f"{made} accounts made, {verified} verified ({_share(verified, made)}%)."
    )
    crawler_views = by_browser.get("(crawler)", 0) + by_browser.get("(none)", 0)
    if views:
        summary += f" {_share(crawler_views, views)}% of the views have a crawler's browser or none."
    found = {
        "days": days,
        "views": views,
        "views_while_signed_in": signed_in,
        "signed_out_sessions": len(sessions),
        "by_referrer": _top(by_referrer, key="came_from"),
        "by_browser": _top(by_browser, key="browser"),
        "sessions_by_pages_seen": dict(sorted(depth.items())),
        "accounts_made": made,
        "accounts_verified": verified,
        "accounts_by_google_or_apple": social,
        "views_per_account": round(views / made, 1) if made else None,
    }
    return _ok(_stopped_note(found, summary, stopped), **found)


# --- E2 ---------------------------------------------------------------------------------------------


def entry_page_outcomes(request, params: dict[str, Any]) -> dict[str, Any]:
    """The first page of each signed-out session, where it came from, and whether it reached sign-up."""
    days = _days(params, default=14, most=60)
    since = _since(days)
    own = _own_host()
    rows, stopped = _capped(
        _views(since, user__isnull=True).exclude(session_id__isnull=True).exclude(session_id=""),
        ("session_id", "url", "referrer", "source", "user_agent"),
    )
    first: dict[str, tuple[str, str]] = {}
    reached: set[str] = set()
    crawlers: set[str] = set()
    for session_id, url, referrer, source, user_agent in rows:
        path = url or ""
        if session_id not in first:
            came = f"src={fence(source)}" if source else referrer_label(referrer, own)
            first[session_id] = (path_family(path), came)
            if browser_family(user_agent) == "(crawler)":
                crawlers.add(session_id)
        if path.startswith(SIGNUP_PATHS + LOGIN_PATHS):
            reached.add(session_id)
    pages: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}
    for session_id, (page, came) in first.items():
        if session_id in crawlers:
            continue
        hit = session_id in reached
        for table, key in ((pages, page), (sources, came)):
            row = table.setdefault(key, {"sessions": 0, "reached_signup_or_login": 0})
            row["sessions"] += 1
            row["reached_signup_or_login"] += hit

    def ranked(table: dict[str, dict[str, Any]], label: str) -> list[dict[str, Any]]:
        out = []
        for key, row in sorted(table.items(), key=lambda item: -item[1]["sessions"])[:TOP]:
            out.append({label: key, **row, "percent": _share(row["reached_signup_or_login"], row["sessions"])})
        return out

    total = len(first) - len(crawlers)
    hits = len(reached - crawlers)
    summary = (
        f"{total:,} signed-out sessions in {days} days; {hits:,} ({_share(hits, total)}%) reached sign-up or "
        "log-in. A session ends at sign-in, so joining an auction afterwards isn't seen here."
    )
    found = {
        "days": days,
        "sessions": total,
        "crawler_sessions_left_out": len(crawlers),
        "by_first_page": ranked(pages, "first_page"),
        "by_source": ranked(sources, "came_from"),
    }
    return _ok(_stopped_note(found, summary, stopped), **found)


# --- E3 ---------------------------------------------------------------------------------------------


def _quarter(when) -> str:
    return f"{when.year}-Q{(when.month - 1) // 3 + 1}"


def club_retention(request, params: dict[str, Any]) -> dict[str, Any]:
    """Clubs grouped by the quarter of their first auction: how many came back for a 2nd and a 5th."""
    from auctions.models import Auction

    dates: dict[int, list] = defaultdict(list)
    rows = (
        Auction.objects.filter(is_deleted=False, club__isnull=False, date_start__lte=timezone.now())
        .values_list("club_id", "date_start")
        .order_by("date_start")
    )
    for club_id, start in rows:
        dates[club_id].append(start)
    cohorts: dict[str, dict[str, Any]] = {}
    gaps: list[int] = []
    for starts in dates.values():
        row = cohorts.setdefault(_quarter(starts[0]), {"clubs": 0, "ran_a_2nd": 0, "ran_a_5th": 0})
        row["clubs"] += 1
        row["ran_a_2nd"] += len(starts) >= 2
        row["ran_a_5th"] += len(starts) >= 5
        if len(starts) >= 2:
            gaps.append((starts[1] - starts[0]).days)
    table = [
        {
            "first_auction": quarter,
            **row,
            "percent_2nd": _share(row["ran_a_2nd"], row["clubs"]),
            "percent_5th": _share(row["ran_a_5th"], row["clubs"]),
        }
        for quarter, row in sorted(cohorts.items())
    ]
    clubs = len(dates)
    seconds = sum(len(s) >= 2 for s in dates.values())
    summary = (
        f"{clubs} clubs have run an auction here; {seconds} ({_share(seconds, clubs)}%) ran a second. "
        "Recent quarters haven't had time to, so read the older rows."
    )
    return _ok(
        summary,
        clubs=clubs,
        cohorts=table,
        median_days_first_to_second=int(statistics.median(gaps)) if gaps else None,
    )


# --- E4 ---------------------------------------------------------------------------------------------


def unclaimed_people(request, params: dict[str, Any]) -> dict[str, Any]:
    """People a club typed in (auction participants, members) whose email has no account here yet."""
    from django.contrib.auth.models import User

    from auctions.models import AuctionTOS, ClubMember, Invoice

    accounts = {email.lower() for email in User.objects.exclude(email="").values_list("email", flat=True)}
    clubs: dict[str, dict[str, set]] = defaultdict(lambda: {"people": set(), "opened": set()})
    added = (
        AuctionTOS.objects.filter(user__isnull=True, manually_added=True)
        .exclude(email__isnull=True)
        .exclude(email="")
        .values_list("pk", "email", "auction__club__name")
    )
    tos_email: dict[int, tuple[str, str]] = {}
    for pk, email, club in added.iterator(chunk_size=5000):
        email = email.strip().lower()
        if email in accounts:
            continue
        name = club or "(no club)"
        clubs[name]["people"].add(email)
        tos_email[pk] = (email, name)
    members = (
        ClubMember.objects.filter(user__isnull=True, is_deleted=False)
        .exclude(email__isnull=True)
        .exclude(email="")
        .values_list("email", "club__name")
    )
    for email, club in members.iterator(chunk_size=5000):
        email = email.strip().lower()
        if email not in accounts:
            clubs[club]["people"].add(email)
    ids = list(tos_email)
    for start in range(0, len(ids), 2000):
        opened = Invoice.objects.filter(auctiontos_user_id__in=ids[start : start + 2000], opened=True)
        for tos_id in opened.values_list("auctiontos_user_id", flat=True):
            email, name = tos_email[tos_id]
            clubs[name]["opened"].add(email)
    everyone = set().union(*(row["people"] for row in clubs.values())) if clubs else set()
    opened_any = set().union(*(row["opened"] for row in clubs.values())) if clubs else set()
    table = sorted(
        (
            {
                "club": name,
                "people": len(row["people"]),
                "opened_an_invoice_link": len(row["opened"]),
                "percent_opened": _share(len(row["opened"]), len(row["people"])),
            }
            for name, row in clubs.items()
        ),
        key=lambda row: -row["people"],
    )
    limit = max(1, min(_int(params, "limit", 25) or 25, 200))
    summary = (
        f"{len(everyone):,} people a club added have no account; {len(opened_any):,} "
        f"({_share(len(opened_any), len(everyone))}%) opened an invoice link, the ones a one-tap claim would reach."
    )
    return _ok(summary, people=len(everyone), opened_an_invoice_link=len(opened_any), clubs=table[:limit])


# --- E5 ---------------------------------------------------------------------------------------------


def dead_ends(request, params: dict[str, Any]) -> dict[str, Any]:
    """Pages that 404'd, by path and referrer, and the pages people were on when sent to log in."""
    days = _days(params)
    since = _since(days)
    own = _own_host()
    missing, stopped = _capped(_views(since, title=NOT_FOUND_TITLE), ("url", "referrer"))
    paths: Counter = Counter()
    pairs: Counter = Counter()
    for url, referrer in missing:
        # The exact path matters for a 404 ("/None"), so it is kept, with numbers taken out; under a
        # prefix whose next segment names somebody or something (/users/<name>/) only the family is.
        path = dead_end_path(url)
        paths[fence(path)] += 1
        pairs[(fence(path), referrer_label(referrer, own))] += 1
    logins, stopped_logins = _capped(_views(since).filter(url__startswith=LOGIN_PATHS[0]), ("referrer",))
    from_pages: Counter = Counter()
    for (referrer,) in logins:
        label = referrer_label(referrer, own)
        if label.startswith("(this site)"):
            from_pages[label] += 1
    summary = f"{len(missing):,} not-found pages in {days} days"
    summary += f", most at {paths.most_common(1)[0][0]}." if paths else "."
    summary += f" {sum(from_pages.values()):,} log-in pages opened from one of ours."
    found = {
        "days": days,
        "not_found": len(missing),
        "not_found_by_path": _top(paths, key="path"),
        "not_found_by_path_and_referrer": [
            {"path": path, "came_from": came, "views": count} for (path, came), count in pairs.most_common(TOP)
        ],
        "login_from_page": _top(from_pages, key="was_on"),
        "note": "404s are counted from the deploy that titled the 404 page onward.",
    }
    return _ok(_stopped_note(found, summary, stopped or stopped_logins), **found)


def dead_end_path(url: str) -> str:
    path = (url or "").split("?")[0][:120]
    first = path.strip("/").split("/")[0].lower() if path.startswith("/") else ""
    if first in SLUG_PREFIXES or not path.startswith("/"):
        return path_family(path)
    return re.sub(r"\d+", "<n>", path)


# --- E6 ---------------------------------------------------------------------------------------------


def _mcp_help_query(arguments: str) -> str:
    try:
        data = json.loads(arguments or "{}")
    except ValueError:
        match = re.search(r'"(?:query|question)"\s*:\s*"([^"]*)', arguments or "")
        return match.group(1) if match else ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("query") or data.get("question") or "")


#: Palette searches shorter than this are left out of the misses: names and lot numbers, not questions.
PALETTE_MIN_WORDS = 3
#: Phrases searched again; the rest are counted, most-asked first.
RECHECKED = 200
_STOPWORDS = {
    "how", "the", "and", "for", "can", "you", "what", "does", "with", "this", "that", "are", "who", "why",
    "when", "where", "there", "have", "from", "your", "into", "about", "want", "need", "get", "use",
}  # fmt: skip


def guide_answers(help_guides, phrase: str) -> bool:
    """Whether one guide section holds every meaningful word of ``phrase``; a shared "how" isn't an answer."""
    words = [word for word in re.findall(r"[a-z0-9']{3,}", phrase) if word not in _STOPWORDS]
    if not words:
        return False
    for result in help_guides.search(phrase, limit=5):
        text = f"{result.get('heading', '')} {result.get('text', '')}".lower()
        if all(word in text for word in words):
            return True
    return False


def help_search_misses(request, params: dict[str, Any]) -> dict[str, Any]:
    """Help searches (by agents and /ai/) and palette searches that found nothing, grouped by phrase.

    Each group is searched again now, so a phrase a guide has since answered says so and sorts last.
    """
    from auctions import help_guides
    from auctions.models import CommandPaletteSearch, MCPCall
    from auctions.palette_assist import normalize_query

    days = _days(params, default=30, most=180)
    since = _since(days)
    groups: dict[str, dict[str, Any]] = {}

    from .admin import redact

    def add(text: str, where: str, who) -> None:
        # Redacted before normalizing, which would turn an email into words nothing recognises.
        phrase = normalize_query(redact(text or ""))[:80]
        if not phrase:
            return
        # A palette search of one or two words is usually somebody's name or a lot; a help
        # question is a phrase.
        if where == "palette" and len(phrase.split()) < PALETTE_MIN_WORDS:
            return
        row = groups.setdefault(phrase, {"times": 0, "people": set(), "where": set()})
        row["times"] += 1
        row["where"].add(where)
        if who:
            row["people"].add(who)

    calls = MCPCall.objects.filter(createdon__gte=since, tool="search_help", outcome=MCPCall.EMPTY, admin=False)
    for arguments, user_id in calls.values_list("arguments", "user_id")[:5000]:
        add(_mcp_help_query(arguments), "help (agents and /ai/)", user_id)
    bounces = CommandPaletteSearch.objects.filter(createdon__gte=since, result=CommandPaletteSearch.RESULT_BOUNCE)
    for search, user_id in bounces.values_list("search", "user_id")[:5000]:
        add(search, "palette", user_id)
    rows = []
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]["people"]), -item[1]["times"]))
    for phrase, row in ranked[:RECHECKED]:
        answered = guide_answers(help_guides, phrase)
        rows.append(
            {
                "phrase": fence(phrase),
                "times": row["times"],
                "people": len(row["people"]),
                "where": sorted(row["where"]),
                "a_guide_answers_it_now": answered,
            }
        )
    rows.sort(key=lambda row: (row["a_guide_answers_it_now"], -row["people"], -row["times"]))
    open_rows = [row for row in rows if not row["a_guide_answers_it_now"]]
    limit = max(1, min(_int(params, "limit", 30) or 30, 100))
    summary = (
        f"{len(groups)} phrases found nothing in {days} days; {len(open_rows)} of the top {len(rows)} "
        "still find no guide. "
        "Write help for the ones more than one person asked."
    )
    return _ok(summary, days=days, phrases=rows[:limit], still_unanswered=len(open_rows))


# --- E7 ---------------------------------------------------------------------------------------------


def club_hosts() -> set[str]:
    """Every listed club's homepage host, ``www.`` dropped."""
    from auctions.models import Club

    hosts = set()
    for homepage in Club.objects.exclude(homepage__isnull=True).exclude(homepage="").values_list("homepage", flat=True):
        text = homepage.strip().lower()
        host = site_of(re.sub(r"^https?://", "", text))
        if host:
            hosts.add(host)
    return hosts


def referrers_without_club(request, params: dict[str, Any]) -> dict[str, Any]:
    """Sites that sent visitors and match no club's homepage: clubs, forums and shops to reach out to."""
    days = _days(params, default=90, most=365)
    since = _since(days)
    own = _own_host()
    known = club_hosts()
    rows, stopped = _capped(
        _views(since).exclude(referrer__isnull=True).exclude(referrer=""), ("referrer", "session_id", "user_id", "url")
    )
    sites: dict[str, dict[str, Any]] = {}
    for referrer, session_id, user_id, url in rows:
        host = site_of(referrer)
        if not host or host == own or host.endswith("." + own) or referrer in ("Google", "Facebook"):
            continue
        if is_generic_referrer(host):
            continue
        if host in known or any(host.endswith("." + k) or k.endswith("." + host) for k in known):
            continue
        row = sites.setdefault(host, {"views": 0, "visitors": set(), "landed_on": Counter()})
        row["views"] += 1
        row["visitors"].add(session_id or f"u{user_id}")
        row["landed_on"][path_family(url or "")] += 1
    table = [
        {
            "site": fence(host),
            "views": row["views"],
            "visitors": len(row["visitors"]),
            "landed_on": row["landed_on"].most_common(1)[0][0],
        }
        for host, row in sorted(sites.items(), key=lambda item: -len(item[1]["visitors"]))
    ]
    limit = max(1, min(_int(params, "limit", 25) or 25, 100))
    summary = f"{len(table)} sites sent visitors in {days} days and match no club's homepage."
    found = {"days": days, "sites": table[:limit], "club_homepages_known": len(known)}
    return _ok(_stopped_note(found, summary, stopped), **found)


# --- E8 ---------------------------------------------------------------------------------------------


def seller_reach(request, params: dict[str, Any]) -> dict[str, Any]:
    """Sellers with lots in two or more clubs' auctions, and the club-less auctions they also sold in."""
    from auctions.models import Lot

    days = _days(params, default=365, most=1095)
    since = _since(days)
    lots = Lot.objects.filter(is_deleted=False, date_posted__gte=since, auction__isnull=False).values_list(
        "user_id", "auctiontos_seller__user_id", "auction__club__name", "auction__slug", "auction__title"
    )
    clubs: dict[int, Counter] = defaultdict(Counter)
    loose: dict[int, dict[str, str]] = defaultdict(dict)
    for user_id, tos_user_id, club, slug, title in lots.iterator(chunk_size=5000):
        seller = tos_user_id or user_id
        if not seller:
            continue
        if club:
            clubs[seller][club] += 1
        else:
            loose[seller][slug] = title
    reach = [
        {
            "user": seller,
            "clubs": sorted(counts),
            "lots": sum(counts.values()),
            "club_less_auctions": list(loose[seller])[:5],
        }
        for seller, counts in clubs.items()
        if len(counts) >= 2
    ]
    reach.sort(key=lambda row: (-len(row["clubs"]), -row["lots"]))
    unfiled = Counter()
    for seller in clubs:
        if len(clubs[seller]) >= 2:
            for slug in loose[seller]:
                unfiled[slug] += 1
    limit = max(1, min(_int(params, "limit", 25) or 25, 100))
    summary = (
        f"{len(reach)} sellers sold in two or more clubs' auctions in {days} days; they also sold in "
        f"{len(unfiled)} auctions with no club. describe_person on a user number gives the rest."
    )
    return _ok(
        summary,
        days=days,
        sellers=reach[:limit],
        club_less_auctions_by_multi_club_sellers=[
            {"auction": slug, "sellers": count} for slug, count in unfiled.most_common(TOP)
        ],
    )


# --- E9 ---------------------------------------------------------------------------------------------

FEATURES = (
    "square",
    "paypal",
    "breeder_award_program",
    "memberships",
    "events",
    "website_embeds",
    "api",
    "discord",
    "mailing_list",
    "google_calendar",
    "donations",
)


def feature_adoption_by_club(request, params: dict[str, Any]) -> dict[str, Any]:
    """Which integrations each club with a recent auction uses, and which it doesn't."""
    from auctions.models import Auction, Club, ClubAPIKey, ClubEvent, PayPalSeller, SquareSeller

    days = _days(params, default=365, most=1095)
    since = _since(days)
    active_ids = set(
        Auction.objects.filter(is_deleted=False, club__isnull=False, date_start__gte=since).values_list(
            "club_id", flat=True
        )
    )
    square = set(SquareSeller.objects.filter(club__isnull=False).values_list("club_id", flat=True))
    paypal = set(PayPalSeller.objects.filter(club__isnull=False).values_list("club_id", flat=True))
    events = set(ClubEvent.objects.filter(date_start__gte=since).values_list("club_id", flat=True))
    api = set(ClubAPIKey.objects.filter(is_active=True, last_used_at__gte=since).values_list("club_id", flat=True))
    auctions = dict(
        Auction.objects.filter(is_deleted=False, club_id__in=active_ids, date_start__gte=since)
        .values("club_id")
        .annotate(n=Count("pk"))
        .values_list("club_id", "n")
    )
    table = []
    totals: Counter = Counter()
    for club in Club.objects.filter(pk__in=active_ids).order_by("name"):
        uses = {
            "square": club.pk in square,
            "paypal": club.pk in paypal or bool(club.paypal_client_id),
            "breeder_award_program": club.enable_breeder_award_program,
            "memberships": club.enable_membership,
            "events": club.pk in events,
            "website_embeds": bool(club.events_website_last_view and club.events_website_last_view >= since),
            "api": club.pk in api,
            "discord": bool(club.discord_server_id),
            "mailing_list": bool(club.mailchimp_connected_on or club.brevo_connected_on),
            "google_calendar": bool(club.google_calendar_connected_on),
            "donations": club.enable_donation_tracking,
        }
        for name, used in uses.items():
            totals[name] += bool(used)
        table.append(
            {
                "club": club.name,
                "auctions": auctions.get(club.pk, 0),
                "uses": sorted(name for name, used in uses.items() if used),
                "doesnt_use": sorted(name for name, used in uses.items() if not used),
            }
        )
    table.sort(key=lambda row: -row["auctions"])
    limit = max(1, min(_int(params, "limit", 40) or 40, 200))
    summary = f"{len(table)} clubs ran an auction in {days} days. " + ", ".join(
        f"{name.replace('_', ' ')} {totals[name]}" for name in FEATURES
    )
    return _ok(summary + ".", days=days, clubs_using=dict(totals), clubs=table[:limit])


# --- E10 --------------------------------------------------------------------------------------------


def auction_setup_time(request, params: dict[str, Any]) -> dict[str, Any]:
    """From creating an auction to its first lot, copied or fresh, and the rules changed after a copy."""
    from auctions.models import Auction, AuctionHistory

    days = _days(params, default=180, most=730)
    since = _since(days)
    auctions = list(
        Auction.objects.filter(is_deleted=False, date_posted__gte=since)
        .annotate(first_lot=Min("lot__date_posted", filter=Q(lot__is_deleted=False)))
        .values_list("pk", "date_posted", "first_lot", "is_online")
    )
    copied = set(
        AuctionHistory.objects.filter(
            auction_id__in=[row[0] for row in auctions], action__startswith="Created auction by copying"
        ).values_list("auction_id", flat=True)
    )
    hours: dict[str, list[float]] = {"copied": [], "fresh": []}
    no_lots: Counter = Counter()
    for pk, posted, first_lot, _online in auctions:
        kind = "copied" if pk in copied else "fresh"
        if first_lot:
            hours[kind].append((first_lot - posted).total_seconds() / 3600)
        else:
            no_lots[kind] += 1
    changed: Counter = Counter()
    changed_auctions: set[int] = set()
    history = AuctionHistory.objects.filter(auction_id__in=copied, applies_to="RULES").values_list(
        "auction_id", "changed_fields"
    )
    for auction_id, fields in history:
        if isinstance(fields, dict) and fields:
            changed_auctions.add(auction_id)
            for name in fields:
                changed[name] += 1

    def describe(kind: str) -> dict[str, Any]:
        values = hours[kind]
        return {
            "auctions": len(values) + no_lots[kind],
            "with_no_lot_yet": no_lots[kind],
            "median_hours_to_first_lot": round(statistics.median(values), 1) if values else None,
        }

    summary = (
        f"{len(auctions)} auctions made in {days} days, {len(copied)} by copying another. "
        f"Median hours to the first lot: copied {describe('copied')['median_hours_to_first_lot']}, "
        f"fresh {describe('fresh')['median_hours_to_first_lot']}."
    )
    return _ok(
        summary,
        days=days,
        copied=describe("copied"),
        fresh=describe("fresh"),
        copies_edited_afterwards=len(changed_auctions),
        fields_changed_after_a_copy=[{"field": name, "times": count} for name, count in changed.most_common(TOP)],
    )


# --- E11 --------------------------------------------------------------------------------------------

FEEDBACK_DEFAULT_DAYS = 7


def _parse_since(text: str):
    """An aware datetime from an ISO string, or ``None`` when it isn't one."""
    from django.utils.dateparse import parse_datetime

    try:
        when = parse_datetime((text or "").strip())
    except ValueError:
        return None
    if when is None:
        return None
    return timezone.make_aware(when) if timezone.is_naive(when) else when


def _ids(text: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", text or "")][:2000]


def read_feedback(request, params: dict[str, Any]) -> dict[str, Any]:
    """Survey comments answered after ``since``, oldest first, with a cursor so none is read twice."""
    from auctions.models import AuctionTOS

    raw = str(params.get("since") or "").strip()
    since = _parse_since(raw) if raw else timezone.now() - timedelta(days=FEEDBACK_DEFAULT_DAYS)
    if since is None:
        return palette_actions._error(
            f"since must be an ISO datetime such as 2026-01-31T12:00:00+00:00, not {raw[:40]!r}"
        )
    limit = max(1, min(_int(params, "limit", 50) or 50, 200))
    seen = _ids(str(params.get("seen") or ""))
    rows = (
        AuctionTOS.objects.filter(survey_answered_on__gte=since)
        .exclude(survey_comments="")
        .exclude(pk__in=seen)
        .select_related("auction", "auction__club")
        .order_by("survey_answered_on", "pk")
    )
    found = list(rows[: limit + 1])
    more = len(found) > limit
    found = found[:limit]
    items = [
        {
            "feedback": tos.pk,
            "answered_on": tos.survey_answered_on.isoformat(),
            "answer": tos.survey_answer or "",
            "comments": palette_actions.untrusted(tos.survey_comments),
            "auction": fence(tos.auction.title),
            "auction_id": tos.auction_id,
            "club": tos.auction.club.name if tos.auction.club_id else None,
        }
        for tos in found
    ]
    next_since = found[-1].survey_answered_on.isoformat() if found else since.isoformat()
    # since is inclusive, so the rows at exactly next_since come back as seen= next time, not as new rows.
    next_seen = [tos.pk for tos in found if tos.survey_answered_on == found[-1].survey_answered_on] if found else seen
    summary = f"{len(items)} survey comments after {since.isoformat()}"
    summary += "; more remain, call again with next_since." if more else "."
    return _ok(
        summary,
        feedback=items,
        more=more,
        next_since=next_since,
        next_seen=",".join(str(pk) for pk in next_seen),
        note=(
            "Comments about the website itself (not an auction or a club) go to suggest_feature. "
            "A comment added after its answer button was clicked can sit before the cursor."
        ),
    )


# --- E12 --------------------------------------------------------------------------------------------

FIRST_TOUCH_VIEWS = 50
NO_TOUCH = "(direct or unknown)"


def _arrival_label(rows, own: str) -> str | None:
    """The label of the first view in ``rows`` that says how somebody arrived, else ``None``."""
    from auctions.models import PageView

    for source, referrer in rows:
        if source and source not in PageView.INTERNAL_SOURCES:
            return PageView.SOURCE_LABELS.get(source) or fence(source)
        host = site_of(referrer)
        if host and host != own and not host.endswith("." + own):
            return referrer_label(referrer, own)
    return None


def club_first_touch(request, params: dict[str, Any]) -> dict[str, Any]:
    """Where clubs come from: how the person who made each new club's first auction first arrived."""
    from auctions.models import Auction, Club, PageView

    days = _days(params, default=365, most=1095)
    since = _since(days)
    own = _own_host()
    first: dict[int, tuple] = {}
    for club_id, pk, posted, creator in (
        Auction.objects.filter(is_deleted=False, club__isnull=False)
        .order_by("date_posted", "pk")
        .values_list("club_id", "pk", "date_posted", "created_by_id")
        .iterator(chunk_size=5000)
    ):
        first.setdefault(club_id, (posted, creator))
    recent = {club_id: row for club_id, row in first.items() if row[0] and row[0] >= since}
    names = dict(Club.objects.filter(pk__in=list(recent)).values_list("pk", "name"))
    by_label: Counter = Counter()
    table = []
    no_views = 0
    for club_id, (posted, creator) in sorted(recent.items(), key=lambda item: item[1][0]):
        views = (
            list(
                PageView.objects.filter(user_id=creator)
                .order_by("date_start", "pk")
                .values_list("source", "referrer")[:FIRST_TOUCH_VIEWS]
            )
            if creator
            else []
        )
        if not views:
            no_views += 1
        label = _arrival_label(views, own) or NO_TOUCH
        by_label[label] += 1
        table.append(
            {
                "club": names.get(club_id, "(unknown)"),
                "first_auction_made": posted.date().isoformat(),
                "first_touch": label,
            }
        )
    limit = max(1, min(_int(params, "limit", 50) or 50, 200))
    summary = (
        f"{len(table)} clubs made their first auction here in {days} days; "
        f"{len(table) - by_label.get(NO_TOUCH, 0)} have a known first arrival. Views before sign-up are "
        "anonymous, so this is the creator's first SIGNED-IN touch."
    )
    return _ok(
        summary,
        days=days,
        clubs=len(table),
        by_first_touch=_top(by_label, key="first_touch"),
        clubs_whose_creator_has_no_page_views=no_views,
        per_club=table[:limit],
    )


# --- E13 --------------------------------------------------------------------------------------------

OUTREACH_WINDOW_DAYS = 60


def outreach_outcomes(request, params: dict[str, Any]) -> dict[str, Any]:
    """Of the clubs contacted at least 60 days ago, how many then made a real auction."""
    from auctions.club_health import ClubHealth, is_test_auction
    from auctions.models import Auction, Club

    days = _days(params, default=365, most=1095)
    now = timezone.now()
    window = timedelta(days=OUTREACH_WINDOW_DAYS)
    clubs = list(
        Club.objects.filter(date_contacted__gte=now - timedelta(days=days), date_contacted__lte=now - window).order_by(
            "date_contacted"
        )
    )
    ids = [club.pk for club in clubs]
    stages = dict(ClubHealth.objects.filter(club_id__in=ids).values_list("club_id", "stage"))
    posted: dict[int, list] = defaultdict(list)
    for auction in Auction.objects.filter(club_id__in=ids, is_deleted=False).only(
        "club_id", "date_posted", "slug", "title"
    ):
        if not is_test_auction(auction):
            posted[auction.club_id].append(auction.date_posted)
    table = []
    by_stage: dict[str, dict[str, int]] = defaultdict(lambda: {"contacted": 0, "moved": 0})
    stalls: Counter = Counter()
    moved = 0
    before_total = 0
    for club in clubs:
        contacted = club.date_contacted
        after = sum(1 for when in posted[club.pk] if when and contacted < when <= contacted + window)
        before = sum(1 for when in posted[club.pk] if when and contacted - window <= when <= contacted)
        stage = stages.get(club.pk, "(no health row)")
        by_stage[stage]["contacted"] += 1
        by_stage[stage]["moved"] += bool(after)
        moved += bool(after)
        before_total += before
        stalls[club.stall_reason or "(not known)"] += 1
        table.append(
            {
                "club": club.name,
                "contacted_on": contacted.date().isoformat(),
                "auctions_in_60_days_after": after,
                "auctions_in_60_days_before": before,
                "stage_now": stage,
            }
        )
    table.sort(key=lambda row: (-row["auctions_in_60_days_after"], row["contacted_on"]))
    limit = max(1, min(_int(params, "limit", 50) or 50, 200))
    summary = (
        f"{len(clubs)} clubs were contacted {OUTREACH_WINDOW_DAYS}+ days ago; {moved} ({_share(moved, len(clubs))}%) "
        f"made a real auction within {OUTREACH_WINDOW_DAYS} days after. Before contact they had made {before_total} "
        "in the 60 days before, so some of that is clubs that were already running."
    )
    return _ok(
        summary,
        days=days,
        contacted=len(clubs),
        moved=moved,
        share_moved=_share(moved, len(clubs)),
        real_auctions_in_60_days_before_contact=before_total,
        by_stage_now=[
            {"stage": stage, **row, "percent_moved": _share(row["moved"], row["contacted"])}
            for stage, row in sorted(by_stage.items())
        ],
        stall_reasons=_top(stalls, key="stall_reason"),
        per_club=table[:limit],
    )


# --- E14 --------------------------------------------------------------------------------------------

RUNNING_STAGES = ("active", "slipping", "new")


def _tool_bucket(count: int) -> str:
    return "0 tools" if count == 0 else "1-2 tools" if count <= 2 else "3+ tools"


def tools_vs_retention(request, params: dict[str, Any]) -> dict[str, Any]:
    """Whether clubs that use more club tools are still running auctions."""
    from auctions.club_health import ClubHealth

    health = list(ClubHealth.objects.filter(real_auctions__gte=1).values_list("stage", "real_auctions", "tools_used"))
    groups: dict[str, list] = defaultdict(list)
    per_tool: dict[str, dict[str, int]] = defaultdict(
        lambda: {"using": 0, "using_running": 0, "not": 0, "not_running": 0}
    )
    every_tool = {tool for _stage, _n, tools in health for tool in (tools or [])}
    for stage, real, tools in health:
        tools = tools if isinstance(tools, list) else []
        groups[_tool_bucket(len(tools))].append((stage, real))
        running = stage in RUNNING_STAGES
        for tool in every_tool:
            key = "using" if tool in tools else "not"
            per_tool[tool][key] += 1
            per_tool[tool][key + "_running"] += running
    table = []
    for bucket in ("0 tools", "1-2 tools", "3+ tools"):
        rows = groups.get(bucket, [])
        stages = Counter(stage for stage, _real in rows)
        table.append(
            {
                "tools_used": bucket,
                "clubs": len(rows),
                **{stage: stages.get(stage, 0) for stage in ("active", "slipping", "dormant", "new")},
                "percent_still_running": _share(sum(stages.get(s, 0) for s in RUNNING_STAGES), len(rows)),
                "median_real_auctions": statistics.median(real for _stage, real in rows) if rows else None,
            }
        )
    tools = [
        {
            "tool": tool,
            "clubs_using": row["using"],
            "percent_still_running_using": _share(row["using_running"], row["using"]),
            "clubs_not_using": row["not"],
            "percent_still_running_not_using": _share(row["not_running"], row["not"]),
        }
        for tool, row in sorted(per_tool.items(), key=lambda item: -item[1]["using"])
    ]
    summary = (
        f"{len(health)} clubs have run a real auction. Still running means active or slipping. "
        "Correlation, not proof: clubs that are already running may simply adopt more tools."
    )
    return _ok(summary, clubs=len(health), by_tools_used=table, by_tool=tools)


#: The funnel's steps, each a subset of the one before: (key, what it means).
FUNNEL_STEPS = (
    ("clubs", "every club in the directory"),
    ("probably_active", "not marked inactive or folded"),
    ("tried_the_site", "made any auction here, a test one included"),
    ("ran_a_real_auction", "made an auction not named like a test"),
    ("used_it_in_window", "a real auction that started inside the window"),
)


def club_funnel(request, params: dict[str, Any]) -> dict[str, Any]:
    """Clubs out there, probably active, tried the site, ran a real auction, and ran one inside the window.

    Read from ``Auction`` rather than the nightly ``ClubHealth``, so an auction filed under a club today
    counts today: there are a few hundred club auctions, not ``PageView``'s millions. "Probably active"
    trusts the hand-set ``Club.active`` and ``stall_reason``; a club marked inactive that still runs
    auctions is named in ``inactive_but_running`` so the flag can be fixed.
    """
    from auctions.club_health import is_test_auction
    from auctions.models import Auction, Club

    days = _days(params, default=365, most=1095)
    now = timezone.now()
    since = _since(days)
    limit = max(1, min(_int(params, "limit", 30) or 30, 200))
    offset = max(0, _int(params, "offset", 0) or 0)
    tried: set[int] = set()
    real: set[int] = set()
    recent: set[int] = set()
    for auction in (
        Auction.objects.filter(is_deleted=False, club__isnull=False)
        .only("club_id", "slug", "title", "date_start")
        .iterator(chunk_size=2000)
    ):
        tried.add(auction.club_id)
        if not is_test_auction(auction):
            real.add(auction.club_id)
            if auction.date_start and since <= auction.date_start <= now:
                recent.add(auction.club_id)
    clubs = list(Club.objects.order_by("name").values_list("pk", "name", "active", "stall_reason"))
    names = {pk: name for pk, name, _active, _stall in clubs}
    probably_active = {pk for pk, _name, active, stall in clubs if active and stall != "folded"}
    members = {
        "clubs": set(names),
        "probably_active": probably_active,
        "tried_the_site": tried,
        "ran_a_real_auction": real,
        "used_it_in_window": recent,
    }
    remaining = [pk for pk, *_rest in clubs]
    steps = []
    stopped = {}
    for key, meaning in FUNNEL_STEPS:
        passed = [pk for pk in remaining if pk in members[key]]
        if steps:
            gone = [pk for pk in remaining if pk not in members[key]]
            stopped[f"stopped_before_{key}"] = {
                "clubs": len(gone),
                "names": [fence(names[pk]) for pk in gone[offset : offset + limit]],
                "next_offset": offset + limit if offset + limit < len(gone) else None,
            }
        steps.append(
            {
                "step": key,
                "means": meaning,
                "clubs": len(passed),
                "percent_of_previous": _share(len(passed), len(remaining)),
            }
        )
        remaining = passed
    stalls = {pk: stall for pk, _name, _active, stall in clubs}
    never_tried = Counter(stalls[pk] or "(not known)" for pk in probably_active - tried)
    outside = sorted(recent - probably_active, key=lambda pk: names[pk])
    unfiled = Auction.objects.filter(
        is_deleted=False, club__isnull=True, date_start__gte=since, date_start__lte=now
    ).only("slug", "title")
    unfiled_real = sum(1 for auction in unfiled.iterator() if not is_test_auction(auction))
    counts = ", ".join(f"{step['clubs']} {step['step'].replace('_', ' ')}" for step in steps)
    summary = (
        f"{counts} (the window is the last {days} days). "
        f"{unfiled_real} real auction(s) in the window have no club, so a club using the site can be missing "
        "here until they're filed (admin_unlinked_auctions)."
    )
    if outside:
        summary += f" {len(outside)} club(s) marked inactive or folded still ran a real auction in the window."
    return _ok(
        summary,
        days=days,
        steps=steps,
        never_tried_by_stall_reason=[
            {"stall_reason": reason, "clubs": count} for reason, count in never_tried.most_common()
        ],
        real_auctions_with_no_club=unfiled_real,
        inactive_but_running=[fence(names[pk]) for pk in outside[:limit]],
        **stopped,
    )


#: Clubs ``outreach_queue`` writes to, by ``ClubHealth`` stage: not running auctions here now. A club
#: with no health row yet counts as ``empty``.
COLD_STAGES = ("aware", "trial", "dormant", "empty")
#: How long a contacted club stays off the queue: people change jobs in a club.
RECONTACT_DAYS = 730
CONTACT_ORDER = {"email": 0, "webform": 1, "facebook": 2, "": 3}


def _miles(a, b) -> float:
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def outreach_queue(request, params: dict[str, Any]) -> dict[str, Any]:
    """The clubs to write to this week, one email each, and which email.

    Not running auctions here (``COLD_STAGES``), not folded or not interested, reachable somehow, and not
    contacted in :data:`RECONTACT_DAYS`. Clubs with members here come first, then those that tried and
    stopped, then the rest; email before web form before Facebook. Version ``b`` (don't ask them to
    switch) goes to a club marked ``other_software`` or stalled on ``uses_other``. No address leaves: the
    row says which door to knock on and links the club's admin page, where the owner reads it.
    """
    from auctions.club_health import ClubHealth
    from auctions.models import Auction, Club

    now = timezone.now()
    days = max(30, min(_int(params, "days_since_contact", RECONTACT_DAYS) or RECONTACT_DAYS, 3650))
    limit = max(1, min(_int(params, "limit", 10) or 10, 50))
    stages = dict(ClubHealth.objects.values_list("club_id", "stage"))
    clubs = (
        Club.objects.filter(active=True)
        .exclude(stall_reason__in=("folded", "not_interested"))
        .filter(Q(date_contacted__isnull=True) | Q(date_contacted__lt=now - timedelta(days=days)))
    )
    candidates = []
    for club in clubs:
        stage = stages.get(club.pk, "empty")
        reachable = club.contact_method or club.contact_email or club.homepage or club.facebook_page
        if stage in COLD_STAGES and reachable:
            candidates.append((club, stage))
    candidates.sort(
        key=lambda row: (
            COLD_STAGES.index(row[1]),
            CONTACT_ORDER.get(row[0].contact_method or ("email" if row[0].contact_email else ""), 3),
            row[0].date_contacted or now.replace(year=1970),
        )
    )
    running = [
        club
        for club in Club.objects.filter(health__stage__in=("active", "new"), latitude__isnull=False)
        if club.longitude is not None
    ]
    domain = _own_host()
    rows = []
    for club, stage in candidates[:limit]:
        nearest = None
        if club.latitude is not None and club.longitude is not None and running:
            other = min(running, key=lambda o: _miles((club.latitude, club.longitude), (o.latitude, o.longitude)))
            nearest = {
                "club": fence(other.name),
                "miles": round(_miles((club.latitude, club.longitude), (other.latitude, other.longitude))),
            }
        last = Auction.objects.filter(club=club, is_deleted=False).order_by("-date_posted").first()
        version = "b" if club.other_software or club.stall_reason == "uses_other" else "a"
        rows.append(
            {
                "club": club.pk,
                "name": fence(club.name),
                "stage": stage,
                "email_version": version,
                "possible_mygroupauctions": club.other_software == "mygroupauctions",
                "other_software": club.other_software,
                "contact_method": club.contact_method or ("email" if club.contact_email else ""),
                "has_contact_email": bool((club.contact_email or "").strip()),
                "homepage": fence(club.homepage or ""),
                "facebook_page": fence(club.facebook_page or ""),
                "last_contacted": club.date_contacted.date().isoformat() if club.date_contacted else None,
                "nearest_club_running_auctions_here": nearest,
                "last_auction_creator_user": last.created_by_id if last else None,
                "admin_url": f"https://{domain}/admin/auctions/club/{club.pk}/change/",
            }
        )
    summary = (
        f"{len(candidates)} clubs are due a first or renewed outreach email; here are the next {len(rows)}. "
        "Version a introduces the site; b doesn't ask a club with its own system to switch. "
        "After sending, propose record_outreach for each club so it leaves the queue."
    )
    return _ok(summary, due=len(candidates), clubs=rows)


def feature_email_results(request, params: dict[str, Any]) -> dict[str, Any]:
    """How each feature email is doing: sent, clicked, opted out, bounced; and whether they're paused."""
    from auctions import feature_emails
    from auctions.models import FeatureEmail, FeatureEmailPause

    days = _days(params, default=30, most=365)
    since = _since(days)
    rows = list(FeatureEmail.objects.filter(sent_on__gte=since).select_related("user__userdata"))
    clicks = set(_capped(_views(since, source__startswith="email_", user__isnull=False), ("user_id", "source"))[0])
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"sent": 0, "clicked": 0, "opted_out": 0, "bounced": 0})
    for row in rows:
        counts = by_kind[row.kind]
        data = row.user.userdata
        counts["sent"] += 1
        counts["clicked"] += (row.user_id, f"email_{row.kind}") in clicks
        counts["opted_out"] += bool(data.has_unsubscribed or not data.email_me_about_new_features)
        counts["bounced"] += bool(data.email_bounced_on and data.email_bounced_on >= row.sent_on)
    pause = FeatureEmailPause.objects.order_by("-created_on").first()
    start = feature_emails.start_date()
    untracked = {kind.key for kind in feature_emails.KINDS if not kind.tracked}
    table = [
        {"kind": kind, **counts, **({"clicked": None} if kind in untracked else {})}
        for kind, counts in sorted(by_kind.items(), key=lambda item: -item[1]["sent"])
    ]
    sent = sum(row["sent"] for row in table)
    summary = (
        f"{sent} feature emails in the last {days} days. "
        + (f"Paused since {pause.created_on.date()}: {pause.reason}. " if pause else "")
        + (
            f"Sending starts {start}; today's cap is {feature_emails.daily_cap(timezone.localdate())}."
            if start
            else "Turned off."
        )
    )
    return _ok(
        summary,
        days=days,
        starts=start.isoformat() if start else None,
        paused=pause.reason if pause else None,
        cap_today=feature_emails.daily_cap(timezone.localdate()),
        by_kind=table,
    )


# --- the catalogue ----------------------------------------------------------------------------------

_DAYS = "integer, optional, default {default}, at most {most}. How far back to look."


def _days_param(default: int, most: int) -> str:
    return _DAYS.format(default=default, most=most)


READS: list[Action] = [
    Action(
        name="signup_funnel",
        description=(
            "The signup page's views by where they came from, browser family (crawlers marked) and how "
            "many pages the session saw, then accounts made, verified and made with Google or Apple. "
            "Settles whether to filter bots or redesign the form first. Nobody is named."
        ),
        params={"days": _days_param(30, 90)},
        danger=DANGER_SAFE,
        resolver=signup_funnel,
    ),
    Action(
        name="entry_page_outcomes",
        description=(
            "The first page of each signed-out session, grouped by page and by source, with the share "
            "that went on to sign up or log in. Settles which page gets a join control. Crawlers left out."
        ),
        params={"days": _days_param(14, 60)},
        danger=DANGER_SAFE,
        resolver=entry_page_outcomes,
    ),
    Action(
        name="club_retention",
        description=(
            "Clubs grouped by the quarter of their first auction here, and the share that ran a 2nd and "
            "a 5th, with the median days between the first two. Settles recurring auctions or onboarding."
        ),
        params={},
        danger=DANGER_SAFE,
        resolver=club_retention,
    ),
    Action(
        name="unclaimed_people",
        description=(
            "People a club added (auction participants and members typed in) whose email has no "
            "account here, counted by club, and how many opened an invoice link. Sizes a one-tap "
            "account claim. Counts only, no emails."
        ),
        params={"limit": "integer, optional, default 25, at most 200. How many clubs to list."},
        danger=DANGER_SAFE,
        resolver=unclaimed_people,
    ),
    Action(
        name="dead_ends",
        description=(
            "Pages people reached that don't exist (404s), by path and by the page that linked there, "
            "and the pages of ours people were on when sent to log in. Finds broken links before "
            "anyone reports them."
        ),
        params={"days": _days_param(30, 90)},
        danger=DANGER_SAFE,
        resolver=dead_ends,
    ),
    Action(
        name="help_search_misses",
        description=(
            "Help searches by agents and /ai/, and command palette searches, that found nothing, grouped "
            "by phrase with how many people asked; each is searched again now and marked if a guide "
            "answers it since. What help to write next."
        ),
        params={
            "days": _days_param(30, 180),
            "limit": "integer, optional, default 30, at most 100.",
        },
        danger=DANGER_SAFE,
        resolver=help_search_misses,
    ),
    Action(
        name="referrers_without_club",
        description=(
            "Sites that sent visitors here and match no club's homepage (search engines and social "
            "sites left out), with visitors and the page they landed on. Clubs and forums to reach out to."
        ),
        params={
            "days": _days_param(90, 365),
            "limit": "integer, optional, default 25, at most 100.",
        },
        danger=DANGER_SAFE,
        resolver=referrers_without_club,
    ),
    Action(
        name="seller_reach",
        description=(
            "Sellers with lots in two or more clubs' auctions, by user number, with the clubs and any "
            "auctions with no club they also sold in. Warm introductions between clubs."
        ),
        params={
            "days": _days_param(365, 1095),
            "limit": "integer, optional, default 25, at most 100.",
        },
        danger=DANGER_SAFE,
        resolver=seller_reach,
    ),
    Action(
        name="feature_adoption_by_club",
        description=(
            "For each club with a recent auction: which of Square, PayPal, the breeder award program, "
            "memberships, events, website embeds, the API, Discord, a mailing list, Google Calendar and "
            "donations it uses and which it doesn't. What to suggest to each club."
        ),
        params={
            "days": _days_param(365, 1095),
            "limit": "integer, optional, default 40, at most 200.",
        },
        danger=DANGER_SAFE,
        resolver=feature_adoption_by_club,
    ),
    Action(
        name="auction_setup_time",
        description=(
            "Hours from creating an auction to its first lot, for copied and fresh auctions, how many "
            "have no lot yet, and which rules people change after copying. Whether repeat auctions are "
            "getting easier."
        ),
        params={"days": _days_param(180, 730)},
        danger=DANGER_SAFE,
        resolver=auction_setup_time,
    ),
    Action(
        name="read_feedback",
        description=(
            "Post-auction survey comments (what people typed after 'How was the auction?'), oldest first, "
            "each with its answer (great or not_fun), auction and club, and nobody named. Reads never write, "
            "so a routine passes the returned next_since back as since and next_seen back as seen, and "
            "nothing is read twice. Feedback about the website itself (not the auction "
            "or the club) should go to suggest_feature."
        ),
        params={
            "since": (
                "string, optional, default 7 days ago. An ISO datetime; comments answered at or after it are "
                "returned. Pass the previous call's next_since."
            ),
            "limit": "integer, optional, default 50, at most 200.",
            "seen": "string, optional. Comma-separated feedback ids already handled, left out of the result: the previous call's next_seen.",
        },
        danger=DANGER_SAFE,
        resolver=read_feedback,
    ),
    Action(
        name="club_first_touch",
        description=(
            "Where clubs come from: for clubs whose first auction here was made in the window, how the "
            "person who made it first arrived (a tracked link, Google, Facebook, another site, or direct or "
            "unknown), by count and per club. Views before sign-up are anonymous, so this is the creator's "
            "first signed-in view. Clubs only, no person named."
        ),
        params={
            "days": _days_param(365, 1095),
            "limit": "integer, optional, default 50, at most 200. How many clubs to list.",
        },
        danger=DANGER_SAFE,
        resolver=club_first_touch,
    ),
    Action(
        name="outreach_outcomes",
        description=(
            "Of the clubs contacted at least 60 days ago, how many made a real (non-test) auction within 60 "
            "days after, split by the club's current health stage, with stall reasons and the auctions made "
            "in the 60 days before contact for comparison. Does outreach move clubs?"
        ),
        params={
            "days": _days_param(365, 1095),
            "limit": "integer, optional, default 50, at most 200. How many clubs to list.",
        },
        danger=DANGER_SAFE,
        resolver=outreach_outcomes,
    ),
    Action(
        name="tools_vs_retention",
        description=(
            "Do clubs that use more of the club tools keep running auctions? Settles whether to push club "
            "tools to auction-only clubs. Clubs with a real auction grouped by tools used (0, 1-2, 3+) with "
            "stage counts, percent still running and median auctions, and each tool against clubs not using it."
        ),
        params={},
        danger=DANGER_SAFE,
        resolver=tools_vs_retention,
    ),
    Action(
        name="club_funnel",
        description=(
            "How many clubs are out there, probably active, have tried the site, ran a real (non-test) "
            "auction, and ran one within the window: each step a subset of the one before, with the "
            "percent kept, the clubs that stopped before each step, and why the never-tried ones stalled."
        ),
        params={
            "days": _days_param(365, 1095),
            "limit": "integer, optional, default 30, at most 200. How many club names to list per step.",
            "offset": "integer, optional, default 0. Skip this many names in each step's list -- how you page.",
        },
        danger=DANGER_SAFE,
        resolver=club_funnel,
    ),
    Action(
        name="outreach_queue",
        description=(
            "The clubs to email this week, one email each: not running auctions here, reachable, not folded, "
            "and not contacted in two years. Each row says which email (a introduces the site, b is for a "
            "club with its own system or possibly on MyGroupAuctions), how to reach it, the nearest club "
            "running auctions here, and its admin page. No address is shown."
        ),
        params={
            "limit": "integer, optional, default 10, at most 50. How many clubs.",
            "days_since_contact": "integer, optional, default 730. Clubs contacted more recently are left out.",
        },
        danger=DANGER_SAFE,
        resolver=outreach_queue,
    ),
    Action(
        name="feature_email_results",
        description=(
            "How the feature emails are doing, per kind: sent, clicked, opted out, bounced; whether sending "
            "is paused and why, when it starts, and today's cap."
        ),
        params={"days": _days_param(30, 365)},
        danger=DANGER_SAFE,
        resolver=feature_email_results,
    ),
]
