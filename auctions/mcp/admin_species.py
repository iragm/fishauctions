"""The species list's upkeep on ``/mcp/admin/``: two reads, and eight changes only a proposal makes.

Everything on the species gaps page and in ``manage.py backfill_lot_species``, for an agent that can't
press a button or answer a prompt. The reads, :func:`species_dashboard` and :func:`species_backfill`,
write nothing (``record=False`` keeps even the cache's hit counter still). Each change is an
``APPROVAL_ONLY`` step for :func:`auctions.mcp.admin.propose_change`: it runs only when a person
approves it, through the same :mod:`auctions.species_admin` function the page's button calls, and each
is checked once when proposed (:data:`PROPOSAL_CHECKS`) so a bad number reaches the agent while it can
still fix it, and again inside the change on approval.

Lot names and search texts are what sellers typed, so they come back fenced. Who added a species is
not said: it is a member, and these answers may end up in a public repository.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied

from auctions import palette_actions, species_admin
from auctions.palette_actions import DANGER_CONFIRM, DANGER_SAFE, Action, _error, _int, _need, _ok, _str

untrusted_short = palette_actions.untrusted_short

#: What ``species_dashboard`` can show, and what each holds.
SECTIONS = {
    "summary": "counts of everything below, and how much of the backfill is left",
    "gaps": "lot names with no species, with the matcher's last verdict on each",
    "pending": "species added on the site that only their author's club sees until approved",
    "remembered": "lot names the site remembers as a species, with the lots that kept or took it off",
    "not_a_species": "lot names remembered as no species, or identified but missing from the list",
    "retired": "pairings retired because lots kept taking them off",
    "duplicates": "pairs of species flagged as possibly the same",
}

#: Names one ``backfill_species`` step may name: what fits on one approval and in one request.
MAX_BACKFILL_NAMES = 200

#: Names the read runs the matcher over by default, and at most. Commonest first, so a few hundred
#: names covers most lots; every one is several queries on production.
DEFAULT_SCAN = 500
MAX_SCAN = 3000


def _superuser(request):
    if not getattr(request.user, "is_superuser", False):
        raise PermissionDenied


def _echo(species) -> dict[str, Any]:
    """A species as these tools report it. A common name an auction runner typed is fenced."""
    echo = palette_actions._species_echo(species)
    if echo and species.source == "admin":
        echo["common_name"] = untrusted_short(species.common_name) or None
    return echo


def _lot_names(params: dict[str, Any], most: int) -> tuple[list[str], str]:
    """``lot_names`` as a list of at most *most* distinct non-blank strings, or ``([], why not)``."""
    raw = params.get("lot_names")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], "lot_names must be a list of the lot names, exactly as the dashboard gave them."
    names = list(dict.fromkeys(_as_typed(name) for name in raw if _as_typed(name)))
    if not names:
        return [], "lot_names must name at least one lot name."
    if len(names) > most:
        return [], f"At most {most} lot names in one step; split it."
    return names, ""


def _as_typed(name: Any) -> str:
    """A lot name as the seller typed it, if an agent copied it with the fence these reads put round it."""
    name = str(name).strip()
    if name.startswith(palette_actions.UNTRUSTED_MARK_OPEN) and name.endswith(palette_actions.UNTRUSTED_CLOSE):
        name = name[1:-1].strip()
    return name[:255]


def _species(params: dict[str, Any], key: str = "species"):
    """The species a step names by number, or ``(None, why not)``."""
    from auctions.models import Species

    number = _int(params, key)
    if number is None:
        return None, f"{key} must be a species number from species_dashboard or species_backfill."
    species = Species.objects.filter(pk=number).first()
    if species is None:
        return None, f"There is no species number {number}."
    return species, ""


def _lots(params: dict[str, Any]):
    """``(eligible lots, "")``, or ``(None, why not)`` for an auction slug that names nothing."""
    try:
        return species_admin.eligible_lots(_str(params, "auction") or None), ""
    except LookupError as error:
        return None, str(error)


# --- reads ------------------------------------------------------------------------------------------


def _gaps(limit, offset):
    rows = species_admin.gap_rows(limit=offset + limit)
    return [
        {
            "lot_name": untrusted_short(row["lot_name"]),
            "lots": row["lots"],
            "bred": row["bred"],
            "newest": row["newest"].date().isoformat() if row["newest"] else None,
            "verdict": row["verdict"],
            "verdict_detail": row["verdict_detail"],
            "answer": row["answer"],
        }
        for row in rows[offset:]
    ], None


def _pending(limit, offset):
    rows = species_admin.pending_species()
    return [
        {
            **_echo(species),
            "strain_of": species.parent.full_scientific_name if species.parent_id else None,
            "club": species.club.name if species.club_id else None,
            "lots": species.lots,
            "added_by_a_site_admin": bool(species.added_by_id and species.added_by.is_superuser),
        }
        for species in rows[offset : offset + limit]
    ], rows.count()


def _votes(row) -> dict[str, Any]:
    return {
        "lots_kept_it": row.kept,
        "lots_took_it_off": row.taken_off,
        "people_who_took_it_off": row.people,
        "those_lots_got_instead": [{"species": label, "lots": lots} for label, lots in row.instead[:5]],
    }


def _remembered(limit, offset):
    from auctions.models import SpeciesSearchCache

    rows = SpeciesSearchCache.objects.filter(species__isnull=False).select_related("species", "species__category")
    page = list(rows.order_by("-hits", "-createdon")[offset : offset + limit])
    species_admin.attach_votes(page)
    return [
        {
            "answer": row.pk,
            "lot_name": untrusted_short(row.search_text),
            "species": _echo(row.species),
            "taught_by": "a person" if row.source == "user" else "the language model",
            "times_served": row.hits,
            **_votes(row),
        }
        for row in page
    ], rows.count()


def _not_a_species(limit, offset):
    from auctions.models import SpeciesSearchCache

    rows = SpeciesSearchCache.objects.filter(species__isnull=True)
    return [
        {
            "answer": row.pk,
            "lot_name": untrusted_short(row.search_text),
            "verdict": "missing from the list" if row.is_a_gap else "not a species",
            "identified_as": row.scientific_name or None,
            "decided_by": "a person" if row.source == "user" else "the language model",
            "times_served": row.hits,
        }
        for row in rows.order_by("-hits", "-createdon")[offset : offset + limit]
    ], rows.count()


def _retired(limit, offset):
    from auctions.models import SpeciesNameRejection

    rows = SpeciesNameRejection.objects.select_related("species", "species__category")
    page = list(rows.order_by("-createdon")[offset : offset + limit])
    species_admin.attach_votes(page)
    return [
        {
            "pairing": row.pk,
            "lot_name": untrusted_short(row.search_text),
            "species": _echo(row.species),
            "retired_on": row.createdon.date().isoformat(),
            **_votes(row),
        }
        for row in page
    ], rows.count()


def _duplicates(limit, offset):
    pairs = species_admin.duplicate_pairs(limit=2 * (offset + limit))
    return [
        {
            "species": {**_echo(pair["species"]), "lots": pair["species"].lots},
            "other": {**_echo(pair["other"]), "lots": pair["other_lots"]},
            "same_scientific_name": pair["same_scientific_name"],
        }
        for pair in pairs[offset : offset + limit]
    ], None


def _summary() -> dict[str, Any]:
    from auctions.models import Species, SpeciesNameRejection, SpeciesSearchCache

    numbers = species_admin.status()
    cache = SpeciesSearchCache.objects
    return {
        "species": Species.objects.count(),
        "species_by_source": numbers["species_by_source"],
        "curated_list_loaded": bool(numbers["curated_by_category"]),
        "strains": numbers["strains"],
        "crosses": numbers["crosses"],
        "lots_with_species": numbers["lots_with_species"],
        "lots_without_species": numbers["lots_without_species"],
        "distinct_names_without_species": numbers["distinct_names_without_species"],
        "percent_done": numbers["percent_done"],
        "pending": Species.objects.filter(approved=False).count(),
        "remembered": cache.filter(species__isnull=False).count(),
        "not_a_species": cache.filter(species__isnull=True, scientific_name="").count(),
        "missing_from_the_list": cache.filter(species__isnull=True).exclude(scientific_name="").count(),
        "retired": SpeciesNameRejection.objects.count(),
        "duplicate_pairs": len(species_admin.duplicate_pairs(limit=1000)),
    }


def species_dashboard(request, params: dict[str, Any]) -> dict[str, Any]:
    """The species gaps page as data, one section at a time, every row with the number a change needs."""
    _superuser(request)
    section = _str(params, "section", "summary").lower().replace(" ", "_")
    if section not in SECTIONS:
        return _need("Which section? One of: " + ", ".join(SECTIONS) + ".")
    if section == "summary":
        numbers = _summary()
        return _ok(
            f"{numbers['lots_without_species']} lots have no species ({numbers['percent_done']}% done); "
            f"{numbers['pending']} species waiting for approval, {numbers['duplicate_pairs']} possible "
            f"duplicate pairs, {numbers['retired']} retired pairings.",
            **numbers,
        )
    limit, offset = palette_actions._slice(params)
    rows, total = {
        "gaps": _gaps,
        "pending": _pending,
        "remembered": _remembered,
        "not_a_species": _not_a_species,
        "retired": _retired,
        "duplicates": _duplicates,
    }[section](limit, offset)
    result = {"section": section, "rows": rows}
    if total is not None:
        result["total"] = total
        summary = f"{total} in {section.replace('_', ' ')}." + palette_actions._showing(total, limit, offset)
        if offset + limit < total:
            result["next_offset"] = offset + limit
    else:
        summary = f"{len(rows)} in {section.replace('_', ' ')} from {offset + 1}."
        if len(rows) == limit:
            result["next_offset"] = offset + limit
    return _ok(summary, **result)


def species_backfill(request, params: dict[str, Any]) -> dict[str, Any]:
    """What ``backfill_lot_species`` would do, without doing it: the automatic pass's matches, or the
    reviewed pass's questions. Writes nothing."""
    _superuser(request)
    lots, problem = _lots(params)
    if problem:
        return _error(problem)
    scan = max(1, min(_int(params, "scan") or DEFAULT_SCAN, MAX_SCAN))
    limit, offset = palette_actions._slice(params)
    which = _str(params, "pass", "automatic").lower()
    if which == "automatic":
        matches = []
        for row in species_admin.name_counts(lots, scan):
            species, source = species_admin.automatic_answer(row["lot_name"], record=False)
            if species is not None:
                matches.append((row, species, source))
        total = len(matches)
        found = [
            {
                "lot_name": untrusted_short(row["lot_name"]),
                "lots": row["count"],
                "species": _echo(species),
                "matched_by": source,
            }
            for row, species, source in matches[offset : offset + limit]
        ]
        result = {"pass": which, "names_scanned": scan, "matches": found, "total": total}
        if offset + limit < total:
            result["next_offset"] = offset + limit
        return _ok(
            f"{total} of the {scan} commonest lot names without a species match exactly one species."
            + palette_actions._showing(total, limit, offset)
            + " Propose backfill_species with their lot_names to set them.",
            **result,
        )
    if which == "review":
        groups = species_admin.review_groups(
            lots,
            include_unmatched=bool(palette_actions._flag(params, "include_unmatched")),
            min_lots=max(1, _int(params, "min_lots") or 2),
            scan=scan,
            record=False,
        )
        total = len(groups)
        questions = [
            {
                "name": untrusted_short(group.display),
                "lots": group.lots,
                "bred": group.bred,
                "lot_names": [untrusted_short(name) for name in group.names[: species_admin.MAX_REMEMBERED]],
                "more_spellings": max(0, len(group.names) - species_admin.MAX_REMEMBERED),
                "candidates": [_echo(species) for species in group.candidates[: species_admin.MAX_CHOICES]],
            }
            for group in groups[offset : offset + limit]
        ]
        result = {"pass": which, "names_scanned": scan, "questions": questions, "total": total}
        if offset + limit < total:
            result["next_offset"] = offset + limit
        return _ok(
            f"{total} names the matcher can't settle on its own."
            + palette_actions._showing(total, limit, offset)
            + " Answer each with set_species_on_lot_names, remember_not_a_species, or add_species first.",
            **result,
        )
    return _need("Which pass? automatic or review.")


