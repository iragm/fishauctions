"""After an online auction: who comes to which pickup, and how far along the club is.

``tabs`` and ``people`` drive the logistics page (one tab per location, two where a multi-location auction's
location has a second pickup time). Sellers always come to the first pickup, since their lots leave from
there. Lots won from other locations, and lots held with ``AuctionTOS.hold_for_second_pickup``, wait for
the second, so a person holding everything is left off the first tab. ``mark_off`` is the tap on a name.
``progress`` is the post-auction banner's numbers.
"""

import datetime
from dataclasses import dataclass

from django.db.models import Case, Count, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

FIRST = 1
SECOND = 2
#: How long after bidding ends the banner keeps showing whatever is left undone.
BANNER_DAYS = datetime.timedelta(days=30)


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    location: object
    #: FIRST or SECOND
    which: int
    #: Whether this auction has a second pickup anywhere, which adds the "to pick up now" column.
    multi_location: bool

    @property
    def field(self):
        return "marked_off_second_pickup" if self.which == SECOND else "marked_off_first_pickup"


def tabs(auction):
    """The page's tabs, in location order: ``MA``, or ``VT - First`` and ``VT - Second``."""
    multi = auction.multi_location
    found = []
    for location in auction.location_qs.filter(pickup_by_mail=False).order_by("pickup_time", "name"):
        name = location.name
        if multi and location.second_pickup_time:
            found.append(Tab(f"{location.pk}-1", f"{name} - First", location, FIRST, True))
            found.append(Tab(f"{location.pk}-2", f"{name} - Second", location, SECOND, True))
        else:
            found.append(Tab(f"{location.pk}-1", name, location, FIRST, multi))
    return found


def _lot_count(field, **conditions):
    from auctions.models import Lot

    return Coalesce(
        Subquery(
            Lot.objects.filter(is_deleted=False, banned=False, **{field: OuterRef("pk")}, **conditions)
            .order_by()
            .values(field)
            .annotate(total=Count("pk"))
            .values("total")[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )


def people(auction, tab):
    """The people in ``tab``, annotated with ``sold``, ``bought`` and ``pickup_now`` (lot counts)."""
    from auctions.models import AuctionTOS, Invoice

    location = tab.location
    qs = AuctionTOS.objects.filter(auction=auction, pickup_location=location).annotate(
        sold=_lot_count("auctiontos_seller", auctiontos_winner__isnull=False),
        bought=_lot_count("auctiontos_winner"),
        from_here=_lot_count("auctiontos_winner", auctiontos_seller__pickup_location=location),
    )
    if not (tab.multi_location and location.second_pickup_time):
        # One pickup for this location: everything changes hands then.
        qs = qs.annotate(pickup_now=F("bought")).filter(Q(sold__gt=0) | Q(bought__gt=0))
    elif tab.which == FIRST:
        # Held lots wait for the second pickup; a seller comes to the first regardless.
        qs = qs.annotate(
            pickup_now=Case(
                When(hold_for_second_pickup=True, then=Value(0)), default=F("from_here"), output_field=IntegerField()
            )
        ).filter(Q(sold__gt=0) | Q(pickup_now__gt=0))
    else:
        qs = qs.annotate(
            pickup_now=Case(
                When(hold_for_second_pickup=True, then=F("bought")),
                default=F("bought") - F("from_here"),
                output_field=IntegerField(),
            )
        ).filter(pickup_now__gt=0)
    return qs.prefetch_related(Prefetch("auctiontos", queryset=Invoice.objects.order_by("-date"))).order_by("name")


def mark_off(tos, which, user):
    """Cross ``tos`` off the ``which`` pickup list, or back on if they were already; returns the new state."""
    field = "marked_off_second_pickup" if which == SECOND else "marked_off_first_pickup"
    marked = getattr(tos, field) is None
    setattr(tos, field, timezone.now() if marked else None)
    # Only the flag: a full save pushes this row's contact fields up to its ClubMember.
    tos.save(update_fields=[field])
    label = "second" if which == SECOND else "first"
    tos.auction.create_history(
        applies_to="USERS",
        action=f"{'Marked off' if marked else 'Cleared'} {tos.name} for the {label} pickup",
        user=user,
    )
    return marked


def _step(done, total):
    return {"done": done, "total": total, "percent": round(100 * done / total) if total else 100}


def progress(auction):
    """The post-auction banner's numbers, or ``None`` when it has nothing to show.

    While lots are in overtime it is only ``lots`` (and ``ends_by``). After, each later step is there until
    it reaches 100%: ``notified`` and ``opened`` (only with invoice notifications on), ``buyers_paid``,
    ``labels`` and ``sellers_paid``.
    """
    from auctions.models import Invoice, Lot

    now = timezone.now()
    if not auction.is_online or not auction.date_end or not auction.date_end <= now < auction.date_end + BANNER_DAYS:
        return None
    lots = auction.lots_qs.exclude(banned=True)
    total = lots.count()
    running = lots.filter(winning_price__isnull=True, date_end__gt=now)
    still_running = running.count()
    if still_running:
        latest = max(
            (end for end in running.values_list("date_end", flat=True) if end),
            default=auction.dynamic_end,
        )
        return {
            "lots": _step(total - still_running, total),
            "running": still_running,
            "ends_by": min(latest, auction.dynamic_end),
        }
    steps = {}
    invoices = Invoice.objects.filter(auction=auction)

    def add(key, done, of):
        if of and done < of:
            steps[key] = _step(done, of)

    if auction.email_users_when_invoices_ready:
        add("notified", invoices.filter(email_sent=True).count(), invoices.count())
        steps_open = invoices.filter(status="DRAFT").exists()
        if "notified" in steps:
            steps["notified"]["open_exist"] = steps_open
        add("opened", invoices.filter(opened=True).count(), invoices.count())
    buyers = invoices.filter(calculated_total__lt=0)
    add("buyers_paid", buyers.filter(status="PAID").count(), buyers.count())
    sold = Lot.objects.filter(auction=auction, is_deleted=False, banned=False, auctiontos_winner__isnull=False).exclude(
        auctiontos_seller__isnull=True
    )
    sellers = set(sold.values_list("auctiontos_seller", flat=True).distinct().order_by())
    unprinted = set(sold.filter(label_printed=False).values_list("auctiontos_seller", flat=True).distinct().order_by())
    add("labels", len(sellers - unprinted), len(sellers))
    payouts = invoices.filter(calculated_total__gt=0)
    add("sellers_paid", payouts.filter(status="PAID").count(), payouts.count())
    return steps or None
