"""The CARES conservation-priority list, kept in step with aquaristspecies.net.

CARES is the hobby's list of species at risk in the wild that aquarists keep going. The partner API
(``settings.CARES_API_URL``, key in ``CARES_PARTNER_KEY``) serves it a page at a time. :func:`sync`
marks each record's species with ``Species.cares_id`` and ``cares_classification``, and adds the
species the list is missing as ``source="cares"`` -- after looking for it by id, by its name, then by
each synonym in ``alt_name``, so a species FishBase files under an older genus is marked, not cloned.

The weekly task asks only for records changed since the last good run started, except when it has no
such time or the last full pass is a month old: only a full pass can see a species *leave* the list,
so only a full pass clears a classification.
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

from .models import CARES_CLASSIFICATIONS, Species, SpeciesCommonName, normalize_species_name

logger = logging.getLogger(__name__)

#: When the last complete run started, and when the last full one did. The cache, not a table: losing
#: them costs one full pass of a list that fits on one page.
LAST_RUN_KEY = "cares_sync_last_start"
LAST_FULL_KEY = "cares_sync_last_full"
FULL_EVERY = datetime.timedelta(days=28)
PAGE_SIZE = 1000
TIMEOUT_SECONDS = 30
#: The API allows 120 requests an hour; a throttled page waits at most this long before giving up.
MAX_RETRY_WAIT_SECONDS = 120

#: Codes the site still holds in older spellings.
LEGACY_CODES = {"VULN": "CVU", "ENDA": "CEN", "CEND": "CEN", "NEAR": "CNT"}
CODES = dict(CARES_CLASSIFICATIONS)

#: Which list to trust first when a name is on it twice.
SOURCE_PREFERENCE = ("fishbase", "aquarium", "admin", "cares", "sealifebase", "manual")

_BINOMIAL = re.compile(r"^[A-Z][a-z]+ [a-z][a-z-]+$")


class CaresError(Exception):
    """The API refused or broke; the run stops and its start time is not recorded."""


def enabled():
    return bool(getattr(settings, "CARES_PARTNER_KEY", ""))


def classification(code):
    """The record's code as one of :data:`CARES_CLASSIFICATIONS`, or "" for one we don't know."""
    code = (code or "").strip().upper()
    code = LEGACY_CODES.get(code, code)
    return code if code in CODES else ""


def _https(url):
    """``next`` comes back as http://; the key is not sent anywhere in the clear."""
    parts = urlsplit(url)
    return urlunsplit(("https", *parts[1:])) if parts.scheme == "http" else url


def fetch(since=None, session=None, sleep=time.sleep):
    """Every CARES record changed since *since* (all of them for ``None``), following ``next``."""
    session = session or requests.Session()
    headers = {"X-Partner-Api-Key": settings.CARES_PARTNER_KEY, "Accept": "application/json"}
    params = {"page_size": PAGE_SIZE}
    if since:
        params["since"] = since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = settings.CARES_API_URL
    while url:
        response = session.get(_https(url), headers=headers, params=params, timeout=TIMEOUT_SECONDS)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After") or 60)
            if wait > MAX_RETRY_WAIT_SECONDS:
                msg = f"throttled for {wait}s"
                raise CaresError(msg)
            sleep(wait)
            continue
        if response.status_code != 200:
            msg = f"HTTP {response.status_code}"
            raise CaresError(msg)
        data = response.json()
        yield from data.get("results") or []
        url = data.get("next")
        params = None  # ``next`` carries them


def _candidates(name):
    """Species rows answering to *name* exactly, the most trusted first."""
    from .species_matching import split_scientific_name

    genus, epithet = split_scientific_name(name)
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
    """``alt_name`` split into synonyms (binomials) and the trade's names ("L047")."""
    synonyms, names = [], []
    for piece in (record.get("alt_name") or "").split(","):
        piece = " ".join(piece.split())
        if not piece or piece.lower() == (record.get("name") or "").strip().lower():
            continue
        (synonyms if _BINOMIAL.match(piece) or " sp." in piece else names).append(piece)
    return synonyms, names