# --- changes a proposal makes -------------------------------------------------------------------------


def _cache_row(params):
    from auctions.models import SpeciesSearchCache

    number = _int(params, "answer")
    row = SpeciesSearchCache.objects.filter(pk=number).first() if number is not None else None
    return row, "" if row else "There is no remembered answer with that number (species_dashboard's answer)."


def _rejection(params):
    from auctions.models import SpeciesNameRejection

    number = _int(params, "pairing")
    row = SpeciesNameRejection.objects.filter(pk=number).first() if number is not None else None
    return row, "" if row else "There is no retired pairing with that number (species_dashboard's pairing)."


def approve_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """The gaps page's Approve."""
    _superuser(request)
    species, problem = _species(params)
    if problem:
        return _error(problem)
    if not species_admin.approve(species):
        return _ok(f"{species.label} was already approved.")
    return _ok(f"{species.label} is now suggested for everyone.", species=_echo(species))


def merge_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """The gaps page's Merge: *duplicate* folds into *keep*, which survives."""
    _superuser(request)
    keep, problem = _species(params, "keep")
    duplicate, other_problem = _species(params, "duplicate")
    if problem or other_problem:
        return _error(problem or other_problem)
    losing_label = duplicate.label
    moved, problem = species_admin.merge(keep, duplicate)
    if problem:
        return _error(problem)
    return _ok(species_admin.merged_sentence(losing_label, keep, moved), moved=moved)


