"""AR lot-scanning service — overlay/card metadata, observation ingestion, and position payloads.

Shared by the mobile ``ar/lots``, ``ar/observations`` and ``ar/positions`` endpoints and the web
admin lot-map page, so scanning attendees and admins always see the same numbers. The app is a dumb
sensor + display: it sends angle measurements and renders overlays from the metadata here; all
fusion lives in :mod:`auctions.ar_mapping`.
"""

import logging

from django.core.cache import cache
from django.utils import timezone

from auctions.models import Lot, LotObservation, LotPosition, Watch

logger = logging.getLogger(__name__)

# Batch/call limits (also enforced in the input serializer so a violation is a clean 400).
MAX_LOTS_PER_METADATA_CALL = 50
MAX_FRAMES_PER_BATCH = 50
MAX_DETECTIONS_PER_FRAME = 10

# Sanity bounds — a detection outside these is dropped (never 400s the batch); buyers scan stray
# labels and phones report junk, and one bad detection must not lose a good frame.
BEARING_ABS_MAX = 90.0
DEPRESSION_ABS_MAX = 90.0

# Recommended-lot set is an expensive ordering query; cache the pk set per (user, auction).
RECOMMENDED_QTY = 25
RECOMMENDED_CACHE_SECONDS = 300


AR_DIRTY_REGISTRY_KEY = "ar_dirty_auctions"


def ar_dirty_key(auction_pk):
    """Cache key the observations endpoint sets and the ``update_ar_positions`` beat task consumes."""
    return f"ar_dirty_{auction_pk}"


def mark_auction_dirty(auction):
    """Flag an auction for the next solver pass — a per-auction flag plus a registry set the beat
    task drains (so it need not enumerate cache keys). The command also has a DB safety net, so the
    tiny read-modify-write race on the registry only ever costs a one-cycle delay."""
    cache.set(ar_dirty_key(auction.pk), True, timeout=None)
    registry = cache.get(AR_DIRTY_REGISTRY_KEY) or set()
    if auction.pk not in registry:
        cache.set(AR_DIRTY_REGISTRY_KEY, set(registry) | {auction.pk}, timeout=None)


def drain_dirty_auction_pks():
    """Return the flagged auction pks and clear the registry + per-auction flags."""
    registry = cache.get(AR_DIRTY_REGISTRY_KEY) or set()
    cache.set(AR_DIRTY_REGISTRY_KEY, set(), timeout=None)
    for pk in registry:
        cache.delete(ar_dirty_key(pk))
    return set(registry)


def _recommended_pks(user, auction):
    """Set of recommended lot pks for (user, auction), cached 5 min (too costly to run per scan).

    ``get_recommended_lots`` returns an already-sliced queryset, so iterate for pks rather than
    chaining ``.values_list`` (disallowed after a slice). Failures degrade to "nothing recommended"
    — a scan overlay must never 500 on the recommender.
    """
    if not user or not user.is_authenticated:
        return set()
    key = f"ar_recommended_{user.pk}_{auction.pk}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from auctions.filters import get_recommended_lots

    try:
        pks = {lot.pk for lot in get_recommended_lots(user=user, auction=auction.slug, qty=RECOMMENDED_QTY)}
    except Exception:
        logger.exception(
            "AR recommended-lot lookup failed for user %s auction %s", getattr(user, "pk", None), auction.pk
        )
        pks = set()
    cache.set(key, pks, RECOMMENDED_CACHE_SECONDS)
    return pks


def _label_fields(lot):
    """The auction's *custom* label fields for this lot, in ``label_print_fields`` order, skipping
    empties. Reuses the existing Lot properties rather than re-deriving display strings."""
    auction = lot.auction
    order = [token.strip() for token in (auction.label_print_fields or "").split(",")]
    # token -> (label, value); value pulled from the same properties the label PDF renders.
    candidates = {
        "custom_field_1": (auction.custom_field_1_name, lot.custom_field_1),
        "custom_checkbox_label": (auction.custom_checkbox_name, lot.custom_checkbox_label),
        "custom_dropdown_label": (auction.custom_dropdown_name, lot.custom_dropdown_label),
    }
    fields = []
    for token in order:
        if token in candidates:
            label, value = candidates[token]
            if value:  # skip fields whose per-lot value is empty
                fields.append({"label": label or "", "value": str(value)})
    return fields


def _thumbnail_url(lot, request):
    thumb = lot.thumbnail
    url = thumb.thumbnail_url if thumb else None
    return request.build_absolute_uri(url) if url else None


