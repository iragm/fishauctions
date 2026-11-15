"""After an online auction: who comes to which pickup, and how far along the club is.

``tabs`` and ``people`` drive the logistics page (one tab per location, two where a multi-location auction's
location has a second pickup time). Sellers always come to the first pickup, since their lots leave from
there. Lots won from other locations, and lots held with ``AuctionTOS.hold_for_second_pickup``, wait for
the second, so a person holding everything is left off the first tab. ``mark_off`` is the tap on a name;
crossing a seller off the first pickup checks in their sold lots (``Lot.checked_in``). ``waiting_on`` is the
note beside a buyer whose sellers haven't come yet, and ``scan`` reads an invoice or lot code on the page.
``progress`` is the post-auction banner's numbers.
"""

import datetime
import re
from dataclasses import dataclass

from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

FIRST = 1
SECOND = 2


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    location: object
    #: FIRST or SECOND
    which: int
    #: Whether this auction has a second pickup anywhere, which adds the "to pick up now" column.
    multi_location: bool


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
    paid = Invoice.objects.filter(auctiontos_user=OuterRef("pk"), status="PAID")
    qs = (
        AuctionTOS.objects.filter(auction=auction, pickup_location=location)
        .select_related("auction")
        .annotate(
            sold=_lot_count("auctiontos_seller", auctiontos_winner__isnull=False),
            bought=_lot_count("auctiontos_winner"),
            from_here=_lot_count("auctiontos_winner", auctiontos_seller__pickup_location=location),
        )
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
        ).filter(
            Q(sold__gt=0)
            | Q(pickup_now__gt=0)
            # Bought only from elsewhere but not paid yet: they come to the first pickup to pay.
            | Q(bought__gt=0, hold_for_second_pickup=False) & ~Exists(paid)
        )
    else:
        qs = qs.annotate(
            pickup_now=Case(
                When(hold_for_second_pickup=True, then=F("bought")),
                default=F("bought") - F("from_here"),
                output_field=IntegerField(),
            )
        ).filter(pickup_now__gt=0)
    return qs.prefetch_related(Prefetch("auctiontos", queryset=Invoice.objects.order_by("-date"))).order_by("name")


def mark_off(tos, which, user, marked):
    """Cross ``tos`` off the ``which`` pickup list (``marked``), or back on. The tap says which, rather than
    toggling, so two volunteers with the same list open can't undo each other.

    Crossing a seller off the first pickup checks in their sold lots; bringing them back on undoes only the
    check-ins made since they were crossed off, so lots scanned one at a time before that stay in.
    """
    field = "marked_off_second_pickup" if which == SECOND else "marked_off_first_pickup"
    previous = getattr(tos, field)
    if bool(previous) != marked:
        setattr(tos, field, timezone.now() if marked else None)
        # Only the field: a full save pushes this row's contact fields up to its ClubMember.
        tos.save(update_fields=[field])
        label = "second" if which == SECOND else "first"
        tos.auction.create_history(
            applies_to="USERS",
            action=f"{'Marked off' if marked else 'Cleared'} {tos.name} for the {label} pickup",
            user=user,
        )
    if which != FIRST:
        return
    lots = sold_lots(tos.auction).filter(auctiontos_seller=tos)
    if marked:
        check_in_lots(lots)
    elif previous:
        lots.filter(checked_in__gte=previous).update(checked_in=None)


def sold_lots(auction):
    """The lots that change hands at a pickup: sold, and not from a seller who gets everything by mail."""
    return (
        auction.lots_qs.filter(banned=False, auctiontos_winner__isnull=False)
        .exclude(auctiontos_seller__pickup_location__pickup_by_mail=True)
        .order_by()
    )


def check_in_lots(lots):
    """Mark ``lots`` as at the pickup; returns how many changed."""
    return lots.filter(checked_in__isnull=True).update(checked_in=timezone.now())


