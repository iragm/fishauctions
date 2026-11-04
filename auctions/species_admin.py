"""The species list's upkeep: every superuser decision about it, written once.

The gaps page's buttons (:mod:`auctions.views.species`), ``manage.py backfill_lot_species`` and an
approved proposal from ``/mcp/admin/`` (:mod:`auctions.mcp.admin_species`) all call these, so none of
the three can drift from the others:

* **the dashboard's decisions**: approve a species added on the site, merge two species or say they
  aren't duplicates, forget a remembered lot-name answer, let a retired pairing be matched again;
* **the backfill's two passes**: the automatic one sets a species only where exactly one answers a lot
  name with no language model (:func:`automatic_answer`); the reviewed one groups the names it can't
  settle into questions (:func:`review_groups`) for a person to answer with :func:`apply_species` or
  :func:`remember_not_a_species`;
* **what there is to do** (:func:`gap_rows`, :func:`backlog`, :func:`duplicate_pairs`, :func:`status`),
  read by the page, the command's ``--status`` and the admin endpoint;
* **fixing a wrong species** (:func:`lots_on_species`, :func:`move_lots`): lots already carrying one
  species moved to another, or to none.

Backfill writes with ``update()``, never ``save()``: a lot's category comes from its species, and moving
categories can flip a lot between BAP tracks while an existing ``BapAward`` reflects the old one.
``set_category`` opts back in for Uncategorized lots with no award.
"""

from collections import defaultdict

from django.db.models import Count, Max, Q

from auctions.models import (
    Auction,
    BapAward,
    Category,
    Lot,
    Species,
    SpeciesNameVote,
    SpeciesSearchCache,
)
from auctions.species_matching import (
    base_words,
    normalize,
    remember,
    singularize,
    suggest_species,
)

#: Picklist size before "search instead" is the honest answer.
MAX_CHOICES = 12

#: Spellings taught to the matcher per decision; keeps the shared cache from bloating on one-offs.
MAX_REMEMBERED = 20

#: Gap rows a sitting can get through; the tail is one-off names.
GAP_LIMIT = 100


# --- what there is to do ------------------------------------------------------------------------------


def missing_lots():
    """Lots that should have a species and don't. Only auctions with the field on: elsewhere nobody was
    offered the choice."""
    return Lot.objects.filter(species__isnull=True, is_deleted=False, banned=False, auction__use_scientific_name=True)


def eligible_lots(auction_slug=None):
    """The lots the backfill may touch: :func:`missing_lots` with a name, optionally in one auction.
    ``LookupError`` for a slug that names no auction."""
    lots = missing_lots().exclude(lot_name="")
    if auction_slug:
        if not Auction.objects.filter(slug=auction_slug).exists():
            msg = f"No auction with slug {auction_slug!r}."
            raise LookupError(msg)
        lots = lots.filter(auction__slug=auction_slug)
    return lots


def lots_with_species():
    return Lot.objects.filter(species__isnull=False, is_deleted=False, auction__use_scientific_name=True)


#: What the matcher last said about a lot name, as :func:`gap_rows` and :func:`backlog` name it.
VERDICTS = {
    "never_looked_up": "never looked up",
    "matches_a_species": "matches a species",
    "missing_from_the_list": "missing from the list",
    "not_a_species_by_the_model": "not a species, decided by the language model",
    "not_a_species_by_a_person": "not a species, chosen by a person",
}


def verdict_of(cache_row):
    """``(key, verdict, detail)`` for a lot name's remembered answer, or for none."""
    if cache_row is None:
        return "never_looked_up", "never looked up", ""
    if cache_row.species_id:
        # Resolves now; these lots predate it or the seller declined.
        return "matches_a_species", "matches a species", cache_row.species.label
    if cache_row.is_a_gap:
        # Identified but not on the list: a row for the curated CSV, and the cache row heals on import.
        return "missing_from_the_list", "missing from the list", cache_row.scientific_name
    if cache_row.source == "llm":
        return "not_a_species_by_the_model", "not a species", "decided by the language model"
    return "not_a_species_by_a_person", "not a species", "chosen by a person"


def _remembered_answers(keys):
    """``{search_text: SpeciesSearchCache}`` for these normalised names, in chunks MariaDB takes."""
    keys = list(keys)
    found = {}
    for start in range(0, len(keys), 2000):
        for cache_row in SpeciesSearchCache.objects.filter(search_text__in=keys[start : start + 2000]).select_related(
            "species"
        ):
            found[cache_row.search_text] = cache_row
    return found


