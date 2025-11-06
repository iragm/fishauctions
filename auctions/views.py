import ast
import base64
import csv
import json
import logging
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from datetime import timezone as date_tz
from decimal import Decimal
from io import BytesIO, TextIOWrapper
from pathlib import Path
from random import choice, randint, sample, uniform
from urllib.parse import quote_plus, unquote, urlencode, urlparse

import channels.layers
import qr_code
import requests
from asgiref.sync import async_to_sync
from chartjs.colors import next_color
from chartjs.views.columns import BaseColumnsHighChartsView
from chartjs.views.lines import BaseLineChartView
from dal import autocomplete
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib.sites.models import Site
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db.models import (
    Avg,
    Case,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.base import Model as Model
from django.db.models.functions import TruncDay
from django.forms import modelformset_factory
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import DetailView, ListView, RedirectView, TemplateView, View
from django.views.generic.base import ContextMixin
from django.views.generic.edit import (
    CreateView,
    DeleteView,
    FormMixin,
    FormView,
    UpdateView,
)
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin
from django_weasyprint import WeasyTemplateResponseMixin
from easy_thumbnails.files import get_thumbnailer
from el_pagination.views import AjaxListView
from PIL import Image
from pytz import timezone as pytz_timezone
from qr_code.qrcode.utils import QRCodeOptions
from reportlab.platypus import (
    Image as PImage,
)
from user_agents import parse
from webpush import send_user_notification
from webpush.models import PushInformation

from .filters import (
    AuctionFilter,
    AuctionHistoryFilter,
    AuctionTOSFilter,
    LotAdminFilter,
    LotFilter,
    UserBidLotFilter,
    UserLotFilter,
    UserWatchLotFilter,
    UserWonLotFilter,
    get_recommended_lots,
)
from .forms import (
    AuctionEditForm,
    AuctionJoin,
    AuctionNoShowForm,
    BulkSellLotsToOnlineHighBidder,
    ChangeInvoiceStatusForm,
    ChangeUsernameForm,
    ChangeUserPreferencesForm,
    CreateAuctionForm,
    CreateEditAuctionTOS,
    CreateImageForm,
    CreateLotForm,
    DeleteAuctionTOS,
    EditLot,
    InvoiceAdjustmentForm,
    InvoiceAdjustmentFormSetHelper,
    LabelPrintFieldsForm,
    LotFormSetHelper,
    LotRefundForm,
    MultiAuctionTOSPrintLabelForm,
    PickupLocationForm,
    QuickAddLot,
    QuickAddTOS,
    TOSFormSetHelper,
    UserLabelPrefsForm,
    UserLocation,
)
from .models import (
    FAQ,
    AdCampaign,
    AdCampaignResponse,
    Auction,
    AuctionCampaign,
    AuctionHistory,
    AuctionIgnore,
    AuctionTOS,
    Bid,
    BlogPost,
    Category,
    ChatSubscription,
    Club,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    LotHistory,
    LotImage,
    PageView,
    PayPalSeller,
    PickupLocation,
    SearchHistory,
    UserBan,
    UserData,
    UserIgnoreCategory,
    UserInterestCategory,
    UserLabelPrefs,
    Watch,
    add_price_info,
    distance_to,
    find_image,
    guess_category,
    median_value,
    nearby_auctions,
)
from .tables import AuctionHistoryHTMxTable, AuctionHTMxTable, AuctionTOSHTMxTable, LotHTMxTable, LotHTMxTableForUsers

# Distance conversion constant
MILES_TO_KM = 1.60934

logger = logging.getLogger(__name__)


def bin_data(
    queryset,
    field_name,
    number_of_bins,
    start_bin=None,
    end_bin=None,
    add_column_for_low_overflow=False,
    add_column_for_high_overflow=False,
    generate_labels=False,
):
    """Pass a queryset and this will spit out a count of how many `field_name`s there are in each `number_of_bins`
    Pass a datetime or an int for start_bin and end_bin, the default is the min/max value in the queryset.
    Specify `add_column_for_low_overflow` and/or `add_column_for_high_overflow`, otherwise data that falls
    outside the start and end bins will be discarded.

    If `generate_labels=True`, a tuple of [labels, data] will be returned
    """
    # some cleanup and validation first
    try:
        queryset = queryset.order_by(field_name)
    except:
        if start_bin is None or end_bin is None:
            msg = f"queryset cannot be ordered by '{field_name}', so start_bin and end_bin are required"
            raise ValueError(msg)
    working_with_date = False
    if queryset.count():
        value = getattr(queryset[0], field_name)
        if isinstance(value, datetime):
            working_with_date = True
        else:
            try:
                float(value)
            except ValueError:
                msg = f"{field_name} needs to be either a datetime or an integer value, got value {value}"
                raise ValueError(msg)
    if start_bin is None:
        start_bin = value
    if end_bin is None:
        end_bin = getattr(queryset.last(), field_name)
    if working_with_date:
        bin_size = (end_bin - start_bin).total_seconds() / number_of_bins
    else:
        bin_size = (end_bin - start_bin) / number_of_bins
    bin_counts = Counter()
    low_overflow_count = 0
    high_overflow_count = 0
    for item in queryset:
        item_value = getattr(item, field_name)
        if item_value < start_bin:
            low_overflow_count += 1
        elif item_value >= end_bin:
            high_overflow_count += 1
        else:
            if working_with_date:
                diff = (item_value - start_bin).total_seconds()
            else:
                diff = item_value - start_bin
            bin_index = int(diff // bin_size)
            bin_counts[bin_index] += 1

    # Ensure all bins are represented (even those with 0)
    counts_list = [bin_counts[i] for i in range(number_of_bins)]

    # overflow values
    if add_column_for_low_overflow:
        counts_list = [low_overflow_count] + counts_list
    if add_column_for_high_overflow:
        counts_list = counts_list + [high_overflow_count]

    # bin labels
    if generate_labels:
        bin_labels = []
        if add_column_for_low_overflow:
            bin_labels.append("low overflow")
        for i in range(number_of_bins):
            if working_with_date:
                bin_start = start_bin + timedelta(seconds=i * bin_size)
                bin_end = start_bin + timedelta(seconds=(i + 1) * bin_size)
            else:
                bin_start = start_bin + i * bin_size
                bin_end = start_bin + (i + 1) * bin_size

            label = f"{bin_start} - {bin_end if i == number_of_bins - 1 else bin_end - 1}"
            bin_labels.append(label)

        if add_column_for_high_overflow:
            bin_labels.append("high overflow")
        return bin_labels, counts_list
    return counts_list


class AdminEmailMixin:
    """Add an admin_email value from settings to the context of a request"""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["admin_email"] = settings.ADMINS[0][1]
        return context


class AuctionPermissionsMixin:
    """For any auction-related views, adds view.is_auction_admin to be used for any kind of permissions-checking
    Based on whether this user's auctionTOS for this auction has is_admin or not
    Not to be called directly: sub-class this, typically as the last class, set self.auction, then use self.is_auction_admin
    DO NOT FORGET TO CALL self.is_auction_admin somewhere or this will do nothing!
    """

    # this can be set to true for views that are shared between admins and regular users, while providing a different view to each.
    # this is most often used in the context as context['is_auction_admin'] = self.is_auction_admin
    allow_non_admins = False

    @property
    def is_auction_admin(self):
        """Helper function used to check and see if request.user is the creator of the auction or is someone who has been made an admin of the auction.
        Returns False on no permission or True if the user has permission to access the auction"""
        if not self.auction:
            msg = "you must set self.auction (typically in dispatch) for self.is_auction_admin to be available"
            raise Exception(msg)
        result = self.auction.permission_check(self.request.user)
        if not result:
            if self.allow_non_admins:
                logger.debug("non-admins allowed")
                pass
            else:
                raise PermissionDenied()
        else:
            logger.debug("allowing user %s to view %s", self.request.user, self.auction)
            pass
        return result


class AuctionViewMixin(AuctionPermissionsMixin):
    """Subclass this when you need auction permissions, it's easier than using AuctionPermissionsMixin"""

    auction = None

    def get_auction(self, slug):
        if not self.auction and slug:
            self.auction = get_object_or_404(Auction, slug=slug, is_deleted=False)
            self.is_auction_admin

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.pop("slug", ""))
        return super().dispatch(request, *args, **kwargs)


class AdminOnlyViewMixin:
    """Include to make this view only visible to super users on the website
    Despite the name, this has nothing to do with auction admins"""

    permission_denied_message = "Only admins can view this page"
    redirect_url = "/"

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser):
            messages.error(request, self.permission_denied_message)
            return redirect(self.redirect_url)
        return super().dispatch(request, *args, **kwargs)


class AuctionStatsPermissionsMixin:
    """For graph classes"""

    @property
    def is_auction_admin(self):
        """Helper function used to check and see if request.user is the creator of the auction or is someone who has been made an admin of the auction.
        Returns False on no permission or True if the user has permission to access the auction"""
        if not self.auction:
            msg = "you must set self.auction (typically in dispatch) for self.is_auction_admin to be available"
            raise Exception(msg)
        result = self.auction.permission_check(self.request.user)
        if not result:
            if not self.auction.make_stats_public:
                logger.debug("non-admins allowed")
                pass

            else:
                raise PermissionDenied()
        else:
            logger.debug("allowing user %s to view %s", self.request.user, self.auction)
            pass
        return result


class LocationMixin:
    """For location aware views, adds a `get_coordinates()` function which returns a tuple of `latitude, longitude` based on self.request.cookies or userdata

    get_coordinates() should be called before get_context_data
    make sure to set `view.no_location_message`"""

    # override this message in your view, it'll be shown to users without a location
    no_location_message = "Click here to set your location"

    # don't set this, it'll get set automatically by get_coordinates() if the user does not have a cookie
    _location_message = None

    def get_coordinates(self):
        try:
            latitude = float(self.request.COOKIES.get("latitude", 0))
            longitude = float(self.request.COOKIES.get("longitude", 0))
        except (ValueError, TypeError):
            latitude, longitude = 0, 0

        if latitude == 0 and longitude == 0:
            self._location_message = self.no_location_message

            if self.request.user.is_authenticated:
                latitude = self.request.user.userdata.latitude
                longitude = self.request.user.userdata.longitude
        return latitude, longitude

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["location_message"] = self._location_message
        return context


class ClickAd(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        try:
            campaignResponse = AdCampaignResponse.objects.get(responseid=self.kwargs["uuid"])
            campaignResponse.clicked = True
            campaignResponse.save()
            return campaignResponse.campaign.external_url
        except:
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
        try:
            if data["auction"]:
                auction = Auction.objects.get(slug=data["auction"], is_deleted=False)
        except:
            pass
        try:
            if data["category"]:
                category = Category.objects.get(pk=data["category"])
        except:
            pass
        if user and not category:
            # there wasn't a category on this page, pick one of the user's interests instead
            try:
                categories = UserInterestCategory.objects.filter(user=user).order_by("-as_percent")[:5]
                category = sample(categories, 1)
            except:
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
                    pass
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
        try:
            userData = UserData.objects.get(user=self.request.user.pk)
            if userData.use_list_view:
                return "lot_list_page.html"
            else:
                return "lot_tile_page.html"
        except:
            pass
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
        try:
            if "auction" in data.keys():
                # now we have tried to search for something, so we should not override the auction
                self.auction = None
        except Exception:
            pass
        context["routeByLastAuction"] = self.routeByLastAuction
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingAuction=self.auction,
        )
        context["embed"] = "all_lots"
        try:
            context["lotsAreHidden"] = len(UserIgnoreCategory.objects.filter(user=self.request.user))
        except:
            # probably not signed in
            context["lotsAreHidden"] = -1
        try:
            context["lastView"] = (
                PageView.objects.filter(user=self.request.user, lot__isnull=False).order_by("-date_start")[0].date_start
            )
        except:
            context["lastView"] = timezone.now()
        try:
            context["auction"] = Auction.objects.get(slug=data["auction"], is_deleted=False)
        except:
            try:
                context["auction"] = Auction.objects.get(slug=data["a"], is_deleted=False)
            except:
                context["auction"] = self.auction
                context["no_filters"] = True
        if context["auction"]:
            try:
                context["auction_tos"] = AuctionTOS.objects.get(
                    auction=context["auction"].pk, user=self.request.user.pk
                )
            except:
                pass
            #     # this message gets added to every scroll event.  Also, it's just noise
            #     messages.error(self.request, f"Please <a href='/auctions/{context['auction'].slug}/'>read the auction's rules and confirm your pickup location</a> to bid")
        else:
            # this will be a mix of auction and non-auction lots
            context["display_auction_on_lots"] = True
        try:
            self.request.COOKIES["longitude"]
        except:
            context["location_message"] = "Set your location to see lots near you"
        context["src"] = "lot_list"
        return context


class LotAutocomplete(autocomplete.Select2QuerySetView):
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
        # we are not using self.is_auction_admin and the AuctionPermissionsMixinMixin here because self.forwarded.get is not available in dispatch
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        auction = self.forwarded.get("auction")
        try:
            auction = Auction.objects.get(pk=auction, is_deleted=False)
        except:
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


class AuctionTOSAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, result):
        return format_html("<b>{}</b>: {}", result.bidder_number, result.name)

    def dispatch(self, request, *args, **kwargs):
        # we are not using self.is_auction_admin and the AuctionPermissionsMixinMixin here because self.forwarded.get is not available in dispatch
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        auction = self.forwarded.get("auction")
        invoice = self.forwarded.get("invoice")
        try:
            auction = Auction.objects.get(pk=auction, is_deleted=False)
        except:
            return AuctionTOS.objects.none()
        if not auction.permission_check(self.request.user):
            return AuctionTOS.objects.none()
        qs = AuctionTOS.objects.filter(auction=auction)
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
        except:
            pass
        return "lot_tile_page.html"  # tile view as default

    def get_queryset(self):
        data = self.request.GET.copy()
        try:
            auction = data["auction"]
        except:
            auction = None
        try:
            qty = int(data["qty"])
        except:
            qty = 10
        try:
            keywords = []
            keywordsString = data["keywords"].lower()
            lotWords = re.findall("[A-Z|a-z]{3,}", keywordsString)
            for word in lotWords:
                if word not in settings.IGNORE_WORDS:
                    keywords.append(word)
        except:
            keywords = []
        return get_recommended_lots(user=self.request.user, auction=auction, qty=qty, keywords=keywords)

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        try:
            context["embed"] = data["embed"]
        except:
            # if not specified in get data, assume this will be viewed by itself
            context["embed"] = "standalone_page"
        try:
            context["lastView"] = PageView.objects.filter(user=self.request.user).order_by("-date_start")[0].date_start
        except:
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
        context["view"] = "mywonlots"
        context["lotsAreHidden"] = -1
        return context


class MyBids(LotListView):
    """Show all lots the current user has bid on"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        context["filter"] = UserBidLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context["view"] = "mybids"
        context["lotsAreHidden"] = -1
        return context


class MyLots(SingleTableMixin, FilterView):
    """Selling dashboard.  List of lots added by this user."""

    model = Lot
    table_class = LotHTMxTableForUsers
    filterset_class = LotAdminFilter
    # paginate_by = 100

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "auctions/lot_user.html"
        return template_name

    def dispatch(self, request, *args, **kwargs):
        self.queryset = UserLotFilter(request=request).qs
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["userdata"], created = UserData.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        context["website_focus"] = settings.WEBSITE_FOCUS
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
                messages.info(self.request, msg)
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
        context["view"] = "watch"
        context["lotsAreHidden"] = -1
        return context


class LotsByUser(LotListView):
    """Show all lots for the user specified in the filter"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        try:
            context["user"] = User.objects.get(username=data["user"])
            context["view"] = "user"
        except:
            context["user"] = None
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingUser=context["user"],
        )

        return context


@login_required
class WatchOrUnwatch(LoginRequiredMixin, View):
    """Watch or unwatch a lot - POST only"""

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


class LotNotifications(LoginRequiredMixin, View):
    """Get count of new lot notifications - POST only"""

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


class IgnoreAuction(LoginRequiredMixin, View):
    """Ignore an auction - POST only"""

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


class NoLotAuctions(View):
    """POST-only method that returns an empty string if most recent auction you've used accepts lots
    or the name of the auction and the end date
    Used on the lot creation form"""

    def post(self, request):
        result = ""
        if request.user.is_authenticated:
            userData, created = UserData.objects.get_or_create(
                user=request.user,
                defaults={},
            )
            auction = userData.last_auction_used
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


class AuctionNotifications(View):
    """
    POST-only method that will return a count of auctions as well as some info about the closest one.
    Used to put an icon next to auctions button in main view
    This is mostly a wrapper to go around models.nearby_auctions so that all info isn't accessible to anyone
    """

    def post(self, request):
        new = 0
        name = ""
        link = ""
        slug = ""
        distance = 0
        try:
            latitude = request.COOKIES["latitude"]
            longitude = request.COOKIES["longitude"]
        except:
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
            try:
                name = str(auctions[0])
                link = auctions[0].get_absolute_url()
                slug = auctions[0].slug
                distance = distances[0]
            except:
                pass
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


class SetCoordinates(LoginRequiredMixin, View):
    """Set user location coordinates - POST only"""

    def post(self, request):
        userData, created = UserData.objects.get_or_create(
            user=request.user,
            defaults={},
        )
        userData.location_coordinates = f"{request.POST['latitude']},{request.POST['longitude']}"
        userData.last_activity = timezone.now()
        userData.save()
        return HttpResponse("Success")


class UserBan(LoginRequiredMixin, View):
    """Ban a user - POST only"""

    def post(self, request, pk):
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        auctionsList = Auction.objects.exclude(is_deleted=True).filter(created_by=user.pk)
        # delete all bids the banned user has made on active lots or in active auctions created by the request user
        bids = Bid.objects.exclude(is_deleted=True).filter(user=bannedUser)
        for bid in bids:
            lot = Lot.objects.get(pk=bid.lot_number.pk, is_deleted=False)
            if lot.user == user or lot.auction in auctionsList:
                if not lot.ended:
                    logger.info("Deleting bid %s", str(bid))
                    bid.delete()
        # ban all lots added by the banned user.  These are not deleted, just removed from the auction
        for auction in auctionsList:
            buy_now_lots = Lot.objects.exclude(is_deleted=True).filter(winner=bannedUser, auction=auction.pk)
            for lot in buy_now_lots:
                lot.winner = None
                lot.winning_price = None
                lot.save()
            lots = Lot.objects.exclude(is_deleted=True).filter(user=bannedUser, auction=auction.pk)
            for lot in lots:
                if not lot.ended:
                    logger.info("User %s has banned lot %s", str(user), lot)
                    lot.banned = True
                    lot.ban_reason = "The seller of this lot has been banned from this auction"
                    lot.save()
        return redirect(reverse("userpage", kwargs={"slug": bannedUser.username}))


class LotDeactivate(View):
    """Deactivate or activate a lot - POST only"""

    def post(self, request, pk):
        lot = Lot.objects.get(pk=pk, is_deleted=False)
        
        # Check permissions: lot owner or superuser can deactivate
        # Lots in auctions cannot be deactivated
        if lot.auction:
            messages.error(request, "Your account doesn't have permission to view this page")
            return redirect("/")
        
        if lot.user.pk != request.user.pk and not request.user.is_superuser:
            messages.error(request, "Your account doesn't have permission to view this page")
            return redirect("/")
        
        if lot.deactivated:
            lot.deactivated = False
        else:
            bids = Bid.objects.exclude(is_deleted=True).filter(lot_number=lot.lot_number)
            for bid in bids:
                bid.delete()
            lot.deactivated = True
        lot.save()
        return HttpResponse("success")


class UserUnban(LoginRequiredMixin, View):
    """Unban a user - POST only"""

    def post(self, request, pk):
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        obj.delete()
        return redirect(reverse("userpage", kwargs={"slug": bannedUser.username}))


class ImagesPrimary(View):
    """Make the specified image the default image for the lot
    Takes pk of image as post param
    this does not check lot.can_add_images, which is deliberate (who cares if you rotate...)
    """

    def post(self, request):
        try:
            pk = int(request.POST["pk"])
        except:
            return HttpResponse("user and pk are required")
        try:
            lotImage = LotImage.objects.get(pk=pk)
        except:
            return HttpResponse(f"Image {pk} not found")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can change images")
            return redirect("/")
        LotImage.objects.filter(lot_number=lotImage.lot_number.pk).update(is_primary=False)
        lotImage.is_primary = True
        lotImage.save()
        return HttpResponse("Success")


class ImagesRotate(View):
    """Rotate an image associated with a lot
    Takes pk of image and angle as post params
    """

    def post(self, request):
        try:
            pk = int(request.POST["pk"])
            angle = int(request.POST["angle"])
        except (KeyError, ValueError):
            return HttpResponse("user, pk, and angle are required")
        try:
            lotImage = LotImage.objects.get(pk=pk)
        except LotImage.DoesNotExist:
            return HttpResponse(f"Image {pk} not found")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can rotate images")
            return redirect("/")
        if not lotImage.image:
            return HttpResponse("No image")
        temp_image = Image.open(BytesIO(lotImage.image.read()))
        temp_image = temp_image.rotate(angle, expand=True)
        if temp_image.mode in ("RGBA", "P"):
            temp_image = temp_image.convert("RGB")
        output = BytesIO()
        temp_image.save(output, format="JPEG", quality=85)
        output.seek(0)
        # Overwrite the original image
        lotImage.image.save(
            lotImage.image.name.replace("images/", ""),
            ContentFile(output.read()),
            save=True,
        )
        return HttpResponse("Success")


class Feedback(View):
    """Leave feedback on a lot
    This can be done as a buyer or a seller
    api/feedback/lot_number/buyer
    api/feedback/lot_number/seller
    """

    def post(self, request, pk, leave_as):
        data = request.POST
        try:
            lot = Lot.objects.get(pk=pk, is_deleted=False)
        except:
            msg = f"No lot found with key {pk}"
            raise Http404(msg)
        winner_checks_pass = False
        seller_checks_pass = False
        if leave_as == "winner":
            if lot.winner:
                if lot.winner.pk == request.user.pk:
                    winner_checks_pass = True
            if lot.auctiontos_winner:
                if lot.auctiontos_winner.user:
                    if (lot.auctiontos_winner.user.pk == request.user.pk) or (
                        lot.auctiontos_winner.email == request.user.email
                    ):
                        winner_checks_pass = True
        if winner_checks_pass:
            try:
                lot.feedback_rating = data["rating"]
                lot.save()
            except:
                pass
            try:
                lot.feedback_text = data["text"]
                lot.save()
            except:
                pass
        if leave_as == "seller":
            if lot.user:
                if lot.user.pk == request.user.pk:
                    seller_checks_pass = True
            if lot.auctiontos_seller:
                if lot.auctiontos_seller.user:
                    if lot.auctiontos_seller.user.pk == request.user.pk:
                        seller_checks_pass = True
        if seller_checks_pass:
            try:
                lot.winner_feedback_rating = data["rating"]
                lot.save()
            except:
                pass
            try:
                lot.winner_feedback_text = data["text"]
                lot.save()
            except:
                pass
        if not winner_checks_pass and not seller_checks_pass:
            messages.error(request, "Only the seller or winner of a lot can leave feedback")
            return redirect("/")
        return HttpResponse("Success")


def clean_referrer(url):
    """Make a URL more human readable"""
    if not url:
        url = ""
    url = re.sub(r"^https?://", "", url)  # no http/s at the beginning
    if Site.objects.get_current().domain not in url:
        url = re.sub(r"\?.*", "", url)  # remove get params
    url = re.sub(r"^www\.", "", url)  # www
    url = re.sub(r"/+$", "", url)  # trailing /
    # if someone has facebook.example.com, it would be recorded as FB...
    # can update this if it becomes an issue
    if re.search(r"(facebook)\.", url):
        url = "Facebook"
    if re.search(r"(google)\.", url):
        url = "Google"
    return url


def pageview(request):
    """Record page views"""
    if request.method == "POST":
        data = request.POST
        auction = data.get("auction", None)
        if auction:
            auction = Auction.objects.filter(pk=auction).first()
        lot_number = data.get("lot", None)
        if lot_number:
            lot_number = Lot.objects.filter(pk=lot_number, is_deleted=False).first()
        url = data.get("url", None)
        url_without_params = re.sub(r"\?.*", "", url)
        url_without_params = url_without_params[:600]
        first_view = data.get("first_view", False)
        if request.user.is_authenticated:
            user = request.user
            session_id = None
        else:
            # anonymous users go by session
            user = None
            # saving the session will force key generation
            if not request.session.session_key:
                request.session.save()
            session_id = request.session.session_key
        if first_view == "true":  # good ol Javascript
            user_agent = request.META.get("HTTP_USER_AGENT", "")
            # platform = 'UNKNOWN'
            os = "UNKNOWN"
            parsed_ua = parse(user_agent)
            # if parsed_ua.is_mobile:
            #     platform = 'MOBILE'
            # if parsed_ua.is_tablet:
            #     platform = 'TABLET'
            # elif parsed_ua.is_pc:
            #     platform = 'DESKTOP'
            user_agent = user_agent[:200]
            referrer = clean_referrer(data.get("referrer", None)[:600])
            source = data.get("src", None)
            uid = data.get("uid", None)
            # mark auction campaign results if applicable present
            ip = ""
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]
            else:
                ip = request.META.get("REMOTE_ADDR")
            if uid:  # and not request.user.is_authenticated:
                userdata = UserData.objects.filter(unsubscribe_link=uid).first()
                if userdata:
                    userdata.last_activity = timezone.now()
                    userdata.save()
            if source:
                campaign = AuctionCampaign.objects.filter(uuid=source).first()
                if campaign and campaign.result == "NONE":
                    campaign.result = "VIEWED"
                    campaign.save()
                if campaign and campaign.user is not None and campaign.auction is not None:
                    tos = AuctionTOS.objects.filter(user=campaign.user, auction=campaign.auction).first()
                    if tos:
                        campaign.result = "JOINED"
                        campaign.save()
            if "Googlebot" not in user_agent:
                PageView.objects.create(
                    lot_number=lot_number,
                    url=url_without_params,
                    auction=auction,
                    session_id=session_id,
                    user=user,
                    user_agent=user_agent,
                    ip_address=ip[:100],
                    platform=parsed_ua.os.family,
                    os=os,
                    referrer=referrer[:600],
                    title=data.get("title", "")[:600],
                    source=source,
                )
            if user and lot_number and lot_number.species_category:
                # create interest in this category if this is a new view for this category
                interest, created = UserInterestCategory.objects.get_or_create(
                    category=lot_number.species_category,
                    user=user,
                    defaults={"interest": 0},
                )
                interest.interest += settings.VIEW_WEIGHT
                interest.save()
            if auction and user:
                if not source:
                    source = referrer
                try:
                    campaign = AuctionCampaign.objects.create(
                        auction=auction,
                        user=user,
                        email=user.email,
                        source=source[:200],
                    )
                except ValidationError:
                    # campaign already exists
                    pass
        # code below would run on subsequent pageviews.  Not worth the extra server effort for an update every 10 seconds.
        # some corresponding js on base_page_view.html is also commented out
        # else:
        #     pageview = PageView.objects.filter(
        #         url = url_without_params,
        #         session_id = session_id,
        #         user = user,
        #     ).order_by('-date_start').first()
        #     if pageview:
        #         # this is the second (or more) time this user has viewed this page
        #         pageview.total_time += 10
        #         pageview.date_end = timezone.now()
        #         pageview.save()
        return HttpResponse("Success")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect("/")


def invoicePaid(request, pk, **kwargs):
    if request.method == "POST":
        try:
            invoice = Invoice.objects.get(pk=pk)
        except:
            msg = f"No invoice found with key {pk}"
            raise Http404(msg)
        checksPass = False
        if invoice.auction:
            if invoice.auction.permission_check(request.user):
                checksPass = True
        if checksPass:
            invoice.status = kwargs["status"]
            invoice.save()
            if invoice.auction:
                invoice.auction.create_history(
                    applies_to="INVOICES",
                    action=f"Set invoice for {invoice.auctiontos_user.name} to {invoice.get_status_display()}",
                    user=request.user,
                )
            return HttpResponse(
                render_to_string("invoice_buttons.html", {"invoice": invoice}),
                status=200,
            )
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect("/")


class APIPostView(LoginRequiredMixin, View):
    """POST only method to do stuff, logged in users only"""

    def get(self, request, *args, **kwargs):
        return HttpResponseNotAllowed(["POST"])

    def post(self, request, *args, **kwargs):
        raise NotImplementedError()


class UpdateLotPushNotificationsView(APIPostView):
    def post(self, request, *args, **kwargs):
        userdata = request.user.userdata
        userdata.push_notifications_when_lots_sell = True
        userdata.save()
        return JsonResponse({"result": "success"})


class AuctionTOSValidation(AuctionViewMixin, APIPostView):
    """For real time validation on the auctiontos admin create form
    See views.AuctionTOSAdmin for the corresponding js and view
    """

    def post(self, request, *args, **kwargs):
        pk = request.POST.get("pk", None)
        try:
            pk = int(pk) if pk is not None else None
        except ValueError:
            pk = None
        name = request.POST.get("name", None)
        bidder_number = request.POST.get("bidder_number", None)
        email = request.POST.get("email", None)
        # note: be careful what you dump in result
        # javascript will fill out any id on the form with this info
        result = {
            "id_bidder_number": "",
            "id_name": "",
            "id_email": "",
            "id_address": "",
            "id_is_club_member": "",
            "id_phone_number": "",
            "id_memo": "",
            "name_tooltip": "",
            "bidder_number_tooltip": "",
            "email_tooltip": "",
        }
        base_qs = self.auction.tos_qs
        if pk:
            base_qs = base_qs.exclude(pk=pk)
        if name and not email and not pk:
            old_auctions = Auction.objects.filter(
                Q(created_by=self.auction.created_by)
                | Q(created_by=self.request.user)
                | Q(auctiontos__is_admin=True, auctiontos__user=self.request.user)
            )
            qs = AuctionTOS.objects.filter(auction__in=old_auctions, email__isnull=False).order_by("-createdon")
            old_tos = AuctionTOSFilter.generic(self, qs, name, match_names_only=True).first()
            if old_tos:
                bidder_number_used_in_this_auction = base_qs.filter(bidder_number=old_tos.bidder_number).first()
                if not bidder_number_used_in_this_auction:
                    result["id_bidder_number"] = old_tos.bidder_number
                result["id_name"] = old_tos.name
                result["id_email"] = old_tos.email
                result["id_address"] = old_tos.address
                result["id_is_club_member"] = old_tos.is_club_member
                result["id_phone_number"] = old_tos.phone_number
                result["id_memo"] = old_tos.memo
            else:
                logger.info("no user found in older auctions with name %s", name)
        if name:
            existing_tos_in_this_auction = AuctionTOSFilter.generic(self, base_qs, name, match_names_only=True).first()
            if existing_tos_in_this_auction:
                result["name_tooltip"] = f"{existing_tos_in_this_auction.name} is already in this auction"
            else:
                logger.info("no user found in older auctions with name %s", name)
        if email:
            existing_tos_in_this_auction = base_qs.filter(email=email).first()
            if existing_tos_in_this_auction:
                result["email_tooltip"] = "Email is already in this auction"
            else:
                logger.info("no user found in this auction with email %s", email)
        if bidder_number:
            existing_tos_in_this_auction = base_qs.filter(bidder_number=bidder_number).first()
            if existing_tos_in_this_auction:
                result["bidder_number_tooltip"] = "Bidder number in use"
            else:
                logger.info("no user found in this auction with email %s", email)
        return JsonResponse(result)


