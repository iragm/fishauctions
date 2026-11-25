"""Readers flagging a lot's scientific name as wrong, and the seller's answer.

A signed-in reader who isn't the seller presses "Report incorrect species" on the lot page. Nobody is
emailed or notified: the seller (or an auction admin) only sees, on that same page, one line saying
other people flagged it, with **Fix** (pick another species, through the same code as
``set_lot_species``, so the lot's category, its species vote and, for an auction admin, the remembered
answer all follow) and **Label correct** (stamps the open flags). A flag names the species the lot had
when it was made, so a lot whose species changed by any route has nothing open. Superusers read every
flag on ``/mcp/admin/`` (``species_flags``).

Fixing follows the lot's edit rule loosened by one step: the seller may fix after lot submission closes
but not once a bid is in or the lot sold (a different species can mean different breeder points);
auction admins always may.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from auctions.models import SpeciesFlag


def _signed_in(user) -> bool:
    return bool(user and user.is_authenticated)


def is_admin(lot, user) -> bool:
    return bool(_signed_in(user) and lot.auction_id and lot.auction.permission_check(user))


def can_settle(lot, user) -> bool:
    """The seller or an auction admin: the two who see the note and its buttons."""
    return _signed_in(user) and (lot.is_owned_by(user) or is_admin(lot, user))


def can_fix(lot, user) -> bool:
    if is_admin(lot, user):
        return True
    return lot.is_owned_by(user) and not lot.cannot_change_reason


def can_report(lot, user) -> bool:
    return bool(_signed_in(user) and lot.species_id and not lot.is_deleted and not lot.is_owned_by(user))


def open_flags(lot):
    """Flags on the species the lot carries now that nobody has called correct."""
    if not lot.species_id:
        return SpeciesFlag.objects.none()
    return SpeciesFlag.objects.filter(lot=lot, species_id=lot.species_id, confirmed_on__isnull=True)


def has_reported(lot, user) -> bool:
    return bool(
        _signed_in(user)
        and lot.species_id
        and SpeciesFlag.objects.filter(lot=lot, user=user, species_id=lot.species_id).exists()
    )


def report(lot, user) -> bool:
    """Flag the lot's current species for *user*. ``False`` when they can't, or already did."""
    if not can_report(lot, user):
        return False
    try:
        with transaction.atomic():
            _flag, created = SpeciesFlag.objects.get_or_create(lot=lot, user=user, species_id=lot.species_id)
    except IntegrityError:
        return False
    return created


def confirm(request, lot) -> int:
    """ "Label correct": settle the open flags, and let an auction admin's answer teach the matcher, as
    ``set_lot_species`` does. How many flags it settled."""
    from auctions.palette_actions import _lot_history, _teach_the_lot_name

    user = request.user
    if not can_settle(lot, user):
        return 0
    settled = open_flags(lot).update(confirmed_on=timezone.now(), confirmed_by=user)
    if settled:
        _teach_the_lot_name(lot, lot.species, user, is_admin(lot, user))
        _lot_history(request, lot, f"Kept the species on lot {lot.lot_number_display} after it was flagged")
    return settled


def fix(request, lot, typed: str) -> dict:
    """ "Fix": put the species *typed* names on the lot, through ``set_lot_species``'s own code."""
    from auctions.palette_actions import _error, apply_lot_species

    user = request.user
    if not can_fix(lot, user):
        return _error(str(lot.cannot_change_reason or "Only the seller or an auction admin can change this."))
    if not typed.strip():
        return _error("Type the scientific or common name.")
    return apply_lot_species(request, lot, is_admin(lot, user), {"species": typed})


def context(lot, user) -> dict:
    """What the lot page's species-flag fragment needs for this viewer."""
    settle = can_settle(lot, user)
    flagged = settle and lot.species_id and open_flags(lot).exists()
    return {
        "species_flag_lot": lot,
        "species_flagged": bool(flagged),
        "species_can_fix": bool(flagged and can_fix(lot, user)),
        "species_can_report": can_report(lot, user),
        "species_reported": has_reported(lot, user),
    }


def status(flag, lot_species_id) -> str:
    """open, confirmed (called correct), or changed (the lot carries another species now)."""
    if flag.confirmed_on:
        return "confirmed"
    if flag.species_id != lot_species_id:
        return "changed"
    return "open"
