"""The curated aquarium-trade species list, and the strains that hang off it.

FishBase is 36,000 fish and nothing else.  It has never heard of a cherry shrimp, a java fern, a
culture of grindal worms, or a Blue Dream -- and those are a large share of what actually crosses
the table at a club auction.  SeaLifeBase would cover some of the invertebrates, but it brings
102,000 mostly-marine species along with them, so it is no longer imported (see
:mod:`auctions.fishbase`).

What fills the gap is ``auctions/data/aquarium_species.csv``: a hand-checked list of the plants,
freshwater invertebrates, live-food cultures and fish cultivars the hobby sells, kept in the repo
where it can be reviewed in a diff rather than edited in a database.  It is deliberately small.
The rule for adding a row is in the file's own header, and it is the same rule the rest of the
species code follows: if the identification is contested, leave it out, because a wrong species
gets printed on a label and counted for breeder points.

**Strains.**  A row with a ``variety`` is a cultivar -- 'Blue Dream', 'Halfmoon', 'Oranda'.  These
are not taxonomy; no code of nomenclature has a rank for them.  So a variety row carries its
parent's genus and epithet and links to it with :attr:`Species.parent`, which means everything
reasoning about the science (breeder points, genus BAP overrides, the family-to-category mapping)
sees the nominal species, while everything a human reads shows *Neocaridina davidi* 'Blue Dream'.
A cultivar of a fish names a FishBase parent, so those rows are skipped -- with a warning -- when
FishBase hasn't been imported.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction

from .models import Species, SpeciesCommonName, normalize_species_name
from .species_categories import CategoryResolver

logger = logging.getLogger(__name__)

#: The list itself.  Kept next to the code rather than in a fixture: it is data we maintain, not
#: data we load once into a fresh database.
DATA_FILE = Path(__file__).resolve().parent / "data" / "aquarium_species.csv"

#: ``Species.source`` for everything this module writes.
SOURCE = "aquarium"

#: ``kind`` column -> the category hint in :mod:`auctions.species_categories`.  ``fish`` is absent
#: on purpose: a fish cultivar takes its category from its FishBase parent's family.
KIND_CATEGORY_HINTS = {
    "plant": "plants",
    "invert": "invertebrates",
    "culture": "live food",
}

_HABITAT_FIELDS = {"fresh": "freshwater", "brackish": "brackish", "salt": "saltwater"}


@dataclass
class Row:
    """One line of the CSV, parsed."""

    scientific_name: str
    variety: str
    common_names: list[str]
    family: str
    order: str
    kind: str
    habitats: list[str]

    @property
    def genus(self):
        return self.scientific_name.split(" ")[0]

    @property
    def epithet(self):
        parts = self.scientific_name.split(" ", 1)
        return parts[1] if len(parts) > 1 else ""

    @property
    def is_variety(self):
        return bool(self.variety)


@dataclass
class Result:
    """What :func:`load` did, for the management command to print."""

    created: int = 0
    updated: int = 0
    common_names: int = 0
    skipped: list[str] = field(default_factory=list)


def read_rows(path=DATA_FILE):
    """Parse the CSV into :class:`Row` objects.  Blank and ``#`` lines are comments."""
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        # Strip comments before csv sees them so a '#' line can sit anywhere, including above the
        # header, without csv.DictReader trying to make a record out of it.
        lines = [line for line in handle if line.strip() and not line.lstrip().startswith("#")]
    for raw in csv.DictReader(lines):
        name = (raw.get("scientific_name") or "").strip()
        if not name:
            continue
        rows.append(
            Row(
                scientific_name=name,
                variety=(raw.get("variety") or "").strip(),
                common_names=[part.strip() for part in (raw.get("common_names") or "").split("|") if part.strip()],
                family=(raw.get("family") or "").strip(),
                order=(raw.get("order") or "").strip(),
                kind=(raw.get("kind") or "").strip(),
                habitats=[part.strip() for part in (raw.get("habitat") or "").split("|") if part.strip()],
            )
        )
    return rows


def kind_hints(path=DATA_FILE):
    """``{(scientific name, variety): category hint}`` for the curated rows.

    The ``kind`` column is the only place that knows a *Microsorum* is a plant: the taxonomy
    mapping in :mod:`auctions.species_categories` is a fish mapping, and this list adds a new
    plant family every few rows.  Exposed so the category pass can be re-run later -- after a club
    finally adds a Plants category -- without re-importing anything.
    """
    return {
        (row.scientific_name.lower(), row.variety.lower()): KIND_CATEGORY_HINTS[row.kind]
        for row in read_rows(path)
        if row.kind in KIND_CATEGORY_HINTS
    }


