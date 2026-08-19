"""Turn a species' taxonomy into one of the site's :class:`~auctions.models.Category` rows.

The category on a lot used to be a keyword guess: :func:`auctions.models.guess_category` looks at
what categories other people put on lots with *similar names*.  That works surprisingly well and
is still the only thing available for a sponge filter or a mixed bag -- but when the seller has
picked a scientific name there is nothing left to guess, because FishBase already knows the
species is in the Cichlidae.

So this module is the bridge between the two vocabularies: FishBase's families and orders on one
side, and whatever a site's admins have actually named their categories on the other.  The second
half is the awkward one.  Categories are rows in a database that every deployment fills in
differently -- "Cichlids", "African Cichlids", "Cichlids - Rift Lake" -- and this file cannot know
which of those exist.  Hence :data:`CATEGORY_CANDIDATES`: each hint lists the names worth trying,
best first, and a hint that matches nothing simply yields no category rather than creating one.
Nothing here ever adds a Category; the category list stays an admin decision.

Two rules keep that matching honest, both learned from this site's own category list:

* **A hint is as specific as the club's list lets it be.**  A club that files every catfish
  together has one Catfish category; a club that sells fish has "Corydoras", "Plecostomus" and
  "Other Catfish".  So the hints are the *fine* ones -- ``corydoras``, ``plecos``, ``catfish`` --
  and :data:`HINT_FALLBACKS` walks from a fine hint to the coarser one that is still a true
  statement about the fish.  Going the other way is what broke: the old generic ``catfish`` hint
  listed "Corydoras" among its spellings, so on this site every one of the four thousand
  Siluriformes -- every pleco, every synodontis -- was filed as a Corydoras.
* **A coarse hint never resolves to a narrow category.**  Each candidate list holds only names
  meaning the same thing as the hint.  "Other Catfish" is a fine spelling of ``catfish`` because
  it is where a club puts the catfish that are not corys or plecos; "Corydoras" is not.

Read :data:`FAMILY_HINTS` and :data:`GENUS_HINTS` as the exceptions to :data:`ORDER_HINTS`.  A
whole order usually maps cleanly -- every Characiformes is a characin -- and the finer maps exist
for the places it doesn't: loaches are Cypriniformes but nobody files them with the barbs, and the
livebearers are Cyprinodontiformes but nobody files a guppy with the killifish.

The cichlids are the extreme case and get :data:`CICHLID_REGIONS` to themselves.  Every one of the
1,790 of them is in the Cichlidae, so family and order say *nothing* a club can use -- and a club
selling cichlids is exactly the club that splits them four ways by where they come from.  FishBase
tells us the genus and nothing about distribution, so the genus is what the map is keyed on.  It
lists the genera the hobby sells and files the rest under the plain ``cichlids`` hint, which on a
site with only split categories resolves to nothing -- and no category is the right answer for a
fish we cannot place, because the lot then keeps whatever the name guesser said.
"""

from __future__ import annotations

import logging
import re

from .models import Category, Species

logger = logging.getLogger(__name__)

#: Hint -> the category names to look for, best first.  Matched against ``Category.name`` with
#: punctuation and case ignored, and then again on the set of words, so "Cichlids - Rift Lake",
#: "cichlids: rift lake" and "Rift Lake Cichlids" are all the same name.  Add spellings here
#: rather than renaming a club's categories.
CATEGORY_CANDIDATES = {
    # -------------------------------------------------------------------------------- cichlids
    # The generic one, for a club that keeps them together and for every genus the region map
    # below has no opinion about.  Deliberately without a fallback: on a site that splits its
    # cichlids, an unplaceable cichlid gets no category rather than an arbitrary one of the four.
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
    # Everything African that isn't a rift lake fish, plus Madagascar and Asia: the kribs, the
    # jewels, the tilapias, the chromides.
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
    # The first name in each of the four lists below is the one this site ships with, and the one
    # Lot.bap_placeholder and Lot.unsold_lot_no_bap_reason match on by name: a plant lot has to land
    # in "Aquatic plants" for HAP to be offered, and a shrimp or a daphnia culture has to land in
    # "Snails and other inverts" or "Live food cultures" for the Culture track, the CAP-disabled
    # ineligibility rule and the quantity-minimum exemption to see it.  Keep them first.
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

#: When a hint's own names match nothing, try this one instead.  Each fallback is a *true*
#: statement about the fish -- a goldfish really is a cyprinid, a cory really is a catfish -- so
#: landing on one is a coarser answer, never a wrong one.  Chains are walked to the end
#: (``corydoras`` -> ``catfish``), and must stay acyclic.
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
    # The knifefish: a black ghost is an oddball at every auction that has an oddball category.
    "Gymnotiformes": "other fish",
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
    # The two catfish families with their own aisle at every auction.  Everything else in the
    # Siluriformes -- synodontis, pictus, banjos, glass cats -- stays on the order's plain
    # "catfish" hint.
    "Loricariidae": "plecos",
    "Callichthyidae": "corydoras",
    # Anabantiformes by taxonomy, oddballs by the time they reach a table: nobody sells a
    # snakehead or a leaffish as a labyrinth fish.
    "Channidae": "other fish",
    "Aenigmachannidae": "other fish",
    "Nandidae": "other fish",
    "Badidae": "other fish",
    "Pristolepididae": "other fish",
    # Sleeper gobies sit outside Gobiiformes in some treatments.
    "Eleotridae": "gobies",
    "Odontobutidae": "gobies",
    # Rainbowfish relatives, wherever the current classification puts them.
    "Melanotaeniidae": "rainbowfish",
    "Pseudomugilidae": "rainbowfish",
    "Telmatherinidae": "rainbowfish",
    "Bedotiidae": "rainbowfish",
}

