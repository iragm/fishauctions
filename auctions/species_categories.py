"""Turn a species' taxonomy into one of the site's :class:`~auctions.models.Category` rows.

Lots without a species still use :func:`auctions.models.guess_category`. With a species, this maps
FishBase families and orders onto whatever each site has named its categories.
:data:`CATEGORY_CANDIDATES` lists names to try per hint, best first; a hint matching nothing gives
no category. Nothing here creates a Category.

* Hints are fine-grained (``corydoras``, ``plecos``, ``catfish``), and :data:`HINT_FALLBACKS` walks
  to a coarser hint that's still true. The reverse is what filed every catfish as a Corydoras.
* A coarse hint never resolves to a narrow category.

:data:`FAMILY_HINTS` and :data:`GENUS_HINTS` are exceptions to :data:`ORDER_HINTS` (loaches aren't
barbs, livebearers aren't killifish). Cichlids get :data:`CICHLID_REGIONS`, keyed on genus since
FishBase has no distribution; unlisted genera use the plain ``cichlids`` hint.
"""

from __future__ import annotations

import logging
import re

from .models import Category, Species

logger = logging.getLogger(__name__)

#: Hint -> category names to look for, best first. Matched ignoring punctuation and case, then as
#: a word set, so "Cichlids - Rift Lake" and "Rift Lake Cichlids" match. Add spellings here.
CATEGORY_CANDIDATES = {
    # -------------------------------------------------------------------------------- cichlids
    # For clubs that keep cichlids together and genera the region map doesn't cover. No fallback:
    # on a site that splits cichlids, an unplaceable one gets no category.
    "cichlids": ("Cichlids", "Cichlid"),
    "cichlids rift": ("Cichlids - Rift Lake", "Rift Lake Cichlids", "Rift Lake", "African Cichlids", "Africans"),
    "cichlids malawi": ("Cichlids - Lake Malawi", "Lake Malawi Cichlids", "Malawi Cichlids", "Malawi", "Mbuna"),
    "cichlids tanganyika": (
        "Cichlids - Lake Tanganyika",
        "Lake Tanganyika Cichlids",
        "Tanganyikan Cichlids",
        "Tanganyika Cichlids",
        "Tanganyika",
    ),
    "cichlids victoria": (
        "Cichlids - Lake Victoria",
        "Lake Victoria Cichlids",
        "Victorian Cichlids",
        "Victoria Cichlids",
    ),
    # Non-rift Africa, Madagascar and Asia.
    "cichlids old world": (
        "Cichlids - Old World",
        "Old World Cichlids",
        "West African Cichlids",
        "African Riverine Cichlids",
        "African Cichlids",
    ),
    "cichlids central america": (
        "Cichlids - Central American",
        "Central American Cichlids",
        "Central Americans",
        "New World Cichlids",
    ),
    "cichlids south america": (
        "Cichlids - South American",
        "South American Cichlids",
        "South Americans",
        "New World Cichlids",
    ),
    # -------------------------------------------------------------------------------- catfish
    "corydoras": ("Corydoras", "Corys", "Cory Cats", "Corydoras and Relatives"),
    "plecos": ("Plecostomus", "Plecos", "Plecs", "Suckermouth Catfish", "Plecos and Catfish"),
    "catfish": ("Other Catfish", "Catfish", "Misc Catfish", "Catfish and Loaches"),
    # -------------------------------------------------------------------------------- the rest
    "livebearers": ("Livebearers", "Livebearer", "Live Bearers", "Live-bearers"),
    "characins": (
        "Characins - Tetras, Pencilfish, Hatchetfish",
        "Characins & Tetras",
        "Characins and Tetras",
        "Characins",
        "Tetras",
        "Tetra",
    ),
    "cyprinids": (
        "Cyprinids - Barbs, Danios, Rasboras",
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
        "Bettas and labyrinth fish",
        "Bettas & Labyrinth Fish",
        "Anabantoids",
        "Bettas & Gouramis",
        "Bettas and Gouramis",
        "Anabantids",
        "Labyrinth Fish",
        "Bettas",
        "Gouramis",
    ),
    "rainbowfish": ("Rainbowfish", "Rainbows", "Rainbowfish & Blue Eyes", "Rainbowfish and Blue Eyes"),
    "goldfish": ("Goldfish", "Goldfish & Koi", "Goldfish and Koi", "Pond Fish"),
    "koi": ("Koi", "Goldfish & Koi", "Goldfish and Koi", "Pond Fish"),
    "gobies": ("Gobies", "Goby", "Gobies and Sleepers"),
    "marine": ("Saltwater fish", "Marine Fish", "Marine", "Saltwater", "Reef", "Reef Fish"),
    # The first name in each of these four is the one this site ships and the one
    # Lot.bap_placeholder and Lot.unsold_lot_no_bap_reason match by name. Keep them first.
    "plants": ("Aquatic plants", "Plants", "Live Plants", "Aquarium Plants", "Plant"),
    "invertebrates": (
        "Snails and other inverts",
        "Invertebrates",
        "Inverts",
        "Snails",
        "Shrimp & Snails",
        "Shrimp and Snails",
        "Invertebrate",
    ),
    "shrimp": ("Shrimp", "Freshwater Shrimp", "Shrimp & Snails", "Shrimp and Snails"),
    "live food": (
        "Live food cultures",
        "Live Food",
        "Live Foods",
        "Live Cultures",
        "Cultures",
    ),
    "other fish": (
        "Misc and oddball fish",
        "Other Fish",
        "Miscellaneous Fish",
        "Misc Fish",
        "Oddballs",
        "Oddball Fish",
    ),
}