def gap_rows(limit=GAP_LIMIT, verdict=None):
    """Lot names with no species, one row per normalised name, with the matcher's last verdict on it.

    It doesn't guess which are hardware; each field is evidence (breeder claims, the verdict) and the
    reader judges. Only names of stopwords and numbers are dropped. Breeder claims first. *verdict*, a
    :data:`VERDICTS` key, keeps only those rows, and then every name is read rather than the commonest.
    """
    rows = (
        missing_lots()
        .exclude(lot_name="")
        .values("lot_name")
        .annotate(
            # Lot's primary key is lot_number, not id.
            lots=Count("pk"),
            bred=Count("pk", filter=Q(i_bred_this_fish=True)),
            newest=Max("date_posted"),
        )
        .order_by("-lots", "-newest")
    )
    if verdict is None:
        rows = rows[: limit * 3]
    # Merged in Python: the normalisation is Python.
    merged = {}
    for row in rows:
        if not base_words(row["lot_name"]):
            continue
        key = normalize(row["lot_name"])
        if not key:
            continue
        entry = merged.setdefault(
            key, {"lot_name": row["lot_name"], "lots": 0, "bred": 0, "newest": row["newest"], "key": key}
        )
        entry["lots"] += row["lots"]
        entry["bred"] += row["bred"]
        entry["newest"] = max(entry["newest"], row["newest"]) if row["newest"] else entry["newest"]

    verdicts = _remembered_answers(merged)
    for key, entry in merged.items():
        cache_row = verdicts.get(key)
        entry["answer"] = cache_row.pk if cache_row else None
        entry["verdict_key"], entry["verdict"], entry["verdict_detail"] = verdict_of(cache_row)
    entries = [entry for entry in merged.values() if verdict is None or entry["verdict_key"] == verdict]
    return sorted(entries, key=lambda entry: (-entry["bred"], -entry["lots"]))[:limit]


def backlog():
    """Lots with no species and their distinct names, by what the matcher last said about the name.

    ``not_a_species_by_a_person`` is the settled part: those lots are hardware, food or mixed bags and
    stay without a species on purpose. Everything else is still to do. ``no_species_words`` are names of
    counts and adjectives only ("3 bags"), which nothing could match.
    """
    tallies = {key: {"lots": 0, "names": 0} for key in (*VERDICTS, "no_species_words")}
    by_key = defaultdict(int)
    for row in missing_lots().exclude(lot_name="").values("lot_name").annotate(lots=Count("pk")).order_by():
        key = normalize(row["lot_name"]) if base_words(row["lot_name"]) else ""
        if not key:
            tallies["no_species_words"]["lots"] += row["lots"]
            tallies["no_species_words"]["names"] += 1
            continue
        by_key[key] += row["lots"]
    answers = _remembered_answers(by_key)
    for key, lots in by_key.items():
        kind = verdict_of(answers.get(key))[0]
        tallies[kind]["lots"] += lots
        tallies[kind]["names"] += 1
    return tallies


def attach_votes(rows):
    """Put the evidence on each row with a ``search_text`` and a ``species``: ``kept`` and ``taken_off``
    (lots, from :class:`SpeciesNameVote`), ``people`` (who took it off) and ``instead`` (what those lots
    got, commonest first). Two queries, however many rows.
    """
    texts = {row.search_text for row in rows}
    if not texts:
        return
    votes = SpeciesNameVote.objects.filter(search_text__in=texts, species__isnull=False).order_by()
    tallies = {
        (entry["search_text"], entry["species"]): entry
        for entry in votes.values("search_text", "species").annotate(
            kept=Count("pk", filter=Q(agrees=True)),
            taken_off=Count("pk", filter=Q(agrees=False)),
            people=Count("user", filter=Q(agrees=False), distinct=True),
        )
    }
    picks = list(votes.filter(agrees=False).values("search_text", "species", "chosen").annotate(lots=Count("pk")))
    labels = {
        species.pk: species.label for species in Species.objects.filter(pk__in={pick["chosen"] for pick in picks})
    }
    instead = defaultdict(list)
    for pick in sorted(picks, key=lambda pick: -pick["lots"]):
        label = labels.get(pick["chosen"], "no species")
        instead[(pick["search_text"], pick["species"])].append((label, pick["lots"]))
    for row in rows:
        tally = tallies.get((row.search_text, row.species_id), {})
        row.kept = tally.get("kept", 0)
        row.taken_off = tally.get("taken_off", 0)
        row.people = tally.get("people", 0)
        row.instead = instead.get((row.search_text, row.species_id), [])


