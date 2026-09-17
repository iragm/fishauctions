"""Turn a typed lot name into a short list of species to pick from, or nothing.

Four steps, most trustworthy first, stopping at the first that answers:

1. **Exact**: the whole name is a scientific or common name.
2. **Cache**: :class:`~auctions.models.SpeciesSearchCache`. Second, not first: it holds shared
   guesses and must not outrank the species list.
3. **Search**: token and phrase matching, ranked.
4. **Language model**, two rounds. :func:`identify` asks cold what the lot names (reads through
   misspellings and abbreviations); :func:`llm_match` picks from a shortlist built from our tables.
   Round two also runs when round one says "not an organism", because that verdict is permanent and
   site-wide. Both rounds are cached, including "a real species we don't stock", which heals when
   the species is imported.

Nothing here returns a species that isn't in the database, and the form validates the pk against
the same table.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import NamedTuple

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from .llm import LLMError, get_provider
from .models import (
    ClubMember,
    LLMUsage,
    Species,
    SpeciesCommonName,
    SpeciesNameRejection,
    SpeciesSearchCache,
    normalize_species_name,
)

logger = logging.getLogger(__name__)

#: Never offer more than this many, plus "No species". Past a handful the honest answer is "we
#: don't know".
MAX_SUGGESTIONS = 5

#: Except for a bare genus or epithet, where the complete set is the answer and a truncated one
#: misleads.
MAX_GENUS_MATCHES = 8

#: How many species a single word may name and still be treated as naming them. Tied to
#: :data:`MAX_SUGGESTIONS`: same question. "guppy" is 3; "tetra" (28) and "catfish" (143) are
#: refused.
MAX_SINGLE_WORD_MATCHES = MAX_SUGGESTIONS

#: How many *other* species' names a word may appear inside and still name a fish rather than a
#: kind of fish. "barb" is one fish's whole name but part of 218 others. Measured: guppy 16, molly
#: 34 vs zebra 60, cory 122, barb 218; forty sits in the gap.
MAX_NAMES_USING_A_WORD = 40

#: Candidates put in front of the model.
LLM_SHORTLIST_SIZE = 40

#: Minimum word length for an epithet match to count. Short epithets are traps: "geo" (the hobby's
#: *Geophagus*) is also *Hoplolatilus geo*, a marine tilefish.
MIN_EPITHET_LETTERS = 6

#: Weights for :func:`search_matches`; only the ordering matters. Results below the best score are
#: dropped.
STRONG_SCORE = 10
WEAK_SCORE = 1

#: Daily model calls per user. Stops a stuck client, not a 50-lot bulk add.
MAX_LLM_CALLS_PER_USER_PER_DAY = 100

_RATE_LIMIT_WINDOW_SECONDS = 60 * 60 * 24

#: Words that never identify a species, on top of ``settings.IGNORE_WORDS``. "box": FishBase calls
#: the spotted boxfish "Box".
_EXTRA_IGNORE_WORDS = {"sp", "spp", "var", "cf", "aff", "unknown", "assorted", "mixed", "misc", "box"}

#: Sources whose common names came in bulk, so a name from them isn't evidence the hobby uses it.
IMPORTED_NAME_SOURCES = ("fishbase", "sealifebase")

#: Round one: the lot name alone, no candidate list. A menu of near misses invites "none of the
#: above"; asked cold, the model reads through typos. The answer is a claim, looked up in
#: :func:`_resolve_identification`.
_IDENTIFY_PROMPT = (
    "An aquarium club member typed this lot name at a fish auction. Say what organism it names.\n"
    'Reply with JSON: {"kind": "species" | "unknown" | "not an organism", "scientific_name": '
    '"Genus species", "common_name": "the name the hobby uses"}.\n'
    "Lot names are typed in a hurry, usually on a phone. Misspellings, missing double letters, "
    "trade abbreviations ('neolamp' is Neolamprologus, 'geo' is Geophagus), quantities, sexes, "
    "sizes and strain names are all normal, and a misspelled name is still that name: 'red "
    "luwigia' is Ludwigia, '8 assasin snails' are assassin snails, 'ammania gracillis' is "
    "Ammannia gracilis. Read through the spelling.\n"
    "Be strict about *which* organism and forgiving about how it is spelled -- those are "
    "different questions, and the second one is not a reason to give up on the first. Naming a "
    "related species you are not sure of is worse than answering unknown.\n"
    '- "species": you know what this is. Put the currently accepted binomial in scientific_name '
    "and the hobby's name for it, spelled correctly, in common_name. When the lot names a strain "
    "or colour form, the binomial is the species it is a strain of and the strain name goes in "
    "common_name.\n"
    '- "not an organism": equipment, food, media, an empty tank, a mixed or assorted bag -- '
    "anything that is not one kind of living thing. 'Sponge filter' is a filter, not a sponge.\n"
    '- "unknown": a living thing you cannot pin down, or a name too vague to identify -- a genus '
    "and a colour, a strain nobody agrees on. Fill in common_name anyway, spelled correctly, "
    "even when it is only a genus: 'red luwigia' is unknown at the species level and its "
    "common_name is still 'red ludwigia'.\n"
    "Where the hobby has a settled answer, that is the answer: a 'green cory' is Corydoras "
    "aeneus, whatever else the genus contains. Never invent a binomial, though -- if you are not "
    "sure of the accepted name, say unknown and let the common name carry it."
)

# Defensive because the failure mode is a confident wrong answer ("sponge filter" -> Ball sponge),
# which gets printed on a label and counted for points. Hence the negative examples.
#: Round two: pick one of ours, when round one couldn't name the lot or named something we don't hold.
_SYSTEM_PROMPT = (
    "You identify the exact species an aquarium club lot is selling. You are given the lot name "
    "and a numbered list of candidate species from a fixed database, which may be empty.\n"
    'Reply with JSON: {"id": <id from the list>} or {"id": null}.\n'
    "Lot names are typed in a hurry at an auction: misspellings, missing double letters, trade "
    "abbreviations, quantities and sexes are all normal, and a misspelled name is still that "
    "name -- '8 assasin snails' is the assassin snail if one is on the list. Be strict about "
    "*which* organism and forgiving about how it is spelled; they are different questions.\n"
    "null is the correct answer far more often than not. Answer null unless a candidate is the "
    "*same organism* the lot name names. In particular answer null when:\n"
    "- the lot is equipment or a mixed/assorted bag. 'Sponge filter' is a filter, not a sponge.\n"
    "- the lot names a real species that is simply not in the list. 'Otocinclus' is Otocinclus; "
    "if no Otocinclus is in the list, answer null rather than offering a different algae eater.\n"
    "- a candidate is merely the same family, genus, or general type of organism.\n"
    "- you are less than confident.\n"
    "Some candidates are cultivars, written Genus species 'Strain' -- 'Neocaridina davidi "
    '"Blue Dream"\'. Pick one only when the lot name names that exact strain; when the lot names '
    "a strain that is not in the list, pick the plain species it is a strain of if that is there, "
    "and otherwise answer null.\n"
    "Only answer with an id when the candidate's scientific name or one of its common names is "
    "what the lot name is calling this organism. Never invent a species or an id.\n"
    "If no candidate is right but you are confident which species the lot name names, you may "
    'instead reply {"scientific_name": "Genus species"} with the currently accepted binomial. It '
    "is looked up in the same database; if it is not there the answer is no species. Use this "
    "only for a species you are sure of, never to guess at a name that might exist."
)

#: "low", not the palette's "minimal", which produced confident nonsense here. The call runs on
#: blur, so the delay goes unnoticed.
REASONING_EFFORT = "low"


def normalize(text):
    """Lowercase, strip punctuation, collapse whitespace. Defined in models.py so stored columns match."""
    return normalize_species_name(text)


def singularize(word):
    """A crude English singular (``guppies`` -> ``guppy``). A wrong singular just fails to match."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


