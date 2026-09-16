"""Which club does this belong to? Name normalisation, initialisms, and the auction backlog.

**Auctions filed under no club.** ``Auction.club`` is only set at creation when the creator has both
a declared ``UserData.club`` and a permission in it, so around four auctions in five belong to
nothing and every ``club_health`` number ignores them. :func:`suggest_clubs` proposes a club per
auction, ranked by what each signal is worth, for a person to approve on
``/admin-unlinked-auctions/``. Nothing here writes; that is
:func:`auctions.services.link_auction_to_club`.

**Clubs found by looking outward.** A directory listing and a club already here are usually the same
club spelled two ways, so anything adding clubs asks this first.

Names are compared three ways: folded case and punctuation, with the words in nearly every club name
dropped, plus each side's initialism. The generic words come out of the comparison, never the stored
name.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

#: Words carrying no identity, dropped before comparison but never from a stored or shown name.
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

#: Below this, two names are different clubs. Set where "…aquarium society" still matches
#: "…aquarium soc" and no pair of real club names here collides.
NAME_MATCH_THRESHOLD = 0.82

#: A shorter abbreviation matches too much ordinary text to be evidence.
MIN_ABBREVIATION = 3

_PUNCTUATION = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Fold case and punctuation and drop the generic words; ``""`` if nothing is left."""
    folded = _PUNCTUATION.sub(" ", (name or "").lower())
    words = [word for word in _WHITESPACE.split(folded) if word and word not in GENERIC_WORDS]
    return " ".join(words)


def initials(name: str) -> str:
    """``"Greater Seattle Aquarium Society"`` -> ``"gsas"``, built from the whole name including generic
    words, because that is how clubs build their own abbreviations.
    """
    folded = _PUNCTUATION.sub(" ", (name or "").lower())
    return "".join(word[0] for word in _WHITESPACE.split(folded) if word)


def derived_abbreviation(name: str) -> str:
    """The abbreviation ``Club.save`` derives for a club with none: ``"MAS"``.

    Not :func:`initials`: ``Club.save`` splits on whitespace alone, so "Mid-Atlantic Aquarium Society"
    gives ``MAS`` while :func:`initials` reads ``maas``. ``Club.save`` calls this, so there is one rule
    and :func:`is_hand_written` can tell derived from chosen.
    """
    return "".join(word[0].upper() for word in (name or "").split() if word)


def similarity(left: str, right: str) -> float:
    """How alike two club names are, 0 to 1: the normalised names, and each side's initialism against the
    other written without spaces, best of the three. Without the initialisms "GSAS" scores near zero.
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


def is_hand_written(club) -> bool:
    """Whether a club's abbreviation was typed by a person rather than derived from its name.

    ``Club.save`` auto-fills it, and an auto-filled abbreviation is the name again in three letters.
    Treating it as evidence makes "Boston" and "Bristol Aquarium Society" one club: both derive ``BAS``,
    which :func:`similarity` scores 1.0. Milwaukee, Minnesota and Missouri are all ``MAS`` too.
    """
    name = getattr(club, "name", "") or ""
    abbreviation = (getattr(club, "abbreviation", "") or "").strip()
    if not abbreviation:
        return False
    # Both derivations, since a row could hold either.
    return abbreviation.lower() not in {derived_abbreviation(name).lower(), initials(name)}


def best_match(name: str, clubs, *, threshold: float = NAME_MATCH_THRESHOLD):
    """The club whose name is closest to ``name``, or ``(None, 0.0)``.

    Ties go to the lower pk, so the same input always returns the same club. The abbreviation is only
    consulted when a person chose it (:func:`is_hand_written`); an acronym passed as ``name`` still
    matches, since :func:`similarity` compares initialisms.
    """
    best, best_score = None, 0.0
    for club in sorted(clubs, key=lambda candidate: candidate.pk):
        score = similarity(name, club.name)
        if is_hand_written(club):
            score = max(score, similarity(name, club.abbreviation))
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
    #: ``high`` means the organizer said so or has answered this before; ``low`` means two strings
    #: looked alike. Shown on the page, because the second kind wants reading first.
    confidence: str


def _club_name_in_title(auction, clubs):
    """A club whose name or abbreviation is in this auction's title.

    An abbreviation must be a whole word: "NEC" is inside "connect".
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
    """Propose a club for each of ``auctions``, keyed by auction pk. Read-only.

    Four signals, in order of how much they can be trusted:

    1. The same organizer's other auctions are already linked -- somebody approved that once.
    2. The organizer's own ``UserData.club`` -- typed, but possibly years stale.
    3. The organizer belongs to exactly one club.
    4. The auction's name looks like a club's -- marked ``low``, meant to be read before approving.

    Three bulk queries whatever the size of ``auctions``, since the backlog is in the hundreds.
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

    # 1. What this organizer's linked auctions were filed under, most common first.
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