def dismiss_species_duplicate(request, params: dict[str, Any]) -> dict[str, Any]:
    """The gaps page's Not a duplicate."""
    _superuser(request)
    species, problem = _species(params)
    if problem:
        return _error(problem)
    other = species_admin.dismiss_duplicate(species)
    return _ok(f"{species.label} is not a duplicate" + (f" of {other.label}." if other else "."))


def forget_species_answer(request, params: dict[str, Any]) -> dict[str, Any]:
    """The gaps page's Forget."""
    _superuser(request)
    row, problem = _cache_row(params)
    if problem:
        return _error(problem)
    name = species_admin.forget(row)
    return _ok(f"Forgot the remembered answer for {untrusted_short(name)}. It will be looked up again.")


def allow_species_pairing_again(request, params: dict[str, Any]) -> dict[str, Any]:
    """The gaps page's Allow it again."""
    _superuser(request)
    row, problem = _rejection(params)
    if problem:
        return _error(problem)
    name, species = species_admin.allow_again(row)
    return _ok(f"{untrusted_short(name)} may be matched to {species.label} again.")


def backfill_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """The automatic pass over the lot names a person read: each gets the one species that answers it
    now, or is skipped. Teaches the cache nothing, as the command's automatic pass doesn't."""
    _superuser(request)
    names, problem = _lot_names(params, MAX_BACKFILL_NAMES)
    lots, lots_problem = _lots(params)
    if problem or lots_problem:
        return _error(problem or lots_problem)
    set_category = bool(palette_actions._flag(params, "set_category"))
    applied, skipped, total, refiled = 0, [], 0, 0
    for name in names:
        species, _source = species_admin.automatic_answer(name)
        if species is None:
            skipped.append(untrusted_short(name))
            continue
        count, moved = species_admin.apply_species(lots, species, [name], set_category=set_category)
        if count:
            applied += 1
            total += count
            refiled += moved
    summary = f"Set a species on {total} lot(s) under {applied} name(s)."
    if refiled:
        summary += f" {refiled} were filed under their species' category."
    if skipped:
        summary += f" {len(skipped)} name(s) no longer match exactly one species and were left alone."
    return _ok(summary, lots=total, names=applied, skipped=skipped[:20])