#: A searchable word: three or more characters, a letter first, digits allowed. Digits keep trade
#: codes ("L046", "CW11"); the leading letter keeps counts out.
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


def base_words(text):
    """The words typed in *text*, minus ignored ones, one per word (unlike :func:`keywords`)."""
    ignore = set(settings.IGNORE_WORDS) | _EXTRA_IGNORE_WORDS
    return [word for word in _WORD.findall(normalize(text)) if word not in ignore]


def keywords(text):
    """The words in *text* worth searching on, longest (most discriminating) first."""
    ignore = set(settings.IGNORE_WORDS) | _EXTRA_IGNORE_WORDS
    words = []
    for word in _WORD.findall(normalize(text)):
        for form in (word, singularize(word)):
            if form not in ignore and len(form) >= 3:
                words.append(form)
    return sorted(dict.fromkeys(words), key=len, reverse=True)


#: Longest common name looked for as a phrase. Five, not four: normalising splits on hyphens.
MAX_PHRASE_WORDS = 5


def _phrases(normalized):
    """Every 2-to-:data:`MAX_PHRASE_WORDS`-word run in *normalized*, plus a variant with the last word singular."""
    words = normalized.split()
    phrases = set()
    for size in range(2, MAX_PHRASE_WORDS + 1):
        for start in range(len(words) - size + 1):
            run = words[start : start + size]
            phrases.add(" ".join(run))
            phrases.add(" ".join(run[:-1] + [singularize(run[-1])]))
    return phrases


class LLMBudget:
    """One named daily allowance of model calls.

    Spent in :func:`_ready_to_ask`, only when a call is about to be made, never because a request
    arrived. One unit is one call, so a two-round lookup costs two. The day is in the cache key, so it
    resets at local midnight. A user has one (lot forms) and a club has one (club API), so one busy
    integration can't switch the model off for everybody.
    """

    def __init__(self, name, limit):
        self.name = name
        self.limit = limit
        self.key = f"species_llm_{name}_{timezone.localtime():%Y%m%d}"
        #: Set once a call is refused: "never asked" vs "found nothing". The club API returns a 429.
        self.blocked = False
        #: Calls this object allowed; the club API reports it as ``llm``.
        self.spent = 0

    @classmethod
    def for_user(cls, user, limit=None):
        """The budget the lot forms spend. ``user=None`` is the shared anonymous bucket."""
        return cls(str(user.pk) if user else "anon", MAX_LLM_CALLS_PER_USER_PER_DAY if limit is None else limit)

    @classmethod
    def for_club(cls, club, limit):
        return cls(f"club{club.pk}", limit)

    def spend(self):
        """Consume one unit. True when the call may proceed. ``add`` then ``incr``, so no race."""
        cache.add(self.key, 0, timeout=_RATE_LIMIT_WINDOW_SECONDS)
        try:
            used = cache.incr(self.key)
        except ValueError:
            cache.set(self.key, 1, timeout=_RATE_LIMIT_WINDOW_SECONDS)
            used = 1
        if used > self.limit:
            self.blocked = True
            return False
        self.spent += 1
        return True

    @property
    def used(self):
        return cache.get(self.key) or 0

    @property
    def remaining(self):
        # The counter climbs past the limit; clamp so "remaining" is never negative.
        return max(0, self.limit - self.used)

    @property
    def resets_at(self):
        """Local midnight -- the moment the key's date changes and the allowance starts again."""
        tomorrow = timezone.localtime() + datetime.timedelta(days=1)
        return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