def find_species(record):
    """The row this record is, or None: its id, then its name, then a synonym. A row another record
    already holds is skipped -- two CARES ids on one species is a question for a person, not a merge.
    """
    cares_id = record["id"]
    found = Species.objects.filter(cares_id=cares_id).first()
    if found:
        return found
    synonyms, _names = _alt_names(record)
    for name in [record.get("name") or "", *synonyms]:
        for row in _candidates(name):
            if row.cares_id is None or row.cares_id == cares_id:
                return row
    return None


def _create(record, code):
    """A new species for a record nothing matched, filed with its genus's relatives."""
    from .palette_actions import _genus_relative
    from .species_matching import split_scientific_name

    genus, epithet = split_scientific_name(record.get("name"))
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
    """Make *names* this species' ``source="cares"`` common names, skipping any another species answers to."""
    from .species_matching import species_carrying_common_name

    SpeciesCommonName.objects.filter(species=species, source="cares").delete()
    have = set(species.common_names.values_list("name_normalized", flat=True)) | {species.common_name_normalized}
    for name in names:
        normalized = normalize_species_name(name)
        if not normalized or normalized in have or species_carrying_common_name(name, exclude=species):
            continue
        SpeciesCommonName.objects.create(species=species, name=name[:255], source="cares")
        have.add(normalized)


def apply(record, counts):
    """Mark, update or add the species for one record, in one transaction. A code we don't know is
    logged and leaves the species' classification as it was.
    """
    from .species_matching import split_scientific_name

    code = classification(record.get("cares_classification"))
    with transaction.atomic():
        species = find_species(record)
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
        else:
            before = (species.cares_id, species.cares_classification, species.genus, species.species)
            species.cares_id = record["id"]
            species.cares_classification = code
            if species.source == "cares":
                # Ours to rename when CARES does.
                genus, epithet = split_scientific_name(record.get("name"))
                if genus and epithet:
                    species.genus, species.species = genus, epithet
            if before != (species.cares_id, species.cares_classification, species.genus, species.species):
                species.save()
                counts["marked"] += 1
            else:
                counts["unchanged"] += 1
        _keep_names(species, _alt_names(record)[1])
    return species


LOCK_KEY = "cares_sync_lock"
LOCK_SECONDS = 60 * 60


def sync(full=None, session=None, now=None, sleep=time.sleep):
    """One run at a time. *full* ``None`` decides by the last runs. Returns the counts; raises
    :class:`CaresError`. A run with a failed record keeps the old start time, so the next one asks again.
    """
    if not enabled():
        return {"disabled": 1}
    if not cache.add(LOCK_KEY, "1", timeout=LOCK_SECONDS):
        return {"already_running": 1}
    try:
        return _sync(full, session, now or timezone.now(), sleep)
    finally:
        cache.delete(LOCK_KEY)


def _sync(full, session, now, sleep):
    from collections import Counter

    last_run = cache.get(LAST_RUN_KEY)
    last_full = cache.get(LAST_FULL_KEY)
    if full is None:
        full = not last_run or not last_full or now - last_full >= FULL_EVERY
    counts = Counter()
    seen, genera = set(), set()
    for record in fetch(None if full else last_run, session=session, sleep=sleep):
        if not isinstance(record.get("id"), int):
            counts["skipped"] += 1
            continue
        seen.add(record["id"])
        try:
            species = apply(record, counts)
        except Exception:  # one bad record must not stop the list
            logger.exception("CARES record %s failed", record.get("id"))
            counts["failed"] += 1
            continue
        if species is not None and species.genus:
            genera.add(species.genus)
    if full:
        # Only a full pass knows what has left the list.
        gone = (
            Species.objects.exclude(cares_classification="").filter(cares_id__isnull=False).exclude(cares_id__in=seen)
        )
        genera.update(gone.exclude(genus="").values_list("genus", flat=True))
        counts["cleared"] = gone.update(cares_classification="")
    for genus in genera:
        Species.recompute_trade_ranks(genus=genus)
    timeout = 60 * 60 * 24 * 365
    if not counts["failed"]:
        cache.set(LAST_RUN_KEY, now, timeout=timeout)
    if full and not counts["failed"]:
        cache.set(LAST_FULL_KEY, now, timeout=timeout)
    counts["full"] = int(full)
    return dict(counts)