def set_species_on_lot_names(request, params: dict[str, Any]) -> dict[str, Any]:
    """The reviewed pass's answer: this species on every lot with no species called any of these, and
    the names remembered so the next one matches by itself."""
    _superuser(request)
    species, problem = _species(params)
    names, names_problem = _lot_names(params, species_admin.MAX_REMEMBERED)
    lots, lots_problem = _lots(params)
    if problem or names_problem or lots_problem:
        return _error(problem or names_problem or lots_problem)
    count, refiled = species_admin.apply_species(
        lots,
        species,
        names,
        set_category=bool(palette_actions._flag(params, "set_category")),
        teach=palette_actions._flag(params, "remember") is not False,
        user=request.user,
    )
    summary = f"{species.label} set on {count} lot(s)."
    if refiled:
        summary += f" {refiled} were filed under {species.category}."
    return _ok(summary, lots=count, species=_echo(species))


def remember_not_a_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """The reviewed pass's "not a species": nothing asks about these names again."""
    _superuser(request)
    names, problem = _lot_names(params, species_admin.MAX_REMEMBERED)
    if problem:
        return _error(problem)
    written = species_admin.remember_not_a_species(names, user=request.user)
    summary = f"Remembered {written} name(s) as not a species."
    if written < len(names):
        summary += " The others name a species exactly, so the list answers them and nothing was remembered."
    return _ok(summary)