def check_rate_limit(user, limit=None):
    """Consume one unit of *user*'s daily model budget.  True when the call may proceed."""
    return LLMBudget.for_user(user, limit).spend()


#: Words that wrap a name in a quantity. Stripped only from the ends: "blue dream shrimp" is a
#: cultivar made of ignored words.
_QUANTITY_WORDS = {
    "x",
    "of",
    "pair",
    "pairs",
    "trio",
    "trios",
    "group",
    "groups",
    "lot",
    "lots",
    "bag",
    "bags",
    "pack",
    "packs",
    "qty",
    "each",
}


def strip_quantity(normalized):
    """``"6 guppies"`` -> ``"guppies"``. Without it, the commonest lot name matched nothing.

    Only counts and quantity words at the ends. "Assorted" and "mixed" are deliberately not quantity
    words: they say it is not one thing, and stripping them turned "assorted tetras" into a picklist.
    """
    words = normalized.split()
    while words and (words[0].isdigit() or words[0] in _QUANTITY_WORDS):
        words.pop(0)
    while words and (words[-1].isdigit() or words[-1] in _QUANTITY_WORDS):
        words.pop()
    return " ".join(words)


def _visible(user=None, club=None, prefix=""):
    """The ``Q`` deciding which species may be offered.  See :func:`visible_species`."""
    approved = Q(**{f"{prefix}approved": True})
    if user is not None and getattr(user, "is_authenticated", False):
        approved |= Q(**{f"{prefix}added_by": user})
        # A subquery: one round trip however many clubs.
        member_of = ClubMember.objects.filter(user=user, is_deleted=False).values("club_id")
        approved |= Q(**{f"{prefix}club__in": member_of})
    # Guarded: ``club=None`` would mean every unapproved species with no club.
    if club is not None:
        approved |= Q(**{f"{prefix}club": club})
    return approved


def visible_species(user=None, club=None):
    """The species *user* may be offered, as a queryset.

    Imported species are approved and visible to all. An unapproved one (added on the site) is visible
    to its author, to members of ``Species.club``, and to callers working in that club. With neither
    user nor club, only approved species.
    """
    return Species.objects.filter(_visible(user, club))


def visible_common_names(user=None, club=None):
    """Common names this caller may be answered with: both the species and the name must be visible.
    Importer and CSV names are ``approved=True``.
    """
    return SpeciesCommonName.objects.filter(_visible(user, club, prefix="species__")).filter(_visible(user, club))


def split_scientific_name(typed):
    """``"Ancistrus Cirrhosus"`` -> ``("Ancistrus", "cirrhosus")``. One box, split once for form and API."""
    parts = (typed or "").strip().split()
    if not parts:
        return "", ""
    return parts[0].capitalize()[:100], " ".join(parts[1:]).lower()[:150]


def species_already_named(genus, epithet, variety="", user=None, club=None, is_hybrid=False):
    """The visible species this name already belongs to, or None. Scoped so it can't leak another club's
    unapproved row. For a hybrid, ``is_hybrid`` and the strain name are the comparison.
    """
    if is_hybrid:
        return visible_species(user, club).filter(is_hybrid=True, variety__iexact=variety or "").first()
    return (
        visible_species(user, club)
        .filter(genus__iexact=genus, species__iexact=epithet, variety__iexact=variety or "", is_hybrid=False)
        .first()
    )


def species_carrying_common_name(name, user=None, club=None, exclude=None):
    """The species this common name already names (designated or synonym), ignoring *exclude*, or None.
    Used to refuse making a name ambiguous: a second "guppy" would turn an exact match into a picklist.
    """
    normalized = normalize(name)
    if not normalized:
        return None
    designated = visible_species(user, club).filter(common_name_normalized=normalized)
    carried = visible_common_names(user, club).filter(name_normalized=normalized)
    if exclude is not None:
        designated = designated.exclude(pk=exclude.pk)
        carried = carried.exclude(species=exclude)
    found = designated.first()
    if found:
        return found
    row = carried.select_related("species").first()
    return row.species if row else None


def exact_matches(text, user=None, club=None):
    """Species whose scientific name or a common name *is* the typed text, ranked: scientific name, then
    the designated common name, then synonyms. Several poeciliids answer to "guppy"; one is the guppy.
    """
    normalized = normalize(text)
    if not normalized:
        return []
    # Singular and quantity-stripped forms too, so "6 guppies" and "guppy" agree.
    candidates = set()
    for form in (normalized, strip_quantity(normalized)):
        if not form:
            continue
        words = form.split()
        candidates.add(form)
        candidates.add(" ".join(words[:-1] + [singularize(words[-1])]))
    # Separate indexed lookups, in rank order: the join with a CASE was the slowest part of a lookup.
    found = {}
    # Nominal species only, or "Neocaridina davidi" also returns its thirteen strains.
    for species in visible_species(user, club).filter(scientific_name__in=candidates, variety="")[:MAX_SUGGESTIONS]:
        found[species.pk] = species
    # The designated name (FBname) before synonyms; PreferredName is set on ~3% of rows. Matches the
    # normalised column, which is the only way to reach names with punctuation ("Ram's horn snail").
    for species in visible_species(user, club).filter(common_name_normalized__in=candidates)[:MAX_SUGGESTIONS]:
        found.setdefault(species.pk, species)
    # Ordered before slicing (habitat, then trade rank), or the one freshwater "Angelfish" can be cut.
    common_names = (
        visible_common_names(user, club)
        .filter(name_normalized__in=candidates)
        .select_related("species")
        .order_by("-is_preferred", "-species__freshwater", "species__trade_rank")[: MAX_SUGGESTIONS * 3]
    )
    carried = []
    for common in common_names:
        if common.species_id not in found:
            carried.append(common.species)
    # A synonym on several species: prefer the one whose designated name agrees.
    if not found:
        carried = _named_after_the_same_thing(normalized, carried)
    for species in carried:
        found.setdefault(species.pk, species)
    return list(found.values())[:MAX_SUGGESTIONS]


