"""One lot: its page, its photos, and creating or editing it.

``ViewLot`` is the most-visited page on the site. The page-view history helpers at the top of the
module are what draws the "who looked at this" panel on it, and they are shared with the seller's
own dashboard.
"""

import collections
import logging
import secrets
from datetime import timedelta
from decimal import Decimal
from random import randint
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import (
    Count,
    Q,
)
from django.db.models.base import Model as Model
from django.db.models.functions import TruncDate
from django.http import (
    Http404,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import date as date_format
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.generic import DetailView, TemplateView, View
from django.views.generic.edit import (
    CreateView,
    DeleteView,
    FormMixin,
    UpdateView,
)
from webpush.models import PushInformation

from auctions.forms import (
    IMAGE_PROCESSING_EXCEPTIONS,
    CreateImageForm,
    CreateLotForm,
    EditLot,
    validate_image_url,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Category,
    ChatSubscription,
    Invoice,
    Lot,
    LotHistory,
    LotImage,
    PageView,
    Watch,
    distance_to,
)
from auctions.notifications import user_has_app_push
from auctions.services import (
    copy_lot_images,
    user_can_clone_lot,
)
from auctions.species_matching import record_choice as record_species_choice
from auctions.species_matching import remember as remember_species

from .base import AuctionViewMixin, check_club_permission, close_modal_response
from .selling import notify_watchers_lot_selling_soon

logger = logging.getLogger(__name__)
#: How far back the page-view history modals look.  Fifteen days is what the modals say on the
#: tin, and it is also the bound that keeps them cheap: PageView is the largest table on the site.
PAGE_VIEW_HISTORY_DAYS = 15

#: How many off-site referrers the history lists.  The referrer column is free text with a long
#: tail, so the whole list would be unreadable and unbounded; the top few are the useful part.
PAGE_VIEW_HISTORY_REFERRERS = 5

#: How many sources the day-by-day chart draws a band for before the rest are added together.
#: ``?src=`` is not a closed vocabulary -- the club API writes a key's name into it -- so without a
#: cap one busy lot could ask for a stack thirty colours deep, most of them one view thick.
PAGE_VIEW_HISTORY_CHART_SOURCES = 6

#: The chart's y axis is always :data:`PAGE_VIEW_HISTORY_Y_TICKS` whole-number steps tall and never
#: shorter than :data:`PAGE_VIEW_HISTORY_MIN_Y` views.  Most lots get five to fifteen views in the
#: whole fortnight, and an axis fitted to numbers that small draws a single view as a full-height
#: bar under half-view gridlines -- which reads as a busy lot with broken labels.  A floor and a
#: whole-number step mean a quiet lot looks quiet and every gridline is a number of views.
PAGE_VIEW_HISTORY_MIN_Y = 4
PAGE_VIEW_HISTORY_Y_TICKS = 4


def page_view_history(page_views, days=PAGE_VIEW_HISTORY_DAYS):
    """A short traffic history: totals, a breakdown by where people came from, and a daily count.

    ``page_views`` is a :class:`~auctions.models.PageView` queryset the caller has **already**
    narrowed to the rows this reader may see -- one lot, or one seller's lots.  This function then
    narrows it again to the last ``days`` days.  PageView is the biggest table on the site, so
    every query below carries both bounds: a query here that is not limited by an owner *and* a
    date has no business existing.

    Four aggregate queries, and no PageView row is ever fetched into Python:

    * one ``values("source").annotate(...)`` for the breakdown by ``?src=``,
    * one ``TruncDate`` group-by over day *and* source, which the chart stacks and which the day
      totals are the sum of -- so the per-day numbers cost no query of their own,
    * one for the top :data:`PAGE_VIEW_HISTORY_REFERRERS` off-site referrers,
    * one ``aggregate()`` for the unique-viewer total, which cannot be summed back out of the
      per-source counts (the same person shows up under two sources).

    What comes back is at most ``days`` columns, seven chart bands, one row per ``?src=`` value in
    use and five referrers, so the dict handed to the template stays small however busy the window
    was.

    ``source`` is the primary breakdown and ``referrer`` the secondary one on purpose.  ``source``
    is the ``?src=`` parameter, a vocabulary this site writes itself and can therefore label
    (``Lot.PAGE_VIEW_SOURCE_LABELS``).  ``referrer`` is whatever the browser chose to send, which
    for a visit from another site is normally only that site's origin: every current browser
    defaults to ``strict-origin-when-cross-origin`` (the policy this site sets on its own pages in
    ``settings.SECURE_REFERRER_POLICY`` too), which keeps the path only within one site.  Since our
    own pages are excluded below, most of what is left is a bare domain.  So the source rows answer
    "which of our surfaces sent them" and the referrer rows are kept for the one thing source
    cannot say: who linked to this from somewhere else.
    """
    now = timezone.localtime()
    first_day = (now - timedelta(days=days - 1)).date()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = page_views.filter(date_start__gte=start)

    # Sources.  ``None`` and ``""`` both mean "no ?src= on the URL" and are one row, the same way
    # Lot.page_view_source_breakdown merges them; the labels come from that same table.
    merged = {}
    for row in recent.values("source").annotate(
        views=Count("pk"),
        users=Count("user", distinct=True),
        sessions=Count("session_id", distinct=True, filter=Q(user__isnull=True)),
    ):
        source = row["source"] or ""
        entry = merged.setdefault(
            source,
            {
                "source": source,
                "label": Lot.PAGE_VIEW_SOURCE_LABELS.get(source, source),
                "is_ar_event": source in Lot.AR_PAGE_VIEW_SOURCES,
                "views": 0,
                "unique": 0,
            },
        )
        entry["views"] += row["views"]
        entry["unique"] += row["users"] + row["sessions"]
    sources = sorted(merged.values(), key=lambda entry: (-entry["views"], entry["label"]))

    # One row per day *and* source: the chart stacks the sources inside each day, so this one
    # group-by is both the day-by-day totals and the split within them.
    counted = collections.defaultdict(int)
    for row in recent.annotate(day=TruncDate("date_start")).values("day", "source").annotate(views=Count("pk")):
        counted[(row["day"], row["source"] or "")] += row["views"]

    # A column for every day, including the ones nobody looked -- a chart with the quiet days left
    # out reads as a busier lot than it was.  Bands are the sources in the order the table has them
    # (busiest first), so the tall part of every stack is at the bottom; everything past the cap is
    # one "Everything else" band rather than a colour nobody can tell from its neighbour.
    window = [first_day + timedelta(days=offset) for offset in range(days)]
    bands = [row["label"] for row in sources[:PAGE_VIEW_HISTORY_CHART_SOURCES]]
    band_of = {row["source"]: min(index, PAGE_VIEW_HISTORY_CHART_SOURCES) for index, row in enumerate(sources)}
    if len(sources) > PAGE_VIEW_HISTORY_CHART_SOURCES:
        bands.append("Everything else")
    chart_data = [[0] * days for _ in bands]
    for (day, source), views in counted.items():
        column = (day - first_day).days
        if 0 <= column < days and source in band_of:
            chart_data[band_of[source]][column] += views
    day_totals = [sum(band[column] for band in chart_data) for column in range(days)]
    busiest = max(day_totals, default=0)

    # The y axis is worked out here rather than left to Chart.js so that the fortnight a lot
    # actually gets -- often a single view on a single day -- is drawn as a small bar on a
    # whole-number axis instead of a full-height one against gridlines at 0.2 of a view.
    y_step = -(-max(busiest, PAGE_VIEW_HISTORY_MIN_Y) // PAGE_VIEW_HISTORY_Y_TICKS)

    # Off-site referrers only: our own pages are already the source breakdown, in better words.
    domain = Site.objects.get_current().domain
    referrers = list(
        recent.exclude(referrer__isnull=True)
        .exclude(referrer="")
        .exclude(referrer__startswith=domain)
        .exclude(referrer__startswith=f"https://{domain}")
        .exclude(referrer__startswith=f"http://{domain}")
        .values("referrer")
        .annotate(views=Count("pk"))
        .order_by("-views", "referrer")[:PAGE_VIEW_HISTORY_REFERRERS]
    )

    totals = recent.aggregate(
        views=Count("pk"),
        users=Count("user", distinct=True),
        sessions=Count("session_id", distinct=True, filter=Q(user__isnull=True)),
    )
    return {
        "days": days,
        "first_day": first_day,
        "last_day": now.date(),
        "total_views": totals["views"] or 0,
        "unique_viewers": (totals["users"] or 0) + (totals["sessions"] or 0),
        "chart": {
            "labels": [date_format(day, "M j") for day in window],
            "series": bands,
            "data": chart_data,
            "busiest": busiest,
            "y_max": y_step * PAGE_VIEW_HISTORY_Y_TICKS,
            "y_step": y_step,
        },
        "sources": sources,
        "referrers": referrers,
        "has_ar_rows": any(row["is_ar_event"] for row in sources),
    }


def page_view_history_context(request, page_views, *, title, subtitle):
    """The context both history modals render, so the two surfaces cannot answer differently.

    The one thing worth putting here rather than in either view is ``show_source_table``.  The
    "How they got here" table is **superuser-only**: which of our own surfaces sent somebody is
    detail an ordinary seller has no use for, and a table of it above the chart is the first thing
    they read.  The by-source split is still in the chart for everybody -- it is the table that
    goes -- and the referrer list stays for everybody too, since "another website linked to my lot"
    is news to a seller in a way that "they came from a lot list" is not.

    Deciding it here rather than in the template is the same rule as
    :func:`can_see_lot_page_view_history`: the template asks one question and gets one answer, and
    a second surface added later cannot quietly gate it differently.
    """
    return {
        "history": page_view_history(page_views),
        "modal_title": title,
        "modal_subtitle": subtitle,
        "show_source_table": request.user.is_superuser,
    }


def can_see_lot_page_view_history(user, lot):
    """Who gets the view-history button on a lot page, and on which lots.

    Two questions, and both are answered here rather than in the template so that
    :class:`LotPageViewHistoryView` and the button that opens it can never disagree.

    **Who:** the seller, and anyone who administers the auction the lot is in.  Nobody else -- how
    many people looked at a lot, and how they found it, is the seller's business.

    **Which lots:** only lots in an *online* auction, or with no auction at all.  A lot in an
    in-person auction already has the per-source breakdown in the collapse under "Views"
    (``show_page_view_breakdown`` and :attr:`Lot.page_view_source_breakdown`), and a second table of
    the same numbers on the same page is worse than either alone.  A sealed-bid lot publishes no
    view count at all, so it gets no history either.
    """
    if not lot or lot.sealed_bid:
        return False
    if lot.auction_id and not lot.auction.is_online:
        return False
    if lot.is_owned_by(user):
        return True
    return bool(lot.auction_id and lot.auction.permission_check(user))


class LotPageViewHistoryView(LoginRequiredMixin, View):
    """The last 15 days of traffic on one lot, as a modal loaded over HTMX into ``#modals-here``.

    GET only -- it reads and never writes, which is also why it needs no ``palette_actions`` entry
    (see the Housekeeping section of CLAUDE.md); ``palette_routes.EXCLUDED`` carries the reason it
    is not a page somebody can be navigated to.
    """

    def get(self, request, pk):
        lot = get_object_or_404(Lot.objects.exclude(is_deleted=True).select_related("auction"), pk=pk)
        # The permission check lives here, not only on the button: the URL is guessable.  A lot
        # whose *type* has no history modal is refused here too, so there is exactly one rule.
        if not can_see_lot_page_view_history(request.user, lot):
            raise PermissionDenied
        return render(
            request,
            "auctions/page_view_history_modal.html",
            page_view_history_context(
                request,
                PageView.objects.filter(lot_number=lot),
                title="How people found this lot",
                subtitle=lot.lot_name,
            ),
        )


class MyLotsPageViewHistoryView(LoginRequiredMixin, View):
    """The same 15 days as :class:`LotPageViewHistoryView`, totalled over every lot you are selling.

    Opened from the selling dashboard (``MyLots``).  There is no permission question -- the answer
    is scoped to ``request.user``'s own lots, matched the way ``filters.UserLotFilter`` matches
    them, so one person can never be handed another's numbers.  The lot set goes in as a subquery
    rather than a list of primary keys: it is one round trip, and it keeps the PageView query
    bounded by owner in the database instead of in Python.
    """

    def get(self, request):
        lots = (
            Lot.objects.exclude(is_deleted=True)
            .filter(Q(user=request.user) | Q(auctiontos_seller__user=request.user))
            .order_by()
            .values("pk")
        )
        return render(
            request,
            "auctions/page_view_history_modal.html",
            page_view_history_context(
                request,
                PageView.objects.filter(lot_number__in=lots),
                title="How people found your lots",
                subtitle="Every lot you are selling",
            ),
        )


class ViewLot(DetailView):
    """Show the picture and detailed information about a lot, and allow users to place bids"""

    template_name = "view_lot_images.html"
    model = Lot
    custom_lot_number = None
    auction_slug = None
    enable_404 = True

    def dispatch(self, request, *args, **kwargs):
        self.auction_slug = kwargs.pop("slug", None)
        self.custom_lot_number = kwargs.pop("custom_lot_number", None)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        obj = self.get_queryset().first()
        if not obj and self.enable_404:
            raise Http404
        return obj

    def get_queryset(self):
        pk = self.kwargs.get(self.pk_url_kwarg)
        qs = Lot.objects.exclude(is_deleted=True)
        latitude = self.request.COOKIES.get("latitude")
        longitude = self.request.COOKIES.get("longitude")
        if latitude and longitude:
            qs = qs.annotate(distance=distance_to(latitude, longitude))
        elif self.request.user.is_authenticated:
            # UserData is auto-created when user is saved
            if self.request.user.userdata.latitude and self.request.user.userdata.longitude:
                latitude = self.request.user.userdata.latitude
                longitude = self.request.user.userdata.longitude
                if latitude and longitude:
                    qs = qs.annotate(distance=distance_to(latitude, longitude))
        if pk:
            qs = qs.filter(pk=pk)
        else:
            # we are probably here form the auction/custom lot number route
            filters = Q(
                # legacy lot numbers in auctions
                auction__isnull=False,
                auction__slug=self.auction_slug,
                auction__use_seller_dash_lot_numbering=True,
                custom_lot_number__isnull=False,
                custom_lot_number=self.custom_lot_number,
            )

            if self.custom_lot_number.isnumeric():
                filters |= Q(
                    # autogenerated int lot numbers in auctions
                    auction__isnull=False,
                    auction__slug=self.auction_slug,
                    auction__use_seller_dash_lot_numbering=False,
                    lot_number_int__isnull=False,
                    lot_number_int=self.custom_lot_number,
                )

            qs = qs.filter(filters)
        return qs

    def get_context_data(self, **kwargs):
        lot = self.get_object()
        context = super().get_context_data(**kwargs)
        context["domain"] = Site.objects.get_current().domain
        context["is_auction_admin"] = False
        if lot.auction:
            context["auction"] = lot.auction
            context["is_auction_admin"] = lot.auction.permission_check(self.request.user)
            if lot.auction.first_bid_payout and not lot.auction.invoiced:
                if not self.request.user.is_authenticated or not Bid.objects.exclude(is_deleted=True).filter(
                    user=self.request.user, lot_number__auction=lot.auction
                ):
                    messages.info(
                        self.request,
                        f"Bid on (and win) any lot in {lot.auction} and get ${lot.auction.first_bid_payout} back!",
                    )
        if self.request.user.is_authenticated:
            viewer_bid = (
                Bid.objects.exclude(is_deleted=True)
                .filter(user=self.request.user, lot_number=lot.pk)
                .order_by("-bid_time")
                .first()
            )
            if viewer_bid:
                context["viewer_bid_pk"] = viewer_bid.pk
                context["viewer_bid"] = viewer_bid.amount
                if lot.auction and not lot.auction.only_whole_dollar_bids:
                    defaultBidAmount = viewer_bid.amount + Decimal("0.01")
                else:
                    defaultBidAmount = viewer_bid.amount + 1
            else:
                defaultBidAmount = 0
                context["viewer_bid"] = None
            # When the app can be reached it is the only channel used (see
            # notify_watchers_lot_selling_soon), so the browser subscribe UI is replaced by a note
            # pointing at the phone. Inside the app's own WebView there is nothing to subscribe to
            # either -- a WebView has no Push API -- so the button is dropped there too.
            context["has_app_push"] = user_has_app_push(self.request.user)
            context["can_subscribe_to_webpush"] = not context["has_app_push"] and not getattr(
                self.request, "is_mobile_app", False
            )
            context["has_push_subscription"] = (
                context["has_app_push"] or PushInformation.objects.filter(user=self.request.user).exists()
            )
        else:
            defaultBidAmount = 0
            context["viewer_bid"] = None
            context["has_app_push"] = False
            context["can_subscribe_to_webpush"] = False
            context["has_push_subscription"] = False
        if lot.auction and lot.auction.online_bidding == "buy_now_only" and lot.buy_now_price:
            defaultBidAmount = lot.buy_now_price
            context["force_buy_now"] = True
        else:
            context["force_buy_now"] = False
        if not lot.sealed_bid:
            # reserve price if there are no bids
            if not lot.high_bidder:
                defaultBidAmount = lot.reserve_price
            else:
                if lot.auction and not lot.auction.only_whole_dollar_bids:
                    # 5% rounded down to nearest cent, minimum $0.01
                    min_increment = max(
                        (lot.high_bid * Decimal("0.05")).quantize(Decimal("0.01"), rounding="ROUND_DOWN"),
                        Decimal("0.01"),
                    )
                else:
                    # 5% rounded down to nearest dollar, minimum $1
                    min_increment = max(
                        (lot.high_bid * Decimal("0.05")).to_integral_value(rounding="ROUND_DOWN"),
                        Decimal(1),
                    )
                if defaultBidAmount > lot.high_bid + min_increment:
                    pass
                else:
                    defaultBidAmount = lot.high_bid + min_increment
        context["viewer_pk"] = self.request.user.pk
        context["submitter_pk"] = getattr(lot.user, "pk", 0)
        context["user_specific_bidding_error"] = False
        if not self.request.user.is_authenticated:
            context["user_specific_bidding_error"] = format_html(
                "You have to <a href='/login/?next={}'>sign in</a> to place bids.", lot.lot_link
            )
        if context["viewer_pk"] == context["submitter_pk"]:
            context["user_specific_bidding_error"] = "You can't bid on your own lot"
        context["amount"] = defaultBidAmount
        context["only_whole_dollar_bids"] = lot.auction.only_whole_dollar_bids if lot.auction else True
        context["watched"] = Watch.objects.filter(lot_number=lot.lot_number, user=self.request.user.id)
        context["category"] = lot.species_category
        # context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':lot.pk, "amount":defaultBidAmount}, request=self.request)
        context["user_tos"] = None
        context["user_tos_location"] = None
        if lot.auction and self.request.user.is_authenticated:
            # same resolver the bid gate uses (newest record wins), so the UI can't
            # disagree with enforcement when duplicate TOS records exist
            tos = lot.auction.tos_for_user(self.request.user)
            if tos:
                context["user_tos"] = True
                context["user_tos_location"] = tos.pickup_location
                if not tos.can_bid_in_auction:
                    if tos.requires_check_in_before_bidding:
                        context["user_specific_bidding_error"] = "You must check in at the event before you can bid"
                    else:
                        context["user_specific_bidding_error"] = (
                            "This auction requires admin approval before you can bid"
                        )
            else:
                context["user_specific_bidding_error"] = format_html(
                    "This lot is part of <b>{}</b>. Please <a href='/auctions/{}/?next={}#join'>read the auction's rules and join the auction</a> to bid<br>",
                    lot.auction,
                    lot.auction.slug,
                    lot.lot_link,
                )
            if not lot.auction.is_online and lot.auction.message_users_when_lots_sell:
                context["push_notifications_possible"] = True
                # Ask the app to offer notifications here, where the offer means something: this is
                # an in-person auction that pushes "your lot is selling now", the user is looking at
                # a lot in it, and the auction is still running. The app owns the "at most once per
                # device" part and simply ignores the call when it has already asked. Deciding it
                # here is the point -- the app can't tell an in-person lot page from any other, and
                # won't spend a round trip per lot page guessing.
                context["offer_push_prompt"] = (
                    getattr(self.request, "is_mobile_app", False)
                    and not lot.auction.pretty_much_over
                    and not (
                        self.request.user.userdata.push_notifications_when_lots_sell
                        and self.request.user.userdata.has_push_device
                    )
                )
        if lot.within_dynamic_end_time and lot.minutes_to_end > 0 and not lot.sealed_bid:
            messages.info(
                self.request,
                "Bidding is ending soon.  Bids placed now will extend the end time of this lot.  This page will update automatically, you don't need to reload it",
            )
        if not context["user_tos"] and not lot.ended and lot.auction:
            if lot.auction.online_bidding != "disable":
                messages.info(
                    self.request,
                    format_html(
                        "Please <a href='/auctions/{}/?next=/lots/{}/'>read the auction's rules and join the auction</a> to bid",
                        lot.auction.slug,
                        lot.pk,
                    ),
                    extra_tags="safe",
                )
        if self.request.user.is_authenticated:
            userData = self.request.user.userdata
            userData.last_activity = timezone.now()
            userData.save()
            if userData.last_ip_address:
                if userData.last_ip_address != lot.seller_ip and lot.bidder_ip_same_as_seller:
                    messages.info(
                        self.request,
                        "Heads up: one of the bidders on this lot has the same IP address as the seller of this lot.  This can happen when someone is bidding on their own lots.  Never bid more than a lot is worth to you.",
                    )
        if lot.user:
            if lot.user.pk == self.request.user.pk:
                LotHistory.objects.filter(lot=lot.pk, seen=False).update(seen=True)
        context["bids"] = []
        if lot.auction:
            if context["is_auction_admin"]:
                context["bids"] = lot.bids
        context["debug"] = settings.DEBUG
        try:
            if lot.local_pickup:
                context["distance"] = f"{int(lot.distance)} miles away"
            else:
                distances = [25, 50, 100, 200, 300, 500, 1000, 2000, 3000]
                for distance in distances:
                    if lot.distance < distance:
                        context["distance"] = f"less than {distance} miles away"
                        break
                if lot.distance > 3000:
                    context["distance"] = "over 3000 miles away"
        except (AttributeError, TypeError):
            context["distance"] = 0
        # for lots that are part of an auction, it's very handy to show the exchange info right on the lot page
        # this should be visible only to people running the auction or the seller
        if lot.auction and lot.auction.is_online and lot.sold:
            if context["is_auction_admin"] or lot.is_owned_by(self.request.user):
                context["show_exchange_info"] = True
        context["show_image_add_button"] = lot.image_permission_check(self.request.user)
        context["show_bap_badge"] = False
        context["bap_eligible_reason"] = None
        context["bap_eligible_reason_display"] = None
        if lot.auction and lot.auction.club:
            seller_user = lot.user or (lot.auctiontos_seller.user if lot.auctiontos_seller else None)
            viewer = self.request.user
            viewer_is_seller = viewer.is_authenticated and seller_user and viewer == seller_user
            viewer_has_bap = viewer.is_authenticated and check_club_permission(
                viewer, lot.auction.club, "permission_manage_bap"
            )
            if viewer_is_seller or viewer_has_bap:
                context["show_bap_badge"] = True
                if lot.ended and not lot.sold:
                    club = lot.auction.club if lot.auction else None
                    reason = "not_sold" if (not club or club.only_sold_lots) else lot.unsold_lot_no_bap_reason
                else:
                    reason = lot.unsold_lot_no_bap_reason
                context["bap_eligible_reason"] = reason
                if reason:
                    context["bap_eligible_reason_display"] = dict(lot.BAP_REASON_CHOICES).get(reason, reason)
            context["viewer_has_bap"] = viewer_has_bap
            if viewer_has_bap and lot.sold:
                club = lot.auction.club
                context["bap_club"] = club
                context["bap_default_points"] = lot.bap_points_for_club(club)
        is_lot_creator = lot.is_owned_by(self.request.user)
        # The template gates the edit/delete/deactivate buttons and the seller-only notes on this,
        # rather than on lot.user, because lot.user is null on lots added through an unlinked TOS.
        context["is_lot_creator"] = is_lot_creator
        if lot.use_images_from and is_lot_creator:
            context["images_managed_from_lot"] = lot.use_images_from
        # The seller of a lot in an in-person auction gets the per-source view breakdown: the AR
        # sources only exist there, and how people found the lot in the room is useful to them and
        # to nobody else. See Lot.page_view_source_breakdown.
        context["show_page_view_breakdown"] = bool(
            is_lot_creator and lot.auction and not lot.auction.is_online and not lot.sealed_bid
        )
        # The 15-day history modal is the other half of that: online (and auction-less) lots, where
        # there is no in-room scanning to break down, and open to the auction's admins as well as
        # the seller. can_see_lot_page_view_history is the one rule; LotPageViewHistoryView asks it
        # again, so this flag only decides whether the button is drawn.
        context["show_page_view_history"] = can_see_lot_page_view_history(self.request.user, lot)
        # chat subscription stuff
        if self.request.user.is_authenticated:
            context["show_chat_subscriptions_checkbox"] = True
            context["autocheck_chat_subscriptions"] = "false"
            existing_subscription = ChatSubscription.objects.filter(lot=lot, user=self.request.user).first()
            if is_lot_creator and not self.request.user.userdata.email_me_when_people_comment_on_my_lots:
                context["show_chat_subscriptions_checkbox"] = False
            if not is_lot_creator and not self.request.user.userdata.email_me_about_new_chat_replies:
                context["show_chat_subscriptions_checkbox"] = False
            if self.request.user.userdata.email_me_about_new_chat_replies and not existing_subscription:
                context["autocheck_chat_subscriptions"] = "true"
            if existing_subscription:
                context["chat_subscriptions_is_checked"] = not existing_subscription.unsubscribed
                context["autocheck_chat_subscriptions"] = "false"
            else:
                context["chat_subscriptions_is_checked"] = False
        if (
            lot.auctiontos_winner
            and self.request.user.is_authenticated
            and self.request.user.email == lot.auctiontos_winner.email
        ) or (lot.winner and self.request.user.is_authenticated and self.request.user == lot.winner):
            if lot.feedback_rating == 0 and lot.date_end and timezone.now() > lot.date_end + timedelta(days=2):
                context["show_feedback_dialog"] = True
        return context


class ViewLotSimple(ViewLot, AuctionViewMixin):
    """Minimalist view of a lot, just image and description.  For htmx calls"""

    template_name = "view_lot_simple.html"
    enable_404 = False

    def get_context_data(self, **kwargs):
        context = DetailView.get_context_data(self, **kwargs)
        lot = self.get_object()
        context["lot"] = lot
        if lot and lot.auction:
            self.auction = lot.auction
            if self.is_auction_admin and self.auction.message_users_when_lots_sell and not lot.sold:
                # The websocket chat message is transient and keeps firing on every view.
                result = {
                    "type": "chat_message",
                    "info": "CHAT",
                    "message": "This lot is about to be sold!",
                    "pk": -1,
                    "username": "System",
                }
                lot.send_websocket_message(result)
                # Web push goes through the deduped helper so a lot that already notified from the
                # queue does not notify again when it's pulled up to be sold (and vice versa).
                notify_watchers_lot_selling_soon(lot, request_user=self.request.user)
        return context


class ImageCreateView(LoginRequiredMixin, CreateView):
    """Add an image to a lot"""

    model = LotImage
    template_name = "image_form.html"
    form_class = CreateImageForm

    def get_lot(self, request, *args, **kwargs):
        return get_object_or_404(Lot, pk=kwargs["lot"], is_deleted=False)

    def dispatch(self, request, *args, **kwargs):
        self.lot = self.get_lot(self, request, *args, **kwargs)
        if not self.lot:
            messages.info(
                request,
                f"All lots for {self.tos.bidder_number} already have an image",
            )
            return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        # try:
        #     self.lot = Lot.objects.get(lot_number=kwargs["lot"], is_deleted=False)
        # except:
        #     raise Http404
        if not self.lot.image_permission_check(request.user):
            messages.error(request, "You can't add an image to this lot")
            return redirect(self.get_success_url())
        if self.lot.image_count > 5:
            messages.error(
                request,
                "You can't add another image to this lot.  Delete one and try again",
            )
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = self.lot.lot_link
        return data["next"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"Add image to {self.lot.lot_name}"
        if self.lot.use_images_from:
            context["images_managed_from_lot"] = self.lot.use_images_from
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = "RANDOM"
        # Anything Pillow can't write as a JPEG (an animated GIF, an MPO from a phone's burst
        # mode) was already converted by CreateImageForm.clean_image -- see forms.jpeg_safe_upload.
        try:
            image.save()
        except IMAGE_PROCESSING_EXCEPTIONS as e:
            # The image itself is unusable (bad format, corrupt, decompression bomb...).
            # Show the uploader a friendly, actionable error.
            logger.info("Rejected lot image during save: %s", e)
            form.add_error(
                "image",
                "We couldn't process that image -- it may be corrupt or in an unsupported format. "
                "Please try a different photo.",
            )
            return self.form_invalid(form)
        # Anything else (permission denied writing to mediafiles, disk full, database
        # errors...) is a server/site problem, not the user's file. Let it propagate so it
        # becomes a 500 and the admins get emailed instead of blaming the uploader's photo.
        return super().form_valid(form)


class QuickBulkAddImages(ImageCreateView):
    """Add images to any lots that don't have one"""

    def get_lot(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.tos = get_object_or_404(AuctionTOS, bidder_number=kwargs.pop("bidder_number"), auction=self.auction)
        return (
            Lot.objects.filter(auctiontos_seller=self.tos, winning_price__isnull=True)
            .exclude(lotimage__isnull=False)
            .distinct()
            .order_by("date_posted")
            .first()
        )

    def get_success_url(self):
        return reverse("bulk_add_image", kwargs={"slug": self.auction.slug, "bidder_number": self.tos.bidder_number})


class ImageUpdateView(UpdateView):
    """Edit an existing image"""

    model = LotImage
    template_name = "image_form.html"
    form_class = CreateImageForm

    def dispatch(self, request, *args, **kwargs):
        try:
            self.lot = self.get_object().lot_number
        except AttributeError:
            raise Http404
        if not self.lot.image_permission_check(request.user):
            messages.error(request, "You can't change this image")
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return self.get_object().lot_number.lot_link

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"Editing image for {self.get_object().lot_number.lot_name}"
        if self.lot.use_images_from:
            context["images_managed_from_lot"] = self.lot.use_images_from
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = "RANDOM"
        try:
            image.save()
        except IMAGE_PROCESSING_EXCEPTIONS as e:
            # Same split as ImageCreateView: an unusable file is the uploader's problem and
            # gets a friendly inline error, while a disk or permission error is ours and is
            # left to become a 500 so the admins hear about it.
            logger.info("Rejected lot image during save: %s", e)
            form.add_error(
                "image",
                "We couldn't process that image -- it may be corrupt or in an unsupported format. "
                "Please try a different photo.",
            )
            return self.form_invalid(form)
        messages.success(self.request, "Image updated")
        return super().form_valid(form)


class LotValidation(LoginRequiredMixin):
    """
    Base class for adding a lot.  This defines the rules for validating a lot
    """

    auction = None  # used for specifying which auction via GET param

    def dispatch(self, request, *args, **kwargs):
        # if the user hasn't filled out their address, redirect:
        userData = request.user.userdata
        if not userData.address or not request.user.first_name or not request.user.last_name:
            messages.error(self.request, "Please fill out your contact info before creating a lot")
            return redirect(f"{reverse('contact_info')}?{urlencode({'next': request.get_full_path()})}")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form, **kwargs):
        """
        There is quite a lot that needs to be done before the lot is saved
        """
        lot = form.save(commit=False)
        if lot.auction and lot.auction.user_banned_by_admins(self.request.user):
            # CreateUserBan sweeps the banned user's existing lots out of the auction;
            # without this, they could simply resubmit them
            form.add_error(None, "You've been banned from selling lots in this auction")
            return self.form_invalid(form)
        lot.user = self.request.user
        lot.date_of_last_user_edit = timezone.now()
        if lot.buy_now_price:
            if lot.buy_now_price < lot.reserve_price:
                lot.buy_now_price = lot.reserve_price
                messages.error(
                    self.request,
                    "Buy now price can't be lower than the minimum bid.  Buy now price has been set to the minimum bid, but you should probably edit this lot and change the buy now price.",
                )
        if lot.auction:
            # if not lot.auction.is_online:
            #    if lot.buy_now_price or lot.reserve_price > lot.auction.minimum_bid:
            #        messages.info(self.request, f"Reserve and buy now prices may not be used in this auction.  Read the auction's rules for more information")
            if lot.auction.reserve_price == "disable":
                lot.reserve_price = lot.auction.minimum_bid
            if lot.auction.buy_now == "disable" and lot.buy_now_price:
                lot.buy_now_price = None
            if (lot.auction.buy_now == "require") and not lot.buy_now_price:
                lot.buy_now_price = lot.auction.minimum_bid
                messages.error(self.request, "You need to set a buy now price for this lot!")
            lot.date_end = lot.auction.date_end
            userData = self.request.user.userdata
            userData.last_auction_used = lot.auction
            userData.last_activity = timezone.now()
            userData.save()
            auctiontos = AuctionTOS.objects.filter(user=self.request.user, auction=lot.auction).first()
            if not auctiontos:
                # it should not be possible to get here (famous last words...)
                # remember that on form submit in CreateLotForm.clean(), we are validating that the user has an auctiontos
                messages.error(
                    self.request,
                    format_html(
                        "You need to <a href='/auctions/{}'>confirm your pickup location for this auction</a> before people can bid on this lot.",
                        lot.auction.slug,
                    ),
                    extra_tags="safe",
                )
            else:
                lot.auctiontos_seller = auctiontos
                invoice = Invoice.objects.filter(auctiontos_user=auctiontos, auction=lot.auction).first()
                if not invoice:
                    invoice = Invoice.objects.create(auctiontos_user=auctiontos, auction=lot.auction)
                invoice.recalculate()
        else:
            # this lot is NOT part of an auction
            try:
                run_duration = int(form.cleaned_data["run_duration"])
            except (ValueError, KeyError):
                run_duration = 10
            if not lot.date_posted:
                lot.date_posted = timezone.now()
            lot.date_end = lot.date_posted + timedelta(days=run_duration)
            lot.lot_run_duration = run_duration
            lot.donation = False
        # someday we may change this to be a field on the form, but for now we need to collect data
        lot.promotion_weight = randint(0, 20)
        lot_is_new = not lot.pk
        if lot.pk:
            # this is an existing lot
            lot.save()
        else:
            # this is a new lot
            lot.added_by = self.request.user
            lot.save()
            # if this was cloned from another lot, get the images from that lot
            if form.cleaned_data["cloned_from"]:
                try:
                    original_lot = Lot.objects.get(pk=form.cleaned_data["cloned_from"], is_deleted=False)
                    if user_can_clone_lot(self.request.user, original_lot):
                        copy_lot_images(original_lot, lot)
                except Exception as e:
                    logger.exception(e)
            msg = mark_safe("Created lot! ")
            if not lot.image_count:
                msg += format_html(
                    "You should probably <a href='/images/add_image/{}/'>add an image</a>  to this lot.  Or, ",
                    lot.lot_number,
                )
            msg += mark_safe("<a href='/lots/new'>create another lot</a>")
            messages.success(
                self.request,
                msg,
                extra_tags="safe",
            )
        # if image_url is set, add an image to the lot using this URL, then clear the field
        image_url = form.cleaned_data.get("image_url")
        if image_url:
            try:
                validate_image_url(image_url)
                # check direct images on this lot (not delegated via use_images_from) for is_primary
                LotImage.objects.create(
                    lot_number=lot,
                    url=image_url,
                    is_primary=not LotImage.objects.filter(lot_number=lot).exists(),
                    image_source="RANDOM",
                )
            except ValidationError:
                messages.error(self.request, "The image URL provided was not valid and will not be used.")
            lot.image_url = None
            lot.save(update_fields=["image_url"])
        # What the seller did with the species the matcher offered for this lot name.  This form
        # never *writes* to the shared name cache -- only the admin's lot editor and the bulk-add
        # page do -- but it is one of the places a wrong remembered answer is visibly taken off a
        # lot, and that is evidence worth keeping.  See species_matching.record_choice.
        if lot.auction and lot.auction.use_scientific_name and lot.lot_name:
            record_species_choice(
                lot.lot_name, lot.species, first_save=lot_is_new, changed="species" in form.changed_data
            )
        return super().form_valid(form)

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["auction"] = self.auction
        kwargs["user"] = self.request.user
        data = self.request.GET.copy()
        kwargs["cloned_from"] = data.get("copy", None)
        return kwargs


class LotCreateView(LotValidation, CreateView):
    """
    Creating a new lot
    """

    model = Lot
    template_name = "lot_form.html"
    form_class = CreateLotForm
    auction = None

    # it's better to take the user to the lot they just added, in case they want to edit it
    # def get_success_url(self):
    #    return "/lots/new/"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "New lot"
        context["new"] = True

        # Check if user needs to see the modal about joining an auction
        userData = self.request.user.userdata
        can_sell_independently = userData.can_submit_standalone_lots

        # Get available auctions for this user
        available_auctions = userData.available_auctions_to_submit_lots

        # Show modal if user can't sell independently and has no available auctions
        context["show_no_auction_modal"] = not can_sell_independently and not available_auctions.exists()
        context["last_auction_name"] = None
        context["lot_submission_ended_message"] = None

        # If they have a last used auction, check if lot submission has ended
        if userData.last_auction_used and context["show_no_auction_modal"]:
            last_auction = userData.last_auction_used
            context["last_auction_name"] = last_auction.title
            if last_auction.lot_submission_end_date and last_auction.lot_submission_end_date < timezone.now():
                context["lot_submission_ended_message"] = (
                    f"Lot submission has ended for the {last_auction.title} auction"
                )

        return context

    def get_initial(self):
        """Pre-fill form fields from GET params. Any field in the form can be set this way.
        The 'auction' param is handled separately in dispatch() and 'cloned_from' in get_form_kwargs()."""
        initial = super().get_initial()
        exclude = {"auction", "cloned_from"}
        form_fields = set(self.form_class.Meta.fields) | set(self.form_class.declared_fields)
        field_objects = getattr(self.form_class, "base_fields", {})
        # Identify checkbox-like fields so we can coerce their initial values properly
        checkbox_fields = {
            name
            for name, field in field_objects.items()
            if getattr(getattr(field, "widget", None), "input_type", None) == "checkbox"
        }
        true_values = {"1", "true", "yes", "on"}
        false_values = {"0", "false", "no", "off"}
        for key, values in self.request.GET.lists():
            if key in form_fields and key not in exclude:
                field = field_objects.get(key)
                if field is not None and getattr(field.widget, "allow_multiple_selected", False):
                    initial[key] = values
                elif key in checkbox_fields:
                    # For checkbox fields, use the last value (multiple values shouldn't occur)
                    normalized = values[-1].strip().lower() if values else ""
                    if normalized in true_values:
                        initial[key] = True
                    elif normalized in false_values:
                        initial[key] = False
                    else:
                        initial[key] = values[-1] if values else ""
                else:
                    # For single-value fields, last value wins (mirrors QueryDict.items() behavior)
                    if values:
                        initial[key] = values[-1]
        return initial

    def form_valid(self, form, **kwargs):
        """When a new lot is created, make sure to create an invoice for the seller"""
        lot = form.save(commit=False)
        if lot.auction and lot.auctiontos_seller:
            invoice = Invoice.objects.filter(auctiontos_user=lot.auctiontos_seller, auction=lot.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=lot.auctiontos_seller, auction=lot.auction)
            invoice.recalculate()
        result = super().form_valid(form, **kwargs)
        # Create history after lot is saved and has a lot_number_display
        if lot.auction and lot.auctiontos_seller:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Added lot {lot.lot_number_display} {lot.lot_name}",
                user=self.request.user,
            )
        return result

    def dispatch(self, request, *args, **kwargs):
        userData = self.request.user.userdata
        if userData.last_auction_used:
            if (
                userData.last_auction_used.can_submit_lots
                and not userData.last_auction_used.is_online
                and userData.last_auction_used.allow_bulk_adding_lots
            ):
                messages.info(
                    request,
                    format_html(
                        "Sick of adding lots one at a time?  <a href='{}'>Add lots of lots to {}</a>",
                        reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": userData.last_auction_used.slug}),
                        userData.last_auction_used,
                    ),
                    extra_tags="safe",
                )
        data = self.request.GET.copy()
        auction_slug = data.get("auction", None)
        if auction_slug:
            self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=auction_slug).first()
            if self.auction:
                error = None
                if timezone.now() < self.auction.lot_submission_start_date:
                    error = "Lot submission has not opened yet for this auction."
                if self.auction.lot_submission_end_date:
                    if self.auction.lot_submission_end_date < timezone.now():
                        error = "Lot submission has ended for this auction."
                tos = AuctionTOS.objects.filter(user=self.request.user, auction=self.auction).first()
                if not tos:
                    error = "You haven't joined this auction yet.  Click the green button at the bottom of this page to join the auction.</a>"
                if error:
                    messages.error(request, error)
                    return redirect(self.auction.get_absolute_url())
        return super().dispatch(request, *args, **kwargs)


class LotUpdate(LotValidation, UpdateView):
    """
    Changing an existing lot
    This is almost identical to the create view, but needs to verify permissions to edit the lot
    """

    model = Lot
    template_name = "lot_form.html"
    form_class = CreateLotForm

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().is_owned_by(request.user)):
            messages.error(request, "Only the lot creator can edit a lot")
            return redirect(reverse("home"))
        if not self.get_object().can_be_edited:
            messages.error(request, self.get_object().cannot_be_edited_reason)
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse("selling")
        # return f"/lots/{self.kwargs['pk']}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"Edit {self.get_object().lot_name}"
        return context

    def form_valid(self, form):
        """Track history when a lot is edited"""
        lot = self.get_object()
        # Check if we should create history before saving
        should_create_history = lot.auction and form.has_changed()
        # Save the form
        result = super().form_valid(form)
        # Create history after successful update
        if should_create_history:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Edited lot {lot.lot_number_display}",
                user=self.request.user,
                form=form,
            )
        return result


