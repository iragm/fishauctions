"""The lot lists people browse, and what they do to a lot without opening it.

The main list, the recommendation feeds, the buying and selling dashboards, the autocompletes behind
them, and the two writes that happen from a list: watching and bidding.
"""

import logging
import re
from decimal import Decimal
from random import choice, uniform

from dal import autocomplete
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import (
    BooleanField,
    Exists,
    OuterRef,
    Q,
    Value,
)
from django.db.models.base import Model as Model
from django.http import (
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.generic import DetailView, ListView, RedirectView, TemplateView
from el_pagination.views import AjaxListView
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from webpush.models import PushInformation

from auctions.bidding import place_bid_and_broadcast
from auctions.filters import (
    AuctionTOSFilter,
    BuyingLotFilter,
    LotAdminFilter,
    LotFilter,
    UserLotFilter,
    get_recommended_lots,
)
from auctions.helper_functions import cookie_coordinates
from auctions.models import (
    AdCampaign,
    AdCampaignResponse,
    Auction,
    AuctionIgnore,
    AuctionTOS,
    Bid,
    Category,
    ClubMember,
    Invoice,
    Lot,
    LotHistory,
    PageView,
    UserData,
    UserIgnoreCategory,
    UserInterestCategory,
    Watch,
)
from auctions.notifications import user_has_app_push
from auctions.queryset_annotations import nearby_auctions
from auctions.tables import (
    LotHTMxTableForBuyers,
    LotHTMxTableForUsers,
)

from .base import MILES_TO_KM, HTMxTableView, check_club_permission, club_from_url

logger = logging.getLogger(__name__)


class ClickAd(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        try:
            campaignResponse = AdCampaignResponse.objects.get(responseid=self.kwargs["uuid"])
            campaignResponse.clicked = True
            campaignResponse.save()
            return campaignResponse.campaign.external_url
        except AdCampaignResponse.DoesNotExist:
            return None


class RenderAd(DetailView):
    """Loaded async by ad.html; returns raw HTML with no CSS, to embed in another template."""

    template_name = "ad_internal.html"
    model = AdCampaignResponse

    def get_object(self, *args, **kwargs):
        data = self.request.GET.copy()
        # request: user, category, auction
        category = None
        auction = None
        if self.request.user.is_authenticated:
            user = self.request.user
        else:
            user = None
        auction_slug = data.get("auction")
        if auction_slug:
            try:
                auction = Auction.objects.get(slug=auction_slug, is_deleted=False)
            except Auction.DoesNotExist:
                pass
        category_pk = data.get("category")
        if category_pk:
            try:
                category = Category.objects.get(pk=category_pk)
            except (Category.DoesNotExist, ValueError, TypeError):
                pass
        if user and not category:
            # No category on this page, so use one of the user's interests.
            #
            # `random.sample` wants a sequence and raised TypeError on a QuerySet -- which the
            # except didn't name -- for every signed-in visitor, and returns a list where the
            # comparison below expects a Category.
            categories = list(UserInterestCategory.objects.filter(user=user).order_by("-as_percent")[:5])
            if categories:
                category = choice(categories).category
        adCampaigns = (
            AdCampaign.objects.filter(begin_date__lte=timezone.now())
            .filter(Q(end_date__gte=timezone.now()) | Q(end_date__isnull=True))
            .order_by("-bid")
        )
        # A campaign tied to an auction shows only on that auction's pages, never site-wide.
        if auction:
            adCampaigns = adCampaigns.filter(Q(auction__isnull=True) | Q(auction=auction.pk))
        else:
            adCampaigns = adCampaigns.filter(auction__isnull=True)
        total = adCampaigns.count()
        chanceOfGoogleAd = 50
        if uniform(0, 100) < chanceOfGoogleAd:
            return None
        for campaign in adCampaigns:
            if campaign.category == category:
                campaign.bid = campaign.bid * 2  # Better chance for matching category.  Don't save after this
            if campaign.bid > uniform(0, total - 1):
                if (
                    campaign.number_of_clicks >= campaign.max_clicks
                    or campaign.number_of_impressions >= campaign.max_ads
                ):
                    logger.debug("not selected -- limit exceeded")
                else:
                    return AdCampaignResponse.objects.create(
                        user=user, campaign=campaign
                    )  # fixme, session here: request.session.session_key


class LotListView(AjaxListView):
    """Base class for lot lists with a filter; never used directly."""

    model = Lot
    template_name = "all_lots.html"
    auction = None
    # Shows the banner explaining why lots from other auctions aren't listed.
    routeByLastAuction = False

    def get_page_template(self):
        if self.request.user.is_authenticated and self.request.user.userdata.use_list_view:
            return "lot_list_page.html"
        return "lot_tile_page.html"  # tile view as default
        # return 'lot_list_page.html' # list view as default

    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        if self.request.GET.get("page"):
            del data["page"]  # required for pagination to work
        # Don't override the auction when one is being filtered for.
        if "auction" in data.keys():
            # A search was made, so don't override the auction.
            self.auction = None
        context["routeByLastAuction"] = self.routeByLastAuction
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingAuction=self.auction,
        )
        context["embed"] = "all_lots"
        if self.request.user.is_authenticated:
            context["lotsAreHidden"] = UserIgnoreCategory.objects.filter(user=self.request.user).count()
        else:
            # probably not signed in
            context["lotsAreHidden"] = -1
        if self.request.user.is_authenticated:
            # values_list, so this reads one column off the biggest table on the site.
            context["lastView"] = (
                PageView.objects.filter(user=self.request.user, lot_number__isnull=False)
                .order_by("-date_start")
                .values_list("date_start", flat=True)
                .first()
            ) or timezone.now()
        else:
            context["lastView"] = timezone.now()
        auction_slug = data.get("auction")
        if auction_slug:
            try:
                context["auction"] = Auction.objects.get(slug=auction_slug, is_deleted=False)
            except Auction.DoesNotExist:
                a_slug = data.get("a")
                if a_slug:
                    try:
                        context["auction"] = Auction.objects.get(slug=a_slug, is_deleted=False)
                    except Auction.DoesNotExist:
                        context["auction"] = self.auction
                        context["no_filters"] = True
                else:
                    context["auction"] = self.auction
                    context["no_filters"] = True
        else:
            context["auction"] = self.auction
            if not auction_slug:
                context["no_filters"] = True
        if context["auction"]:
            if self.request.user.is_authenticated:
                context["auction_tos"] = AuctionTOS.objects.filter(
                    auction=context["auction"].pk, user=self.request.user.pk
                ).first()
        else:
            # this will be a mix of auction and non-auction lots
            context["display_auction_on_lots"] = True
        if not self.request.COOKIES.get("longitude"):
            context["location_message"] = "Set your location to see lots near you"
        # The beacon tags a page view with this auction; only pages that are a visitor looking at
        # an auction do. See base_page_view.html.
        context["page_view_auction"] = context["auction"].pk if context["auction"] else None
        context["src"] = "lot_list"
        return context


class LotAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    def get_result_label(self, result):
        if result.high_bidder:
            return format_html(
                '<b>{}</b>: {}<br><small>High bidder:<span class="text-warning">{} (${})</span></small>',
                result.lot_number_display,
                result.lot_name,
                result.high_bidder_for_admins,
                result.high_bid,
            )
        else:
            return format_html("<b>{}</b>: {}", result.lot_number_display, result.lot_name)

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        auction = self.forwarded.get("auction")
        try:
            auction = Auction.objects.get(pk=auction, is_deleted=False)
        except Auction.DoesNotExist:
            return Lot.objects.none()
        if not auction.permission_check(self.request.user):
            return Lot.objects.none()
        # only this auction
        qs = Lot.objects.exclude(is_deleted=True).filter(auction=auction)
        # winner not alrady set
        qs = qs.filter(auctiontos_winner__isnull=True)
        # not removed
        qs = qs.filter(banned=False)
        if self.q:
            qs = LotAdminFilter.generic(self, qs, self.q)
        return qs


class AuctionTOSAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    def get_result_label(self, result):
        return format_html("<b>{}</b>: {}", result.bidder_number, result.name)

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        auction = self.forwarded.get("auction")
        invoice = self.forwarded.get("invoice")
        exclude_auctiontos = self.forwarded.get("exclude_auctiontos")
        try:
            auction = Auction.objects.get(pk=auction, is_deleted=False)
        except Auction.DoesNotExist:
            return AuctionTOS.objects.none()
        if not auction.permission_check(self.request.user):
            return AuctionTOS.objects.none()
        qs = AuctionTOS.objects.filter(auction=auction)
        if exclude_auctiontos:
            try:
                qs = qs.exclude(pk=int(exclude_auctiontos))
            except (ValueError, TypeError):
                pass
        if invoice:
            qs = qs.exclude(
                Exists(
                    Invoice.objects.filter(
                        Q(status="PAID") | Q(status="READY"),
                        auctiontos_user=OuterRef("pk"),
                    )
                )
            )
        if self.q:
            qs = AuctionTOSFilter.generic(self, qs, self.q)
        return qs.order_by("-name")


class ClubMemberAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for ClubMember — scoped to a forwarded club slug, BAP admins only."""

    def get_result_label(self, result):
        email = f" ({result.email})" if result.email else ""
        return format_html("{}{}", str(result), email)

    def get_queryset(self):
        slug = self.forwarded.get("club_slug", "")
        if not slug:
            return ClubMember.objects.none()
        club = club_from_url(slug)
        if not club or not check_club_permission(self.request.user, club, "permission_manage_bap"):
            return ClubMember.objects.none()
        qs = ClubMember.objects.filter(club=club, is_deleted=False).order_by("name")
        if self.forwarded.get("require_membership_number"):
            qs = qs.filter(membership_number__isnull=False)
        if self.q:
            qs = qs.filter(Q(name__icontains=self.q) | Q(email__icontains=self.q))
        return qs


class ClubMemberMergeAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for the club-member merge target.

    Forwards club_slug and exclude_member. Includes deactivated members, labelled; needs
    permission_add_edit.
    """

    def get_result_label(self, result):
        label = str(result)
        email = f" ({result.email})" if result.email else ""
        suffix = " (Deactivated)" if result.is_deleted else ""
        return format_html("{}{}{}", label, email, suffix)

    def get_queryset(self):
        slug = self.forwarded.get("club_slug", "")
        exclude_pk = self.forwarded.get("exclude_member")
        if not slug:
            return ClubMember.objects.none()
        club = club_from_url(slug)
        if not club or not check_club_permission(self.request.user, club, "permission_add_edit"):
            return ClubMember.objects.none()
        qs = ClubMember.objects.filter(club=club).order_by("is_deleted", "name")
        if exclude_pk:
            try:
                qs = qs.exclude(pk=int(exclude_pk))
            except (ValueError, TypeError):
                pass
        if self.q:
            qs = qs.filter(Q(name__icontains=self.q) | Q(email__icontains=self.q))
        return qs


class CategoryAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for all categories (used in BAP category override form)."""

    def get_queryset(self):
        qs = Category.objects.all().order_by("name")
        if self.q:
            qs = qs.filter(name__icontains=self.q)
        return qs


class AuctionAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for auctions that the current user is an admin of"""

    def get_result_label(self, result):
        return format_html("{}", result.title)

    def get_result_value(self, result):
        """Return slug instead of PK for the value"""
        return result.slug

    def get_queryset(self):
        # Base: auctions where user is creator or admin
        qs = (
            Auction.objects.filter(
                Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True),
                is_deleted=False,
            )
            .distinct()
            .order_by("-date_start")
        )

        # Exclude the current auction, from forwarded or plain query params.
        current_slug = (
            self.forwarded.get("current_slug")
            or self.request.GET.get("current")
            or self.request.GET.get("exclude")
            or self.request.GET.get("slug")
        )
        current_pk = self.forwarded.get("current_pk") or self.request.GET.get("current_pk")

        if current_slug:
            qs = qs.exclude(slug=current_slug)
        if current_pk:
            try:
                qs = qs.exclude(pk=int(current_pk))
            except (TypeError, ValueError):
                pass

        if self.q:
            qs = qs.filter(Q(title__icontains=self.q) | Q(slug__icontains=self.q))

        return qs


