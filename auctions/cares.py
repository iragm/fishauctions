"""The CARES conservation-priority list, kept in step with aquaristspecies.net.

CARES is the hobby's list of species at risk in the wild that aquarists keep going. The partner API
(``settings.CARES_API_URL``, key in ``CARES_PARTNER_KEY``) serves it a page at a time. :func:`sync`
marks each record's species with ``Species.cares_id`` and ``cares_classification``, and adds the
species the list is missing as ``source="cares"`` -- after looking for it by id, by its name, then by
each synonym in ``alt_name``, so a species FishBase files under an older genus is marked, not cloned.

The first run fetches the whole list; after that each run asks only for records changed since the
last finished run started, kept as ``finished`` on the ``cares_sync`` :class:`ChunkedJobState` row
(no row means never). The API has no way to say a species left
the list, so a mark is never taken off by the sync.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import CARES_CLASSIFICATIONS, ChunkedJobState, Species, SpeciesCommonName, normalize_species_name

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
TIMEOUT_SECONDS = 30
#: The API allows 120 requests an hour; a throttled page waits at most this long before giving up.
MAX_RETRY_WAIT_SECONDS = 120

#: Codes the site still holds in older spellings.
LEGACY_CODES = {"VULN": "CVU", "ENDA": "CEN", "CEND": "CEN", "NEAR": "CNT"}
CODES = dict(CARES_CLASSIFICATIONS)

#: Which list to trust first when a name is on it twice.
SOURCE_PREFERENCE = ("fishbase", "aquarium", "admin", "cares", "sealifebase", "manual")

#: A binomial or trinomial synonym ("Neoophorus regalis", "Xiphophorus maculatus jalapae").
_BINOMIAL = re.compile(r"^[A-Z][a-z]+ [a-z][a-z-]+( [a-z][a-z-]+)?$")
#: A throttled page is asked again at most this many times.
MAX_RETRIES = 3


class CaresError(Exception):
    """The API refused or broke; the run stops and its start time is not recorded."""


def enabled():
    return bool(getattr(settings, "CARES_PARTNER_KEY", ""))


def classification(code):
    """The record's code as one of :data:`CARES_CLASSIFICATIONS`, or "" for one we don't know."""
    code = str(code or "").strip().upper()
    code = LEGACY_CODES.get(code, code)
    return code if code in CODES else ""


def _next_url(url, base):
    """*url* as https on the configured API's host, or :class:`CaresError` -- the key goes nowhere else.
    ``next`` comes back as http://.
    """
    parts, home = urlsplit(url), urlsplit(base)
    if parts.netloc.lower() != home.netloc.lower():
        msg = "next page is on another host"
        raise CaresError(msg)
    return urlunsplit(("https", *parts[1:]))


def _retry_after(response):
    """Seconds the API asks us to wait, from 1 up; 60 when it doesn't say in a number."""
    try:
        return max(int(response.headers.get("Retry-After") or 60), 1)
    except (TypeError, ValueError):
        return 60


def fetch(since=None, session=None, sleep=time.sleep):
    """Every CARES record changed since *since* (all of them for ``None``), following ``next``. Never
    follows a redirect: ``requests`` would replay the key header to wherever it points.
    """
    session = session or requests.Session()
    headers = {"X-Partner-Api-Key": settings.CARES_PARTNER_KEY, "Accept": "application/json"}
    params = {"page_size": PAGE_SIZE}
    if since:
        params["since"] = since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = settings.CARES_API_URL
    url, retries = _next_url(base, base), 0
    while url:
        response = session.get(url, headers=headers, params=params, timeout=TIMEOUT_SECONDS, allow_redirects=False)
        if response.status_code == 429:
            wait = _retry_after(response)
            retries += 1
            if wait > MAX_RETRY_WAIT_SECONDS or retries > MAX_RETRIES:
                msg = f"throttled for {wait}s"
                raise CaresError(msg)
            sleep(wait)
            continue
        if response.status_code != 200:
            msg = f"HTTP {response.status_code}"
            raise CaresError(msg)
        try:
            data = response.json()
        except ValueError as error:
            msg = "not JSON"
            raise CaresError(msg) from error
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            msg = "no results list"
            raise CaresError(msg)
        yield from data["results"]
        url = _next_url(data["next"], base) if data.get("next") else None
        params, retries = None, 0  # ``next`` carries them