def waiting_on(auction, tab):
    """``{buyer pk: [seller names]}``: whose lots, of the ones this tab hands over, haven't been checked in."""
    lots = sold_lots(auction).filter(checked_in__isnull=True)
    if tab.multi_location and tab.location.second_pickup_time:
        here = Q(auctiontos_seller__pickup_location=tab.location)
        lots = lots.filter(here if tab.which == FIRST else ~here)
    found = {}
    for buyer, seller, name in (
        lots.filter(auctiontos_winner__pickup_location=tab.location)
        .values_list("auctiontos_winner", "auctiontos_seller", "auctiontos_seller__name")
        .distinct()
        .order_by("auctiontos_seller__name")
    ):
        found.setdefault(buyer, {})[seller] = name or "Unknown"
    return {buyer: list(sellers.values()) for buyer, sellers in found.items()}


def exchange_window(auction):
    """``(start, end)``: two hours before the first pickup to a day after the last, or ``None``."""
    times = []
    for location in auction.location_qs.filter(pickup_by_mail=False):
        times += [t for t in (location.pickup_time, location.second_pickup_time) if t]
    if not times:
        return None
    return min(times) - datetime.timedelta(hours=2), max(times) + datetime.timedelta(hours=24)


def in_exchange_window(auction, now=None):
    window = exchange_window(auction)
    now = now or timezone.now()
    return bool(window) and window[0] <= now <= window[1]


def shows_pickup_code(auction, now=None):
    """Whether invoices show the code the logistics page scans: in ``exchange_window``, or once bidding has
    ended when no pickup has a time to make a window from.
    """
    now = now or timezone.now()
    if exchange_window(auction) is None:
        return bool(auction.date_end and auction.date_end <= now)
    return in_exchange_window(auction, now)


def _lots(count):
    return f"{count} lot{'' if count == 1 else 's'}"


def _check_in_seller(auction, tos, user):
    """Cross a seller off the first pickup and check in their lots; the message, or ``None`` with none sold."""
    total = sold_lots(auction).filter(auctiontos_seller=tos).count()
    if not total:
        return None
    mark_off(tos, FIRST, user, marked=True)
    return format_html("{} accounted for from {}.", _lots(total), tos.name or "Unknown")


def _scan_invoice(auction, pk, user, which):
    from auctions.models import Invoice

    invoice = Invoice.objects.filter(pk=pk, auctiontos_user__auction=auction).select_related("auctiontos_user").first()
    if not invoice:
        return {"ok": False, "message": "That invoice isn't from this auction."}
    tos = invoice.auctiontos_user
    parts = []
    # Sellers' lots all arrive at the first pickup; at the second, an invoice only says what to collect.
    sold = _check_in_seller(auction, tos, user) if which == FIRST else None
    if sold:
        parts.append(sold)
    bought = sold_lots(auction).filter(auctiontos_winner=tos)
    lot_count = bought.count()
    if lot_count:
        sellers = bought.values("auctiontos_seller").distinct().count()
        parts.append(
            format_html(
                "{} needs to collect {} from {} seller{}, <a href='{}'>open invoice</a>",
                tos.name or "Unknown",
                _lots(lot_count),
                sellers,
                "" if sellers == 1 else "s",
                reverse("invoice_by_pk", kwargs={"pk": invoice.pk}),
            )
        )
    if not parts:
        return {"ok": False, "message": format_html("{} has no lots to exchange.", tos.name or "Unknown")}
    return {"ok": True, "message": format_html(" ".join(["{}"] * len(parts)), *parts)}


def _find_lot(auction, code):
    """A lot label (``/qr/<pk>/``) or a typed lot number, to ``(lot, error)``."""
    lots = auction.lots_qs.filter(banned=False).select_related("auctiontos_seller__pickup_location")
    match = re.search(r"/qr/(\d+)/?(?:[?#]|$)", code)
    if match:
        found = list(lots.filter(pk=int(match.group(1))))
    elif auction.use_seller_dash_lot_numbering:
        found = list(lots.filter(custom_lot_number=code)[:2])
    elif code.isdigit():
        found = list(lots.filter(lot_number_int=int(code))[:2])
    else:
        return None, "Scan an invoice or a lot label."
    if len(found) > 1:
        return None, "More than one lot has this number -- scan its label instead."
    if not found:
        return None, "That lot isn't in this auction."
    return found[0], None