def _find_parent(row, by_name):
    """The nominal species a variety row belongs to, or None.

    Looks in the curated list first -- that is where a plant's parent lives -- and then anywhere
    else in the table, which is how a fish cultivar finds its FishBase row.  Ordered that way so a
    curated row always wins over a same-named row from somewhere else.
    """
    parent = by_name.get(row.scientific_name.lower())
    if parent:
        return parent
    return Species.objects.filter(scientific_name__iexact=row.scientific_name, variety="").order_by("source").first()


def _apply(species, row, resolver, parent):
    """Copy *row* onto *species*.  Returns True when anything actually changed."""
    # A variety takes its parent's category; otherwise the kind column decides.  The kind is used
    # rather than the family because this list adds a new plant family every few rows, and a
    # family nobody remembered to map would silently leave the category blank.
    category = parent.category if parent else resolver.resolve(KIND_CATEGORY_HINTS.get(row.kind))
    values = {
        "category": category,
        "genus": row.genus[:100],
        "species": row.epithet[:150],
        "variety": row.variety[:100],
        "family": row.family[:100],
        "order": row.order[:100],
        "source": SOURCE,
        # The trade list exists because somebody sells these, so they are all "in the trade" --
        # Species.in_aquarium_trade reads the source for exactly this reason and this column stays
        # empty, since FishBase's rating is not ours to invent.
        "aquarium_use": "",
    }
    for habitat, attribute in _HABITAT_FIELDS.items():
        values[attribute] = habitat in row.habitats
    # The first common name is the one shown in the picker, title-cased the way FishBase writes
    # its FBname ("Cherry shrimp", not "cherry shrimp").
    if row.common_names:
        first = row.common_names[0]
        values["common_name"] = (first[0].upper() + first[1:])[:255]
    changed = False
    for attribute, value in values.items():
        if getattr(species, attribute) != value:
            setattr(species, attribute, value)
            changed = True
    return changed


@transaction.atomic
def load(path=DATA_FILE, *, dry_run=False):
    """Upsert every row in the CSV.  Safe to re-run; returns a :class:`Result`.

    Rows are matched on (scientific name, variety) rather than on a code, because the list has no
    codes -- which also means renaming a species in the CSV adds a row rather than moving one.
    That is the right trade: a rename is rare and deliberate, and the old row can be retired by
    hand, whereas silently repointing lots at a different name is not something a data file should
    be able to do.

    Species come before varieties in one pass over a file that already lists them that way, and
    :func:`_find_parent` falls back to a database lookup, so ordering inside the file only matters
    for readability.
    """
    result = Result()
    resolver = CategoryResolver()
    rows = read_rows(path)
    existing = {
        (species.scientific_name.lower(), species.variety.lower()): species
        for species in Species.objects.filter(source=SOURCE)
    }
    by_name = {
        key[0]: species for key, species in existing.items() if not key[1]
    }  # nominal species only, for parent lookups

    for row in rows:
        key = (row.scientific_name.lower(), row.variety.lower())
        species = existing.get(key)
        parent = None
        if row.is_variety:
            parent = _find_parent(row, by_name)
            if parent is None:
                result.skipped.append(f"{row.scientific_name} '{row.variety}' (parent not in the species list)")
                continue
        if species is None:
            species = Species(source=SOURCE)
            result.created += 1
        else:
            result.updated += 1
        changed = _apply(species, row, resolver, parent)
        if species.parent_id != (parent.pk if parent else None):
            species.parent = parent
            changed = True
        if changed or species.pk is None:
            species.save()
        existing[key] = species
        if not row.is_variety:
            by_name[key[0]] = species

        # Replace rather than merge, so a name dropped from the CSV disappears from the site.
        wanted = {name.lower(): name for name in row.common_names}
        SpeciesCommonName.objects.filter(species=species).exclude(name__in=wanted.values()).delete()
        have = set(SpeciesCommonName.objects.filter(species=species).values_list("name", flat=True))
        new = [
            SpeciesCommonName(
                species=species,
                name=name[:255],
                # bulk_create skips save(); this column is what every lookup matches on.
                name_normalized=normalize_species_name(name),
                language="English",
                is_preferred=(index == 0),
            )
            for index, name in enumerate(wanted.values())
            if name not in have
        ]
        SpeciesCommonName.objects.bulk_create(new)
        result.common_names += len(new)

    if dry_run:
        transaction.set_rollback(True)
    return result