def _valid(record):
    """A record with an integer id and a name; anything else from the API is skipped."""
    return (
        isinstance(record, dict)
        and isinstance(record.get("id"), int)
        and not isinstance(record.get("id"), bool)
        and record["id"] > 0
        and isinstance(record.get("name"), str)
    )


def _key(name):
    from .species_matching import split_scientific_name

    return split_scientific_name(name)


def _candidates(name):
    """Species rows answering to *name* exactly, the most trusted first."""
    genus, epithet = _key(name)
    if not genus or not epithet:
        return []
    rows = Species.objects.filter(genus__iexact=genus, species__iexact=epithet, variety="", is_hybrid=False)
    return sorted(
        rows,
        key=lambda row: (
            not row.approved,
            row.parent_id is not None,
            SOURCE_PREFERENCE.index(row.source) if row.source in SOURCE_PREFERENCE else len(SOURCE_PREFERENCE),
            row.pk,
        ),
    )


def _alt_names(record):
    """``alt_name`` split into synonyms (binomials, "Genus sp. ...") and the trade's names ("L047")."""
    synonyms, names = [], []
    own = " ".join(str(record.get("name") or "").split()).lower()
    alt = record.get("alt_name")
    for piece in (alt if isinstance(alt, str) else "").split(","):
        piece = " ".join(piece.split())
        if not piece or piece.lower() == own:
            continue
        (synonyms if _BINOMIAL.match(piece) or " sp." in piece else names).append(piece)
    return synonyms, names


class Claimed(Exception):
    """The record's own name is on a row another CARES id holds: a person's question, not a new row."""


def find_species(record, others_names=frozenset()):
    """The row this record is, or None: its id, then its name, then a synonym.

    A row another record holds is skipped. A synonym never takes a row that is another record's own
    name in this run (*others_names*), so the result doesn't depend on record order. When the record's
    own name is held by another id, :class:`Claimed` -- creating it would clone a listed species.
    """
    cares_id = record["id"]
    found = Species.objects.filter(cares_id=cares_id).first()
    if found:
        return found
    own = _candidates(record["name"])
    for row in own:
        if row.cares_id is None:
            return row
    if own:
        raise Claimed
    for name in _alt_names(record)[0]:
        if _key(name) in others_names:
            continue
        for row in _candidates(name):
            if row.cares_id is None:
                return row
    return None


def _create(record, code):
    """A new species for a record nothing matched, filed with its genus's relatives."""
    from .palette_actions import _genus_relative

    genus, epithet = _key(record["name"])
    if not genus or not epithet:
        return None
    relative = _genus_relative(genus)
    species = Species(genus=genus, species=epithet, source="cares", cares_id=record["id"], cares_classification=code)
    if relative:
        species.family, species.order, species.category = relative.family, relative.order, relative.category
    if relative and (relative.freshwater or relative.brackish or relative.saltwater):
        species.freshwater, species.brackish, species.saltwater = (
            relative.freshwater,
            relative.brackish,
            relative.saltwater,
        )
    else:
        species.freshwater = True
    species.save()
    return species


def _keep_names(species, names):
    """Make *names* this species' ``source="cares"`` common names, skipping any another species answers to.
    Touches only what differs.
    """
    from .species_matching import species_carrying_common_name

    wanted = {}
    for name in names:
        normalized = normalize_species_name(name)
        if normalized:
            wanted.setdefault(normalized, name[:255])
    ours = SpeciesCommonName.objects.filter(species=species, source="cares")
    ours.exclude(name_normalized__in=wanted).delete()
    have = set(species.common_names.values_list("name_normalized", flat=True)) | {species.common_name_normalized}
    for normalized, name in wanted.items():
        if normalized in have or species_carrying_common_name(name, exclude=species):
            continue
        SpeciesCommonName.objects.create(species=species, name=name, source="cares")
        have.add(normalized)