def _named_after_the_same_thing(normalized, candidates):
    """Narrow a shared synonym to the species really called that, by its designated name.

    "Peppered cory" names *C. paleatus* and *C. julii* ("Leopard corydoras"); a two-fish picklist left
    the commonest cory unreachable. Only narrows to one, and only when nothing stronger matched.
    """
    if len(candidates) < 2:
        return candidates
    typed = set(keywords(normalized))
    agreeing = [species for species in candidates if typed & set(keywords(species.common_name_normalized))]
    return agreeing if len(agreeing) == 1 else candidates


def _trade_first(queryset, prefix=""):
    """Order by :attr:`Species.trade_rank`. Applied before every ``LIMIT``, so the traded species survive it."""
    return queryset.order_by(f"{prefix}trade_rank")


def _rank(species_list, category=None):
    """Move the likeliest candidates to the front. A stable sort: only ties are broken.

    1. The category the lot looks like (re-orders only; it is itself a guess).
    2. Freshwater, because trade rank can't tell a marine angelfish from *Pterophyllum scalare*.
    3. :attr:`Species.trade_rank`.
    """
    category_pk = getattr(category, "pk", category)
    return sorted(
        species_list,
        key=lambda species: (
            not (category_pk and species.category_id == category_pk),
            not species.freshwater,
            species.trade_rank,
        ),
    )


def _alphabetical(species_list):
    """A deterministic order for candidates that scored identically and so have none of their own."""
    return sorted(species_list, key=lambda species: (species.scientific_name, species.variety))


def _single_word_matches(words, user=None, club=None):
    """Species named by one word of the lot name ("male guppy", "L046 pleco"), when it is safe to act on.

    A word answers only when all three hold:

    1. It names at most :data:`MAX_SINGLE_WORD_MATCHES` species.
    2. Its fish is in the trade, unless the name is ours rather than FishBase's. Without this "bronze
       cory" answers the copper shark.
    3. It names a fish, not a kind of fish (:data:`MAX_NAMES_USING_A_WORD`).

    Several qualifying words: fewest species, then longest word, wins.
    """
    best = None
    for word in words:
        # DISTINCT ... LIMIT n+1 gives the count when small without counting 143 rows. order_by()
        # first, or Meta.ordering puts `name` in the DISTINCT and counts names, not species.
        species_ids = list(
            visible_common_names(user, club)
            .filter(name_normalized=word)
            .filter(Q(species__trade_rank=Species.TRADE_RANK_SPECIES) | ~Q(source__in=IMPORTED_NAME_SOURCES))
            .order_by()
            .values_list("species_id", flat=True)
            .distinct()[: MAX_SINGLE_WORD_MATCHES + 1]
        )
        if not species_ids or len(species_ids) > MAX_SINGLE_WORD_MATCHES:
            continue
        if best is not None and (len(species_ids), -len(word)) >= (len(best[1]), -len(best[0])):
            continue
        # Three LIKEs with a leading wildcard; only for a word that got this far.
        component = Q(name_normalized__startswith=f"{word} ") | Q(name_normalized__endswith=f" {word}")
        component |= Q(name_normalized__contains=f" {word} ")
        used_inside = (
            SpeciesCommonName.objects.filter(component)
            .order_by()
            .values_list("species_id", flat=True)
            .distinct()[: MAX_NAMES_USING_A_WORD + 1]
        )
        if len(list(used_inside)) > MAX_NAMES_USING_A_WORD:
            continue
        best = (word, species_ids)
    if best is None:
        return []
    return list(visible_species(user, club).filter(pk__in=best[1]))