#: Cichlid genera the hobby sells, by where they come from.  See the module docstring for why the
#: cichlids need a genus map when nothing else does.  Anything not listed falls through to the
#: plain ``cichlids`` hint.
#:
#: The lakes are separate from the plain rift-lake tier because clubs split them both ways: some
#: have one "Rift Lake" category, some have a Malawi and a Tanganyika one.  A genus whose species
#: are spread across the rift ( *Astatotilapia*, *Ctenochromis* ) is filed at the coarser tier
#: rather than guessed at.
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

#: Rift-lake genera that are not one lake's: *Astatotilapia calliptera* is Malawi's and
#: *A. burtoni* is Tanganyika's, and both are sold.
CICHLIDS_RIFT = "Astatotilapia Ctenochromis".split()

CICHLIDS_OLD_WORLD = (
    # West and central African rivers -- kribs, jewels, dwarf cichlids, the rapids fish
    "Anomalochromis Benitochromis Chilochromis Chromidotilapia Congochromis Congolapia Cyclopharynx "
    "Divandu Enigmatochromis Etia Gobiocichla Guentherochromis Hemichromis Heterochromis Konia "
    "Limbochromis Myaka Nanochromis Orthochromis Paragobiocichla Parananochromis Pelmatochromis "
    "Pelvicachromis Pterochromis Pungu Rubricatochromis Schwetzochromis Shuja Steatocranus "
    "Stomatepia Teleogramma Thysochromis Wallaceochromis "
    # southern and eastern Africa, and the tilapias wherever they are farmed
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

#: Genus -> hint, for the handful the family cannot separate.  Goldfish and koi are cyprinids and
#: share Cyprinidae with every barb and danio, but a club with a Goldfish category means these.
GENUS_HINTS = {
    "Carassius": "goldfish",
    "Cyprinus": "koi",
    **CICHLID_REGIONS,
}

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_category_name(name):
    """``"Cichlids - Rift Lake"`` -> ``"cichlids rift lake"``.  How a category name is compared.

    Punctuation is what actually varies between clubs writing down the same category: "Cichlids -
    Rift Lake", "Cichlids: Rift Lake", "Cichlids (Rift Lake)".  Matching on the letters means one
    spelling in :data:`CATEGORY_CANDIDATES` covers all of them.
    """
    return _NON_WORD.sub(" ", (name or "").lower()).strip()


class CategoryResolver:
    """Hint -> :class:`Category`, resolved once and cached.

    A resolver is built per import run rather than per process: categories are admin-editable, and
    a long-lived cache would mean a newly added "Plants" category needed a restart to be used.

    Matching runs in three passes, each one a weaker claim than the last, and the first hit wins:

    1. the candidate name, exactly as :func:`normalize_category_name` sees it;
    2. the same words in a different order -- "Rift Lake Cichlids" for "Cichlids - Rift Lake";
    3. :data:`HINT_FALLBACKS`, which is a different (coarser) hint rather than a looser match.

    What it deliberately does *not* do is match on a word or two in common.  "Other Catfish" and
    "Corydoras" are both catfish categories and share nothing; "Shrimp" and "Shrimp & Snails" do
    share a word and mean different things on a site that has both.  A missed match costs a
    category on some species, which the lot's own name guesser then fills in; a wrong one is
    printed on a label.
    """

    def __init__(self):
        self._by_name = {}
        self._by_words = {}
        for category in Category.objects.all():
            normalized = normalize_category_name(category.name)
            if not normalized:
                continue
            self._by_name.setdefault(normalized, category)
            # setdefault, so two categories whose names are anagrams of each other leave the
            # first one winning rather than the last one silently replacing it.
            self._by_words.setdefault(frozenset(normalized.split()), category)
        self._cache = {}

    def _match(self, hint):
        """The category one hint's own names find, ignoring fallbacks.  None if none of them do."""
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
        # Written before the recursive call so a cycle in HINT_FALLBACKS -- which would be a bug
        # in the data, not in a club's category list -- cannot recurse forever.
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
        """``[(hint, category or None), ...]`` for every hint, for a person reading a terminal.

        The whole mapping rather than the failures, because the interesting problem on a real site
        is not a hint that matched nothing -- a club with no Plants category does not sell plants
        -- but a hint that matched something *unexpected*.
        """
        return [(hint, self.resolve(hint)) for hint in sorted(CATEGORY_CANDIDATES)]


def hint_for(species, curated_hints=None):
    """The category hint for a species, or None when its taxonomy says nothing useful.

    The curated list first, when its hints are supplied: everything below is a *fish* mapping, and
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