@login_required
def my_won_lot_csv(request):
    """CSV file showing won lots"""
    lots = add_price_info(
        Lot.objects.filter(Q(winner=request.user) | Q(auctiontos_winner__email=request.user.email)).exclude(
            is_deleted=True
        )
    )
    current_site = Site.objects.get_current()
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="my_won_lots_from_{current_site.domain.replace(".", "_")}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(["Lot number", "Name", "Auction", "Winning price", "Link"])
    for lot in lots:
        writer.writerow(
            [
                lot.lot_number_display,
                lot.lot_name,
                lot.auction,
                f"${lot.winning_price}",
                "https://" + lot.full_lot_link,
            ]
        )
    return response


@login_required
def my_lot_report(request):
    """CSV file showing sold lots"""
    lots = add_price_info(
        Lot.objects.filter(Q(user=request.user) | Q(auctiontos_seller__email=request.user.email)).exclude(
            is_deleted=True
        )
    )
    current_site = Site.objects.get_current()
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="my_lots_from_{current_site.domain.replace(".", "_")}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Lot number", "Name", "Auction", "Status", "Winning price", "My cut"])
    for lot in lots:
        status = "Unsold"
        if lot.banned:
            status = "Removed"
        elif lot.deactivated:
            status = "Deactivated"
        elif lot.winner or lot.auctiontos_winner:
            status = "Sold"
        writer.writerow(
            [
                lot.lot_number_display,
                lot.lot_name,
                lot.auction,
                status,
                lot.winning_price,
                lot.your_cut,
            ]
        )
    return response


@login_required
def auctionReport(request, slug):
    """Get a CSV file showing all users who are participating in this auction"""
    auction = get_object_or_404(Auction, slug=slug, is_deleted=False)
    if auction.permission_check(request.user):
        # Create the HttpResponse object with the appropriate CSV header.
        query = request.GET.get("query", None)
        response = HttpResponse(content_type="text/csv")
        end = timezone.now().strftime("%Y-%m-%d")
        if not query:
            filename = slug + "-report-" + end
        else:
            filename = slug + "-report-" + query + "-" + end
        response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Join date",
                "Bidder number",
                "Username",
                "Name",
                "Email",
                "Phone",
                "Address",
                "Location",
                "Miles to pickup location",
                "Club",
                "Lots viewed",
                "Lots bid",
                "Lots submitted",
                "Lots won",
                "Invoice",
                "Total bought",
                "Gross sold",
                "Total payout",
                "Total club cut",
                "Invoice total due",
                "Breeder points",
                "Number of lots sold outside auction",
                "Total value of lots sold outside auction",
                "Seconds spent reading rules",
                "Other auctions joined",
                "Users who have banned this user",
                "Account created on",
                "Memo",
                auction.alternative_split_label.capitalize(),
                "Bidding allowed",
                "Added auction to their calendar",
            ]
        )
        users = (
            AuctionTOS.objects.filter(auction=auction)
            .select_related("user__userdata")
            .select_related("pickup_location")
            .order_by("createdon")
        )
        # Apply filter if query is provided
        if query:
            users = AuctionTOSFilter.generic(None, users, query)
        # .annotate(distance_traveled=distance_to(\
        # '`auctions_userdata`.`latitude`', '`auctions_userdata`.`longitude`', \
        # lat_field_name='`auctions_pickuplocation`.`latitude`',\
        # lng_field_name="`auctions_pickuplocation`.`longitude`",\
        # approximate_distance_to=1)\
        # )
        for data in users:
            distance = ""
            club = ""
            if data.user and not data.manually_added:
                # these things will only be written out if the user wants you to have it
                lotsViewed = PageView.objects.filter(lot_number__auction=auction, user=data.user)
                lotsBid = Bid.objects.exclude(is_deleted=True).filter(lot_number__auction=auction, user=data.user)
                lot_qs = Lot.objects.exclude(is_deleted=True).filter(
                    user=data.user,
                    auction__isnull=True,
                    date_posted__gte=auction.date_start - timedelta(days=2),
                )
                if auction.is_online:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=auction.date_end + timedelta(days=2))
                else:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=auction.date_start + timedelta(days=5))
                numberLotsOutsideAuction = lotsOutsideAuction.count()
                profitOutsideAuction = lotsOutsideAuction.aggregate(total=Sum("winning_price"))["total"]
                if not profitOutsideAuction:
                    profitOutsideAuction = 0
                distance = data.distance_traveled or ""
                try:
                    club = data.user.userdata.club
                except:
                    pass
                username = data.user.username
                previous_auctions = AuctionTOS.objects.filter(user=data.user).exclude(pk=data.pk).count()
                number_of_userbans = data.number_of_userbans
                account_age = data.user.date_joined
                add_to_calendar = "Yes" if data.add_to_calendar else ""
            else:
                add_to_calendar = ""
                previous_auctions = ""
                lotsViewed = ""
                lotsBid = ""
                numberLotsOutsideAuction = ""
                profitOutsideAuction = ""
                username = ""
                number_of_userbans = 0
                account_age = ""
            lotsSumbitted = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=data, auction=auction)
            lotsWon = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=data, auction=auction)
            breederPoints = Lot.objects.exclude(is_deleted=True).filter(
                auctiontos_seller=data, auction=auction, i_bred_this_fish=True
            )
            address = data.address or ""
            try:
                invoice = Invoice.objects.get(auction=auction, auctiontos_user=data)
                invoiceStatus = invoice.get_status_display()
                totalSpent = invoice.total_bought
                totalPaid = invoice.total_sold
                invoiceTotal = invoice.rounded_net
            except:
                invoiceStatus = ""
                totalSpent = 0
                totalPaid = 0
                invoiceTotal = 0
            writer.writerow(
                [
                    data.createdon.strftime("%m-%d-%Y"),
                    data.bidder_number,
                    username,
                    data.name,
                    data.email,
                    data.phone_as_string,
                    address,
                    data.pickup_location,
                    distance,
                    club,
                    len(lotsViewed),
                    len(lotsBid),
                    len(lotsSumbitted),
                    len(lotsWon),
                    invoiceStatus,
                    f"{totalSpent:.2f}",
                    f"{data.gross_sold:.2f}",
                    f"{totalPaid:.2f}",
                    f"{data.total_club_cut:.2f}",
                    f"{invoiceTotal:.2f}",
                    len(breederPoints),
                    numberLotsOutsideAuction,
                    profitOutsideAuction,
                    data.time_spent_reading_rules,
                    previous_auctions,
                    number_of_userbans,
                    account_age,
                    data.memo,
                    "Yes" if data.is_club_member else "",
                    "No" if not data.bidding_allowed else "",
                    add_to_calendar,
                ]
            )
        auction.create_history(
            applies_to="USERS",
            action="Exported user CSV",
            user=request.user,
        )
        return response
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect("/")


class ComposeEmailToUsers(AuctionViewMixin, TemplateView):
    """Generate a mailto: link with BCC for filtered users - HTMX endpoint"""

    template_name = "email_users_button.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get query parameter
        query = self.request.GET.get("query", "")
        # Get all users for the auction
        users = AuctionTOS.objects.filter(auction=self.auction).select_related("user")

        # Apply filter if query is provided
        if query:
            users = AuctionTOSFilter.generic(None, users, query)

        # Collect valid emails (non-null and non-empty)
        emails = list(users.filter(email__isnull=False).exclude(email="").values_list("email", flat=True))
        # Default values
        mailto_url = "#"
        email_count = 0

        if emails:
            # Limit to avoid overly long URLs (conservative cap)
            max_emails = 60
            if len(emails) > max_emails:
                emails = emails[:max_emails]

            bcc = ",".join(emails)
            subject = f"{self.auction.title}"
            body = f"Hello,\n\nThis message is being sent to participants in {self.auction.title}.\n\n"

            if "open" in query or "ready" in query:
                url = reverse("my_auction_invoice", kwargs={"slug": self.auction.slug})
                body += f"You can view your invoice here: https://{Site.objects.get_current().domain}{url}\n\n"
            mailto_url = f"mailto:?bcc={quote_plus(bcc)}&subject={quote_plus(subject)}&body={quote_plus(body)}"
            email_count = len(emails)

        context.update(
            {
                "mailto_url": mailto_url,
                "email_count": email_count,
            }
        )
        return context


@login_required
def userReport(request):
    """Get a CSV file showing all users from all auctions you're an admin for"""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=all_auction_contacts.csv"
    writer = csv.writer(response)
    found = []
    writer.writerow(["Name", "Email", "Phone"])
    auctions = Auction.objects.filter(
        Q(created_by=request.user) | Q(auctiontos__is_admin=True, auctiontos__user=request.user)
    ).distinct()
    users = AuctionTOS.objects.filter(auction__in=auctions).exclude(email_address_status="BAD")
    for user in users:
        if user.email not in found:
            writer.writerow([user.name, user.email, user.phone_as_string])
            found.append(user.email)
    for auction in auctions:
        auction.create_history(
            applies_to="USERS",
            action="Exported marketing list CSV for all their auctions (including this one)",
            user=request.user,
        )
    return response


@login_required
def auctionInvoicesPaypalCSV(request, slug, chunk):
    """Get a CSV file of all unpaid invoices that owe the club money"""
    auction = Auction.objects.get(slug=slug, is_deleted=False)
    if auction.permission_check(request.user):
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type="text/csv")
        dueDate = timezone.now().strftime("%m/%d/%Y")
        current_site = Site.objects.get_current()
        response["Content-Disposition"] = f'attachment; filename="{slug}-paypal-{chunk}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Recipient Email",
                "Recipient First Name",
                "Recipient Last Name",
                "Invoice Number",
                "Due Date",
                "Reference",
                "Item Name",
                "Description",
                "Item Amount",
                "Shipping Amount",
                "Discount Amount",
                "Currency Code",
                "Note to Customer",
                "Terms and Conditions",
                "Memo to Self",
            ]
        )
        invoices = auction.paypal_invoices
        count = 0
        chunkSize = 150  # attention: this is also set in models.auction.paypal_invoice_chunks
        for invoice in invoices:
            invoice.recalculate
            # we loop through everything regardless of which chunk
            if not invoice.user_should_be_paid:
                count += 1
                if count <= chunkSize * chunk and count > chunkSize * (chunk - 1):
                    email = invoice.auctiontos_user.email
                    firstName = ""
                    lastName = invoice.auctiontos_user.name
                    invoiceNumber = invoice.pk
                    reference = ""
                    itemName = "Auction total"
                    description = ""
                    itemAmount = invoice.absolute_amount
                    shippingAmount = 0
                    discountAmount = 0
                    currencyCode = auction.created_by.userdata.currency
                    noteToCustomer = f"https://{current_site.domain}/invoices/{invoice.pk}/"
                    termsAndConditions = ""
                    memoToSelf = invoice.auctiontos_user.memo
                    if itemAmount > 0 and email:
                        writer.writerow(
                            [
                                email,
                                firstName,
                                lastName,
                                invoiceNumber,
                                dueDate,
                                reference,
                                itemName,
                                description,
                                itemAmount,
                                shippingAmount,
                                discountAmount,
                                currencyCode,
                                noteToCustomer,
                                termsAndConditions,
                                memoToSelf,
                            ]
                        )
        auction.create_history(applies_to="USERS", action="Exported PayPal invoices CSV", user=request.user)
        return response
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect("/")


@login_required
def auctionLotList(request, slug):
    """Get a CSV file showing all sold lots, who bought/sold them, and the winner's location"""
    auction = Auction.objects.get(slug=slug, is_deleted=False)
    if auction.permission_check(request.user):
        # Create the HttpResponse object with the appropriate CSV header.
        query = request.GET.get("query", None)
        response = HttpResponse(content_type="text/csv")
        if not query:
            filename = "all-lot-list"
        else:
            filename = "lot-list-" + query
            query = unquote(query)
        response["Content-Disposition"] = f'attachment; filename="{slug}-{filename}.csv"'
        writer = csv.writer(response)
        first_row_fields = [
            "Lot number",
            "Lot",
            "Seller",
            "Seller email",
            "Seller phone",
            "Seller location",
            "Winner",
            "Winner email",
            "Winner phone",
            "Winner location",
            "Breeder points",
            "Sell price",
            "Club Cut",
            "Seller cut",
        ]
        if auction.use_custom_checkbox_field and auction.custom_checkbox_name:
            first_row_fields.append(auction.custom_checkbox_name)
        if auction.custom_field_1 != "disable" and auction.custom_field_1_name:
            first_row_fields.append(auction.custom_field_1_name)
        writer.writerow(first_row_fields)
        # lots = Lot.objects.exclude(is_deleted=True).filter(auction__slug=slug, auctiontos_winner__isnull=False).select_related('user', 'winner')
        lots = auction.lots_qs
        lots = add_price_info(lots)
        if query:
            lots = LotAdminFilter.generic(None, lots, query)
        for lot in lots:
            row = [
                lot.lot_number_display,
                lot.lot_name,
                lot.auctiontos_seller.name,
                lot.auctiontos_seller.email,
                lot.auctiontos_seller.phone_as_string,
                lot.location,
                lot.auctiontos_winner.name if lot.auctiontos_winner else "",
                lot.auctiontos_winner.email if lot.auctiontos_winner else "",
                lot.auctiontos_winner.phone_as_string if lot.auctiontos_winner else "",
                lot.winner_location,
                lot.i_bred_this_fish_display,
                f"{lot.winning_price:.2f}" if lot.winning_price else "",
                f"{lot.club_cut:.2f}" if lot.winning_price else "",
                f"{lot.your_cut:.2f}" if lot.winning_price else "",
            ]
            if auction.use_custom_checkbox_field and auction.custom_checkbox_name:
                row.append(lot.custom_checkbox_label)
            if auction.custom_field_1 != "disable" and auction.custom_field_1_name:
                row.append(lot.custom_field_1)
            writer.writerow(row)
        auction.create_history(
            applies_to="LOTS",
            action=f"Exported lot list CSV for {query if query else 'all lots'}",
            user=request.user,
        )
        return response
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect("/")


class LeaveFeedbackView(LoginRequiredMixin, ListView):
    """Show all pickup locations belonging to the current user"""

    model = Lot
    template_name = "leave_feedback.html"
    ordering = ["-date_posted"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cutoffDate = timezone.now() - timedelta(days=90)
        context["won_lots"] = (
            Lot.objects.exclude(is_deleted=True)
            .filter(
                Q(winner=self.request.user) | Q(auctiontos_winner__user=self.request.user),
                date_posted__gte=cutoffDate,
            )
            .order_by("-date_posted")
        )
        context["sold_lots"] = (
            Lot.objects.exclude(is_deleted=True)
            .filter(
                Q(user=self.request.user) | Q(auctiontos_seller__user=self.request.user),
                date_posted__gte=cutoffDate,
                winning_price__isnull=False,
            )
            .order_by("-date_posted")
        )
        return context


class FindImageIcon(LoginRequiredMixin, View):
    """Return a handy little icon if the lot name will have an image associated with it"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        name = request.POST["name"]
        result = find_image(name, None, self.auction)
        if result:
            return HttpResponse("image available")
        return HttpResponse("")


class AuctionChats(AuctionViewMixin, LoginRequiredMixin, ListView):
    """Auction admins view to show and delete all chats for an auction"""

    model = LotHistory
    template_name = "chats.html"

    # def dispatch(self, request, *args, **kwargs):
    #     self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
    #     if not self.auction:
    #         raise Http404
    #     self.is_auction_admin
    #     return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # get related auctiontos if the user has joined the auction
        auctiontos_subquery = AuctionTOS.objects.filter(user=OuterRef("user"), auction=self.auction).values("pk")[:1]
        qs = (
            LotHistory.objects.filter(
                lot__auction=self.auction,
                changed_price=False,
            )
            .annotate(auctiontos_pk=Subquery(auctiontos_subquery, output_field=IntegerField(), null=True))
            .order_by("-timestamp")
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionChatDeleteUndelete(AuctionViewMixin, View):
    """HTMX for auction admins only"""

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        self.history = get_object_or_404(LotHistory, pk=pk, lot__auction__is_deleted=False)
        self.auction = self.history.lot.auction
        if not self.auction:
            raise Http404
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Toggle the removed field
        self.history.removed = not self.history.removed
        self.history.save()
        if not self.history.removed:
            result = f'<span id="message_{self.history.pk}" class="badge bg-info">Delete</span>'
        else:
            result = f'<span id="message_{self.history.pk}" class="badge bg-danger">Deleted</span>'
            self.auction.create_history(
                applies_to="USERS",
                action="Deleted chat message",
                user=self.request.user,
            )
        return HttpResponse(result)


class AuctionShowHighBidder(AuctionViewMixin, View):
    """HTMX for auction admins only"""

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        self.lot = get_object_or_404(Lot, pk=pk, is_deleted=False, auction__isnull=False)
        self.auction = self.lot.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if not self.lot.max_bid_revealed_by:
            self.lot.max_bid_revealed_by = request.user
            self.lot.save()
            LotHistory.objects.create(
                lot=self.lot,
                user=self.request.user,
                message=f"{self.request.user} has looked at the max bid on this lot",
                changed_price=True,
            )
        return HttpResponse(f"Max bid: ${self.lot.max_bid}")
        # return HttpResponse(f"Max bid: ${self.lot.max_bid: .2f}")


class PickupLocations(AuctionViewMixin, ListView):
    """Show all pickup locations belonging to the current auction"""

    model = PickupLocation
    template_name = "all_pickup_locations.html"
    ordering = ["name"]

    # def dispatch(self, request, *args, **kwargs):
    #     self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
    #     if not self.auction:
    #         raise Http404
    #     self.is_auction_admin
    #     return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = PickupLocation.objects.filter(
            auction=self.auction,
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class PickupLocationsDelete(AuctionViewMixin, DeleteView):
    model = PickupLocation

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object().auction
        self.success_url = reverse("auction_pickup_location", kwargs={"slug": self.auction.slug})
        if self.get_object().auction.location_qs.count() < 2:
            self.success_url = reverse("auction_main", kwargs={"slug": self.auction.slug})
            messages.error(request, "You can't delete the only pickup location in this auction")
            return redirect(self.success_url)
        if self.get_object().number_of_users:
            messages.error(
                request,
                "There are already users that have selected this location, it can't be deleted",
            )
            return redirect(self.success_url)
        if not self.is_auction_admin:
            messages.error(request, "You don't have permission to delete a pickup location")
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return self.success_url

    def form_valid(self, form):
        self.auction.create_history(
            applies_to="RULES", action=f"Deleted location {self.object}", user=self.request.user
        )
        return super().form_valid(form)


class PickupLocationForm:
    """Base form for create and update"""

    model = PickupLocation
    template_name = "location_form.html"
    form_class = PickupLocationForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["auction"] = self.auction
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        return kwargs

    def get_success_url(self):
        data = self.request.GET.copy()
        try:
            return data["next"]
        except:
            if self.auction.is_online:
                return reverse("auction_pickup_location", kwargs={"slug": self.auction.slug})
            else:
                return self.auction.get_absolute_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def form_valid(self, form):
        location = form.save(commit=False)
        location.user = self.request.user
        location.auction = self.auction
        if not location.name:
            location.name = str(location.auction)
        if not location.pickup_time:
            location.users_must_coordinate_pickup = True
        if form.cleaned_data.get("mail_or_not") == "False":
            location.pickup_by_mail = False
        else:
            location.pickup_by_mail = True
        location.save()
        return super().form_valid(form)


class PickupLocationsUpdate(AuctionViewMixin, PickupLocationForm, UpdateView):
    """Edit pickup locations"""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["is_edit_form"] = True
        kwargs["pickup_location"] = self.get_object()
        return kwargs

    def get(self, *args, **kwargs):
        users = AuctionTOS.objects.filter(pickup_location=self.get_object().pk).count()
        if users:
            messages.info(
                self.request,
                f"{users} users have already selected this as a pickup location.  Don't make large changes!",
            )
        return super().get(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object().auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form, **kwargs):
        if form.has_changed():
            self.auction.create_history(
                applies_to="RULES",
                action=f"Edited location {self.get_object()}",
                user=self.request.user,
            )
        form = super().form_valid(form)
        messages.info(self.request, "Updated location")
        return form


class PickupLocationsCreate(AuctionViewMixin, PickupLocationForm, CreateView):
    """Create a new pickup location"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["is_edit_form"] = False
        kwargs["pickup_location"] = None
        return kwargs

    def form_valid(self, form, **kwargs):
        form = super().form_valid(form)
        self.auction.create_history(
            applies_to="RULES",
            action=f"Added {self.object}",
            user=self.request.user,
        )
        return form


class AuctionUpdate(AuctionViewMixin, UpdateView):
    """The form users fill out to edit an auction"""

    model = Auction
    template_name = "auction_edit_form.html"
    form_class = AuctionEditForm

    def get_success_url(self):
        return "/auctions/" + str(self.kwargs["slug"])

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["user"] = self.request.user
        kwargs["cloned_from"] = None
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        return kwargs

    def get_context_data(self, **kwargs):
        existing_lots = Lot.objects.exclude(is_deleted=True).filter(auction=self.get_object()).count()
        if existing_lots:
            messages.info(
                self.request,
                "Lots have already been added to this auction.  Don't make large changes!",
            )
        context = super().get_context_data(**kwargs)
        context["title"] = f"{self.auction}"
        context["is_online"] = self.auction.is_online
        return context

    def form_valid(self, form, **kwargs):
        if form.has_changed():
            self.get_object().create_history(applies_to="RULES", user=self.request.user, form=form)
        form = super().form_valid(form)
        if (
            not self.get_object().is_online
            and self.get_object().online_bidding == "buy_now_only"
            and self.get_object().buy_now == "disable"
        ):
            messages.info(
                self.request,
                "You've enabled online buy now with no bidding, but buy now isn't enabled.  Sellers won't be able to set a buy now price.",
            )
        elif not self.get_object().is_online and self.get_object().online_bidding != "disable" and settings.ENABLE_HELP:
            messages.info(
                self.request,
                f"This auction allows online bidding -- make sure to <a href='{reverse('auction_help', kwargs={'slug': self.get_object().slug})}'>watch the tutorial in the help</a> to see how this works",
            )
        if (
            self.get_object().buy_now == "allow" or self.get_object().buy_now == "required"
        ) and "buy_now_label" not in self.get_object().label_print_fields:
            messages.info(
                self.request,
                f"Buy now is enabled, but labels are not set to print a buy now price. <a href='{reverse('auction_label_config', kwargs={'slug': self.get_object().slug})}'>You should enable printing buy now on labels here.</a>",
            )
        if (
            self.get_object().reserve_price == "allow" or self.get_object().reserve_price == "required"
        ) and "min_bid_label" not in self.get_object().label_print_fields:
            messages.info(
                self.request,
                f"Minimum bid is enabled, but labels are not set to print a minimum bid. <a href='{reverse('auction_label_config', kwargs={'slug': self.get_object().slug})}'>You should enable printing minimum bids on labels here.</a>",
            )

        # some checks to warn if an important time is set for midnight (00:00)
        user_tz = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        try:
            user_tz = pytz_timezone(user_tz)
        except Exception:  # Catch any invalid timezone errors
            user_tz = pytz_timezone(settings.TIME_ZONE)
        if self.get_object().is_online:
            time_value = self.get_object().date_end
        else:
            time_value = self.get_object().date_start
        localized_time = time_value.astimezone(user_tz)
        if localized_time.hour == 0 and localized_time.minute == 0:
            messages.info(
                self.request,
                f"Don't set your {'end' if self.get_object().is_online else 'start'} time to midnight, users will find it confusing.  Use 23:59 instead.",
            )

        return form


class AuctionHistoryView(SingleTableMixin, AuctionViewMixin, FilterView):
    model = AuctionHistory
    table_class = AuctionHistoryHTMxTable
    filterset_class = AuctionHistoryFilter

    def get_queryset(self):
        return AuctionHistory.objects.filter(auction=self.auction).order_by("-timestamp")

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "auctions/auction_history.html"
        return template_name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["auction"] = self.auction
        return kwargs


class AuctionLots(SingleTableMixin, AuctionViewMixin, FilterView):
    """List of lots associated with an auction.  This is for admins; don't confuse this with the thumbnail-enhanced lot view `AllLots` for users.

    At some point, it may make sense to subclass AllLots here, but I think the needs of the two views are so different that it doesn't make sense
    """

    model = Lot
    table_class = LotHTMxTable
    filterset_class = LotAdminFilter
    # paginate_by = 50

    def get_queryset(self):
        return Lot.objects.exclude(is_deleted=True).filter(auction=self.auction).order_by("lot_number")

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "auctions/auction_lot_admin.html"
        return template_name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "lots"
        context["auction"] = self.auction
        # context['filter'] = LotAdminFilter(auction = self.auction)
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["auction"] = self.auction
        return kwargs


class AuctionHelp(AdminEmailMixin, AuctionViewMixin, TemplateView):
    template_name = "auction_help.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_HELP:
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionUsers(SingleTableMixin, AuctionViewMixin, FilterView):
    """List of users (AuctionTOS) associated with an auction"""

    model = AuctionTOS
    table_class = AuctionTOSHTMxTable
    filterset_class = AuctionTOSFilter
    # paginate_by = 100

    def get_queryset(self):
        return AuctionTOS.objects.filter(auction=self.auction).order_by("name")

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "auction_users.html"
        return template_name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["active_tab"] = "users"
        return context

    def get(self, *args, **kwargs):
        if not self.request.htmx and self.get_queryset().filter(bidder_number="ERROR").count():
            messages.error(
                self.request,
                "Automatic bidder number generation failed, manually set the bidder numbers for these users",
            )
        return super().get(*args, **kwargs)


class AuctionStats(AuctionViewMixin, DetailView):
    """Fun facts about an auction"""

    model = Auction
    template_name = "auction_stats.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.get_object().closed and self.get_object().is_online:
            messages.info(
                self.request,
                "This auction is still in progress, check back once it's finished for more complete stats",
            )
        if self.get_object().date_posted < datetime(year=2024, month=1, day=1, tzinfo=date_tz.utc):
            messages.info(self.request, "Not all stats are available for old auctions.")
        return context


# The following lines of code can most likely be removed unless some club complains about the current system to set winners
# class QuickSetLotWinner(FormView, AuctionPermissionsMixin):
#     """A form to let people record the winners of lots (really just for in-person auctions). Just 3 fields:
#     lot number
#     winner
#     winning price
#     """

#     template_name = "auctions/quick_set_winner.html"
#     form_class = WinnerLot
#     model = Lot

#     def get_success_url(self):
#         return reverse("auction_lot_winners_autocomplete", kwargs={"slug": self.auction.slug})

#     def get_queryset(self):
#         return self.auction.lots_qs

#     def dispatch(self, request, *args, **kwargs):
#         self.auction = Auction.objects.get(slug=kwargs.pop("slug"), is_deleted=False)
#         self.is_auction_admin
#         undo = self.request.GET.get("undo")
#         if undo and request.method == "GET":
#             undo_lot = Lot.objects.filter(custom_lot_number=undo, auction=self.auction).first()
#             if undo_lot:
#                 undo_lot.winner = None
#                 undo_lot.auctiontos_winner = None
#                 undo_lot.winning_price = None
#                 if not self.auction.is_online:
#                     undo_lot.date_end = None
#                 undo_lot.active = False
#                 undo_lot.save()
#                 undo_lot.create_update_invoices
#                 messages.info(
#                     request,
#                     f"{undo_lot.custom_lot_number} {undo_lot.lot_name} now has no winner and can be sold",
#                 )
#         return super().dispatch(request, *args, **kwargs)

#     def get_form_kwargs(self):
#         form_kwargs = super().get_form_kwargs()
#         form_kwargs["auction"] = self.auction
#         return form_kwargs

#     def get_context_data(self, **kwargs):
#         context = super().get_context_data(**kwargs)
#         context["auction"] = self.auction
#         return context

#     def form_valid(self, form, **kwargs):
#         """A bit of cleanup"""
#         lot = form.cleaned_data.get("lot")
#         winner = form.cleaned_data.get("winner")
#         winning_price = form.cleaned_data.get("winning_price")
#         lot = Lot.objects.get(pk=lot, is_deleted=False)
#         tos = AuctionTOS.objects.get(pk=winner)
#         # check auction, find a lot that matches this one, confirm it belongs to this auction
#         if lot.auction and lot.auction == self.auction:
#             if (not tos and not winning_price) or (tos.auction and tos.auction == self.auction):
#                 return self.set_winner(lot, tos, winning_price)
#         return self.form_invalid(form)

#     def set_winner(self, lot, winning_tos, winning_price):
#         """Set the winner (or mark lot unsold),
#         add a success message
#         This does not do permissions or validation checks, do those first
#         Call this once the form is valid"""
#         if not winning_price:
#             lot.date_end = timezone.now()
#             lot.save()
#             messages.success(
#                 self.request,
#                 f"Lot {lot.custom_lot_number} has been ended with no winner",
#             )
#         else:
#             lot.auctiontos_winner = winning_tos
#             lot.winning_price = winning_price
#             lot.date_end = timezone.now()
#             lot.save()
#             lot.create_update_invoices
#             lot.add_winner_message(self.request.user, winning_tos, winning_price)
#             undo_url = self.get_success_url() + f"?undo={lot.custom_lot_number}"
#             messages.success(
#                 self.request,
#                 f"Bidder {winning_tos.bidder_number} is now the winner of lot {lot.custom_lot_number}.  <a href='{undo_url}'>Undo</a>",
#             )
#         return HttpResponseRedirect(self.get_success_url())


# class SetLotWinner(QuickSetLotWinner):
#     """Same as QuickSetLotWinner but without the autocomplete, per user requests"""

#     form_class = WinnerLotSimple

#     def get_success_url(self):
#         return reverse("auction_lot_winners", kwargs={"slug": self.auction.slug})

#     def form_valid(self, form, **kwargs):
#         """A bit of cleanup"""
#         lot = form.cleaned_data.get("lot")
#         winner = form.cleaned_data.get("winner")
#         winning_price = form.cleaned_data.get("winning_price")
#         if winning_price is None or winning_price < 0:
#             form.add_error("winning_price", "How much did the lot sell for?")
#         qs = self.auction.lots_qs
#         lot = qs.filter(custom_lot_number=lot).first()
#         tos = None
#         if not lot:
#             form.add_error("lot", "No lot found")
#         if winning_price is not None and winning_price > 0:
#             tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=winner)
#             if len(tos) > 1:
#                 form.add_error("winner", f"{len(tos)} bidders found with this number!")
#             else:
#                 tos = tos.first()
#             if not tos:
#                 form.add_error("winner", "No bidder found")
#             else:
#                 if tos.invoice and tos.invoice.status != "DRAFT":
#                     form.add_error("winner", "This user's invoice is not open")
#         if lot:
#             if (
#                 lot.auctiontos_seller
#                 and lot.auctiontos_seller.invoice
#                 and lot.auctiontos_seller.invoice.status != "DRAFT"
#             ):
#                 form.add_error("lot", "The seller's invoice is not open")
#             else:
#                 # right now we allow you to mark unsold lots that have already been sold, bad idea but it's what people want
#                 if lot.auctiontos_winner and lot.winning_price and winning_price != 0:
#                     # if lot.auctiontos_winner and lot.winning_price: # this would be better, but would confuse
#                     error = f"Lot {lot.lot_number_display} has already been sold"
#                     try:
#                         if (
#                             tos.invoice.status == "DRAFT"
#                             and lot.auctiontos_seller
#                             and lot.auctiontos_seller.invoice.status == "DRAFT"
#                         ):
#                             undo_url = self.get_success_url() + f"?undo={lot.custom_lot_number}"
#                             form.add_error(
#                                 "lot",
#                                 mark_safe(f"{error}.  <a href='{undo_url}'>Click here to mark unsold</a>."),
#                             )
#                     except:
#                         # one invoice or the other doesn't exist, this only happens when the selling the first lot to a given tos
#                         form.add_error("lot", mark_safe(f"{error}"))
#                 if lot.high_bidder and lot.auction.allow_bidding_on_lots:
#                     if winning_price <= lot.max_bid and winner != lot.high_bidder_for_admins:
#                         form.add_error("winning_price", "Lower than an online bid")
#                         form.add_error("winner", f"Bidder {lot.high_bidder_for_admins} has bid more than this")
#         if form.is_valid():
#             return self.set_winner(lot, tos, winning_price)
#         return self.form_invalid(form)


# class SetLotWinnerImage(SetLotWinner):
#     """Same as QuickSetLotWinner but without the autocomplete, and with images, per user requests"""

#     template_name = "auctions/quick_set_winner_images.html"
#     form_class = WinnerLotSimpleImages

#     def get_success_url(self):
#         return reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})

#     def get_context_data(self, **kwargs):
#         context = super().get_context_data(**kwargs)
#         context["hide_navbar"] = True
#         context["menu_tip"] = mark_safe(
#             f"Press Tab to move the to next field, F11 for full screen, control + or control - to zoom.  <a href='{reverse('auction_main', kwargs={'slug': self.auction.slug})}'>{self.auction} home</a>."
#         )
#         return context


class DynamicSetLotWinner(AuctionViewMixin, TemplateView):
    """A form to set lot winners.  Totally async with no page loads, just POST"""

    template_name = "auctions/dynamic_set_lot_winner.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        # Don't want notifications to show up on the projector
        # context['disable_websocket'] = True
        return context

    def validate_lot(self, lot, action):
        """Returns (Lot or None, error or None)"""
        error = None
        result_lot = None
        if not lot and action != "validate":
            error = "Enter a lot number"
        else:
            # this next line makes it so you cannot search by custom_lot_number in a use_seller_dash_lot_numbering auction
            # if custom lot numbers are ever reenabled, change this
            result_lot_qs = Lot.objects.none()
            if self.auction.use_seller_dash_lot_numbering:
                result_lot_qs = self.auction.lots_qs.filter(custom_lot_number=lot)
            else:
                try:
                    lot = int(lot)
                except ValueError:
                    error = "Lot number must be a number"
                if not error and lot:
                    result_lot_qs = self.auction.lots_qs.filter(lot_number_int=lot)
                if error and not lot and action == "validate":
                    error = ""
            # This can happen if two people are submitting lots at the exact same millisecond.  It seems very unlikely but an easy enough edge case to catch.
            if result_lot_qs.count() > 1:
                error = "Multiple lots with this lot number.  Go to the lot's page and set the winner there."
            else:
                result_lot = result_lot_qs.first()
            if not result_lot and lot and not error:
                error = "No lot found"
        if (
            result_lot
            and result_lot.auctiontos_seller
            and result_lot.auctiontos_seller.invoice
            and result_lot.auctiontos_seller.invoice.status != "DRAFT"
        ):
            if action != "force_save":
                error = "The seller's invoice is not open"
        if result_lot and result_lot.auctiontos_winner and result_lot.winning_price and action != "force_save":
            error = "This lot has already been sold"
        return result_lot, error

    def validate_price(self, price, action):
        """Returns (Int or None, error or None)"""
        result_price = None
        error = None
        try:
            result_price = int(price)
        except (ValueError, TypeError):
            if action == "save":
                error = "Enter the winning price"
            if action == "force_save":
                error = "You can skip some errors, but you still need to enter a price"
        return result_price, error

    def validate_winner(self, winner, action):
        """Returns (AuctionTOS or None, error or None)"""
        error = None
        tos = None
        if not winner and (action == "force_save" or action == "save"):
            error = "Enter the winning bidder's number"
        else:
            tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=winner).order_by("-createdon").first()
            if not tos and winner:
                error = "No bidder found"
            else:
                if tos and tos.invoice and tos.invoice.status != "DRAFT" and action != "force_save":
                    error = "This user's invoice is not open"
        return tos, error

    def end_unsold(self, lot):
        """Mark lot unsold"""
        lot.date_end = timezone.now()
        lot.winner = None
        lot.auctiontos_winner = None
        lot.winning_price = None
        lot.active = False
        lot.save()
        message = f"{self.request.user} has marked lot {lot.lot_number_display} as not sold"
        LotHistory.objects.create(
            lot=lot,
            user=self.request.user,
            message=message,
            changed_price=True,
        )
        lot.send_websocket_message(
            {
                "type": "chat_message",
                "info": "ENDED_NO_WINNER",
                "message": message,
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
            }
        )
        return message

    def set_winner(self, lot, winning_tos, winning_price):
        lot.auctiontos_winner = winning_tos
        lot.winning_price = winning_price
        lot.date_end = timezone.now()
        lot.active = False
        lot.save()
        lot.add_winner_message(self.request.user, winning_tos, winning_price)
        return f"Bidder {winning_tos.bidder_number} is now the winner of lot {lot.lot_number_display}"

    def post(self, request, *args, **kwargs):
        """All lot validation checks called from here"""
        lot = request.POST.get("lot", None)
        price = request.POST.get("price", None)
        winner = request.POST.get("winner", None)
        action = request.POST.get("action", "validate")

        result = {
            "price": None,
            "winner": None,
            "lot": None,
            "last_sold_lot_number": None,
            "success_message": None,
            "online_high_bidder_message": None,
            "auction_minutes_to_end": None,
        }
        lot, lot_error = self.validate_lot(lot, action)
        if lot and not lot_error and action == "to_online_high_bidder":
            result["success_message"] = lot.sell_to_online_high_bidder
            result["last_sold_lot_number"] = lot.lot_number_display
            lot.add_winner_message(self.request.user, lot.auctiontos_winner, lot.winning_price)
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Sold lot {lot.lot_number_display} to online high bidder",
                user=self.request.user,
            )
            return JsonResponse(result)
        price, price_error = self.validate_price(price, action)
        winner, winner_error = self.validate_winner(winner, action)
        if lot and not lot_error and action == "end_unsold":
            result["success_message"] = self.end_unsold(lot)
            result["last_sold_lot_number"] = lot.lot_number_display
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Marked lot {lot.lot_number_display} as ended without being sold",
                user=self.request.user,
            )
            return JsonResponse(result)
        if (
            not price_error
            and lot
            and winner
            and lot.high_bidder
            and lot.auction.online_bidding == "allow"
            and action != "force_save"
        ):
            if price and price <= lot.max_bid and f"{winner}" != f"{lot.high_bidder_for_admins}":
                price_error = "Lower than an online bid"
                winner_error = f"Bidder {lot.high_bidder_for_admins} has bid more than this"
        if not price_error and price and lot and not lot_error and action != "force_save":
            if lot.reserve_price and price < lot.reserve_price:
                price_error = f"This lot's minimum bid is ${lot.reserve_price}"
            if price < self.auction.minimum_bid:
                price_error = f"Minimum bid is ${self.auction.minimum_bid}"
        if not lot_error and not price_error and not winner_error:
            if action != "validate":
                result["last_sold_lot_number"] = lot.lot_number_display
            if action == "force_save" or action == "save":
                result["success_message"] = self.set_winner(lot, winner, price)
                lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"{'Ignored errors and set ' if action == 'force_save' else 'Set'} lot {lot.lot_number_display} as sold",
                    user=self.request.user,
                )
        # if two people are recording bids, we can validate whether or not a lot was sold
        if (
            lot
            and winner
            and price
            and not price_error
            and not winner_error
            and lot_error == "This lot has already been sold"
            and (action == "force_save" or action == "save")
        ):
            if winner == lot.auctiontos_winner and price == lot.winning_price:
                # Lot has been double checked -- mark it as good
                lot.admin_validated = True
                lot.save()
                result["success_message"] = "This lot has been double checked"
                result["last_sold_lot_number"] = lot.lot_number_display
            else:
                # Mismatch between what's been saved in the db and the current request
                result = {
                    "banner": "error",
                    "last_sold_lot_number": lot.lot_number_display,
                    "success_message": f"Lot {lot.lot_number_display} already sold for ${lot.winning_price} to {lot.auctiontos_winner.bidder_number}.  If this is not correct, you can undo this sale",
                }
        if lot and (action == "validate" or not result["success_message"]) and lot.high_bidder:
            result["online_high_bidder_message"] = f"Sell to {lot.high_bidder_for_admins} for ${lot.high_bid}"
            # js code is not in place for this, also remove code from view_lot_simple
        if lot and not lot_error:
            lot = "valid"
        if price and not price_error:
            price = "valid"
        if winner and not winner_error:
            winner = "valid"
        result["lot"] = lot_error or lot
        result["price"] = price_error or price
        result["winner"] = winner_error or winner
        if not lot_error and not price_error and not winner_error:
            result["auction_minutes_to_end"] = self.auction.estimate_end
            result["unsold_lot_count"] = self.auction.total_unsold_lots
        return JsonResponse(result)