def search_matches(text, limit=MAX_SUGGESTIONS, category=None, user=None, club=None):
    """Species the typed text genuinely names, ranked. Empty when nothing does.

    Strict, because a wrong species gets printed and scored while no answer falls through to the model:

    *Scientific token*: a word is a genus or epithet ("Tropheus duboisi maswa").
    *Common-name phrase*: a whole multi-word common name appears in the lot name.
    *Single common name*: :func:`_single_word_matches`, only when nothing above matched.
    *Bare epithet*: the whole lot name is one epithet ("saulosi").

    No loose substring matching: "sponge filter" would hit *Sponge frillgoby*. *category* only breaks ties.
    """
    words = set(keywords(text))
    if not words:
        return []
    normalized = normalize(text)
    scored = {}

    # Rule 1: genus in SQL (indexed), epithet checked in Python. Nominal species only, or strains
    # score identically with their parent.
    genus_candidates = {word.capitalize() for word in words}
    # Trade-ordered before the slice, so the in-trade fallback below has rows to work with.
    genus_hits = _trade_first(visible_species(user, club).filter(genus__in=genus_candidates, parent__isnull=True))
    for species in genus_hits[: LLM_SHORTLIST_SIZE * 2]:
        has_genus = species.genus.lower() in words
        has_epithet = bool(species.species) and species.species.lower() in words
        if has_genus and has_epithet:
            scored[species.pk] = (STRONG_SCORE, species)
        elif has_genus:
            scored[species.pk] = (WEAK_SCORE, species)

    # Rule 2: every 2-to-5-word run looked up by equality; icontains returns thousands for "tetra".
    # Single-word names are left to later rules ("Bolivian ram" is not "Ram").
    phrases = _phrases(normalized)
    if phrases:
        for common in visible_common_names(user, club).filter(name_normalized__in=phrases).select_related("species"):
            score = STRONG_SCORE + len(common.name.split()) + (1 if common.is_preferred else 0)
            previous = scored.get(common.species_id)
            if previous is None or previous[0] < score:
                scored[common.species_id] = (score, common.species)

    if scored:
        best = max(score for score, _ in scored.values())
        ranked = _rank(_alphabetical([species for score, species in scored.values() if score == best]), category)
        if best > WEAK_SCORE:
            return ranked[:limit]
        # Only a genus matched: the complete genus is an answer, a slice of a big one isn't.
        if len(ranked) <= MAX_GENUS_MATCHES:
            return ranked
        # Unless few of them are in the trade (77 Ancistrus, 2 kept). Species-level rank: every
        # member shares the genus rank.
        in_trade = [species for species in ranked if species.trade_rank == Species.TRADE_RANK_SPECIES]
        if 0 < len(in_trade) <= MAX_GENUS_MATCHES:
            return in_trade
        # Too broad to answer, so fall through: "male bettas" still reaches the common name "betta".

    # Rule 3: an unambiguous single-word common name, after the rules above so it can't dilute them.
    single = _single_word_matches(words, user=user, club=club)
    if single:
        return _rank(_alphabetical(single), category)

    # Rule 4: a one-word lot name that is an epithet. Counted on base_words, since keywords() adds
    # singulars.
    typed = base_words(text)
    if len(typed) == 1:
        forms = {typed[0], singularize(typed[0])}
        epithet_hits = list(
            visible_species(user, club).filter(species__in=forms, parent__isnull=True)[: MAX_GENUS_MATCHES + 1]
        )
        if 0 < len(epithet_hits) <= MAX_GENUS_MATCHES:
            return _rank(_alphabetical(epithet_hits), category)
    return []


def _shortlist(words, normalized, user=None, club=None, reading=""):
    """Species worth putting in front of the model, best evidence first, until the list is full.

    Recall matters more than precision here, but the list must be ordered: an unordered ``icontains``
    slice for "german blue ram" missed the fish called "Ram".

    1. a common name equal to a keyword or phrase
    2. a word that is an epithet ("apisto cacatuoides": the epithet names one fish)
    3. a genus keyword, and genus siblings of what was found
    4. common names containing a keyword

    Then the nominal species of every cultivar on the list. *reading* is round one's corrected
    spelling; it only adds phrases, since a correction is itself a guess.
    """
    candidates = {}

    def add(queryset, limit=LLM_SHORTLIST_SIZE):
        """Take rows until the shortlist is full; earlier layers keep their places."""
        remaining = limit - len(candidates)
        if remaining <= 0:
            return
        for row in queryset[:remaining]:
            species = row if isinstance(row, Species) else row.species
            candidates.setdefault(species.pk, species)

    phrases = _phrases(normalized) | set(words)
    if reading:
        phrases |= _phrases(reading)
    add(
        _trade_first(visible_common_names(user, club).filter(name_normalized__in=phrases), "species__").select_related(
            "species"
        )
    )

    # Nominal species only, and long words only (MIN_EPITHET_LETTERS): a short epithet match is
    # coincidence.
    epithets = {word for word in words if len(word) >= MIN_EPITHET_LETTERS}
    if epithets:
        add(_trade_first(visible_species(user, club).filter(species__in=epithets, parent__isnull=True)))

    genera = {word.capitalize() for word in words} | {species.genus for species in candidates.values()}
    add(_trade_first(visible_species(user, club).filter(genus__in=genera)))

    name_q = Q()
    for word in words:
        name_q |= Q(name_normalized__icontains=word)
    add(_trade_first(visible_common_names(user, club).filter(name_q), "species__").select_related("species"))

    # Parents of listed cultivars, past the cap, so "pick the plain species" is possible. Without it
    # the model chose between three wrong strains and null.
    parents = {species.parent_id for species in candidates.values() if species.parent_id} - set(candidates)
    if parents:
        add(visible_species(user, club).filter(pk__in=parents), limit=LLM_SHORTLIST_SIZE + len(parents))
    return list(candidates.values())


def _record_usage(user, result, query, kind, *, success=True):
    """Write one :class:`LLMUsage` row.  Never allowed to break the caller."""
    try:
        LLMUsage.objects.create(
            user=user,
            model=(result.model if result else "")[:100],
            prompt_tokens=result.prompt_tokens if result else 0,
            cached_prompt_tokens=result.cached_prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            total_tokens=result.total_tokens if result else 0,
            query=(query or "")[:600],
            response_kind=kind[:30],
            success=success,
        )
    except Exception:
        logger.exception("Could not record species-matching LLM usage")


