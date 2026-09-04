"""The JSON behind the charts on one auction's stats page.

One view per chart, each returning the series its canvas asks for. They all go through
:class:`AuctionStatsPermissionsMixin`, because an auction's numbers are its admin's business.
"""

import logging

from chartjs.colors import next_color
from chartjs.views.columns import BaseColumnsHighChartsView
from chartjs.views.lines import BaseLineChartView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.db.models import (
    Avg,
    Count,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.base import Model as Model
from django.db.models.functions import Coalesce
from django.http import (
    JsonResponse,
)
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View
from webpush.models import PushInformation

from auctions.helper_functions import bin_data
from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Category,
    Invoice,
    Lot,
    LotHistory,
    PageView,
    SearchHistory,
    UserData,
    Watch,
    median_value,
)

from .base import AuctionStatsPermissionsMixin, AuctionViewMixin

logger = logging.getLogger(__name__)


class AuctionChartView(View, AuctionStatsPermissionsMixin):
    """GET methods for generating auction charts"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)


class AuctionFunnelChartData(AuctionChartView):
    """
    Inverted funnel chart showing user participation
    """

    def get(self, *args, **kwargs):
        # Serve only the values cached by recalculate_stats (the update_auction_stats celery task).
        # Never compute unique_views synchronously here: even the optimized version scans
        # auctions_pageview, and this endpoint is hit on every chart load. The stats page schedules a
        # recalculation when opened and refreshes over WebSocket when it finishes (see AuctionStats),
        # so an auction with no cached stats yet shows 0 briefly rather than blocking the DB.
        misc = self.auction.get_stat_misc()
        total_views = misc.get("total_unique_views", 0)
        user_views = misc.get("logged_in_unique_views", 0)
        total_bidders = User.objects.filter(bid__lot_number__auction=self.auction).annotate(dcount=Count("id")).count()
        # Count every sold, live lot's winner -- including admin-declared winners and winners with
        # no user account, which the old winner__auction join silently dropped.
        total_winners = self.auction.buyer_tos_qs.count()
        labels = [
            "Total unique views",
            "Views from users with accounts",
            "Users who bid on at least one item",
            "Users who won at least one item",
        ]
        data = [
            total_views,
            user_views,
            total_bidders,
            total_winners,
        ]
        return JsonResponse(
            data={
                "labels": labels,
                "data": data,
            }
        )


class AuctionLotBiddersChartData(AuctionChartView):
    """How many bidders were there per lot?"""

    def get(self, *args, **kwargs):
        lots = self.auction.lots_qs
        labels = [
            "Not sold",
            "Lots with bids from 1 user",
            "Lots with bids from 2 users",
            "Lots with bids from 3 users",
            "Lots with bids from 4 users",
            "Lots with bids from 5 users",
            "Lots with bids from 6 or more users",
        ]
        data = [0, 0, 0, 0, 0, 0, 0]
        # Distinct bidders per lot for the whole auction in one GROUP BY. This used to be a query
        # per lot, so a five-hundred-lot auction drew this chart with five hundred queries.
        bidders_per_lot = dict(
            Bid.objects.exclude(is_deleted=True)
            .filter(lot_number__in=lots)
            .order_by()
            .values("lot_number")
            .annotate(bidders=Count("user", distinct=True))
            .values_list("lot_number", "bidders")
        )
        for lot_pk, winning_price in lots.values_list("pk", "winning_price"):
            if not winning_price:
                data[0] += 1
            else:
                # Count distinct bidders (the labels say "users"), not raw Bid rows. Clamp into the
                # final "6 or more" bucket so lots with >6 bidders are counted rather than silently
                # dropped. A sold lot with no recorded bids (buy-now / admin-declared winner) still
                # had a buyer, so floor it at bucket 1 -- never "Not sold" (bucket 0).
                data[min(max(bidders_per_lot.get(lot_pk, 0), 1), 6)] += 1
        return JsonResponse(
            data={
                "labels": labels,
                "data": data,
            }
        )


class AuctionCategoriesChartData(AuctionChartView):
    """Categories by views and lots sold"""

    number_of_categories_to_show = 20

    def process_stat(self, n, d):
        """Divide and catch div/0, round result"""
        if n is not None and d:
            result = round(((n / d) * 100), 2)
        else:
            result = 0
        return result

    def get(self, *args, **kwargs):
        labels = []
        views = []
        bids = []
        lots = []
        volumes = []
        categories = (
            Category.objects.filter(lot__auction=self.auction).annotate(num_lots=Count("lot")).order_by("-num_lots")
        )
        lot_count = self.auction.lots_qs.count()
        allViews = PageView.objects.filter(lot_number__auction=self.auction).count()
        allBids = Bid.objects.exclude(is_deleted=True).filter(lot_number__auction=self.auction).count()
        allVolume = (
            Lot.objects.exclude(is_deleted=True)
            .filter(auction=self.auction)
            .aggregate(Sum("winning_price"))["winning_price__sum"]
        )
        if lot_count:
            shown = list(categories[: self.number_of_categories_to_show])
            # Three GROUP BYs for the whole chart, rather than three queries per category.
            views_by_category = dict(
                PageView.objects.filter(lot_number__auction=self.auction, lot_number__species_category__in=shown)
                .order_by()
                .values("lot_number__species_category")
                .annotate(total=Count("pk"))
                .values_list("lot_number__species_category", "total")
            )
            bids_by_category = dict(
                Bid.objects.exclude(is_deleted=True)
                .filter(lot_number__auction=self.auction, lot_number__species_category__in=shown)
                .order_by()
                .values("lot_number__species_category")
                .annotate(total=Count("pk"))
                .values_list("lot_number__species_category", "total")
            )
            volume_by_category = dict(
                Lot.objects.exclude(is_deleted=True)
                .filter(auction=self.auction, species_category__in=shown)
                .order_by()
                .values("species_category")
                .annotate(total=Sum("winning_price"))
                .values_list("species_category", "total")
            )
            for category in shown:
                labels.append(str(category))
                percentOfLots = self.process_stat(category.num_lots, lot_count)
                percentOfViews = self.process_stat(views_by_category.get(category.pk), allViews)
                percentOfBids = self.process_stat(bids_by_category.get(category.pk), allBids)
                percentOfVolume = self.process_stat(volume_by_category.get(category.pk), allVolume)
                lots.append(percentOfLots)
                views.append(percentOfViews)
                bids.append(percentOfBids)
                volumes.append(percentOfVolume)
        return JsonResponse(
            data={
                "labels": labels,
                "lots": lots,
                "views": views,
                "bids": bids,
                "volumes": volumes,
            }
        )


class AuctionStatsActivityJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    # these will no doubt need to be tweaked, perhaps differnt for in-person and online auctions?
    bins = 21
    days_before = 16
    days_after = bins - days_before
    dates_messed_with = False

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))

        # Load comparison auction if provided
        self.compare_auction = None
        compare_slug = request.GET.get("compare")
        if compare_slug:
            try:
                compare_auction = Auction.objects.get(slug=compare_slug, is_deleted=False)
                # Verify user has access to this auction
                if (
                    compare_auction.created_by == request.user
                    or AuctionTOS.objects.filter(auction=compare_auction, user=request.user, is_admin=True).exists()
                ):
                    self.compare_auction = compare_auction
            except Auction.DoesNotExist:
                pass

        if self.auction.is_online:
            self.date_start = self.auction.date_end - timezone.timedelta(days=self.days_before)
            self.date_end = self.auction.date_end + timezone.timedelta(days=self.days_after)
        else:  # in person
            self.date_start = self.auction.date_start - timezone.timedelta(days=self.days_before)
            self.date_end = self.auction.date_start + timezone.timedelta(days=self.days_after)
        # if date_end is in the future, shift the graph to show the same range, but for the present
        if self.date_end > timezone.now():
            time_difference = self.date_end - self.date_start
            self.date_end = timezone.now()
            self.date_start = self.date_end - time_difference
            self.dates_messed_with = True
        # self.bin_size = (self.date_end - self.date_start).total_seconds() / self.bins
        # self.bin_edges = [self.date_start + timezone.timedelta(seconds=self.bin_size * i) for i in range(self.bins + 1)]
        return super().dispatch(request, *args, **kwargs)

    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "activity" in self.auction.cached_stats:
            return self.auction.cached_stats["activity"]["labels"]

        # Fallback to original calculation
        if self.dates_messed_with:
            return [(f"{i - 1} days ago") for i in range(self.bins, 0, -1)]
        before = [(f"{i} days before") for i in range(self.days_before, 0, -1)]
        after = [(f"{i} days after") for i in range(1, self.days_after)]
        midpoint = "start"
        if self.auction.is_online:
            midpoint = "end"
        return before + [midpoint] + after

    def get_providers(self):
        # Check if we have cached stats
        providers = []
        if self.auction.cached_stats and "activity" in self.auction.cached_stats:
            providers = self.auction.cached_stats["activity"]["providers"]
        else:
            # Fallback to original calculation
            providers = ["Views", "Joins", "New lots", "Searches", "Bids", "Watches"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "activity" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["activity"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        """Return activity data from cache if available, otherwise calculate it"""
        # Get main auction data
        if self.auction.cached_stats and "activity" in self.auction.cached_stats:
            data = self.auction.cached_stats["activity"]["data"]
        else:
            # Fallback to original calculation if cache is not available
            views = PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
            joins = AuctionTOS.objects.filter(auction=self.auction)
            new_lots = Lot.objects.filter(auction=self.auction)
            searches = SearchHistory.objects.filter(auction=self.auction)
            bids = LotHistory.objects.filter(lot__auction=self.auction, changed_price=True)
            watches = Watch.objects.filter(lot_number__auction=self.auction)

            data = [
                bin_data(views, "date_start", self.bins, self.date_start, self.date_end),
                bin_data(joins, "createdon", self.bins, self.date_start, self.date_end),
                bin_data(new_lots, "date_posted", self.bins, self.date_start, self.date_end),
                bin_data(searches, "createdon", self.bins, self.date_start, self.date_end),
                bin_data(bids, "timestamp", self.bins, self.date_start, self.date_end),
                bin_data(watches, "createdon", self.bins, self.date_start, self.date_end),
            ]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "activity" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["activity"]["data"]
            data = data + compare_data

        return data


class AuctionStatsAttritionJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    ignore_percent = 10

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))

        # Load comparison auction if provided
        self.compare_auction = None
        compare_slug = request.GET.get("compare")
        if compare_slug:
            try:
                compare_auction = Auction.objects.get(slug=compare_slug, is_deleted=False)
                # Verify user has access to this auction
                if (
                    compare_auction.created_by == request.user
                    or AuctionTOS.objects.filter(auction=compare_auction, user=request.user, is_admin=True).exists()
                ):
                    self.compare_auction = compare_auction
            except Auction.DoesNotExist:
                pass

        self.lots = (
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True))
            .filter(auction=self.auction, winning_price__isnull=False)
            .order_by("-date_end")
        )
        self.total_lots = self.lots.count()
        start_index = int(self.ignore_percent / 100 * self.total_lots)
        end_index = (
            int((1 - (self.ignore_percent / 100)) * self.total_lots) - 1
        )  # Subtract 1 because indexing is zero-based
        if self.total_lots > 0:
            self.start_date = self.lots[start_index].date_end
            self.end_date = (
                self.lots[end_index].date_end if self.total_lots > 1 else self.start_date
            )  # Handle case with only one lot
            self.total_runtime = self.end_date - self.start_date
            add_back_on = self.total_runtime / self.ignore_percent
            self.start_date = self.start_date - (add_back_on * 2)
            self.end_date = self.end_date + (add_back_on * 2)
            self.lots = self.lots.filter(date_end__lte=self.start_date, date_end__gte=self.end_date)
        result = super().dispatch(request, *args, **kwargs)
        return result

    def get_labels(self):
        """Not used for scatter plots"""
        # Check if we have cached stats
        if self.auction.cached_stats and "attrition" in self.auction.cached_stats:
            return self.auction.cached_stats["attrition"]["labels"]
        return []

    def get_providers(self):
        """Return names of datasets."""
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "attrition" in self.auction.cached_stats:
            providers = self.auction.cached_stats["attrition"]["providers"]
        else:
            providers = ["Lots"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "attrition" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["attrition"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "attrition" in self.auction.cached_stats:
            data = self.auction.cached_stats["attrition"]["data"]
        else:
            # Fallback to original calculation
            data = [
                [
                    {
                        "x": (lot.date_end - self.end_date).total_seconds() // 60,  # minutes after auction start
                        # 'x': lot.date_end.timestamp() * 1000, # this one gives js timestamps and would need moment.js to convert to date
                        "y": lot.winning_price,
                    }
                    for lot in self.lots
                ]
            ]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "attrition" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["attrition"]["data"]
            data = data + compare_data

        return data


class AuctionStatsBarChartJSONView(LoginRequiredMixin, AuctionViewMixin, BaseColumnsHighChartsView):
    """This is needed because of https://github.com/peopledoc/django-chartjs/issues/56"""

    # allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))

        # Load comparison auction if provided
        self.compare_auction = None
        compare_slug = request.GET.get("compare")
        if compare_slug:
            try:
                compare_auction = Auction.objects.get(slug=compare_slug, is_deleted=False)
                # Verify user has access to this auction
                if (
                    compare_auction.created_by == request.user
                    or AuctionTOS.objects.filter(auction=compare_auction, user=request.user, is_admin=True).exists()
                ):
                    self.compare_auction = compare_auction
            except Auction.DoesNotExist:
                pass

        result = super().dispatch(request, *args, **kwargs)
        return result

    def get_yUnit(self):
        return ""

    def get_colors(self):
        return next_color()

    def get_context_data(self, **kwargs):
        """Return graph configuration."""
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "labels": self.get_labels(),
                "chart": self.get_type(),
                "title": self.get_title(),
                "subtitle": self.get_subtitle(),
                "xAxis": self.get_xAxis(),
                "yAxis": self.get_yAxis(),
                "tooltip": self.get_tooltip(),
                "plotOptions": self.get_plotOptions(),
                "datasets": self.get_series(),
                "credits": self.credits,
            }
        )
        return context

    def get_series(self):
        datasets = []
        color_generator = self.get_colors()
        data = self.get_data()
        providers = self.get_providers()
        if len(data) is not len(providers):
            msg = f"self.get_data() return a {len(data)} long array, self.get_providers() returned a {len(providers)} long array.  These need to return the same length array."
            raise ValueError(msg)
        for i, entry in enumerate(data):
            color = tuple(next(color_generator))
            dataset = {
                "data": entry,
                "label": providers[i],
            }
            dataset.update(self.get_dataset_options(i, color))
            datasets.append(dataset)
        return datasets

    def get_dataset_options(self, index, color):
        default_opt = {
            "backgroundColor": f"rgba({color[0]}, {color[1]}, {color[2]}, 0.5)",
            "borderColor": f"rgba({color[0]}, {color[1]}, {color[2]}, 1)",
            "pointBackgroundColor": f"rgba({color[0]}, {color[1]}, {color[2]}, 1)",
        }
        return default_opt

    def get_title(self):
        return ""


