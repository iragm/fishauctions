"""AR lot scanning: overlay and card metadata, observation ingestion, and position payloads.

Shared by the mobile ``ar/lots``, ``ar/observations`` and ``ar/positions`` endpoints and the admin
lot-map page. The app is a sensor and a display; the fusion is in :mod:`auctions.ar_mapping`.
"""

import datetime
import logging

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from auctions.models import Auction, Lot, LotObservation, LotPosition, PageView, Watch

logger = logging.getLogger(__name__)

# Also enforced in the serializer, so a violation is a clean 400.
MAX_LOTS_PER_METADATA_CALL = 50
MAX_FRAMES_PER_BATCH = 50
MAX_DETECTIONS_PER_FRAME = 10

# A detection outside these is dropped, never a 400: buyers scan stray labels and phones report junk.
BEARING_ABS_MAX = 90.0
DEPRESSION_ABS_MAX = 90.0

# The recommended set is an expensive ordering query, cached per (user, auction).
RECOMMENDED_QTY = 25
RECOMMENDED_CACHE_SECONDS = 300

# AR interaction events become PageViews tagged with the mapped ``source``, so they count as lot
# views but can be broken out. De-duped per user, lot and event type, so counts are distinct users.
AR_EVENT_SOURCES = {"scanned": "ar_scan", "zoomed": "ar_zoom", "zoomed_full": "ar_zoom_full"}
AR_EVENT_TYPES = tuple(AR_EVENT_SOURCES)  # accepted "event" values in the payload
MAX_AR_EVENTS_PER_BATCH = 100

# "Locate with AR" only applies while an in-person auction is happening: from LOCATE_LEAD_TIME
# before the start until pretty_much_over.
LOCATE_LEAD_TIME = datetime.timedelta(hours=2)
# pretty_much_over's grace period, mirrored to pre-filter candidates in SQL.
LOCATE_GRACE = datetime.timedelta(hours=24)


AR_DIRTY_REGISTRY_KEY = "ar_dirty_auctions"


def ar_dirty_key(auction_pk):
    """Cache key the observations endpoint sets and the ``update_ar_positions`` task consumes."""
    return f"ar_dirty_{auction_pk}"


def mark_auction_dirty(auction):
    """Flag an auction for the next solver pass: a per-auction flag plus a registry set the task drains.

    The command has a DB safety net, so the registry's read-modify-write race costs one cycle.
    """
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


def locatable_auction_pks():
    """Auction pks whose lots may offer "locate with AR" now, in one query.

    In-person only, from ``LOCATE_LEAD_TIME`` before the start until :attr:`Auction.pretty_much_over`.
    The SQL is a superset pre-filter; ``pretty_much_over`` makes the final call so this can't drift.
    """
    now = timezone.now()
    grace_floor = now - LOCATE_GRACE
    candidates = Auction.objects.filter(
        Q(date_start__gte=grace_floor)
        | Q(date_online_bidding_ends__gte=grace_floor)
        | Q(lot_submission_end_date__gte=grace_floor),
        is_online=False,
        is_deleted=False,
        date_start__lte=now + LOCATE_LEAD_TIME,
    ).only("is_online", "date_start", "date_online_bidding_ends", "lot_submission_end_date")
    return {auction.pk for auction in candidates if not auction.pretty_much_over}


def _recommended_pks(user, auction):
    """The recommended lot pks for (user, auction), cached 5 minutes.

    ``get_recommended_lots`` returns a sliced queryset, so iterate for pks. Failures degrade to nothing
    recommended: an overlay must never 500 on the recommender.
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
    """The auction's custom label fields for this lot, in ``label_print_fields`` order, skipping empties."""
    auction = lot.auction
    order = [token.strip() for token in (auction.label_print_fields or "").split(",")]
    # token -> (label, value), from the same properties the label PDF renders.
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


def _image_url(lot, request):
    """The full-size primary image URL for the AR preview card, or None."""
    thumb = lot.thumbnail
    url = thumb.display_url if thumb else None
    return request.build_absolute_uri(url) if url else None