class AuctionUnsellLot(AuctionViewMixin, View):
    def post(self, request, *args, **kwargs):
        undo_lot = request.POST.get("lot_number", None)
        if undo_lot:
            if self.auction.use_seller_dash_lot_numbering:
                undo_lot = self.auction.lots_qs.filter(custom_lot_number=undo_lot).first()
            else:
                undo_lot = self.auction.lots_qs.filter(lot_number_int=undo_lot).first()
        if undo_lot:
            result = {
                "hide_undo_button": "true",
                "last_sold_lot_number": "",
                "success_message": f"{undo_lot.lot_number_display} {undo_lot.lot_name} now has no winner and can be sold",
            }
            undo_lot.winner = None
            undo_lot.auctiontos_winner = None
            undo_lot.winning_price = None
            if not self.auction.is_online:
                undo_lot.date_end = None
                # this might need changing for online auctions
                # but as it is now, this view is only ever called for in-person auctions
            undo_lot.active = True
            undo_lot.admin_validated = False
            undo_lot.save()
            undo_lot.auction.create_history(
                applies_to="LOTS",
                action=f"Cleared the winner on lot {undo_lot.lot_number_display} to make it unsold",
                user=self.request.user,
            )
        else:
            result = {"message": "No lot found"}
        return JsonResponse(result)

    def get(self, request, *args, **kwargs):
        return self.http_method_not_allowed


class BulkAddUsers(AuctionViewMixin, TemplateView, ContextMixin):
    """Add/edit lots of auctiontos"""

    template_name = "auctions/bulk_add_users.html"
    max_users_that_can_be_added_at_once = 200
    extra_rows = 5
    AuctionTOSFormSet = None

    def get(self, *args, **kwargs):
        # first, try to read in a CSV file stored in session
        initial_formset_data = self.request.session.get("initial_formset_data", [])
        if initial_formset_data:
            self.extra_rows = len(initial_formset_data) + 1
            del self.request.session["initial_formset_data"]
        else:
            # next, check GET to see if they're asking for an import from a past auction
            import_from_auction = self.request.GET.get("import")
            if import_from_auction:
                other_auction = Auction.objects.exclude(is_deleted=True).filter(slug=import_from_auction).first()
                if not other_auction.permission_check(self.request.user):
                    messages.error(
                        self.request,
                        f"You don't have permission to add users from {other_auction}",
                    )
                else:
                    auctiontos = AuctionTOS.objects.filter(auction=other_auction)
                    total_skipped = 0
                    total_tos = 0
                    for tos in auctiontos:
                        # if not self.tos_is_in_auction(self.auction, tos.name, tos.email):
                        if not self.auction.find_user(tos.name, tos.email):
                            initial_formset_data.append(
                                {
                                    "bidder_number": tos.bidder_number,
                                    "name": tos.name,
                                    "phone": tos.phone_number,
                                    "email": tos.email,
                                    "address": tos.address,
                                    "is_club_member": tos.is_club_member,
                                }
                            )
                            total_tos += 1
                        else:
                            total_skipped += 1
                    if total_tos >= self.max_users_that_can_be_added_at_once:
                        messages.error(
                            self.request,
                            f"You can only add {self.max_users_that_can_be_added_at_once} users from another auction at once; run this again to add additional users.",
                        )
                    if total_skipped:
                        messages.info(
                            self.request,
                            f"{total_skipped} users are already in this auction (matched by email, or name if email not set) and do not appear below",
                        )
                    if total_tos:
                        self.extra_rows = total_tos + 1
        self.instantiate_formset()
        self.tos_formset = self.AuctionTOSFormSet(
            form_kwargs={"auction": self.auction, "bidder_numbers_on_this_form": []},
            queryset=self.queryset,
            initial=initial_formset_data,
        )
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def process_csv_data(self, csv_reader, *args, **kwargs):
        """Process CSV data from a DictReader object and add/update users"""

        def extract_info(row, field_name_list, default_response=""):
            """Pass a row, and a lowercase list of field names
            extract the first match found (case insensitive) and return the value from the row
            empty string returned if the value is not found in the row"""
            case_insensitive_row = {k.lower(): v for k, v in row.items()}
            for name in field_name_list:
                try:
                    return case_insensitive_row[name]
                except:
                    pass
            return default_response

        def columns_exist(field_names, columns):
            """returns True if any value in the list `columns` exists in the file"""
            case_insensitive_row = {k.lower() for k in field_names}
            for column in columns:
                if column in case_insensitive_row:
                    return True
            return False

        email_field_names = ["email", "e-mail", "email address", "e-mail address"]
        bidder_number_fields = ["bidder number", "bidder"]
        name_field_names = ["name", "full name", "first name", "firstname"]
        address_field_names = ["address", "mailing address"]
        phone_field_names = ["phone", "phone number", "telephone", "telephone number"]
        is_club_member_fields = ["member", "club member", self.auction.alternative_split_label.lower()]
        is_bidding_allowed_field_names = ["allow bidding", "bidding", "bidding allowed"]
        memo_field_names = ["memo", "note", "notes"]
        is_admin_field_names = ["admin", "staff", "is_admin", "is_staff"]
        # we are not reading in location here, do we care??
        some_columns_exist = False
        error = ""
        # order matters here - the most important columns should be validated last,
        # so the error refers to the most important missing column
        try:
            # we're not going to error out for club member or bidding allowed missing columns, but track it for updating existing users
            is_club_member_field_exists = columns_exist(csv_reader.fieldnames, is_club_member_fields)
            is_bidding_allowed_fields_exists = columns_exist(csv_reader.fieldnames, is_bidding_allowed_field_names)
            memo_field_exists = columns_exist(csv_reader.fieldnames, memo_field_names)
            is_admin_field_exists = columns_exist(csv_reader.fieldnames, is_admin_field_names)
            if not columns_exist(csv_reader.fieldnames, phone_field_names):
                error = "Warning: This file does not contain a phone column"
            else:
                some_columns_exist = True
            if not columns_exist(csv_reader.fieldnames, address_field_names):
                error = "Warning: This file does not contain an address column"
            else:
                some_columns_exist = True
            if not columns_exist(csv_reader.fieldnames, name_field_names):
                error = "Warning: This file does not contain a name column"
            else:
                some_columns_exist = True
            if not columns_exist(csv_reader.fieldnames, email_field_names):
                error = "Warning: This file does not contain an email column"
            else:
                some_columns_exist = True
            if not some_columns_exist:
                error = (
                    "Unable to read information from this CSV file.  Make sure it contains an email and a name column"
                )

            total_tos = 0
            total_skipped = 0
            total_updated = 0
            for row in csv_reader:
                bidder_number = extract_info(row, bidder_number_fields)
                email = extract_info(row, email_field_names)
                name = extract_info(row, name_field_names)
                phone = extract_info(row, phone_field_names)
                address = extract_info(row, address_field_names)
                memo = extract_info(row, memo_field_names)
                is_club_member = extract_info(row, is_club_member_fields)
                if is_club_member.lower() in [
                    "yes",
                    "true",
                    "member",
                    "club member",
                    self.auction.alternative_split_label.lower(),
                ]:
                    is_club_member = True
                else:
                    is_club_member = False
                is_bidding_allowed = extract_info(row, is_bidding_allowed_field_names, "yes")
                if is_bidding_allowed.lower() in ["yes", "true"]:
                    bidding_allowed = True
                else:
                    bidding_allowed = False
                is_admin = extract_info(row, is_admin_field_names)
                if is_admin and is_admin.lower() in ["yes", "true", "1"]:
                    is_admin = True
                else:
                    is_admin = False
                # if email or name or phone or address:
                if email:
                    # The old way -- skip anybody who is already in the auction
                    # if self.auction.find_user(name, email):
                    #     # if self.tos_is_in_auction(self.auction, name, email):
                    #     logger.debug("CSV import skipping %s", name)
                    #     total_skipped += 1
                    # new way: update existing users with the same email
                    existing_tos = self.auction.find_user(name="", email=email)
                    if existing_tos:
                        logger.debug("CSV import updating %s", email)
                        total_updated += 1
                        if phone:
                            existing_tos.phone_number = phone[:20]
                        if address:
                            existing_tos.address = address[:500]
                        if is_club_member_field_exists:
                            existing_tos.is_club_member = is_club_member
                        if is_bidding_allowed_fields_exists:
                            existing_tos.bidding_allowed = bidding_allowed
                        if name:
                            existing_tos.name = name[:181]
                        if bidder_number:
                            if (
                                not AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number)
                                .exclude(pk=existing_tos.pk)
                                .first()
                            ):
                                existing_tos.bidder_number = bidder_number[:20]
                        if memo_field_exists:
                            existing_tos.memo = memo[:500] if memo else ""
                        if is_admin_field_exists:
                            existing_tos.is_admin = is_admin
                        existing_tos.save()
                    else:
                        logger.debug("CSV import adding %s", name)
                        if bidder_number:
                            if AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number).first():
                                bidder_number = ""
                        AuctionTOS.objects.create(
                            auction=self.auction,
                            pickup_location=self.auction.location_qs.first(),
                            manually_added=True,
                            bidder_number=bidder_number[:20],
                            name=name[:181],
                            phone_number=phone[:20],
                            email=email[:254],
                            address=address[:500],
                            is_club_member=is_club_member,
                            bidding_allowed=bidding_allowed,
                            memo=memo[:500] if memo else "",
                            is_admin=is_admin,
                        )
                        total_tos += 1
                else:
                    total_skipped += 1
            if error:
                messages.error(self.request, error)
            msg = f"{total_tos} users added"
            self.auction.create_history(applies_to="USERS", action=msg, user=self.request.user)
            if total_updated:
                msg += f", {total_updated} users are already in this auction (matched by email) and were updated"
            if total_skipped:
                msg += f", {total_skipped} users were skipped because they did not contain an email address"
            messages.info(self.request, msg)
            url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
            response = HttpResponse(status=200)
            response["HX-Redirect"] = url
            return response
        except (UnicodeDecodeError, ValueError) as e:
            messages.error(
                self.request, f"Unable to read file.  Make sure this is a valid UTF-8 CSV file.  Error was: {e}"
            )
            url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
            response = HttpResponse(status=200)
            response["HX-Redirect"] = url
            return response

    def handle_csv_file(self, csv_file, *args, **kwargs):
        """If a CSV file has been uploaded, parse it and redirect"""
        try:
            csv_file.seek(0)
            csv_reader = csv.DictReader(TextIOWrapper(csv_file.file))
            return self.process_csv_data(csv_reader)
        except (UnicodeDecodeError, ValueError) as e:
            messages.error(
                self.request, f"Unable to read file.  Make sure this is a valid UTF-8 CSV file.  Error was: {e}"
            )
            url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
            response = HttpResponse(status=200)
            response["HX-Redirect"] = url
            return response

        # The code below would populate the formset with the info from the CSV.
        # This process is simply not working, and it fails silently with no log.

        # total_tos = 0
        # total_skipped = 0
        # initial_formset_data = []
        # for row in csv_reader:
        #     bidder_number = extract_info(row, bidder_number_fields)
        #     email = extract_info(row, email_field_names)
        #     name = extract_info(row, name_field_names)
        #     phone = extract_info(row, phone_field_names)
        #     address = extract_info(row, address_field_names)
        #     is_club_member = extract_info(row, is_club_member_fields)

        #     if (email or name or phone or address) and total_tos <= self.max_users_that_can_be_added_at_once:
        #         if self.tos_is_in_auction(self.auction, name, email):
        #             total_skipped += 1
        #         else:
        #             total_tos += 1
        #             initial_formset_data.append(
        #                 {
        #                     "bidder_number": bidder_number,
        #                     "name": name,
        #                     "phone_number": phone,
        #                     "email": email,
        #                     "address": address,
        #                     "is_club_member": is_club_member,
        #                     "pickup_location": "2",
        #                 }
        #             )
        # # this needs to be added to the session in order to persist when moving from POST (this csv processing) to GET
        # self.request.session["initial_formset_data"] = initial_formset_data
        # if total_tos >= self.max_users_that_can_be_added_at_once:
        #     messages.error(
        #         self.request,
        #         f"You can only add {self.max_users_that_can_be_added_at_once} users at once; run this again to add additional users.",
        #     )
        # if total_skipped:
        #     messages.info(
        #         self.request,
        #         f"{total_skipped} users are already in this auction (matched by email, or name if email not set) and do not appear below",
        #     )
        # if error:
        #     messages.error(self.request, error)
        # if total_tos:
        #     self.extra_rows = total_tos
        # # note that regardless of whether this is valid or not, we redirect to the same page after parsing the CSV file
        # return redirect(reverse("bulk_add_users", kwargs={"slug": self.auction.slug}))

    def post(self, request, *args, **kwargs):
        csv_file = request.FILES.get("csv_file", None)
        if csv_file:
            return self.handle_csv_file(csv_file)
        self.instantiate_formset()
        tos_formset = self.AuctionTOSFormSet(
            self.request.POST,
            form_kwargs={"auction": self.auction, "bidder_numbers_on_this_form": []},
            queryset=self.queryset,
        )
        if tos_formset.is_valid():
            auctiontos = tos_formset.save(commit=False)
            for tos in auctiontos:
                tos.auction = self.auction
                tos.manually_added = True
                tos.save()
            messages.success(self.request, f"Added {len(auctiontos)} users")
            self.auction.create_history(
                applies_to="USERS", action=f"Bulk added {len(auctiontos)} users", user=self.request.user
            )
            return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        self.tos_formset = tos_formset
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["formset"] = self.tos_formset
        context["helper"] = TOSFormSetHelper()
        context["auction"] = self.auction
        context["other_auctions"] = (
            Auction.objects.exclude(is_deleted=True)
            .filter(Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True))
            .exclude(pk=self.auction.pk)
            .distinct()
            .order_by("-date_posted")[:10]
        )
        return context

    def tos_is_in_auction(self, auction, name, email):
        """Return the tos if the name or email are already present in the auction, otherwise None"""
        logger.warning("tos_is_in_auction is deprecated, use auction.find_user() instead")
        qs = AuctionTOS.objects.filter(auction=auction)
        if email:
            qs = qs.filter(email=email)
        elif name:
            qs = qs.filter(Q(name=name, email=None) | Q(name=name, email=""))
        else:
            return None
        return qs.first()

    def dispatch(self, request, *args, **kwargs):
        self.queryset = AuctionTOS.objects.none()  # we don't want to allow editing
        return super().dispatch(request, *args, **kwargs)

    def instantiate_formset(self, *args, **kwargs):
        if not self.AuctionTOSFormSet:
            self.AuctionTOSFormSet = modelformset_factory(
                AuctionTOS,
                extra=self.extra_rows,
                fields=(
                    "bidder_number",
                    "name",
                    "email",
                    "phone_number",
                    "address",
                    "pickup_location",
                    "is_club_member",
                ),
                form=QuickAddTOS,
            )


class ImportFromGoogleDrive(AuctionViewMixin, TemplateView, ContextMixin):
    """Import users from a Google Drive spreadsheet"""

    template_name = "auctions/import_from_google_drive.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def post(self, request, *args, **kwargs):
        # Check if this is a sync request (no google_drive_link in POST)
        google_drive_link = request.POST.get("google_drive_link", "").strip()

        # If google_drive_link is provided, update it and sync
        if google_drive_link:
            self.auction.google_drive_link = google_drive_link
            self.auction.save()

        # Perform the sync (whether it's a new link or existing link)
        return self.sync_google_drive()

    def sync_google_drive(self):
        """Read data from Google Drive and import users"""
        if not self.auction.google_drive_link:
            messages.error(self.request, "No Google Drive link configured")
            url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
            return redirect(url)

        try:
            # Convert Google Sheets sharing link to export CSV URL
            # Example: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit#gid=0
            # Convert to: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/export?format=csv&gid=0
            link = self.auction.google_drive_link

            # Extract the spreadsheet ID from the URL
            if "/spreadsheets/d/" in link:
                spreadsheet_id = link.split("/spreadsheets/d/")[1].split("/")[0]
                # Extract gid if present
                gid = "0"
                if "gid=" in link:
                    gid = link.split("gid=")[1].split("&")[0].split("#")[0]
                csv_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
            else:
                return self._error_redirect("Invalid Google Drive link. Please use a link to a Google Sheets document.")

            # Fetch the CSV data with timeout to prevent hanging
            response = requests.get(csv_url, timeout=30)
            response.raise_for_status()

            # Create a CSV reader from the response text (handles encoding automatically)
            csv_reader = csv.DictReader(response.text.splitlines())

            # Create a BulkAddUsers instance to use its process_csv_data method
            # Note: This reuses existing CSV processing logic. Any exceptions from
            # process_csv_data will be caught by the outer try/except blocks.
            bulk_add_view = BulkAddUsers()
            bulk_add_view.request = self.request
            bulk_add_view.auction = self.auction

            # Process the CSV data (this adds messages via self.request)
            bulk_add_view.process_csv_data(csv_reader)

            # Update the last sync time
            self.auction.last_sync_time = timezone.now()
            self.auction.save()
            self.auction.create_history("USERS", action="Google Drive sync complete")
            # Redirect to the users list
            url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
            return redirect(url)

        except requests.RequestException as e:
            if "401" in str(e):
                return self._error_redirect(
                    "Unable to fetch data from Google Drive. Make sure the link is shared with 'anyone with the link can view'"
                )
            elif "404" in str(e):
                return self._error_redirect("Link not found or invalid")
            else:
                return self._error_redirect(f"Unable to fetch data from Google Drive. Error was {e}")

        except Exception as e:
            return self._error_redirect(f"An error occurred while importing from Google Drive: {e}")

    def _error_redirect(self, error_message):
        """Helper method to display error and redirect to bulk add users page"""
        messages.error(self.request, error_message)
        url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
        return redirect(url)