def _rename(species, record, genera):
    """A ``source="cares"`` row follows CARES when it renames, unless the new name is already listed."""
    if species.source != "cares":
        return
    genus, epithet = _key(record["name"])
    if not genus or not epithet or (genus, epithet) == (species.genus, species.species):
        return
    if Species.objects.filter(genus__iexact=genus, species__iexact=epithet, variety="").exclude(pk=species.pk).exists():
        return
    genera.add(species.genus)
    species.genus, species.species = genus, epithet


def apply(record, counts, genera, others_names=frozenset()):
    """Mark, update or add the species for one record, in one transaction. A code we don't know is
    logged and leaves the species' classification as it was. Adds to *genera* whatever needs its
    trade ranks rebuilt.
    """
    code = classification(record.get("cares_classification"))
    with transaction.atomic():
        try:
            species = find_species(record, others_names)
        except Claimed:
            counts["claimed"] += 1
            logger.warning("CARES record %s names a species another CARES record holds", record["id"])
            return None
        if not code:
            counts["unknown_code"] += 1
            logger.warning("CARES record %s has an unknown classification", record["id"])
            if species is None or species.cares_id == record["id"]:
                return species
            code = species.cares_classification
        if species is None:
            species = _create(record, code)
            if species is None:
                counts["skipped"] += 1
                return None
            counts["created"] += 1
            genera.add(species.genus)
        else:
            before = (species.cares_id, species.cares_classification, species.genus, species.species)
            species.cares_id = record["id"]
            species.cares_classification = code
            _rename(species, record, genera)
            if before != (species.cares_id, species.cares_classification, species.genus, species.species):
                species.save()
                counts["marked"] += 1
                genera.add(species.genus)
            else:
                counts["unchanged"] += 1
        _keep_names(species, _alt_names(record)[1])
    return species


LOCK_KEY = "cares_sync_lock"
STATE_NAME = "cares_sync"
LOCK_SECONDS = 60 * 60


def sync(session=None, now=None, sleep=time.sleep):
    """One run at a time. Returns the counts; raises :class:`CaresError`. A run with a failed record keeps
    the old start time, so the next one asks for those records again.
    """
    if not enabled():
        return {"disabled": 1}
    if not cache.add(LOCK_KEY, "1", timeout=LOCK_SECONDS):
        return {"already_running": 1}
    try:
        return _sync(session, now or timezone.now(), sleep)
    finally:
        cache.delete(LOCK_KEY)


def _sync(session, now, sleep):
    from collections import Counter

    from celery.exceptions import SoftTimeLimitExceeded

    state, _created = ChunkedJobState.objects.get_or_create(name=STATE_NAME)
    counts = Counter()
    # The list is a page or two, so all of it first: every record's own name is known before synonyms.
    records = list(fetch(state.finished, session=session, sleep=sleep))
    valid = [record for record in records if _valid(record)]
    counts["skipped"] += len(records) - len(valid)
    names = Counter(_key(record["name"]) for record in valid)
    genera = set()
    for record in valid:
        own = _key(record["name"])
        others = {name for name, count in names.items() if name != own or count > 1}
        try:
            apply(record, counts, genera, others)
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # one bad record must not stop the list
            logger.exception("CARES record %s failed", record["id"])
            counts["failed"] += 1
    genera.discard("")
    for genus in genera:
        Species.recompute_trade_ranks(genus=genus)
    counts["full"] = int(state.finished is None)
    if not counts["failed"]:
        # When this run started, not ended: a record changed mid-run comes back next time.
        state.finished = now
        state.save(update_fields=["finished", "updated"])
    return dict(counts)