def pending_species():
    """Species added on the site, visible only to their author and club until approved. Newest first."""
    return (
        Species.objects.filter(approved=False)
        .select_related("added_by", "category", "parent", "club")
        .annotate(lots=Count("lot"))
        .order_by("-id")
    )


def duplicate_pairs(limit=100):
    """Species flagged as possible duplicates, one entry per pair (both halves carry the flag), stably
    ordered by pk, each half with its lot count."""
    flagged = list(
        Species.objects.filter(possible_duplicate__isnull=False)
        .select_related("possible_duplicate", "category", "added_by", "club")
        .annotate(lots=Count("lot"))
        .order_by("pk")[:limit]
    )
    # One query for the other halves' lot counts.
    other_lots = dict(
        Lot.objects.filter(species__in=[species.possible_duplicate_id for species in flagged])
        .values_list("species")
        .annotate(count=Count("pk"))
    )
    pairs = []
    seen = set()
    for species in flagged:
        other = species.possible_duplicate
        pair = tuple(sorted((species.pk, other.pk)))
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(
            {
                "species": species,
                "other": other,
                "other_lots": other_lots.get(other.pk, 0),
                "same_scientific_name": bool(
                    species.scientific_name
                    and species.scientific_name.lower() == other.scientific_name.lower()
                    and species.variety.lower() == other.variety.lower()
                ),
            }
        )
    return pairs


def status(lots=None):
    """What the list covers and how much of the backfill is left, as numbers. *lots* defaults to every
    lot :func:`eligible_lots` would touch."""
    lots = eligible_lots() if lots is None else lots
    curated = Species.objects.filter(source="aquarium")
    done = lots_with_species().count()
    missing = lots.count()
    return {
        "species_by_source": {
            row["source"]: row["n"] for row in Species.objects.values("source").annotate(n=Count("pk")).order_by()
        },
        # Grouped by category rather than the CSV's "kind" column, to match the site's list.
        "curated_by_category": [
            (row["category__name"] or "no category", row["n"])
            for row in curated.values("category__name").annotate(n=Count("pk")).order_by("-n")
        ],
        "strains": Species.objects.filter(parent__isnull=False).count(),
        "crosses": Species.objects.filter(is_hybrid=True).count(),
        "lots_with_species": done,
        "lots_without_species": missing,
        "distinct_names_without_species": lots.values("lot_name").distinct().count(),
        "percent_done": done * 100 // (done + missing) if done + missing else 0,
    }


# --- the dashboard's decisions -------------------------------------------------------------------------


def approve(species, lot_names=20):
    """Share a species added on the site with everybody. False when it already was.

    Its names become everybody's at the same time, and the lot names it was set on are learned, which
    ``remember()`` refused while it was unapproved.
    """
    if species.approved:
        return False
    species.approved = True
    species.save()
    species.common_names.filter(approved=False).update(approved=True)
    # This row was invisible to the last genus-tier pass.
    Species.recompute_trade_ranks(genus=species.genus)
    for lot_name in (
        Lot.objects.filter(species=species)
        .exclude(lot_name="")
        .order_by()
        .values_list("lot_name", flat=True)
        .distinct()
    )[:lot_names]:
        remember(lot_name, species, source="user", user=species.added_by)
    return True


def forget(row):
    """Delete one remembered answer so the name is worked out again; the undo for ``remember()``.
    Returns the name it was for."""
    name = row.search_text
    row.delete()
    return name


def allow_again(rejection):
    """Let a retired pairing be matched again, usually because the rejections were about the lot names,
    not the species. The lots that took it off are overruled, so their votes go: left in place, the
    next lot to take it off would retire it again at once. Returns ``(name, species)``."""
    name, species = rejection.search_text, rejection.species
    SpeciesNameVote.objects.filter(search_text=name, species=species, agrees=False).delete()
    rejection.delete()
    return name, species


def dismiss_duplicate(species):
    """ "These two are not the same species": clears the flag on both sides, since some species really
    share a designated name. Returns the other half, or ``None``."""
    other = species.possible_duplicate
    Species.objects.filter(pk=species.pk).update(possible_duplicate=None)
    if other:
        Species.objects.filter(pk=other.pk).update(possible_duplicate=None)
    return other