class Identification(NamedTuple):
    """What the model made of one lot name; the return of both rounds.

    *species* is the only field that reaches a lot, always a row of ours. *answered* is False when the
    model never ran; only answered results are cached. *scientific_name* is what it says the lot is,
    stocked or not (see ``SpeciesSearchCache.is_a_gap``). *corrected_name* is the spelling-fixed hobby
    name, useful even without a species ("red ludwigia"), and what round two builds its shortlist from.
    """

    species: Species | None = None
    answered: bool = False
    scientific_name: str = ""
    corrected_name: str = ""

    @property
    def settled(self):
        """True only once we have a species.

        "Not an organism" doesn't settle it: a remembered negative is served site-wide and nothing walks it
        back, so :func:`suggest_species` requires the shortlist round to agree.
        """
        return self.species is not None


#: A name has two or three words; "some kind of small brown fish" is not a name.
def _looks_like_a_binomial(name):
    """True when *name* is shaped like a scientific name rather than like a description."""
    words = (name or "").split()
    return 2 <= len(words) <= 3 and all(re.fullmatch(r"[A-Za-z.'-]+", word) for word in words)


def _resolve_identification(scientific_name, common_name, user=None, club=None):
    """The species behind the model's identification, if we hold it.

    The binomial is looked up exactly. The common name refines it to a strain of that same species
    (cultivars share the parent's binomial), or stands alone only when unambiguous.
    """
    named = _species_named(scientific_name, user=user, club=club)
    if common_name:
        hits = exact_matches(common_name, user=user, club=club)
        if named is not None:
            strain = next((species for species in hits if species.parent_id == named.pk), None)
            if strain is not None:
                return strain
        elif len(hits) == 1:
            return hits[0]
    return named


def _species_named(scientific_name, user=None, club=None):
    """The nominal species with exactly this binomial, or None. Lets the model recover from a shortlist
    miss without ever returning a species we don't have.
    """
    name = (scientific_name or "").strip()
    if not name or len(name.split()) > 3:
        return None
    return visible_species(user, club).filter(scientific_name__iexact=name, variety="").first()


def _ready_to_ask(text, user, budget):
    """Provider, words, budget: checked before either round. Returns ``(provider, budget)`` or
    ``(None, budget)``. Budget is spent here, at the moment of the call.
    """
    provider = get_provider()
    if not provider.is_configured():
        return None, budget
    # Not the site-wide effort; left alone if the deployment turned the parameter off.
    if provider.reasoning_effort:
        provider.reasoning_effort = REASONING_EFFORT
    if not keywords(text):
        return None, budget
    budget = budget or LLMBudget.for_user(user)
    if not budget.spend():
        logger.info("Species lookup rate limit reached for %s", budget.name)
        return None, budget
    return provider, budget


def identify(text, user=None, club=None, budget=None):
    """Round one: ask what the lot name names, without showing our list.

    A few dozen tokens. Reads through misspellings the shortlist can't ("red luwigia"), gives an answer
    that survives the list changing (a gap, not "not a species"), and avoids the menu effect. Every name
    is still resolved against our table.
    """
    provider, budget = _ready_to_ask(text, user, budget)
    if provider is None:
        return Identification()
    try:
        result = provider.complete_json(_IDENTIFY_PROMPT, [{"role": "user", "content": f"Lot name: {text}"}])
    except LLMError:
        logger.info("Species identification failed for %r", text, exc_info=True)
        _record_usage(user, None, text, "error", success=False)
        return Identification()
    kind = str(result.data.get("kind") or "").strip().lower()
    if kind == "not an organism":
        # Answered but not settled: this verdict becomes permanent, so round two must agree.
        _record_usage(user, result, text, "not_an_organism")
        return Identification(None, True, "")
    scientific_name = str(result.data.get("scientific_name") or "").strip()
    common_name = str(result.data.get("common_name") or "").strip()
    if kind != "species" or not _looks_like_a_binomial(scientific_name):
        # Not an answer about the name, so nothing is cached, but a corrected spelling still helps.
        _record_usage(user, result, text, "unknown")
        return Identification(corrected_name=common_name)
    species = _resolve_identification(scientific_name, common_name, user, club)
    if species is not None and is_rejected(normalize(text), species):
        # A retired pairing: dropped, not remembered (see record_choice).
        return _retired_answer(user, result, text)
    _record_usage(user, result, text, "species" if species else "gap")
    # The binomial is kept only when nothing resolved; it is the gap column.
    return Identification(species, True, "" if species else scientific_name, common_name)


