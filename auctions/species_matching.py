"""Turn a lot name someone typed into a short list of species to pick from.

The user never types a scientific name.  They type "blue dream shrimp" or "F1 Tropheus duboisi
maswa" or "sponge filter", and this module answers with a handful of species out of the
:class:`~auctions.models.Species` table -- or nothing, which is the right answer for the sponge
filter.  "A handful" is :data:`MAX_SUGGESTIONS`, except for a bare genus or epithet where the
complete set is the answer and the cap is :data:`MAX_GENUS_MATCHES`.

Four steps, most trustworthy first, stopping at the first one that answers:

1. **Exact** -- the whole typed name is a scientific name or a common name.  "Guppy" is done here.
2. **Cache** -- :class:`~auctions.models.SpeciesSearchCache`, keyed on the normalised name.  Lot
   names repeat constantly across clubs, so most lookups that get this far end here.
3. **Search** -- token and phrase matching against scientific and common names, ranked.  Handles
   "Tropheus duboisi maswa", where the name is a real species plus a collection location.
4. **Language model** -- only when the first three found nothing, and only ever asked to *pick
   from a shortlist we built*, never to invent a name.  Its answer is written to the cache so the
   same lot name is free forever after.

The cache is second rather than first even though it is the cheapest lookup: it holds guesses and
is shared by every club, so a single bad row must not be able to outrank the species list itself.

Nothing here can return a species that isn't in the database, which is what makes the
"don't let the user enter anything not on the list" rule enforceable: the form validates the
submitted pk against the same table, so a hand-crafted POST can't smuggle in free text.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .llm import LLMError, get_provider
from .models import LLMUsage, Species, SpeciesCommonName, SpeciesSearchCache, normalize_species_name

logger = logging.getLogger(__name__)

#: Never offer more than this many, plus "No species".  A picklist you have to read is a picklist
#: nobody reads -- if we can't get it down to a handful the honest answer is "we don't know".
MAX_SUGGESTIONS = 5

#: Except for a bare genus or a bare epithet, where the *complete* set is the answer and a
#: truncated one would be a lie -- "here are five of the six Tropheus" is worse than nothing.  A
#: little larger than :data:`MAX_SUGGESTIONS` because the list is homogeneous and reads fast; past
#: this the genus is telling the user nothing they didn't already type.
MAX_GENUS_MATCHES = 8

#: How many candidates to put in front of the model.  Big enough that the right answer is usually
#: in there, small enough to stay cheap on a per-lot call.
LLM_SHORTLIST_SIZE = 40

#: Relative weights for :func:`search_matches`.  Only the ordering matters -- a strong match (a
#: full scientific name, or a multi-word common name) always outranks a weak one (a bare genus),
#: and results below the best score found are dropped rather than padding the list.
STRONG_SCORE = 10
WEAK_SCORE = 1

#: Daily cap on model calls per user.  Bulk-adding 50 lots that all miss the cache is a real
#: scenario; this stops a stuck client from spending the month's budget in an afternoon.
MAX_LLM_CALLS_PER_USER_PER_DAY = 100

_RATE_LIMIT_WINDOW_SECONDS = 60 * 60 * 24

#: Words that never help identify a species.  Reuses the list the category guesser already tunes
#: against real lot names ("pair", "trio", "young", colours...), plus a few that only matter here.
_EXTRA_IGNORE_WORDS = {"sp", "spp", "var", "cf", "aff", "unknown", "assorted", "mixed", "misc"}

# Written defensively because the failure mode is not "no answer", it is a *confident wrong*
# answer: with a shortlist in front of it a model will happily decide "sponge filter" is a Ball
# sponge, "Bolivian ram" is a Banded gourami, and "cherry shrimp" is an Amano shrimp. Each of
# those then gets printed on a label and counted for breeder points. Hence the worked negative
# examples and the flat instruction that null is the normal answer.
_SYSTEM_PROMPT = (
    "You identify the exact species an aquarium club lot is selling. You are given the lot name "
    "and a numbered list of candidate species from a fixed database.\n"
    'Reply with JSON: {"id": <id from the list>} or {"id": null}.\n'
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
    "what the lot name is calling this organism. Never invent a species or an id."
)

#: How hard the model should think about this. The palette's default is "minimal" because it is
#: picking from a menu with a person waiting; here the same setting produced confident nonsense,
#: and the call happens on blur while the user carries on typing, so a slower, more careful answer
#: costs nothing they will notice.
REASONING_EFFORT = "low"


def normalize(text):
    """Lowercase, strip punctuation, collapse whitespace.  The cache key and the match key.

    Defined in models.py so that the *stored* normalised name columns are built by the identical
    function -- see :func:`auctions.models.normalize_species_name`.  Re-exported here because this
    is where it is read as part of the matching rules.
    """
    return normalize_species_name(text)


def singularize(word):
    """A crude English singular.  ``guppies`` -> ``guppy``, ``tetras`` -> ``tetra``.

    Lot names are almost always plural ("6 cardinal tetras", "guppies") and species lists never
    are, so without this the most common phrasing on the site matches nothing.  Crude is fine:
    a wrong singular just fails to match, which is the same as not trying.
    """
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def base_words(text):
    """The words actually typed in *text*, minus the ones that never identify a species.

    One entry per word, unlike :func:`keywords`, which also emits a singular for each -- so this
    is what to count when the question is "how many things did they type?".
    """
    ignore = set(settings.IGNORE_WORDS) | _EXTRA_IGNORE_WORDS
    return [word for word in re.findall(r"[a-z]{3,}", normalize(text)) if word not in ignore]


def keywords(text):
    """The words in *text* worth searching on, longest first.

    Longest first because the discriminating word in "young albino bristlenose pleco" is the long
    one, and the shortlist is capped.
    """
    ignore = set(settings.IGNORE_WORDS) | _EXTRA_IGNORE_WORDS
    words = []
    for word in re.findall(r"[a-z]{3,}", normalize(text)):
        for form in (word, singularize(word)):
            if form not in ignore and len(form) >= 3:
                words.append(form)
    # dict.fromkeys de-duplicates without losing the order we then sort on
    return sorted(dict.fromkeys(words), key=len, reverse=True)


#: Longest common name worth looking for as a phrase.  FishBase's English names run to four words
#: ("Southern platyfish", "Green swordtail", "Black-banded leporinus"...); beyond that a lot name
#: is describing the fish, not naming it.  Five rather than four because both sides of the lookup
#: are normalised, and normalising splits a name on its hyphens: "Black-banded leporinus" is two
#: words to a reader and three to the matcher.
MAX_PHRASE_WORDS = 5


def _phrases(normalized):
    """Every 2-to-:data:`MAX_PHRASE_WORDS`-word run in *normalized*, plus a singular variant.

    The singular variant only touches the last word, which is where English puts the plural:
    "6 cardinal tetras" yields "cardinal tetra", which is what the species list actually holds.
    """
    words = normalized.split()
    phrases = set()
    for size in range(2, MAX_PHRASE_WORDS + 1):
        for start in range(len(words) - size + 1):
            run = words[start : start + size]
            phrases.add(" ".join(run))
            phrases.add(" ".join(run[:-1] + [singularize(run[-1])]))
    return phrases


def _rate_limit_key(user):
    return f"species_llm_{user.pk if user else 'anon'}_{timezone.localtime():%Y%m%d}"


def check_rate_limit(user, limit=None):
    """Consume one unit of *user*'s daily model budget.  True when the call may proceed.

    ``cache.add`` then ``cache.incr``: one round trip, and no read-modify-write race between two
    workers handling a bulk-add page's parallel lookups.

    The default is read here rather than bound as an argument default so that changing
    :data:`MAX_LLM_CALLS_PER_USER_PER_DAY` actually changes the budget.
    """
    limit = MAX_LLM_CALLS_PER_USER_PER_DAY if limit is None else limit
    key = _rate_limit_key(user)
    cache.add(key, 0, timeout=_RATE_LIMIT_WINDOW_SECONDS)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=_RATE_LIMIT_WINDOW_SECONDS)
        used = 1
    return used <= limit


#: Words that wrap a name in a quantity rather than saying anything about the animal.  Stripped
#: only from the *ends* of a lot name, never from the middle: "blue dream shrimp" is a cultivar's
#: name and two thirds of it are in :data:`settings.IGNORE_WORDS`.
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
    "assorted",
    "mixed",
}


def strip_quantity(normalized):
    """``"6 guppies"`` -> ``"guppies"``.  The commonest shape a lot name comes in.

    ``exact_matches`` asks whether the *whole* typed name is a species name, so a leading count
    stopped it dead: "guppies" found the guppy and "6 guppies" found nothing at all, and the
    search step could not save it either -- its common-name rule needs a two-word phrase, and
    "guppy" is one word.  Between them that left the single most common phrasing on the site with
    no answer.

    Only the ends are trimmed, and only counts and quantity words.  Anything that touched the
    middle of a name would break the cultivars, where the strain is spelled out of ordinary
    adjectives: *Neocaridina davidi* 'Blue Dream' is reached by typing "blue dream shrimp".
    """
    words = normalized.split()
    while words and (words[0].isdigit() or words[0] in _QUANTITY_WORDS):
        words.pop(0)
    while words and (words[-1].isdigit() or words[-1] in _QUANTITY_WORDS):
        words.pop()
    return " ".join(words)


def exact_matches(text):
    """Species whose scientific name or one of whose common names *is* the typed text.

    Ranked by how much each kind of match means: the scientific name, then the species FishBase
    *designates* by that common name, then anything merely carrying it as a synonym.  Several
    poeciliids answer to "guppy"; only one of them is the guppy.
    """
    normalized = normalize(text)
    if not normalized:
        return []
    # "Guppies" and "Guppy" are the same request; the list only ever holds the singular.  The
    # quantity-stripped form is asked for as well, so "6 guppies" and "guppies" agree -- see
    # strip_quantity().  A set, so a name with no count in it costs nothing extra.
    candidates = set()
    for form in (normalized, strip_quantity(normalized)):
        if not form:
            continue
        words = form.split()
        candidates.add(form)
        candidates.add(" ".join(words[:-1] + [singularize(words[-1])]))
    # Separate indexed lookups rather than one join with a CASE ordering: on 139k species and 75k
    # common names the join plan was the single slowest thing in a lookup, and running them in
    # order of how much each one means is also how the results get ranked.
    found = {}
    # Nominal species only.  A cultivar shares its parent's scientific name, so "Neocaridina
    # davidi" would otherwise answer with the species *and* its thirteen colour strains, none of
    # which the user asked for.  A strain is reached by its own name -- "blue dream shrimp" is one
    # of its common names, and that is the lookup below.
    for species in Species.objects.filter(scientific_name__in=candidates, variety="")[:MAX_SUGGESTIONS]:
        found[species.pk] = species
    # FBname -- the one English name FishBase designates for a species -- before the synonym list.
    # Several poeciliids carry "Guppy" as *a* common name; only Poecilia reticulata is *the* guppy,
    # and the per-name PreferredName flag is set on barely 3% of rows, so it can't do this job.
    # Both of these match the *normalised* column, not the name as written.  The candidates have
    # had their punctuation stripped by normalize(), and a fifth of FishBase's common names have
    # punctuation of their own -- so "Ram's horn snail" is only reachable through this column.
    for species in Species.objects.filter(common_name_normalized__in=candidates)[:MAX_SUGGESTIONS]:
        found.setdefault(species.pk, species)
    # Ordered before the slice, for the same reason every other LIMIT in this module is: a name
    # like "Angelfish" is carried by thirty-odd species, and an unordered fifteen of them is how
    # the freshwater one -- the only one a freshwater club is selling -- ends up not being offered
    # at all.  Habitat before trade rank because a reef fish is flagged for the aquarium trade
    # just as firmly as a freshwater one, so trade_rank alone cannot tell them apart.
    common_names = (
        SpeciesCommonName.objects.filter(name_normalized__in=candidates)
        .select_related("species")
        .order_by("-is_preferred", "-species__freshwater", "species__trade_rank")[: MAX_SUGGESTIONS * 3]
    )
    for common in common_names:
        found.setdefault(common.species_id, common.species)
    return list(found.values())[:MAX_SUGGESTIONS]


def _trade_first(queryset, prefix=""):
    """Order *queryset* by :attr:`Species.trade_rank`, so aquarium species come first.

    Applied before every ``LIMIT`` this module takes, which is the whole point: an unordered slice
    of everything whose common name contains "tetra" is how the one tetra anybody sells ends up
    not being offered to the model.
    """
    return queryset.order_by(f"{prefix}trade_rank")


def _rank(species_list, category=None):
    """Move the likeliest candidates to the front, without disturbing anything else.

    Three preferences, in this order:

    1. **The category the lot already looks like.**  Only ever a re-ordering -- a category is a
       guess made from the lot's *name*, so letting it exclude a species would be one guess
       silently overruling the species list.
    2. **Whether it lives in fresh water.**  FishBase's habitat columns are on the model for
       exactly this -- "there are freshwater and saltwater fish called perch" -- and until this
       used them, "Angelfish" answered with five marine angelfish and never offered *Pterophyllum
       scalare*, because a reef fish is flagged for the aquarium trade just as firmly as a
       freshwater one and :attr:`Species.trade_rank` therefore cannot separate them.  It sits
       *below* the category so a club that really is selling marine fish still gets its own answer
       first, and it is only ever a tie-break: an exact match on "Emperor angelfish" is unaffected.
    3. **Whether anyone keeps this fish**, in the three steps of
       :attr:`Species.trade_rank`.  FishBase carries 36,000 species and about 3,500 of them are
       flagged as aquarium fish; when a name is shared, the one being sold at a fish club is
       overwhelmingly the one in the hobby.

    A stable sort on purpose.  Callers arrive with an order that already means something --
    :func:`exact_matches` puts *the* guppy ahead of the four other fish called one -- and this must
    only break ties in it, not replace it.
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