_LOT_NAMES = "array of string, required. Lot names exactly as species_dashboard or species_backfill gave them"
_AUCTION = "string, optional. Only lots in this auction, by slug. Default: every auction that uses scientific names."
_SET_CATEGORY = (
    "boolean, optional, default false. Also file Uncategorized lots with no breeder award under the species' category."
)

APPROVAL_ONLY: list[Action] = [
    Action(
        name="approve_species",
        description="Approve a species added on the site so it is suggested for everyone, with its names.",
        params={"species": "integer, required. The species number from species_dashboard's pending section."},
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Approve a species",
        resolver=approve_species,
    ),
    Action(
        name="merge_species",
        description=(
            "Fold one species into another: its lots, names, strains and remembered names move to keep and "
            "it is deleted. Not reversible."
        ),
        params={
            "keep": "integer, required. The species number that survives.",
            "duplicate": "integer, required. The species number folded into it.",
        },
        danger=DANGER_CONFIRM,
        confirm_template="Merge two species",
        resolver=merge_species,
    ),
    Action(
        name="dismiss_species_duplicate",
        description="Say two species flagged as duplicates are different; clears the flag on both.",
        params={"species": "integer, required. Either species number of the pair."},
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Not a duplicate",
        resolver=dismiss_species_duplicate,
    ),
    Action(
        name="forget_species_answer",
        description="Forget what the site remembers a lot name to be, species or not, so it is worked out again.",
        params={"answer": "integer, required. The answer number from species_dashboard."},
        danger=DANGER_CONFIRM,
        confirm_template="Forget a remembered species answer",
        resolver=forget_species_answer,
    ),
    Action(
        name="allow_species_pairing_again",
        description=(
            "Let a retired pairing of a lot name and a species be matched again; the lots that took it off "
            "are overruled."
        ),
        params={"pairing": "integer, required. The pairing number from species_dashboard's retired section."},
        danger=DANGER_CONFIRM,
        confirm_template="Allow a retired species pairing again",
        resolver=allow_species_pairing_again,
    ),
    Action(
        name="backfill_species",
        description=(
            "The backfill's automatic pass over named lot names: each lot with no species gets the one "
            "species that matches its name, checked again on approval; a name that no longer matches "
            "exactly one is left alone."
        ),
        params={
            "lot_names": f"{_LOT_NAMES}, at most {MAX_BACKFILL_NAMES}.",
            "auction": _AUCTION,
            "set_category": _SET_CATEGORY,
        },
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Backfill species",
        resolver=backfill_species,
    ),
    Action(
        name="set_species_on_lot_names",
        description=(
            "Answer a backfill question: put this species on every lot with no species called any of "
            "these names, and remember the names so the next one matches by itself."
        ),
        params={
            "species": "integer, required. The species number, one of the question's candidates or any other.",
            "lot_names": f"{_LOT_NAMES}, at most {species_admin.MAX_REMEMBERED}.",
            "auction": _AUCTION,
            "set_category": _SET_CATEGORY,
            "remember": "boolean, optional, default true. false sets the lots without teaching the matcher.",
        },
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Set a species on lots",
        resolver=set_species_on_lot_names,
    ),
    Action(
        name="remember_not_a_species",
        description="Answer a backfill question with not a species (hardware, food, a mixed bag), so it isn't asked again.",
        params={"lot_names": f"{_LOT_NAMES}, at most {species_admin.MAX_REMEMBERED}."},
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Remember as not a species",
        resolver=remember_not_a_species,
    ),
]


