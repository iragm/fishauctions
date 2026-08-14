"""Where the species list comes from.

FishBase publishes annual snapshots; rfishbase mirrors them as parquet on source.coop, which is
what ``manage.py import_fishbase`` reads.  Two things live here because both the importer and the
attribution notice shown to users need them: the pinned snapshot version, and the URLs it implies.

**The version is pinned on purpose.**  There is no ``latest`` path segment -- rfishbase resolves
one by listing the bucket -- and resolving it at run time would mean a re-import could quietly
swap the whole species list underneath a club mid-auction.  :func:`available_versions` exists so
``--check-version`` can tell you a newer snapshot is out; bumping to it is a deliberate edit here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

#: The snapshot the species table is built from.  Bump deliberately, then re-run the import.
FISHBASE_VERSION = "v25.04"

#: The two databases on the mirror, keyed by the path segment, mapped to the ``Species.source``
#: value rows from each get.
DATABASES = {
    "fb": "fishbase",
    # SeaLifeBase is FishBase's sister database and the only one of the two that knows about
    # invertebrates.  It is *not* imported by default, and the code is kept because that decision
    # could sensibly go the other way for a marine club: `--databases fb,slb` still works.
    #
    # The reason it is off is proportion.  SeaLifeBase is 102,000 species to FishBase's 36,000,
    # and almost all of it is marine -- deep-sea molluscs, corals, things no freshwater club will
    # ever sell -- while the couple of dozen invertebrates that *do* sell are mostly missing or
    # filed under a name the hobby does not use (it has no Neocaridina davidi at all, which is
    # every cherry shrimp on earth).  So it made the picker three times bigger and slower while
    # still failing on the one search anybody tried.  Those invertebrates now come from the
    # curated list instead -- see auctions/aquarium_species.py.
    "slb": "sealifebase",
}

#: What ``manage.py import_fishbase`` loads when you don't say.  See the note above.
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
    """Every snapshot version on the mirror, oldest first.

    Parses ``CommonPrefixes`` out of the S3 listing.  Raises ``httpx.HTTPError`` if the mirror is
    unreachable -- the caller is a management command, so failing loudly is right.
    """
    response = httpx.get(_LISTING_URL.format(database=database), timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    # The listing is namespaced; matching on the local name avoids hardcoding the namespace URI.
    versions = []
    for prefix in root.iter():
        if not prefix.tag.endswith("Prefix") or not prefix.text:
            continue
        match = _VERSION_PATTERN.search(prefix.text)
        if match:
            versions.append(match.group(1))
    return sorted(set(versions))
