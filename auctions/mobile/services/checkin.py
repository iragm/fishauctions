"""Proximity check-in and welcome.

The app POSTs the phone's position to ``checkin/ping/`` while the shell is up. The server evaluates
the geofence, welcome window and join/check-in/admin state, performs the auto-check-in, and returns
display-ready actions with all the copy in them.

Also backs ``checkin/join/`` and ``checkin/set-location/``. Every mutation lands in auction history.
"""

import logging

from django.utils import timezone

from auctions.models import AuctionTOS, CheckinNudge, PickupLocation, distance_to
from auctions.services import apply_club_member_to_tos, ensure_club_member

logger = logging.getLogger(__name__)

# Geofence radii, miles: 500 ft for the welcome nudge, and a generous 2 mi for the admin
# location-fix offer, whose whole point is that the stored location may be wrong.
WELCOME_RADIUS_MI = 0.095
ADMIN_RADIUS_MI = 2.0
# distance_to ceiling-rounds to this bucket (a privacy feature), which is fine for a 500 ft fence.
DISTANCE_RESOLUTION_MI = 0.005


def _single_pickup_location(auction):
    """The auction's one physical pickup location, or None unless exactly one exists."""
    locations = list(auction.location_qs.exclude(pickup_by_mail=True))
    return locations[0] if len(locations) == 1 else None


def _find_and_bind_tos(user, auction):
    """The user's AuctionTOS, matched by user FK or by email; an email-matched row with no user is bound
    here, the same claim the web join makes.
    """
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
    """Create the one-shot nudge row; True only the first time."""
    _, created = CheckinNudge.objects.get_or_create(user=user, auction=auction, kind=kind)
    return created


def _set_last_auction_used(user, auction):
    """Make ``auction`` the user's current auction (the palette, AR and lot queue read it).

    Arriving near an in-person auction you're part of is a strong signal. Guarded so a routine ping
    doesn't write on every fix.
    """
    userdata = getattr(user, "userdata", None)
    if userdata is None or userdata.last_auction_used_id == auction.pk:
        return
    userdata.last_auction_used = auction
    userdata.save(update_fields=["last_auction_used"])


def _rules_url(auction):
    return auction.get_absolute_url()


def _check_in(user, auction, tos, now):
    """Auto-check-in: stamp checked_in, grant bidding, log history. Idempotent.

    Joining a check-in-mode auction leaves ``bidding_allowed`` False, and checking in is what grants it.
    """
    tos.checked_in = now
    update_fields = ["checked_in"]
    if not tos.bidding_allowed:
        tos.bidding_allowed = True
        update_fields.append("bidding_allowed")
    tos.save(update_fields=update_fields)
    _record_nudge(user, auction, "checked_in")  # sanity cap; the timestamp is the real guard
    auction.create_history(
        applies_to="USERS",
        action=f"{tos.name or user.get_full_name() or user.username} checked in via the app on arrival",
        user=user,
    )


def _evaluate_auction(user, auction, location, now):
    """``(actions, is_member)`` for one candidate auction.

    ``is_member`` is True when the user already has an AuctionTOS, so the caller can point
    ``last_auction_used`` at the nearest auction they belong to.
    """
    actions = []
    distance = location.distance  # miles, annotated
    # The 500 ft radius assumes the coordinates are the front door; until an admin pins them
    # (``exact_location_set``) they are a geocoded street address, so everything but the
    # auto-check-in falls back to the 2 mi radius.
    #
    # Auto-check-in is never widened: it happens with no user intent, and somebody who has really
    # arrived will be inside 500 ft within a minute. Tapping "join" on the widened offer is explicit
    # intent, so it still checks them in.
    within_checkin = distance <= WELCOME_RADIUS_MI
    within_welcome = within_checkin or (not auction.exact_location_set and distance <= ADMIN_RADIUS_MI)
    title = auction.title

    tos = _find_and_bind_tos(user, auction)
    # An auction that assigns bidder numbers at the door turns self-check-in off; check the flag
    # first, so no one-shot nudge is burned while the feature is off.
    self_checkin = auction.allows_app_self_checkin

    if tos is None:
        # One join offer per person per auction, whichever band it fired in: hardly anybody lives
        # inside the widened radius, so a second prompt costs more in nagging than it saves.
        if self_checkin and within_welcome and _record_nudge(user, auction, "join_offer"):
            actions.append(
                {
                    "type": "join_offer",
                    "auction": auction.slug,
                    "title": title,
                    "message": f"Welcome to the {title}.",
                    "rules_url": _rules_url(auction),
                }
            )
    elif self_checkin and auction.use_check_in_mode and tos.checked_in is None and within_checkin:
        _check_in(user, auction, tos, now)
        message = f"Welcome to {title} — you're all checked in!"
        if tos.bidder_number:
            message += f" Your bidder number is {tos.bidder_number}."
        actions.append(
            {
                "type": "checked_in",
                "auction": auction.slug,
                "title": title,
                "message": message,
                "bidder_number": tos.bidder_number or "",
            }
        )

    # The admin location-fix offer can coexist with a join or check-in action.
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
    return actions, tos is not None