def llm_match(text, user=None, club=None, budget=None, reading=""):
    """Round two: ask the model to pick one species from a shortlist we built.

    Reached when round one couldn't name the lot or named something we don't hold under that name
    (a synonym, a strain, a club's word). An id not on the shortlist is discarded; a named species is
    resolved via :func:`_species_named`. An empty shortlist is still asked ("Yellow lab"), once per name.

    *reading* is round one's corrected spelling; the shortlist uses both, while the cache row, veto and
    recorded query stay keyed on what was typed. *budget* defaults to *user*'s (see :class:`LLMBudget`).
    """
    # Preflight before the shortlist's four queries.
    provider, budget = _ready_to_ask(text, user, budget)
    if provider is None:
        return Identification()
    normalized = normalize(text)
    # Re-sorted longest first, since the discriminating word may be the corrected one.
    reading = normalize(reading)
    words = keywords(text)
    if reading and reading != normalized:
        words = sorted(dict.fromkeys(words + keywords(reading)), key=len, reverse=True)
    else:
        reading = ""
    candidates = _shortlist(words, normalized, user=user, club=club, reading=reading)
    # Never offer a retired pairing again, or the model would re-teach it.
    vetoed = rejected_species_ids(normalized)
    if vetoed:
        candidates = [species for species in candidates if species.pk not in vetoed]
    listing = "\n".join(f"{species.pk}: {species.label_with_common_name}" for species in candidates)
    # Shown as well as searched, so the model sees why candidates are there.
    asked = f"Lot name: {text}" + (f"\nRead as: {reading}" if reading else "")
    # "(none)" explicitly, so an empty list doesn't read as a truncated prompt.
    messages = [{"role": "user", "content": f"{asked}\n\nCandidates:\n{listing or '(none)'}"}]
    try:
        result = provider.complete_json(_SYSTEM_PROMPT, messages, max_tokens=1000)
    except LLMError:
        logger.info("Species lookup failed for %r", text, exc_info=True)
        _record_usage(user, None, text, "error", success=False)
        return Identification()
    raw = result.data.get("id")
    try:
        chosen_pk = int(raw)
    except (TypeError, ValueError):
        spoken = str(result.data.get("scientific_name") or "").strip()
        named = _species_named(spoken, user=user, club=club)
        if named and named.pk in vetoed:
            return _retired_answer(user, result, text)
        # A binomial we don't stock is kept as a gap, never "not a species". A shrug isn't a binomial.
        gap = spoken if not named and _looks_like_a_binomial(spoken) else ""
        _record_usage(user, result, text, "species" if named else ("gap" if gap else "no_species"))
        return Identification(named, True, gap)
    if chosen_pk in vetoed:
        # It named a retired pairing from memory rather than from the list it was given.
        return _retired_answer(user, result, text)
    # Never trust the id: it has to be one we offered.
    chosen = next((species for species in candidates if species.pk == chosen_pk), None)
    _record_usage(user, result, text, "species" if chosen else "no_species")
    return Identification(chosen, True, "")


def remember(text, species, source="llm", user=None, scientific_name=""):
    """Write an answer to the cache, including "this is not a species".

    *scientific_name* with no species makes the row a **gap** that answers once the species is
    imported (``SpeciesSearchCache.is_a_gap``). *user* is recorded because every row is served site-wide
    and a wrong one must be traceable.
    """
    normalized = normalize(text)
    if not normalized:
        return
    # Global table: an unapproved species can't teach the site a name.
    if species is not None and not species.approved:
        return
    # A retired pairing is not re-learned, or accept/reject would loop. The gaps page can undo it.
    if species is not None and is_rejected(normalized, species):
        return
    defaults = {"species": species, "source": source, "scientific_name": (scientific_name or "")[:120]}
    if user is not None and getattr(user, "is_authenticated", False):
        defaults["created_by"] = user
    existing = SpeciesSearchCache.objects.filter(search_text=normalized).first()
    if existing is not None and existing.species_id != (species.pk if species is not None else None):
        # Counters score an answer, not a name; a different answer starts at zero.
        defaults["accepts"] = 0
        defaults["rejects"] = 0
    SpeciesSearchCache.objects.update_or_create(search_text=normalized, defaults=defaults)


def _retired_answer(user, result, text):
    """The model named a species this name was retired from: discard, remember nothing (``answered=False``).
    Not "not a species": only this one species was rejected.
    """
    _record_usage(user, result, text, "no_species")
    return Identification()


def is_rejected(normalized, species):
    """True when this (already normalised) name was retired from naming this species."""
    if species is None or not normalized:
        return False
    return SpeciesNameRejection.objects.filter(search_text=normalized, species=species).exists()


def rejected_species_ids(normalized):
    """The species this name has been retired from naming.  A set, usually empty."""
    if not normalized:
        return set()
    return set(SpeciesNameRejection.objects.filter(search_text=normalized).values_list("species_id", flat=True))


def record_choice(text, species, *, first_save=False, changed=False, user=None):
    """Score what a person did with this name's remembered answer. One indexed lookup; usually a no-op.

    The cache is written by sellers and served site-wide, so the same forms report back. Both counters
    count **lots**, not saves: an accept only on the lot's first save; a reject on the first save or a
    save that changed the species.

    A remembered "not a species" is scored too: a lot saved with no species is agreement, and nobody
    is ever shown a negative, so a deliberate species pick is the disagreement.
    ``MIN_REJECTS_TO_RETIRE`` picks on different lots replace it with what they chose.
    """
    normalized = normalize(text)
    if not normalized:
        return
    row = SpeciesSearchCache.objects.filter(search_text=normalized).first()
    if row is None:
        # Nothing was remembered, so there is nothing to score.
        return
    chosen_pk = getattr(species, "pk", species)
    if row.species_id is None:
        _reject_a_negative(row, text, species, chosen_pk, first_save=first_save, changed=changed, user=user)
        return
    if chosen_pk and str(chosen_pk) == str(row.species_id):
        if first_save:
            # F(): two concurrent saves count twice.
            SpeciesSearchCache.objects.filter(pk=row.pk).update(accepts=F("accepts") + 1)
        return
    if not (first_save or changed):
        # Already mismatched before this save (a price edit); already counted.
        return
    SpeciesSearchCache.objects.filter(pk=row.pk).update(rejects=F("rejects") + 1)
    # Re-read: a concurrent save may have retired the row, and a lot save must not fail over it.
    row = SpeciesSearchCache.objects.filter(pk=row.pk).first()
    if row and row.is_discredited:
        logger.info(
            "Retiring remembered species %r -> %s after %s reject(s) and %s accept(s)",
            row.search_text,
            row.species,
            row.rejects,
            row.accepts,
        )
        row.retire()