def _scan_lot(auction, code):
    lot, error = _find_lot(auction, code)
    if error:
        return {"ok": False, "message": error}
    if not lot.auctiontos_winner_id:
        return {"ok": False, "message": format_html("Lot {} didn't sell.", lot.lot_number_display)}
    if lot.auctiontos_seller.pickup_location and lot.auctiontos_seller.pickup_location.pickup_by_mail:
        return {"ok": False, "message": format_html("Lot {} ships by mail.", lot.lot_number_display)}
    if lot.checked_in:
        return {"ok": True, "message": format_html("Lot {} was already accounted for.", lot.lot_number_display)}
    seller = lot.auctiontos_seller
    from_seller = sold_lots(auction).filter(auctiontos_seller=seller)
    first = not from_seller.filter(checked_in__isnull=False).exists()
    check_in_lots(from_seller.filter(pk=lot.pk))
    message = format_html("Lot {} accounted for.", lot.lot_number_display)
    total = from_seller.count()
    if first and total > 1:
        message = format_html(
            "{} Tip: you can scan invoice barcodes to mark all lots from a seller collected, or "
            "<a href='#' data-check-in-seller='{}'>click here to mark all {} from {} as dropped off</a>",
            message,
            seller.pk,
            _lots(total),
            seller.name or "Unknown",
        )
    return {"ok": True, "message": message}


def scan(auction, code, user, which=FIRST):
    """A code read on the logistics page: an invoice (``/invoices/<pk>/``), a lot label (``/qr/<pk>/``) or a
    typed lot number. ``which`` is the pickup the page is on. Returns ``{"ok", "message"}``; the message is HTML.
    """
    code = (code or "").strip()
    # Anchored, so a no-login link (/invoices/<uuid>/) is never read as the pk its digits start with.
    match = re.search(r"/invoices/(\d+)/?(?:[?#]|$)", code)
    if match:
        return _scan_invoice(auction, int(match.group(1)), user, which)
    return _scan_lot(auction, code)


def check_in_seller(auction, tos_pk, user):
    """The tip's link after a lot scan: every lot from that seller."""
    from auctions.models import AuctionTOS

    tos = AuctionTOS.objects.filter(pk=tos_pk, auction=auction).first()
    message = tos and _check_in_seller(auction, tos, user)
    if not message:
        return {"ok": False, "message": "That seller has no sold lots here."}
    return {"ok": True, "message": message}


def running_lots(auction):
    """Lots of an online auction still taking bids: unsold, with an end still ahead (overtime included)."""
    return auction.lots_qs.exclude(banned=True).filter(winning_price__isnull=True, date_end__gt=timezone.now())


def bidding_over(auction):
    """Whether every lot of an online auction has stopped taking bids."""
    return bool(auction.date_end and auction.date_end <= timezone.now() and not running_lots(auction).exists())


def _step(done, total):
    return {"done": done, "total": total, "percent": round(100 * done / total) if total else 100}


def progress(auction):
    """The post-auction banner's numbers, or ``None`` when it has nothing to show.

    While lots are in overtime it is only ``lots`` (and ``ends_by``). After, each later step is there until
    it reaches 100%: ``notified`` and ``opened`` (only with invoice notifications on), ``buyers_paid``,
    ``labels`` and ``sellers_paid``. ``accounted`` (lots checked in) is there throughout ``exchange_window``.
    """
    from auctions.models import Invoice, Lot

    if not auction.shows_post_end_banner:
        return None
    lots = auction.lots_qs.exclude(banned=True)
    total = lots.count()
    running = running_lots(auction)
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
    if in_exchange_window(auction):
        exchanged = sold_lots(auction)
        total_exchanged = exchanged.count()
        if total_exchanged:
            # Shown for the whole window, even once every lot is in.
            steps["accounted"] = _step(exchanged.filter(checked_in__isnull=False).count(), total_exchanged)
    return steps or None