class AuctionStatsLotSellPricesJSONView(AuctionStatsBarChartJSONView):
    def _fallback_stats(self):
        """Recompute the sell-price chart when cached_stats is missing.

        Delegates to Auction.set_stat_lot_sell_prices so the fallback labels, providers and
        data come from the same single source of truth -- previously get_labels() and get_data()
        each rederived the bins with different math (num_bins = (max-1)//2 vs max//2, and
        end_bin = start+num*width vs max-1), so the labels and bars disagreed about both the bar
        count and the bin boundaries. Memoized so the three getters compute it once per request.
        """
        if not hasattr(self, "_fallback_stats_cache"):
            self._fallback_stats_cache = self.auction.set_stat_lot_sell_prices()
        return self._fallback_stats_cache

    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "lot_sell_prices" in self.auction.cached_stats:
            return self.auction.cached_stats["lot_sell_prices"]["labels"]
        # Fallback: recompute from the single source of truth
        return self._fallback_stats()["labels"]

    def get_providers(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "lot_sell_prices" in self.auction.cached_stats:
            providers = self.auction.cached_stats["lot_sell_prices"]["providers"]
        else:
            providers = self._fallback_stats()["providers"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lot_sell_prices" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["lot_sell_prices"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "lot_sell_prices" in self.auction.cached_stats:
            data = self.auction.cached_stats["lot_sell_prices"]["data"]
        else:
            # Fallback: recompute from the single source of truth
            data = self._fallback_stats()["data"]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lot_sell_prices" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["lot_sell_prices"]["data"]
            data = data + compare_data

        return data


class AuctionStatsReferrersJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "referrers" in self.auction.cached_stats:
            return self.auction.cached_stats["referrers"]["labels"]

        # Fallback to original calculation
        self.views = (
            PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
            .exclude(referrer__isnull=True)
            .exclude(referrer__startswith=Site.objects.get_current().domain)
            .exclude(referrer__exact="")
            .values("referrer")
            .annotate(count=Count("referrer"))
        )
        result = []
        for view in self.views:
            if view["count"] > 1:
                result.append(view["referrer"])
        result.append("Other")
        return result

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "referrers" in self.auction.cached_stats:
            providers = self.auction.cached_stats["referrers"]["providers"]
        else:
            providers = ["Number of clicks"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "referrers" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["referrers"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "referrers" in self.auction.cached_stats:
            data = self.auction.cached_stats["referrers"]["data"]
        else:
            # Fallback to original calculation
            result = []
            other = 0
            for view in self.views:
                if view["count"] > 1:
                    result.append(view["count"])
                else:
                    other += 1
            result.append(other)
            data = [result]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "referrers" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["referrers"]["data"]
            data = data + compare_data

        return data


# # this view and the following collect specific data for the tutorial videos
# class AdminStatsImages(AuctionStatsBarChartJSONView):
#     def get_labels(self):
#         return ['No images', 'Has image']

#     def get_providers(self):
#         return ['Median sell price', "Average sell price"]

#     def get_data(self):
#         lots = Lot.objects.filter(auction__slug__in=['njas-in-person-spring-auction-april-2024','nec-2024-auction'], winning_price__isnull=False).annotate(num_images=Count('lotimage'))
#         lots_with_no_images = lots.filter(num_images=0)
#         lots_with_one_image = lots.filter(num_images__gt=0)
#         medians = []
#         averages = []
#         counts = []
#         for lots in [lots_with_no_images, lots_with_one_image]:
#             try:
#                 medians.append(median_value(lots, 'winning_price'))
#             except:
#                 medians.append(0)
#             averages.append(lots.aggregate(avg_value=Avg('winning_price'))['avg_value'])
#         return [medians, averages ]

#     def dispatch(self, request, *args, **kwargs):
#         # little hack for permissions
#         return super().dispatch(request, *args, slug="tfcb-2023-annual-auction", **kwargs)

# class AdminStatsDistanceTraveled(AuctionStatsBarChartJSONView):
#     def get_labels(self):
#         return ['Less than 10 miles', '10-20 miles', '21-30 miles', '31-40 miles', '41-50 miles', '51+ miles']

#     def get_providers(self):
#         return ['Number of people']
#         return ['Sellers', 'Buyers']

#     def get_data(self):
#         slugs_list = ['tfcb-annual', 'acm', 'ovas', 'njas', 'nec', 'scas']
#         q_object = Q()
#         for slug in slugs_list:
#             q_object |= Q(auction__slug__icontains=slug)

#         buyers = AuctionTOS.objects.filter(q_object, auctiontos_winner__isnull=False, auction__promote_this_auction=True)
#         #sellers = AuctionTOS.objects.filter(q_object, auctiontos_seller__isnull=False, auction__promote_this_auction=True)
#         #auctiontos = AuctionTOS.objects.filter(auction__promote_this_auction=True, user__isnull=False)
#         buyer_histogram = bin_data(buyers, 'distance_traveled', number_of_bins=5, start_bin=1, end_bin=51, add_column_for_high_overflow=True,)
#         #seller_histogram = bin_data(sellers, 'distance_traveled', number_of_bins=5, start_bin=1, end_bin=51, add_column_for_high_overflow=True,)
#         logger.debug(buyers.count())
#         return [buyer_histogram]

#     def dispatch(self, request, *args, **kwargs):
#         # little hack for permissions
#         return super().dispatch(request, *args, slug="tfcb-2023-annual-auction", **kwargs)
# # the two previous views collect specific data for the tutorial videos


class AuctionStatsImagesJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "images" in self.auction.cached_stats:
            return self.auction.cached_stats["images"]["labels"]

        return ["No images", "One image", "More than one image"]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "images" in self.auction.cached_stats:
            providers = self.auction.cached_stats["images"]["providers"]
        else:
            providers = ["Median sell price", "Average sell price", "Number of lots"]

        # Add comparison auction providers if available
        if self.compare_auction and self.compare_auction.cached_stats and "images" in self.compare_auction.cached_stats:
            compare_providers = [
                f"{p} ({self.compare_auction.title})" for p in self.compare_auction.cached_stats["images"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "images" in self.auction.cached_stats:
            data = self.auction.cached_stats["images"]["data"]
        else:
            # Fallback to original calculation -- exclude banned/soft-deleted lots to match
            # set_stat_images and every other sold-lot money stat (see models.Auction.set_stat_images).
            lots = (
                self.auction.lots_qs.filter(winning_price__isnull=False)
                .exclude(banned=True)
                .annotate(num_images=Count("lotimage"))
            )
            lots_with_no_images = lots.filter(num_images=0)
            lots_with_one_image = lots.filter(num_images=1)
            lots_with_one_or_more_images = lots.filter(num_images__gt=1)
            medians = []
            averages = []
            counts = []
            for lots in [
                lots_with_no_images,
                lots_with_one_image,
                lots_with_one_or_more_images,
            ]:
                try:
                    medians.append(median_value(lots, "winning_price"))
                except:
                    medians.append(0)
                averages.append(lots.aggregate(avg_value=Avg("winning_price"))["avg_value"])
                counts.append(lots.count())
            data = [medians, averages, counts]

        # Add comparison auction data if available
        if self.compare_auction and self.compare_auction.cached_stats and "images" in self.compare_auction.cached_stats:
            compare_data = self.compare_auction.cached_stats["images"]["data"]
            data = data + compare_data

        return data


class AuctionStatsTravelDistanceJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "travel_distance" in self.auction.cached_stats:
            return self.auction.cached_stats["travel_distance"]["labels"]

        return [
            "Less than 10 miles",
            "10-20 miles",
            "21-30 miles",
            "31-40 miles",
            "41-50 miles",
            "51+ miles",
        ]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "travel_distance" in self.auction.cached_stats:
            providers = self.auction.cached_stats["travel_distance"]["providers"]
        else:
            providers = ["Number of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "travel_distance" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["travel_distance"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "travel_distance" in self.auction.cached_stats:
            data = self.auction.cached_stats["travel_distance"]["data"]
        else:
            # Fallback to original calculation
            auctiontos = AuctionTOS.objects.filter(auction=self.auction, user__isnull=False)
            histogram = bin_data(
                auctiontos,
                "distance_traveled",
                number_of_bins=5,
                start_bin=1,
                end_bin=51,
                add_column_for_high_overflow=True,
            )
            data = [histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "travel_distance" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["travel_distance"]["data"]
            data = data + compare_data

        return data


class AuctionStatsPreviousAuctionsJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "previous_auctions" in self.auction.cached_stats:
            return self.auction.cached_stats["previous_auctions"]["labels"]

        return ["First auction", "1 previous auction", "2+ previous auctions"]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "previous_auctions" in self.auction.cached_stats:
            providers = self.auction.cached_stats["previous_auctions"]["providers"]
        else:
            providers = ["Number of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "previous_auctions" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["previous_auctions"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "previous_auctions" in self.auction.cached_stats:
            data = self.auction.cached_stats["previous_auctions"]["data"]
        else:
            # Fallback to original calculation
            # Annotated, not read off each row: AuctionTOS.previous_auctions_count is a COUNT and
            # this histogram walks every person in the auction.
            auctiontos = AuctionTOS.objects.filter(auction=self.auction, email__isnull=False).annotate(
                previous_auctions=Coalesce(
                    Subquery(
                        AuctionTOS.objects.filter(email=OuterRef("email"), createdon__lte=OuterRef("createdon"))
                        .exclude(pk=OuterRef("pk"))
                        .order_by()
                        .values("email")
                        .annotate(total=Count("pk"))
                        .values("total")[:1],
                        output_field=IntegerField(),
                    ),
                    Value(0),
                )
            )
            histogram = bin_data(
                auctiontos,
                "previous_auctions",
                number_of_bins=2,
                start_bin=0,
                end_bin=2,
                add_column_for_high_overflow=True,
            )
            data = [histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "previous_auctions" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["previous_auctions"]["data"]
            data = data + compare_data

        return data


class AuctionStatsLotsSubmittedJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "lots_submitted" in self.auction.cached_stats:
            return self.auction.cached_stats["lots_submitted"]["labels"]

        return [
            "Buyer only (0 lots sold)",
            "1-2 lots",
            "3-4 lots",
            "5-6 lots",
            "7-8 lots",
            "9+ lots",
        ]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "lots_submitted" in self.auction.cached_stats:
            providers = self.auction.cached_stats["lots_submitted"]["providers"]
        else:
            providers = ["Number of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lots_submitted" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["lots_submitted"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "lots_submitted" in self.auction.cached_stats:
            data = self.auction.cached_stats["lots_submitted"]["data"]
        else:
            # Fallback to original calculation
            # Annotated, not read off each row: Invoice.lots_sold counts that person's lots and
            # this histogram walks every invoice in the auction.
            invoices = Invoice.objects.filter(auction=self.auction).annotate(
                sold_lot_count=Coalesce(
                    Subquery(
                        Lot.objects.filter(
                            auctiontos_seller=OuterRef("auctiontos_user"),
                            auction=OuterRef("auction"),
                            is_deleted=False,
                        )
                        .order_by()
                        .values("auctiontos_seller")
                        .annotate(total=Count("pk"))
                        .values("total")[:1],
                        output_field=IntegerField(),
                    ),
                    Value(0),
                )
            )
            histogram = bin_data(
                invoices,
                "sold_lot_count",
                number_of_bins=4,
                start_bin=1,
                end_bin=9,
                add_column_for_low_overflow=True,
                add_column_for_high_overflow=True,
            )
            data = [histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lots_submitted" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["lots_submitted"]["data"]
            data = data + compare_data

        return data


class AuctionStatsLocationVolumeJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "location_volume" in self.auction.cached_stats:
            return self.auction.cached_stats["location_volume"]["labels"]

        # Fallback to original calculation
        locations = []
        for location in self.auction.location_qs:
            locations.append(location.name)
        return locations

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "location_volume" in self.auction.cached_stats:
            providers = self.auction.cached_stats["location_volume"]["providers"]
        else:
            providers = ["Total bought", "Total sold"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "location_volume" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["location_volume"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "location_volume" in self.auction.cached_stats:
            data = self.auction.cached_stats["location_volume"]["data"]
        else:
            # Fallback to original calculation
            sold = []
            bought = []
            for location in self.auction.location_qs:
                sold.append(location.total_sold)
                bought.append(location.total_bought)
            data = [bought, sold]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "location_volume" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["location_volume"]["data"]
            data = data + compare_data

        return data


class AuctionStatsLocationFeatureUseJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "feature_use" in self.auction.cached_stats:
            return self.auction.cached_stats["feature_use"]["labels"]

        return [
            "An account",
            "Mobile app",
            "Search",
            "Watch",
            "Push notifications as lots sell",
            "Proxy bidding",
            "Chat",
            "Buy now",
            "View invoice",
            "Leave feedback for sellers",
        ]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "feature_use" in self.auction.cached_stats:
            providers = self.auction.cached_stats["feature_use"]["providers"]
        else:
            providers = ["Percent of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "feature_use" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["feature_use"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "feature_use" in self.auction.cached_stats:
            data = self.auction.cached_stats["feature_use"]["data"]
        else:
            # Fallback to original calculation
            auctiontos = AuctionTOS.objects.filter(auction=self.auction)
            auctiontos_with_account = auctiontos.filter(user__isnull=False)
            searches = (
                SearchHistory.objects.filter(user__isnull=False, auction=self.auction).values("user").distinct().count()
            )
            seach_percent = (
                int(searches / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
            )
            watch_qs = Watch.objects.filter(lot_number__auction=self.auction).values("user").distinct()
            watches = watch_qs.count()
            watch_percent = int(watches / auctiontos_with_account.count() * 100)
            notifications = (
                PushInformation.objects.filter(
                    user__in=watch_qs, user__userdata__push_notifications_when_lots_sell=True
                )
                .values("user")
                .distinct()
                .count()
            )
            notification_percent = int(notifications / auctiontos_with_account.count() * 100)
            has_used_proxy_bidding = UserData.objects.filter(
                has_used_proxy_bidding=True,
                user__in=auctiontos_with_account.values_list("user"),
            ).count()
            has_used_proxy_bidding_percent = int(has_used_proxy_bidding / auctiontos_with_account.count() * 100)
            chat = (
                LotHistory.objects.filter(
                    changed_price=False,
                    lot__auction=self.auction,
                    user__in=auctiontos_with_account.values_list("user"),
                )
                .values("user")
                .distinct()
                .count()
            )
            chat_percent = int(chat / auctiontos_with_account.count() * 100)
            mobile_app = (
                auctiontos_with_account.filter(user__mobile_devices__isnull=False).values("user").distinct().count()
            )
            if self.auction.is_online:
                lot_with_buy_now = (
                    Lot.objects.filter(auction=self.auction, buy_now_used=True)
                    .values("auctiontos_winner")
                    .distinct()
                    .count()
                )
            else:
                lot_with_buy_now = (
                    Lot.objects.filter(auction=self.auction, winning_price=F("buy_now_price"))
                    .values("auctiontos_winner")
                    .distinct()
                    .count()
                )
            auctiontos_count = auctiontos.count()
            if auctiontos_count == 0:
                lot_with_buy_now_percent = 0
                account_percent = 0
                mobile_app_percent = 0
            else:
                account_percent = int(auctiontos_with_account.count() / auctiontos_count * 100)
                lot_with_buy_now_percent = int(lot_with_buy_now / auctiontos_count * 100)
                mobile_app_percent = int(mobile_app / auctiontos_count * 100)
            invoice_count = Invoice.objects.filter(auction=self.auction).count()
            if invoice_count:
                viewed_invoices = Invoice.objects.filter(auction=self.auction, opened=True).count()
                view_invoice_percent = int(viewed_invoices / invoice_count * 100)
            else:
                view_invoice_percent = 0
            sold_lots = Lot.objects.filter(auction=self.auction, auctiontos_winner__isnull=False)
            leave_feedback = sold_lots.filter(~Q(feedback_rating=0)).values("auctiontos_winner").distinct().count()
            all_sold_lots = sold_lots.values("auctiontos_winner").distinct().count()
            if all_sold_lots == 0:
                leave_feedback_percent = 0
            else:
                leave_feedback_percent = int(leave_feedback / all_sold_lots * 100)
            data = [
                [
                    account_percent,
                    mobile_app_percent,
                    seach_percent,
                    watch_percent,
                    notification_percent,
                    has_used_proxy_bidding_percent,
                    chat_percent,
                    lot_with_buy_now_percent,
                    view_invoice_percent,
                    leave_feedback_percent,
                ]
            ]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "feature_use" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["feature_use"]["data"]
            data = data + compare_data

        return data


class AuctionStatsAuctioneerSpeedJSONView(AuctionStatsAttritionJSONView):
    def get_providers(self):
        """Return names of datasets."""
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "auctioneer_speed" in self.auction.cached_stats:
            providers = self.auction.cached_stats["auctioneer_speed"]["providers"]
        else:
            providers = ["Minutes per lot"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "auctioneer_speed" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["auctioneer_speed"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "auctioneer_speed" in self.auction.cached_stats:
            data = self.auction.cached_stats["auctioneer_speed"]["data"]
        else:
            # Fallback to original calculation
            data_points = []
            for i in range(1, len(self.lots)):
                minutes = (self.lots[i - 1].date_end - self.lots[i].date_end).total_seconds() / 60
                ignore_if_more_than = 3  # minutes
                if minutes <= ignore_if_more_than:
                    data_points.append({"x": i, "y": minutes})
            data = [data_points]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "auctioneer_speed" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["auctioneer_speed"]["data"]
            data = data + compare_data

        return data