def _reject_a_negative(row, text, species, chosen_pk, *, first_save, changed, user):
    """Score a species put on a lot the cache calls "not a species". See :func:`record_choice`. A lot
    saved with no species counts for nothing: most just weren't filled in.
    """
    if not (chosen_pk and (first_save or changed)):
        return
    SpeciesSearchCache.objects.filter(pk=row.pk).update(rejects=F("rejects") + 1)
    # Re-read, as in record_choice.
    row = SpeciesSearchCache.objects.filter(pk=row.pk).first()
    if row and row.is_discredited:
        logger.info(
            "Replacing remembered %r -> no species with %s after %s pick(s)",
            row.search_text,
            species,
            row.rejects,
        )
        # Through remember() for its guards (approval, retirement), and it resets the counters.
        remember(text, species, source="user", user=user)


def _is_somebody_elses_name(normalized, species, user=None, club=None):
    """True when a cached answer is really one club's scoped common name for that fish.

    The cache is read before the token search and served to every club, so without this a club-scoped
    name leaks site-wide. Nothing in ``SpeciesCommonName`` claiming the text ends it in one lookup: an
    inference is nobody's property.
    """
    if species is None:
        return False
    claimed = SpeciesCommonName.objects.filter(name_normalized=normalized, species=species)
    if not claimed.exists():
        return False
    return not visible_common_names(user, club).filter(name_normalized=normalized, species=species).exists()


def suggest_species(text, user=None, use_llm=True, category=None, club=None, budget=None):
    """The one call views make: ``(species_list, source)`` for a typed lot name.

    *source* (``cache``/``exact``/``search``/``llm``/``none``) is for debugging. An empty list is a
    real answer. *budget* is whose model allowance is spent (:class:`LLMBudget`). *category* only
    re-orders (:func:`_rank`). *club* only widens visibility (:func:`visible_species`).
    """
    normalized = normalize(text)
    if not normalized:
        return [], "none"

    # Exact before cache: the list must always outrank a shared guess.
    exact = _rank(exact_matches(text, user=user, club=club), category)
    if exact:
        return exact, "exact"

    cached = SpeciesSearchCache.objects.filter(search_text=normalized).select_related("species").first()
    if cached:
        # Racy on purpose; it only shows which names carry the cache.
        SpeciesSearchCache.objects.filter(pk=cached.pk).update(hits=cached.hits + 1)
        remembered = cached.species
        # A gap row healing itself: the fish may have been imported since. One lookup, no model call.
        healed = False
        if remembered is None and cached.scientific_name:
            remembered = _species_named(cached.scientific_name, user=user, club=club)
            if remembered is not None and is_rejected(normalized, remembered):
                remembered = None
            healed = remembered is not None
        # A cached species must still be visible (it may have been un-approved). Falls through
        # rather than answering "no species", since the cache mustn't outrank the list.
        seen = remembered is None or remembered.approved
        if not seen:
            seen = visible_species(user, club).filter(pk=remembered.pk).exists()
        # ...and so must the name (see _is_somebody_elses_name).
        if seen and _is_somebody_elses_name(normalized, remembered, user=user, club=club):
            seen = False
        if seen:
            # Written back only when approved; this row is served site-wide.
            if healed and remembered.approved:
                SpeciesSearchCache.objects.filter(pk=cached.pk).update(species=remembered)
            return ([remembered] if remembered else []), "cache"

    found = search_matches(text, category=category, user=user, club=club)
    if found:
        return found, "search"

    if use_llm:
        # Round one; see identify().
        answer = identify(text, user=user, club=club, budget=budget)
        if not answer.settled and answer.corrected_name and normalize(answer.corrected_name) != normalized:
            # A corrected spelling with no species ("red ludwigia"): run it through exact and search.
            # Not remembered: often several species, and the cache's name guard checks the typed
            # misspelling, so a club-scoped match could leak. The lot form writes its own row on save.
            corrected = _rank(exact_matches(answer.corrected_name, user=user, club=club), category) or search_matches(
                answer.corrected_name, category=category, user=user, club=club
            )
            if corrected:
                return corrected, "llm"
        # Whether round two ran and answered: required before a bare negative is written.
        confirmed = False
        if not answer.settled:
            # Round two: our list, where synonyms and strain names live, and the second opinion on
            # "not an organism". Round one's binomial is kept whatever it decides.
            second = llm_match(text, user=user, club=club, budget=budget, reading=answer.corrected_name)
            confirmed = second.answered
            answer = Identification(
                second.species,
                second.answered or answer.answered,
                # ...unless round two found a species.
                second.scientific_name or ("" if second.species else answer.scientific_name),
            )
        if answer.species is not None and _is_somebody_elses_name(normalized, answer.species, user=user, club=club):
            # The name is another club's scoped word; the model recognised it anyway. Answer
            # "no species" and remember nothing: one shared row can't be right for both clubs.
            answer = Identification()
        # Remember only when the model answered. A species or gap row needs one round (both are
        # recoverable); a bare negative needs both, because nothing walks it back.
        if answer.answered and (answer.species or answer.scientific_name or confirmed):
            remember(text, answer.species, source="llm", scientific_name=answer.scientific_name)
        if answer.species:
            return [answer.species], "llm"

    return [], "none"
