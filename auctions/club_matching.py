"""Which club does this belong to?  Name normalisation, initialisms, and the auction backlog.

Two jobs need the same answer, and neither can get it from an exact string compare.

**Auctions filed under no club.**  ``Auction.club`` is set at creation only when the creator has
*both* a declared ``UserData.club`` and a permission in that club (``services.finish_new_auction``),
and most creators have neither -- so around four auctions in five belong to nothing, and every
number in ``club_health`` is computed as though they never happened.  :func:`suggest_clubs`
proposes a club per auction from rows that already exist, ranked by what the signal is actually
worth, and a person approves it on ``/admin-unlinked-auctions/``.  Nothing in this module writes
anything; the writing is :func:`auctions.services.link_auction_to_club`.

**Clubs found by looking outward** (phase 8).  A directory listing and a club already on the site
are the same club under two spellings far more often than they are two different clubs, so anything
that adds clubs has to ask this question before it inserts a row.

**Names are compared three ways, because clubs write themselves down three ways.**  "Greater
Seattle Aquarium Society", "greater-seattle aquarium soc." and "GSAS" all name one club.  So a
comparison folds case and punctuation, drops the words that appear in nearly every club name, and
separately asks whether one side is the initialism of the other.  The generic words come out of the
*comparison* and not out of the name: "Aquarium Society" is most of every name on this site, and
scoring on it would make every club a candidate for every other one.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

#: Words that carry no identity because nearly every club name here has them.  Dropped before two
#: names are compared, never from a name that is stored or shown.
GENERIC_WORDS = frozenset(
    {
        "aquarium",
        "aquariums",
        "aquatic",
        "aquatics",
        "association",
        "club",
        "fish",
        "group",
        "hobbyist",
        "hobbyists",
        "keepers",
        "of",
        "society",
        "the",
        "tropical",
    }
)

#: Below this two names are different clubs.  Set where "greater seattle aquarium society" still
#: matches "greater seattle aquarium soc" and no pair of real club names on this site collides.
NAME_MATCH_THRESHOLD = 0.82

#: An abbreviation shorter than this matches too much ordinary text to be evidence of anything.
MIN_ABBREVIATION = 3

_PUNCTUATION = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Fold case and punctuation and drop the generic words.  ``""`` if nothing is left."""
    folded = _PUNCTUATION.sub(" ", (name or "").lower())
    words = [word for word in _WHITESPACE.split(folded) if word and word not in GENERIC_WORDS]
    return " ".join(words)


def initials(name: str) -> str:
    """``"Greater Seattle Aquarium Society"`` -> ``"gsas"``.

    Built from the *whole* name, generic words included, because that is how clubs build their own
    abbreviations -- the S in GSAS is the Society this module otherwise ignores.
    """
    folded = _PUNCTUATION.sub(" ", (name or "").lower())
    return "".join(word[0] for word in _WHITESPACE.split(folded) if word)


def similarity(left: str, right: str) -> float:
    """How alike two club names are, 0 to 1.

    Three comparisons, best one wins: the normalised names against each other, and each side's
    initialism against the other side written without spaces.  The initialism comparisons are what
    make "GSAS" and "Greater Seattle Aquarium Society" the same club; without them the normalised
    forms share almost no characters and score near zero.
    """
    left_normal, right_normal = normalize(left), normalize(right)
    if not left_normal or not right_normal:
        return 0.0
    if left_normal == right_normal:
        return 1.0
    scores = [SequenceMatcher(None, left_normal, right_normal).ratio()]
    for one, other in ((left, right), (right, left)):
        abbreviation = _PUNCTUATION.sub("", (one or "").lower()).replace(" ", "")
        if len(abbreviation) >= MIN_ABBREVIATION and abbreviation == initials(other):
            scores.append(1.0)
    return max(scores)


def best_match(name: str, clubs, *, threshold: float = NAME_MATCH_THRESHOLD):
    """The club whose name is closest to ``name``, or ``(None, 0.0)`` if none is close enough.

    Ties go to the lower primary key, so the same input always returns the same club: a matcher
    that picks a different row on a second run makes every count computed from it unrepeatable.
    """
    best, best_score = None, 0.0
    for club in sorted(clubs, key=lambda candidate: candidate.pk):
        score = max(similarity(name, club.name), similarity(name, club.abbreviation or ""))
        if score > best_score:
            best, best_score = club, score
    if best_score < threshold:
        return None, 0.0
    return best, best_score