def search_matches(text, limit=MAX_SUGGESTIONS, category=None):
    """Species the typed text genuinely names, ranked.  Empty when nothing does.

    Three rules, all deliberately strict, because a plausible-looking wrong answer is worse here
    than no answer -- a wrong species gets printed on a label and counted for breeder points,
    while no answer just leaves "No species" selected and falls through to the model.

    *Scientific token*
        A word in the lot name **is** a genus or a specific epithet.  "Tropheus duboisi maswa"
        matches *Tropheus duboisi* on two tokens; "Betta splendens pair" matches on two.

    *Common-name phrase*
        A species' whole common name appears in the lot name as a phrase.  "6 young cardinal
        tetras" contains "Cardinal tetra".

    *Bare epithet*
        The lot name is one word and that word is a specific epithet.  Somebody typing "saulosi"
        means *Chindongo saulosi* or *Aulonocara saulosi* and would like to be shown both.  It
        takes a single-word name because that is what makes it safe: in "Neocaridina davidi" the
        first word is a genus this list has never heard of, and answering with the unrelated fish
        that happen to share "davidi" would be worse than admitting we don't know.

    What is deliberately excluded is loose substring matching, which is where the obvious
    implementation goes wrong: "sponge filter" hits *Sponge frillgoby*, "blue dream shrimp" hits
    *Shrimp scad*, and "Bolivian ram" hits *Abramis brama*.  None of those are the thing being
    sold, and all of them look like real answers in a dropdown.

    *category*, when given, only ever breaks a tie -- see :func:`_rank`.
    """
    words = set(keywords(text))
    if not words:
        return []
    normalized = normalize(text)
    scored = {}

    # Rule 1: words that are exactly a genus and a specific epithet.  Only the genus is asked for
    # in SQL -- it is the indexed column, and an epithet on its own is handled by rule 3 under much
    # tighter conditions.  So the epithet is checked in Python against the rows the genus already
    # narrowed us to, which keeps this a single indexed lookup instead of a full scan.
    #
    # Nominal species only (``parent__isnull=True``): a cultivar carries its parent's genus and
    # epithet, so without this "Neocaridina davidi" would score the species and every one of its
    # colour strains identically and then show five of them at random.  Strains are reached
    # through their own names in rule 2.
    genus_candidates = {word.capitalize() for word in words}
    # Trade-ordered before the slice: a genus with more species than the bound would otherwise
    # hand back an arbitrary 80 of them, and the fallback below -- "show the ones people keep" --
    # can only work on rows it was actually given.
    genus_hits = _trade_first(Species.objects.filter(genus__in=genus_candidates, parent__isnull=True))
    for species in genus_hits[: LLM_SHORTLIST_SIZE * 2]:
        has_genus = species.genus.lower() in words
        has_epithet = bool(species.species) and species.species.lower() in words
        if has_genus and has_epithet:
            scored[species.pk] = (STRONG_SCORE, species)
        elif has_genus:
            scored[species.pk] = (WEAK_SCORE, species)

    # Rule 2: a common name of two or more words appearing in the lot name as a phrase.  Asking
    # for the phrases directly -- every 2-to-4 word run in the lot name, looked up by equality --
    # rather than searching for names *containing* a keyword: an `icontains` over the common-name
    # table returns thousands of rows for a word like "tetra", and any bound you put on that is an
    # unordered slice that quietly loses the right answer.
    #
    # Single-word common names are left to exact_matches.  On their own they are as likely to
    # mislead as to help: "Bolivian ram" would match the species FishBase simply calls "Ram",
    # which is a different fish.
    phrases = _phrases(normalized)
    if phrases:
        for common in SpeciesCommonName.objects.filter(name_normalized__in=phrases).select_related("species"):
            score = STRONG_SCORE + len(common.name.split()) + (1 if common.is_preferred else 0)
            previous = scored.get(common.species_id)
            if previous is None or previous[0] < score:
                scored[common.species_id] = (score, common.species)

    # Rule 3: a one-word lot name that is a specific epithet.  Last, and only when the first two
    # found nothing, so it can never dilute a real answer.
    # Counted on base_words, not on `words`: keywords() emits a singular alongside every word, so
    # a one-word lot name ending in "s" ("Corydoras") arrives here looking like two.
    typed = base_words(text)
    if not scored and len(typed) == 1:
        forms = {typed[0], singularize(typed[0])}
        epithet_hits = list(Species.objects.filter(species__in=forms, parent__isnull=True)[: MAX_GENUS_MATCHES + 1])
        if 0 < len(epithet_hits) <= MAX_GENUS_MATCHES:
            return _rank(_alphabetical(epithet_hits), category)

    if not scored:
        return []
    best = max(score for score, _ in scored.values())
    ranked = _rank(_alphabetical([species for score, species in scored.values() if score == best]), category)
    if best <= WEAK_SCORE:
        # Nothing but a genus matched.  The complete genus is a real answer -- somebody typing
        # "Tropheus" wants to see the six of them -- but five out of seventy Ancistrus is not an
        # answer, it is a list that implies one.
        if len(ranked) <= MAX_GENUS_MATCHES:
            return ranked
        # Unless the hobby has an opinion: seventy-seven Ancistrus in FishBase are two in the
        # hobby, and those two are what a fish club is selling.  Deliberately the species-level
        # rank and not the genus one -- every member of a genus shares the genus rank, so it can't
        # narrow a genus down by definition.
        in_trade = [species for species in ranked if species.trade_rank == Species.TRADE_RANK_SPECIES]
        if 0 < len(in_trade) <= MAX_GENUS_MATCHES:
            return in_trade
        return []
    return ranked[:limit]


