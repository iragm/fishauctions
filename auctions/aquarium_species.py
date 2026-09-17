"""The curated aquarium-trade species list (plants, invertebrates, live food, cultivars), and the
strains that hang off it -- filling what FishBase's 36,000 fish don't cover. Loaded from
``auctions/data/aquarium_species.csv``; see the file's own header for the rule on adding a row.
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

#: Kept next to the code rather than in a fixture: data we maintain, not load once.
DATA_FILE = Path(__file__).resolve().parent / "data" / "aquarium_species.csv"

#: ``Species.source`` for everything this module writes.
SOURCE = "aquarium"

#: ``kind`` -> category hint. ``fish`` is absent: a fish cultivar takes its category from its
#: FishBase parent's family.
KIND_CATEGORY_HINTS = {
    "plant": "plants",
    "invert": "invertebrates",
    "culture": "live food",
}

#: Invertebrate families a club's "Shrimp" category means (read for ``kind=invert`` rows only).
#: Crayfish are excluded: "Shrimp" means shrimp.
INVERT_FAMILY_HINTS = {
    "Atyidae": "shrimp",
    "Palaemonidae": "shrimp",
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
        return bool(self.variety) and bool(self.scientific_name)

    @property
    def is_hybrid(self):
        """A cross (e.g. a flowerhorn): a trade name with no binomial to hang it on."""
        return bool(self.variety) and not self.scientific_name

    @property
    def is_names_only(self):
        """True when this row only adds names to a species some other list owns.

        Declared by leaving every taxonomy column blank, which lets :func:`load` refuse to invent a species
        for a row that meant to find one.
        """
        return bool(self.scientific_name) and not (self.kind or self.family or self.order or self.habitats)


@dataclass
class Result:
    """What :func:`load` did, for the management command to print."""

    created: int = 0
    updated: int = 0
    common_names: int = 0
    #: Rows that only taught names to a species some other list owns.
    adopted: int = 0
    skipped: list[str] = field(default_factory=list)


def read_rows(path=DATA_FILE):
    """Parse the CSV into :class:`Row` objects. Blank and ``#`` lines are comments."""
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        # Strip comments before csv sees them, so a '#' line can sit anywhere.
        lines = [line for line in handle if line.strip() and not line.lstrip().startswith("#")]
    for raw in csv.DictReader(lines):
        name = (raw.get("scientific_name") or "").strip()
        variety = (raw.get("variety") or "").strip()
        if not name and not variety:
            continue
        rows.append(
            Row(
                scientific_name=name,
                variety=variety,
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

    Exposed so the category pass can re-run without re-importing. :data:`INVERT_FAMILY_HINTS` is the one
    place family gets a say, since "invertebrate" is often two shelves.
    """
    return {
        (row.scientific_name.lower(), row.variety.lower()): (
            INVERT_FAMILY_HINTS.get(row.family) if row.kind == "invert" else None
        )
        or KIND_CATEGORY_HINTS[row.kind]
        for row in read_rows(path)
        if row.kind in KIND_CATEGORY_HINTS
    }


def _find_elsewhere(row):
    """A species some other list already owns, matching this row exactly, or None.

    What makes a names-only row possible: FishBase has the fish but not the hobby's names for it.
    Ordered by source so ``admin`` wins over ``fishbase``.
    """
    if row.is_hybrid:
        others = Species.objects.filter(is_hybrid=True, variety__iexact=row.variety)
    else:
        others = Species.objects.filter(scientific_name__iexact=row.scientific_name, variety__iexact=row.variety)
    return others.exclude(source=SOURCE).order_by("source").first()


def _find_parent(row, by_name):
    """The nominal species a variety row belongs to, or None.

    Checked in the curated list first (where a plant's parent lives), then anywhere else in the table
    (how a fish cultivar finds its FishBase row).
    """
    parent = by_name.get(row.scientific_name.lower())
    if parent:
        return parent
    return Species.objects.filter(scientific_name__iexact=row.scientific_name, variety="").order_by("source").first()


def _apply(species, row, resolver, parent):
    """Copy *row* onto *species*. Returns True when anything actually changed."""
    # A variety takes its parent's category; the kind column decides otherwise, not family, since
    # this list adds plant families often and an unmapped one would blank the category.
    category = parent.category if parent else resolver.resolve(KIND_CATEGORY_HINTS.get(row.kind))
    values = {
        "category": category,
        # Species.save() clears genus, epithet and parent on a hybrid, so a row that stops being one
        # in the CSV stops being one in the database.
        "is_hybrid": row.is_hybrid,
        "genus": row.genus[:100],
        "species": row.epithet[:150],
        "variety": row.variety[:100],
        "family": row.family[:100],
        "order": row.order[:100],
        "source": SOURCE,
        # This list exists because somebody sells these, and Species.in_aquarium_trade reads the
        # source for that, so aquarium_use stays empty.
        "aquarium_use": "",
    }
    for habitat, attribute in _HABITAT_FIELDS.items():
        values[attribute] = habitat in row.habitats
    if row.common_names:
        # Title-cased the way FishBase writes its FBname.
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
    """Upsert every row in the CSV; safe to re-run. Returns a :class:`Result`.

    Matched on (scientific name, variety), not a code, so renaming a species in the CSV adds a row and
    the old one is retired by hand. A names-only row never creates a duplicate and never touches
    taxonomy columns: it only attaches common_names.
    """
    result = Result()
    resolver = CategoryResolver()
    rows = read_rows(path)
    existing = {
        (species.scientific_name.lower(), species.variety.lower()): species
        for species in Species.objects.filter(source=SOURCE)
    }
    by_name = {key[0]: species for key, species in existing.items() if not key[1]}  # nominal species only

    named = set()
    for row in rows:
        key = (row.scientific_name.lower(), row.variety.lower())
        species = existing.get(key)
        # Only a row this list owns is a row this list may rewrite.
        adopted = species is None and _find_elsewhere(row)
        parent = None
        if adopted:
            species = adopted
            result.adopted += 1
        elif species is None and row.is_names_only:
            # Claimed to add names to something that doesn't exist -- almost always a typo, and
            # inventing a bare species here would hide it.
            result.skipped.append(f"{row.scientific_name} (names-only row, but no such species)")
            continue
        else:
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

        # Replace rather than merge, so a name dropped from the CSV disappears -- but only our own
        # names; an adopted species keeps FishBase's other English names.
        wanted = {name.lower(): name for name in row.common_names}
        ours = SpeciesCommonName.objects.filter(species=species, source=SOURCE)
        ours.exclude(name__in=wanted.values()).delete()
        have = set(ours.values_list("name", flat=True))
        new = [
            SpeciesCommonName(
                species=species,
                name=name[:255],
                name_normalized=normalize_species_name(name),  # bulk_create skips save()
                language="English",
                # An adopted species already has a preferred name from the list that owns it.
                is_preferred=(index == 0 and not adopted),
                source=SOURCE,
            )
            for index, name in enumerate(wanted.values())
            if name not in have
        ]
        SpeciesCommonName.objects.bulk_create(new)
        result.common_names += len(new)
        named.add(species.pk)

    # A names-only row removed from the CSV takes its names with it, scoped to our own names.
    SpeciesCommonName.objects.filter(source=SOURCE).exclude(species_id__in=named).delete()

    if dry_run:
        transaction.set_rollback(True)
    return result
