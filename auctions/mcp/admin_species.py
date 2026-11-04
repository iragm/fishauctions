"""The species list's upkeep on ``/mcp/admin/``: three reads, and nine changes only a proposal makes.

Everything on the species gaps page and in ``manage.py backfill_lot_species``, for an agent that can't
press a button or answer a prompt, plus fixing lots matched to the wrong species. The reads,
:func:`species_dashboard`, :func:`species_backfill` and :func:`lots_by_species`, write nothing (``record=False`` keeps even the cache's hit counter still). Each change is an
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

#: Remembered answers one ``forget_species_answer`` step may forget.
MAX_FORGET = 1000

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


def _gaps(limit, offset, verdict=None):
    # Filtered, every name is read anyway, so the whole list is kept and counted.
    rows = species_admin.gap_rows(limit=offset + limit if verdict is None else None, verdict=verdict)
    total = len(rows) if verdict else None
    rows = rows[offset : offset + limit]
    return [
        {
            "lot_name": untrusted_short(row["lot_name"]),
            "lots": row["lots"],
            "bred": row["bred"],
            "newest": row["newest"].date().isoformat() if row["newest"] else None,
            "verdict": row["verdict_key"],
            "verdict_detail": row["verdict_detail"],
            "answer": row["answer"],
        }
        for row in rows
    ], total


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
        "backlog_by_verdict": species_admin.backlog(),
    }


def species_dashboard(request, params: dict[str, Any]) -> dict[str, Any]:
    """The species gaps page as data, one section at a time, every row with the number a change needs."""
    _superuser(request)
    section = _str(params, "section", "summary").lower().replace(" ", "_")
    if section not in SECTIONS:
        return _need("Which section? One of: " + ", ".join(SECTIONS) + ".")
    verdict = _str(params, "verdict").lower().replace(" ", "_") or None
    if verdict and (section != "gaps" or verdict not in species_admin.VERDICTS):
        return _need("verdict only filters the gaps section, and is one of: " + ", ".join(species_admin.VERDICTS) + ".")
    if section == "summary":
        numbers = _summary()
        settled = numbers["backlog_by_verdict"]["not_a_species_by_a_person"]["lots"]
        return _ok(
            f"{numbers['lots_without_species']} lots have no species ({numbers['percent_done']}% done), "
            f"{settled} of them settled as not a species by a person; "
            f"{numbers['pending']} species waiting for approval, {numbers['duplicate_pairs']} possible "
            f"duplicate pairs, {numbers['retired']} retired pairings.",
            **numbers,
        )
    limit, offset = palette_actions._slice(params)
    verdict = _str(params, "verdict").lower().replace(" ", "_") or None
    if verdict and (section != "gaps" or verdict not in species_admin.VERDICTS):
        return _need("verdict only filters the gaps section, and is one of: " + ", ".join(species_admin.VERDICTS) + ".")
    rows, total = {
        "gaps": lambda limit, offset: _gaps(limit, offset, verdict),
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
    skip = max(0, _int(params, "skip") or 0)
    limit, offset = palette_actions._slice(params)
    which = _str(params, "pass", "automatic").lower()
    if which == "automatic":
        matches = []
        for row in species_admin.name_counts(lots, scan, skip=skip):
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
        result = {"pass": which, "names_skipped": skip, "names_scanned": scan, "matches": found, "total": total}
        if offset + limit < total:
            result["next_offset"] = offset + limit
        return _ok(
            f"{total} of {scan} lot names without a species"
            + (f" (after the {skip} commonest)" if skip else " (the commonest)")
            + " match exactly one species."
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
            skip=skip,
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
        result = {"pass": which, "names_skipped": skip, "names_scanned": scan, "questions": questions, "total": total}
        if offset + limit < total:
            result["next_offset"] = offset + limit
        return _ok(
            f"{total} names the matcher can't settle on its own."
            + palette_actions._showing(total, limit, offset)
            + " Answer each with set_species_on_lot_names, remember_not_a_species, or add_species first.",
            **result,
        )
    return _need("Which pass? automatic or review.")


def lots_by_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """Lot names already carrying a species, grouped by name and species, commonest first: what to read
    before proposing change_lots_species. By species number, by part of a name, or both."""
    from django.db.models import Count

    from auctions.models import Lot, Species

    _superuser(request)
    species = None
    if params.get("species") not in (None, ""):
        species, problem = _species(params)
        if problem:
            return _error(problem)
    name = _str(params, "name")
    if species is None and not name:
        return _need("Which lots? Give a species number, part of a lot name, or both.")
    lots = Lot.objects.filter(is_deleted=False, species__isnull=False)
    if species is not None:
        lots = lots.filter(species=species)
    if name:
        lots = lots.filter(lot_name__icontains=name)
    auction = _str(params, "auction")
    if auction:
        from auctions.models import Auction

        if not Auction.objects.filter(slug=auction).exists():
            return _error(f"No auction with slug {auction!r}.")
        lots = lots.filter(auction__slug=auction)
    grouped = lots.values("lot_name", "species").annotate(lots=Count("pk")).order_by("-lots", "lot_name")
    limit, offset = palette_actions._slice(params)
    total = grouped.count()
    page = list(grouped[offset : offset + limit])
    found = {row.pk: row for row in Species.objects.filter(pk__in={row["species"] for row in page})}
    rows = [
        {"lot_name": untrusted_short(row["lot_name"]), "lots": row["lots"], "species": _echo(found[row["species"]])}
        for row in page
        if row["species"] in found
    ]
    result = {"rows": rows, "total": total}
    if offset + limit < total:
        result["next_offset"] = offset + limit
    return _ok(
        f"{total} lot name and species pairs."
        + palette_actions._showing(total, limit, offset)
        + " Propose change_lots_species to move any of them to another species.",
        **result,
    )


# --- changes a proposal makes -------------------------------------------------------------------------


def _cache_rows(params):
    """``(rows, "")`` for a step's ``answer`` or ``answers``, or ``([], why not)``. Every number must still
    name a remembered answer, so a typo is caught when proposed."""
    from auctions.models import SpeciesSearchCache

    raw = params.get("answers")
    if raw in (None, "", []):
        raw = [params.get("answer")] if params.get("answer") not in (None, "") else []
    if not isinstance(raw, list):
        raw = [raw]
    numbers = []
    for value in raw:
        number = _int({"n": value}, "n")
        if number is None:
            return [], "answer / answers must be answer numbers from species_dashboard."
        numbers.append(number)
    numbers = list(dict.fromkeys(numbers))
    if not numbers:
        return [], "Which answer? Give answer, or answers as a list, from species_dashboard."
    if len(numbers) > MAX_FORGET:
        return [], f"At most {MAX_FORGET} answers in one step; split it."
    rows = list(SpeciesSearchCache.objects.filter(pk__in=numbers).select_related("species"))
    missing = sorted(set(numbers) - {row.pk for row in rows})
    if missing:
        return [], f"There is no remembered answer number {missing[0]} (species_dashboard's answer)."
    return rows, ""


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
    rows, problem = _cache_rows(params)
    if problem:
        return _error(problem)
    names = [species_admin.forget(row) for row in rows]
    if len(names) == 1:
        return _ok(f"Forgot the remembered answer for {untrusted_short(names[0])}. It will be looked up again.")
    return _ok(f"Forgot {len(names)} remembered answers. They will be looked up again.")


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


def _to_species(params):
    """The species lots move to: ``(species, "")``, ``(None, "")`` for "no species", or ``(None, why not)``."""
    raw = params.get("to")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "none", "null", "no species")):
        if "to" not in params:
            return None, "to must be a species number, or none to take the species off."
        return None, ""
    return _species(params, "to")


def _move(params):
    """``(lots, to, names, "")`` for a change_lots_species step, or ``(None, None, [], why not)``."""
    source, problem = _species(params, "species")
    if problem:
        return None, None, [], problem
    to, problem = _to_species(params)
    if problem:
        return None, None, [], problem
    if to is not None and to.pk == source.pk:
        return None, None, [], "species and to are the same species."
    names = []
    if params.get("lot_names") not in (None, "", []):
        names, problem = _lot_names(params, species_admin.MAX_REMEMBERED)
        if problem:
            return None, None, [], problem
    try:
        lots = species_admin.lots_on_species(source, names, _str(params, "auction") or None)
    except LookupError as error:
        return None, None, [], str(error)
    if not names and not _str(params, "auction") and to is None:
        return None, None, [], "Taking a species off every lot needs lot_names or an auction."
    return lots, to, names, ""


def change_lots_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """Find the lots with one species and give them another, or none: the fix for a wrong match."""
    _superuser(request)
    lots, to, names, problem = _move(params)
    if problem:
        return _error(problem)
    source = _species(params, "species")[0]
    count, refiled, untaught = species_admin.move_lots(
        lots,
        source,
        to,
        names=names,
        set_category=bool(palette_actions._flag(params, "set_category")),
        teach=bool(names) and palette_actions._flag(params, "remember") is not False,
        user=request.user,
    )
    summary = f"{count} lot(s) moved to {to.label if to else 'no species'}."
    if refiled:
        summary += f" {refiled} were filed under {to.category}."
    if untaught:
        summary += (
            f" {len(untaught)} name(s) were not remembered as the new answer (a species not yet approved, a "
            "retired pairing, or a name the list already answers), so they will be looked up again."
        )
    return _ok(summary, lots=count, species=_echo(to) if to else None)


def remember_not_a_species(request, params: dict[str, Any]) -> dict[str, Any]:
    """The reviewed pass's "not a species": nothing asks about these names again."""
    _superuser(request)
    names, problem = _lot_names(params, species_admin.MAX_NOT_A_SPECIES)
    if problem:
        return _error(problem)
    written = species_admin.remember_not_a_species(names, user=request.user, most=species_admin.MAX_NOT_A_SPECIES)
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
        params={
            "answer": "integer. One answer number from species_dashboard.",
            "answers": f"array of integer. Several answer numbers instead, at most {MAX_FORGET}.",
        },
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
        params={"lot_names": f"{_LOT_NAMES}, at most {species_admin.MAX_NOT_A_SPECIES}."},
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Remember as not a species",
        resolver=remember_not_a_species,
    ),
    Action(
        name="change_lots_species",
        description=(
            "Fix lots matched to the wrong species: every lot carrying species, or only those called one "
            "of lot_names, gets to instead (none takes the species off). Named lot names are remembered "
            "as the new answer. Read lots_by_species first."
        ),
        params={
            "species": "integer, required. The species number the lots carry now.",
            "to": "integer or none, required. The species number they should carry, or none for no species.",
            "lot_names": (
                f"array of string, optional. Only lots called one of these, exactly as lots_by_species gave "
                f"them, at most {species_admin.MAX_REMEMBERED}. Default: every lot with the species."
            ),
            "auction": "string, optional. Only lots in this auction, by slug.",
            "set_category": _SET_CATEGORY,
            "remember": "boolean, optional, default true. false moves the lots without teaching the matcher.",
        },
        danger=DANGER_CONFIRM,
        idempotent=True,
        confirm_template="Change the species on lots",
        resolver=change_lots_species,
    ),
]