class AuctionDelete(LoginRequiredMixin, AuctionViewMixin, DeleteView):
    model = Auction

    def dispatch(self, request, *args, **kwargs):
        result = super().dispatch(request, *args, **kwargs)
        # self.auction may not be set if LoginRequiredMixin redirected
        if hasattr(self, "auction") and self.auction and not self.auction.can_be_deleted:
            messages.error(request, "There are already lots in this auction, it can't be deleted")
            return redirect(reverse("home"))
        return result

    def get_success_url(self):
        return reverse("auctions")


class LotDelete(LoginRequiredMixin, DeleteView):
    model = Lot

    def dispatch(self, request, *args, **kwargs):
        if not self.get_object().can_be_deleted:
            messages.error(request, self.get_object().cannot_be_deleted_reason)
            return redirect(reverse("home"))
        if not (request.user.is_superuser or self.get_object().is_owned_by(request.user)):
            messages.error(request, "Only the creator of a lot can delete it")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        next_url = self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        return reverse("selling")

    def form_valid(self, form):
        """Track history when a lot is deleted"""
        lot = self.get_object()
        if lot.auction:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Deleted lot {lot.lot_number_display}",
                user=self.request.user,
            )
        messages.info(self.request, f"Successfully deleted lot {lot.lot_number_display} {lot.lot_name}")
        return super().form_valid(form)