def build_lot_metadata(auction, pks, user, request):
    """Overlay + card metadata for the scanned ``pks`` (already capped to 50) in ``auction``.

    Returns rows in the same order as ``pks``. A lot in another auction → ``in_auction: false`` with
    name/thumbnail only; an unknown/deleted pk → ``in_auction: false, removed: true, name: null``.
    """
    # select_related the FKs Lot.sold / lot_link touch so a 50-pk scan is a couple of queries, not N.
    lots = {
        lot.pk: lot
        for lot in Lot.objects.filter(pk__in=pks, is_deleted=False).select_related(
            "auction", "user", "winner", "auctiontos_winner"
        )
    }
    watched = (
        set(Watch.objects.filter(user=user, lot_number_id__in=pks).values_list("lot_number_id", flat=True))
        if user and user.is_authenticated
        else set()
    )
    recommended = _recommended_pks(user, auction)
    has_position = set(LotPosition.objects.filter(lot_id__in=pks).values_list("lot_id", flat=True))

    rows = []
    for pk in pks:
        lot = lots.get(pk)
        if lot is None:
            # Unknown or deleted pk.
            rows.append({"pk": pk, "in_auction": False, "removed": True, "name": None})
            continue
        if lot.auction_id != auction.pk:
            # A stray label from a different auction: neutral chip, no observations.
            rows.append(
                {
                    "pk": pk,
                    "in_auction": False,
                    "removed": False,
                    "name": lot.lot_name,
                    "thumbnail_url": _thumbnail_url(lot, request),
                }
            )
            continue
        rows.append(
            {
                "pk": pk,
                "in_auction": True,
                "lot_number": str(lot.lot_number_display),
                "name": lot.lot_name,
                "thumbnail_url": _thumbnail_url(lot, request),
                "watched": pk in watched,
                "recommended": pk in recommended,
                "sold": lot.sold,
                "removed": bool(lot.banned or lot.deactivated),
                "lot_url": lot.lot_link,
                "label_fields": _label_fields(lot),
                "has_position": pk in has_position,
            }
        )
    return rows


def ingest_observations(auction, user, session_id, fov_hdeg, frames):
    """Turn a validated observation batch into LotObservation rows; returns the accepted count.

    Detections referencing a lot not live in this auction, or with out-of-range angles/quality, are
    silently dropped (never fail the batch). ``captured_at`` is clamped to ``now``. Sets the dirty
    flag when anything was accepted so the solver picks the auction up.
    """
    now = timezone.now()
    fov_calibrated = fov_hdeg is not None

    # Lot pks referenced anywhere in the batch that are actually live in this auction.
    referenced = {det["lot"] for frame in frames for det in frame["detections"]}
    valid_pks = set(
        Lot.objects.filter(pk__in=referenced, auction=auction, is_deleted=False, banned=False).values_list(
            "pk", flat=True
        )
    )

    to_create = []
    for frame in frames:
        captured_at = frame["captured_at"]
        if captured_at > now:
            captured_at = now  # client clock ahead of us
        for det in frame["detections"]:
            lot_pk = det["lot"]
            if lot_pk not in valid_pks:
                continue
            bearing = det["bearing_deg"]
            depression = det["depression_deg"]
            quality = det.get("quality", 1.0)
            if not (-BEARING_ABS_MAX <= bearing <= BEARING_ABS_MAX):
                continue
            if not (-DEPRESSION_ABS_MAX <= depression <= DEPRESSION_ABS_MAX):
                continue
            if not (0 < quality <= 1):
                continue
            to_create.append(
                LotObservation(
                    auction=auction,
                    lot_id=lot_pk,
                    user=user if user and user.is_authenticated else None,
                    session_id=session_id,
                    frame_id=frame["frame_id"],
                    captured_at=captured_at,
                    bearing_deg=bearing,
                    depression_deg=depression,
                    quality=quality,
                    fov_calibrated=fov_calibrated,
                )
            )

    if to_create:
        LotObservation.objects.bulk_create(to_create)
        mark_auction_dirty(auction)
    return len(to_create)


def positions_payload(auction, *, include_lot_details=False):
    """Positions for the auction's not-sold, not-removed lots, plus coverage counters.

    ``include_lot_details`` (admin map) adds ``lot_number``/``name`` to each position row and a full
    ``lots`` list (every unsold lot, with ``has_position``) for the locate search.
    """
    positions_by_lot = {p.lot_id: p for p in LotPosition.objects.filter(auction=auction)}
    # updated_at is the latest across ALL of the auction's positions (spec), even sold ones not yet
    # cleaned by the solver.
    updated_at = max((p.updated_at for p in positions_by_lot.values()), default=None)

    # Unsold + not-removed, filtered in SQL (winning_price__isnull mirrors the app's total_unsold_lots
    # convention and avoids an N+1 from the Lot.sold property's winner/auctiontos_winner FK lookups).
    unsold = list(
        Lot.objects.filter(
            auction=auction, is_deleted=False, banned=False, deactivated=False, winning_price__isnull=True
        )
    )

    positions = []
    unsold_list = []
    unsold_with_position = 0
    for lot in unsold:
        pos = positions_by_lot.get(lot.pk)
        has_pos = pos is not None
        if has_pos:
            unsold_with_position += 1
            row = {"lot": lot.pk, "x": pos.x, "y": pos.y, "confidence": pos.confidence}
            if include_lot_details:
                row["lot_number"] = str(lot.lot_number_display)
                row["name"] = lot.lot_name
            positions.append(row)
        if include_lot_details:
            unsold_list.append(
                {
                    "pk": lot.pk,
                    "lot_number": str(lot.lot_number_display),
                    "name": lot.lot_name,
                    "has_position": has_pos,
                }
            )

    payload = {
        "updated_at": updated_at.isoformat() if updated_at else None,
        "positions": positions,
        "unsold_total": len(unsold),
        "unsold_with_position": unsold_with_position,
    }
    if include_lot_details:
        payload["lots"] = unsold_list
    return payload


def clear_positions(auction):
    """Wipe an auction's AR data (admin "clear all locations"). Returns (observations, positions)."""
    obs_deleted, _ = LotObservation.objects.filter(auction=auction).delete()
    pos_deleted, _ = LotPosition.objects.filter(auction=auction).delete()
    cache.delete(ar_dirty_key(auction.pk))
    return obs_deleted, pos_deleted
