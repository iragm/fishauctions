"""Turn a species' taxonomy into one of the site's :class:`~auctions.models.Category` rows.

The category on a lot used to be a keyword guess: :func:`auctions.models.guess_category` looks at
what categories other people put on lots with *similar names*.  That works surprisingly well and
is still the only thing available for a sponge filter or a mixed bag -- but when the seller has
picked a scientific name there is nothing left to guess, because FishBase already knows the
species is in the Cichlidae.

So this module is the bridge between the two vocabularies: FishBase's families and orders on one
side, and whatever a site's admins have actually named their categories on the other.  The second
half is the awkward one.  Categories are rows in a database that every deployment fills in
differently -- "Cichlids", "African Cichlids", "Rift Lake Cichlids" -- and this file cannot know
which of those exist.  Hence :data:`CATEGORY_CANDIDATES`: each hint lists the names worth trying,
best first, and a hint that matches nothing simply yields no category rather than creating one.
Nothing here ever adds a Category; the category list stays an admin decision.

Read :data:`FAMILY_HINTS` as the exceptions to :data:`ORDER_HINTS`.  A whole order usually maps
cleanly -- every Characiformes is a characin -- and the family map exists for the places it
doesn't: loaches are Cypriniformes but nobody files them with the barbs, and the livebearers are
Cyprinodontiformes but nobody files a guppy with the killifish.
"""

from __future__ import annotations

import logging

from .models import Category, Species

logger = logging.getLogger(__name__)

#: Hint -> the category names to look for, best first.  Matched case-insensitively against
#: ``Category.name``.  Add spellings here rather than renaming a club's categories.
CATEGORY_CANDIDATES = {
    "cichlids": ("Cichlids", "Cichlid", "African Cichlids", "New World Cichlids", "Rift Lake Cichlids"),
    "livebearers": ("Livebearers", "Livebearer", "Live Bearers", "Live-bearers"),
    "catfish": ("Catfish", "Catfish and Loaches", "Plecos and Catfish", "Plecos", "Corydoras"),
    "characins": ("Characins & Tetras", "Characins and Tetras", "Characins", "Tetras", "Tetra"),
    "cyprinids": (
        "Cyprinids & Barbs",
        "Cyprinids and Barbs",
        "Cyprinids",
        "Barbs and Danios",
        "Barbs",
        "Danios",
    ),
    "loaches": ("Loaches", "Loach", "Catfish and Loaches"),
    "killifish": ("Killifish", "Killies", "Killifish and Rivulines"),
    "anabantoids": (
        "Anabantoids",
        "Bettas & Gouramis",
        "Bettas and Gouramis",
        "Anabantids",
        "Labyrinth Fish",
        "Bettas",
        "Gouramis",
    ),
    "rainbowfish": ("Rainbowfish", "Rainbows", "Rainbowfish & Blue Eyes"),
    "goldfish": ("Goldfish & Koi", "Goldfish and Koi", "Goldfish", "Koi", "Pond Fish"),
    "gobies": ("Gobies", "Goby", "Gobies and Sleepers"),
    "marine": ("Marine", "Marine Fish", "Saltwater", "Reef"),
    # The first name in each of the three lists below is the one this site ships with, and the one
    # Lot.bap_placeholder and Lot.unsold_lot_no_bap_reason match on by name: a plant lot has to land
    # in "Aquatic plants" for HAP to be offered, and a shrimp or a daphnia culture has to land in
    # "Snails and other inverts" or "Live food cultures" for the Culture track, the CAP-disabled
    # ineligibility rule and the quantity-minimum exemption to see it.  Keep them first.
    "plants": ("Aquatic plants", "Plants", "Live Plants", "Aquarium Plants", "Plant"),
    "invertebrates": (
        "Snails and other inverts",
        "Invertebrates",
        "Inverts",
        "Shrimp & Snails",
        "Shrimp and Snails",
        "Shrimp",
        "Snails",
        "Invertebrate",
    ),
    "live food": (
        "Live food cultures",
        "Live Food",
        "Live Foods",
        "Live Cultures",
        "Cultures",
        "Foods",
        "Food",
    ),
    "other fish": ("Other Fish", "Miscellaneous Fish", "Misc Fish", "Oddballs"),
}

#: When a hint's own names match nothing, try this one instead.  Each fallback is a *true*
#: statement about the fish -- a goldfish really is a cyprinid -- so landing on one is a coarser
#: answer, never a wrong one.
HINT_FALLBACKS = {
    "goldfish": "cyprinids",
    "loaches": "cyprinids",
    "gobies": "other fish",
    "live food": "invertebrates",
    "rainbowfish": "other fish",
    "marine": "other fish",
}

#: Order -> hint.  The common case: a whole order files under one category.
ORDER_HINTS = {
    "Cichliformes": "cichlids",
    "Siluriformes": "catfish",
    "Characiformes": "characins",
    "Cypriniformes": "cyprinids",
    "Cyprinodontiformes": "killifish",
    "Anabantiformes": "anabantoids",
    "Atheriniformes": "rainbowfish",
    "Gobiiformes": "gobies",
    "Osteoglossiformes": "other fish",
    "Beloniformes": "other fish",
    "Synbranchiformes": "other fish",
    "Tetraodontiformes": "other fish",
}