class BulkAddLots(AuctionViewMixin, TemplateView):
    """Add/edit lots of lots for a given auctiontos pk"""

    template_name = "auctions/bulk_add_lots.html"
    allow_non_admins = True

    def get(self, *args, **kwargs):
        lot_formset = self.LotFormSet(
            form_kwargs={
                "tos": self.tos,
                "auction": self.auction,
                # "custom_lot_numbers_used": [],
                "is_admin": self.is_admin,
            },
            queryset=self.queryset,
        )
        helper = LotFormSetHelper()
        context = self.get_context_data(**kwargs)
        context["formset"] = lot_formset
        context["helper"] = helper
        return self.render_to_response(context)

    def post(self, *args, **kwargs):
        lot_formset = self.LotFormSet(
            self.request.POST,
            form_kwargs={
                "tos": self.tos,
                "auction": self.auction,
                # "custom_lot_numbers_used": [],
                "is_admin": self.is_admin,
            },
            queryset=self.queryset,
        )
        if lot_formset.is_valid():
            lots = lot_formset.save(commit=False)
            new_lot_count = 0
            for lot in lots:
                lot.auctiontos_seller = self.tos
                lot.auction = self.auction
                if self.tos.user:
                    lot.user = self.tos.user
                # if not lot.description:
                #    lot.description = ""
                if not lot.pk:
                    new_lot_count += 1
                    lot.added_by = self.request.user
                    if not self.is_admin:
                        if self.tos.user:
                            lot.user = self.tos.user
                lot.save()
            if lots:
                updated_lot_count = len(lots) - new_lot_count
                self.auction.create_history(
                    applies_to="LOTS",
                    action=f"Bulk added {new_lot_count}{f' and updated {updated_lot_count}' if updated_lot_count else ''} lots for {self.tos.name}",
                    user=self.request.user,
                )
                messages.success(self.request, f"Updated lots for {self.tos.name}")
                invoice = Invoice.objects.filter(auctiontos_user=self.tos, auction=self.auction).first()
                if not invoice:
                    invoice = Invoice.objects.create(auctiontos_user=self.tos, auction=self.auction)
                invoice.recalculate
            # when saving labels, it doesn't take you off from the page you're on
            # So we need to go somewhere, and then say "download labels"
            if "print" in str(self.request.GET.get("type", "")):
                print_url = f"printredirect={reverse('print_labels_by_bidder_number', kwargs={'slug': self.auction.slug, 'bidder_number': self.tos.bidder_number})}"
            else:
                print_url = ""
            if self.is_admin:
                redirect_url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
                if print_url:
                    redirect_url += "?" + print_url
            else:
                redirect_url = reverse("selling")
                if print_url:
                    redirect_url += "?" + print_url
            return redirect(redirect_url)

        context = self.get_context_data(**kwargs)
        context["formset"] = lot_formset
        context["helper"] = LotFormSetHelper()
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tos"] = self.tos
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin
        return context

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_admin = False
        if not self.auction:
            raise Http404
        bidder_number = kwargs.pop("bidder_number", None)
        self.tos = None
        if bidder_number:
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
        if self.is_auction_admin:
            self.is_admin = True
        if not self.tos:
            # if you don't got permission to edit this auction, you can only add lots for yourself
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )
        if not self.tos:
            messages.error(request, "You can't add lots until you join this auction")
            return redirect(
                f"/auctions/{self.auction.slug}/?next={reverse('bulk_add_lots_for_myself', kwargs={'slug': self.auction.slug})}"
            )
        else:
            if not self.tos.selling_allowed and not self.is_admin:
                messages.error(request, "You don't have permission to add lots to this auction")
                return redirect(f"/auctions/{self.auction.slug}/")
        if not self.is_admin and not self.auction.can_submit_lots:
            messages.error(request, f"Lot submission has ended for {self.auction}")
            return redirect(f"/auctions/{self.auction.slug}/")
        if not self.is_admin and not self.auction.allow_bulk_adding_lots:
            messages.error(
                request,
                "Bulk adding lots has been disabled in this auction, add your lots one at a time using this form",
            )
            return redirect(self.auction.add_lot_link)
        self.queryset = self.tos.unbanned_lot_qs
        if self.auction.max_lots_per_user:
            # default rows should be the max that are allowed in the auction
            if self.queryset.count() > self.auction.max_lots_per_user:
                extra = self.queryset.count()
            else:
                extra = self.auction.max_lots_per_user - self.queryset.count()
            # but of course sometimes admisn will break the rules for their users:
            if extra < 0:
                extra = 0
        else:
            extra = 5  # default rows to show if max_lots_per_user is not set for this auction
        self.LotFormSet = modelformset_factory(
            Lot,
            extra=extra,
            fields=(
                # "custom_lot_number",
                "lot_name",
                "summernote_description",
                "species_category",
                "i_bred_this_fish",
                "quantity",
                "donation",
                "reserve_price",
                "buy_now_price",
                "custom_checkbox",
                "custom_field_1",
            ),
            form=QuickAddLot,
        )
        return super().dispatch(request, *args, **kwargs)


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
        try:
            latitude = self.request.COOKIES["latitude"]
            longitude = self.request.COOKIES["longitude"]
            qs = qs.annotate(distance=distance_to(latitude, longitude))
        except:
            if self.request.user.is_authenticated:
                userData, created = UserData.objects.get_or_create(
                    user=self.request.user,
                    defaults={},
                )
                latitude = userData.latitude
                longitude = userData.longitude
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
        try:
            defaultBidAmount = Bid.objects.get(user=self.request.user, lot_number=lot.pk, is_deleted=False).amount
            context["viewer_bid_pk"] = Bid.objects.get(user=self.request.user, lot_number=lot.pk, is_deleted=False).pk
            context["viewer_bid"] = defaultBidAmount
            defaultBidAmount = defaultBidAmount + 1
        except:
            defaultBidAmount = 0
            context["viewer_bid"] = None
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
                if defaultBidAmount > lot.high_bid + max(math.floor(lot.high_bid * 0.05), 1):
                    pass
                else:
                    defaultBidAmount = lot.high_bid + max(math.floor(lot.high_bid * 0.05), 1)
        context["viewer_pk"] = self.request.user.pk
        try:
            context["submitter_pk"] = lot.user.pk
        except:
            context["submitter_pk"] = 0
        context["user_specific_bidding_error"] = False
        if not self.request.user.is_authenticated:
            context["user_specific_bidding_error"] = (
                f"You have to <a href='/login/?next={lot.lot_link}'>sign in</a> to place bids."
            )
        if context["viewer_pk"] == context["submitter_pk"]:
            context["user_specific_bidding_error"] = "You can't bid on your own lot"
        context["amount"] = defaultBidAmount
        context["watched"] = Watch.objects.filter(lot_number=lot.lot_number, user=self.request.user.id)
        context["category"] = lot.species_category
        # context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':lot.pk, "amount":defaultBidAmount}, request=self.request)
        context["user_tos"] = None
        context["user_tos_location"] = None
        if lot.auction and self.request.user.is_authenticated:
            tos = AuctionTOS.objects.filter(
                Q(user=self.request.user) | Q(email=self.request.user.email),
                auction=lot.auction,
            ).first()
            if tos:
                context["user_tos"] = True
                context["user_tos_location"] = tos.pickup_location
                if not tos.bidding_allowed:
                    context["user_specific_bidding_error"] = "This auction requires admin approval before you can bid"
            else:
                context["user_specific_bidding_error"] = (
                    f"This lot is part of <b>{lot.auction}</b>. Please <a href='/auctions/{lot.auction.slug}/?next={lot.lot_link}#join'>read the auction's rules and join the auction</a> to bid<br>"
                )
            if not lot.auction.is_online and lot.auction.message_users_when_lots_sell:
                context["push_notifications_possible"] = True
        if lot.within_dynamic_end_time and lot.minutes_to_end > 0 and not lot.sealed_bid:
            messages.info(
                self.request,
                "Bidding is ending soon.  Bids placed now will extend the end time of this lot.  This page will update automatically, you don't need to reload it",
            )
        if not context["user_tos"] and not lot.ended and lot.auction:
            if lot.auction.online_bidding != "disable":
                messages.info(
                    self.request,
                    f"Please <a href='/auctions/{lot.auction.slug}/?next=/lots/{lot.pk}/'>read the auction's rules and join the auction</a> to bid",
                )
        if self.request.user.is_authenticated:
            userData, created = UserData.objects.get_or_create(
                user=self.request.user,
                defaults={},
            )
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
                bids = Bid.objects.exclude(is_deleted=True).filter(lot_number=lot.pk)
                context["bids"] = bids
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
        except:
            context["distance"] = 0
        # for lots that are part of an auction, it's very handy to show the exchange info right on the lot page
        # this should be visible only to people running the auction or the seller
        if lot.auction and lot.auction.is_online and lot.sold:
            if context["is_auction_admin"] or self.request.user == lot.user:
                context["show_exchange_info"] = True
        context["show_image_add_button"] = lot.image_permission_check(self.request.user)
        # chat subscription stuff
        if self.request.user.is_authenticated:
            context["show_chat_subscriptions_checkbox"] = True
            context["autocheck_chat_subscriptions"] = "false"
            existing_subscription = ChatSubscription.objects.filter(lot=lot, user=self.request.user).first()
            if (
                lot.user
                and lot.user == self.request.user
                and not self.request.user.userdata.email_me_when_people_comment_on_my_lots
            ):
                context["show_chat_subscriptions_checkbox"] = False
            if lot.user != self.request.user and not self.request.user.userdata.email_me_about_new_chat_replies:
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
                result = {
                    "type": "chat_message",
                    "info": "CHAT",
                    "message": "This lot is about to be sold!",
                    "pk": -1,
                    "username": "System",
                }
                lot.send_websocket_message(result)
                watchers = Watch.objects.filter(
                    lot_number=lot.pk, user__userdata__push_notifications_when_lots_sell=True
                ).exclude(
                    # it would be awkward to have notifications pop up when you're projecting an image of the lot
                    user=self.request.user
                )
                for watch in watchers:
                    # does the user actually have a subscription?
                    if PushInformation.objects.filter(user=watch.user).first():
                        payload = {
                            "head": lot.lot_name + " is about to be sold",
                            "body": f"Lot {lot.custom_lot_number}  Don't miss out, bid now!  You're getting this notification because you watched this lot.",
                            "url": "https://" + lot.full_lot_link,
                            "tag": f"lot_sell_notification_{lot.pk}",
                        }
                        if lot.thumbnail:
                            payload["icon"] = lot.thumbnail.image.url
                        send_user_notification(user=watch.user, payload=payload, ttl=10000)
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
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = "RANDOM"
        uploaded_image = form.cleaned_data.get("image")

        # Attempt to convert non-standard JPEG formats (like MPO) to standard JPEG
        try:
            with Image.open(uploaded_image) as img:
                if img.format != "JPEG":
                    img = img.convert("RGB")  # Ensure it's in a JPEG-safe mode
                    buffer = BytesIO()
                    img.save(buffer, format="JPEG")
                    buffer.seek(0)
                    image.image.save(
                        uploaded_image.name.replace(".jpeg", "") + ".jpg", ContentFile(buffer.read()), save=False
                    )
        except Exception as e:
            logger.error("Error processing image: %s", e)
        try:
            image.save()
        except Exception as e:
            form.add_error("image", f"Image is not in a supported format or is corrupt.  Error: {e}")
            return self.form_invalid(form)
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
        except:
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
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = "RANDOM"
        image.save()
        messages.success(self.request, "Image updated")
        return super().form_valid(form)


class LotValidation(LoginRequiredMixin):
    """
    Base class for adding a lot.  This defines the rules for validating a lot
    """

    auction = None  # used for specifying which auction via GET param

    def dispatch(self, request, *args, **kwargs):
        # if the user hasn't filled out their address, redirect:
        userData, created = UserData.objects.get_or_create(
            user=request.user.pk,
            defaults={},
        )
        if not userData.address or not request.user.first_name or not request.user.last_name:
            messages.error(self.request, "Please fill out your contact info before creating a lot")
            return redirect("/contact_info?next=/lots/new/")
            # return redirect(reverse("contact_info"))
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form, **kwargs):
        """
        There is quite a lot that needs to be done before the lot is saved
        """
        lot = form.save(commit=False)
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
            userData, created = UserData.objects.get_or_create(
                user=self.request.user.pk,
                defaults={},
            )
            userData.last_auction_used = lot.auction
            userData.last_activity = timezone.now()
            userData.save()
            auctiontos = AuctionTOS.objects.filter(user=self.request.user, auction=lot.auction).first()
            if not auctiontos:
                # it should not be possible to get here (famous last words...)
                # remember that on form submit in CreateLotForm.clean(), we are validating that the user has an auctiontos
                messages.error(
                    self.request,
                    f"You need to <a href='/auctions/{lot.auction.slug}'>confirm your pickup location for this auction</a> before people can bid on this lot.",
                )
            else:
                lot.auctiontos_seller = auctiontos
                invoice = Invoice.objects.filter(auctiontos_user=auctiontos, auction=lot.auction).first()
                if not invoice:
                    invoice = Invoice.objects.create(auctiontos_user=auctiontos, auction=lot.auction)
                invoice.recalculate
        else:
            # this lot is NOT part of an auction
            try:
                run_duration = int(form.cleaned_data["run_duration"])
            except:
                run_duration = 10
            if not lot.date_posted:
                lot.date_posted = timezone.now()
            lot.date_end = lot.date_posted + timedelta(days=run_duration)
            lot.lot_run_duration = run_duration
            lot.donation = False
        # someday we may change this to be a field on the form, but for now we need to collect data
        lot.promotion_weight = randint(0, 20)
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
                    originalLot = Lot.objects.get(pk=form.cleaned_data["cloned_from"], is_deleted=False)
                    if (originalLot.user.pk == self.request.user.pk) or self.request.user.is_superuser:
                        originalImages = LotImage.objects.filter(lot_number=originalLot.lot_number)
                        for originalImage in originalImages:
                            newImage = LotImage.objects.create(
                                createdon=originalImage.createdon,
                                lot_number=lot,
                                image_source=originalImage.image_source,
                                is_primary=originalImage.is_primary,
                            )
                            newImage.image = get_thumbnailer(originalImage.image)
                            # if the original lot sold, this picture sure isn't of the actual item
                            if originalLot.winner and originalImage.image_source == "ACTUAL":
                                newImage.image_source = "REPRESENTATIVE"
                            newImage.save()
                        # we are only cloning images here, not watchers, views, or other related models
                except Exception as e:
                    logger.exception(e)
            msg = "Created lot! "
            if not lot.image_count:
                msg += f"You should probably <a href='/images/add_image/{lot.lot_number}/'>add an image</a>  to this lot.  Or, "
            msg += "<a href='/lots/new'>create another lot</a>"
            messages.success(
                self.request,
                msg,
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
        return context

    def form_valid(self, form, **kwargs):
        """When a new lot is created, make sure to create an invoice for the seller"""
        lot = form.save(commit=False)
        if lot.auction and lot.auctiontos_seller:
            invoice = Invoice.objects.filter(auctiontos_user=lot.auctiontos_seller, auction=lot.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=lot.auctiontos_seller, auction=lot.auction)
            invoice.recalculate
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
        userData, created = UserData.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        if userData.last_auction_used:
            if (
                userData.last_auction_used.can_submit_lots
                and not userData.last_auction_used.is_online
                and userData.last_auction_used.allow_bulk_adding_lots
            ):
                messages.info(
                    request,
                    f"Sick of adding lots one at a time?  <a href='{reverse('bulk_add_lots_for_myself', kwargs={'slug': userData.last_auction_used.slug})}'>Add lots of lots to {userData.last_auction_used}</a>",
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
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            messages.error(request, "Only the lot creator can edit a lot")
            return redirect("/")
        if not self.get_object().can_be_edited:
            messages.error(request, self.get_object().cannot_be_edited_reason)
            return redirect("/")
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


class AuctionDelete(AuctionViewMixin, DeleteView):
    model = Auction

    def dispatch(self, request, *args, **kwargs):
        result = super().dispatch(request, *args, **kwargs)
        if not self.auction.can_be_deleted:
            messages.error(request, "There are already lots in this auction, it can't be deleted")
            return redirect("/")
        return result

    def get_success_url(self):
        return "/auctions/"


class LotDelete(LoginRequiredMixin, DeleteView):
    model = Lot

    def dispatch(self, request, *args, **kwargs):
        if not self.get_object().can_be_deleted:
            messages.error(request, self.get_object().cannot_be_deleted_reason)
            return redirect("/")
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            messages.error(request, "Only the creator of a lot can delete it")
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return f"/lots/user/?user={self.request.user.pk}"

    def form_valid(self, form):
        """Track history when a lot is deleted"""
        lot = self.get_object()
        if lot.auction:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Deleted lot {lot.lot_number_display}",
                user=self.request.user,
            )
        return super().form_valid(form)


class ImageDelete(LoginRequiredMixin, DeleteView):
    model = LotImage

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().lot_number.user == request.user:
            auth = True
        if not self.get_object().lot_number.can_be_edited:
            auth = False
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "You can't change this image")
            return redirect(f"/lots/{self.get_object().lot_number.lot_number}/{self.get_object().lot_number.slug}/")
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
            except:
                pass
        return f"/lots/{self.get_object().lot_number.lot_number}/{self.get_object().lot_number.slug}/"


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
        lot = self.get_object().lot_number
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
            history_message = choice(own_bid_removal_messages).format(user=self.request.user)
        else:
            history_message = f"{self.request.user} has removed {self.get_object().user}'s bid"
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
        self.get_object().delete()
        LotHistory.objects.create(lot=lot, user=self.request.user, message=history_message, changed_price=True)
        return HttpResponseRedirect(success_url)

    def get_success_url(self):
        return f"/lots/{self.get_object().lot_number.pk}/{self.get_object().lot_number.slug}/"


class LotAdmin(TemplateView, FormMixin, AuctionViewMixin):
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
            # need to make sure the winner matches the auctiontos_winner
            if obj.pk and obj.winner:
                if not obj.auctiontos_winner:
                    obj.winner = None
                elif obj.auctiontos_winner.user:
                    obj.winner = obj.auctiontos_winner.user
                # winner not set if auctiontos_winner is set for the first time...don't see a real downside here, winner is generally not set as part of an auction anyway
            obj.save()
            # add message if the winner changed
            if obj.auctiontos_winner:
                if self.lot_initial_winner != obj.auctiontos_winner:
                    obj.add_winner_message(self.request.user, obj.auctiontos_winner, obj.winning_price)
                    if not obj.date_end:
                        obj.date_end = timezone.now()
                        obj.active = False
                        obj.save()
            return HttpResponse("<script>location.reload();</script>", status=200)
            # return HttpResponse("<script>closeModal();</script>", status=200)
        else:
            return self.form_invalid(form)


class AuctionTOSDelete(TemplateView, FormMixin, AuctionViewMixin):
    """Delete AuctionTOSs"""

    template_name = "auctions/auctiontos_confirm_delete.html"
    form_class = DeleteAuctionTOS
    model = AuctionTOS

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop("pk")
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["auctiontos"] = self.auctiontos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auctiontos"] = self.auctiontos
        context["tooltip"] = ""
        context["modal_title"] = f"Delete {self.auctiontos.name}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            success_url = reverse("auction_tos_list", kwargs={"slug": self.auctiontos.auction.slug})
            sold_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=self.auctiontos)
            won_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=self.auctiontos)
            if form.cleaned_data["delete_lots"]:
                for lot in sold_lots:
                    lot.delete()
                for lot in won_lots:
                    LotHistory.objects.create(
                        lot=lot,
                        user=request.user,
                        message=f"{request.user.username} has removed {self.auctiontos} from this auction, this lot no longer has a winner.",
                        notification_sent=True,
                        bid_amount=0,
                        changed_price=True,
                        seen=True,
                    )
                    lot.auctiontos_winner = None
                    lot.winning_price = None
                    lot.active = True
                    lot.save()
            else:
                if form.cleaned_data["merge_with"]:
                    new_auctiontos = AuctionTOS.objects.get(pk=form.cleaned_data["merge_with"])
                    invoice = Invoice.objects.filter(
                        auctiontos_user=new_auctiontos, auction=new_auctiontos.auction
                    ).first()
                    if not invoice:
                        invoice = Invoice.objects.create(auctiontos_user=new_auctiontos, auction=new_auctiontos.auction)
                    invoice.recalculate
                    for lot in sold_lots:
                        lot.auctiontos_seller = new_auctiontos
                        lot.save()
                    for lot in won_lots:
                        lot.auctiontos_winner = new_auctiontos
                        lot.save()
                        lot.add_winner_message(request.user, new_auctiontos, lot.winning_price)
                    invoice.recalculate
            # not needed if we have models.CASCADE on Invoice
            # invoices = Invoice.objects.filter(auctiontos_user=self.auctiontos)
            # for invoice in invoices:
            #    invoice.delete()
            self.auction.create_history(applies_to="USERS", action=f"Deleted {self.auctiontos.name}", user=request.user)
            self.auctiontos.delete()
            return redirect(success_url)
        else:
            return self.form_invalid(form)


class AuctionTOSAdmin(TemplateView, FormMixin, AuctionViewMixin):
    """Creation and management for AuctionTOSs"""

    template_name = "auctions/generic_admin_form.html"
    form_class = CreateEditAuctionTOS
    model = AuctionTOS

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop("pk")
        self.is_edit_form = True
        try:
            self.auctiontos = AuctionTOS.objects.get(pk=pk)
        except Exception:
            self.auctiontos = None
        if self.auctiontos:
            self.auction = self.auctiontos.auction
        else:
            try:
                self.auction = Auction.objects.get(slug=pk, is_deleted=False)
                self.is_edit_form = False
            except:
                raise Http404
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["is_edit_form"] = self.is_edit_form
        form_kwargs["auctiontos"] = self.auctiontos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.is_edit_form and self.auction.is_online:
            context["tooltip"] = (
                "This is an online auction: users should join through this site. You probably don't want to add them here."
            )
        # context['new_form'] = CreateEditAuctionTOS(
        #     is_edit_form=self.is_edit_form,
        #     auctiontos=self.auctiontos,
        #     auction=self.auction
        # )
        context["unsold_lot_warning"] = ""
        if self.auctiontos:
            try:
                invoice = self.auctiontos.invoice
                invoice_string = invoice.invoice_summary_short
                context["top_buttons"] = render_to_string("invoice_buttons.html", {"invoice": invoice})
                context["unsold_lot_warning"] = invoice.unsold_lot_warning
            except:
                invoice = None
                invoice_string = ""
            context["modal_title"] = f"{self.auctiontos.name} {invoice_string}"
        else:
            context["modal_title"] = "Add new user"
        if self.auctiontos:
            context["invoice"] = self.auctiontos.invoice
        # for real time form validation
        extra_script = "<script>"
        if self.auctiontos:
            extra_script += f"var pk={self.auctiontos.pk};"
        else:
            extra_script += "var pk=null;"
        extra_script += f"""var validation_url = '{reverse("auctiontos_validation", kwargs={"slug": self.auction.slug})}';
                            var csrf_token = '{get_token(self.request)}';"""
        extra_script += """

    function setFieldInvalid(fieldId, message, is_invalid) {
        var field = document.getElementById(fieldId);
        if (!field) return;

        var feedbackId = fieldId + "_feedback";
        var feedback = document.getElementById(feedbackId);

        if (is_invalid) {
            field.classList.add("is-invalid");
            var existing_error = document.getElementById( "error_1_"+fieldId);
            if (existing_error) {
                existing_error.remove();
            }
            if (feedback) {
                feedback.remove();
            }
            feedback = document.createElement("div");
            feedback.id = feedbackId;
            feedback.className = "invalid-feedback";
            field.parentNode.appendChild(feedback);

            feedback.textContent = message;
        } else {
            field.classList.remove("is-invalid");
            if (feedback) {
                feedback.remove();
            }
        }
    }

    function showAutocomplete(response, remove) {
        var feedback = document.getElementById('id_name_feedback');
        if (feedback) {
            feedback.remove();
        }
        if (remove) {
            return;
        }
        feedback = document.createElement("div");
        feedback.id = "id_name_feedback";
        feedback.className = "valid-feedback d-block cursor-pointer";
        feedback.innerHTML = "<button role='button' class='btn btn-sm btn-info' id='autocompleteTosForm'>Click to use " + \
            response.id_email + "</button>";
        var autocomplete = response;
        document.getElementById('id_name').parentNode.appendChild(feedback);

        //setTimeout(function() {
            var link = document.getElementById('autocompleteTosForm');
            link.addEventListener('click', function(event) {
            event.preventDefault();

            for (var key in autocomplete) {
                console.log(key);
                if (autocomplete.hasOwnProperty(key)) {
                    var element = document.getElementById(key);
                    if (element) {
                        if (element.type !== "checkbox" && element.value === "") {
                            element.value = autocomplete[key] || '';
                        }
                        if (element.type === "checkbox") {
                            element.checked = autocomplete[key] === true;
                        }
                    }
                }
            }

            });
            link.focus();
        //}, 40);

    }


    function showTooltip(element, message) {
        var el = element;

        // Destroy existing tooltip (if any)
        if (el._tooltipInstance) {
            el._tooltipInstance.dispose();
        }

        // Set tooltip attributes
        el.setAttribute("data-bs-toggle", "tooltip");
        el.setAttribute("data-bs-placement", "right");
        el.setAttribute("title", message);

        // Initialize and show Bootstrap tooltip
        el._tooltipInstance = new bootstrap.Tooltip(el);
        el._tooltipInstance.show();
    }

    function hideTooltip(element) {
        if (element._tooltipInstance) {
            element._tooltipInstance.dispose();
            element._tooltipInstance = null;
        }
    }

    function validateField() {
        var nameInput = document.getElementById("id_name");

        var data = {
            pk: pk,
            name: $("#id_name").val(),
            bidder_number: $("#id_bidder_number").val(),
            email: $("#id_email").val(),
        };

        $.ajax({
            url: validation_url,
            type: "POST",
            data: data,
            headers: { "X-CSRFToken": csrf_token },
            success: function (response) {
                if (response.name_tooltip) {
                    showTooltip(nameInput, response.name_tooltip);
                    showAutocomplete(response, true)
                } else if (response.id_email) {
                    showAutocomplete(response)
                } else {
                    hideTooltip(nameInput);
                    showAutocomplete(response, true)
                }
                if (response.email_tooltip) {
                    setFieldInvalid("id_email", response.email_tooltip, true);
                } else {
                    setFieldInvalid("id_email", response.email_tooltip, false);
                }
                if (response.bidder_number_tooltip) {
                    setFieldInvalid("id_bidder_number", response.bidder_number_tooltip, true);
                } else {
                    setFieldInvalid("id_bidder_number", response.bidder_number_tooltip, false);
                }
            }
        });
    }

    $("#id_bidder_number, #id_name, #id_email").on("blur", validateField);
        </script>"""
        context["extra_script"] = mark_safe(extra_script)
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            if self.auctiontos:
                obj = self.auctiontos
                if form.has_changed():
                    self.auction.create_history(
                        applies_to="USERS",
                        action=f"Edited {obj.name}: ",
                        user=request.user,
                        form=form,
                    )
            else:
                obj = AuctionTOS.objects.create(
                    auction=self.auction,
                    pickup_location=form.cleaned_data["pickup_location"],
                    manually_added=True,
                )
                self.auction.create_history(
                    applies_to="USERS",
                    action=f"Added {form.cleaned_data['name']}",
                    user=request.user,
                )
            obj.bidder_number = form.cleaned_data["bidder_number"]
            obj.pickup_location = form.cleaned_data["pickup_location"]
            obj.name = form.cleaned_data["name"]
            obj.email = form.cleaned_data["email"]
            obj.phone_number = form.cleaned_data["phone_number"]
            obj.address = form.cleaned_data["address"]
            obj.is_admin = form.cleaned_data["is_admin"]
            obj.bidding_allowed = form.cleaned_data["bidding_allowed"]
            obj.selling_allowed = form.cleaned_data["selling_allowed"]
            obj.is_club_member = form.cleaned_data["is_club_member"]
            obj.memo = form.cleaned_data["memo"]
            obj.save()
            return HttpResponse("<script>location.reload();</script>", status=200)
            # return HttpResponse("<script>closeModal();</script>", status=200)
        else:
            name = form.cleaned_data.get("name")
            if not name:
                self.get_form().add_error("name", "Name is required")
            return self.form_invalid(form)


