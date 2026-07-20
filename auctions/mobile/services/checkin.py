"""Proximity check-in & welcome service.

The app POSTs the phone's position to ``checkin/ping/`` while the WebView shell is up (mount,
app-resume, every 10 min). The server owns all the logic: it evaluates the geofence + welcome window
+ join/check-in/admin state, performs the auto-check-in itself, and returns display-ready actions the
app renders (bottom sheet / snackbar / dialog). All copy comes from here.

Also backs the ``checkin/join/`` and ``checkin/set-location/`` mutations. Every mutation lands in the
auction history.
"""

import logging

from django.utils import timezone

from auctions.models import AuctionTOS, CheckinNudge, PickupLocation, distance_to

logger = logging.getLogger(__name__)

# Geofence radii, miles. 500 ft ≈ 0.095 mi for the welcome/check-in nudge; the admin location-fix
# offer uses a generous 2 mi because the whole point is that the stored location may be wrong.
WELCOME_RADIUS_MI = 0.095
ADMIN_RADIUS_MI = 2.0
# distance_to CEILING-rounds to this bucket (a privacy feature); 0.005 mi (~26 ft) is fine enough for
# a 500 ft geofence without exposing an exact distance.
DISTANCE_RESOLUTION_MI = 0.005


def _single_pickup_location(auction):
    """The auction's one physical (non-mail) pickup location, or None unless exactly one exists."""
    locations = list(auction.location_qs.exclude(pickup_by_mail=True))
    return locations[0] if len(locations) == 1 else None


def _find_and_bind_tos(user, auction):
    """The user's AuctionTOS for this auction, matched by user FK or (added-by-email) by email.

    An email-matched row with no user is bound to this user now — the same claim the web join does."""
    tos = AuctionTOS.objects.filter(auction=auction, user=user).first()
    if tos:
        return tos
    if user.email:
        tos = AuctionTOS.objects.filter(auction=auction, email__iexact=user.email).first()
        if tos:
            if tos.user_id is None:
                tos.user = user
                tos.save(update_fields=["user"])
            return tos
    return None


def _record_nudge(user, auction, kind):
    """Create the one-shot nudge row; return True only the first time (so we don't re-nudge)."""
    _, created = CheckinNudge.objects.get_or_create(user=user, auction=auction, kind=kind)
    return created


def _rules_url(auction):
    return auction.get_absolute_url()


def _check_in(user, auction, tos, now):
    """Auto-check-in: stamp checked_in and log history. Idempotent (checked_in is a timestamp)."""
    tos.checked_in = now
    tos.save(update_fields=["checked_in"])
    _record_nudge(user, auction, "checked_in")  # sanity cap; the timestamp is the real guard
    auction.create_history(
        applies_to="USERS",
        action=f"{tos.name or user.get_full_name() or user.username} checked in via the app on arrival",
        user=user,
    )


def _evaluate_auction(user, auction, location, now):
    """Return the action dicts for a single candidate auction (usually 0-2)."""
    actions = []
    distance = location.distance  # miles, annotated
    within_welcome = distance <= WELCOME_RADIUS_MI
    title = auction.title

    tos = _find_and_bind_tos(user, auction)

    if tos is None:
        if within_welcome and _record_nudge(user, auction, "join_offer"):
            actions.append(
                {
                    "type": "join_offer",
                    "auction": auction.slug,
                    "title": title,
                    "message": f"Welcome to the {title}.",
                    "rules_url": _rules_url(auction),
                }
            )
    elif auction.use_check_in_mode and tos.checked_in is None and within_welcome:
        _check_in(user, auction, tos, now)
        actions.append(
            {
                "type": "checked_in",
                "auction": auction.slug,
                "title": title,
                "message": f"Welcome to {title} — you're all checked in!",
            }
        )

    # The admin location-fix offer can coexist with a join/check-in action.
    if not auction.exact_location_set and auction.permission_check(user):
        if _record_nudge(user, auction, "set_location_offer"):
            actions.append(
                {
                    "type": "set_location_offer",
                    "auction": auction.slug,
                    "title": title,
                    "message": "Use this phone's current position as the auction's location.",
                }
            )
    return actions


def evaluate_ping(user, latitude, longitude, now=None):
    """Evaluate one position ping and return the list of display-ready actions (possibly empty)."""
    now = now or timezone.now()
    # Candidate physical pickup locations within the (larger) admin radius; the auction is filtered
    # down to in-person, single-location, in-window below.
    locations = (
        PickupLocation.objects.filter(
            auction__is_online=False,
            auction__is_deleted=False,
            pickup_by_mail=False,
        )
        .annotate(distance=distance_to(latitude, longitude, approximate_distance_to=DISTANCE_RESOLUTION_MI))
        .exclude(distance__gt=ADMIN_RADIUS_MI)
        .select_related("auction")
        .order_by("distance")
    )
    actions = []
    seen = set()
    for location in locations:
        auction = location.auction
        if auction.pk in seen:
            continue
        seen.add(auction.pk)
        if _single_pickup_location(auction) is None:
            continue  # feature only applies to auctions with exactly one physical location
        if not auction.in_welcome_window(now):
            continue
        actions.extend(_evaluate_auction(user, auction, location, now))
    return actions


def join_auction(user, auction, now=None):
    """Join ``auction`` as ``user`` via the app welcome prompt; return (tos, checked_in).

    Mirrors the essentials of the web rules-page confirm: bind an added-by-email row, otherwise
    create the AuctionTOS against the single pickup location, mark it a real (not manually-added)
    join, and — for check-in-mode auctions — check the user in at the same time. Idempotent."""
    now = now or timezone.now()
    tos = _find_and_bind_tos(user, auction)
    created = False
    if tos is None:
        tos = AuctionTOS.objects.create(
            user=user,
            auction=auction,
            pickup_location=_single_pickup_location(auction) or auction.location_qs.first(),
            email=user.email or None,
            name=user.get_full_name() or user.username,
            manually_added=False,
        )
        created = True
    else:
        if tos.manually_added:
            tos.manually_added = False
        if not tos.name:
            tos.name = user.get_full_name() or user.username
        if not tos.email:
            tos.email = user.email or None
        tos.save(update_fields=["manually_added", "name", "email"])

    checked_in = tos.checked_in is not None
    if auction.use_check_in_mode and tos.checked_in is None:
        tos.checked_in = now
        tos.save(update_fields=["checked_in"])
        checked_in = True

    if created:
        auction.create_history(
            applies_to="USERS",
            action=f"{tos.name or user.username} joined via the app's welcome prompt",
            user=user,
        )
    return tos, checked_in


def set_auction_location(auction, user, latitude, longitude):
    """Write the phone's position onto the auction's single pickup location and flag it exact.

    Returns False when the auction has no single physical location to pin."""
    location = _single_pickup_location(auction)
    if location is None:
        return False
    location.latitude = latitude
    location.longitude = longitude
    location.location_coordinates = f"{latitude},{longitude}"
    location.save(update_fields=["latitude", "longitude", "location_coordinates"])
    auction.exact_location_set = True
    auction.save(update_fields=["exact_location_set"])
    auction.create_history(
        applies_to="RULES",
        action=f"Exact location set from {user.get_full_name() or user.username}'s phone position",
        user=user,
    )
    return True