def _shortlist(words, normalized):
    """Species worth putting in front of the model, for the keywords in a lot name.

    A wider net than :func:`search_matches` casts -- the model can discard noise, so recall
    matters more than precision here -- but the net has to be *ordered*.  The obvious version, one
    ``icontains`` OR with a ``LIMIT``, is an unordered slice: for "german blue ram" it returns
    forty species whose names merely contain "ram" (*Abramis*, *Abramites*, ...) and leaves out the
    one FishBase actually calls "Ram", so the model is asked to choose and correctly answers null.
    Recall failures here look exactly like the model being unhelpful.

    So the layers run best-evidence-first and stop once the list is full:

    1. a common name that *is* one of the keywords, or one of the phrases in the lot name
    2. a genus that is one of the keywords, and the genus siblings of anything found in layer 1 --
       "Ram" finds *Mikrogeophagus ramirezi*, and its sibling is the Bolivian ram
    3. anything whose common name merely contains a keyword, to fill the remaining space
    """
    candidates = {}

    def add(queryset):
        """Take rows from *queryset* until the shortlist is full.  Earlier layers keep their places."""
        remaining = LLM_SHORTLIST_SIZE - len(candidates)
        if remaining <= 0:
            return
        for row in queryset[:remaining]:
            species = row if isinstance(row, Species) else row.species
            candidates.setdefault(species.pk, species)

    phrases = _phrases(normalized) | set(words)
    add(
        _trade_first(SpeciesCommonName.objects.filter(name_normalized__in=phrases), "species__").select_related(
            "species"
        )
    )

    genera = {word.capitalize() for word in words} | {species.genus for species in candidates.values()}
    add(_trade_first(Species.objects.filter(genus__in=genera)))

    name_q = Q()
    for word in words:
        name_q |= Q(name_normalized__icontains=word)
    add(_trade_first(SpeciesCommonName.objects.filter(name_q), "species__").select_related("species"))
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