class AuctionCreateView(CreateView, LoginRequiredMixin):
    """
    Creating a new auction
    """

    model = Auction
    template_name = "auction_create_form.html"
    form_class = CreateAuctionForm
    redirect_url = None  # really only used if this is a cloned auction
    cloned_from = None

    def dispatch(self, request, *args, **kwargs):
        original_dispatch = super().dispatch(request, *args, **kwargs)
        auction_creation_allowed = False
        if self.request.user.is_authenticated and self.request.user.userdata.can_create_club_auctions:
            auction_creation_allowed = True
        if self.request.user.is_superuser:
            auction_creation_allowed = True
        if not auction_creation_allowed:
            return redirect("/")
        return original_dispatch

    def get_success_url(self):
        if self.redirect_url:
            return self.redirect_url
        else:
            messages.success(
                self.request,
                "Auction created!  Now, create a location to exchange lots.",
            )
            return reverse("create_auction_pickup_location", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "New auction"
        context["new"] = True
        userData, created = UserData.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        # a bit of logic used on auction_create_form.html to suggest auction names
        context["club"] = ""
        club = userData.club
        if club:
            context["club"] = str(club)
            if club.abbreviation:
                context["club"] = club.abbreviation
        return context

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["user"] = self.request.user
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        data = self.request.GET.copy()
        self.cloned_from = data.get("copy", None)
        kwargs["cloned_from"] = self.cloned_from
        return kwargs

    def form_valid(self, form, **kwargs):
        """Rules for new auction creation"""
        auction = form.save(commit=False)
        auction.created_by = self.request.user
        auction.promote_this_auction = False  # all auctions start not promoted
        cloned_from = form.cleaned_data["cloned_from"]
        auction.date_start = form.cleaned_data["date_start"]
        is_online = True
        clone_from_auction = None
        if "clone" in str(self.request.GET):
            try:
                original_auction = Auction.objects.get(slug=cloned_from, is_deleted=False)
                if original_auction:
                    # you still don't get to clone auctions that aren't yours...
                    if original_auction.permission_check(self.request.user):
                        clone_from_auction = original_auction
            except Exception as e:
                logger.exception(e)
                pass
        elif "online" in str(self.request.GET):
            is_online = True
        else:
            is_online = False
        run_duration = timezone.timedelta(days=7)  # only set for is_online
        online_bidding_start_diff = timezone.timedelta(days=7)
        online_bidding_end_diff = timezone.timedelta(minutes=0)
        lot_submission_end_date_diff = timezone.timedelta(minutes=0)
        if clone_from_auction:
            fields_to_clone = [
                "is_online",
                "summernote_description",
                "lot_entry_fee",
                "unsold_lot_fee",
                "winning_bid_percent_to_club",
                "first_bid_payout",
                "sealed_bid",
                "max_lots_per_user",
                "allow_additional_lots_as_donation",
                "make_stats_public",
                "use_categories",
                "bump_cost",
                "is_chat_allowed",
                "lot_promotion_cost",
                "code_to_add_lots",
                "online_bidding",
                "pre_register_lot_discount_percent",
                "only_approved_sellers",
                "only_approved_bidders",
                "email_users_when_invoices_ready",
                "invoice_payment_instructions",
                "minimum_bid",
                "winning_bid_percent_to_club_for_club_members",
                "lot_entry_fee_for_club_members",
                "set_lot_winners_url",
                "require_phone_number",
                "buy_now",
                "reserve_price",
                "tax",
                "advanced_lot_adding",
                "date_online_bidding_starts",
                "date_online_bidding_ends",
                "allow_deleting_bids",
                "auto_add_images",
                "message_users_when_lots_sell",
                "label_print_fields",
                "force_donation_threshold",
                "use_quantity_field",
                "custom_checkbox_name",
                "custom_field_1",
                "custom_field_1_name",
                "allow_bulk_adding_lots",
                "copy_users_when_copying_this_auction",
                "use_donation_field",
                "use_i_bred_this_fish_field",
                "use_seller_dash_lot_numbering",
                "enable_online_payments",
                "alternative_split_label",
                "google_drive_link",
            ]
            for field in fields_to_clone:
                setattr(auction, field, getattr(original_auction, field))
            if original_auction.date_end:
                run_duration = original_auction.date_end - original_auction.date_start
            if original_auction.date_online_bidding_starts:
                online_bidding_start_diff = original_auction.date_start - original_auction.date_online_bidding_starts
            if original_auction.date_online_bidding_ends:
                online_bidding_end_diff = original_auction.date_start - original_auction.date_online_bidding_ends
            if original_auction.lot_submission_end_date:
                lot_submission_end_date_diff = original_auction.date_start - original_auction.lot_submission_end_date
            auction.cloned_from = original_auction
        else:
            auction.is_online = is_online
            if not is_online:
                # override default settings for new in-person auctions
                auction.online_bidding = "disable"
                auction.buy_now = "disable"
                auction.reserve_price = "disable"
            else:
                # override default settings for new online auctions
                auction.use_quantity_field = True
        if not auction.summernote_description:
            auction.summernote_description = """
            <h4>General information</h4>
            You should remove this line and edit this section to suit your auction.
            Use the formatting here as an example.<br><br>
            <h4>Rules</h4>
            <ul><li>You cannot sell anything banned by state law.</li>
            <li>All lots must be properly bagged.  No leaking bags!</li>
            <li>You do not need to be a club member to buy or sell lots.</li></ul>"""
        if auction.is_online:
            auction.date_end = auction.date_start + run_duration
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_end
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start
        else:
            auction.date_end = None
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_start - lot_submission_end_date_diff
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start - run_duration
            if not auction.date_online_bidding_starts:
                auction.date_online_bidding_starts = auction.date_start - online_bidding_start_diff
            if not auction.date_online_bidding_ends:
                auction.date_online_bidding_ends = auction.date_start - online_bidding_end_diff
        auction.save()
        # let's route in-person auctions to the rule page next
        if not auction.is_online and not clone_from_auction:
            self.redirect_url = auction.get_edit_url()
            # Create a default pickup location.  This is handled better in models.auction.save()
            # PickupLocation.objects.create(
            #     name=str(auction),
            #     auction=auction,
            #     is_default=True,
            #     user=self.request.user)
        if clone_from_auction:
            # because we will almost certainly have locations, we can simply default to the main auction page
            self.redirect_url = auction.get_absolute_url()
            originalLocations = PickupLocation.objects.filter(auction=clone_from_auction)
            for location in originalLocations:
                location.pk = None  # duplicate all fields
                if location.name == str(clone_from_auction):
                    location.name = str(auction)
                location.auction = auction
                auction_time = clone_from_auction.date_start
                if clone_from_auction.date_end:
                    auction_time = clone_from_auction.date_end
                if location.pickup_time:
                    firstTimeDiff = location.pickup_time - auction_time
                    if auction.date_end:
                        location.pickup_time = auction.date_end + firstTimeDiff
                    else:
                        location.pickup_time = auction.date_start + firstTimeDiff
                if location.second_pickup_time:
                    secondTimeDiff = location.second_pickup_time - auction_time
                    if auction.date_end:
                        location.second_pickup_time = auction.date_end + secondTimeDiff
                    else:
                        location.second_pickup_time = auction.date_start + secondTimeDiff
                location.save()
            # copy any auctiontos, if appropriate
            if clone_from_auction.copy_users_when_copying_this_auction:
                auctiontos = AuctionTOS.objects.filter(auction=clone_from_auction)
                for tos in auctiontos:
                    # in tos.save(), bid permissions are reset if there's no pk
                    # to preserve them, we store them here, then resave again once the new instance is created
                    original_bid_permission = tos.bidding_allowed
                    tos.pk = None
                    tos.createdon = None
                    tos.auction = auction
                    # tos.email_address_status = "UNKNOWN"
                    tos.manually_added = True
                    tos.print_reminder_email_sent = False
                    if tos.pickup_location.name == str(clone_from_auction):
                        new_location_name = str(auction)
                    else:
                        new_location_name = tos.pickup_location.name
                    new_location = PickupLocation.objects.filter(auction=auction, name=new_location_name).first()
                    if new_location:
                        tos.pickup_location = new_location
                        tos.save()
                        tos.bidding_allowed = original_bid_permission
                        tos.save()  # see comment above
        action = "Created auction"
        if clone_from_auction:
            action += f" by copying {clone_from_auction}"
        auction.create_history(
            applies_to="RULES",
            action=action,
            user=self.request.user,
        )
        return super().form_valid(form)


class AuctionInfo(FormMixin, DetailView, AuctionViewMixin):
    """Main view of a single auction"""

    template_name = "auction.html"
    model = Auction
    form_class = AuctionJoin
    rewrite_url = None
    auction = None
    allow_non_admins = True

    def get(self, request, *args, **kwargs):
        if self.is_auction_admin:
            if str(request.GET.get("dismissed_promo_banner", "")).lower() in ("1", "true"):
                self.auction.dismissed_promo_banner = True
                self.auction.save()
            if self.auction.created_by.pk == request.user.pk:
                if str(request.GET.get("enable_online_payments", "")).lower() in ("1", "true"):
                    self.auction.enable_online_payments = True
                    self.auction.save()
                if str(request.GET.get("dismissed_paypal_banner", "")).lower() in ("1", "true"):
                    self.auction.dismissed_paypal_banner = True
                    self.auction.save()
                if str(request.GET.get("never_show_paypal_connect", "")).lower() in ("1", "true"):
                    messages.info(
                        request,
                        "You won't see the PayPal connection prompt again.  You can always enable PayPal under Preferences>More>Connect your PayPal account.",
                    )
                    request.user.userdata.never_show_paypal_connect = True
                    request.user.userdata.save()
        return super().get(request, *args, **kwargs)

    def get_object(self, *args, **kwargs):
        if self.auction:
            self.object = self.auction
        else:
            try:
                auction = Auction.objects.get(slug=self.kwargs.get(self.slug_url_kwarg), is_deleted=False)
                self.auction = auction
                self.object = self.auction
            except:
                msg = "No auctions found matching the query"
                raise Http404(msg)
        return self.object

    def get_success_url(self):
        data = self.request.GET.copy()
        try:
            if not data["next"]:
                data["next"] = self.auction.view_lot_link
            return data["next"]
        except Exception:
            return self.auction.view_lot_link

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["user"] = self.request.user
        form_kwargs["auction"] = self.auction
        return form_kwargs

    def dispatch(self, request, *args, **kwargs):
        if self.get_object().permission_check(request.user):
            locations = self.get_object().location_qs.count()
            if not locations:
                messages.info(
                    self.request,
                    "You haven't added any pickup locations to this auction yet. <a href='/locations/new/'>Add one now</a>",
                )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pickup_locations"] = self.auction.location_qs
        current_site = Site.objects.get_current()
        context["domain"] = current_site.domain
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        if self.auction.closed:
            context["ended"] = True
            messages.info(
                self.request,
                f"This auction has ended.  You can't bid on anything, but you can still <a href='{self.auction.view_lot_link}'>view lots</a>.",
            )
        else:
            context["ended"] = False
        try:
            existingTos = AuctionTOS.objects.get(user=self.request.user, auction=self.auction)
            existingTos = existingTos.pickup_location
            i_agree = True
            context["hasChosenLocation"] = existingTos.pk
        except:
            context["hasChosenLocation"] = False
            # if not self.get_object().no_location:
            #     # this selects the first location in multi-location auction as the default
            #     existingTos = PickupLocation.objects.filter(auction=self.get_object().pk)[0]
            # else:
            existingTos = None
            if self.auction.multi_location:
                i_agree = True
            else:
                i_agree = False
                existingTos = PickupLocation.objects.filter(auction=self.auction).first()
            # if self.request.user.is_authenticated and not context['ended']:
            #     if not self.get_object().no_location:
            #         messages.add_message(self.request, messages.ERROR, "Please confirm you have read these rules by selecting your pickup location at the bottom of this page.")
        context["active_tab"] = "main"
        if self.request.user.pk == self.auction.created_by.pk:
            invalidPickups = self.auction.pickup_locations_before_end
            if invalidPickups:
                messages.info(
                    self.request,
                    f"<a href='{invalidPickups}'>Some pickup times</a> are set before the end date of the auction",
                )
            if self.auction.time_start_is_at_night and not self.auction.is_online:
                messages.info(
                    self.request,
                    f"You know your auction is starting in the middle of the night, right? <a href='{reverse('edit_auction', kwargs={'slug': self.auction.slug})}'>Click here to change when bidding opens</a> and remember that it's in 24 hour time",
                )

        context["form"] = AuctionJoin(
            user=self.request.user,
            auction=self.auction,
            initial={
                "user": self.request.user.id,
                "auction": self.auction.pk,
                "pickup_location": existingTos,
                "i_agree": i_agree,
            },
        )
        context["rewrite_url"] = self.rewrite_url
        return context

    def post(self, request, *args, **kwargs):
        auction = self.auction
        form = self.get_form()
        if form.is_valid():
            userData, created = UserData.objects.get_or_create(
                user=self.request.user,
                defaults={},
            )
            if auction.require_phone_number and not userData.phone_number:
                messages.error(
                    self.request,
                    "This auction requires a phone number before you can join",
                )
                return redirect(f"/contact_info?next={auction.get_absolute_url()}")
            find_by_email = AuctionTOS.objects.filter(
                email=self.request.user.email,
                auction=auction,
                # manually_added=True,
                # user__isnull=True
            ).first()
            is_new_join = False
            if find_by_email:
                obj = find_by_email
                obj.user = self.request.user
            else:
                obj, created = AuctionTOS.objects.get_or_create(
                    user=self.request.user,
                    auction=auction,
                    defaults={"pickup_location": form.cleaned_data["pickup_location"]},
                )
                is_new_join = created
            obj.pickup_location = form.cleaned_data["pickup_location"]
            # check if mail was chosen
            if obj.pickup_location.pickup_by_mail:
                if not userData.address:
                    messages.error(
                        self.request,
                        "You have to set your address before you can choose pickup by mail",
                    )
                    return redirect(f"/contact_info?next={auction.get_absolute_url()}")
            if form.cleaned_data["time_spent_reading_rules"] > obj.time_spent_reading_rules:
                obj.time_spent_reading_rules = form.cleaned_data["time_spent_reading_rules"]
            # even if an auctiontos was originally manually added, if the user clicked join, mark them as not manually added
            obj.manually_added = False
            if obj.email_address_status == "UNKNOWN":
                # if it bounced in the past, the user may have a full inbox or something
                obj.email_address_status = "VALID"
            # fill out some information in the tos if not already filled out
            if not obj.name or obj.name == "Unknown":
                obj.name = self.request.user.first_name + " " + self.request.user.last_name
            if not obj.email:
                obj.email = self.request.user.email
            if not obj.phone_number:
                obj.phone_number = userData.phone_number
            if not obj.address:
                obj.address = userData.address
            obj.save()
            # also update userdata to reflect the last auction
            userData.last_auction_used = auction
            userData.last_activity = timezone.now()
            userData.save()
            # Only create history if this is a new join
            if is_new_join:
                auction.create_history(
                    applies_to="USERS",
                    action=f"{obj.name} has joined this auction",
                    user=self.request.user,
                )
            return self.form_valid(form)
        else:
            logger.debug(form.cleaned_data)
            return self.form_invalid(form)


class FAQ(AdminEmailMixin, ListView):
    """Show all questions"""

    model = FAQ
    template_name = "faq.html"
    ordering = ["category_text"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_site = Site.objects.get_current()
        context["domain"] = current_site.domain
        context["hide_google_login"] = True
        return context


class PromoSite(TemplateView):
    template_name = "promo.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_PROMO_PAGE:
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        context["online_tutorial"] = settings.ONLINE_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial"] = settings.IN_PERSON_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial_chapters"] = settings.IN_PERSON_TUTORIAL_CHAPTERS
        context["online_tutorial_chapters"] = settings.ONLINE_TUTORIAL_CHAPTERS
        return context


def toDefaultLandingPage(request):
    """
    Allow the user to pick up where they left off
    """

    def tos_check(request, auction, routeByLastAuction):
        if not auction:
            if request.user.is_authenticated:
                return AllLots.as_view()(request)
            else:
                # promo page for non-logged in users
                if settings.ENABLE_PROMO_PAGE:
                    return PromoSite.as_view()(request)
                else:
                    return AllAuctions.as_view()(request)
        try:
            # Did the user sign the tos yet?
            AuctionTOS.objects.get(user=request.user, auction=auction)
            # If so, redirect them to the lot view
            return AllLots.as_view(
                rewrite_url=f"/?{auction.slug}",
                auction=auction,
                routeByLastAuction=routeByLastAuction,
            )(request)
        except Exception:
            # No tos?  Take them there so they can sign
            return AuctionInfo.as_view(rewrite_url=f"/?{auction.slug}", auction=auction)(request)

    data = request.GET.copy()
    routeByLastAuction = False
    try:
        userData, created = UserData.objects.get_or_create(
            user=request.user,
            defaults={},
        )
        userData.last_activity = timezone.now()
        userData.save()
    except:
        # probably not signed in
        pass
    try:
        # if the slug was set in the URL
        auction = Auction.objects.exclude(is_deleted=True).filter(slug=list(data.keys())[0])[0]
        # return tos_check(request, auction, routeByLastAuction)
    except Exception:
        # if not, check and see if the user has been participating in an auction
        try:
            auction = UserData.objects.get(user=request.user).last_auction_used
            invoice = (
                Invoice.objects.filter(auctiontos_user__user=request.user, auctiontos_user__auction=auction)
                .exclude(status="DRAFT")
                .first()
            )
            if invoice:
                messages.info(
                    request,
                    f'{auction} has ended.  <a href="/invoices/{invoice.pk}">View your invoice</a> or <a href="/feedback/">leave feedback</a> on lots you bought or sold',
                )
                return redirect("/lots/")
            else:
                try:
                    # in progress online auctions get routed
                    AuctionTOS.objects.get(user=request.user, auction=auction, auction__is_online=True)
                    # only show the banner if the TOS is signed
                    # messages.add_message(request, messages.INFO, f'{auction} is the last auction you joined.  <a href="/lots/">View all lots instead</a>')
                    routeByLastAuction = True
                except:
                    pass
        except:
            # probably no userdata or userdata.auction is None
            auction = None
    return tos_check(request, auction, routeByLastAuction)


@login_required
def toAccount(request):
    # response = redirect(f'/users/{request.user.username}/')
    return redirect(reverse("userpage", kwargs={"slug": request.user.username}))


class AllAuctions(LocationMixin, SingleTableMixin, FilterView):
    model = Auction
    no_location_message = "Set your location to see how far away auctions are"
    table_class = AuctionHTMxTable
    filterset_class = AuctionFilter
    # paginate_by = 100

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "all_auctions.html"
        return template_name

    def get_queryset(self):
        last_auction_pk = -1
        if self.request.user.is_authenticated and self.request.user.userdata.last_auction_used:
            last_auction_pk = self.request.user.userdata.last_auction_used.pk
        qs = (
            Auction.objects.all()
            .annotate(
                is_last_used=Case(
                    When(pk=last_auction_pk, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            )
            .order_by("-is_last_used", "-date_start")
        )
        next_90_days = timezone.now() + timedelta(days=90)
        two_years_ago = timezone.now() - timedelta(days=365 * 2)
        standard_filter = Q(
            promote_this_auction=True,
            date_start__lte=next_90_days,
            date_posted__gte=two_years_ago,
        )
        latitude, longitude = self.get_coordinates()
        if latitude and longitude:
            closest_pickup_location_subquery = (
                PickupLocation.objects.filter(auction=OuterRef("pk"))
                .annotate(distance=distance_to(latitude, longitude))
                .order_by("distance")
                .values("distance")[:1]
            )
            qs = qs.annotate(distance=Subquery(closest_pickup_location_subquery))
        else:
            qs = qs.annotate(distance=Value(0, output_field=FloatField()))
        if not self.request.user.is_authenticated:
            qs = qs.exclude(is_deleted=True)
            return qs.filter(standard_filter).annotate(joined=Value(0, output_field=FloatField())).distinct()
        if self.request.user.is_superuser:
            # joined is disabled for admins because we need to return before filtering non-promoted auctions
            return qs.annotate(joined=Value(0, output_field=FloatField())).order_by("-date_posted").distinct()
        qs = qs.exclude(is_deleted=True)
        qs = (
            qs.filter(
                Q(auctiontos__user=self.request.user)
                | Q(auctiontos__email=self.request.user.email)
                | Q(created_by=self.request.user)
                | standard_filter
            )
            .annotate(
                joined=Exists(
                    AuctionTOS.objects.filter(
                        auction=OuterRef("pk"),
                        user=self.request.user,
                    )
                )
            )
            .distinct()
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        if not self.object_list.exists():
            context["no_results"] = (
                f"<span class='text-danger'>No auctions found.</span>  This only searches club auctions, if you're looking for {settings.WEBSITE_FOCUS} to buy, check out <a href='/lots/'>the list of lots for sale</a>"
            )
        context["show_new_auction_button"] = True
        if self.request.user.is_authenticated and not self.request.user.userdata.can_create_club_auctions:
            context["show_new_auction_button"] = False
        if not self.request.user.is_authenticated and not settings.ALLOW_USERS_TO_CREATE_AUCTIONS:
            context["show_new_auction_button"] = False
        if self.request.user.is_superuser:
            context["show_new_auction_button"] = True
        return context

    def get_table(self, **kwargs):
        return self.table_class(self.get_table_data(), request=self.request, **kwargs)


class Leaderboard(ListView):
    model = UserData
    template_name = "leaderboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["total_lots"] = UserData.objects.filter(rank_total_lots__isnull=False).order_by("rank_total_lots")
        context["unique_species"] = UserData.objects.filter(number_unique_species__isnull=False).order_by(
            "rank_unique_species"
        )
        # context['total_spent'] = UserData.objects.filter(rank_total_spent__isnull=False).order_by('rank_total_spent')
        context["total_bids"] = UserData.objects.filter(rank_total_bids__isnull=False).order_by("rank_total_bids")
        return context


class AllLots(LotListView, AuctionViewMixin):
    """Show all lots"""

    rewrite_url = (
        # use JS to rewrite the shown URL.  This is used only for auctions.
        None
    )
    auction = None
    allow_non_admins = True

    def render_to_response(self, context, **response_kwargs):
        """override the default just to add a cookie -- this will allow us to save ordering for subsequent views"""
        response = super().render_to_response(context, **response_kwargs)
        try:
            response.set_cookie("lot_order", self.ordering)
        except:
            pass
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        data = self.request.GET.copy()
        can_show_unloved_tip = True
        self.ordering = ""  # default ordering is set in LotFilter.__init__
        # I don't love having this in two places, but it seems necessary
        if self.request.GET.get("page"):
            del data["page"]  # required for pagination to work
        if "order" in data:
            self.ordering = data["order"]
        else:
            if "lot_order" in self.request.COOKIES:
                data["order"] = self.request.COOKIES["lot_order"]
                self.ordering = data["order"]
        if self.ordering == "unloved":
            can_show_unloved_tip = False
            if randint(1, 10) > 9:
                # we need a gentle nudge to remind people not to ALWAYS sort by least popular
                context["search_button_tooltip"] = "Sorting by least popular"
        if not context["auction"]:
            context["auction"] = self.auction
        else:
            self.auction = context["auction"]
        if self.auction:
            context["is_auction_admin"] = self.is_auction_admin
            if self.auction.minutes_to_end < 1440 and self.auction.minutes_to_end > 0 and can_show_unloved_tip:
                context["search_button_tooltip"] = "Try sorting by least popular to find deals!"
        if self.rewrite_url:
            if "auction" not in data and "q" not in data:
                context["rewrite_url"] = self.rewrite_url
        if "q" in data:
            if data["q"]:
                user = None
                if self.request.user.is_authenticated:
                    user = self.request.user
                SearchHistory.objects.create(user=user, search=data["q"], auction=self.auction)
        context["view"] = "all"
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingAuction=self.auction,
        )
        context["hide_google_login"] = True
        return context


class Invoices(ListView, LoginRequiredMixin):
    """Get all invoices for the current user"""

    model = Invoice
    template_name = "all_invoices.html"
    ordering = ["-date"]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            Q(auctiontos_user__user=self.request.user) | Q(auctiontos_user__email=self.request.user.email)
        ).order_by("-date")
        return qs

    # def get_context_data(self, **kwargs):
    #     context = super().get_context_data(**kwargs)
    #     return context


class InvoiceCreateView(View, AuctionViewMixin):
    """Create a new invoice for a user in an auction"""

    def get(self, request, *args, **kwargs):
        """Create invoice and redirect to invoice detail page"""
        # Get the auctiontos
        auctiontos_pk = self.kwargs.get("pk")
        try:
            auctiontos = AuctionTOS.objects.get(pk=auctiontos_pk)
        except AuctionTOS.DoesNotExist:
            messages.error(request, "User not found")
            return redirect("/")

        # Set auction for permission check
        self.auction = auctiontos.auction

        # Check if user is auction admin
        if not self.is_auction_admin:
            messages.error(request, "You don't have permission to create invoices for this auction")
            return redirect("/")

        # Check for existing invoices - get the oldest one (first created)
        existing_invoice = (
            Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).order_by("date").first()
        )

        if existing_invoice:
            # Check for and delete any duplicate invoices (keep the oldest)
            duplicate_invoices = Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).exclude(
                pk=existing_invoice.pk
            )

            duplicate_count = duplicate_invoices.count()
            if duplicate_count > 0:
                duplicate_invoices.delete()
                messages.info(request, f"Removed {duplicate_count} duplicate invoice(s)")

            # Redirect to existing invoice
            messages.info(request, "Invoice already exists for this user")
            return redirect(existing_invoice.get_absolute_url())

        # Create new invoice
        invoice = Invoice.objects.create(auctiontos_user=auctiontos, auction=auctiontos.auction)
        invoice.recalculate

        messages.success(request, f"Invoice created for {auctiontos.name}")
        return redirect(invoice.get_absolute_url())


# password protected in views.py
class InvoiceView(DetailView, FormMixin, AuctionViewMixin):
    """Show a single invoice"""

    template_name = "invoice.html"
    model = Invoice
    # form_class = InvoiceUpdateForm
    # expects opened or printed, this field will be set to true when the user the invoice is for opens it
    form_view = "opened"
    allow_non_admins = True
    authorized_by_default = False
    using_no_login_link = False

    def get_object(self):
        """"""
        try:
            return Invoice.objects.get(pk=self.kwargs.get(self.pk_url_kwarg))
        except:
            if self.request.user.is_authenticated:
                return Invoice.objects.filter(
                    auctiontos_user__user=self.request.user,
                    auction__slug=self.kwargs["slug"],
                ).first()
        return None

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        auth = self.authorized_by_default
        self.is_admin = False
        invoice = self.get_object()
        if not invoice:
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=self.kwargs["slug"]).first()
            if auction:
                messages.error(
                    request,
                    "You don't have an invoice for this auction yet.  Your invoice will be created once you buy or sell lots in this auction.",
                )
                return redirect(auction.get_absolute_url())
            raise Http404
        mark_invoice_viewed_by_user = False
        self.auction = invoice.auctiontos_user.auction
        if self.is_auction_admin:
            auth = True
            self.is_admin = True
        if self.auction.invoice_payment_instructions and invoice.status == "UNPAID":
            messages.info(request, self.auction.invoice_payment_instructions)
        if request.user.is_authenticated:
            if invoice.auctiontos_user.email == request.user.email or invoice.auctiontos_user.user == request.user:
                mark_invoice_viewed_by_user = True
                auth = True
        if not auth:
            messages.error(
                request,
                "Your account doesn't have permission to view this invoice. Are you signed in with the correct account?",
            )
            return redirect("/")
        if mark_invoice_viewed_by_user:
            setattr(invoice, self.form_view, True)  # this will set printed or opened as appropriate
            invoice.save()
        self.InvoiceAdjustmentFormSet = modelformset_factory(
            InvoiceAdjustment, extra=1, can_delete=True, form=InvoiceAdjustmentForm
        )
        self.queryset = invoice.adjustments
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        invoice = self.get_object()
        context = {}
        context["debug"] = settings.DEBUG
        context["using_no_login_link"] = self.using_no_login_link
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin
        context["invoice"] = invoice
        # light theme for some invoices to allow printing
        if "print" in self.request.GET.copy():
            context["base_template_name"] = "print.html"
            context["show_links"] = False
        else:
            context["base_template_name"] = "base.html"
            context["show_links"] = True
        context["location"] = invoice.location
        context["print_label_link"] = None
        if invoice.auction.is_online:
            context["print_label_link"] = reverse(
                "print_labels_by_bidder_number",
                kwargs={
                    "slug": invoice.auction.slug,
                    "bidder_number": invoice.auctiontos_user.bidder_number,
                },
            )
        context["is_auction_admin"] = self.is_auction_admin
        context["website_focus"] = settings.WEBSITE_FOCUS
        return context

    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        adjustment_formset = self.InvoiceAdjustmentFormSet(
            self.request.POST,
            form_kwargs={"invoice": self.get_object()},
            queryset=self.queryset,
        )
        if adjustment_formset.is_valid() and self.is_admin:
            adjustments = adjustment_formset.save(commit=False)
            for adjustment in adjustments:
                adjustment.invoice = self.get_object()
                adjustment.user = request.user
                adjustment.save()
            if adjustments:
                messages.success(self.request, "Invoice adjusted")
                self.auction.create_history(
                    applies_to="INVOICES",
                    action=f"Adjusted invoice for {self.get_object().auctiontos_user.name}",
                    user=request.user,
                )
            for form in adjustment_formset.deleted_forms:
                if form.instance.pk:
                    form.instance.delete()
            return redirect(reverse("invoice_by_pk", kwargs={"pk": self.get_object().pk}))
        context = self.get_context_data(**kwargs)
        context["formset"] = adjustment_formset
        context["helper"] = InvoiceAdjustmentFormSetHelper()
        return self.render_to_response(context)

    def get(self, request, *args, **kwargs):
        if self.get_object().unsold_lot_warning and self.is_auction_admin:
            messages.info(
                self.request,
                "This user still has unsold lots, make sure to sell all non-donation lots before marking this ready or paid.",
            )
        self.object = self.get_object()
        invoice_adjustment_formset = self.InvoiceAdjustmentFormSet(
            form_kwargs={"invoice": self.get_object()}, queryset=self.queryset
        )
        helper = InvoiceAdjustmentFormSetHelper()
        context = self.get_context_data(object=self.object)
        context["formset"] = invoice_adjustment_formset
        context["helper"] = helper
        # recaluclating slows things down,
        # I am not sure if it's a good idea to have it here or not
        self.object.recalculate
        return self.render_to_response(context)


class InvoiceNoLoginView(InvoiceView):
    """Enter a uuid, go to your invoice.  This bypasses the login checks"""

    # need a template with a popup
    authorized_by_default = True
    form_view = "opened"
    using_no_login_link = True

    def get_object(self):
        if not self.uuid:
            raise Http404
        invoice = Invoice.objects.filter(no_login_link=self.uuid).first()
        if invoice:
            return invoice
        else:
            raise Http404

    def dispatch(self, request, *args, **kwargs):
        self.uuid = kwargs.get("uuid", None)
        invoice = self.get_object()
        invoice.opened = True
        invoice.save()
        invoice.auctiontos_user.email_address_status = "VALID"
        invoice.auctiontos_user.save()
        return super().dispatch(request, *args, **kwargs)


