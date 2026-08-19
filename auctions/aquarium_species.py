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

**Hybrids.**  A row with a ``variety`` and *no* scientific name is a cross -- a tibee, a
flowerhorn.  The blank first column is the whole declaration, and it is honest rather than a
sentinel: a cross between two species has no binomial, which is exactly why the trade's name is
all there is to write down.  It loads as :attr:`Species.is_hybrid`, and the model refuses to keep
a genus, an epithet or a parent on such a row, so nothing that reasons about taxonomy can be
handed one of the two parents by accident.  ``family`` and ``order`` are the exception and may be
filled in when both parents are in the same one -- every tibee is an atyid and every flowerhorn is
a cichlid, and it is what gives the row a category.
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

#: The invertebrate families a club with a Shrimp category means by it.  Plenty of clubs have one
#: -- cherry shrimp are half the invertebrate lots at a freshwater auction -- and "Snails and other
#: inverts" is then the wrong shelf for them.  Read only for ``kind=invert`` rows, and falling back
#: to ``invertebrates`` through :data:`~auctions.species_categories.HINT_FALLBACKS` on a site with
#: no Shrimp category, so this can never *lose* a shrimp a category it used to have.
#:
#: The crayfish (Cambaridae, Parastacidae) are deliberately not here: a category named "Shrimp"
#: means shrimp, and a club that keeps crayfish somewhere specific has said so in its own list.
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
        """A cross: the trade's name, and no binomial to hang it on.  See the module docstring."""
        return bool(self.variety) and not self.scientific_name

    @property
    def is_names_only(self):
        """True when this row is here to add names to a species some other list owns.

        Declared by leaving every taxonomy column blank, which is also the only honest way to
        write such a row: the owning list is the authority on all four and they would be ignored.
        Reading it back is what lets :func:`load` refuse to *invent* a species for a row that
        clearly meant to find one -- a typo in the scientific name would otherwise create a bare
        row with no family, no category and nobody the wiser.
        """
        return bool(self.scientific_name) and not (self.kind or self.family or self.order or self.habitats)


@dataclass
class Result:
    """What :func:`load` did, for the management command to print."""

    created: int = 0
    updated: int = 0
    common_names: int = 0
    #: Rows that only taught names to a species some other list owns -- see :func:`load`.
    adopted: int = 0
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
        variety = (raw.get("variety") or "").strip()
        # A hybrid row is the one kind with no scientific name at all, so the variety has to be
        # able to carry a row on its own; a line with neither is a stray comma.
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

    The ``kind`` column is the only place that knows a *Microsorum* is a plant: the taxonomy
    mapping in :mod:`auctions.species_categories` is a fish mapping, and this list adds a new
    plant family every few rows.  Exposed so the category pass can be re-run later -- after a club
    finally adds a Plants category -- without re-importing anything.

    The one place the family gets a say is :data:`INVERT_FAMILY_HINTS`, because "invertebrate" is
    two shelves at most clubs rather than one.
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
    """A species some *other* list already owns, matching this row exactly.  Or None.

    What makes a names-only row possible.  FishBase has the fish; what it does not have is what
    people call them, and it never will -- it is an ichthyology database, and "yellow lab",
    "pea puffer" and "cw11" are hobby vocabulary.  So the CSV needs to be able to say "this
    FishBase species also answers to these names" without going anywhere near its taxonomy.

    Ordered by source so the answer is stable when two lists somehow hold the same name: the
    alphabetical order happens to put ``admin`` (somebody added it here, on purpose, recently)
    ahead of ``fishbase``, which is the right precedence for a row a person is maintaining.
    """
    # A hybrid has no scientific name to match on, so it is found the same way
    # Species.find_possible_duplicate finds one: the trade's name, and the flag.  Without this a
    # flowerhorn an auction admin had already added by hand would end up on the picker twice.
    if row.is_hybrid:
        others = Species.objects.filter(is_hybrid=True, variety__iexact=row.variety)
    else:
        others = Species.objects.filter(scientific_name__iexact=row.scientific_name, variety__iexact=row.variety)
    return others.exclude(source=SOURCE).order_by("source").first()


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
        # Species.save() clears the genus, the epithet and the parent on a hybrid; this is here so
        # a row that stops being a hybrid in the CSV stops being one in the database too.
        "is_hybrid": row.is_hybrid,
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

    **Names-only rows.**  A row naming a species some other list already owns -- almost always a
    FishBase fish -- does not create a second copy of it and does not touch a single one of its
    taxonomy columns.  All it does is attach the ``common_names``.  That is how the hobby's own
    vocabulary gets into the database: FishBase files *Labidochromis caeruleus* under "Blue streak
    hap", so "yellow lab" found nothing, and the only way this file could previously answer that
    was to add a duplicate *Labidochromis caeruleus* with ``source="aquarium"`` sitting alongside
    the real one.  Leave ``family``, ``order``, ``kind`` and ``habitat`` blank on such a row; they
    would be ignored, because the owning list is the authority on all four.

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
            # It said it was only adding names to something that already exists, and nothing does.
            # Almost always a typo in the scientific name; inventing a species with no family and
            # no category to hang the names off would hide it.
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

        # Replace rather than merge, so a name dropped from the CSV disappears from the site --
        # but only ever *our* names.  On an adopted species the rest of this table is FishBase's
        # 49,000 English names, and deleting those to make room for two hobby ones would throw
        # away most of what makes the matcher work.
        wanted = {name.lower(): name for name in row.common_names}
        ours = SpeciesCommonName.objects.filter(species=species, source=SOURCE)
        ours.exclude(name__in=wanted.values()).delete()
        have = set(ours.values_list("name", flat=True))
        new = [
            SpeciesCommonName(
                species=species,
                name=name[:255],
                # bulk_create skips save(); this column is what every lookup matches on.
                name_normalized=normalize_species_name(name),
                language="English",
                # An adopted species already has a preferred name, from the list that owns it.
                # Claiming to be the preferred one as well would put two rows at the top of every
                # tie-break that reads the flag.
                is_preferred=(index == 0 and not adopted),
                source=SOURCE,
            )
            for index, name in enumerate(wanted.values())
            if name not in have
        ]
        SpeciesCommonName.objects.bulk_create(new)
        result.common_names += len(new)
        named.add(species.pk)

    # Deleting a row from the file has to take its names with it, and the per-species delete above
    # only reaches species the file still mentions.  Without this sweep a names-only row removed
    # from the CSV -- which is how a bad identification gets retracted -- would leave its names
    # attached to somebody else's species with nothing pointing at them.  Scoped to our own names,
    # so it can never touch FishBase's.
    SpeciesCommonName.objects.filter(source=SOURCE).exclude(species_id__in=named).delete()

    if dry_run:
        transaction.set_rollback(True)
    return result
