"""Where the species list comes from.

FishBase publishes annual snapshots as parquet on source.coop, read by ``manage.py
import_fishbase``. The version is pinned deliberately -- there is no ``latest`` path, so an
unpinned resolve could swap the species list mid-auction; :func:`available_versions` just reports
that a newer snapshot exists.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

#: The snapshot the species table is built from. Bump deliberately, then re-run the import.
FISHBASE_VERSION = "v25.04"

#: The two databases on the mirror, keyed by path segment, mapped to the ``Species.source`` value.
DATABASES = {
    "fb": "fishbase",
    # SeaLifeBase (invertebrates) is off by default: mostly marine, missing common hobby names
    # like Neocaridina davidi. Those species come from auctions/aquarium_species.py instead.
    "slb": "sealifebase",
}

#: What ``manage.py import_fishbase`` loads when you don't say. See the note above.
DEFAULT_DATABASES = ("fb",)

_LISTING_URL = (
    "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop"
    "?list-type=2&prefix=cboettig/fishbase/{database}/&delimiter=/"
)

_PARQUET_URL = "https://data.source.coop/cboettig/fishbase/{database}/{version}/parquet/{table}.parquet"

_VERSION_PATTERN = re.compile(r"/(v\d+\.\d+)/$")


def parquet_url(table, version=FISHBASE_VERSION, database="fb"):
    """URL of one table, e.g. ``species`` or ``comnames``, from ``fb`` or ``slb``."""
    return _PARQUET_URL.format(database=database, version=version, table=table)


def available_versions(timeout=30, database="fb"):
    """Every snapshot version on the mirror, oldest first. Raises ``httpx.HTTPError`` on failure."""
    response = httpx.get(_LISTING_URL.format(database=database), timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    # Namespaced listing; match on local tag name to avoid hardcoding the namespace URI.
    versions = []
    for prefix in root.iter():
        if not prefix.tag.endswith("Prefix") or not prefix.text:
            continue
        match = _VERSION_PATTERN.search(prefix.text)
        if match:
            versions.append(match.group(1))
    return sorted(set(versions))