#: Checked when proposed; each runs again inside the change on approval.
PROPOSAL_CHECKS = {
    "approve_species": lambda arguments: _species(arguments)[1],
    "merge_species": lambda arguments: _species(arguments, "keep")[1] or _species(arguments, "duplicate")[1],
    "dismiss_species_duplicate": lambda arguments: _species(arguments)[1],
    "forget_species_answer": lambda arguments: _cache_rows(arguments)[1],
    "allow_species_pairing_again": lambda arguments: _rejection(arguments)[1],
    "backfill_species": lambda arguments: _lot_names(arguments, MAX_BACKFILL_NAMES)[1] or _lots(arguments)[1],
    "set_species_on_lot_names": lambda arguments: (
        _species(arguments)[1] or _lot_names(arguments, species_admin.MAX_REMEMBERED)[1] or _lots(arguments)[1]
    ),
    "remember_not_a_species": lambda arguments: _lot_names(arguments, species_admin.MAX_NOT_A_SPECIES)[1],
    "change_lots_species": lambda arguments: _move(arguments)[3],
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
            "verdict": (
                "string, optional. gaps only: just the names with this verdict, read from every name, not "
                "only the commonest. One of: " + ", ".join(species_admin.VERDICTS) + "."
            ),
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
            "skip": "integer, optional, default 0. Start the scan this many names down the list, to reach the tail.",
            "include_unmatched": "boolean, optional, default false. review: also names nothing matches, usually a new species.",
            "min_lots": "integer, optional, default 2. review: ignore names on fewer lots.",
            "limit": "integer, optional, default 15.",
            "offset": "integer, optional, default 0.",
        },
        danger=DANGER_SAFE,
        resolver=species_backfill,
    ),
    Action(
        name="lots_by_species",
        description=(
            "Lots that already carry a species, grouped by lot name and species, commonest first: by species "
            "number, by part of a lot name, or both. Read before proposing change_lots_species."
        ),
        params={
            "species": "integer, optional. Only lots with this species number.",
            "name": "string, optional. Only lot names containing this.",
            "auction": "string, optional. Only lots in this auction, by slug.",
            "limit": "integer, optional, default 15.",
            "offset": "integer, optional, default 0.",
        },
        danger=DANGER_SAFE,
        resolver=lots_by_species,
    ),
]