def llm_match(text, user=None):
    """Ask the model to pick one species out of a shortlist.  Returns a Species or None.

    The shortlist is built from the database by keyword, so this is a ranking problem for the
    model, not a recall problem -- it cannot name a species we don't stock, and an answer that
    isn't in the shortlist is discarded rather than trusted.
    """
    provider = get_provider()
    if not provider.is_configured():
        return None
    # Deliberately not the site-wide effort; see REASONING_EFFORT.  Left alone when the deployment
    # has switched it off entirely, which is how an operator says "don't send this parameter".
    if provider.reasoning_effort:
        provider.reasoning_effort = REASONING_EFFORT
    words = keywords(text)
    if not words:
        return None
    candidates = _shortlist(words, normalize(text))
    if not candidates:
        return None
    if not check_rate_limit(user):
        logger.info("Species lookup rate limit reached for %s", user)
        return None
    listing = "\n".join(f"{species.pk}: {species.label}" for species in candidates)
    messages = [{"role": "user", "content": f"Lot name: {text}\n\nCandidates:\n{listing}"}]
    try:
        result = provider.complete_json(_SYSTEM_PROMPT, messages, max_tokens=1000)
    except LLMError:
        logger.info("Species lookup failed for %r", text, exc_info=True)
        _record_usage(user, None, text, "error", success=False)
        return None
    raw = result.data.get("id")
    try:
        chosen_pk = int(raw)
    except (TypeError, ValueError):
        _record_usage(user, result, text, "no_species")
        return None
    # Never trust the id: it has to be one we offered.
    chosen = next((species for species in candidates if species.pk == chosen_pk), None)
    _record_usage(user, result, text, "species" if chosen else "no_species")
    return chosen