#: Checked when proposed; each runs again inside the change on approval.
PROPOSAL_CHECKS = {
    "approve_species": lambda arguments: _species(arguments)[1],
    "merge_species": lambda arguments: _species(arguments, "keep")[1] or _species(arguments, "duplicate")[1],
    "dismiss_species_duplicate": lambda arguments: _species(arguments)[1],
    "forget_species_answer": lambda arguments: _cache_row(arguments)[1],
    "allow_species_pairing_again": lambda arguments: _rejection(arguments)[1],
    "backfill_species": lambda arguments: _lot_names(arguments, MAX_BACKFILL_NAMES)[1] or _lots(arguments)[1],
    "set_species_on_lot_names": lambda arguments: (
        _species(arguments)[1] or _lot_names(arguments, species_admin.MAX_REMEMBERED)[1] or _lots(arguments)[1]
    ),
    "remember_not_a_species": lambda arguments: _lot_names(arguments, species_admin.MAX_REMEMBERED)[1],
}

READS: list[Action] = [
    Action(
        name="species_dashboard",
        description=(
            "The species gaps page as data, one section at a time, with the numbers a proposed change "
            "names: " + "; ".join(f"{name}: {what}" for name, what in SECTIONS.items()) + "."
        ),
        params={
            "section": "string, optional, default summary. " + ", ".join(SECTIONS) + ".",
            "limit": "integer, optional, default 15.",
            "offset": "integer, optional, default 0.",
        },
        danger=DANGER_SAFE,
        resolver=species_dashboard,
    ),
    Action(
        name="species_backfill",
        description=(
            "What the species backfill would do, without doing it. automatic: lot names with no species "
            "that match exactly one species (propose backfill_species). review: the names it can't settle, "
            "grouped into questions with their candidates (propose set_species_on_lot_names, "
            "remember_not_a_species, or add_species first). Commonest names first."
        ),
        params={
            "pass": "string, optional, default automatic. automatic or review.",
            "auction": _AUCTION,
            "scan": f"integer, optional, default {DEFAULT_SCAN}, at most {MAX_SCAN}. How many of the commonest names to match.",
            "include_unmatched": "boolean, optional, default false. review: also names nothing matches, usually a new species.",
            "min_lots": "integer, optional, default 2. review: ignore names on fewer lots.",
            "limit": "integer, optional, default 15.",
            "offset": "integer, optional, default 0.",
        },
        danger=DANGER_SAFE,
        resolver=species_backfill,
    ),
]