class LotLabelView(TemplateView, WeasyTemplateResponseMixin, AuctionViewMixin):
    """View and print labels for an auction"""

    # these are defined in urls.py and used in get_object(), below
    bidder_number = None
    username = None
    # This one is the old one, it has some good stuff in it like QR code
    # template_name = "invoice_labels.html"
    template_name = "label_template.html"
    allow_non_admins = True
    filename = ""  # this will be automatically generated in dispatch

    def get_queryset(self):
        return self.tos.print_labels_qs

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs["slug"]).first()
        self.bidder_number = kwargs.pop("bidder_number", None)
        self.username = kwargs.pop("username", None)
        printing_for_self = False
        if self.bidder_number:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=self.bidder_number).first()
        if self.username:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, user__username=self.username).first()
        if not self.bidder_number and not self.username:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, user=request.user).first()
        if not self.tos:
            if self.is_auction_admin:
                # should never get here as long as admins are following links
                messages.error(request, "Unable to find any labels to print.")
            else:
                messages.error(
                    request,
                    "You haven't joined this auction yet.  You need to join this auction and add lots before you can print labels.",
                )
            return redirect(self.auction.get_absolute_url())
        checks_pass = False
        if self.is_auction_admin:
            checks_pass = True
            # if this is an admin printing someone else's lots, the file name should be the name of the person whose lots they're printing
            self.filename = self.tos.name or self.tos.bidder_number
        if request.user.is_authenticated:
            if request.user == self.tos.user:
                printing_for_self = True
                checks_pass = True
                # if this is a user printing their own lots, the file name should be the name of the auction
                self.filename = str(self.auction)
        if printing_for_self:
            if self.auction.is_online and not self.auction.closed:
                messages.error(
                    request,
                    "This is an online auction; you should print your labels after the auction ends, and before you exchange lots.",
                )
                return redirect(self.auction.get_absolute_url())
        if checks_pass and self.tos:
            if not self.get_queryset():
                if printing_for_self:
                    messages.error(
                        request,
                        "You don't have any lots with printable labels in this auction.",
                    )
                else:
                    if not self.auction.is_online:
                        messages.error(request, "There aren't any lots with printable labels")
                    else:
                        messages.error(
                            request,
                            "No lots with printable labels.  Only lots with a winner will have a label generated for them.",
                        )
                return redirect(self.auction.get_absolute_url())
            return super().dispatch(request, *args, **kwargs)
        else:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect("/")

    def get_pdf_filename(self):
        label_name = re.sub(r"[^a-zA-Z0-9]", "_", (self.filename or "labels").lower())
        return f"{label_name}.pdf"

    def get_context_data(self, **kwargs):
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        context = {}
        context["empty_labels"] = user_label_prefs.empty_labels
        context["print_border"] = user_label_prefs.print_border
        context["first_column_width"] = 0.62
        if user_label_prefs.preset == "sm":
            # Avery 5160 labels
            context["page_width"] = 8.5
            context["page_height"] = 11
            context["label_width"] = 2.55
            context["label_height"] = 0.99
            context["label_margin_right"] = 0.19
            context["label_margin_bottom"] = 0.01
            context["page_margin_top"] = 0.57
            context["page_margin_bottom"] = 0.1
            context["page_margin_left"] = 0.23
            context["page_margin_right"] = 0
            context["font_size"] = 10
            context["unit"] = "in"
        elif user_label_prefs.preset == "lg":
            # Avery 18262 labels
            context["page_width"] = 8.5
            context["page_height"] = 11
            context["label_width"] = 3.85
            context["label_height"] = 1.2
            context["label_margin_right"] = 0.25
            context["label_margin_bottom"] = 0.13
            context["page_margin_top"] = 0.88
            context["page_margin_bottom"] = 0.6
            context["page_margin_left"] = 0.3
            context["page_margin_right"] = 0
            context["font_size"] = 13
            context["first_column_width"] = 0.75
            context["unit"] = "in"
        elif user_label_prefs.preset == "thermal_sm":
            # thermal label printer 3x2
            context["page_width"] = 3
            context["page_height"] = 2
            context["label_width"] = 2.78
            context["label_height"] = 1.9
            context["label_margin_right"] = 0
            context["label_margin_bottom"] = 0
            context["page_margin_top"] = 0.04
            context["page_margin_bottom"] = 0.04
            context["page_margin_left"] = 0.16
            context["page_margin_right"] = 0.04
            context["font_size"] = 13
            context["first_column_width"] = 0.75
            context["unit"] = "in"
            # override the user selected setting for thermal labels
            context["print_border"] = False
        elif user_label_prefs.preset == "thermal_very_sm":
            # thermal label printer 30252 (1 1/8" x 3 1/2")
            context["page_width"] = 3.5
            context["page_height"] = 1.125
            context["label_width"] = 3.3
            context["label_height"] = 1.025
            context["label_margin_right"] = 0
            context["label_margin_bottom"] = 0
            context["page_margin_top"] = 0.04
            context["page_margin_bottom"] = 0.04
            context["page_margin_left"] = 0.16
            context["page_margin_right"] = 0.04
            context["font_size"] = 12
            context["first_column_width"] = 0.75
            context["unit"] = "in"
            context["print_border"] = False
        else:
            context.update(
                {f"{field.name}": getattr(user_label_prefs, field.name) for field in UserLabelPrefs._meta.get_fields()}
            )
        unit = 2.54 if context.get("unit") == "cm" else 1

        context["label_width"] = context.get("label_width") * unit
        context["label_height"] = context.get("label_height") * unit
        context["label_margin_right"] = context.get("label_margin_right") * unit
        context["label_margin_bottom"] = context.get("label_margin_bottom") * unit

        context["page_margin_top"] = context.get("page_margin_top") * unit
        context["page_margin_bottom"] = context.get("page_margin_bottom") * unit
        context["page_margin_left"] = context.get("page_margin_left") * unit
        context["page_margin_right"] = context.get("page_margin_right") * unit

        context["page_width"] = context.get("page_width") * unit
        context["page_height"] = context.get("page_height") * unit

        # Calculate the available space on the page
        available_width = context["page_width"] - context["page_margin_left"] - context["page_margin_right"]

        available_height = context["page_height"] - context["page_margin_top"] - context["page_margin_bottom"]

        # Page breaks don't work, see https://github.com/Kozea/WeasyPrint/issues/1967
        # manually calculating
        labels_per_row = int(available_width // (context["label_width"] + context["label_margin_right"]))
        labels_per_column = int(available_height // (context["label_height"] + context["label_margin_bottom"]))
        context["labels_per_page"] = labels_per_row * labels_per_column
        if context["labels_per_page"] == 0:
            messages.error(
                self.request,
                "Your lot label setting may be wrong. The label size is too large for the page size.  <a href='/printing'>Adjust your label settings</a>",
            )
            context["labels_per_page"] = 1

        labels = self.get_queryset()
        for label in labels:
            label.label_printed = True
            label.label_needs_reprinting = False
            label.save()

        # First column width is fixed at 0.63 for most labels and overridden for large and thermal
        # context['first_column_width'] = (context['label_width'] / 4)
        # let's keep the QR code a fixed size regardless of the label size
        # context['qr_code_height'] = min(context['first_column_width'], context['label_height'] / 2)
        context["qr_code_height"] = 0.5 * 72
        height_for_text = context["label_height"] * 72
        if "qr_code" in self.auction.label_print_fields:
            height_for_text = height_for_text - context["qr_code_height"]
        leading_ratio = 1.3
        line_height = context["font_size"] * leading_ratio
        lines_that_fit = int(height_for_text / line_height * 1.2)
        lines_that_fit -= 1  # for the lot number
        first_column_fields = [
            "quantity_label",
            "donation_label",
            "min_bid_label",
            "buy_now_label",
            "custom_checkbox_label",
            "i_bred_this_fish_label",
            "auction_date",
        ]
        first_column_fields_to_print = [
            field for field in first_column_fields if field in self.auction.label_print_fields
        ]
        # Split the fields: first column and overflow to second column
        first_column_fields = first_column_fields_to_print[:lines_that_fit]
        first_column_fields_to_put_in_second_column = first_column_fields_to_print[lines_that_fit:]

        for label in labels:
            label_first_column_fields = []
            label_second_column_fields = []
            for field in first_column_fields:
                label_first_column_fields.append(getattr(label, field))
            for field in first_column_fields_to_put_in_second_column:
                label_second_column_fields.append(getattr(label, field))
            label.first_column_fields = label_first_column_fields
            label.second_column_fields = label_second_column_fields
        context["labels"] = (["empty"] * context["empty_labels"]) + list(labels)
        context["text_area_width"] = context["label_width"] - context["first_column_width"]
        context["description_font_size"] = int(context["font_size"] * 0.7)
        context["first_column_font_size"] = int(context["font_size"] * 0.8)
        # for sizing
        context["all_borders"] = False
        return context

    def generate_qr_code(self, label, qr_code_width, qr_code_height):
        label_qr_code = qr_code.qrcode.maker.make_qr_code_image(
            label.qr_code,
            QRCodeOptions(
                size="T",
                border=1,
                error_correction="L",
                image_format="png",
            ),
        )
        image_stream = BytesIO(label_qr_code)
        return PImage(
            image_stream,
            width=qr_code_width,
            height=qr_code_height,
            lazy=0,
            hAlign="LEFT",
        )


class UnprintedLotLabelsView(LotLabelView):
    """Print lot labels, but only ones that haven't already been printed"""

    def get_queryset(self):
        return self.tos.unprinted_labels_qs


class SingleLotLabelView(LotLabelView):
    """Reprint labels for just one lot"""

    def get_queryset(self):
        return Lot.objects.filter(pk=self.lot.pk)

    def dispatch(self, request, *args, **kwargs):
        self.lot = get_object_or_404(Lot, pk=kwargs.pop("pk"), is_deleted=False)
        self.filename = f"label_{self.lot.lot_number_display}"
        if self.lot.auctiontos_seller:
            self.auction = self.lot.auctiontos_seller.auction
            auth = False
            if self.lot.auctiontos_seller.user and self.lot.auctiontos_seller.user.pk == request.user.pk:
                auth = True
            if not auth and not self.is_auction_admin:
                messages.error(
                    request,
                    "You can't print labels for other people's lots unless you are an admin",
                )
                return redirect("/")
        if not self.lot.auctiontos_seller:
            if self.lot.user and self.lot.user is not request.user:
                messages.error(request, "You can only print labels for your own lots")
                return redirect("/")
        # super() would try to find an auction
        return View.dispatch(self, request, *args, **kwargs)


@login_required
def getClubs(request):
    if request.method == "POST":
        species = request.POST["search"]
        result = Club.objects.filter(Q(name__icontains=species) | Q(abbreviation__icontains=species)).values(
            "id", "name", "abbreviation"
        )
        return JsonResponse(list(result), safe=False)


class BulkSetLotsWon(TemplateView, FormMixin, AuctionViewMixin):
    """Sell all lots based on the current filter to online high bidder"""

    template_name = "auctions/generic_admin_form.html"
    form_class = BulkSellLotsToOnlineHighBidder

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.original_query = request.GET.get("query", "")
        if not self.original_query:
            self.original_query = request.POST.get("query", "")
        self.query = unquote(self.original_query)
        self.queryset = LotAdminFilter.generic(self, self.auction.lots_qs, self.query)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            for lot in self.queryset:
                lot.sell_to_online_high_bidder
                if lot.auctiontos_winner:
                    lot.add_winner_message(self.request.user, lot.auctiontos_winner, lot.winning_price)
            self.auction.create_history(
                applies_to="LOTS",
                action=f"Sold {self.queryset.count()} lots to online high bidder",
                user=request.user,
            )
            return HttpResponse("<script>location.reload();</script>", status=200)
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tooltip = "This is intended to be used with silent auctions where people place bids on their phones, or with hybrid online auctions where some lots will be sold ahead of time.  It will sell any lots with online bids to the current online high bidder."
        if not self.query:
            tooltip += "<br><br><span class='text-warning'>You are about to set the winners of all lots.  This is a bad idea, you should click on cancel and then type in a filter first.</span>"
        else:
            tooltip += f"<br><br>You are about to set the winners of {self.queryset.count()} lots that match the filter <span class='text-warning'>{self.query}</span>"
        context["tooltip"] = tooltip
        context["modal_title"] = "Sell lots to online high bidders"
        return context

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["query"] = self.query
        form_kwargs["auction"] = self.auction
        form_kwargs["queryset"] = self.queryset
        return form_kwargs


class InvoiceBulkUpdateStatus(TemplateView, FormMixin, AuctionViewMixin):
    """Change invoice statuses in bulk"""

    template_name = "auctions/generic_admin_form.html"
    form_class = ChangeInvoiceStatusForm
    show_checkbox = False

    def get_queryset(self):
        return Invoice.objects.filter(auctiontos_user__auction=self.auction, status=self.old_invoice_status)

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.invoice_count = self.get_queryset().count()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["invoice_count"] = self.invoice_count
        if self.invoice_count:
            form_kwargs["show_checkbox"] = self.show_checkbox
        else:
            form_kwargs["show_checkbox"] = False
        form_kwargs["post_target_url"] = "auction_invoices_" + self.new_status_display.lower()
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context

    def post(self, request, *args, **kwargs):
        invoices = self.get_queryset()
        for invoice in invoices:
            invoice.status = self.new_invoice_status
            invoice.recalculate
            invoice.save()
        action = f"Set {invoices.count()} invoices from {self.old_status_display} to {self.new_status_display}"
        self.auction.create_history(
            applies_to="INVOICES",
            action=action,
            user=request.user,
        )
        return HttpResponse("<script>location.reload();</script>", status=200)


class MarkInvoicesReady(InvoiceBulkUpdateStatus):
    old_invoice_status = "DRAFT"
    new_invoice_status = "UNPAID"
    old_status_display = "draft"
    new_status_display = "ready"

    show_checkbox = True

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.auction.email_users_when_invoices_ready = form.cleaned_data.get(
                "send_invoice_ready_notification_emails"
            )
            self.auction.save()
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = (
            f"Changing the invoice status to ready will block users from bidding.  You should make any needed adjustments before setting invoices to ready.  This will change the status of {self.invoice_count} invoices, and allow you to export them to PayPal."
        )
        if not self.auction.closed and self.auction.is_online:
            context["unsold_lot_warning"] = "Don't set invoices ready yet!  This auction hasn't ended."
            if not self.auction.minutes_to_end:
                active_lot_count = self.auction.lots_qs.filter(active=True).count()
                context["unsold_lot_warning"] += (
                    f" There are still {active_lot_count} lots with last-minute bids on them"
                )
        if not self.auction.is_online:
            context["unsold_lot_warning"] = (
                "You usually don't need to use this.  Set people's invoices to paid one at a time, as people leave the auction."
            )
        if not self.invoice_count:
            context["modal_title"] = "No open invoices"
            context["tooltip"] = (
                "There aren't any open invoices in this auction.  Invoices are created automatically whenever a user buys or sells a lot."
            )
        else:
            context["modal_title"] = f"Set {self.invoice_count} open invoices to ready"
        return context


class MarkInvoicesPaid(InvoiceBulkUpdateStatus):
    old_invoice_status = "UNPAID"
    new_invoice_status = "PAID"
    old_status_display = "ready"
    new_status_display = "paid"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        unchanged_invoices = Invoice.objects.filter(auctiontos_user__auction=self.auction, status="DRAFT").count()
        context["unsold_lot_warning"] = (
            "You probably don't need this.  Instead, set invoices to paid one at a time, as users pay them."
        )
        context["tooltip"] = ""
        if unchanged_invoices:
            context["tooltip"] += (
                f" There are {unchanged_invoices} open invoices in this auction that will not be changed; set them ready first if you want to change them."
            )
        context["tooltip"] += f" This will set {self.invoice_count} ready invoices to paid."
        if not self.invoice_count:
            context["modal_title"] = "No ready invoices"
            if unchanged_invoices:
                context["tooltip"] = (
                    f"There's {unchanged_invoices} invoices that are still open.  You should set open invoices to ready before using this."
                )
        else:
            context["modal_title"] = f"Set {self.invoice_count} ready invoices to paid"
        return context


class LotRefundDialog(DetailView, FormMixin, AuctionViewMixin):
    model = Lot
    template_name = "auctions/generic_admin_form.html"
    form_class = LotRefundForm
    winner_invoice = None
    seller_invoice = None

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.lot.auction.create_history(
                applies_to="LOTS",
                action=f"Removed/refunded lot {self.lot.lot_number_display}",
                user=request.user,
            )
            refund = form.cleaned_data["partial_refund_percent"] or 0
            self.lot.refund(refund, request.user)
            banned = form.cleaned_data["banned"]
            self.lot.remove(banned, request.user)
            if self.seller_invoice:
                self.seller_invoice.recalculate
            if self.winner_invoice:
                self.winner_invoice.recalculate
            return HttpResponse("<script>location.reload();</script>", status=200)
        else:
            return self.form_invalid(form)

    def dispatch(self, request, *args, **kwargs):
        pk = self.kwargs.get(self.pk_url_kwarg)
        self.lot = get_object_or_404(
            Lot,
            is_deleted=False,
            auction__isnull=False,
            auctiontos_seller__isnull=False,
            pk=pk,
        )
        self.object = self.lot
        self.auction = self.lot.auction
        self.is_auction_admin
        self.seller_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_seller).first()
        if self.lot.auctiontos_winner:
            self.winner_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_winner).first()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["lot"] = self.lot
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["lot"] = self.lot
        if not self.lot.sold:
            context["tooltip"] = (
                "This lot has not sold, there's nothing to refund.  If there's a problem with this lot, remove it."
            )
        else:
            # if a refund has already been issued for this lot, we need to calculate how much is unpaid by temporarily removing it
            existing_refund = self.lot.partial_refund_percent
            if existing_refund:
                self.lot.partial_refund_percent = 0
                self.lot.save()
                full_seller_refund = add_price_info(Lot.objects.filter(pk=self.lot.pk)).first().your_cut
                # if we removed a refund before for math purposes, put it back now
                self.lot.partial_refund_percent = existing_refund
                self.lot.save()
                tooltip = "A refund has already been issued for this lot.  The refund percent is based on the original sale price.<br><br>"
            else:
                full_seller_refund = add_price_info(Lot.objects.filter(pk=self.lot.pk)).first().your_cut
                tooltip = "<small>This lot has sold.  If there's a problem with it, you should issue a refund which will show up on the seller and winner's invoices.</small><br><br>"
            if self.lot.winning_price:
                full_buyer_refund = self.lot.winning_price + (self.lot.winning_price * self.lot.auction.tax / 100)
            else:
                full_buyer_refund = 0
            if self.seller_invoice and self.seller_invoice.status == "DRAFT":
                tooltip += (
                    "Seller's invoice is open; $<span id='seller_refund'></span> will automatically be removed.<br>"
                )
            else:
                tooltip += "Seller's invoice is not open.  <span class='text-warning'>Collect $<span id='seller_refund'></span> from the seller</span><br>"
            if self.lot.winning_price and self.lot.auctiontos_winner:
                if self.winner_invoice and self.winner_invoice.status == "DRAFT":
                    tooltip += "Winner's invoice is open; refund of $<span id='buyer_refund'></span> will automatically be added<br>"
                else:
                    tooltip += "Winner's invoice is not open.  <span class='text-warning'>Refund the winner $<span id='buyer_refund'></span></span>"
                    if self.lot.auction.tax:
                        tooltip += f"<small> (includes {self.lot.auction.tax}% tax)</small><br>"
                    else:
                        tooltip += "<br>"
            tooltip += "<br><br>"
            extra_script = """
            <script>$('#id_partial_refund_percent').on('change keyup', function(){recalculate()});
            function recalculate(){
                var refund = $('#id_partial_refund_percent').val();var tax = """
            extra_script += f"{self.lot.auction.tax};var full_seller_refund = {full_seller_refund};var full_buyer_refund = {full_buyer_refund};"
            extra_script += """
                $('#seller_refund').text((full_seller_refund*refund/100).toFixed(2));
                if (full_buyer_refund) {
                    $('#buyer_refund').text((full_buyer_refund*refund/100).toFixed(2));
                };
            }
            $(document).ready( function(){recalculate()});
            </script>
            """
            context["extra_script"] = mark_safe(extra_script)
            context["tooltip"] = mark_safe(tooltip)
        context["modal_title"] = f"Remove or refund lot {self.lot.lot_number_display}"
        return context


class PayPalAPIMixin:
    """Couple API methods for PayPal stuff"""

    def _get_access_token(self):
        token_resp = requests.post(
            f"{settings.PAYPAL_API_BASE}/v1/oauth2/token",
            auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_SECRET),
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            data={"grant_type": "client_credentials"},
        )
        token_resp.raise_for_status()
        return token_resp.json()["access_token"]

    def _build_paypal_headers(self, merchant_id="", include_bn_code=True, token=None):
        """Common header builder for PayPal API calls"""
        headers = {
            "Authorization": f"Bearer {token or self._get_access_token()}",
            "Content-Type": "application/json",
        }
        if include_bn_code and getattr(settings, "PAYPAL_BN_CODE", None):
            headers["PayPal-Partner-Attribution-Id"] = settings.PAYPAL_BN_CODE
        if merchant_id:
            headers["PayPal-Auth-Assertion"] = self._build_auth_assertion(merchant_id)
        return headers

    def _build_auth_assertion(self, merchant_payer_id):
        """Build unsigned PayPal-Auth-Assertion JWT for acting on behalf of a merchant."""
        header = {"alg": "none"}
        payload = {
            "iss": settings.PAYPAL_CLIENT_ID,
            "payer_id": merchant_payer_id,  # obtained from partner referral flow
        }

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")

        unsigned_jwt = f"{b64(header)}.{b64(payload)}."
        return unsigned_jwt

    def _paypal_request(self, method, endpoint, *, json=None, params=None, merchant_id="", include_bn_code=True):
        """Single request helper used by get_from_paypal and post_to_paypal."""
        url = f"{settings.PAYPAL_API_BASE}/{str(endpoint).lstrip('/')}"
        headers = self._build_paypal_headers(merchant_id=merchant_id, include_bn_code=include_bn_code)
        self.paypal_debug = ""
        try:
            resp = requests.request(method, url, headers=headers, json=json, params=params)
            resp.raise_for_status()
            self.paypal_debug = resp.headers.get("Paypal-Debug-Id")
            return resp.json()
        except requests.HTTPError:
            safe_headers = dict(headers or {})
            if "Authorization" in safe_headers:
                safe_headers["Authorization"] = "Bearer ****"
            log_kwargs = {
                "method": method,
                "url": url,
                "status": resp.status_code,
                "Paypal-Debug-Id": self.paypal_debug,
                "req_headers": safe_headers,
                "req_params": params,
                "req_json": json,
                "resp_headers": dict(resp.headers),
                "resp_text": resp.text,
            }
            msg = (
                """PayPal API call failed: %(method)s %(url)s status=%(status)s debug_id=%(debug_id)s
                         req_headers=%(req_headers)s req_params=%(req_params)s req_json=%(req_json)s
                         resp_headers=%(resp_headers)s resp_text=%(resp_text)s""",
                log_kwargs,
            )
            logger.error(msg)
            raise Exception(msg)

    def post_to_paypal(self, endpoint, payload, include_bn_code=True):
        """POST JSON to a PayPal API endpoint and return parsed JSON."""
        return self._paypal_request("POST", endpoint, json=payload, include_bn_code=include_bn_code)

    def get_from_paypal(self, endpoint, include_bn_code=True, params=None):
        """GET from a PayPal API endpoint and return parsed JSON."""
        return self._paypal_request("GET", endpoint, params=params, include_bn_code=include_bn_code)

    def create_order(self, invoice):
        """Pass an invoice object and create an order for it.
        Returns an approval URL or None if the request failed"""
        currency = invoice.auction.created_by.userdata.currency

        items = []
        for lot in invoice.bought_lots_queryset:
            items.append(
                {
                    "name": f"{lot.lot_number_display} - {lot.lot_name}",
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{lot.winning_price:.2f}"},
                    "category": "PHYSICAL_GOODS",
                    "url": lot.full_lot_link,
                    "tax": {"currency_code": currency, "value": f"{lot.tax:.2f}"},
                }
            )

        target_total = (Decimal("0.00") - Decimal(invoice.net_after_payments)).quantize(Decimal("0.01"))
        # Base components from items
        item_total = Decimal(str(invoice.total_bought)).quantize(Decimal("0.01"))
        tax_total = Decimal(str(invoice.tax)).quantize(Decimal("0.01"))

        # Adjustment needed to make breakdown sum to target_total
        # target_total = item_total + tax_total + handling/shipping/insurance - discount
        # We’ll use:
        #  - discount for negative adjustments
        #  - an explicit “Adjustments” line item for positive adjustments (and include it in item_total)
        adjustment = (target_total - (item_total + tax_total)).quantize(Decimal("0.01"))

        discount_value = Decimal("0.00")
        if adjustment > 0:
            # Add an adjustment item and include in item_total
            items.append(
                {
                    "name": "Adjustments",
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{adjustment:.2f}"},
                    "category": "PHYSICAL_GOODS",
                }
            )
            item_total = (item_total + adjustment).quantize(Decimal("0.01"))
            adjustment = Decimal("0.00")
        elif adjustment < 0:
            # Use discount as a positive number
            discount_value = abs(adjustment)

        breakdown = {
            "item_total": {"currency_code": currency, "value": f"{item_total:.2f}"},
            "tax_total": {"currency_code": currency, "value": f"{tax_total:.2f}"},
        }
        if discount_value > 0:
            breakdown["discount"] = {"currency_code": currency, "value": f"{discount_value:.2f}"}

        purchase_unit = {
            "description": f"Bidder {invoice.auctiontos_user.bidder_number} in {invoice.auction.title}"[:127],
            "reference_id": str(invoice.pk),
            "amount": {
                "currency_code": currency,
                "value": f"{Decimal(-invoice.net_after_payments):.2f}",
                "breakdown": breakdown,
            },
            "items": items,
        }
        if invoice.soft_descriptor:
            purchase_unit["soft_descriptor"] = invoice.soft_descriptor[:22]
        if invoice.auction.paypal_information and invoice.auction.paypal_information != "admin":
            # if this is not set, payment will go to the platform account whose keys are in the .env
            purchase_unit["payee"] = {"merchant_id": invoice.auction.paypal_information}
            if settings.PAYPAL_PLATFORM_FEE and settings.PAYPAL_PLATFORM_FEE > 0:
                amt_value = Decimal(purchase_unit["amount"]["value"])
                fee_amount = (amt_value * settings.PAYPAL_PLATFORM_FEE / Decimal(100)).quantize(Decimal(0.01))
                if fee_amount > 0:
                    purchase_unit["payment_instruction"] = {
                        "platform_fees": [
                            {
                                "amount": {
                                    "currency_code": currency,
                                    "value": str(fee_amount),
                                }
                            }
                        ],
                        "disbursement_mode": "INSTANT",
                    }

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            # This code forces payment from the auctiontos.email and will fail if the user
            # doesn't have that email address as their primary PayPal address
            # "payment_source": {
            #     "paypal": {
            #         "email_address": invoice.auctiontos_user.email,
            #     },
            # },
            "application_context": {
                "brand_name": settings.NAVBAR_BRAND,
                "return_url": self.request.build_absolute_uri("/paypal/success/"),
                "cancel_url": self.request.build_absolute_uri(
                    reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link})
                ),
            },
        }

        order_data = self.post_to_paypal("v2/checkout/orders", payload)
        approval_url = None
        for link in order_data.get("links") or []:
            if link.get("rel") == "approve":
                approval_url = link.get("href")
                break
        self.order_id = order_data.get("id", "")
        if not approval_url:
            logger.error("PayPal order creation failed (platform): %s, debug_id %s", order_data, self.paypal_debug)
        return approval_url

    def handle_order(self, order_id):
        """Find an invoice associated with a given order ID, and create a payment for it.  Returns error string and invoice object"""
        order_data = self.post_to_paypal(f"v2/checkout/orders/{order_id}/capture", {})
        if order_data.get("status") != "COMPLETED":
            return (
                "PayPal payment has not yet been completed, please ask the auction administrator to manually confirm payment.",
                None,
            )

        # Extract identifiers
        purchase_unit = order_data.get("purchase_units", [{}])[0]
        invoice_id = purchase_unit.get("reference_id")

        # Load invoice
        invoice = Invoice.objects.filter(pk=invoice_id).first()
        if not invoice:
            return (
                "No invoice associated with this PayPal order, please ask the auction administrator to manually confirm payment.",
                None,
            )

        # Safely extract capture info (amount, currency, external id, payer info)
        capture = None
        try:
            capture = purchase_unit.get("payments", {}).get("captures", [None])[0]
        except Exception:
            capture = None

        amount_value = None
        currency = "USD"
        external_id = order_data.get("id")
        if capture:
            amount_value = capture.get("amount", {}).get("value")
            currency = capture.get("amount", {}).get("currency_code", currency)
            external_id = capture.get("id") or external_id

        # fallback to purchase_unit.amount
        if not amount_value:
            pu_amount = purchase_unit.get("amount", {}) or {}
            amount_value = pu_amount.get("value")
            currency = pu_amount.get("currency_code", currency)

        # payer info
        payer = order_data.get("payer", {}) or {}
        payer_name = None
        try:
            payer_name_parts = payer.get("name", {}) or {}
            given = payer_name_parts.get("given_name", "")
            surname = payer_name_parts.get("surname", "")
            payer_name = " ".join(p for p in (given, surname) if p).strip() or None
        except Exception:
            payer_name = None
        payer_email = payer.get("email_address")

        payer_address = None
        # prefer purchase_unit.shipping.address, fallback to payer.address
        shipping = purchase_unit.get("shipping", {}) or {}
        address_obj = (shipping.get("address") or {}) or (payer.get("address") or {})
        if address_obj:
            parts = []
            for k in (
                "address_line_1",
                "address_line_2",
                "admin_area_2",
                "admin_area_1",
                "postal_code",
                "country_code",
            ):
                v = address_obj.get(k)
                if v:
                    parts.append(v)
            if parts:
                payer_address = ", ".join(parts)

        if payer_email and not invoice.auctiontos_user.email:
            invoice.auctiontos_user.email = payer_email
            invoice.auctiontos_user.save()
            invoice.auction.create_history(
                applies_to="USERS",
                action=f"Added email {payer_email} to user {invoice.auctiontos_user.name} from PayPal payment",
                user=None,
            )
        if payer_address and payer_address != invoice.auctiontos_user.address:
            # always overwrite the address with the one from PayPal
            invoice.auction.create_history(
                applies_to="USERS",
                action=f"Updated address for user {invoice.auctiontos_user.name} from PayPal payment.  Old address {invoice.auctiontos_user.address}",
                user=None,
            )
            invoice.auctiontos_user.address = payer_address[:500]
            invoice.auctiontos_user.save()

            # also save the address site-wide
            if invoice.auctiontos_user.user and not invoice.auctiontos_user.user.userdata.address:
                invoice.auctiontos_user.user.userdata.address = payer_address[:500]
                invoice.auctiontos_user.user.userdata.save()

        amt = Decimal(str(amount_value)) if amount_value else Decimal("0.00")
        if not amt:
            return (
                "Unable to determine payment amount from PayPal order, please ask the auction administrator to manually confirm payment.",
                invoice,
            )
        payment, created = InvoicePayment.objects.update_or_create(
            external_id=external_id,
            defaults={
                "invoice": invoice,
                "amount": amt,
                "currency": currency,
                "payer_name": payer_name,
                "payer_email": payer_email,
                "payer_address": payer_address,
                "payment_method": "PayPal",
                "amount_available_to_refund": amt,
            },
        )
        invoice.recalculate
        if created:
            action = f"Payment received via PayPal for {invoice.auctiontos_user.name} ${payment.amount} ({payment.external_id})"
            invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
        # If the total owed is zero or less and invoice is DRAFT/UNPAID, mark PAID
        if invoice.net_after_payments >= 0 and invoice.status in ("DRAFT", "UNPAID"):
            invoice.status = "PAID"
            invoice.save()
            invoice.auction.create_history(
                applies_to="INVOICES",
                action=f"Invoice {invoice.auctiontos_user.name} automatically marked PAID after PayPal payment",
            )
            # I have given some thought to putting this in a model property instead
            # Putting it here only sends the message when an invoice is paid via PayPal
            channel_layer = channels.layers.get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"auctions_{invoice.auction.pk}",
                {"type": "invoice_paid", "pk": invoice.pk},
            )

        return None, invoice

    def can_refund_invoice(self, invoice, amount):
        """Check if we can refund the given amount on this invoice via PayPal."""
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method="PayPal")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )
        # if multiple payments have been made, we will only refund the largest one
        # I am too lazy to implement partial refunds across multiple payments right now
        total_available = payment.amount_available_to_refund if payment else Decimal("0.00")
        if total_available >= amount:
            return True
        return False

    def refund_invoice(self, invoice, amount):
        """Refund the given amount on this invoice via PayPal.
        Returns error or none on success"""
        if not self.can_refund_invoice(invoice, amount):
            return "Unable to automatically refund payment"
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method="PayPal")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )
        payload = {"amount": {"value": str(amount), "currency_code": str(payment.currency)}}
        result = self.post_to_paypal(f"v2/payments/captures/{payment.external_id}/refund", payload)
        if result.get("status") != "COMPLETED":
            logger.exception("PayPal refund failed: %s, debug_id: %s", result, self.paypal_debug)
            return "PayPal refund failed"
        # no database recording happens here, that goes through the webhook, see handle_refund()
        return None

    def handle_refund(self, refund_resource):
        """
        Process a refund webhook resource:
          - find the capture id (payment reference) from resource.links where rel == 'up'
          - find the InvoicePayment with external_id == capture_id
          - create a new InvoicePayment with negative amount and external_id == refund_id
        Returns: (invoice, refund_payment) or (None, None) on failure
        """
        refund_id = refund_resource.get("id")
        note_to_payer = refund_resource.get("note_to_payer") or refund_resource.get("note") or ""
        amount_obj = refund_resource.get("amount") or {}
        amount_value = amount_obj.get("value")
        currency = amount_obj.get("currency_code") or amount_obj.get("currency")

        # find capture id from the `up` link in the resource
        capture_id = None
        for item in refund_resource.get("links") or []:
            if item.get("rel") == "up" and item.get("href"):
                try:
                    capture_id = urlparse(item["href"]).path.rstrip("/").split("/")[-1]
                except Exception:
                    capture_id = None
                break

        if not capture_id:
            logger.warning("Refund resource missing capture 'up' link; resource=%s", refund_resource)
            return None, None

        # Find the original InvoicePayment by external_id == capture_id
        original_payment = InvoicePayment.objects.filter(external_id=capture_id).first()
        if not original_payment:
            logger.warning("No InvoicePayment found for capture id %s (refund %s)", capture_id, refund_id)
            return None, None
        invoice = original_payment.invoice

        # parse amount as Decimal and make negative
        try:
            refund_amt = Decimal(str(amount_value)) if amount_value is not None else None
        except Exception:
            logger.exception("Invalid refund amount in resource: %s", amount_value)
            refund_amt = None

        if refund_amt is None:
            logger.warning("Refund amount missing or invalid in refund resource %s", refund_id)
            return invoice, None

        refund_amt_signed = -abs(refund_amt)  # ensure negative

        original_payment.amount_available_to_refund = original_payment.amount_available_to_refund - abs(refund_amt)
        original_payment.save()
        # Create a new InvoicePayment record for the refund.
        refund_payment, created = InvoicePayment.objects.update_or_create(
            external_id=refund_id,
            defaults={
                "invoice": invoice,
                "amount": refund_amt_signed,
                "currency": currency or original_payment.currency,
                "payer_name": (refund_resource.get("payer") or {}).get("name") or None,
                "payer_email": (refund_resource.get("payer") or {}).get("email_address") or None,
                "payer_address": None,
                "payment_method": "PayPal Refund",
                "memo": note_to_payer[:500],
            },
        )

        invoice.recalculate
        action = f"Refund received via PayPal {refund_id} for capture {capture_id}: {refund_amt_signed} {currency}. Note: {note_to_payer}"
        invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
        return invoice, refund_payment