#: The species list's own steps. A proposal made only of these may be bigger (``admin.MAX_SPECIES_STEPS``).
SPECIES_STEPS = frozenset(action.name for action in APPROVAL_ONLY)


def _names_text(names) -> str:
    # Sellers' words, fenced as everywhere on this endpoint: agents read this page too.
    return ", ".join(untrusted_short(name) for name in names)


def _species_text(params, key="species") -> str:
    species = _species(params, key)[0]
    return species.label if species else "(a species that no longer exists)"


def _work(step) -> int:
    """Names or answers one step acts on, for the cap on a whole proposal (:data:`MAX_PROPOSAL_ITEMS`)."""
    arguments = step.get("arguments") or {}
    if not isinstance(arguments, dict):
        return 1
    items = arguments.get("lot_names") or arguments.get("answers") or []
    return len(items) if isinstance(items, list) else 1


#: Names and answers one proposal of species steps may touch: about a thousand, which the owner reads
#: in one sitting and one approval runs within a request.
MAX_PROPOSAL_ITEMS = 1200


def proposal_problem(steps) -> str:
    """Why a proposal of species steps is too much for one approval, or ``""``."""
    total = sum(_work(step) for step in steps)
    if total > MAX_PROPOSAL_ITEMS:
        return f"Those steps name {total} lot names and answers; one proposal takes at most {MAX_PROPOSAL_ITEMS}. Split it."
    return ""


