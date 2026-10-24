"""Online auctions after bidding: the logistics page, its tap to cross a name off, and the admin's progress banner.

The rules for who comes to which pickup are in :mod:`auctions.logistics`.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView, View

from auctions import logistics
from auctions.filters import AuctionTOSFilter
from auctions.models import AuctionTOS
from auctions.tables import LogisticsHTMxTable, logistics_name_cell

from .base import AuctionViewMixin, HTMxTableView

__all__ = ["AuctionLogistics", "AuctionLogisticsMark", "AuctionPostEndBanner"]


class AuctionLogistics(LoginRequiredMixin, AuctionViewMixin, HTMxTableView):
    """Who comes to each pickup of an online auction, one tab per location and pickup time."""

    model = AuctionTOS
    table_class = LogisticsHTMxTable
    filterset_class = AuctionTOSFilter
    template_name = "auction_logistics.html"
    filter_placeholder_text = "Filter by name, bidder number..."

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.pop("slug", ""))
        if not self.auction.is_online:
            raise Http404
        self.tabs = logistics.tabs(self.auction)
        if not self.tabs:
            raise Http404
        wanted = request.GET.get("tab")
        self.tab = next((tab for tab in self.tabs if tab.key == wanted), self.tabs[0])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return logistics.people(self.auction, self.tab)

    def get_table_kwargs(self):
        kwargs = super().get_table_kwargs()
        kwargs["tab"] = self.tab
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["tabs"] = self.tabs
        context["tab"] = self.tab
        context["active_tab"] = "logistics"
        return context


class AuctionLogisticsMark(LoginRequiredMixin, AuctionViewMixin, View):
    """A tap on a name on the logistics page: crossed off for that pickup, or back on."""

    def post(self, request, pk, which):
        if which not in (logistics.FIRST, logistics.SECOND):
            raise Http404
        tos = get_object_or_404(AuctionTOS, pk=pk, auction=self.auction)
        logistics.mark_off(tos, which, request.user)
        return HttpResponse(logistics_name_cell(tos, which))


class AuctionPostEndBanner(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """The admin's banner on an online auction once bidding ends; it reloads itself until it's done."""

    template_name = "auctions/partials/post_auction_banner.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["post_end"] = logistics.progress(self.auction)
        return context