def build_lot_metadata(auction, pks, user, request):
    """Overlay and card metadata for the scanned ``pks`` in ``auction``, in the same order.

    A lot in another auction is ``in_auction: false``; an unknown pk is also ``removed: true``.
    """
    # select_related the FKs Lot.sold and lot_link touch, so a 50-pk scan is a couple of queries.
    lots = {
        lot.pk: lot
        for lot in Lot.objects.filter(pk__in=pks, is_deleted=False).select_related(
            # species__parent: a strain falls back to its parent's common name. See
            # Lot.common_name_line.
            "auction",
            "user",
            "winner",
            "auctiontos_winner",
            "species",
            "species__parent",
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
            # A stray label from another auction: neutral chip, no observations.
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
                # The other name, blank when there isn't one: the overlay draws it under the lot
                # name, and which is filled in depends on what the seller typed. See
                # Lot.scientific_name_line.
                "scientific_name": lot.scientific_name_line,
                "common_name": lot.common_name_line,
                "thumbnail_url": _thumbnail_url(lot, request),
                "image_url": _image_url(lot, request),
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

    Detections for lots not live in this auction, or with out-of-range angles, are dropped silently.
    ``captured_at`` is clamped to now. Sets the dirty flag when anything was accepted.
    """
    now = timezone.now()
    fov_calibrated = fov_hdeg is not None

    # Lot pks referenced in the batch that are live in this auction.
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
        frame_yaw = frame.get("yaw_deg")  # every detection row of a frame stores the frame's yaw
        # ...the frame's compass heading (the serializer dropped junk)...
        frame_heading = frame.get("heading_deg")
        # ...its GPS fix (the serializer nulled a bad or half one)...
        frame_lat = frame.get("latitude")
        frame_lon = frame.get("longitude")
        # ...and its dead-reckoning displacement, where (0, 0) is a valid origin.
        frame_odo_x = frame.get("odo_x_m")
        frame_odo_y = frame.get("odo_y_m")
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
                    yaw_deg=frame_yaw,
                    heading_deg=frame_heading,
                    latitude=frame_lat,
                    longitude=frame_lon,
                    odo_x_m=frame_odo_x,
                    odo_y_m=frame_odo_y,
                )
            )

    if to_create:
        LotObservation.objects.bulk_create(to_create)
        mark_auction_dirty(auction)
    return len(to_create)


def _client_ip(request):
    """Best-effort client IP (first X-Forwarded-For hop, else REMOTE_ADDR); '' when unknown."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or ""


def record_ar_events(auction, user, events, request):
    """Record AR interaction events as lot PageViews, returning the accepted count.

    Each becomes a ``PageView`` with a ``source`` from ``AR_EVENT_SOURCES``. Events for lots not live in
    this auction, or with unknown types, are dropped silently, and rows are de-duped per (user, lot,
    source) so counts are distinct users.
    """
    if not (user and user.is_authenticated):
        return 0  # AR endpoints are JWT-authed, so this is just defensive.

    # De-dupe the batch to (lot_pk, source) and collect the lot pks.
    wanted = set()
    lot_pks = set()
    for ev in events:
        source = AR_EVENT_SOURCES.get(ev.get("event"))
        lot_pk = ev.get("lot")
        if source and isinstance(lot_pk, int):
            wanted.add((lot_pk, source))
            lot_pks.add(lot_pk)
    if not wanted:
        return 0

    lots = {lot.pk: lot for lot in Lot.objects.filter(pk__in=lot_pks, auction=auction, is_deleted=False)}
    # Rows this user already has, so a re-scan doesn't double-count.
    existing = set(
        PageView.objects.filter(
            user=user, lot_number_id__in=lot_pks, source__in=set(AR_EVENT_SOURCES.values())
        ).values_list("lot_number_id", "source")
    )
    ip = _client_ip(request)

    to_create = []
    for lot_pk, source in wanted:
        lot = lots.get(lot_pk)
        if lot is None or (lot_pk, source) in existing:
            continue
        to_create.append(
            PageView(
                user=user,
                lot_number=lot,
                # The auction as well as the lot, as the browser beacon sends it: it costs nothing
                # here and lets a reader match on one indexed column. See base_page_view.html.
                auction=auction,
                source=source,
                url=(lot.lot_link or "")[:600],
                title=(lot.lot_name or "")[:600],
                ip_address=(ip[:100] or None),
            )
        )
    if to_create:
        PageView.objects.bulk_create(to_create)
    return len(to_create)


def positions_payload(auction, *, include_lot_details=False):
    """Positions for the auction's unsold, unremoved lots, plus coverage counters.

    ``include_lot_details`` (the admin map) adds lot numbers and names, and a full ``lots`` list for the
    locate search.
    """
    positions_by_lot = {p.lot_id: p for p in LotPosition.objects.filter(auction=auction)}
    # The latest across all of the auction's positions, sold ones included.
    updated_at = max((p.updated_at for p in positions_by_lot.values()), default=None)

    # Unsold and not removed, filtered in SQL (winning_price__isnull avoids the Lot.sold N+1).
    unsold = list(
        # auction as well as species: lot.scientific_name reads use_scientific_name.
        Lot.objects.filter(
            auction=auction, is_deleted=False, banned=False, deactivated=False, winning_price__isnull=True
        ).select_related("species", "species__parent", "auction")
    )

    positions = []
    unsold_list = []
    unsold_with_position = 0
    for lot in unsold:
        pos = positions_by_lot.get(lot.pk)
        has_pos = pos is not None
        if has_pos:
            unsold_with_position += 1
            row = {"lot": lot.pk, "x": pos.x, "y": pos.y, "confidence": pos.confidence, "component": pos.component}
            if include_lot_details:
                row["lot_number"] = str(lot.lot_number_display)
                row["name"] = lot.lot_name
                # The other name, as the overlay and printed label do it.
                row["scientific_name"] = lot.scientific_name_line
                row["common_name"] = lot.common_name_line
            positions.append(row)
        if include_lot_details:
            unsold_list.append(
                {
                    "pk": lot.pk,
                    "lot_number": str(lot.lot_number_display),
                    "name": lot.lot_name,
                    "scientific_name": lot.scientific_name_line,
                    "common_name": lot.common_name_line,
                    "has_position": has_pos,
                }
            )

    # Islands are connected components among the located lots: one island means a coherent map.
    island_count = len({row["component"] for row in positions})

    payload = {
        "updated_at": updated_at.isoformat() if updated_at else None,
        "positions": positions,
        "unsold_total": len(unsold),
        "unsold_with_position": unsold_with_position,
        "island_count": island_count,
    }
    if include_lot_details:
        payload["lots"] = unsold_list
    return payload


def clear_positions(auction):
    """Wipe an auction's AR data. Returns (observations, positions)."""
    obs_deleted, _ = LotObservation.objects.filter(auction=auction).delete()
    pos_deleted, _ = LotPosition.objects.filter(auction=auction).delete()
    cache.delete(ar_dirty_key(auction.pk))
    return obs_deleted, pos_deleted
