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
4. **Language model** -- only when the first three found nothing.  Two rounds, and the cheap one
   comes first: it is shown the lot name *alone* and asked what organism it names, because that
   is a question about the hobby rather than about our tables, and it is where a misspelling or a
   trade abbreviation gets read through ("red luwigia" is *Ludwigia*).  The name it gives back is
   resolved against the species list here.  The second round is the older one: a shortlist we
   built out of our own tables, to pick from -- built from round one's corrected spelling as well
   as from what was typed, because a shortlist is equality and substring matching and a
   misspelling reaches neither.  It runs whenever round one did not land on a species, *including*
   when round one says the lot is not an organism at all: that verdict is permanent and site-wide,
   so it takes two calls to write one.  Either way nothing is invented -- both
   answers are looked up in the same table the lot form validates against.

   Both rounds are written to the cache, including the answer "we know what this is and the list
   does not have it".  See :class:`~auctions.models.SpeciesSearchCache.scientific_name`: that row
   heals itself the day the species is imported, where "not a species" never could.

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

#: How long a word has to be before matching a specific epithet counts as evidence.  Six, because
#: the epithets this layer exists for are long -- *caudopunctatus*, *cacatuoides*, *ramirezi* --
#: and the short ones are traps: "geo", the hobby's abbreviation for *Geophagus*, is also the
#: epithet of *Hoplolatilus geo*, a marine tilefish, and a lot called "geo alto sinu" was shortlisted
#: with it and came back as it.  search_matches has the same rule in a stricter form: it will answer
#: a bare epithet only when the whole lot name is that one word.
MIN_EPITHET_LETTERS = 6

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

#: The first round, and the cheap one: no candidate list, just the lot name.  It is a different
#: question from the one below and the difference is the whole point -- handed forty alphabetical
#: Corydoras first, the model is answering multiple choice off a menu that mostly looks wrong, and
#: "none of the above" is the natural reply; asked cold, it is answering from what it knows about
#: the hobby, which is where a typo or an abbreviation gets read through.  The examples are real
#: lot names this module used to write off as "not a species".
#:
#: What comes back is a *claim*, not an answer: every name is looked up in our own table before it
#: can reach a lot.  See :func:`_resolve_identification`.
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

# Written defensively because the failure mode is not "no answer", it is a *confident wrong*
# answer: with a shortlist in front of it a model will happily decide "sponge filter" is a Ball
# sponge, "Bolivian ram" is a Banded gourami, and "cherry shrimp" is an Amano shrimp. Each of
# those then gets printed on a label and counted for breeder points. Hence the worked negative
# examples and the flat instruction that null is the normal answer.
#: The second round: pick one of ours.  Reached only when :data:`_IDENTIFY_PROMPT` could not name
#: the lot, or named something the species list does not hold -- which is usually a synonym or a
#: strain we file under a different name, and is exactly what a list in front of it is good for.
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

    Spent at the moment a call is about to be made -- in :func:`_ready_to_ask`, past the exact,
    cache and search steps -- and never merely because a request arrived.  That distinction is the
    whole point of the class: a caller doing ten thousand lookups a day that the database answers
    for free has spent nothing, and must not be told it is out of budget.

    A unit is one **call**, not one lookup, and a lookup the model cannot place on sight takes two
    of them -- :func:`identify` and then :func:`llm_match`.  Counting calls is what keeps the
    allowance about money; counting lookups would have made a second round free.

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


