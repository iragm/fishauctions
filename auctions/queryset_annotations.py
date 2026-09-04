"""Queryset builders that answer a question about many rows at once.

Three functions that take a queryset and give back the same queryset with the answers already in
it. They exist so that a page showing a hundred people or a hundred lots asks the database once
rather than once per row -- which is the same reason ``AuctionTOS.annotate_lot_counts`` exists next
to the model it annotates.

* ``nearby_auctions`` -- auctions with a pickup location within *distance* of a point, ordered
  nearest first, with the ignore/already-joined filtering a signed-in user expects.
* ``add_tos_info`` -- everything the users table and the club API say about a person in an auction:
  what they bought and sold, whether they have bid, whether an admin has banned them.
* ``add_tos_distance_info`` -- how far each of those people travelled to their pickup location.

They live outside ``models.py`` because that file is at its 15000-line ceiling and none of these is
reached from inside it. That direction of dependency is the whole arrangement: this module imports
models, models does not import this, and there is no cycle to manage.
"""

from django.db.models import (
    BooleanField,
    Case,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet
from django.utils import timezone

from auctions.models import (
    AuctionTOS,
    Bid,
    PageView,
    PickupLocation,
    UserBan,
    distance_to,
)


def nearby_auctions(
    latitude,
    longitude,
    distance=100,
    include_already_joined=False,
    user=None,
    return_slugs=False,
):
    """Return a list of auctions or auction slugs that are within a specified distance of the given location"""
    auctions = []
    slugs = []
    distances = []
    locations = (
        PickupLocation.objects.annotate(distance=distance_to(latitude, longitude))
        .exclude(distance__gt=distance)
        .filter(
            auction__date_end__gte=timezone.now(),
            auction__date_start__lte=timezone.now(),
        )
        .exclude(auction__promote_this_auction=False)
        .exclude(auction__isnull=True)
    )
    if user:
        if user.is_authenticated and not include_already_joined:
            locations = locations.exclude(auction__auctiontos__user=user)
        locations = locations.exclude(auction__auctionignore__user=user)
    for location in locations:
        if location.auction.slug not in slugs:
            auctions.append(location.auction)
            slugs.append(location.auction.slug)
            distances.append(location.distance)
    if return_slugs:
        return slugs
    else:
        return auctions, distances


def add_tos_info(qs):
    """Add fields to a given AuctionTOS queryset."""
    if not (isinstance(qs, QuerySet) and qs.model == AuctionTOS):
        msg = "must be passed a queryset of the AuctionTOS model"
        raise TypeError(msg)

    # Add has_ever_granted_permission annotation if not already present
    # This checks if the user has ever joined an auction (manually_added=False)
    # for the same auction creator
    qs = qs.annotate(
        has_ever_granted_permission=Case(
            When(
                Q(user__isnull=False)
                & Exists(
                    AuctionTOS.objects.filter(
                        user=OuterRef("user"), auction__created_by=OuterRef("auction__created_by"), manually_added=False
                    )
                ),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )

    return qs.annotate(
        lots_bid_actual=Coalesce(
            Subquery(
                Bid.objects.exclude(is_deleted=True)
                .filter(user=OuterRef("user"), lot_number__auction=OuterRef("auction"))
                .values("user")
                .annotate(count=Count("pk", distinct=True))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        lots_bid=Case(When(Q(has_ever_granted_permission=False), then=Value(0)), default=F("lots_bid_actual")),
        lots_viewed_actual=Coalesce(
            Subquery(
                PageView.objects.filter(user=OuterRef("user"), lot_number__auction=OuterRef("auction"))
                .values("user")
                .annotate(count=Count("lot_number", distinct=True))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        lots_viewed=Case(When(Q(has_ever_granted_permission=False), then=Value(0)), default=F("lots_viewed_actual")),
        lots_won=Count("auctiontos_winner", distinct=True),
        lots_submitted=Count("auctiontos_seller", distinct=True),
        other_auctions=Coalesce(
            Subquery(
                AuctionTOS.objects.filter(email=OuterRef("email"))
                .exclude(id=OuterRef("id"))
                .values("email")
                .annotate(count=Count("*"))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        lots_outbid=Case(
            When(lots_won__gt=F("lots_bid"), then=0),
            default=F("lots_bid") - F("lots_won"),
            output_field=IntegerField(),
        ),
        account_age_ms=Case(
            When(
                Q(has_ever_granted_permission=False),
                then=ExpressionWrapper(timezone.now() - F("createdon"), output_field=IntegerField()),
            ),
            default=ExpressionWrapper(timezone.now() - F("user__date_joined"), output_field=IntegerField()),
        ),
        account_age_days=ExpressionWrapper(F("account_age_ms") / 86400000000, output_field=IntegerField()),
        other_user_bans_actual=Coalesce(
            Subquery(
                UserBan.objects.filter(banned_user=OuterRef("user"))
                .values("pk")
                .annotate(count=Count("*"))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        other_user_bans=Case(
            When(Q(has_ever_granted_permission=False), then=Value(0)),
            default=F("other_user_bans_actual"),
        ),
        trust=ExpressionWrapper(
            1 * F("lots_bid")
            + 0.2 * F("lots_viewed")
            + 2 * F("lots_won")
            + 2 * F("lots_submitted")
            + 5 * F("other_auctions")
            - 2 * F("lots_outbid")
            + 0.01 * F("account_age_days")
            - 100 * F("other_user_bans"),
            output_field=IntegerField(),
        ),
    )


def add_tos_distance_info(qs):
    """Add a distance_traveled to an auctiontos query"""
    if not (isinstance(qs, QuerySet) and qs.model == AuctionTOS):
        msg = "must be passed a queryset of the AuctionTOS model"
        raise TypeError(msg)

    # Add has_ever_granted_permission annotation if not already present
    qs = qs.annotate(
        has_ever_granted_permission=Case(
            When(
                Q(user__isnull=False)
                & Exists(
                    AuctionTOS.objects.filter(
                        user=OuterRef("user"), auction__created_by=OuterRef("auction__created_by"), manually_added=False
                    )
                ),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )

    return (
        qs.select_related("user__userdata")
        .select_related("pickup_location")
        .annotate(
            new_distance_traveled=Case(
                When(Q(has_ever_granted_permission=False), then=Value(-1)),
                default=distance_to(
                    """`auctions_userdata`.`latitude`""",
                    """`auctions_userdata`.`longitude`""",
                    lat_field_name="""`auctions_pickuplocation`.`latitude`""",
                    lng_field_name="""`auctions_pickuplocation`.`longitude`""",
                    approximate_distance_to=1,
                ),
                output_field=IntegerField(),
            ),
        )
    )