class LotQRView(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        lot = Lot.objects.filter(pk=self.kwargs["pk"]).first()
        if lot:
            return f"{lot.lot_link}?src=qr"
        return None


class AllRecommendedLots(TemplateView):
    """Show all recommended lots as a standalone page; the lots load async via JavaScript."""

    template_name = "recommended_lots.html"


class RecommendedLots(ListView):
    """A somewhat random list of lots the user hasn't seen, as HTML to embed in another view."""

    model = Lot

    def get_template_names(self):
        try:
            userData = UserData.objects.get(user=self.request.user.pk)
            if userData.use_list_view:
                return "lot_list_page.html"
            else:
                return "lot_tile_page.html"
        except (UserData.DoesNotExist, AttributeError):
            pass
        return "lot_tile_page.html"  # tile view as default

    def get_queryset(self):
        data = self.request.GET.copy()
        auction = data.get("auction")
        try:
            qty = int(data.get("qty", 10))
        except (ValueError, TypeError):
            qty = 10
        keywords = []
        keywords_string = data.get("keywords", "")
        if keywords_string:
            keywords_string = keywords_string.lower()
            lotWords = re.findall("[A-Z|a-z]{3,}", keywords_string)
            for word in lotWords:
                if word not in settings.IGNORE_WORDS:
                    keywords.append(word)
        try:
            exclude_pk = int(data.get("exclude")) if data.get("exclude") else None
        except (ValueError, TypeError):
            exclude_pk = None
        return get_recommended_lots(
            user=self.request.user, auction=auction, qty=qty, keywords=keywords, exclude_pk=exclude_pk
        )

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        context["embed"] = data.get("embed", "standalone_page")
        if self.request.user.is_authenticated:
            try:
                context["lastView"] = (
                    PageView.objects.filter(user=self.request.user).order_by("-date_start")[0].date_start
                )
            except IndexError:
                context["lastView"] = timezone.now()
        else:
            context["lastView"] = timezone.now()
        context["src"] = "recommended"
        return context


class MyLots(HTMxTableView):
    """Selling dashboard.  List of lots added by this user."""

    model = Lot
    table_class = LotHTMxTableForUsers
    filterset_class = LotAdminFilter
    template_name = "auctions/lot_user.html"
    htmx_table_header_template = "auctions/partials/lot_user_table_header.html"
    # paginate_by = 100

    def dispatch(self, request, *args, **kwargs):
        if legacy := _filter_to_query_redirect(request):
            return legacy
        filter_value = request.GET.get("query", "").strip().lower()
        qs = UserLotFilter(request=request).qs
        if filter_value == "bap":
            qs = qs.select_related("bap_award__club_member__club").annotate(
                show_bap_badge=Value(True, output_field=BooleanField())
            )
        self.queryset = qs
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["userdata"] = self.request.user.userdata
        context["website_focus"] = settings.WEBSITE_FOCUS
        context["filter_bap"] = self.request.GET.get("query", "").strip().lower() == "bap"
        return context

    def get(self, *args, **kwargs):
        if not self.request.htmx:
            if self.request.user.userdata.unnotified_subscriptions_count:
                msg = f"You've got {self.request.user.userdata.unnotified_subscriptions_count} lot"
                if self.request.user.userdata.unnotified_subscriptions_count > 1:
                    msg += "s"
                msg += (
                    f""" with new messages.  <a href="{reverse("messages")}">Go to your messages page to see them</a>"""
                )
                messages.info(self.request, msg, extra_tags="safe")
        return super().get(*args, **kwargs)


def _filter_to_query_redirect(request):
    """``?filter=X`` becomes ``?query=X``, so the search box pre-populates. None when there's nothing to do."""
    if "query" not in request.GET and request.GET.get("filter") and not request.htmx:
        params = request.GET.copy()
        params["query"] = params.pop("filter")[0]
        return HttpResponseRedirect(f"{request.path}?{params.urlencode()}")
    return None


class BuyingRedirect(RedirectView):
    """``/lots/watched/``, ``/lots/won/`` and ``/bids/``, which are the buying dashboard filtered now."""

    keyword = ""

    def get_redirect_url(self, *args, **kwargs):
        params = self.request.GET.copy()
        params["query"] = self.keyword
        return f"{reverse('buying')}?{params.urlencode()}"


def _recent_auctions(user, limit=10):
    """The auctions ``user`` joined most recently, newest first."""
    auctions = []
    for tos in (
        AuctionTOS.objects.filter(user=user, auction__is_deleted=False)
        .select_related("auction")
        .order_by("-createdon")[: limit * 2]
    ):
        if tos.auction not in auctions:
            auctions.append(tos.auction)
    return auctions[:limit]


class BuyingDashboard(HTMxTableView):
    """Buying dashboard: the lots you watched, bid on or won in one auction, ``?auction=<slug>`` or the last
    one you used.
    """

    model = Lot
    table_class = LotHTMxTableForBuyers
    filterset_class = BuyingLotFilter
    template_name = "auctions/lot_buying.html"
    htmx_table_header_template = "auctions/partials/lot_buying_table_header.html"

    def dispatch(self, request, *args, **kwargs):
        if legacy := _filter_to_query_redirect(request):
            return legacy
        self.auction = self.get_auction()
        self.queryset = self.get_lots()
        return super().dispatch(request, *args, **kwargs)

    def get_auction(self):
        slug = self.request.GET.get("auction")
        if slug:
            auction = Auction.objects.filter(slug=slug, is_deleted=False).first()
            if auction:
                return auction
        last = self.request.user.userdata.last_auction_used
        if last and not last.is_deleted:
            return last
        recent = _recent_auctions(self.request.user, limit=1)
        return recent[0] if recent else None

    def get_lots(self):
        if not self.auction:
            return Lot.objects.none()
        user = self.request.user
        return (
            Lot.objects.filter(auction=self.auction, is_deleted=False)
            .annotate(
                watching=Exists(Watch.objects.filter(lot_number=OuterRef("pk"), user=user)),
                bidding=Exists(Bid.objects.filter(lot_number=OuterRef("pk"), user=user, is_deleted=False)),
            )
            .filter(Q(watching=True) | Q(bidding=True) | Q(winner=user) | Q(auctiontos_winner__user=user))
            .select_related("auction", "auctiontos_winner")
            .prefetch_related("bid_set")
            .order_by("lot_number_int", "pk")
        )

    def get_table_kwargs(self):
        return {"user": self.request.user}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        auction = self.auction
        context["auction"] = auction
        if self.request.htmx:
            return context
        context["recent_auctions"] = _recent_auctions(self.request.user)
        context["keywords"] = list(BuyingLotFilter.KEYWORDS)
        query = self.request.GET.get("query", "").lower().split()
        context["active_keywords"] = [keyword for keyword in BuyingLotFilter.KEYWORDS if keyword in query]
        if auction:
            context["bidding_enabled"] = auction.is_online or auction.online_bidding != "disable"
            context["note"] = self.get_note(auction)
        return context

    def get_note(self, auction):
        """Which one line goes above the table, if any."""
        user = self.request.user
        if auction.pretty_much_over:
            return "ended"
        if not auction.is_online and auction.message_users_when_lots_sell:
            has_app_push = user_has_app_push(user)
            if user.userdata.push_notifications_when_lots_sell and (
                has_app_push or PushInformation.objects.filter(user=user).exists()
            ):
                return "push_in_app" if has_app_push else "push_here"
            # The app does its own asking; a WebView has no Push API.
            if not has_app_push and not getattr(self.request, "is_mobile_app", False):
                return "push_offer"
            return None
        if auction.is_online and auction.closed:
            invoice = Invoice.objects.filter(auctiontos_user__user=user, auction=auction).first()
            if invoice and invoice.status != "PAID" and invoice.rounded_net_after_payments < 0:
                return "unpaid"
        return None


class LotsByUser(LotListView):
    """Show all lots for the user specified in the filter"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        username = data.get("user")
        if username:
            try:
                context["user"] = User.objects.get(username=username)
                context["lot_view_type"] = "user"
            except User.DoesNotExist:
                context["user"] = None
        else:
            context["user"] = None
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingUser=context["user"],
        )

        return context


class WatchOrUnwatch(APIView):
    """Watch or unwatch a lot - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        watch = request.POST.get("watch", "")
        user = request.user
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if not lot:
            return HttpResponse("Failure")
        obj = Watch.objects.filter(lot_number=lot, user=user).first()
        if not obj:
            obj = Watch.objects.create(lot_number=lot, user=user)
        if watch == "false":  # string not bool...
            obj.delete()
        if obj:
            return HttpResponse("Success")
        else:
            return HttpResponse("Failure")


class PlaceBid(APIView):
    """Place a bid over HTTP; POST only.

    Bidding used to happen over the lot websocket, where a stalled socket could silently lose a bid.
    This persists the bid and then broadcasts as before; the client still listens on the websocket.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if not lot:
            return JsonResponse({"type": "ERROR", "message": "Lot not found"}, status=404)
        # Persist first; the best-effort broadcast happens inside.
        result = place_bid_and_broadcast(lot, request.user, request.POST.get("bid"))
        high_bid = result.get("current_high_bid")
        if isinstance(high_bid, Decimal):
            high_bid = float(high_bid)
        # Always 200 for a processed bid, including "bid too low": those reach the user over the
        # websocket, and a non-2xx would show a second, generic error.
        return JsonResponse(
            {
                "type": result["type"],
                "message": result["message"],
                "current_high_bid": high_bid,
                "high_bidder_pk": result["high_bidder_pk"],
            }
        )


class LotNotifications(APIView):
    """Get count of new lot notifications - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        new = (
            LotHistory.objects.filter(lot__user=user.pk, seen=False, changed_price=False)
            .exclude(user=request.user)
            .count()
        )
        if not new:
            new = ""
        return JsonResponse(data={"new": new})


class IgnoreAuction(APIView):
    """Ignore an auction - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        auction = request.POST.get("auction", "")
        user = request.user
        if not auction:
            return HttpResponse("Failure: auction parameter required")
        try:
            auction = Auction.objects.get(slug=auction, is_deleted=False)
            obj, created = AuctionIgnore.objects.update_or_create(
                auction=auction,
                user=user,
                defaults={},
            )
            return HttpResponse("Success")
        except Exception as e:
            return HttpResponse(f"Failure: {e}")


class NoLotAuctions(APIView):
    """POST only: the name and end date of the most recent auction you've used, or an empty string if it
    accepts lots. Used on the lot creation form.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        result = ""
        auction = request.user.userdata.last_auction_used
        now = timezone.now()
        if auction:
            if auction.lot_submission_start_date > now:
                result = f"Lot submission is not yet open for {auction}"
            if auction.lot_submission_end_date < now:
                result = f"Lot submission has ended for {auction}"
            if auction.date_end:
                if auction.date_end < now:
                    result = f"{auction} has ended"
            if not result:
                tos = AuctionTOS.objects.filter(user=request.user, auction=auction).first()
                if tos:
                    if not tos.selling_allowed:
                        result = f"You don't have permission to add lots to {auction}"
            if not result:
                if auction.max_lots_per_user:
                    lot_list = Lot.objects.filter(
                        user=request.user,
                        banned=False,
                        deactivated=False,
                        auction=auction,
                        is_deleted=False,
                    )
                    if auction.allow_additional_lots_as_donation:
                        lot_list = lot_list.filter(donation=False)
                    lot_list = lot_list.count()
                    result = f"You've added {lot_list} of {auction.max_lots_per_user} lots to {auction}"
        # The page inserts this as HTML, and the auction's title is whatever its creator typed.
        return JsonResponse(data={"result": format_html("{}<br>", result) if result else ""})


class AuctionNotifications(APIView):
    """POST only: a count of nearby auctions and some detail about the closest, wrapping
    models.nearby_auctions so not everything is exposed.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        new = 0
        name = ""
        link = ""
        slug = ""
        distance = 0
        latitude, longitude = cookie_coordinates(request)
        if not latitude or not longitude:
            if request.user.is_authenticated:
                if request.user.userdata.latitude:
                    latitude = request.user.userdata.latitude
                    longitude = request.user.userdata.longitude
        try:
            distance = 100
            if request.user.is_authenticated:
                distance = request.user.userdata.email_me_about_new_auctions_distance
            if not distance:
                distance = 100
            auctions, distances = nearby_auctions(latitude, longitude, distance, user=request.user)
            new = len(auctions)
            if auctions:
                name = str(auctions[0])
                link = auctions[0].get_absolute_url()
                slug = auctions[0].slug
                distance = distances[0]
        except Exception:
            pass
        if not new:
            new = ""
        # Convert distance to user's preferred unit
        distance_value = distance
        distance_unit = "miles"
        if request.user.is_authenticated:
            try:
                user_unit = request.user.userdata.distance_unit
                if user_unit == "km":
                    distance_value = round(distance * MILES_TO_KM)
                    distance_unit = "km"
                else:
                    distance_value = round(distance)
            except AttributeError:
                distance_value = round(distance)
        else:
            distance_value = round(distance)
        return JsonResponse(
            data={
                "new": new,
                "name": name,
                "link": link,
                "slug": slug,
                "distance": distance_value,
                "distance_unit": distance_unit,
            }
        )


class SetCoordinates(APIView):
    """Set user location coordinates; POST only. Probably unused now."""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            latitude = float(request.POST.get("latitude", ""))
            longitude = float(request.POST.get("longitude", ""))
        except (TypeError, ValueError):
            return HttpResponse("latitude and longitude are required", status=400)
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return HttpResponse("latitude and longitude are out of range", status=400)
        userdata = request.user.userdata
        userdata.location_coordinates = f"{latitude},{longitude}"
        userdata.latitude = latitude
        userdata.longitude = longitude
        userdata.save(update_fields=["location_coordinates", "latitude", "longitude"])
        return HttpResponse("Success")