def remember(text, species, source="llm"):
    """Write an answer to the cache, including the answer "this is not a species"."""
    normalized = normalize(text)
    if not normalized:
        return
    SpeciesSearchCache.objects.update_or_create(
        search_text=normalized,
        defaults={"species": species, "source": source},
    )


def suggest_species(text, user=None, use_llm=True, category=None):
    """The one call the views make: a handful of species for a typed lot name.

    Returns ``(species_list, source)`` where source is one of ``cache``, ``exact``, ``search``,
    ``llm`` or ``none`` -- the caller shows it for debugging and nothing else.  An empty list is
    a legitimate answer, and the UI turns it into "No species".

    *category* is the category the lot form currently shows, when the caller has one.  It only
    ever re-orders candidates that already matched (see :func:`_rank`), never filters them: the
    category is itself a guess from the lot's name, and one guess quietly vetoing the species list
    is exactly the failure this module is written to avoid.
    """
    normalized = normalize(text)
    if not normalized:
        return [], "none"

    # Exact matching runs *before* the cache even though the cache is cheaper.  The cache holds
    # answers that were guessed, and it is shared by every club: one bad row would otherwise
    # outrank the species list itself, forever, for everybody.  Two indexed lookups is a small
    # price for the guarantee that a name the list knows is always answered by the list.
    exact = _rank(exact_matches(text), category)
    if exact:
        return exact, "exact"

    cached = SpeciesSearchCache.objects.filter(search_text=normalized).select_related("species").first()
    if cached:
        # Cheap and racy on purpose: this counter exists to show which names are carrying the
        # cache, not to be exact.
        SpeciesSearchCache.objects.filter(pk=cached.pk).update(hits=cached.hits + 1)
        return ([cached.species] if cached.species else []), "cache"

    found = search_matches(text, category=category)
    if found:
        return found, "search"

    if use_llm:
        chosen = llm_match(text, user=user)
        # Remember the miss as well as the hit.  "Sponge filter" should cost one call ever, not
        # one per club that sells one.
        remember(text, chosen, source="llm")
        if chosen:
            return [chosen], "llm"

    return [], "none"