#: When a hint's names match nothing, try this coarser but still true hint. Chains are followed
#: to the end and must stay acyclic.
HINT_FALLBACKS = {
    "cichlids malawi": "cichlids rift",
    "cichlids tanganyika": "cichlids rift",
    "cichlids victoria": "cichlids rift",
    "cichlids rift": "cichlids",
    "cichlids old world": "cichlids",
    "cichlids central america": "cichlids",
    "cichlids south america": "cichlids",
    "corydoras": "catfish",
    "plecos": "catfish",
    "koi": "goldfish",
    "goldfish": "cyprinids",
    "loaches": "cyprinids",
    "gobies": "other fish",
    "shrimp": "invertebrates",
    "live food": "invertebrates",
    "rainbowfish": "other fish",
    "marine": "other fish",
}

#: Order -> hint.
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
    "Gymnotiformes": "other fish",
    "Beloniformes": "other fish",
    "Synbranchiformes": "other fish",
    "Tetraodontiformes": "other fish",
}

#: Family -> hint, where the order would misfile them.
FAMILY_HINTS = {
    # Livebearers are Cyprinodontiformes, but a guppy is not a killifish.
    "Poeciliidae": "livebearers",
    "Goodeidae": "livebearers",
    "Anablepidae": "livebearers",
    "Botiidae": "loaches",
    "Cobitidae": "loaches",
    "Nemacheilidae": "loaches",
    "Balitoridae": "loaches",
    "Gastromyzontidae": "loaches",
    "Serpenticobitidae": "loaches",
    "Vaillantellidae": "loaches",
    # The two catfish families with their own categories; the rest use "catfish".
    "Loricariidae": "plecos",
    "Callichthyidae": "corydoras",
    # Sold as oddballs, not labyrinth fish.
    "Channidae": "other fish",
    "Aenigmachannidae": "other fish",
    "Nandidae": "other fish",
    "Badidae": "other fish",
    "Pristolepididae": "other fish",
    # Sleeper gobies sit outside Gobiiformes in some treatments.
    "Eleotridae": "gobies",
    "Odontobutidae": "gobies",
    "Melanotaeniidae": "rainbowfish",
    "Pseudomugilidae": "rainbowfish",
    "Telmatherinidae": "rainbowfish",
    "Bedotiidae": "rainbowfish",
}

#: Cichlid genera the hobby sells, by region; unlisted genera fall through to ``cichlids``. The
#: lakes are separate from the plain rift tier because clubs split both ways; genera spread across
#: the rift are filed at the coarser tier.
CICHLIDS_MALAWI = (
    # mbuna
    "Abactochromis Chindongo Cyathochromis Cynotilapia Genyochromis Gephyrochromis Iodotropheus "
    "Labeotropheus Labidochromis Maylandia Melanochromis Metriaclima Petrotilapia Pseudotropheus "
    "Tropheops "
    # haplochromines, peacocks and the utaka
    "Alticorpus Aristochromis Aulonocara Buccochromis Caprichromis Champsochromis Cheilochromis "
    "Chilotilapia Copadichromis Corematodus Ctenopharynx Cyrtocara Dimidiochromis Diplotaxodon "
    "Docimodus Eclectochromis Exochochromis Fossorochromis Hemitaeniochromis Hemitilapia "
    "Lethrinops Lichnochromis Mchenga Mylochromis Naevochromis Nimbochromis Nyassachromis "
    "Otopharynx Pallidochromis Placidochromis Protomelas Rhamphochromis Sciaenochromis "
    "Stigmatochromis Taeniochromis Taeniolethrinops Tramitichromis Trematocranus Tyrannochromis"
).split()