#: Family -> hint, for the families their order would file in the wrong place.
FAMILY_HINTS = {
    # Livebearers are Cyprinodontiformes, but a guppy is not a killifish.
    "Poeciliidae": "livebearers",
    "Goodeidae": "livebearers",
    "Anablepidae": "livebearers",
    # Loaches are Cypriniformes, but nobody files a kuhli loach with the barbs.
    "Botiidae": "loaches",
    "Cobitidae": "loaches",
    "Nemacheilidae": "loaches",
    "Balitoridae": "loaches",
    "Gastromyzontidae": "loaches",
    "Serpenticobitidae": "loaches",
    "Vaillantellidae": "loaches",
    # Sleeper gobies sit outside Gobiiformes in some treatments.
    "Eleotridae": "gobies",
    "Odontobutidae": "gobies",
    # Rainbowfish relatives, wherever the current classification puts them.
    "Melanotaeniidae": "rainbowfish",
    "Pseudomugilidae": "rainbowfish",
    "Telmatherinidae": "rainbowfish",
    "Bedotiidae": "rainbowfish",
}

#: Genus -> hint, for the handful the family cannot separate.  Goldfish and koi are cyprinids and
#: share Cyprinidae with every barb and danio, but a club that has a Goldfish category means these.
GENUS_HINTS = {
    "Carassius": "goldfish",
    "Cyprinus": "goldfish",
}


def hint_for(species, curated_hints=None):
    """The category hint for a species, or None when its taxonomy says nothing useful.

    The curated list first, when its hints are supplied: everything above is a *fish* mapping, and
    only the list itself knows that a *Microsorum* is a plant and a *Daphnia* is a live food.

    Then genus (the narrowest statement), then family, then order.  Habitat is the last resort: a
    saltwater-only fish belongs in a marine category whatever its order, and for most of the
    27,000 marine species FishBase carries that is the only category anyone would want.
    """
    if curated_hints is not None:
        hint = curated_hints.get((species.scientific_name.lower(), species.variety.lower()))
        if hint:
            return hint
    if species.genus and species.genus in GENUS_HINTS:
        return GENUS_HINTS[species.genus]
    if species.family and species.family in FAMILY_HINTS:
        return FAMILY_HINTS[species.family]
    if species.order and species.order in ORDER_HINTS:
        return ORDER_HINTS[species.order]
    if species.saltwater and not species.freshwater:
        return "marine"
    return None


class CategoryResolver:
    """Hint -> :class:`Category`, resolved once and cached.

    A resolver is built per import run rather than per process: categories are admin-editable, and
    a long-lived cache would mean a newly added "Plants" category needed a restart to be used.
    """

    def __init__(self):
        self._by_name = {category.name.strip().lower(): category for category in Category.objects.all()}
        self._cache = {}

    def resolve(self, hint):
        """The Category for *hint*, or None when this site has nothing that fits."""
        if not hint:
            return None
        if hint in self._cache:
            return self._cache[hint]
        category = None
        for name in CATEGORY_CANDIDATES.get(hint, ()):
            category = self._by_name.get(name.strip().lower())
            if category:
                break
        if category is None:
            fallback = HINT_FALLBACKS.get(hint)
            # Guarded against a cycle in HINT_FALLBACKS: the recursive call is only ever made for
            # a *different* hint, and self._cache is written before returning either way.
            if fallback and fallback != hint:
                category = self.resolve(fallback)
        self._cache[hint] = category
        return category

    @property
    def unmatched_hints(self):
        """Hints that found no category, for the import command to report."""
        return sorted(hint for hint, category in self._cache.items() if category is None)


def assign_categories(queryset=None, *, resolver=None, batch_size=2000):
    """Fill in ``Species.category`` from the taxonomy.  Returns ``(changed, resolver)``.

    Only writes rows whose category is actually wrong, so a re-run over 36,000 species is a read
    and nothing else.  A category somebody set by hand *is* overwritten -- the taxonomy is a
    better answer than a hand edit made when the family column was empty, and the whole point of
    this pass is that it can be re-run after the mapping above is corrected.
    """
    from .aquarium_species import kind_hints  # here, to keep the curated list's import one-way

    resolver = resolver or CategoryResolver()
    curated_hints = kind_hints()
    if queryset is None:
        queryset = Species.objects.all()
    changed = 0
    batch = []
    # Varieties are handled by a second pass: their own family/order are blank because they
    # inherit everything from the parent, including this.
    for species in queryset.filter(parent__isnull=True).iterator(chunk_size=batch_size):
        category = resolver.resolve(hint_for(species, curated_hints))
        if category and species.category_id != category.pk:
            species.category = category
            batch.append(species)
        if len(batch) >= batch_size:
            Species.objects.bulk_update(batch, ["category"])
            changed += len(batch)
            batch = []
    if batch:
        Species.objects.bulk_update(batch, ["category"])
        changed += len(batch)

    varieties = []
    for species in Species.objects.filter(parent__isnull=False).select_related("parent").iterator():
        if species.parent.category_id and species.category_id != species.parent.category_id:
            species.category_id = species.parent.category_id
            varieties.append(species)
    if varieties:
        Species.objects.bulk_update(varieties, ["category"], batch_size=batch_size)
        changed += len(varieties)
    return changed, resolver