def describe_step(tool: str, arguments: dict[str, Any]) -> str | None:
    """One step of a proposal as the approver reads it: names, not numbers. ``None`` for a step that isn't
    the species list's, so the page shows its arguments instead."""
    from auctions.models import SpeciesNameRejection, SpeciesSearchCache

    if tool not in SPECIES_STEPS:
        return None
    auction = _str(arguments, "auction")
    where = f" in auction {auction}" if auction else ""
    category = " and file them under its category" if palette_actions._flag(arguments, "set_category") else ""
    teaches = palette_actions._flag(arguments, "remember") is not False
    if tool == "approve_species":
        return f"Approve {_species_text(arguments)} for everyone."
    if tool == "merge_species":
        return (
            f"Merge {_species_text(arguments, 'duplicate')} into {_species_text(arguments, 'keep')} "
            "(its lots and names move over; not reversible)."
        )
    if tool == "dismiss_species_duplicate":
        return f"{_species_text(arguments)} is not a duplicate."
    if tool == "forget_species_answer":
        raw = arguments.get("answers") or [arguments.get("answer")]
        numbers = [_int({"n": value}, "n") for value in (raw if isinstance(raw, list) else [raw])]
        rows = list(
            SpeciesSearchCache.objects.filter(pk__in=[n for n in numbers if n is not None]).select_related("species")
        )
        said = []
        for row in rows:
            _key, verdict, detail = species_admin.verdict_of(row)
            remembered = row.species.label if row.species_id else ", ".join(filter(None, (verdict, detail)))
            said.append(f"{untrusted_short(row.search_text)} (now: {remembered})")
        gone = len(set(numbers)) - len(rows)
        text = "Look these up again: " + (", ".join(said) or "nothing") + "."
        if gone:
            text += f" {gone} of the answers no longer exist, so this step will fail."
        return text
    if tool == "allow_species_pairing_again":
        row = SpeciesNameRejection.objects.filter(pk=_int(arguments, "pairing")).select_related("species").first()
        if row is None:
            return "Allow a retired pairing again (it no longer exists)."
        return f"Let {untrusted_short(row.search_text)} match {row.species.label} again."
    names = _lot_names(arguments, species_admin.MAX_NOT_A_SPECIES)[0]
    if tool == "backfill_species":
        return f"Give the one matching species to lots{where} named {_names_text(names)}{category}."
    if tool == "set_species_on_lot_names":
        taught = ", and teach the matcher these names" if teaches else ""
        return f"{_species_text(arguments)} on lots{where} named {_names_text(names)}{category}{taught}."
    if tool == "remember_not_a_species":
        return f"Not a species, never looked up again: {_names_text(names)}."
    if tool == "change_lots_species":
        to, problem = _to_species(arguments)
        target = "(a species that no longer exists)" if problem else (to.label if to else "no species")
        named = arguments.get("lot_names") not in (None, "", [])
        which = f"named {_names_text(names)}" if named else "(all of them)"
        taught = ", and teach the matcher these names" if named and teaches else ""
        return f"Lots with {_species_text(arguments)}{where} {which} change to {target}{category}{taught}."
    return None
