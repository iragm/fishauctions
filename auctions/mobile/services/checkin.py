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
from auctions.services import apply_club_member_to_tos, ensure_club_member

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


def _set_last_auction_used(user, auction):
    """Make ``auction`` the user's current auction (drives the command palette, AR, lot queue, etc.).

    Arriving near an in-person auction you're part of (joined or admin-added) is a strong signal it's
    the auction you're now working with. Guarded so a routine ping doesn't write on every fix."""
    userdata = getattr(user, "userdata", None)
    if userdata is None or userdata.last_auction_used_id == auction.pk:
        return
    userdata.last_auction_used = auction
    userdata.save(update_fields=["last_auction_used"])


def _rules_url(auction):
    return auction.get_absolute_url()


def _check_in(user, auction, tos, now):
    """Auto-check-in: stamp checked_in, grant bidding, log history. Idempotent (checked_in is a
    timestamp).

    Joining a check-in-mode auction deliberately leaves ``bidding_allowed`` False — checking in is
    what grants it (see AuctionTOSFormView and the admin check-in modal, which both set it). Without
    this the app's self-check-in stamped the timestamp but still left the user unable to bid."""
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
    """Return ``(actions, is_member)`` for a single candidate auction.

    ``actions`` is the display-ready list (usually 0-2). ``is_member`` is True when the user already
    has an AuctionTOS here — i.e. they've joined or been added by an admin — so the caller can point
    ``last_auction_used`` at the nearest auction the user actually belongs to."""
    actions = []
    distance = location.distance  # miles, annotated
    # The 500 ft welcome radius assumes the stored coordinates really are the front door. Until an
    # admin has pinned the location from their phone (``exact_location_set``), they're a geocoded
    # street address that can be off by far more than that, so everything except the auto-check-in
    # falls back to the generous 2 mi radius the admin location-fix offer already uses.
    #
    # Auto-check-in is deliberately never widened: it happens with no user intent at all (a ping
    # while driving past would put a bidder number on the floor for someone who isn't there), and
    # somebody who really has arrived will be inside 500 ft within a minute. Tapping "join" on the
    # widened offer is different — that's explicit intent from someone who says they're here — so it
    # still checks them in.
    within_checkin = distance <= WELCOME_RADIUS_MI
    within_welcome = within_checkin or (not auction.exact_location_set and distance <= ADMIN_RADIUS_MI)
    title = auction.title

    tos = _find_and_bind_tos(user, auction)
    # An auction that assigns bidder numbers at the door turns self-check-in off; then neither the
    # join offer nor the auto-check-in is offered (checking the flag first so no one-shot nudge row
    # is burned while the feature is off).
    self_checkin = auction.allows_app_self_checkin

    if tos is None:
        # Strictly one join offer per person per auction, whichever band it fired in: an offer
        # dismissed from a mile away is spent. Deliberate — hardly anybody lives inside the widened
        # radius of a venue, so a second prompt would cost more in nagging than it saves.
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
    return actions, tos is not None


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
        # Locations are distance-ordered, so the first auction the user belongs to is the nearest one.
        if is_member and nearest_member_auction is None:
            nearest_member_auction = auction
    if nearest_member_auction is not None:
        _set_last_auction_used(user, nearest_member_auction)
    return actions


def join_auction(user, auction, now=None):
    """Join ``auction`` as ``user`` via the app welcome prompt; return (tos, checked_in).

    Mirrors the essentials of the web rules-page confirm: bind an added-by-email row, otherwise
    create the AuctionTOS against the single pickup location, mark it a real (not manually-added)
    join, create/link the ClubMember in a club-managed auction (the club owns the bidder number),
    and — for check-in-mode auctions — check the user in at the same time. Idempotent.

    Returns ``(None, False)`` when the auction has app self-check-in turned off; nothing is written
    (the endpoint turns that into a 403)."""
    now = now or timezone.now()
    if not auction.allows_app_self_checkin:
        return None, False
    tos = _find_and_bind_tos(user, auction)
    member = None
    if tos is None and auction.is_club_managed:
        # No participant record yet in a club-managed auction: make the club member first, because
        # creating one also creates its shadow AuctionTOS (signals.propagate_clubmember_to_shadow_tos).
        # Adopting that row is how the app join ends up with the club's bidder number instead of
        # racing it with a second record that AuctionTOS.save() would then have to merge away.
        member, _created = ensure_club_member(
            auction,
            user=user,
            name=user.get_full_name() or user.username,
            email=user.email or "",
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
            # Already had a participant record (admin-added, or a member from a previous ping): the
            # member may still be missing, so resolve it the same way.
            member, _created = ensure_club_member(
                auction,
                user=user,
                name=tos.name or "",
                email=tos.email or "",
                phone_number=tos.phone_number or "",
                address=tos.address or "",
            )
        apply_club_member_to_tos(auction, tos, member)
        member.update_last_club_activity()
    tos.save()

    checked_in = tos.checked_in is not None
    if auction.use_check_in_mode and tos.checked_in is None:
        # Same path as arriving with an existing TOS, so bidding_allowed and the history entry match.
        _check_in(user, auction, tos, now)
        checked_in = True

    if created:
        auction.create_history(
            applies_to="USERS",
            action=f"{tos.name or user.username} joined via the app's welcome prompt",
            user=user,
        )
    # Joining from the welcome prompt makes this the auction the user is working with.
    _set_last_auction_used(user, auction)
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