CICHLIDS_TANGANYIKA = (
    "Altolamprologus Asprotilapia Aulonocranus Baileychromis Bathybates Benthochromis "
    "Boulengerochromis Callochromis Cardiopharynx Chalinochromis Cunningtonia Cyathopharynx "
    "Cyphotilapia Cyprichromis Ectodus Enantiopus Eretmodus Gnathochromis Grammatotria "
    "Greenwoodochromis Haplotaxodon Hemibates Interochromis Jabarichromis Julidochromis "
    "Lamprologus Lepidiolamprologus Lestradea Limnochromis Limnotilapia Lobochilotes "
    "Lufubuchromis Microdontochromis Neolamprologus Ophthalmotilapia Paracyprichromis Perissodus "
    "Petrochromis Plecodus Pseudosimochromis Reganochromis Simochromis Spathodus Tangachromis "
    "Tanganicodus Telmatochromis Trematocara Trematochromis Triglachromis Tropheus "
    "Variabilichromis Xenochromis Xenotilapia"
).split()

CICHLIDS_VICTORIA = (
    "Allochromis Astatoreochromis Enterochromis Gaurochromis Haplochromis Harpagochromis "
    "Hoplotilapia Lipochromis Lithochromis Macropleurodus Mbipia Neochromis Paralabidochromis "
    "Platytaeniodus Prognathochromis Psammochromis Pundamilia Pyxichromis Ptyochromis "
    "Xystichromis Yssichromis"
).split()

#: Rift-lake genera not specific to one lake.
CICHLIDS_RIFT = "Astatotilapia Ctenochromis".split()

CICHLIDS_OLD_WORLD = (
    # West and central African rivers
    "Anomalochromis Benitochromis Chilochromis Chromidotilapia Congochromis Congolapia Cyclopharynx "
    "Divandu Enigmatochromis Etia Gobiocichla Guentherochromis Hemichromis Heterochromis Konia "
    "Limbochromis Myaka Nanochromis Orthochromis Paragobiocichla Parananochromis Pelmatochromis "
    "Pelvicachromis Pterochromis Pungu Rubricatochromis Schwetzochromis Shuja Steatocranus "
    "Stomatepia Teleogramma Thysochromis Wallaceochromis "
    # Southern and eastern Africa, and tilapias
    "Chetia Coelotilapia Coptodon Danakilia Heterotilapia Iranocichla Oreochromis Pelmatolapia "
    "Pharyngochromis Pseudocrenilabrus Sargochromis Sarotherodon Serranochromis Thoracochromis "
    "Tilapia Tristramella Tylochromis "
    # Madagascar and Asia
    "Etroplus Katria Oxylapia Palaeoplex Paratilapia Paretroplus Ptychochromis Ptychochromoides "
    "Pseudetroplus"
).split()

CICHLIDS_CENTRAL_AMERICA = (
    "Amatitlania Amphilophus Archocentrus Astatheros Chiapaheros Chortiheros Chuco Cincelichthys "
    "Cribroheros Cryptoheros Darienheros Herichthys Herotilapia Hypsophrys Isthmoheros Kihnichthys "
    "Maskaheros Mayaheros Nandopsis Neetroplus Oscura Panamius Parachromis Paraneetroplus Petenia "
    "Rheoheros Rocio Talamancaheros Theraps Thorichthys Tomocichla Trichromis Vieja Wajpamheros"
).split()

