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

import datetime
import logging
import re

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

#: Never offer more than this many, plus "No species".  A picklist you have to read is a picklist
#: nobody reads -- if we can't get it down to a handful the honest answer is "we don't know".
MAX_SUGGESTIONS = 5

#: Except for a bare genus or a bare epithet, where the *complete* set is the answer and a
#: truncated one would be a lie -- "here are five of the six Tropheus" is worse than nothing.  A
#: little larger than :data:`MAX_SUGGESTIONS` because the list is homogeneous and reads fast; past
#: this the genus is telling the user nothing they didn't already type.
MAX_GENUS_MATCHES = 8

#: How many species a single word may name and still be treated as naming them.
#:
#: The risk with a one-word common name is not that it is wrong, it is that it is *ambiguous* --
#: "guppy" means one fish and "catfish" means 143 -- so the rule is the ambiguity bound rather
#: than a hand-kept list of words we trust.  A list would have to be maintained, would always be
#: behind the hobby, and could not answer for the name somebody adds tomorrow; this can.  Set to
#: :data:`MAX_SUGGESTIONS` on purpose: the question "can we get it down to a picklist?" is the
#: same question, and one number should not be able to drift away from the other.
#:
#: Where it lands on real data: 91% of the single-word common names in the database resolve to
#: four species or fewer.  "guppy" is 3, "ram" 1, "cory" 1, "molly" 2, "goldfish" 2, "barb" 4,
#: "gourami" 4 -- and "tetra" (28), "angelfish" (27), "killifish" (19) and "catfish" (143) are
#: refused, which is the answer a person would give too.
MAX_SINGLE_WORD_MATCHES = MAX_SUGGESTIONS

#: How many *other* species' names a word may appear inside and still be treated as naming a fish.
#:
#: The second guard, and the one that separates a name from a category.  Counting how many species
#: a word names on its own is not enough: "barb" is the whole common name of exactly one fish in
#: FishBase (*Pethia ticto*, the ticto barb), so by the ambiguity bound alone "odessa barb" would
#: confidently answer with a ticto barb.  What gives it away is that "barb" is a *component* of
#: 218 other names -- it is a kind of fish, and picking one member of a group of 218 is the same
#: mistake as offering five of the seventy *Ancistrus*.
#:
#: Measured on the real table: ram 1, oscar 2, badis 3, koi 5, discus 7, platy 9, goldfish 12,
#: convict 16, guppy 16, neon 20, gourami 21, swordtail 21, harlequin 23, molly 34 -- against
#: zebra 60, glass 64, rainbow 99, angelfish 103, cory 122, tetra 134, loach 209, barb 218,
#: dwarf 228, catfish 480.  Forty sits in the gap.  Losing "cory" is the right outcome and the
#: one the hobby would agree with: which cory?
MAX_NAMES_USING_A_WORD = 40

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
#: "box" is here for the same reason "filter" is in the site-wide list: it is a container, it turns
#: up in "breeder box" and "box of misc", and FishBase calls the spotted boxfish "Box" -- so
#: without it the single-word rule answers a box of hardware with a reef fish.  It is the only
#: hardware word that collides with an in-trade species; the rest of the vocabulary ("tank",
#: "heater", "gravel", "media") names nothing in the list and needs no help.
_EXTRA_IGNORE_WORDS = {"sp", "spp", "var", "cf", "aff", "unknown", "assorted", "mixed", "misc", "box"}

#: The two sources whose common names arrived in bulk from an ichthyology database rather than
#: from anybody who sells fish.  Both are worth having -- they are what makes 36,000 species
#: findable at all -- but a name from one of them is not evidence that the hobby uses it, which is
#: the distinction :func:`_single_word_matches` turns on.
IMPORTED_NAME_SOURCES = ("fishbase", "sealifebase")