@dataclass(frozen=True)
class Suggestion:
    """One proposed ``Auction`` -> ``Club`` link, and why anybody should believe it."""

    club: object
    reason: str
    #: ``high`` means the organizer themselves said so, or has already done it for another auction.
    #: ``low`` means two strings looked alike.  Shown on the page, because the second kind wants
    #: reading before it is approved and the first kind does not.
    confidence: str


def _club_name_in_title(auction, clubs):
    """A club whose name or abbreviation is in this auction's title.

    An abbreviation has to appear as a whole word: "NEC" is in "connect", and an auction matched
    that way would be filed under a club that has never heard of it.
    """
    title = auction.title or ""
    for club in sorted(clubs, key=lambda candidate: candidate.pk):
        abbreviation = (club.abbreviation or "").strip()
        if len(abbreviation) >= MIN_ABBREVIATION and re.search(rf"\b{re.escape(abbreviation)}\b", title, re.IGNORECASE):
            return club, f"{abbreviation} is in the auction name"
    club, score = best_match(title, clubs)
    if club:
        return club, f"auction name looks like {club.name} ({score:.0%})"
    return None, ""


def suggest_clubs(auctions, clubs=None) -> dict[int, Suggestion]:
    """Propose a club for each of ``auctions``, keyed by auction pk.  Read-only.

    Four signals, and the order between them is the point of the function -- they disagree, and
    what they disagree about is how much anybody should trust them:

    1. **The same organizer's other auctions are already linked.**  Somebody approved that link
       once.  Observed, and about a person who has already answered this question.
    2. **The organizer's own club affiliation** (``UserData.club``).  They typed it, which is
       better than a guess and worse than a decision somebody checked -- it can be years stale.
    3. **The organizer belongs to exactly one club.**  Only when there is exactly one: a person in
       three clubs has told us nothing about which one this auction is for.
    4. **The auction's name.**  Two strings looking alike, and the only signal here that involves
       no human statement at all.  Marked ``low`` and meant to be read before it is approved.

    Three bulk queries whatever the size of ``auctions``, because the backlog is in the hundreds
    and a per-auction query would make the page that shows it the slow thing on the site.
    """
    from .models import Auction, ClubMember, UserData

    auctions = list(auctions)
    if not auctions:
        return {}
    if clubs is None:
        from .models import Club

        clubs = list(Club.objects.all())
    clubs = list(clubs)
    by_pk = {club.pk: club for club in clubs}
    creator_ids = {auction.created_by_id for auction in auctions if auction.created_by_id}

    # 1. What this organizer's already-linked auctions were filed under, most common first.
    linked: dict[int, Counter] = {}
    for creator_id, club_id in Auction.objects.filter(
        created_by_id__in=creator_ids, club__isnull=False, is_deleted=False
    ).values_list("created_by_id", "club_id"):
        linked.setdefault(creator_id, Counter())[club_id] += 1

    # 2. The affiliation on their account.
    affiliation = dict(
        UserData.objects.filter(user_id__in=creator_ids, club__isnull=False).values_list("user_id", "club_id")
    )

    # 3. Club memberships, but only useful where there is exactly one.
    memberships: dict[int, set] = {}
    for user_id, club_id in ClubMember.objects.filter(
        user_id__in=creator_ids, is_deleted=False, club__isnull=False
    ).values_list("user_id", "club_id"):
        memberships.setdefault(user_id, set()).add(club_id)

    suggestions: dict[int, Suggestion] = {}
    for auction in auctions:
        creator_id = auction.created_by_id
        club_id = None
        reason, confidence = "", "high"
        if creator_id and linked.get(creator_id):
            club_id, count = linked[creator_id].most_common(1)[0]
            reason = f"this organizer's other auction{'s' if count > 1 else ''} ({count}) are filed here"
        elif creator_id and affiliation.get(creator_id):
            club_id = affiliation[creator_id]
            reason = "the club on the organizer's account"
            confidence = "medium"
        elif creator_id and len(memberships.get(creator_id, ())) == 1:
            club_id = next(iter(memberships[creator_id]))
            reason = "the only club the organizer belongs to"
            confidence = "medium"
        if club_id is None or club_id not in by_pk:
            club, reason = _club_name_in_title(auction, clubs)
            confidence = "low"
            if not club:
                continue
        else:
            club = by_pk[club_id]
        suggestions[auction.pk] = Suggestion(club=club, reason=reason, confidence=confidence)
    return suggestions