def merge(keep, duplicate):
    """Fold *duplicate* into *keep*: ``(moved, "")`` or ``({}, why not)``. Irreversible, and which name
    the site keeps is the list maintainer's call."""
    if keep.pk == duplicate.pk:
        return {}, "A species cannot be merged into itself."
    # A strain and its parent aren't duplicates; merging would lose the strain.
    if keep.parent_id == duplicate.pk or duplicate.parent_id == keep.pk:
        return {}, "That is a strain and its parent species, not a duplicate.  Nothing was merged."
    return keep.merge_duplicate(duplicate), ""


def merged_sentence(losing_label, keep, moved):
    return (
        f"Merged {losing_label} into {keep.label}: "
        f"{moved.get('lots', 0)} lot(s), {moved.get('common_names', 0)} common name(s), "
        f"{moved.get('varieties', 0)} strain(s) and {moved.get('remembered_names', 0)} "
        "remembered name(s) moved."
    )


# --- the backfill ---------------------------------------------------------------------------------------


def group_key(lot_name):
    """Words in a lot name that could name a species, singular, in order.

    Groups "6 male guppies", "Guppies (pair)" and "young guppy" under ``guppy``. Checked before and
    after singularizing, since the stop-word list only covers one form.
    """
    words = []
    for word in base_words(lot_name):
        singular = singularize(word)
        if base_words(singular):
            words.append(singular)
    return " ".join(words)


class NameGroup:
    """One review question: every spelling of a name that means the same thing, and its candidates.

    Grouped by :func:`group_key` and by candidate species, since the key strips colours ("blue dream"
    and "green dream shrimp" share a key but name different cultivars).
    """

    def __init__(self, key, candidates, source):
        self.key = key
        self.candidates = candidates
        self.source = source
        self.spellings = []
        self.lots = 0
        self.bred = 0

    def add(self, lot_name, lots, bred):
        self.spellings.append((lot_name, lots))
        self.lots += lots
        self.bred += bred

    @property
    def display(self):
        """The spelling to show, which is the one most lots actually use."""
        return self.spellings[0][0] if self.spellings else self.key

    @property
    def names(self):
        return [name for name, _ in self.spellings]


def name_counts(lots, limit=None, skip=0):
    """``[{lot_name, count, bred}, ...]``, commonest first, so a limit spends on the big names. *skip*
    passes over that many of the commonest first, to reach the tail."""
    rows = list(
        lots.values("lot_name")
        .annotate(count=Count("pk"), bred=Count("pk", filter=Q(i_bred_this_fish=True)))
        .order_by("-count", "lot_name")
    )
    rows = rows[skip:]
    return rows[:limit] if limit else rows


def automatic_answer(lot_name, record=True):
    """``(species, source)`` when exactly one species answers *lot_name* with no language model, else
    ``(None, source)``: a shortlist means only a person can decide. ``record=False`` writes nothing."""
    found, source = suggest_species(lot_name, use_llm=False, record=record)
    return (found[0], source) if len(found) == 1 else (None, source)


def apply_species(lots, species, names, *, set_category=False, teach=False, user=None, dry_run=False):
    """Set *species* on every lot in *lots* named any of *names*; returns ``(lots, refiled)``.

    ``update()``, not ``save()`` (see the module docstring). *teach* writes the decision to the shared
    cache, as the reviewed pass does, for at most :data:`MAX_REMEMBERED` spellings.
    """
    pks = list(lots.filter(lot_name__in=names).values_list("pk", flat=True))
    if not pks:
        return 0, 0
    movable = _refilable(pks, species) if set_category else []
    if dry_run:
        return len(pks), len(movable)
    Lot.objects.filter(pk__in=pks).update(species=species)
    _refile(movable, species)
    if teach:
        for name in names[:MAX_REMEMBERED]:
            remember(name, species, source="user", user=user)
    return len(pks), len(movable)


def _refilable(pks, species):
    """Of these lots, the Uncategorized ones with no breeder award that *species*' category could take."""
    uncategorized = Category.objects.filter(name="Uncategorized").values_list("pk", flat=True).first()
    if species is None or not species.category_id or species.category_id == uncategorized:
        return []
    return list(
        Lot.objects.filter(pk__in=pks)
        .filter(Q(species_category__isnull=True) | Q(species_category__name="Uncategorized"))
        .exclude(pk__in=BapAward.objects.filter(lot__isnull=False).values("lot_id"))
        .values_list("pk", flat=True)
    )


def _refile(pks, species):
    if pks and species is not None:
        Lot.objects.filter(pk__in=pks).update(
            species_category_id=species.category_id,
            category_automatically_added=True,
            category_checked=True,
        )