CICHLIDS_SOUTH_AMERICA = (
    "Acarichthys Acaronia Aequidens Andinoacara Apistogramma Apistogrammoides Astronotus "
    "Australoheros Biotodoma Biotoecus Bujurquina Caquetaia Chaetobranchopsis Chaetobranchus "
    "Chocoheros Cichla Cichlasoma Cleithracara Crenicara Crenicichla Dicrossus Geophagus "
    "Guianacara Gymnogeophagus Heroina Heros Hoplarchus Hypselecara Ivanacara Krobia Kronoheros "
    "Laetacara Lugubria Mazarunia Mesonauta Mesoheros Mikrogeophagus Nannacara Pterophyllum "
    "Retroculus Rondonacara Satanoperca Symphysodon Taeniacara Tahuantinsuyoa Teleocichla Uaru "
    "Wallaciia"
).split()

CICHLID_REGIONS = {
    **dict.fromkeys(CICHLIDS_MALAWI, "cichlids malawi"),
    **dict.fromkeys(CICHLIDS_TANGANYIKA, "cichlids tanganyika"),
    **dict.fromkeys(CICHLIDS_VICTORIA, "cichlids victoria"),
    **dict.fromkeys(CICHLIDS_RIFT, "cichlids rift"),
    **dict.fromkeys(CICHLIDS_OLD_WORLD, "cichlids old world"),
    **dict.fromkeys(CICHLIDS_CENTRAL_AMERICA, "cichlids central america"),
    **dict.fromkeys(CICHLIDS_SOUTH_AMERICA, "cichlids south america"),
}

#: Genus -> hint where the family can't separate them (goldfish and koi among the cyprinids).
GENUS_HINTS = {
    "Carassius": "goldfish",
    "Cyprinus": "koi",
    **CICHLID_REGIONS,
}

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_category_name(name):
    """``"Cichlids - Rift Lake"`` -> ``"cichlids rift lake"``: how category names are compared."""
    return _NON_WORD.sub(" ", (name or "").lower()).strip()


class CategoryResolver:
    """Hint -> :class:`Category`, resolved once per import run (categories are admin-editable).

    Three passes, first hit wins: the exact normalized name, the same words in any order, then
    :data:`HINT_FALLBACKS`. Never partial word overlap: a missed match leaves the name guesser to fill
    it in, but a wrong one gets printed on a label.
    """

    def __init__(self):
        self._by_name = {}
        self._by_words = {}
        for category in Category.objects.all():
            normalized = normalize_category_name(category.name)
            if not normalized:
                continue
            self._by_name.setdefault(normalized, category)
            # setdefault, so the first of two same-word names wins.
            self._by_words.setdefault(frozenset(normalized.split()), category)
        self._cache = {}

    def _match(self, hint):
        """The category one hint's own names find, ignoring fallbacks, or None."""
        names = [normalize_category_name(name) for name in CATEGORY_CANDIDATES.get(hint, ())]
        for name in names:
            category = self._by_name.get(name)
            if category:
                return category
        for name in names:
            category = self._by_words.get(frozenset(name.split()))
            if category:
                return category
        return None

    def resolve(self, hint):
        """The Category for *hint*, or None when this site has nothing that fits."""
        if not hint:
            return None
        if hint in self._cache:
            return self._cache[hint]
        # Cached before recursing, so a cycle in HINT_FALLBACKS can't loop forever.
        self._cache[hint] = None
        category = self._match(hint)
        if category is None:
            fallback = HINT_FALLBACKS.get(hint)
            if fallback and fallback != hint:
                category = self.resolve(fallback)
        self._cache[hint] = category
        return category

    @property
    def unmatched_hints(self):
        """Hints that found no category, for the import command to report."""
        return sorted(hint for hint, category in self._cache.items() if category is None)

    def report(self):
        """``[(hint, category or None), ...]`` for every hint, for checking the mapping in a terminal."""
        return [(hint, self.resolve(hint)) for hint in sorted(CATEGORY_CANDIDATES)]


def hint_for(species, curated_hints=None):
    """The category hint for a species, or None.

    The curated list first (it knows plants and live foods), then genus, family, order, and finally
    habitat for marine-only fish.
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


def assign_categories(queryset=None, *, resolver=None, batch_size=2000):
    """Fill in ``Species.category`` from taxonomy, returning ``(changed, resolver)``.

    Only writes rows that differ, and overwrites hand-set categories so re-runs apply mapping fixes.
    """
    from .aquarium_species import kind_hints  # here, to keep the curated list's import one-way

    resolver = resolver or CategoryResolver()
    curated_hints = kind_hints()
    if queryset is None:
        queryset = Species.objects.all()
    changed = 0
    batch = []
    # Varieties inherit from their parent in a second pass.
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
