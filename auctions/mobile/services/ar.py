"""AR lot-scanning service — overlay/card metadata, observation ingestion, and position payloads.

Shared by the mobile ``ar/lots``, ``ar/observations`` and ``ar/positions`` endpoints and the web
admin lot-map page, so scanning attendees and admins always see the same numbers. The app is a dumb
sensor + display: it sends angle measurements and renders overlays from the metadata here; all
fusion lives in :mod:`auctions.ar_mapping`.
"""

import datetime
import logging

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from auctions.models import Auction, Lot, LotObservation, LotPosition, PageView, Watch

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

# AR interaction events (item: track who scanned / zoomed in / zoomed all the way in on a lot). Each
# becomes a PageView tagged with the mapped ``source`` so it's counted as a lot pageview but can be
# broken out separately on the lot page. The app posts these to POST /api/mobile/ar/events/; it should
# send each (lot, event) at most once per AR session — the server also de-dupes to one row per user per
# lot per event type, so a count is "number of distinct users who did X", never inflated by re-scans.
AR_EVENT_SOURCES = {"scanned": "ar_scan", "zoomed": "ar_zoom", "zoomed_full": "ar_zoom_full"}
AR_EVENT_TYPES = tuple(AR_EVENT_SOURCES)  # accepted "event" values in the payload
MAX_AR_EVENTS_PER_BATCH = 100

# "Locate with AR" entry points on lot lists only make sense while an in-person auction is actually
# happening: from LOCATE_LEAD_TIME before the start until pretty_much_over (24 h after wind-down).
LOCATE_LEAD_TIME = datetime.timedelta(hours=2)
# pretty_much_over's grace period, mirrored here only to pre-filter candidates in SQL.
LOCATE_GRACE = datetime.timedelta(hours=24)


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


def locatable_auction_pks():
    """Auction pks whose lots may offer "locate with AR" right now — one small query.

    In-person auctions only (there is nothing to walk to at an online one), from
    ``LOCATE_LEAD_TIME`` before the start until :attr:`Auction.pretty_much_over`.

    The SQL half is a superset pre-filter: ``wind_down_time`` is the max of three dates, so an
    auction can only still be inside the 24 h grace period if at least one of them is. The final
    call is left to ``pretty_much_over`` in Python so this can't drift from the property — the
    pre-filtered set is a handful of rows (in-person auctions from roughly the last day).
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


def _image_url(lot, request):
    """Full-size (not the 250x150 thumbnail) primary image URL, for the AR preview card that renders
    the picture fit-to-width. None when the lot has no image."""
    thumb = lot.thumbnail
    url = thumb.display_url if thumb else None
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
            "auction", "user", "winner", "auctiontos_winner", "species"
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
                # The seller's scientific name, blank when they didn't pick one.  The overlay draws
                # it under the lot name -- it is the one piece of a lot that is worth reading in a
                # room where you can't get close enough to read the label.
                "scientific_name": lot.scientific_name,
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
        frame_yaw = frame.get("yaw_deg")  # every detection row of a frame stores the frame's yaw
        # …the frame's absolute compass heading (serializer already dropped/normalized junk)…
        frame_heading = frame.get("heading_deg")
        # …the frame's GPS fix (serializer already nulled a bad/half/(0,0) fix)…
        frame_lat = frame.get("latitude")
        frame_lon = frame.get("longitude")
        # …and the frame's cumulative dead-reckoning displacement (serializer already dropped a
        # half/junk pair; (0, 0) is a valid origin here, not a sentinel).
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
    """Record AR interaction events (scan / zoom-in / zoom-all-the-way) as lot PageViews.

    Each accepted event becomes a ``PageView`` with ``source`` in ``AR_EVENT_SOURCES.values()`` so it
    is counted among the lot's page views but can be listed separately on the lot page. Events for a
    lot not live in this auction, or with an unknown event type, are dropped silently (never fail the
    batch). De-duped to one row per (user, lot, source) so the per-source counts are "distinct users
    who did X" and a user re-scanning the same lot never inflates them. Returns the accepted count.
    """
    if not (user and user.is_authenticated):
        return 0  # AR endpoints are JWT-authed, so this is just defensive.

    # De-dupe the batch to (lot_pk, source) and collect the referenced lot pks.
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
    # Rows this user already has for these lots — skip so a re-scan/re-zoom doesn't double-count them.
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
        ).select_related("species")
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
                row["scientific_name"] = lot.scientific_name
            positions.append(row)
        if include_lot_details:
            unsold_list.append(
                {
                    "pk": lot.pk,
                    "lot_number": str(lot.lot_number_display),
                    "name": lot.lot_name,
                    "scientific_name": lot.scientific_name,
                    "has_position": has_pos,
                }
            )

    # Islands = distinct connected components among the located lots shown on the map. Disconnected
    # scanning walks that never linked up form separate islands; the count tells admins/attendees how
    # fragmented the map still is (one island ⇒ a single coherent layout).
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
    """Wipe an auction's AR data (admin "clear all locations"). Returns (observations, positions)."""
    obs_deleted, _ = LotObservation.objects.filter(auction=auction).delete()
    pos_deleted, _ = LotPosition.objects.filter(auction=auction).delete()
    cache.delete(ar_dirty_key(auction.pk))
    return obs_deleted, pos_deleted