class PayPalConnectView(LoginRequiredMixin, PayPalAPIMixin, View):
    """Start the PayPal onboarding process for a seller"""

    def get(self, request):
        tracking_id = request.user.userdata.unsubscribe_link
        payload = {
            "tracking_id": tracking_id,
            "operations": [
                {
                    "operation": "API_INTEGRATION",
                    "api_integration_preference": {
                        "rest_api_integration": {
                            "integration_method": "PAYPAL",
                            "integration_type": "THIRD_PARTY",
                            "third_party_details": {"features": ["PAYMENT", "REFUND", "ACCESS_MERCHANT_INFORMATION"]},
                        }
                    },
                }
            ],
            "products": ["EXPRESS_CHECKOUT"],
            "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
            # take us to PayPalCallbackView when we are done
            "partner_config_override": {
                "return_url": request.build_absolute_uri("/paypal/onboard/success/"),
                # "return_url_description": f"Continue on {settings.NAVBAR_BRAND}",
            },
        }
        data = self.post_to_paypal("v2/customer/partner-referrals", payload)

        # Extract the action_url from the links list
        action_url = next((link["href"] for link in data.get("links", []) if link.get("rel") == "action_url"), None)
        if not action_url:
            logger.exception("PayPal onboarding failed %s, debug_id %s", data, self.paypal_debug)
            messages.error(request, "Unable to start PayPal onboarding process, please try again later.")
            return redirect("/")
        # Redirect seller to PayPal to complete onboarding
        return redirect(action_url)


class PayPalCallbackView(LoginRequiredMixin, PayPalAPIMixin, View):
    """After onboarding, PayPal redirects here"""

    def get_success_url(self):
        success_url = "/"
        if self.request.user.userdata.last_auction_created:
            success_url = self.request.user.userdata.last_auction_created.get_absolute_url()
        if self.error:
            messages.error(self.request, self.error)
        else:
            messages.success(
                self.request,
                "You're all set - PayPal account linked!  Your users will see a PayPal button on invoices.",
            )
            success_url += "?enable_online_payments=True"
        return redirect(success_url)

    def get(self, request):
        self.error = None
        self.valid = False
        tracking_id = request.GET.get("merchantId")
        merchant_id = request.GET.get("merchantIdInPayPal")
        partner_merchant_id = settings.PARTNER_MERCHANT_ID

        if not tracking_id or not merchant_id:
            self.error = "Missing ID from PayPal callback"
            return self.get_success_url()

        data = UserData.objects.filter(unsubscribe_link=tracking_id).first()
        if not data:
            self.error = "Could not find user for PayPal onboarding"
            return self.get_success_url()
        else:
            user = data.user

        # Fetch referral info from PayPal
        merchant_info = self.get_from_paypal(
            f"v1/customer/partners/{partner_merchant_id}/merchant-integrations/{merchant_id}"
        )
        # Integration checklist: ensure payments_receivable, email confirmed and oauth_third_party present
        currency = merchant_info.get("primary_currency", "USD")
        if not merchant_info.get("payments_receivable"):
            self.error = "Attention: You currently cannot receive payments due to restriction on your PayPal account. Please resolve any issues with PayPal and re-link your account here."
            return self.get_success_url()
        if not merchant_info.get("primary_email_confirmed"):
            self.error = "Attention: Please confirm your email address on https://www.paypal.com/businessprofile/settings in order to receive payments! You currently cannot receive payments.  Re-link your account here when finished."
            return self.get_success_url()
        oauth_integrations = merchant_info.get("oauth_integrations") or []
        oauth_ok = any(
            oi.get("integration_type") == "OAUTH_THIRD_PARTY" and oi.get("oauth_third_party")
            for oi in oauth_integrations
        )
        if not oauth_ok:
            self.error = "It doesn't look like you've granted us the third-party oauth integrations permissions. Please re-link your PayPal account and be sure to accept all requested permissions."
            return self.get_success_url()

        # update model
        seller, _ = PayPalSeller.objects.get_or_create(user=user)
        seller.paypal_merchant_id = merchant_id
        seller.payer_email = merchant_info.get("primary_email") or seller.payer_email
        seller.currency = currency
        seller.save()
        return self.get_success_url()


class CreatePayPalOrderView(PayPalAPIMixin, View):
    """Create a PayPal order for an invoice and redirect to PayPal checkout"""

    def dispatch(self, request, *args, **kwargs):
        self.invoice = get_object_or_404(Invoice, no_login_link=kwargs.pop("uuid"))
        error = self.invoice.reason_for_payment_not_available
        if not self.invoice.show_payment_button:
            error = "PayPal payments are not possible in this auction"
        if error:
            messages.error(request, error)
            return redirect(reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link}))
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """Create the order"""
        approval_url = self.create_order(self.invoice)
        if not approval_url:
            messages.error(
                request, "Payment provider rejected the order. Contact the auction administrator to confirm payment."
            )
            return redirect(self.invoice.get_absolute_url())
        return redirect(approval_url)


class PayPalSuccessView(PayPalAPIMixin, View):
    """Capture PayPal order after approval and mark payment complete"""

    def get(self, request, *args, **kwargs):
        order_id = request.GET.get("token")
        error, invoice = self.handle_order(order_id)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Payment completed successfully. Thank you!")
        if invoice:
            return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))
        return redirect("/")


class PayPalInfoView(TemplateView):
    template_name = "auctions/paypal_seller.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context["seller"] = PayPalSeller.objects.filter(user=self.request.user).first()
            context["auction"] = self.request.user.userdata.last_auction_created
        else:
            context["seller"] = None
            context["auction"] = None
        return context


class PayPalSellerDeleteView(LoginRequiredMixin, DeleteView):
    template_name = "auctions/paypal_seller_confirm_delete.html"
    model = PayPalSeller

    def get_object(self, queryset=None):
        return get_object_or_404(PayPalSeller, user=self.request.user)

    def get_success_url(self):
        return reverse("paypal_seller")


class UserView(DetailView):
    """View information about a single user"""

    template_name = "user.html"
    model = User

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["data"], created = UserData.objects.get_or_create(
            user=self.object,
            defaults={},
        )
        try:
            context["banned"] = UserBan.objects.get(user=self.request.user.pk, banned_user=self.object.pk)
        except:
            context["banned"] = False
        context["seller_feedback"] = (
            context["data"].my_lots_qs.exclude(feedback_text__isnull=True).order_by("-date_posted")
        )
        context["buyer_feedback"] = (
            context["data"].my_won_lots_qs.exclude(winner_feedback_text__isnull=True).order_by("-date_posted")
        )
        return context


class UserByName(UserView):
    """/user/username storefront view"""

    def dispatch(self, request, *args, **kwargs):
        self.username = kwargs["slug"]
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, *args, **kwargs):
        try:
            return User.objects.get(username=unquote(self.username))
        except:
            pass
        # try:
        #     return User.objects.get(pk=self.username)
        # except:
        #     pass
        raise Http404


class UsernameUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_username.html"
    model = User
    success_message = "Username updated"
    form_class = ChangeUsernameForm

    def get_object(self, *args, **kwargs):
        try:
            return User.objects.get(pk=self.request.user.pk)
        except:
            raise Http404

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().pk == request.user.pk:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            # "/users/" + str(self.kwargs['pk'])
            data["next"] = reverse("account")
        return data["next"]


class UserLabelPrefsView(UpdateView, SuccessMessageMixin):
    template_name = "user_labels.html"
    model = UserLabelPrefs
    success_message = "Printing preferences updated"
    form_class = UserLabelPrefsForm
    user_pk = None

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
        return data["next"]

    def get_object(self, *args, **kwargs):
        label_prefs, created = UserLabelPrefs.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        return label_prefs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "printing"
        userData, created = UserData.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        context["last_auction_used"] = userData.last_auction_used
        context["last_admin_auction"] = (
            Auction.objects.filter(
                Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True),
                is_deleted=False,
            )
            .order_by("-date_start")
            .first()
        )
        return context


class UserPreferencesUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_preferences.html"
    model = UserData
    success_message = "User preferences updated"
    form_class = ChangeUserPreferencesForm
    user_pk = None

    def dispatch(self, request, *args, **kwargs):
        # self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
            # data['next'] = "/users/" + str(self.kwargs['pk'])
        return data["next"]

    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk)  # get the hack

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "preferences"
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class UserLocationUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_location.html"
    model = UserData
    success_message = "Contact info updated"
    form_class = UserLocation
    # such a hack...UserData and User do not have the same pks.
    # This means that if we go to /users/1/edit, we'll get the wrong UserData
    # The fix is to have a self.user_pk, which is set in dispatch and called in get_object
    user_pk = None

    def dispatch(self, request, *args, **kwargs):
        # self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
            # "/users/" + str(self.kwargs['pk'])
        return data["next"]

    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk)  # get the hack

    def get_initial(self):
        user = User.objects.get(pk=self.get_object().user.pk)
        return {"first_name": user.first_name, "last_name": user.last_name}

    def form_valid(self, form):
        userData = form.save(commit=False)
        user = User.objects.get(pk=self.get_object().user.pk)
        user.first_name = form.cleaned_data["first_name"]
        user.last_name = form.cleaned_data["last_name"]
        user.save()
        userData.last_activity = timezone.now()
        userData.save()
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "contact"
        return context


class UserChartView(View):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            user = self.kwargs.get("pk", None)
            allBids = (
                Bid.objects.exclude(is_deleted=True)
                .select_related("lot_number__species_category")
                .filter(user=user, lot_number__species_category__isnull=False)
            )
            pageViews = PageView.objects.select_related("lot_number__species_category").filter(
                user=user, lot_number__species_category__isnull=False
            )
            # This is extremely inefficient
            # Almost all of it could be done in SQL with a more complex join and a count
            # However, I keep changing attributes (views, view duration, bids) and sorting here
            # This code is also only run for admins (and async of page load), so the server load is pretty low

            categories = {}
            for item in allBids:
                category = str(item.lot_number.species_category)
                try:
                    categories[category]["bids"] += 1
                except:
                    categories[category] = {"bids": 1, "views": 0}
            for item in pageViews:
                category = str(item.lot_number.species_category)
                try:
                    categories[category]["views"] += 1
                except:
                    # brand new category
                    categories[category] = {"bids": 0, "views": 1}
            # sort the result
            sortedCategories = sorted(categories, key=lambda t: -categories[t]["views"])
            # sortedCategories = sorted(categories, key=lambda t: -categories[t]['bids'] )
            # format for chart.js
            labels = []
            bids = []
            views = []
            for item in sortedCategories:
                labels.append(item)
                bids.append(categories[item]["bids"])
                views.append(categories[item]["views"])
            return JsonResponse(data={"labels": labels, "bids": bids, "views": views})
        messages.error(request, "Your account doesn't have permission to view this page.")
        return redirect("/")


class LotChartView(View):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            lot_number = self.kwargs.get("pk", None)
            queryset = (
                PageView.objects.filter(lot_number=lot_number)
                .exclude(user_id__isnull=True)
                .order_by("-total_time")
                .values()[:20]
            )
            labels = []
            data = []
            for entry in queryset:
                labels.append(str(User.objects.get(pk=entry["user_id"])))
                data.append(int(entry["total_time"]))

            return JsonResponse(
                data={
                    "labels": labels,
                    "data": data,
                }
            )
        messages.error(request, "Your account doesn't have permission to view this page.")
        return redirect("/")


class AdminErrorPage(AdminOnlyViewMixin, TemplateView):
    """A sanity check to make sure the 500 error emails are working as they should be"""

    template_name = "dashboard.html"

    def get(self, request, *args, **kwargs):
        return 1 / 0


class AdminTraffic(AdminOnlyViewMixin, TemplateView):
    """Popular pages and user last activity"""

    template_name = "dashboard_traffic.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days_param = self.request.GET.get("days", 7)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 7
        context["days"] = days
        timeframe = timezone.now() - timedelta(days=days)

        # this next section is the user last activity
        # this is very old code, and it would probably be far better to use PageViews
        # for logged in and not logged in users instead to show overall traffic over time
        qs = UserData.objects.filter(user__is_active=True)
        activity = (
            qs.filter(last_activity__gte=timezone.now() - timedelta(days=60))
            .annotate(day=TruncDay("last_activity"))
            .order_by("-day")
            .values("day")
            .annotate(c=Count("pk"))
            .values("day", "c")
        )
        context["last_activity_days"] = []
        context["last_activity_count"] = []
        for day in activity:
            context["last_activity_days"].append((timezone.now() - day["day"]).days)
            context["last_activity_count"].append(day["c"])
        # popular page stuff follows
        page_view_qs = PageView.objects.filter(date_start__gte=timeframe)
        # may want to move this to a get param at some point
        number_of_popular_pages_to_show = 50
        context["page_views"] = (
            page_view_qs.values("url", "title")
            .annotate(
                # there's no way this code is right,
                # it dates back to when view counter was being used, and that field is no longer filled out
                # total_view_count=Sum("counter") + F("unique_view_count"),
                view_count=Count("url"),
            )
            .order_by("-view_count")[:number_of_popular_pages_to_show]
        )
        # heat  map stuff follows
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        context["pageviews"] = PageView.objects.exclude(latitude=0).filter(date_start__gte=timeframe)
        return context


class AdminTrafficJSON(AdminOnlyViewMixin, BaseLineChartView):
    """JSON userdata"""

    def dispatch(self, request, *args, **kwargs):
        days_param = self.request.GET.get("days", 7)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 7
        self.bins = days
        return super().dispatch(request, *args, **kwargs)

    def get_labels(self):
        return [(f"{i - 1} days ago") for i in range(self.bins, 0, -1)][::-1]

    def get_providers(self):
        return ["Views"]

    def get_data(self):
        timeframe = timezone.now() - timedelta(days=self.bins)
        views = PageView.objects.filter(date_start__gte=timeframe).order_by("-date_start")

        # what follows is a delightful reminder of how important a consistent naming scheme is
        return [
            bin_data(views, "date_start", self.bins, timeframe, timezone.now())[::-1],
        ]


class AdminReferrers(AdminOnlyViewMixin, TemplateView):
    """Where's your traffic coming from?"""

    template_name = "dashboard_referrers.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days_param = self.request.GET.get("days", 7)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 7
        context["days"] = days
        timeframe = timezone.now() - timedelta(days=days)
        page_view_qs = PageView.objects.filter(date_end__gte=timeframe)
        referrers = (
            page_view_qs.exclude(referrer__isnull=True)
            .exclude(referrer="")
            .exclude(referrer__startswith="http://127.0.0.1:8000")
        )
        # comment out next line to include internal referrers
        referrers = referrers.exclude(referrer__startswith="https://" + Site.objects.get_current().domain)
        referrers = referrers.exclude(referrer__startswith="" + Site.objects.get_current().domain)
        context["referrers"] = (
            referrers.values("referrer", "url", "title")
            .annotate(
                total_clicks=Count("referrer"),
                # total_view_count=Sum('counter') + F('unique_view_count')
            )
            .order_by("-total_clicks")[:100]
        )
        return context


class AdminDashboard(AdminOnlyViewMixin, TemplateView):
    """Currently active users overview"""

    template_name = "dashboard.html"

    def unique_page_views(self, minutes, view_type="anon"):
        timeframe = timezone.now() - timezone.timedelta(minutes=minutes)
        base_qs = PageView.objects.filter(date_start__gte=timeframe)
        if view_type == "logged_in":
            # return base_qs.filter(user__isnull=False).aggregate(unique_views=Count("user", distinct=True))[
            #    "unique_views"
            # ]
            return base_qs.filter(user__isnull=False).values("user").distinct().count()
        if view_type == "anon":
            # return base_qs.filter(user__isnull=True, session_id__isnull=False).aggregate(
            #    unique_views=Count("session_id", distinct=True)
            # )["unique_views"]
            # this one is the same as above.  Both use session which is somehow getting clobbered.  Maybe cloudflare.
            # return (
            #     base_qs.filter(user__isnull=True, session_id__isnull=False)
            #     .exclude(session_id="")
            #     .values("session_id")
            #     .distinct()
            #     .count()
            # )
            return (
                base_qs.filter(user__isnull=True)
                .exclude(ip_address="", ip_address__isnull=True)
                .values("ip_address")
                .distinct()
                .count()
            )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = UserData.objects.filter(user__is_active=True)
        context["total_users"] = qs.count()

        context["verified_emails_count"] = User.objects.filter(emailaddress__verified=True).distinct().count()
        context["users_with_email_no_account_count"] = (
            AuctionTOS.objects.filter(user__isnull=True, email__isnull=False).values("email").distinct().count()
        )

        # context["unsubscribes"] = qs.filter(has_unsubscribed=True).count()
        # context["anonymous"] = (
        #     qs.filter(username_visible=False).exclude(user__username__icontains="@").count()
        # )  # inactive users with an email as their username were set to anonymous Nov 2023
        # context["light_theme"] = qs.filter(use_dark_theme=False).count()
        # context["hide_ads"] = qs.filter(show_ads=False).count()
        # context["no_club_auction"] = qs.filter(user__auctiontos__isnull=True).distinct().count()
        # context["no_participate"] = (
        #     qs.exclude(Q(user__winner__isnull=False) | Q(user__lot__isnull=False)).distinct().count()
        # )
        # context["using_watch"] = qs.exclude(user__watch__isnull=True).distinct().count()
        # context["using_buy_now"] = qs.filter(user__winner__buy_now_used=True).count()
        # context["using_proxy_bidding"] = qs.filter(has_used_proxy_bidding=True).count()
        # context["buyers"] = qs.filter(user__winner__isnull=False).distinct().count()
        # context["sellers"] = qs.filter(user__lot__isnull=False).distinct().count()
        # context["has_location"] = qs.exclude(latitude=0).count()
        # context["new_lots_last_7_days"] = (
        #     Lot.objects.exclude(is_deleted=True).filter(date_posted__gte=timezone.now() - timedelta(days=7)).count()
        # )
        # context["new_lots_last_30_days"] = (
        #     Lot.objects.exclude(is_deleted=True).filter(date_posted__gte=timezone.now() - timedelta(days=30)).count()
        # )
        # context["bidders_last_30_days"] = (
        #     qs.filter(user__bid__last_bid_time__gte=timezone.now() - timedelta(days=30))
        #     .values("user")
        #     .distinct()
        #     .count()
        # )
        # context["feedback_last_30_days"] = (
        #     Lot.objects.exclude(feedback_rating=0).filter(date_posted__gte=timezone.now() - timedelta(days=30)).count()
        # )
        # context["users_with_search_history"] = User.objects.filter(searchhistory__isnull=False).distinct().count()
        logged_in_5m = self.unique_page_views(5, "logged_in")
        anon_5m = self.unique_page_views(5, "anon")
        logged_in_30m = self.unique_page_views(30, "logged_in")
        anon_30m = self.unique_page_views(30, "anon")
        logged_in_1d = self.unique_page_views(24 * 60, "logged_in")
        anon_1d = self.unique_page_views(24 * 60, "anon")
        context["day_views_count"] = logged_in_1d + anon_1d
        context["5m_views_count"] = logged_in_5m + anon_5m
        context["30m_views_count"] = logged_in_30m + anon_30m
        if logged_in_1d + anon_1d == 0:
            anon_1d = 1  # so it's a hack to avoid /0, whatever
        context["day_views_count_percent_with_account"] = int(logged_in_1d / (logged_in_1d + anon_1d) * 100)
        timeframe = timezone.now() - timezone.timedelta(minutes=30)
        # check to make sure no auctions are happening before applying server updates
        context["in_person_lots_ended"] = Lot.objects.filter(
            is_deleted=False, auction__is_online=False, date_end__gte=timeframe, date_end__lte=timezone.now()
        ).count()
        timeframe = timezone.now() + timezone.timedelta(minutes=120)
        context["online_auction_lots_ending"] = Lot.objects.filter(
            is_deleted=False, date_end__lte=timeframe, date_end__gte=timezone.now()
        ).count()
        # users_with_printed_labels = User.objects.filter(lot__label_printed=True).distinct()
        # context["users_with_printed_labels"] = users_with_printed_labels.count()
        # context["preset_counts"] = (
        #     UserLabelPrefs.objects.filter(user__in=users_with_printed_labels)
        #     .values("preset")
        #     .annotate(count=Count("user"))
        #     .order_by("-count")
        # )
        return context


class UserMap(TemplateView):
    template_name = "user_map.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.request.user.is_superuser:
            messages.error(self.request, "Only admins can view the user map")
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        data = self.request.GET.copy()
        try:
            view = data["view"]
        except:
            view = None
        try:
            filter1 = data["filter"]
        except:
            filter1 = None
        # view_qs = PageView.objects.exclude(latitude=0)
        qs = User.objects.filter(userdata__latitude__isnull=False, is_active=True).annotate(
            lots_sold=Count("lot"), lots_bought=Count("winner")
        )
        if view == "club" and filter1:
            # Users from a club
            qs = qs.filter(userdata__club__name=filter1)
        elif view == "buyers_and_sellers" and filter1:
            # Users who sold and bought
            qs = qs.filter(lots_sold__gte=filter1, lots_bought__gte=filter1)
        elif view == "volume" and filter1:
            # users by top volume_percentile
            qs = qs.filter(userdata__volume_percentile__lte=filter1)
        elif view == "recent" and filter1:
            # view_qs = view_qs.filter(date_start__gte=timezone.now() - timedelta(hours=int(filter1)))
            qs = qs.filter(userdata__last_activity__gte=timezone.now() - timedelta(hours=int(filter1)))
        context["users"] = qs
        # context["pageviews"] = view_qs
        return context


class ClubMap(AdminEmailMixin, TemplateView):
    template_name = "clubs.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_CLUB_FINDER:
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        context["clubs"] = Club.objects.filter(active=True, latitude__isnull=False)
        context["location_message"] = "Set your location to see clubs near you"
        try:
            context["latitude"] = self.request.COOKIES["latitude"]
            context["longitude"] = self.request.COOKIES["longitude"]
        except:
            pass
        context["hide_google_login"] = True
        return context


class UserAgreement(TemplateView):
    template_name = "tos_wrapper.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        tos_path = Path(settings.BASE_DIR / "tos.html")
        if Path.exists(tos_path):
            with Path.open(tos_path) as file:
                context["tos_content"] = file.read()
        else:
            msg = "No TOS found.  You must place a file called tos.html in the root project directory (next to the .env file)"
            raise ImproperlyConfigured(msg)
        return context


class IgnoreCategoriesView(TemplateView):
    template_name = "ignore_categories.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "ignore"
        return context


class CreateUserIgnoreCategory(View):
    """Add category with given pk to ignore list"""

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to ignore categories")
            return redirect("/")
        pk = self.kwargs.get("pk", None)
        category = Category.objects.get(pk=pk)
        result, created = UserIgnoreCategory.objects.update_or_create(category=category, user=self.request.user)
        return JsonResponse(data={"pk": result.pk})


class DeleteUserIgnoreCategory(View):
    """Allow users to see lots in a given category again."""

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to show categories")
            return redirect("/")
        pk = self.kwargs.get("pk", None)
        category = Category.objects.get(pk=pk)
        try:
            exists = UserIgnoreCategory.objects.get(category=category, user=self.request.user)
            exists.delete()
            return JsonResponse(data={"result": "deleted"})
        except Exception as e:
            return JsonResponse(data={"error": str(e)})


class GetUserIgnoreCategory(View):
    """Get a list of all user ignore categories for the request user"""

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to use this feature")
            return redirect("/")
        categories = Category.objects.all().order_by("name")
        results = []
        for category in categories:
            item = {
                "id": category.pk,
                "text": category.name,
            }
            try:
                UserIgnoreCategory.objects.get(user=self.request.user, category=category.pk)
                item["selected"] = True
            except:
                pass
            results.append(item)
        return JsonResponse({"results": results}, safe=False)


class BlogPostView(DetailView):
    """Render a blog post"""

    model = BlogPost
    template_name = "blog_post.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        blogpost = self.get_object()
        # this is to allow the chart# syntax
        context["formatted_contents"] = re.sub(r"chart\d", r"<canvas id=\g<0>></canvas>", blogpost.body_rendered)
        return context


class UnsubscribeView(TemplateView):
    """
    Match a UUID in the URL to a UserData, and unsubscribe that user
    """

    template_name = "unsubscribe.html"

    def get_context_data(self, **kwargs):
        userData = UserData.objects.filter(unsubscribe_link=kwargs["slug"]).first()
        if not userData:
            raise Http404
        else:
            userData.unsubscribe_from_all
        context = super().get_context_data(**kwargs)
        return context


class AuctionChartView(View, AuctionStatsPermissionsMixin):
    """GET methods for generating auction charts"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)


class AuctionFunnelChartData(AuctionChartView):
    """
    Inverted funnel chart showing user participation
    """

    def get(self, *args, **kwargs):
        all_views = PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
        anonymous_views = all_views.values("session_id").annotate(session_count=Count("session_id")).count()
        user_views = all_views.values("user").annotate(user_count=Count("user")).count()
        total_views = anonymous_views + user_views
        total_bidders = User.objects.filter(bid__lot_number__auction=self.auction).annotate(dcount=Count("id")).count()
        total_winners = User.objects.filter(winner__auction=self.auction).annotate(dcount=Count("id")).count()
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
        for lot in lots:
            if not lot.winning_price:
                data[0] += 1
            else:
                bids = len(Bid.objects.exclude(is_deleted=True).filter(lot_number=lot))
                if bids > 6:
                    bids = 6
                else:
                    data[bids] += 1
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
            for category in categories[: self.number_of_categories_to_show]:
                labels.append(str(category))
                thisViews = PageView.objects.filter(
                    lot_number__auction=self.auction,
                    lot_number__species_category=category,
                ).count()
                thisBids = (
                    Bid.objects.exclude(is_deleted=True)
                    .filter(
                        lot_number__auction=self.auction,
                        lot_number__species_category=category,
                    )
                    .count()
                )
                thisVolume = (
                    Lot.objects.exclude(is_deleted=True)
                    .filter(auction=self.auction, species_category=category)
                    .aggregate(Sum("winning_price"))["winning_price__sum"]
                )
                percentOfLots = self.process_stat(category.num_lots, lot_count)
                percentOfViews = self.process_stat(thisViews, allViews)
                percentOfBids = self.process_stat(thisBids, allBids)
                percentOfVolume = self.process_stat(thisVolume, allVolume)
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
            return redirect("/")
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
        if self.dates_messed_with:
            return [(f"{i - 1} days ago") for i in range(self.bins, 0, -1)]
        before = [(f"{i} days before") for i in range(self.days_before, 0, -1)]
        after = [(f"{i} days after") for i in range(1, self.days_after)]
        midpoint = "start"
        if self.auction.is_online:
            midpoint = "end"
        return before + [midpoint] + after

    def get_providers(self):
        return ["Views", "Joins", "New lots", "Searches", "Bids", "Watches"]

    def get_data(self):
        """Wonder if these qs should be properties of the Auction model...
        Might add invoice views here, but it would require updating the pageview model to have an invoice field similar to how auction and lot currently work"""

        views = PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
        joins = AuctionTOS.objects.filter(auction=self.auction)
        new_lots = Lot.objects.filter(auction=self.auction)
        searches = SearchHistory.objects.filter(auction=self.auction)
        bids = LotHistory.objects.filter(lot__auction=self.auction, changed_price=True)
        watches = Watch.objects.filter(lot_number__auction=self.auction)

        # what follows is a delightful reminder of how important a consistent naming scheme is
        return [
            bin_data(views, "date_start", self.bins, self.date_start, self.date_end),
            bin_data(joins, "createdon", self.bins, self.date_start, self.date_end),
            bin_data(new_lots, "date_posted", self.bins, self.date_start, self.date_end),
            bin_data(searches, "createdon", self.bins, self.date_start, self.date_end),
            bin_data(bids, "timestamp", self.bins, self.date_start, self.date_end),
            bin_data(watches, "createdon", self.bins, self.date_start, self.date_end),
        ]


class AuctionStatsAttritionJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    ignore_percent = 10

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect("/")
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
        return []

    def get_providers(self):
        """Return names of datasets."""
        return ["Lots"]

    def get_data(self):
        data = [
            {
                "x": (lot.date_end - self.end_date).total_seconds() // 60,  # minutes after auction start
                # 'x': lot.date_end.timestamp() * 1000, # this one gives js timestamps and would need moment.js to convert to date
                "y": lot.winning_price,
            }
            for lot in self.lots
        ]
        # Prepare the data structure for Chart.js
        # data = {
        #     'data': data
        # }
        # return data
        return [data]


class AuctionStatsBarChartJSONView(AuctionViewMixin, BaseColumnsHighChartsView):
    """This is needed because of https://github.com/peopledoc/django-chartjs/issues/56"""

    # allow_non_admins = True

    # def dispatch(self, request, *args, **kwargs):
    #     self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
    #     if not self.is_auction_admin:
    #         return redirect("/")
    #     result = super().dispatch(request, *args, **kwargs)
    #     return result

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
    def get_labels(self):
        labels = [(f"${i + 1}-{i + 2}") for i in range(0, 37, 2)]
        return ["Not sold"] + labels + ["$40+"]

    def get_providers(self):
        return ["Number of lots"]

    def get_data(self):
        sold_lots = self.auction.lots_qs.filter(winning_price__isnull=False)
        histogram = bin_data(
            sold_lots,
            "winning_price",
            number_of_bins=19,
            start_bin=1,
            end_bin=39,
            add_column_for_high_overflow=True,
        )
        return [[self.auction.total_unsold_lots] + histogram]


class AuctionStatsReferrersJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
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
        return ["Number of clicks"]

    def get_data(self):
        result = []
        other = 0
        for view in self.views:
            if view["count"] > 1:
                result.append(view["count"])
            else:
                other += 1
        result.append(other)
        return [result]


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
        return ["No images", "One image", "More than one image"]

    def get_providers(self):
        return ["Median sell price", "Average sell price", "Number of lots"]

    def get_data(self):
        lots = Lot.objects.filter(auction=self.auction, winning_price__isnull=False).annotate(
            num_images=Count("lotimage")
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
        return [medians, averages, counts]


class AuctionStatsTravelDistanceJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return [
            "Less than 10 miles",
            "10-20 miles",
            "21-30 miles",
            "31-40 miles",
            "41-50 miles",
            "51+ miles",
        ]

    def get_providers(self):
        return ["Number of users"]

    def get_data(self):
        auctiontos = AuctionTOS.objects.filter(auction=self.auction, user__isnull=False)
        histogram = bin_data(
            auctiontos,
            "distance_traveled",
            number_of_bins=5,
            start_bin=1,
            end_bin=51,
            add_column_for_high_overflow=True,
        )
        return [histogram]


class AuctionStatsPreviousAuctionsJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return ["First auction", "1 previous auction", "2+ previous auctions"]

    def get_providers(self):
        return ["Number of users"]

    def get_data(self):
        auctiontos = AuctionTOS.objects.filter(auction=self.auction, email__isnull=False)
        histogram = bin_data(
            auctiontos,
            "previous_auctions_count",
            number_of_bins=2,
            start_bin=0,
            end_bin=2,
            add_column_for_high_overflow=True,
        )
        return [histogram]


class AuctionStatsLotsSubmittedJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return [
            "Buyer only (0 lots sold)",
            "1-2 lots",
            "3-4 lots",
            "5-6 lots",
            "7-8 lots",
            "9+ lots",
        ]

    def get_providers(self):
        return ["Number of users"]

    def get_data(self):
        invoices = Invoice.objects.filter(auction=self.auction)
        histogram = bin_data(
            invoices,
            "lots_sold",
            number_of_bins=4,
            start_bin=1,
            end_bin=9,
            add_column_for_low_overflow=True,
            add_column_for_high_overflow=True,
        )
        return [histogram]


class AuctionStatsLocationVolumeJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        locations = []
        for location in self.auction.location_qs:
            locations.append(location.name)
        return locations

    def get_providers(self):
        return ["Total bought", "Total sold"]

    def get_data(self):
        sold = []
        bought = []
        for location in self.auction.location_qs:
            sold.append(location.total_sold)
            bought.append(location.total_bought)
        return [bought, sold]


class AuctionStatsLocationFeatureUseJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return [
            "An account",
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
        return ["Percent of users"]

    def get_data(self):
        auctiontos = AuctionTOS.objects.filter(auction=self.auction)
        auctiontos_with_account = auctiontos.filter(user__isnull=False)
        searches = (
            SearchHistory.objects.filter(user__isnull=False, auction=self.auction).values("user").distinct().count()
        )
        seach_percent = int(searches / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
        watch_qs = Watch.objects.filter(lot_number__auction=self.auction).values("user").distinct()
        watches = watch_qs.count()
        watch_percent = int(watches / auctiontos_with_account.count() * 100)
        notifications = (
            PushInformation.objects.filter(user__in=watch_qs, user__userdata__push_notifications_when_lots_sell=True)
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
        if auctiontos.count() == 0:
            lot_with_buy_now_percent = 0
            account_percent = 0
        else:
            account_percent = int(auctiontos_with_account.count() / auctiontos.count() * 100)
            lot_with_buy_now_percent = int(lot_with_buy_now / auctiontos.count() * 100)
        invoices = Invoice.objects.filter(auction=self.auction)
        viewed_invoices = invoices.filter(opened=True)
        if invoices.count():
            view_invoice_percent = int(viewed_invoices.count() / invoices.count() * 100)
        else:
            view_invoice_percent = 0
        sold_lots = Lot.objects.filter(auction=self.auction, auctiontos_winner__isnull=False)
        leave_feedback = sold_lots.filter(~Q(feedback_rating=0)).values("auctiontos_winner").distinct().count()
        all_sold_lots = sold_lots.values("auctiontos_winner").distinct().count()
        if all_sold_lots == 0:
            leave_feedback_percent = 0
        else:
            leave_feedback_percent = int(leave_feedback / all_sold_lots * 100)
        return [
            [
                account_percent,
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


class AuctionStatsAuctioneerSpeedJSONView(AuctionStatsAttritionJSONView):
    def get_providers(self):
        """Return names of datasets."""
        return ["Lots per minute"]

    def get_data(self):
        data = []
        for i in range(1, len(self.lots)):
            minutes = (self.lots[i - 1].date_end - self.lots[i].date_end).total_seconds() / 60
            ignore_if_more_than = 3  # minutes
            if minutes <= ignore_if_more_than:
                data.append({"x": i, "y": minutes})
        return [data]


class AuctionLabelConfig(AuctionViewMixin, FormView):
    form_class = LabelPrintFieldsForm
    template_name = "auction_print_setup.html"

    def get_success_url(self):
        return reverse("auction_printing", kwargs={"slug": self.auction.slug})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["auction"] = self.auction
        return kwargs

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionBulkPrinting(AuctionViewMixin, FormView):
    model = Auction
    template_name = "auction_printing.html"
    form_class = MultiAuctionTOSPrintLabelForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["all_labels_count"] = self.auction.labels_qs.count()
        context["unprinted_label_count"] = self.auction.unprinted_labels_qs.count()
        context["printed_labels_count"] = context["all_labels_count"] - context["unprinted_label_count"]
        context["auction"] = self.auction
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["auctiontos"] = (
            AuctionTOS.objects.filter(auction=self.auction)
            .annotate(
                lots_count=Count(
                    "auctiontos_seller",
                    filter=Q(
                        auctiontos_seller__banned=False,
                        auctiontos_seller__is_deleted=False,
                    ),
                ),
                unprinted_labels_count=Count(
                    "auctiontos_seller",
                    filter=Q(
                        auctiontos_seller__banned=False,
                        auctiontos_seller__label_printed=False,
                        auctiontos_seller__is_deleted=False,
                    ),
                ),
            )
            .filter(lots_count__gt=0)
        )
        return kwargs

    def form_valid(self, form):
        print_only_unprinted = form.cleaned_data["print_only_unprinted"]
        selected_tos = []
        for key, value in form.cleaned_data.items():
            if key.startswith("tos_") and value:
                pk = key.split("_")[1]
                selected_tos.append(pk)
        data = {
            "selected_tos": selected_tos,
            "print_only_unprinted": print_only_unprinted,
        }
        url = reverse("auction_printing_pdf", kwargs={"slug": self.auction.slug})
        url_with_params = f"{url}?{urlencode(data)}"
        return HttpResponseRedirect(url_with_params)


class AuctionBulkPrintingPDF(LotLabelView):
    """Admin page to print labels for multiple users at once"""

    allow_non_admins = False

    def get_queryset(self):
        return self.queryset

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs["slug"]).first()
        self.is_auction_admin

        self.selected_tos = request.GET.get("selected_tos", None)
        self.print_only_unprinted = request.GET.get("print_only_unprinted", "True") == "True"
        if not self.selected_tos:
            self.queryset = self.auction.unprinted_labels_qs
        else:
            self.selected_tos = ast.literal_eval(self.selected_tos)
            if self.print_only_unprinted:
                self.queryset = self.auction.unprinted_labels_qs
            else:
                self.queryset = self.auction.lots_qs
            self.queryset = self.queryset.filter(auctiontos_seller__pk__in=self.selected_tos)
        if not self.get_queryset():
            if not self.selected_tos:
                messages.error(request, "No users selected")
            else:
                messages.error(request, "Couldn't find any labels to print")
            return redirect(reverse("auction_printing", kwargs={"slug": self.auction.slug}))
        if request.method.lower() in self.http_method_names:
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
        else:
            handler = self.http_method_not_allowed
        return handler(request, *args, **kwargs)


class PickupLocationsIncoming(View, AuctionViewMixin):
    """All lots destined for this location"""

    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop("pk")).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        queryset = self.location.incoming_lots.order_by("-auctiontos_seller__name")
        response = HttpResponse(content_type="text/csv")
        name = self.location.name.lower().replace(" ", "_")
        response["Content-Disposition"] = f'attachment; filename="incoming_lots_destined_for_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(
            [
                "Lot number",
                "Lot name",
                "Winner name",
                "Origin",
                "Seller name",
            ]
        )
        for lot in queryset:
            csv_writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.winner_name,
                    lot.location,
                    lot.seller_name,
                ]
            )
        self.auction.create_history(applies_to="LOTS", action="CSV download of incoming lots", user=request.user)
        return response


class PickupLocationsOutgoing(View, AuctionViewMixin):
    """CSV of all lots coming from this location"""

    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop("pk")).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        queryset = self.location.outgoing_lots.order_by("-auctiontos_winner__pickup_location__name")
        response = HttpResponse(content_type="text/csv")
        name = self.location.name.lower().replace(" ", "_")
        response["Content-Disposition"] = f'attachment; filename="outgoing_lots_coming_from_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(["Lot number", "Seller name", "Lot name", "Destination", "Winner name"])
        for lot in queryset:
            csv_writer.writerow(
                [
                    lot.lot_number_display,
                    lot.seller_name,
                    lot.lot_name,
                    lot.winner_location,
                    lot.winner_name,
                ]
            )
        self.auction.create_history(applies_to="LOTS", action="CSV download of outgoing lots", user=request.user)
        return response


class AddToCalendarView(LoginRequiredMixin, View):
    """Redirect or generate an 'Add to Calendar' link for a pickup location"""

    def dispatch(self, request, *args, **kwargs):
        # Extract query params
        self.calendar_type = request.GET.get("type")
        self.second = request.GET.get("second") in ("1", "true", "yes", "True")
        self.location_pk = request.GET.get("location")

        # Validate location exists
        self.location = get_object_or_404(PickupLocation, pk=self.location_pk)
        self.auction = self.location.auction

        if self.second and not self.location.second_pickup_time:
            messages.error(
                request,
                "This location does not have a second pickup time",
            )
            return redirect(self.auction.get_absolute_url())

        if not self.second and not self.location.pickup_time:
            messages.error(
                request,
                "This location does not have a pickup time",
            )
            return redirect(self.auction.get_absolute_url())

        # Confirm user has joined this auction
        self.tos = AuctionTOS.objects.filter(
            auction=self.location.auction,
            user=request.user,
        ).first()

        if not self.tos:
            messages.error(
                request,
                "You haven't joined this auction yet",
            )
            return redirect(self.auction.get_absolute_url())

        # if self.tos.pickup_location.pk is not self.location.pk:
        #     messages.error(
        #         request,
        #         "You can't add a location to your calendar unless you've selected it",
        #     )
        #     return redirect(self.auction.get_absolute_url())

        if self.calendar_type not in ("google", "outlook", "ics"):
            messages.error(
                request,
                "Unknown calendar type requested",
            )
            return redirect(self.auction.get_absolute_url())

        self.tos.add_to_calendar = self.calendar_type
        self.tos.save()

        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        """Handle GET: redirect user or return ICS"""

        # Select pickup time
        start = self.location.second_pickup_time if self.second else self.location.pickup_time
        if not start:
            msg = "Pickup time not available"
            raise Http404(msg)

        # Convert to UTC and define end
        end = start + timedelta(hours=1)

        # Build common event info
        title = f"{self.location.auction.title}"
        if self.second:
            title += " – second pickup"

        details = f"{self.location.auction.title}\n{self.location.description or ''}".strip()
        loc = self.location.address or f"{self.location.latitude},{self.location.longitude}"

        if self.calendar_type == "google":
            params = {
                "action": "TEMPLATE",
                "text": title,
                "dates": f"{start.strftime('%Y%m%dT%H%M%SZ')}/{end.strftime('%Y%m%dT%H%M%SZ')}",
                "details": details,
                "location": loc,
            }
            url = f"https://calendar.google.com/calendar/render?{urlencode(params)}"
            return redirect(url)

        elif self.calendar_type == "outlook":
            # Outlook supports ISO 8601 with UTC Z
            params = {
                "subject": title,
                "body": details,
                "startdt": start.isoformat().replace("+00:00", "Z"),
                "enddt": end.isoformat().replace("+00:00", "Z"),
                "location": loc,
            }
            url = f"https://outlook.live.com/calendar/0/deeplink/compose?{urlencode(params)}"
            return redirect(url)

        else:
            ics_content = self._generate_ics(title, details, start, end, loc)
            filename = f"{self.location.auction.slug}.ics"
            response = HttpResponse(ics_content, content_type="text/calendar")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

    def _generate_ics(self, title, description, start, end, location):
        """Return a valid ICS file string (UTC-based, RFC5545 compliant)"""
        uid = uuid.uuid4()
        now_utc = timezone.now()
        escaped_description = description.replace("\n", "\\n")
        return (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//YourSite//Auction Pickup//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            "METHOD:PUBLISH\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}@yourdomain.com\r\n"
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"SUMMARY:{title}\r\n"
            f"DESCRIPTION:{escaped_description}\r\n"
            f"LOCATION:{location}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )


class CategoryFinder(View, LoginRequiredMixin):
    """API view which will return a category (or none) based on POST keyword lot_name"""

    def get(self, request, *args, **kwargs):
        return redirect("/")

    def post(self, request, *args, **kwargs):
        lot_name = request.POST["lot_name"]
        result = guess_category(lot_name)
        if result:
            result = {"name": result.name, "value": result.pk}
        else:
            result = {"value": None}
        return JsonResponse(result)


class AuctionFinder(View, LoginRequiredMixin):
    """API view which will return information about an auction based on POST keyword auction.  Expects a pk."""

    def get(self, request, *args, **kwargs):
        return redirect("/")

    def post(self, request, *args, **kwargs):
        try:
            self.auction = Auction.objects.filter(pk=request.POST["auction"]).first()
        except ValueError:
            self.auction = None
        if not self.auction or not AuctionTOS.objects.filter(user=request.user, auction=self.auction):
            # you don't get to query auctions you haven't joined
            result = {}
        else:
            result = {
                "use_categories": self.auction.use_categories,
                "reserve_price": self.auction.reserve_price,
                "buy_now": self.auction.buy_now,
                "use_quantity_field": self.auction.use_quantity_field,
                "custom_checkbox_name": self.auction.custom_checkbox_name,
                "custom_field_1": self.auction.custom_field_1,
                "custom_field_1_name": self.auction.custom_field_1_name,
                "use_donation_field": self.auction.use_donation_field,
                "use_i_bred_this_fish_field": self.auction.use_i_bred_this_fish_field,
                "use_custom_checkbox_field": self.auction.use_custom_checkbox_field,
                "use_reference_link": self.auction.use_reference_link,
                "use_description": self.auction.use_description,
            }
        return JsonResponse(result)


class LotChatSubscribe(View, LoginRequiredMixin):
    """Called when a user sends a chat message about a lot to create a ChatSubscription model"""

    def get(self, request, *args, **kwargs):
        return redirect("/")

    def post(self, request, *args, **kwargs):
        try:
            lot = Lot.objects.filter(pk=request.POST["lot"]).first()
        except ValueError:
            lot = None
        if not lot:
            msg = f"No lot found with key {lot}"
            raise Http404(msg)
        else:
            subscription, created = ChatSubscription.objects.get_or_create(
                user=request.user,
                lot=lot,
            )
            unsubscribed = request.POST["unsubscribed"]
            if unsubscribed == "true":  # classic javascript, again
                subscription.unsubscribed = True
            else:
                subscription.unsubscribed = False
            subscription.save()
        return JsonResponse({"unsubscribed": subscription.unsubscribed})


class ChatSubscriptions(LoginRequiredMixin, TemplateView):
    """Show chat messages on your lots and other lots"""

    template_name = "chat_subscriptions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriptions"] = self.request.user.userdata.subscriptions_with_new_message_annotation.order_by(
            "-new_message_count", "-lot__date_posted"
        )
        context["data"] = self.request.user.userdata
        return context


class AddTosMemo(View, LoginRequiredMixin, AuctionViewMixin):
    """API view to update the memo field of an auctiontos"""

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop("pk")
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return redirect("/")

    def post(self, request, *args, **kwargs):
        memo = request.POST["memo"]
        if memo or memo == "":
            self.auctiontos.memo = memo
            self.auctiontos.save()
            return JsonResponse({"result": "ok"})
        raise Http404


class AuctionNoShow(TemplateView, LoginRequiredMixin, AuctionViewMixin):
    """When someone doesn't show up for an auction, offer some tools to clean up the situation"""

    template_name = "auctions/noshow.html"

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.tos = get_object_or_404(AuctionTOS, auction=self.auction, bidder_number=kwargs.pop("tos"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["tos"] = self.tos
        context["bought_lots"] = add_price_info(self.tos.bought_lots_qs)
        context["sold_lots"] = self.tos.lots_qs.annotate(
            full_buyer_refund=ExpressionWrapper(
                F("winning_price") + (F("winning_price") * F("auction__tax") / 100),
                output_field=FloatField(),
            )
        )
        return context


class AuctionNoShowAction(AuctionNoShow, FormMixin):
    """Refund lots, leave feedback, and ban this user"""

    template_name = "auctions/generic_admin_form.html"
    form_class = AuctionNoShowForm

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["tos"] = self.tos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = f"Check any actions you wish to take against {self.tos.name}"
        context["unsold_lot_warning"] = "These actions cannot be undone!"
        context["modal_title"] = f"Take action against {self.tos.name}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            actions = f"Took actions against {self.tos.name}: "
            refund_sold_lots = form.cleaned_data["refund_sold_lots"]
            refund_bought_lots = form.cleaned_data["refund_bought_lots"]
            leave_negative_feedback = form.cleaned_data["leave_negative_feedback"]
            ban_this_user = form.cleaned_data["ban_this_user"]
            if refund_sold_lots:
                actions += "refunded sold lots, "
                for lot in self.tos.lots_qs:
                    if lot.winning_price:
                        lot.refund(100, request.user)
                    else:
                        lot.remove(True, request.user)
            if refund_bought_lots:
                actions += "refunded bought lots, "
                for lot in self.tos.bought_lots_qs:
                    lot.refund(100, request.user)
            if leave_negative_feedback:
                actions += "left negative feedback, "
                for lot in self.tos.bought_lots_qs:
                    lot.winner_feedback_rating = -1
                    lot.winner_feedback_text = "Did not pay"
                    lot.save()
                for lot in self.tos.lots_qs:
                    lot.feedback_rating - 1
                    lot.feedback_text = "Did not provide lot"
                    lot.save()
            if ban_this_user:
                actions += "banned user from future auctions, "
                # we will ban the user whether or not the tos was manually added
                # do not return any evidence to the caller of this request that the ban worked or didn't
                # as that could be used to determine if someone has an account on the site
                user = User.objects.filter(email=self.tos.email).first()
                if self.tos.email and user:
                    obj, created = UserBan.objects.update_or_create(
                        banned_user=user,
                        user=request.user,
                        defaults={},
                    )
            self.auction.create_history(applies_to="USERS", action=actions[:-2], user=request.user)
            return HttpResponse("<script>location.reload();</script>", status=200)
        else:
            return self.form_invalid(form)


@method_decorator(csrf_exempt, name="dispatch")
class PayPalWebhookView(PayPalAPIMixin, View):
    """
    Minimal PayPal webhook handler that:
      - validates the webhook signature with PayPal (verify-webhook-signature)
      - processes a few important event types (onboarding, consent revoke, capture/refund, disputes)
    Requirements:
      - settings.PAYPAL_API_BASE (e.g. https://api-m.sandbox.paypal.com)
      - settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET
      - settings.PAYPAL_WEBHOOK_ID (the webhook id you registered in PayPal dashboard)
      - (optional) settings.PAYPAL_PARTNER_ATTRIBUTION_ID (BN code) if you want to include it in calls
    """

    def post(self, request, *args, **kwargs):
        # Read raw body and parse JSON
        raw_body = request.body
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            logger.exception("Invalid JSON in PayPal webhook: %s", exc)
            return HttpResponseBadRequest("invalid json")

        # Extract PayPal transmission headers (case-insensitive)
        # Django exposes headers as HTTP_<HEADER_NAME> in request.META
        def hdr(name):
            return request.META.get(f"HTTP_{name.upper().replace('-', '_')}", request.headers.get(name))

        transmission_id = hdr("PayPal-Transmission-Id")
        transmission_time = hdr("PayPal-Transmission-Time")
        cert_url = hdr("PayPal-Cert-Url")
        auth_algo = hdr("PayPal-Auth-Algo")
        transmission_sig = hdr("PayPal-Transmission-Sig")
        # webhook_id = getattr(settings, "PAYPAL_WEBHOOK_ID", None)

        if not all([transmission_id, transmission_time, cert_url, auth_algo, transmission_sig]):
            logger.warning(
                "Missing PayPal webhook headers; rejecting. headers=%s webhook_id=%s",
                {
                    k: hdr(k)
                    for k in [
                        "PayPal-Transmission-Id",
                        "PayPal-Transmission-Time",
                        "PayPal-Cert-Url",
                        "PayPal-Auth-Algo",
                        "PayPal-Transmission-Sig",
                    ]
                },
            )
            return HttpResponseBadRequest("missing verification headers or webhook_id")

        # Build verification payload
        verify_payload = {
            "transmission_id": transmission_id,
            "transmission_time": transmission_time,
            "cert_url": cert_url,
            "auth_algo": auth_algo,
            "transmission_sig": transmission_sig,
            # "webhook_id": webhook_id,
            "webhook_event": event,
        }

        # Get platform token using client credentials
        response = self.post_to_paypal("/v1/oauth2/token", payload=verify_payload)
        platform_token = response.get("access_token", None)
        if not platform_token:
            logger.error(
                "Failed to obtain PayPal platform access token for webhook verification, debug_id %s", self.paypal_debug
            )
            return HttpResponse(status=500)

        # Call PayPal verify endpoint
        verify_data = self.post_to_paypal("/v1/notifications/verify-webhook-signature", payload={})
        verification_status = verify_data.get("verification_status")
        if verification_status != "SUCCESS":
            logger.warning(
                "PayPal webhook signature verification failed: %s (verify_resp=%s) debug_id=%s",
                verification_status,
                verify_data,
                self.paypal_debug,
            )
            return HttpResponseBadRequest("webhook verification failed")

        # At this point, the webhook is verified as coming from PayPal.
        event_type = event.get("event_type")
        resource = event.get("resource", {}) or {}

        logger.info("Verified PayPal webhook: %s", event_type)

        if event_type == "MERCHANT.ONBOARDING.COMPLETED":
            # Example: merchant onboarding completed
            # resource may contain merchantId / merchantIdInPayPal / tracking_id
            merchant_id_in_paypal = resource.get("merchant_id")
            tracking_id = resource.get("tracking_id")
            # try find user via tracking_id first
            user = None
            if tracking_id:
                ud = UserData.objects.filter(unsubscribe_link=tracking_id).first()
                if ud:
                    user = ud.user
            # fallback: attempt to find PayPalSeller by merchant id
            if not user and merchant_id_in_paypal:
                seller = PayPalSeller.objects.filter(paypal_merchant_id=merchant_id_in_paypal).first()
                user = seller.user if seller else None

            # Create or update PayPalSeller record (if user found)
            if user:
                seller, _ = PayPalSeller.objects.get_or_create(user=user)
                if merchant_id_in_paypal:
                    seller.paypal_merchant_id = merchant_id_in_paypal
                # email may not be present in the resource; if present, update
                email = resource.get("payerEmail") or resource.get("primary_email") or resource.get("primaryEmail")
                if email:
                    seller.payer_email = email
                seller.save()
                logger.info("Updated PayPalSeller for user %s after onboarding webhook", user.pk)
            else:
                logger.info(
                    "Onboarding webhook: no local user found for merchant %s tracking_id=%s",
                    merchant_id_in_paypal,
                    tracking_id,
                )

        elif event_type == "MERCHANT.PARTNER-CONSENT.REVOKED":
            # Mark seller as disconnected / revoke tokens
            merchant_id_in_paypal = resource.get("merchant_id")
            seller = None
            if merchant_id_in_paypal:
                seller = PayPalSeller.objects.filter(paypal_merchant_id=merchant_id_in_paypal).first()
            if seller:
                seller.delete()
                logger.info("Revoked selling for", seller.pk)
            else:
                logger.info("Partner-consent revoked for unknown merchant %s", merchant_id_in_paypal)

        elif event_type in ("CHECKOUT.ORDER.COMPLETED"):
            """Actually mark the invoice paid and save payment"""
            capture_id = resource.get("purchase_units", [{}])[0].get("payments", {}).get("captures", [{}])[0].get("id")
            if capture_id:
                error, _ = self.handle_order(capture_id)
                if error:
                    logger.error("Error handling completed order for capture_id=%s: %s", capture_id, error)
                else:
                    logger.info("Payment capture completed: capture_id=%s", capture_id)

        elif event_type in ("CHECKOUT.CAPTURE.COMPLETED"):
            """This one doesn't save the invoice"""
            try:
                order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
                # Fetch the order details to get purchase_units[0].reference_id (our invoice pk)
                order_data = self.get_from_paypal(f"v2/checkout/orders/{order_id}")
                purchase_unit = (order_data.get("purchase_units") or [{}])[0]
                reference_id = purchase_unit.get("reference_id")
                logger.info("Capture webhook resolved order_id=%s reference_id=%s", order_id, reference_id)

                invoice = Invoice.objects.filter(pk=reference_id).first()
                if invoice:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "capture_complete", "pk": invoice.pk},
                    )
            except Exception:
                logger.exception(
                    "Error processing capture webhook for resource: %s, debug_id %s", resource, self.paypal_debug
                )
            return JsonResponse({"status": "ok"})
        elif event_type in ("CHECKOUT.ORDER.APPROVED",):
            # Extract the reference_id (our invoice reference) from the approved order and print/log it.
            try:
                purchase_unit = resource.get("purchase_units", [{}])[0]
                reference_id = purchase_unit.get("reference_id")
                logger.info("PayPal webhook CHECKOUT.ORDER.APPROVED reference_id=%s", reference_id)
                invoice = Invoice.objects.filter(pk=reference_id).first()
                if invoice:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "invoice_approved", "pk": invoice.pk},
                    )
            except ValueError:
                logger.exception("Failed to extract reference_id for CHECKOUT.ORDER.APPROVED: %s", resource)
            return JsonResponse({"status": "ok"})

        elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.SALE.REFUNDED"):
            self.handle_refund(resource)
            logger.info("Refund received")

        else:
            # Unhandled event types: log for inspection
            logger.info("Unhandled PayPal webhook event_type=%s resource_keys=%s", event_type, list(resource.keys()))

        # Return success to PayPal
        return JsonResponse({"status": "ok"})


class QuickCheckout(AuctionViewMixin, TemplateView):
    """Enter a bidder number or name and mark their invoice as paid
    For https://github.com/iragm/fishauctions/issues/292"""

    template_name = "auctions/quick_checkout.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class QuickCheckoutHTMX(AuctionViewMixin, PayPalAPIMixin, TemplateView):
    """For use with HTMX calls on QuickCheckout"""

    template_name = "auctions/quick_checkout_htmx.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        qs = AuctionTOS.objects.filter(auction=self.auction)
        filtered_qs = AuctionTOSFilter.generic(self, qs, kwargs.get("filter"))
        invoice = None
        if filtered_qs.count() > 1:
            context["multiple_tos"] = True
        else:
            context["multiple_tos"] = False
            context["tos"] = filtered_qs.first()
            if context["tos"]:
                invoice = context["tos"].invoice
                context["invoice"] = invoice
            if invoice and invoice.show_payment_button and not invoice.reason_for_payment_not_available:
                # generate a paypal order to be placed in a QR code for the user to scan
                context["qr_code_link"] = self.create_order(invoice)
        return context