class ImageDelete(LoginRequiredMixin, DeleteView):
    model = LotImage

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().lot_number.is_owned_by(request.user):
            auth = True
        if not self.get_object().lot_number.can_be_edited:
            auth = False
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "You can't change this image")
            return redirect(self.get_object().lot_number.get_absolute_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        if self.get_object().is_primary:
            # in this case, we need to set a new primary image
            try:
                newImage = (
                    LotImage.objects.filter(lot_number=self.get_object().lot_number)
                    .exclude(pk=self.get_object().pk)
                    .order_by("createdon")[0]
                )
                newImage.is_primary = True
                newImage.save()
            except IndexError:
                pass
        return self.get_object().lot_number.get_absolute_url()


class BidDelete(LoginRequiredMixin, DeleteView):
    model = Bid
    removing_own_bid = False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["removing_own_bid"] = self.removing_own_bid
        return context

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if not self.get_object().lot_number.bids_can_be_removed:
            messages.error(request, "You can no longer remove bids from this lot.")
            return redirect(self.get_success_url())
        if self.get_object().lot_number.auction:
            if self.get_object().lot_number.auction.allow_deleting_bids and request.user == self.get_object().user:
                if request.user == self.get_object().user:
                    self.removing_own_bid = True
                    auth = True
            if self.get_object().lot_number.auction.permission_check(self.request.user):
                auth = True
        if not auth:
            messages.error(
                request,
                "Your account doesn't have permission to remove bids from this lot",
            )
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        bid = self.get_object()
        lot = bid.lot_number
        success_url = self.get_success_url()
        if self.removing_own_bid:
            own_bid_removal_messages = [
                "{user} got cold feet and withdrew their bid!",
                "{user} has bravely retreated from the bidding war!",
                "And just like that, {user}'s bid vanished into thin air!",
                "The elusive {user} has chickened out and removed their bid!",
                "Looks like {user} couldn't handle the heat and pulled their bid!",
                "{user} just remembered they haven't paid rent this month and removed their bid!",
            ]
            history_message = secrets.choice(own_bid_removal_messages).format(user=self.request.user)
        else:
            history_message = f"{self.request.user} has removed {bid.user}'s bid"
        if lot.ended:
            lot.winner = None
            lot.auctiontos_winner = None
            lot.winning_price = None
            if lot.auction and lot.auction.date_end:
                lot.date_end = lot.auction.date_end
            else:
                lot.date_end = timezone.now() + timedelta(days=lot.lot_run_duration)
            lot.active = True
            lot.buy_now_used = False
            if lot.label_printed:
                lot.label_needs_reprinting = True
            lot.save()
        bid.delete()
        # Also soft-delete any other bid records for this user on the same lot
        Bid.objects.exclude(is_deleted=True).filter(
            user=bid.user,
            lot_number=lot,
        ).update(is_deleted=True)
        LotHistory.objects.create(lot=lot, user=self.request.user, message=history_message, changed_price=True)
        return HttpResponseRedirect(success_url)

    def get_success_url(self):
        return self.get_object().lot_number.get_absolute_url()


class LotAdmin(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Creation and management for Lots that are part of an auction"""

    template_name = "auctions/generic_admin_form.html"
    form_class = EditLot
    model = Lot

    def get_queryset(self):
        return Lot.objects.all()

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop("pk")
        try:
            self.lot = Lot.objects.get(pk=pk, is_deleted=False)
        except Exception:
            raise Http404
        if self.lot.auction:
            self.auction = self.lot.auction
        else:
            raise Http404
        self.is_auction_admin
        self.lot_initial_winner = self.lot.auctiontos_winner
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["lot"] = self.lot
        form_kwargs["user"] = self.request.user
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = ""
        context["modal_title"] = f"Edit lot {self.lot.lot_number_display}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            if form.has_changed():
                self.lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"Edited lot {self.lot.lot_number_display}:",
                    user=self.request.user,
                    form=form,
                )
                # Check if only winner and winning_price were changed
                changed_fields = set(form.changed_data)
                winner_fields = {"auctiontos_winner", "winning_price"}
                if changed_fields and changed_fields.issubset(winner_fields):
                    quick_set_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.auction.slug})
                    messages.info(
                        self.request,
                        format_html(
                            "You're doing things the hard way - <a href='{}'>quick set lot winners</a> page lets you mark lots sold much more quickly.",
                            quick_set_url,
                        ),
                        extra_tags="safe",
                    )
            obj = self.lot
            # obj.custom_lot_number = form.cleaned_data["custom_lot_number"]
            obj.lot_name = form.cleaned_data["lot_name"] or "Unknown lot"
            category = form.cleaned_data["species_category"]
            if not category:
                category = Category.objects.filter(name="Uncategorized").first()
            obj.species_category = category
            obj.summernote_description = form.cleaned_data["summernote_description"]
            # obj.auctiontos_seller = form.cleaned_data['auctiontos_seller'] or request.user
            obj.quantity = form.cleaned_data["quantity"] or 1
            obj.donation = form.cleaned_data["donation"]
            obj.i_bred_this_fish = form.cleaned_data["i_bred_this_fish"]
            obj.reserve_price = form.cleaned_data["reserve_price"]
            obj.buy_now_price = form.cleaned_data["buy_now_price"]
            obj.banned = form.cleaned_data["banned"]
            obj.auctiontos_winner = form.cleaned_data["auctiontos_winner"]
            obj.winning_price = form.cleaned_data["winning_price"]
            obj.custom_checkbox = form.cleaned_data["custom_checkbox"]
            obj.custom_field_1 = form.cleaned_data["custom_field_1"]
            obj.custom_dropdown = form.cleaned_data["custom_dropdown"]
            # This view assigns field by field rather than calling form.save(), and the scientific
            # name was simply not on the list -- so the picker rendered, validated, and had its
            # answer thrown away on every save since it was added.  It is the *admin's* lot form:
            # the one place a wrong species is meant to get fixed.
            #
            # Guarded on the auction's own setting rather than trusting cleaned_data, because
            # EditLot is built without an ``instance``: clean_species_for_auction falls back to
            # "whatever is stored on the lot" when the field is switched off, and what it actually
            # reads is a blank Lot() -- so assigning that would wipe the column on every auction
            # that has scientific names turned off.  Turning the setting off hides the field; it
            # does not throw the data away.
            species = form.cleaned_data.get("species") if self.auction.use_scientific_name else None
            species_changed = bool(self.auction.use_scientific_name) and obj.species_id != getattr(species, "pk", None)
            if self.auction.use_scientific_name:
                obj.species = species
            # need to make sure the winner matches the auctiontos_winner
            if obj.pk and obj.winner:
                if not obj.auctiontos_winner:
                    obj.winner = None
                elif obj.auctiontos_winner.user:
                    obj.winner = obj.auctiontos_winner.user
                # winner not set if auctiontos_winner is set for the first time...don't see a real downside here, winner is generally not set as part of an auction anyway
            obj.save()
            # Teach the site the pairing -- but only from here, and only on a real change.  This
            # form has the "search every species" box on it, so the choice is not bounded by the
            # five suggestions the matcher produced; it can be any of 36,000 rows, and the cache
            # is read by every club ahead of the token search.  What makes it safe to write anyway
            # is who is doing it: this view is auction admins only, they are correcting a lot on
            # purpose, and the answer is listed and revertible on the species gaps page.  The
            # seller-facing forms deliberately do not do this.
            if self.auction.use_scientific_name and species_changed and obj.lot_name:
                # An admin moving a lot off the species it was given is the clearest rejection
                # there is of whatever the matcher remembered for this name.  Never an *accept*:
                # this form is only ever a later edit, and re-saving a lot to set its winner is
                # not somebody confirming the species.  See species_matching.record_choice.
                record_species_choice(obj.lot_name, species, first_save=False, changed=True)
            if species_changed and species and obj.lot_name:
                remember_species(obj.lot_name, species, source="user", user=self.request.user)
            # add message if the winner changed
            if obj.auctiontos_winner:
                if self.lot_initial_winner != obj.auctiontos_winner:
                    try:
                        obj.add_winner_message(self.request.user, obj.auctiontos_winner, obj.winning_price)
                    except Exception:
                        logger.exception("add_winner_message failed for lot %s", obj.pk)
                    if not obj.date_end:
                        obj.date_end = timezone.now()
                        obj.active = False
                        obj.save()
                    if obj.auction and obj.auction.club and not obj.bap_points_awarded and not obj.manually_approved:
                        try:
                            obj.auto_award_bap_points()
                        except Exception:
                            logger.exception("auto_award_bap_points failed for lot %s", obj.pk)
            return close_modal_response("reload-page")
        else:
            return self.form_invalid(form)