def species_already_named(genus, epithet, variety="", user=None, club=None, is_hybrid=False):
    """The species this name already belongs to, if the caller can see one.  None otherwise.

    Scoped to what the caller may see, for both halves of the reason :func:`visible_species`
    exists: pointing somebody at a row they cannot open is no help, and answering "that already
    exists" when what exists is another club's unapproved row leaks it.

    A hybrid has no genus and no epithet, so the strain name carries the whole comparison -- and
    the flag has to be part of the query, or *Neocaridina davidi* 'Blue Dream' and a hypothetical
    cross of the same name would be told they are each other.
    """
    if is_hybrid:
        return visible_species(user, club).filter(is_hybrid=True, variety__iexact=variety or "").first()
    return (
        visible_species(user, club)
        .filter(genus__iexact=genus, species__iexact=epithet, variety__iexact=variety or "", is_hybrid=False)
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


def _shortlist(words, normalized, user=None, club=None, reading=""):
    """Species worth putting in front of the model, for the keywords in a lot name.

    A wider net than :func:`search_matches` casts -- the model can discard noise, so recall
    matters more than precision here -- but the net has to be *ordered*.  The obvious version, one
    ``icontains`` OR with a ``LIMIT``, is an unordered slice: for "german blue ram" it returns
    forty species whose names merely contain "ram" (*Abramis*, *Abramites*, ...) and leaves out the
    one FishBase actually calls "Ram", so the model is asked to choose and correctly answers null.
    Recall failures here look exactly like the model being unhelpful.

    So the layers run best-evidence-first and stop once the list is full:

    1. a common name that *is* one of the keywords, or one of the phrases in the lot name
    2. a word that *is* a specific epithet.  Half the fish in this hobby are sold as an
       abbreviated genus plus a full epithet -- "neolamp caudopunctatus", "apisto cacatuoides" --
       and the epithet is the discriminating half: it names one fish where the genus names fifty.
       This layer used not to exist, and "neolamp caudopunctatus red fin" was handed an *empty*
       shortlist while *Neolamprologus caudopunctatus* sat in the table.
    3. a genus that is one of the keywords, and the genus siblings of anything found so far --
       "Ram" finds *Mikrogeophagus ramirezi*, and its sibling is the Bolivian ram
    4. anything whose common name merely contains a keyword, to fill the remaining space

    ...and then the nominal species of every cultivar that made the list, which is not a layer:
    see below.

    *reading* is the same lot name with its spelling corrected, normalised, when round one handed
    one back.  Every layer here is an equality or a substring against a column, so a misspelling
    reaches none of them -- which is the whole reason :func:`identify` runs first, and the reason
    its correction has to arrive here rather than being dropped on the way.  It only ever *adds*
    phrases: the typed name is still searched on, because a "correction" is itself a guess.
    """
    candidates = {}

    def add(queryset, limit=LLM_SHORTLIST_SIZE):
        """Take rows from *queryset* until the shortlist is full.  Earlier layers keep their places."""
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

    # Nominal species only, for the reason rule 1 of search_matches gives: a cultivar carries its
    # parent's epithet, so "Neocaridina davidi" would otherwise spend the shortlist on thirteen
    # colour strains nobody asked for.  Strains are reached by their own names, in the layer above.
    #
    # Long words only, and MIN_EPITHET_LETTERS says why: a short word matching an epithet is a
    # coincidence rather than evidence, and a coincidence in the shortlist is a wrong species with
    # a plausible-looking scientific name next to it.
    epithets = {word for word in words if len(word) >= MIN_EPITHET_LETTERS}
    if epithets:
        add(_trade_first(visible_species(user, club).filter(species__in=epithets, parent__isnull=True)))

    genera = {word.capitalize() for word in words} | {species.genus for species in candidates.values()}
    add(_trade_first(visible_species(user, club).filter(genus__in=genera)))

    name_q = Q()
    for word in words:
        name_q |= Q(name_normalized__icontains=word)
    add(_trade_first(visible_common_names(user, club).filter(name_q), "species__").select_related("species"))

    # The nominal species of every cultivar on the list.  A cultivar is only ever the answer when
    # the lot names that exact strain, so the prompt's fallback for one we don't stock -- "pick
    # the plain species it is a strain of" -- can only be taken when that species is in front of
    # it too.  "Male calico bristlenose" reached three *Ancistrus cirrhosus* colour strains and,
    # depending on where the cap happened to fall, not the fish itself, which left the model
    # choosing between three wrong strains and null.  Allowed past the cap because a parent is not
    # another candidate competing for room; it is half of one that is already there.
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
    """What the model made of one lot name.  The return of both rounds.

    *species* is the only field that can reach a lot, and it is always a row out of our own table.

    *answered* separates "the model looked, and this is what it says" from "the model never ran" --
    no provider configured, nothing worth asking about, no budget left, the call failed.  Only the
    first is worth writing to a cache every club reads: remembering the others would teach the
    whole site an answer to a question nobody has actually asked yet.

    *scientific_name* is what it says the lot is, whether or not we stock it.  It is the field
    that stops a correct identification being recorded as a wrong one -- see
    :attr:`~auctions.models.SpeciesSearchCache.is_a_gap`.

    *corrected_name* is the lot's hobby name with the spelling fixed, and it is worth having even
    when the model would not commit to a species: "red luwigia" is a genus and a colour, so there
    is no binomial to give, but "red ludwigia" is something :func:`search_matches` can answer.
    Splitting the work that way is the point -- the model is good at spelling and we are good at
    the species list, and neither is much good at the other's half.  It is also what round two
    builds its shortlist from, for the same reason: see :func:`llm_match`.
    """

    species: Species | None = None
    answered: bool = False
    scientific_name: str = ""
    corrected_name: str = ""

    @property
    def settled(self):
        """True when a second round has nothing left to add, which means: we have a species.

        Nothing else ends it, and "not an organism" least of all.  A remembered negative is the
        one answer on this site that nothing walks back on its own -- it is served to every club
        ahead of the token search, and a lot saved with no species is *agreement* with it, so the
        ordinary accept/reject machinery never fires.  One call is not enough to earn that.  See
        :func:`suggest_species`, which asks the shortlist round as well and writes the negative
        only if the two agree.

        A lot the model could not name, or named something we don't stock, is worth showing our
        own list to for the older reason: the list is where a synonym, a strain name or a club's
        own word for a fish lives.
        """
        return self.species is not None


#: A binomial has two words, or three for a trinomial or an open nomenclature "Genus sp. cf".  The
#: point of counting is to tell a *name* from a sentence: asked for one the model may reply "some
#: kind of small brown fish", and that is not something to look up, print on the gaps page, or
#: keep in a column called scientific_name.
def _looks_like_a_binomial(name):
    """True when *name* is shaped like a scientific name rather than like a description."""
    words = (name or "").split()
    return 2 <= len(words) <= 3 and all(re.fullmatch(r"[A-Za-z.'-]+", word) for word in words)


def _resolve_identification(scientific_name, common_name, user=None, club=None):
    """The species behind an identification the model made, if the list holds it.  None otherwise.

    Two names arrive because the hobby uses two and they resolve differently.  The binomial is the
    precise claim and is looked up as one.  The common name is what recovers everything the
    binomial cannot say: a cultivar has no scientific name of its own -- *Neocaridina davidi*
    "Blue Dream" shares its parent's -- so a model correctly answering "Neocaridina davidi" for
    "blue dream shrimp" would land on the plain species and lose the strain the seller named.  It
    is also the only route to a name a club added here, which is in our table and in nothing the
    model was trained on.

    Deliberately asymmetric.  A common name is taken as *refining* a binomial we already resolved
    (a strain of that same species, never a different fish), and on its own only when it is
    unambiguous -- several poeciliids answer to "guppy", and a name carried by five species is not
    an identification.
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


def _ready_to_ask(text, user, budget):
    """The three things both rounds check before spending anything: provider, words, budget.

    Returns ``(provider, budget)``, or ``(None, budget)`` when there is no call to make.  Budget is
    spent here, at the moment a call is about to happen and never merely because a lookup arrived
    -- see :class:`LLMBudget`.
    """
    provider = get_provider()
    if not provider.is_configured():
        return None, budget
    # Deliberately not the site-wide effort; see REASONING_EFFORT.  Left alone when the deployment
    # has switched it off entirely, which is how an operator says "don't send this parameter".
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
    """Round one: ask what the lot name names, without showing the model our list.

    The cheap half of the model step and the one that does most of the work.  The lot name goes on
    its own -- no candidates, a prompt of a few dozen tokens against the several hundred a
    shortlist costs -- and what comes back is a claim about the hobby, which is then looked up
    here.  Nothing about the answer depends on our tables, and that is the point three times over:

    * it is where a **misspelling** gets read through.  "Red luwigia" shortlists nothing at all,
      because every layer of :func:`_shortlist` is an equality or a substring against a column, and
      "luwigia" is not a substring of "ludwigia".  Asked cold, the model simply reads it.
    * the answer is **not list-shaped**, so it survives the list changing.  A binomial we don't
      stock is written to the cache as itself rather than as "not a species", and the row starts
      answering the day the species is imported -- see :func:`suggest_species`.
    * asking it cold is a **different question** from picking off a menu.  Handed forty
      alphabetical *Corydoras* and a lot called "orange venezuelan corydoras", the model has to
      decide none of them is right, and it did; asked what the lot is, it says *Corydoras aeneus*.

    Nothing here can invent a species: :func:`_resolve_identification` looks every name up in the
    same table the lot form validates against, so an identification we don't stock is still no
    species on the lot -- just a recorded gap instead of a verdict.
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
        # Answered, but not settled, and deliberately: this is the verdict that becomes permanent
        # and site-wide, so it goes to the shortlist round for a second opinion before it is
        # written down.  Round one is the better instrument for the question -- a menu of near
        # misses is what talks a model into calling a sponge filter a Ball sponge -- but "better"
        # is not the standard for an answer nothing can take back.  See Identification.settled.
        _record_usage(user, result, text, "not_an_organism")
        return Identification(None, True, "")
    scientific_name = str(result.data.get("scientific_name") or "").strip()
    common_name = str(result.data.get("common_name") or "").strip()
    if kind != "species" or not _looks_like_a_binomial(scientific_name):
        # "Unknown", or a sentence where a name should be.  Not an answer about the *name*, so
        # nothing is written down -- but a corrected spelling is still worth carrying out of here
        # even when the model would not name a species: "red luwigia" has no binomial to give and
        # "red ludwigia" is a question the species list can answer on its own.
        _record_usage(user, result, text, "unknown")
        return Identification(corrected_name=common_name)
    species = _resolve_identification(scientific_name, common_name, user, club)
    if species is not None and is_rejected(normalize(text), species):
        # Named a pairing the site has already retired.  Same reasoning as the shortlist round:
        # this is the loop record_choice exists to break, so it is dropped rather than remembered.
        return _retired_answer(user, result, text)
    _record_usage(user, result, text, "species" if species else "gap")
    # The binomial is carried only when nothing was resolved.  It is the *gap* column, and a row
    # naming both a species and a species we don't stock says two different things about one lot:
    # the assassin snail resolves through its common name while the model calls it "Clea helena",
    # and recording that pairing would leave the cache asserting a fish it had just identified.
    return Identification(species, True, "" if species else scientific_name, common_name)


def llm_match(text, user=None, club=None, budget=None, reading=""):
    """Round two: ask the model to pick one species out of a shortlist we built.

    Reached only when :func:`identify` could not name the lot, or named something the species list
    does not hold.  That second case is what this round is *for*: the model said "Clea helena" and
    we file the assassin snail under *Anentome helena*, or it said "Neocaridina davidi" and the
    strain the seller named is a row of its own.  A synonym, a strain name and a club's own word
    for a fish are all things that are in our table and not in the model's head, and the only way
    to use them is to put them in front of it.

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
    one per name ever, because the answer goes into the cache either way.

    *reading* is round one's corrected spelling of the lot name, when it gave one.  Without it
    this round is handed the misspelling that the step before just established the database cannot
    match -- "mudflwoer" shortlists nothing at all, so the call is spent asking the model to choose
    from an empty list, which is the one question it cannot answer.  The shortlist is built from
    both spellings; everything that has to stay keyed on what the seller actually typed -- the
    cache row, the rejection veto, the recorded query -- still is.

    *budget* is whose daily allowance this call comes out of, defaulting to *user*'s.  The club API
    passes the club's -- see :class:`LLMBudget`.
    """
    # Preflight first: an unconfigured provider or an exhausted budget must not cost the four
    # queries a shortlist takes.  Spending the budget before building the list is safe because
    # nothing between here and the call can decide not to make it.
    provider, budget = _ready_to_ask(text, user, budget)
    if provider is None:
        return Identification()
    normalized = normalize(text)
    # Longest first is the order _shortlist's capped layers consume, so the merged list is re-sorted
    # rather than concatenated: the discriminating word may well be the one round one corrected.
    reading = normalize(reading)
    words = keywords(text)
    if reading and reading != normalized:
        words = sorted(dict.fromkeys(words + keywords(reading)), key=len, reverse=True)
    else:
        reading = ""
    candidates = _shortlist(words, normalized, user=user, club=club, reading=reading)
    # Never put a pairing the site has retired back in front of the model.  A rejection is the one
    # piece of evidence that outlives the cache row it came from (see record_choice), and the model
    # would otherwise answer the same question the same way and have the answer written straight
    # back -- which is exactly the loop the counters exist to break.  Filtered here rather than in
    # _shortlist so that the shortlist stays a pure "what looks relevant" query.
    vetoed = rejected_species_ids(normalized)
    if vetoed:
        candidates = [species for species in candidates if species.pk not in vetoed]
    listing = "\n".join(f"{species.pk}: {species.label_with_common_name}" for species in candidates)
    # The reading is shown as well as searched on, so the model can see why a candidate is on the
    # list at all: without it "mudflwoer" and *Micranthemum umbrosum* look unrelated on the page.
    asked = f"Lot name: {text}" + (f"\nRead as: {reading}" if reading else "")
    # "(none)" said out loud rather than left as an empty block, so the model reads it as "the list
    # is empty" rather than as a truncated prompt.
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
        # No id.  It may have named a species instead, which is the shortlist admitting it missed.
        spoken = str(result.data.get("scientific_name") or "").strip()
        named = _species_named(spoken, user=user, club=club)
        if named and named.pk in vetoed:
            return _retired_answer(user, result, text)
        # The name is kept even when we can't resolve it: a species we don't stock is a gap in the
        # list, and writing that down as "not a species" is how adding the fish later stopped
        # fixing anything.  Only when it is shaped like a name -- "some kind of small brown fish"
        # is a shrug, not a binomial.
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
    """Write an answer to the cache, including the answer "this is not a species".

    *scientific_name* is what the name was identified as when the species list could not supply
    it.  A row with that filled in and no species is a **gap** rather than a verdict -- the
    difference between "sponge filter is not a species" and "we have never stocked *Yssichromis
    piceatus*" -- and it is what lets the row start answering when the species is imported.  See
    :attr:`~auctions.models.SpeciesSearchCache.is_a_gap` and :func:`suggest_species`.

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
    defaults = {"species": species, "source": source, "scientific_name": (scientific_name or "")[:120]}
    if user is not None and getattr(user, "is_authenticated", False):
        defaults["created_by"] = user
    existing = SpeciesSearchCache.objects.filter(search_text=normalized).first()
    if existing is not None and existing.species_id != (species.pk if species is not None else None):
        # The counters score *an answer*, not a name, and this is a different answer.  Carrying
        # them over would leave a row that had collected two rejections one rejection away from
        # being retired for something it had never been asked about -- and retiring writes a
        # SpeciesNameRejection, which outlives the row.  See record_choice.
        defaults["accepts"] = 0
        defaults["rejects"] = 0
    SpeciesSearchCache.objects.update_or_create(search_text=normalized, defaults=defaults)


def _retired_answer(user, result, text):
    """The model named a species this name has been retired from.  Discard it, remember nothing.

    Deliberately not written down as "not a species": that is a claim about the *name*, and all
    anybody has actually said is that it is not this one species -- see :func:`record_choice`.
    Returning ``answered=False`` is what keeps it out of the cache.
    """
    _record_usage(user, result, text, "no_species")
    return Identification()


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


def record_choice(text, species, *, first_save=False, changed=False, user=None):
    """Score what a person did with the answer this lot name was remembered as.

    This is the counterweight to :func:`remember`, and the reason it exists is that the cache is
    written by *sellers*: the bulk-add page remembers the pairing on a row's first save, and the
    row is then served to every club on the site ahead of the token search.  One misclick used to
    become the site's answer forever, and the only way back was a superuser noticing it on the gaps
    page.  Now the same forms that write the answer also report what happened to it.

    *first_save* and *changed* are what keep the two counters honest against each other, and what
    makes both of them count **lots** rather than saves.  An **accept** is only counted the first
    time a lot is saved -- somebody re-saving a lot to fix its price has not re-confirmed the
    species, and counting it would let a busy club vote a wrong answer permanent.  A **rejection**
    is counted on the save that created the lot, or on a later save that actually moved the
    species; re-saving a lot whose species was already cleared is the same non-event, and counting
    it once per save let one seller editing one lot three times retire an answer by themselves.

    A remembered **"not a species"** is scored here too, and it is the one that needed it most:
    it is served to every club ahead of the token search, a lot saved with no species is agreement
    with it rather than evidence against it, and nobody is ever *shown* a negative answer to
    disagree with -- so it collected nothing, and the only way back was a superuser deleting the
    row on the gaps page.  Somebody deliberately putting a species on the lot is the disagreement,
    said out loud, and :attr:`~auctions.models.SpeciesSearchCache.MIN_REJECTS_TO_RETIRE` of them
    on different lots replace the answer with what those people actually picked.  Not the first
    one, because one seller's pick becoming the site's answer is the misclick this function exists
    to prevent; three of them are stronger evidence than the two model calls that wrote the row,
    and unlike a negative the species they leave behind is something the counters can undo.

    Does nothing at all when the name has no remembered answer, which is the common case -- this
    runs on every lot save, so it is one indexed lookup and out.
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
            # F() rather than a read-modify-write: two sellers saving at once should count twice.
            SpeciesSearchCache.objects.filter(pk=row.pk).update(accepts=F("accepts") + 1)
        return
    if not (first_save or changed):
        # The species is not what this name is remembered as, but it was not this save that made
        # that true -- somebody is editing a price on a lot they fixed a week ago.  Already counted.
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


def _reject_a_negative(row, text, species, chosen_pk, *, first_save, changed, user):
    """Score somebody putting a species on a lot the cache says is not one.  See :func:`record_choice`.

    Counted on the save that created the lot or on a later one that actually moved the species,
    for the reason the positive half counts the same two: re-saving a lot to fix its price is not
    a second person disagreeing.  A lot saved with no species is not counted at all -- most lots
    have no species because nobody filled the field in, so agreement here measures nothing.
    """
    if not (chosen_pk and (first_save or changed)):
        return
    SpeciesSearchCache.objects.filter(pk=row.pk).update(rejects=F("rejects") + 1)
    # Re-read rather than refresh_from_db(), for the reason the positive half does: somebody
    # else's save may have got here first, and a lot save must not fail because of it.
    row = SpeciesSearchCache.objects.filter(pk=row.pk).first()
    if row and row.is_discredited:
        logger.info(
            "Replacing remembered %r -> no species with %s after %s pick(s)",
            row.search_text,
            species,
            row.rejects,
        )
        # remember() carries the guards this must not go round: an unapproved species stays out of
        # a table every club reads, and a retired pairing is not learned again.  It also resets the
        # counters, because the votes above were about the answer being replaced.
        remember(text, species, source="user", user=user)


def _is_somebody_elses_name(normalized, species, user=None, club=None):
    """True when a cached answer is really one club's private word for that fish.

    A :class:`SpeciesSearchCache` row is served to every club, which is right for what the table
    mostly holds: the model working out that "blue dream shrimp" is a *Neocaridina* strain is a
    fact about the hobby, not about whoever paid for the call.  It is wrong when the text is a
    **name** somebody added here and it was scoped -- "yellow lab" belongs to the club that taught
    it until a superuser approves it, and :func:`visible_common_names` is careful about exactly
    that.  One cached row must not be the way round it: the cache is read before the token search
    and answers on its own, so a leak here is a leak everywhere, forever, for everybody.

    Two indexed lookups, and only when a row would otherwise answer.  The first one ends it for
    the common case: nothing in ``SpeciesCommonName`` claims the text at all, because the row is
    an inference rather than a name, and an inference is nobody's property.
    """
    if species is None:
        return False
    claimed = SpeciesCommonName.objects.filter(name_normalized=normalized, species=species)
    if not claimed.exists():
        return False
    return not visible_common_names(user, club).filter(name_normalized=normalized, species=species).exists()


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
        remembered = cached.species
        # A gap row healing itself.  The row says what this lot is and says the list did not have
        # it; the list has since been imported into a dozen times, so the question is asked again
        # -- one indexed lookup, no model call.  This is the whole reason the column exists: an
        # identification we couldn't supply used to be written down as "not a species", where
        # adding the fish afterwards changed nothing and only a superuser deleting the row by hand
        # could undo it.
        healed = False
        if remembered is None and cached.scientific_name:
            remembered = _species_named(cached.scientific_name, user=user, club=club)
            if remembered is not None and is_rejected(normalized, remembered):
                remembered = None
            healed = remembered is not None
        # A cached answer still has to be one this caller may see.  remember() will not write an
        # unapproved species in the first place, so the extra query below only ever runs for a
        # species that was approved when it was remembered and has since been un-approved.  Asking
        # visible_species rather than re-deriving the rule here is what stops the two drifting.
        # Falls *through* rather than answering "no species": the name may well match something in
        # the list, and the whole point of the cache being second is that one row cannot outrank
        # the species table.
        seen = remembered is None or remembered.approved
        if not seen:
            seen = visible_species(user, club).filter(pk=remembered.pk).exists()
        # ...and the *name* has to be one they may see, not just the species.  See
        # _is_somebody_elses_name: without this, one cached row hands a club-scoped common name
        # to every club, which is the one thing the name table itself refuses to do.
        if seen and _is_somebody_elses_name(normalized, remembered, user=user, club=club):
            seen = False
        if seen:
            # Written back only when it is everybody's.  This row is served to every club, so
            # healing it with a species that is visible to *this* caller alone would hand one
            # club's private row to the whole site -- the same leak _is_somebody_elses_name is
            # there to stop.  An unapproved one is still answered with, just not written down.
            if healed and remembered.approved:
                SpeciesSearchCache.objects.filter(pk=cached.pk).update(species=remembered)
            return ([remembered] if remembered else []), "cache"

    found = search_matches(text, category=category, user=user, club=club)
    if found:
        return found, "search"

    if use_llm:
        # Round one: what does this name mean?  Cheap, list-free, and where a misspelling or a
        # trade abbreviation gets read through.  See identify().
        answer = identify(text, user=user, club=club, budget=budget)
        if not answer.settled and answer.corrected_name and normalize(answer.corrected_name) != normalized:
            # The model fixed the spelling but would not commit to a species -- "red luwigia" is a
            # genus and a colour, and there is no binomial that means it.  So the corrected name
            # goes back through the same two steps the typed name just failed, which is where the
            # seven *Ludwigia* come from: the model did the spelling, the species list does the
            # identifying, and neither had to do the other's half.
            #
            # Deliberately not remembered.  What comes back here is often several species -- "here
            # are the seven Ludwigia" is a picker rather than an answer -- and the single-species
            # case cannot be written to a table every club reads: exact_matches was scoped to this
            # caller, so the row that answered may be one club's own name for the fish, and the
            # read guard on the cache checks the name that was *typed*, which is the misspelling
            # and belongs to nobody.  Costing one short call per lookup is the cheaper mistake; the
            # lot form writes a row of its own the moment a seller picks one of these and saves.
            corrected = _rank(exact_matches(answer.corrected_name, user=user, club=club), category) or search_matches(
                answer.corrected_name, category=category, user=user, club=club
            )
            if corrected:
                return corrected, "llm"
        # Whether the shortlist round ran *and answered*, which is the only thing that can make a
        # bare "not a species" safe to write down.  See the remember() call below.
        confirmed = False
        if not answer.settled:
            # It could not name the lot, named something we don't stock, or said the lot is not an
            # organism at all.  Round two shows it what we *do* have, which is where a synonym, a
            # strain name or a club's own word for a fish lives -- and, for that last case, is the
            # second opinion rather than a lookup: see Identification.settled.  A binomial from
            # round one is kept whatever round two decides: "this is Yssichromis piceatus and we
            # don't have it" stays true even when a list that does not contain the fish fails to
            # produce it.
            second = llm_match(text, user=user, club=club, budget=budget, reading=answer.corrected_name)
            confirmed = second.answered
            answer = Identification(
                second.species,
                second.answered or answer.answered,
                # Round one's binomial survives round two failing to place the lot, and only that:
                # once round two has a species, "and also it is a fish we don't stock" is no longer
                # true of this row.  See identify().
                second.scientific_name or ("" if second.species else answer.scientific_name),
            )
        if answer.species is not None and _is_somebody_elses_name(normalized, answer.species, user=user, club=club):
            # The lot name is one club's private word for that fish, and this caller is not in
            # that club.  The species itself is everybody's, which is exactly what makes this
            # worth checking here: the model is answering out of its own head rather than out of
            # visible_common_names, so it will happily identify "yellow lab" for a club that was
            # never taught the name -- past a name table that refuses to, and then into a cache
            # every club reads.  Same guard the cache branch above applies, for the same reason.
            #
            # Nothing is remembered.  "No species" is the right answer for this caller and the
            # wrong one for the club that owns the name, and one shared row cannot say both.
            answer = Identification()
        # Remember the miss as well as the hit, but only when the model actually answered.  A
        # name nobody has looked at yet (no model configured, no budget left, the call failed)
        # must not be written down as "not a species" for every club on the site, forever.
        #
        # A **bare** negative -- no species and no binomial -- needs both rounds to have answered,
        # because it is the only thing written here that nothing walks back: record_choice now
        # scores one, but only once somebody has picked a species for the name, and nobody is
        # shown a negative to disagree with in the first place.  A species and a gap row are both
        # written on one round, because both are recoverable: sellers outvote a wrong species, and
        # a gap heals itself the day the fish is imported.  The cost is two short calls for a
        # sponge filter, once, for the whole site.
        if answer.answered and (answer.species or answer.scientific_name or confirmed):
            remember(text, answer.species, source="llm", scientific_name=answer.scientific_name)
        if answer.species:
            return [answer.species], "llm"

    return [], "none"