def lots_on_species(species, names=None, auction_slug=None):
    """Lots (not deleted) carrying *species*, optionally only those named *names* or in one auction.
    ``LookupError`` for a slug that names no auction."""
    lots = Lot.objects.filter(species=species, is_deleted=False)
    if auction_slug:
        if not Auction.objects.filter(slug=auction_slug).exists():
            msg = f"No auction with slug {auction_slug!r}."
            raise LookupError(msg)
        lots = lots.filter(auction__slug=auction_slug)
    if names:
        lots = lots.filter(lot_name__in=names)
    return lots


def move_lots(lots, from_species, to_species, *, names=(), set_category=False, teach=False, user=None):
    """Put *to_species* (``None``: no species) on *lots*, which carry *from_species*; returns
    ``(lots, refiled, untaught)``.

    The fix for lots matched to the wrong species, where :func:`apply_species` only fills blanks.
    ``update()``, not ``save()``, for the reason in the module docstring. Whatever the site remembered
    the moved lots' names to be *from_species* is forgotten, so the wrong answer isn't served to the next
    lot. *teach* then remembers each of *names* that a lot actually carried as the new answer, at most
    :data:`MAX_REMEMBERED`; *untaught* are those ``remember()`` refused (an unapproved species, a retired
    pairing, a name the list answers exactly).
    """
    moved = list(lots.values_list("pk", "lot_name"))
    pks = [pk for pk, _ in moved]
    movable = []
    if set_category and pks and to_species is not None:
        movable = _refilable(pks, to_species)
        if from_species.category_id and from_species.category_id != to_species.category_id:
            # Filed under the wrong species' category by the site, not by a person.
            movable += list(
                Lot.objects.filter(
                    pk__in=pks, species_category_id=from_species.category_id, category_automatically_added=True
                )
                .exclude(pk__in=BapAward.objects.filter(lot__isnull=False).values("lot_id"))
                .exclude(pk__in=movable)
                .values_list("pk", flat=True)
            )
    if pks:
        Lot.objects.filter(pk__in=pks).update(species=to_species)
        if to_species is not None and to_species.category_id:
            _refile(movable, to_species)
    keys = {normalize(lot_name) for _, lot_name in moved} - {""}
    if keys:
        SpeciesSearchCache.objects.filter(search_text__in=list(keys), species=from_species).delete()
    untaught = []
    if teach:
        carried = {normalize(lot_name) for _, lot_name in moved}
        for name in [name for name in names if normalize(name) in carried][:MAX_REMEMBERED]:
            if not remember(name, to_species, source="user", user=user):
                untaught.append(name)
    return len(pks), len(movable), untaught


def remember_not_a_species(names, user=None):
    """Remember "not a species" for each spelling, so nothing asks about it again. Returns how many were
    written: a name the list answers exactly is never written (``remember()``)."""
    return sum(bool(remember(name, None, source="user", user=user)) for name in names[:MAX_REMEMBERED])


def review_groups(
    lots, *, include_unmatched=False, min_lots=2, scan=5000, skip=0, limit=None, record=True, progress=None
):
    """The questions the automatic pass leaves, biggest first.

    Runs the matcher over the *scan* commonest names (``0`` for all) and keeps those with several
    candidates, plus those with none when *include_unmatched*, grouped into :class:`NameGroup`. Groups
    on fewer than *min_lots* lots are dropped: a one-off is not yet a pattern. *skip* starts the scan
    that many names down the list. *progress* is called with ``(done, total)`` every thousand names.
    """
    groups = {}
    rows = name_counts(lots, scan or None, skip=skip)
    for scanned, row in enumerate(rows, start=1):
        if progress and scanned % 1000 == 0:
            progress(scanned, len(rows))
        name = row["lot_name"]
        key = group_key(name)
        if not key:
            continue  # nothing but counts and adjectives: "3 bags", "assorted"
        found, source = suggest_species(name, use_llm=False, record=record)
        if len(found) == 1:
            continue  # the automatic pass already owns this one
        if not found and not include_unmatched:
            continue
        fingerprint = (key, tuple(sorted(species.pk for species in found)))
        group = groups.get(fingerprint)
        if group is None:
            group = groups[fingerprint] = NameGroup(key, found, source)
        group.add(name, row["count"], row["bred"])
    ranked = sorted(groups.values(), key=lambda group: (-group.lots, group.key))
    ranked = [group for group in ranked if group.lots >= min_lots]
    return ranked[:limit] if limit else ranked