def evaluate_ping(user, latitude, longitude, now=None):
    """Evaluate one position ping and return the display-ready actions, possibly none."""
    now = now or timezone.now()
    # Physical pickup locations within the admin radius; the auction is filtered below.
    #
    # ``promote_this_auction`` is the disclosure gate: every auction starts unpromoted, and without
    # this filter standing near the venue pushed the full title and a working Join button to any
    # signed-in app user nearby. It gates the admin nudges too -- a reminder to set the location
    # still mentions an auction we should not be mentioning.
    locations = (
        PickupLocation.objects.filter(
            auction__is_online=False,
            auction__is_deleted=False,
            auction__promote_this_auction=True,
            pickup_by_mail=False,
        )
        .annotate(distance=distance_to(latitude, longitude, approximate_distance_to=DISTANCE_RESOLUTION_MI))
        .exclude(distance__gt=ADMIN_RADIUS_MI)
        .select_related("auction")
        .order_by("distance")
    )
    actions = []
    seen = set()
    nearest_member_auction = None
    for location in locations:
        auction = location.auction
        if auction.pk in seen:
            continue
        seen.add(auction.pk)
        if _single_pickup_location(auction) is None:
            continue  # feature only applies to auctions with exactly one physical location
        if not auction.in_welcome_window(now):
            continue
        auction_actions, is_member = _evaluate_auction(user, auction, location, now)
        actions.extend(auction_actions)
        # Locations are distance-ordered, so the first is the nearest.
        if is_member and nearest_member_auction is None:
            nearest_member_auction = auction
    if nearest_member_auction is not None:
        _set_last_auction_used(user, nearest_member_auction)
    return actions


def join_auction(user, auction, now=None):
    """Join ``auction`` from the app welcome prompt; returns (tos, checked_in).

    Mirrors the web rules-page confirm: bind an added-by-email row or create the AuctionTOS against the
    single pickup location, mark it a real join, create or link the ClubMember in a club-managed
    auction, and check the user in for check-in-mode auctions. Idempotent.

    Returns ``(None, False)``, writing nothing, when app self-check-in is off (the endpoint 403s).
    """
    now = now or timezone.now()
    if not auction.allows_app_self_checkin:
        return None, False
    tos = _find_and_bind_tos(user, auction)
    member = None
    if tos is None and auction.is_club_managed:
        # In a club-managed auction, make the club member first: creating one also creates its
        # shadow AuctionTOS, which is adopted rather than raced with a second record.
        member, _created = ensure_club_member(
            auction,
            user=user,
            name=user.get_full_name() or user.username,
            email=user.email or "",
            # The user is signing themselves up; no admin has touched this record.
            admin_edited=False,
        )
        tos = _find_and_bind_tos(user, auction)
    created = False
    if tos is None:
        tos = AuctionTOS(
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
    if auction.is_club_managed:
        if member is None:
            # An existing participant record may still be missing its member.
            member, _created = ensure_club_member(
                auction,
                user=user,
                name=tos.name or "",
                email=tos.email or "",
                phone_number=tos.phone_number or "",
                address=tos.address or "",
                admin_edited=False,
            )
        apply_club_member_to_tos(auction, tos, member)
        member.update_last_club_activity()
    tos.save()

    checked_in = tos.checked_in is not None
    if auction.use_check_in_mode and tos.checked_in is None:
        # The same path as arriving with an existing TOS, so bidding and history match.
        _check_in(user, auction, tos, now)
        checked_in = True

    if created:
        auction.create_history(
            applies_to="USERS",
            action=f"{tos.name or user.username} joined via the app's welcome prompt",
            user=user,
        )
    # Joining makes this the auction the user is working with.
    _set_last_auction_used(user, auction)
    return tos, checked_in


def set_auction_location(auction, user, latitude, longitude):
    """Write the phone's position onto the auction's single pickup location and flag it exact.

    False when there is no single physical location to pin.
    """
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