# Written defensively because the failure mode is not "no answer", it is a *confident wrong*
# answer: with a shortlist in front of it a model will happily decide "sponge filter" is a Ball
# sponge, "Bolivian ram" is a Banded gourami, and "cherry shrimp" is an Amano shrimp. Each of
# those then gets printed on a label and counted for breeder points. Hence the worked negative
# examples and the flat instruction that null is the normal answer.
_SYSTEM_PROMPT = (
    "You identify the exact species an aquarium club lot is selling. You are given the lot name "
    "and a numbered list of candidate species from a fixed database, which may be empty.\n"
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
    "what the lot name is calling this organism. Never invent a species or an id.\n"
    "If no candidate is right but you are confident which species the lot name names, you may "
    'instead reply {"scientific_name": "Genus species"} with the currently accepted binomial. It '
    "is looked up in the same database; if it is not there the answer is no species. Use this "
    "only for a species you are sure of, never to guess at a name that might exist."
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


#: What counts as a word worth searching on: three or more characters, starting with a letter and
#: allowed to carry digits after it.
#:
#: The digits are not decoration.  Half of what a fish club sells is named by a code rather than by
#: a species -- "L046", "CW11", "C121", "OB peacock" -- because the fish is undescribed and the
#: code *is* the identification, agreed on internationally and printed in every catalogue.  A
#: letters-only pattern threw all of them away before any lookup ran: "l046" became "l", which is
#: too short to survive, so a lot called "L046 pleco" was searched for as "pleco".  Requiring a
#: leading letter is what keeps the counts out -- "6" and "10" in "6 guppies" and "10 gallon tank"
#: are not words, and :func:`strip_quantity` is what deals with those.
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


def base_words(text):
    """The words actually typed in *text*, minus the ones that never identify a species.

    One entry per word, unlike :func:`keywords`, which also emits a singular for each -- so this
    is what to count when the question is "how many things did they type?".
    """
    ignore = set(settings.IGNORE_WORDS) | _EXTRA_IGNORE_WORDS
    return [word for word in _WORD.findall(normalize(text)) if word not in ignore]


def keywords(text):
    """The words in *text* worth searching on, longest first.

    Longest first because the discriminating word in "young albino bristlenose pleco" is the long
    one, and the shortlist is capped.
    """
    ignore = set(settings.IGNORE_WORDS) | _EXTRA_IGNORE_WORDS
    words = []
    for word in _WORD.findall(normalize(text)):
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


class LLMBudget:
    """One named daily allowance of model calls, and what is left of it.

    Spent at the moment a call is about to be made -- inside :func:`llm_match`, past the exact,
    cache and search steps -- and never merely because a request arrived.  That distinction is the
    whole point of the class: a caller doing ten thousand lookups a day that the database answers
    for free has spent nothing, and must not be told it is out of budget.

    The day is part of the cache key, so the allowance rolls over at local midnight without
    anything having to expire it, and the day it names is the day an operator would name.

    *name* says whose allowance it is.  A user has one (the lot forms), and so does a club (the
    club API), because a key is a script rather than a person: without a bucket of its own every
    key on the site would share the single anonymous one and one busy integration would switch the
    model off for everybody.
    """

    def __init__(self, name, limit):
        self.name = name
        self.limit = limit
        self.key = f"species_llm_{name}_{timezone.localtime():%Y%m%d}"
        #: Set once a call has been refused, so a caller can tell "the model found nothing" from
        #: "the model was never asked".  The club API turns the second into a 429.
        self.blocked = False
        #: How many calls this object allowed, which for a single request is "did this one cost
        #: money".  The club API reports it as the response's ``llm`` field.
        self.spent = 0

    @classmethod
    def for_user(cls, user, limit=None):
        """The budget the lot forms spend.  ``user=None`` is the shared anonymous bucket.

        The default is read here rather than bound as an argument default so that changing
        :data:`MAX_LLM_CALLS_PER_USER_PER_DAY` actually changes the budget.
        """
        return cls(str(user.pk) if user else "anon", MAX_LLM_CALLS_PER_USER_PER_DAY if limit is None else limit)

    @classmethod
    def for_club(cls, club, limit):
        return cls(f"club{club.pk}", limit)

    def spend(self):
        """Consume one unit.  True when the call may proceed.

        ``cache.add`` then ``cache.incr``: one round trip, and no read-modify-write race between
        two workers handling a bulk-add page's parallel lookups.
        """
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
        # Clamped: the counter keeps climbing past the limit, because refusing a call is cheaper
        # than reading the counter first, and a negative "remaining" would only confuse a caller.
        return max(0, self.limit - self.used)

    @property
    def resets_at(self):
        """Local midnight -- the moment the key's date changes and the allowance starts again."""
        tomorrow = timezone.localtime() + datetime.timedelta(days=1)
        return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


def check_rate_limit(user, limit=None):
    """Consume one unit of *user*'s daily model budget.  True when the call may proceed."""
    return LLMBudget.for_user(user, limit).spend()


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

    "Assorted" and "mixed" are deliberately *not* quantity words, though they are ignored
    everywhere else.  A count says how many of one thing; those two say it is not one thing.
    Stripping them made "assorted tetras" mean "tetras", which is 28 species, of which the caller
    then showed five at random -- a picklist for a lot whose whole point is that it is a mixed
    bag.  Left in place the name simply fails to match, and "assorted guppies" and "assorted
    platy" still work, because they are answered a step later by the single-word rule, which
    ignores the word properly instead of deleting it.
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
        # A subquery rather than a list of ids, so this stays one round trip however many clubs
        # somebody belongs to.
        member_of = ClubMember.objects.filter(user=user, is_deleted=False).values("club_id")
        approved |= Q(**{f"{prefix}club__in": member_of})
    # Guarded, and it has to be: `club=None` would read as "every species with no club", which is
    # every unapproved species on the site.
    if club is not None:
        approved |= Q(**{f"{prefix}club": club})
    return approved


def visible_species(user=None, club=None):
    """The species *user* may be offered.  A queryset, so callers keep filtering it.

    Everything the importers loaded is approved and visible to everybody.  What is not is a
    species somebody added *on the site* without the standing to add it to the whole site: an
    auction admin at a check-in table needs a missing fish on a label in the next thirty seconds,
    and waiting for a superuser is not an option -- but 36,000 imported rows are a shared asset
    and one club's guess at a name should not land in another club's picker.

    So an unapproved row is visible three ways, and approving it is what makes it everyone's:

    * to the person who added it, always;
    * to anyone at the club it was added for, because a check-in table is staffed by more than one
      person and the volunteer on the next laptop needs the same picker;
    * to a caller working in the context of that club -- the club API, a lot in one of its
      auctions.

    :attr:`Species.club` is filled in only when there was an obvious club to fill in, so it can
    never be the *only* route: plenty of auctions have no club attached at all, and scoping this
    to clubs alone would leave the feature doing nothing at exactly the auctions most likely to
    need it.  Hence "user or club", with both optional.

    ``user=None`` and ``club=None`` -- the backfill command, the club API authenticating a script
    rather than a person -- sees only approved species, which is the conservative answer for a
    caller writing to old lots or feeding somebody else's breeder-award program.
    """
    return Species.objects.filter(_visible(user, club))


def visible_common_names(user=None, club=None):
    """Common names this caller may be answered with.  See :func:`visible_species`.

    Two conditions, not one: the *species* has to be visible and so does the *name*.  A name is
    scoped the same way a species is -- by ``approved``, ``added_by`` and ``club`` -- because it is
    read ahead of everything else the matcher does.  "Yellow lab" is answered out of this table, so
    a club teaching the site a name for the wrong fish would otherwise be everybody's problem, on
    a row with no approval step in front of it.

    Everything the importers and the curated CSV wrote is ``approved=True``, which is what keeps
    FishBase's 49,000 names visible to everybody without a migration having to say so.
    """
    return SpeciesCommonName.objects.filter(_visible(user, club, prefix="species__")).filter(_visible(user, club))


def split_scientific_name(typed):
    """``"Ancistrus Cirrhosus"`` -> ``("Ancistrus", "cirrhosus")``.  A genus on its own is fine.

    Asked for as one string rather than two boxes -- nobody types a genus and an epithet into
    separate fields at a check-in table -- and split here, once, so the form and the club API
    cannot disagree about what "Ancistrus sp. L183" means.
    """
    parts = (typed or "").strip().split()
    if not parts:
        return "", ""
    return parts[0].capitalize()[:100], " ".join(parts[1:]).lower()[:150]


def species_already_named(genus, epithet, variety="", user=None, club=None):
    """The species this name already belongs to, if the caller can see one.  None otherwise.

    Scoped to what the caller may see, for both halves of the reason :func:`visible_species`
    exists: pointing somebody at a row they cannot open is no help, and answering "that already
    exists" when what exists is another club's unapproved row leaks it.
    """
    return (
        visible_species(user, club)
        .filter(genus__iexact=genus, species__iexact=epithet, variety__iexact=variety or "")
        .first()
    )


def species_carrying_common_name(name, user=None, club=None, exclude=None):
    """The species this common name already names, ignoring *exclude*.  None if it is free.

    Two places a name can live -- :attr:`Species.common_name`, which is the one designated name,
    and the :class:`SpeciesCommonName` rows -- so both are asked.

    What it is for is refusing to make an existing name ambiguous.  A name is the strongest signal
    the matcher has: :func:`exact_matches` answers on it before anything else runs, and one name
    on two species turns a lookup that used to be ``unambiguous`` into a picklist for every club
    that could see both.  Adding "guppy" to a second fish is not a new name, it is the loss of an
    old one, so the answer is to say which species already has it.
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
    for species in visible_species(user, club).filter(scientific_name__in=candidates, variety="")[:MAX_SUGGESTIONS]:
        found[species.pk] = species
    # FBname -- the one English name FishBase designates for a species -- before the synonym list.
    # Several poeciliids carry "Guppy" as *a* common name; only Poecilia reticulata is *the* guppy,
    # and the per-name PreferredName flag is set on barely 3% of rows, so it can't do this job.
    # Both of these match the *normalised* column, not the name as written.  The candidates have
    # had their punctuation stripped by normalize(), and a fifth of FishBase's common names have
    # punctuation of their own -- so "Ram's horn snail" is only reachable through this column.
    for species in visible_species(user, club).filter(common_name_normalized__in=candidates)[:MAX_SUGGESTIONS]:
        found.setdefault(species.pk, species)
    # Ordered before the slice, for the same reason every other LIMIT in this module is: a name
    # like "Angelfish" is carried by thirty-odd species, and an unordered fifteen of them is how
    # the freshwater one -- the only one a freshwater club is selling -- ends up not being offered
    # at all.  Habitat before trade rank because a reef fish is flagged for the aquarium trade
    # just as firmly as a freshwater one, so trade_rank alone cannot tell them apart.
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
    # A synonym carried by several species, and nothing stronger to go on: prefer the species whose
    # *own* designated name says the same thing the typed name does.
    if not found:
        carried = _named_after_the_same_thing(normalized, carried)
    for species in carried:
        found.setdefault(species.pk, species)
    return list(found.values())[:MAX_SUGGESTIONS]


def _named_after_the_same_thing(normalized, candidates):
    """Narrow a shared common name down to the species that is really called that, if there is one.

    FishBase hands the same synonym to different fish on purpose, and "Peppered cory" is the case
    that matters: it is listed for *Corydoras paleatus*, which every hobbyist means by it, and also
    for *Corydoras julii*, whose own name is "Leopard corydoras".  Two candidates is not an answer
    -- the bulk-add page fills nothing in unless there is exactly one, and the seller is offered a
    picklist of two fish they cannot tell apart -- so the commonest cory in the hobby was
    unreachable by the name everybody types.

    The tie-break is the *designated* name, the one name FishBase picks out per species: "Peppered
    corydoras" shares a word with what was typed and "Leopard corydoras" shares none.  It only
    ever narrows to a single candidate, and only when nothing better matched at all -- a shared
    synonym is the weakest evidence :func:`exact_matches` acts on, so refining it cannot cost
    anything that was already a real answer.
    """
    if len(candidates) < 2:
        return candidates
    typed = set(keywords(normalized))
    agreeing = [species for species in candidates if typed & set(keywords(species.common_name_normalized))]
    return agreeing if len(agreeing) == 1 else candidates


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


def _single_word_matches(words, user=None, club=None):
    """Species named by *one word* of the lot name, when that word is unambiguous enough to act on.

    The gap this fills is "male guppy", "black guppy", "young koi", "L046 pleco" -- a lot name
    where the part that identifies the fish is a single common name and the rest is describing it.
    :func:`exact_matches` only answers when the *whole* typed name is a species name, and the
    phrase rule in :func:`search_matches` needs two words to work with, so between them every one
    of those returned nothing at all.

    What makes it safe is three bounds read off our own data rather than a list of words somebody
    has to keep.  A whitelist would need maintaining, would always be behind the hobby, and would
    have nothing to say about the name added to the curated list tomorrow.  A word answers only
    when all three hold:

    1. **It is not ambiguous.**  It names :data:`MAX_SINGLE_WORD_MATCHES` species or fewer, so
       "guppy" (3) answers and "catfish" (143) does not.
    2. **Somebody keeps the fish it names** -- :attr:`Species.trade_rank` 0 -- unless the name is
       one of *ours* rather than one of FishBase's, in which case it is in the list precisely
       because the hobby uses it.  This is the guard that matters most: without it "bronze cory"
       answers *Carcharhinus brachyurus*, because FishBase calls the copper shark "Bronze", and
       "black angel" answers with an angelshark.
    3. **It names a fish rather than a kind of fish** -- see :data:`MAX_NAMES_USING_A_WORD`.

    When several words qualify, the most *specific* one wins: fewest species, then longest word.
    """
    best = None
    for word in words:
        # SELECT DISTINCT ... LIMIT n+1: the exact count when it is small, and "more than we will
        # accept" when it is not, without counting all 143 rows for "catfish".
        # order_by() before values_list, here and below: SpeciesCommonName.Meta.ordering would
        # otherwise put `name` into the SELECT DISTINCT, so what comes back is one row per *name*
        # rather than per species and both bounds count the wrong thing.
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
        # Only for a word that got this far: three LIKEs, and the leading wildcard on one of them
        # means no index helps, so it must not run for every word of every lot name.
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

    *Single common name*
        One word of the lot name is a species' whole common name, and that word names few enough
        species to be worth acting on -- see :func:`_single_word_matches`.  Only when nothing
        above matched, so "Bolivian ram" is still the Bolivian ram rather than the fish FishBase
        simply calls "Ram".

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
    genus_hits = _trade_first(visible_species(user, club).filter(genus__in=genus_candidates, parent__isnull=True))
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
        # The genus is too broad to be an answer, so it is not one -- and the rules below have not
        # run yet.  "Male bettas" matched the genus *Betta*, which is 75 species and 20 in the
        # trade, and stopping here left the commonest lot name at a fish auction with no answer at
        # all; the common name "betta" is right there and means one fish.

    # Rule 3: one word of the lot name is a whole common name, and an unambiguous one.  After the
    # rules above rather than among them, so it can never dilute a real answer -- which is what
    # keeps "Bolivian ram" as the Bolivian ram rather than the fish FishBase simply calls "Ram".
    single = _single_word_matches(words, user=user, club=club)
    if single:
        return _rank(_alphabetical(single), category)

    # Rule 4: a one-word lot name that is a specific epithet.  Last, so it can never dilute a real
    # answer.  Counted on base_words, not on `words`: keywords() emits a singular alongside every
    # word, so a one-word lot name ending in "s" ("Corydoras") arrives here looking like two.
    typed = base_words(text)
    if len(typed) == 1:
        forms = {typed[0], singularize(typed[0])}
        epithet_hits = list(
            visible_species(user, club).filter(species__in=forms, parent__isnull=True)[: MAX_GENUS_MATCHES + 1]
        )
        if 0 < len(epithet_hits) <= MAX_GENUS_MATCHES:
            return _rank(_alphabetical(epithet_hits), category)
    return []


def _shortlist(words, normalized, user=None, club=None):
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
        _trade_first(visible_common_names(user, club).filter(name_normalized__in=phrases), "species__").select_related(
            "species"
        )
    )

    genera = {word.capitalize() for word in words} | {species.genus for species in candidates.values()}
    add(_trade_first(visible_species(user, club).filter(genus__in=genera)))

    name_q = Q()
    for word in words:
        name_q |= Q(name_normalized__icontains=word)
    add(_trade_first(visible_common_names(user, club).filter(name_q), "species__").select_related("species"))
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


def _species_named(scientific_name, user=None, club=None):
    """The species the model *named*, if we have it.  None otherwise.

    The shortlist is built by keyword search over our own tables, so its recall is our recall:
    "Yellow lab" only ever reached *Labidochromis caeruleus* because ``icontains "lab"`` happens
    to hit FishBase's "Labidochromis yellow", which is luck rather than design.  Letting the model
    answer with a scientific name instead of an id removes that dependency without giving up the
    guarantee that matters -- the name is looked up here, in the same table the form validates
    against, so a species we do not have is still no species.

    Nominal species only, and an exact match on the binomial: near-misses are how a plausible
    wrong answer would get in, and a wrong species is printed on a label and counted for points.
    """
    name = (scientific_name or "").strip()
    if not name or len(name.split()) > 3:
        return None
    return visible_species(user, club).filter(scientific_name__iexact=name, variety="").first()


def llm_match(text, user=None, club=None, budget=None):
    """Ask the model to pick one species out of a shortlist.

    Returns ``(species_or_None, answered)``.  *answered* is what separates "the model looked and
    says this is not a species" from "the model never ran" -- no provider configured, nothing worth
    asking about, no budget left, or the call failed.  Only the first of those is worth writing to
    the shared cache, and the caller decides on this flag: remembering the others would teach every
    club on the site "not a species" for a name nobody has actually looked at yet.

    The shortlist is built from the database by keyword, so this is mostly a ranking problem for
    the model rather than a recall problem, and an id that isn't in the shortlist is discarded
    rather than trusted.  Where the shortlist *has* failed, the model may name a species instead,
    and that name is resolved against the same table -- see :func:`_species_named`.  Either way
    nothing here can return a species the database doesn't have.

    An **empty** shortlist is the extreme version of that failure, and is asked anyway rather than
    answered "no species" without looking.  "Yellow lab" is the case: FishBase files
    *Labidochromis caeruleus* under "Blue streak hap", so the only keyword left after the ignore
    list is "lab", and whether that shortlists anything at all depends on an ``icontains`` happening
    to hit -- which is luck, not design.  The cost is bounded the same way every other call here is:
    one per name ever, because the answer, including "this is not a species", goes into the cache.

    *budget* is whose daily allowance this call comes out of, defaulting to *user*'s.  The club API
    passes the club's -- see :class:`LLMBudget`.
    """
    provider = get_provider()
    if not provider.is_configured():
        return None, False
    # Deliberately not the site-wide effort; see REASONING_EFFORT.  Left alone when the deployment
    # has switched it off entirely, which is how an operator says "don't send this parameter".
    if provider.reasoning_effort:
        provider.reasoning_effort = REASONING_EFFORT
    words = keywords(text)
    if not words:
        return None, False
    normalized = normalize(text)
    candidates = _shortlist(words, normalized, user=user, club=club)
    # Never put a pairing the site has retired back in front of the model.  A rejection is the one
    # piece of evidence that outlives the cache row it came from (see record_choice), and the model
    # would otherwise answer the same question the same way and have the answer written straight
    # back -- which is exactly the loop the counters exist to break.  Filtered here rather than in
    # _shortlist so that the shortlist stays a pure "what looks relevant" query.
    vetoed = rejected_species_ids(normalized)
    if vetoed:
        candidates = [species for species in candidates if species.pk not in vetoed]
    budget = budget or LLMBudget.for_user(user)
    if not budget.spend():
        logger.info("Species lookup rate limit reached for %s", budget.name)
        return None, False
    listing = "\n".join(f"{species.pk}: {species.label_with_common_name}" for species in candidates)
    # Said out loud rather than left as an empty block, so the model reads it as "the list is
    # empty" rather than as a truncated prompt.
    messages = [{"role": "user", "content": f"Lot name: {text}\n\nCandidates:\n{listing or '(none)'}"}]
    try:
        result = provider.complete_json(_SYSTEM_PROMPT, messages, max_tokens=1000)
    except LLMError:
        logger.info("Species lookup failed for %r", text, exc_info=True)
        _record_usage(user, None, text, "error", success=False)
        return None, False
    raw = result.data.get("id")
    try:
        chosen_pk = int(raw)
    except (TypeError, ValueError):
        # No id.  It may have named a species instead, which is the shortlist admitting it missed.
        named = _species_named(result.data.get("scientific_name"), user=user, club=club)
        if named and named.pk in vetoed:
            return _retired_answer(user, result, text)
        _record_usage(user, result, text, "species" if named else "no_species")
        return named, True
    if chosen_pk in vetoed:
        # It named a retired pairing from memory rather than from the list it was given.
        return _retired_answer(user, result, text)
    # Never trust the id: it has to be one we offered.
    chosen = next((species for species in candidates if species.pk == chosen_pk), None)
    _record_usage(user, result, text, "species" if chosen else "no_species")
    return chosen, True


def remember(text, species, source="llm", user=None):
    """Write an answer to the cache, including the answer "this is not a species".

    *user* is who taught it, when a person did.  Recorded because every row here is served back to
    every club ahead of the token search, so a wrong one is a site-wide problem and needs to be
    traceable to whoever created it -- see :class:`~auctions.models.SpeciesSearchCache` and the
    "names the matcher has already decided" table on the gaps page.
    """
    normalized = normalize(text)
    if not normalized:
        return
    # This table is global and is read ahead of the token search, so a species that is not
    # everybody's yet has no business in it.  The person who added it still gets it offered, by
    # visible_species(); what they don't get is to teach the rest of the site a name using it.
    if species is not None and not species.approved:
        return
    # A pairing the site has already retired is not learned again.  Without this the whole
    # accept/reject mechanism would be a loop: enough people take the species off the lots called
    # "sponge filter", the row is retired, and the next person to save one writes it straight back.
    # A site admin can delete the rejection on the gaps page, which is the way back in.
    if species is not None and is_rejected(normalized, species):
        return
    defaults = {"species": species, "source": source}
    if user is not None and getattr(user, "is_authenticated", False):
        defaults["created_by"] = user
    SpeciesSearchCache.objects.update_or_create(search_text=normalized, defaults=defaults)


def _retired_answer(user, result, text):
    """The model named a species this name has been retired from.  Discard it, remember nothing.

    Deliberately not written down as "not a species": that is a claim about the *name*, and all
    anybody has actually said is that it is not this one species -- see :func:`record_choice`.
    Returning ``answered=False`` is what keeps it out of the cache.
    """
    _record_usage(user, result, text, "no_species")
    return None, False


def is_rejected(normalized, species):
    """True when this name has already been retired from naming this species.

    Takes an *already normalised* name, because the caller has one in hand.
    """
    if species is None or not normalized:
        return False
    return SpeciesNameRejection.objects.filter(search_text=normalized, species=species).exists()


def rejected_species_ids(normalized):
    """The species this name has been retired from naming.  A set, usually empty."""
    if not normalized:
        return set()
    return set(SpeciesNameRejection.objects.filter(search_text=normalized).values_list("species_id", flat=True))


def record_choice(text, species, *, first_save=False):
    """Score what a person did with the answer this lot name was remembered as.

    This is the counterweight to :func:`remember`, and the reason it exists is that the cache is
    written by *sellers*: the bulk-add page remembers the pairing on a row's first save, and the
    row is then served to every club on the site ahead of the token search.  One misclick used to
    become the site's answer forever, and the only way back was a superuser noticing it on the gaps
    page.  Now the same forms that write the answer also report what happened to it.

    *first_save* is what keeps the two counters honest against each other.  An **accept** is only
    counted the first time a lot is saved -- somebody re-saving a lot to fix its price has not
    re-confirmed the species, and counting it would let a busy club vote a wrong answer permanent.
    A **rejection** counts on any save, because that is when it happens: the species was already on
    the lot and somebody took it off or picked a different one.

    Does nothing at all when the name has no remembered answer, which is the common case -- this
    runs on every lot save, so it is one indexed lookup and out.
    """
    normalized = normalize(text)
    if not normalized:
        return
    row = SpeciesSearchCache.objects.filter(search_text=normalized).first()
    if row is None or row.species_id is None:
        # Nothing was remembered, or what was remembered is "this is not a species" -- and a lot
        # saved with no species is agreement with that, not evidence against it.  Nobody is shown
        # a negative answer to disagree with, so there is nothing to score.
        return
    chosen_pk = getattr(species, "pk", species)
    if chosen_pk and str(chosen_pk) == str(row.species_id):
        if first_save:
            # F() rather than a read-modify-write: two sellers saving at once should count twice.
            SpeciesSearchCache.objects.filter(pk=row.pk).update(accepts=F("accepts") + 1)
        return
    SpeciesSearchCache.objects.filter(pk=row.pk).update(rejects=F("rejects") + 1)
    # Re-read rather than refresh_from_db(): two people can be saving lots with this name at the
    # same moment, and the other one may have retired the row already.  A lot save must not fail
    # because of what somebody else's save did to a cache row.
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


def suggest_species(text, user=None, use_llm=True, category=None, club=None, budget=None):
    """The one call the views make: a handful of species for a typed lot name.

    Returns ``(species_list, source)`` where source is one of ``cache``, ``exact``, ``search``,
    ``llm`` or ``none`` -- the caller shows it for debugging and nothing else.  An empty list is
    a legitimate answer, and the UI turns it into "No species".

    *budget* is whose daily model allowance a call would come out of; the caller keeps the object
    and can ask it afterwards whether a call was refused.  See :class:`LLMBudget`.

    *category* is the category the lot form currently shows, when the caller has one.  It only
    ever re-orders candidates that already matched (see :func:`_rank`), never filters them: the
    category is itself a guess from the lot's name, and one guess quietly vetoing the species list
    is exactly the failure this module is written to avoid.

    *club* is the club this lookup is happening for, when the caller has one to hand -- the club
    running the auction, or the club whose API key made the call.  It only ever *widens* the
    answer, by :func:`visible_species`, so a caller with no club to pass loses nothing that was
    already everybody's.
    """
    normalized = normalize(text)
    if not normalized:
        return [], "none"

    # Exact matching runs *before* the cache even though the cache is cheaper.  The cache holds
    # answers that were guessed, and it is shared by every club: one bad row would otherwise
    # outrank the species list itself, forever, for everybody.  Two indexed lookups is a small
    # price for the guarantee that a name the list knows is always answered by the list.
    exact = _rank(exact_matches(text, user=user, club=club), category)
    if exact:
        return exact, "exact"

    cached = SpeciesSearchCache.objects.filter(search_text=normalized).select_related("species").first()
    if cached:
        # Cheap and racy on purpose: this counter exists to show which names are carrying the
        # cache, not to be exact.
        SpeciesSearchCache.objects.filter(pk=cached.pk).update(hits=cached.hits + 1)
        # A cached answer still has to be one this caller may see.  remember() will not write an
        # unapproved species in the first place, so the extra query below only ever runs for a
        # species that was approved when it was remembered and has since been un-approved.  Asking
        # visible_species rather than re-deriving the rule here is what stops the two drifting.
        # Falls *through* rather than answering "no species": the name may well match something in
        # the list, and the whole point of the cache being second is that one row cannot outrank
        # the species table.
        seen = cached.species is None or cached.species.approved
        if not seen:
            seen = visible_species(user, club).filter(pk=cached.species_id).exists()
        if seen:
            return ([cached.species] if cached.species else []), "cache"

    found = search_matches(text, category=category, user=user, club=club)
    if found:
        return found, "search"

    if use_llm:
        chosen, answered = llm_match(text, user=user, club=club, budget=budget)
        # Remember the miss as well as the hit, but only when the model actually answered.
        # "Sponge filter" should cost one call ever, not one per club that sells one -- and a name
        # nobody has looked at yet (no model configured, no budget left, the call failed) must not
        # be written down as "not a species" for every club on the site, forever.
        if answered:
            remember(text, chosen, source="llm")
        if chosen:
            return [chosen], "llm"

    return [], "none"
