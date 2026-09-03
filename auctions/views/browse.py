"""The lot lists people browse, and what they do to a lot without opening it.

Everything a signed-in member sees of somebody else's lots: the main list, the recommendation
feeds, "my bids"/"my watched", the select2 autocompletes those pages are backed by, and the two
writes that happen from a list rather than a page -- watching and bidding.
"""

import logging
import re
from decimal import Decimal
from random import sample, uniform

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

from auctions.bidding import place_bid_and_broadcast
from auctions.filters import (
    AuctionTOSFilter,
    LotAdminFilter,
    LotFilter,
    UserBidLotFilter,
    UserLotFilter,
    UserWatchLotFilter,
    UserWonLotFilter,
    get_recommended_lots,
)
from auctions.models import (
    AdCampaign,
    AdCampaignResponse,
    Auction,
    AuctionIgnore,
    AuctionTOS,
    Category,
    Club,
    ClubMember,
    Invoice,
    Lot,
    LotHistory,
    PageView,
    UserData,
    UserIgnoreCategory,
    UserInterestCategory,
    Watch,
    nearby_auctions,
)
from auctions.tables import (
    LotHTMxTableForUsers,
)

from .base import MILES_TO_KM, HTMxTableView, check_club_permission

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
    """
    loaded async with js on ad.html, this view will spit out some raw html (with no css) suitable for displaying as part of a template
    """

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
            except Category.DoesNotExist:
                pass
        if user and not category:
            # there wasn't a category on this page, pick one of the user's interests instead
            try:
                categories = UserInterestCategory.objects.filter(user=user).order_by("-as_percent")[:5]
                category = sample(categories, 1)
            except (IndexError, ValueError):
                pass
        adCampaigns = (
            AdCampaign.objects.filter(begin_date__lte=timezone.now())
            .filter(Q(end_date__gte=timezone.now()) | Q(end_date__isnull=True))
            .order_by("-bid")
        )
        if auction:
            adCampaigns = adCampaigns.filter(Q(auction__isnull=True) | Q(auction=auction.pk))
        total = adCampaigns.count()
        chanceOfGoogleAd = 50
        if uniform(0, 100) < chanceOfGoogleAd:
            return None
        for campaign in adCampaigns:
            if campaign.category == category:
                campaign.bid = campaign.bid * 2  # Better chance for matching category.  Don't save after this
            if campaign.bid > uniform(0, total - 1):
                if campaign.number_of_clicks > campaign.max_clicks or campaign.number_of_impressions > campaign.max_ads:
                    logger.debug("not selected -- limit exceeded")
                else:
                    return AdCampaignResponse.objects.create(
                        user=user, campaign=campaign
                    )  # fixme, session here: request.session.session_key


class LotListView(AjaxListView):
    """This is a base class that shows lots, with a filter.  This class is never used directly, but it's a parent for several other classes.
    The context is overridden to set the view type"""

    model = Lot
    template_name = "all_lots.html"
    auction = None
    # to display the banner telling users why they are not seeing lots for all auctions
    routeByLastAuction = False

    def get_page_template(self):
        if self.request.user.is_authenticated and self.request.user.userdata.use_list_view:
            return "lot_list_page.html"
        return "lot_tile_page.html"  # tile view as default
        # return 'lot_list_page.html' # list view as default

    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        # if len(data) == 0:
        #    data['status'] = "open" # this would show only open lots by default
        context = super().get_context_data(**kwargs)
        if self.request.GET.get("page"):
            del data["page"]  # required for pagination to work
        # gotta check to make sure we're not trying to filter by an auction, or no auction
        if "auction" in data.keys():
            # now we have tried to search for something, so we should not override the auction
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
            context["lotsAreHidden"] = len(UserIgnoreCategory.objects.filter(user=self.request.user))
        else:
            # probably not signed in
            context["lotsAreHidden"] = -1
        if self.request.user.is_authenticated:
            try:
                context["lastView"] = (
                    PageView.objects.filter(user=self.request.user, lot_number__isnull=False)
                    .order_by("-date_start")[0]
                    .date_start
                )
            except IndexError:
                context["lastView"] = timezone.now()
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
            #     # this message gets added to every scroll event.  Also, it's just noise
            #     messages.error(self.request, f"Please <a href='/auctions/{context['auction'].slug}/'>read the auction's rules and confirm your pickup location</a> to bid")
        else:
            # this will be a mix of auction and non-auction lots
            context["display_auction_on_lots"] = True
        if not self.request.COOKIES.get("longitude"):
            context["location_message"] = "Set your location to see lots near you"
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
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).first()
        if not club or not check_club_permission(self.request.user, club, "permission_manage_bap"):
            return ClubMember.objects.none()
        qs = ClubMember.objects.filter(club=club, is_deleted=False).order_by("name")
        if self.forwarded.get("require_membership_number"):
            qs = qs.filter(membership_number__isnull=False)
        if self.q:
            qs = qs.filter(Q(name__icontains=self.q) | Q(email__icontains=self.q))
        return qs


class ClubMemberMergeAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for the club-member merge target selector.

    Forwards: club_slug, exclude_member (pk of the source being merged away).
    Includes both active and deactivated members; labels deactivated ones.
    Requires permission_add_edit on the club.
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
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).first()
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

        # Exclude the current auction if provided (via DAL forwarded params or plain query params)
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
    """
    Show all recommended lots as a standalone page
    Lots are loaded async on the template via javascript
    """

    template_name = "recommended_lots.html"


class RecommendedLots(ListView):
    """
    Return a somewhat random list of lots that have not been seen by the current user.
    This is rendered html ready to embed in another view
    It shouldn't really be called directly as there's no CSS in the templates
    """

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


class MyWonLots(LotListView):
    """Show all lots won by the current user"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["status"] = "closed"
        context = super().get_context_data(**kwargs)
        context["filter"] = UserWonLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context["lot_view_type"] = "mywonlots"
        context["lotsAreHidden"] = -1
        return context


class MyBids(LotListView):
    """Show all lots the current user has bid on"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        context["filter"] = UserBidLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context["lot_view_type"] = "mybids"
        context["lotsAreHidden"] = -1
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
        # Legacy ?filter=X bookmarks: canonicalize to ?query=X so the shared HTMX
        # template's input pre-populates and its URL-sync stays consistent.
        if "query" not in request.GET and request.GET.get("filter") and not request.htmx:
            params = request.GET.copy()
            params["query"] = params.pop("filter")[0]
            return HttpResponseRedirect(f"{request.path}?{params.urlencode()}")
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


class MyWatched(LotListView):
    """Show all lots watched by the current user"""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter"] = UserWatchLotFilter(
            self.request.GET,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=False,
        )
        context["lot_view_type"] = "watch"
        context["lotsAreHidden"] = -1
        return context


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
    """Place a bid over HTTP - POST only.

    Bidding used to happen entirely over the lot websocket, which meant a dropped
    or stalled socket could silently lose a bid. This endpoint persists the bid via
    a normal request and then broadcasts the result over the websocket as before, so
    the user experience is unchanged but the bid no longer depends on the socket.

    The client does not need to parse this response -- it keeps listening on the
    websocket for the broadcast -- but we return the result for robustness/tests.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if not lot:
            return JsonResponse({"type": "ERROR", "message": "Lot not found"}, status=404)
        # Persist the bid first (best-effort websocket broadcast happens inside).
        result = place_bid_and_broadcast(lot, request.user, request.POST.get("bid"))
        high_bid = result.get("current_high_bid")
        if isinstance(high_bid, Decimal):
            high_bid = float(high_bid)
        # Always 200 for a processed bid (including validation errors like "bid too
        # low"): those are surfaced to the user via the websocket broadcast, so a
        # non-2xx here would make the client show a second, generic error toast.
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
    """POST-only method that returns an empty string if most recent auction you've used accepts lots
    or the name of the auction and the end date
    Used on the lot creation form"""

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
        if result:
            result += "<br>"
        return JsonResponse(
            data={
                "result": result,
            }
        )


class AuctionNotifications(APIView):
    """
    POST-only method that will return a count of auctions as well as some info about the closest one.
    This is mostly a wrapper to go around models.nearby_auctions so that all info isn't accessible to anyone
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        new = 0
        name = ""
        link = ""
        slug = ""
        distance = 0
        latitude = request.COOKIES.get("latitude")
        longitude = request.COOKIES.get("longitude")
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
    """Set user location coordinates - POST only.  I don't think this is used anywhere any more"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.userdata.location_coordinates = f"{request.POST['latitude']},{request.POST['longitude']}"
        request.user.userdata.save()
        return HttpResponse("Success")
