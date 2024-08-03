import ast
import csv
from datetime import datetime
from random import randint, uniform, sample
from itertools import chain
from typing import Any, Dict
from django.db.models.base import Model as Model
from django.db.models.query import QuerySet
from django.shortcuts import render,redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect, Http404, FileResponse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.views.generic import ListView, DetailView, View, TemplateView, RedirectView
from django.views.generic.edit import FormView
from django.views.generic.base import TemplateView, ContextMixin
from django.urls import reverse
from django.views.generic.edit import UpdateView, CreateView, DeleteView, FormMixin
from django.db.models import Count, Case, When, IntegerField, Q, Avg
from django.db.models.functions import TruncDay
from django.contrib.messages.views import SuccessMessageMixin
from django.core.files.base import ContentFile
from allauth.account.models import EmailAddress
from el_pagination.views import AjaxListView
from easy_thumbnails.templatetags.thumbnail import thumbnail_url
from easy_thumbnails.files import get_thumbnailer
from post_office import mail
from PIL import Image
from asgiref.sync import sync_to_async
import HeifImagePlugin
import os
from django.conf import settings
from .models import *
from .filters import *
from .forms import *
from .tables import *
from io import BytesIO, TextIOWrapper
from django.core.files import File
import re
from urllib.parse import unquote, urlencode
from django_tables2 import SingleTableMixin
from django_filters.views import FilterView
from dal import autocomplete
from django.utils.html import format_html
from django.core.exceptions import PermissionDenied
from django.forms import modelformset_factory
from django.template.loader import render_to_string
from reportlab.lib import pagesizes, utils
from reportlab.lib.units import inch, cm
from reportlab.lib import colors
from reportlab.platypus import BaseDocTemplate, Paragraph, SimpleDocTemplate, Table, TableStyle, Flowable, Spacer, ImageAndFlowables, Image as PImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_JUSTIFY
import qr_code
from qr_code.qrcode.utils import QRCodeOptions
import textwrap
from user_agents import parse
from chartjs.views.lines import BaseLineChartView
from chartjs.views.columns import BaseColumnsHighChartsView
from chartjs.colors import next_color
from collections import Counter

def bin_data(queryset, field_name,
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
            raise ValueError(f"queryset cannot be ordered by '{field_name}', so start_bin and end_bin are required")
    working_with_date = False
    if queryset.count():
        value = getattr(queryset[0], field_name)
        if isinstance(value, datetime.datetime):
            working_with_date = True
        else:
            try:
                float(value)
            except ValueError:
                raise ValueError(f"{field_name} needs to be either a datetime or an integer value, got value {value}")
    if start_bin is None:
        start_bin = value
    if end_bin is None:
        end_bin = getattr(queryset.last(), field_name)
    if working_with_date:
        bin_size = (end_bin - start_bin).total_seconds() / number_of_bins
    else:
        bin_size = (end_bin - start_bin ) / number_of_bins
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
                bin_start = start_bin + datetime.timedelta(seconds=i * bin_size)
                bin_end = start_bin + datetime.timedelta(seconds=(i + 1) * bin_size)
            else:
                bin_start = start_bin + i * bin_size
                bin_end = start_bin + (i + 1) * bin_size
            
            label = f"{bin_start} - {bin_end if i == number_of_bins - 1 else bin_end - 1}"
            bin_labels.append(label)
        
        if add_column_for_high_overflow:
            bin_labels.append("high overflow")
        return bin_labels, counts_list
    return counts_list
class AdminEmailMixin():
    """Add an admin_email value from settings to the context of a request"""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['admin_email'] = settings.ADMINS[0][1]
        print(settings.ADMINS[0][1])
        print("OK")
        return context

class AuctionPermissionsMixin():
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
            raise Exception("you must set self.auction (typically in dispatch) for self.is_auction_admin to be available")
        result = self.auction.permission_check(self.request.user)
        if not result:
            if self.allow_non_admins:
                pass
                #print('non-admins allowed')
            else:
                raise PermissionDenied()
        else:
            pass
            #print(f"allowing user {self.request.user} to view {self.auction}")
        return result

class AuctionStatsPermissionsMixin():
    """For graph classes"""

    @property
    def is_auction_admin(self):
        """Helper function used to check and see if request.user is the creator of the auction or is someone who has been made an admin of the auction.
        Returns False on no permission or True if the user has permission to access the auction"""
        if not self.auction:
            raise Exception("you must set self.auction (typically in dispatch) for self.is_auction_admin to be available")
        result = self.auction.permission_check(self.request.user)
        if not result:
            if not self.auction.make_stats_public:
                pass
                #print('non-admins allowed')
            else:
                raise PermissionDenied()
        else:
            pass
            #print(f"allowing user {self.request.user} to view {self.auction}")
        return result

class ClickAd(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        try:
            campaignResponse = AdCampaignResponse.objects.get(responseid=self.kwargs['uuid'])
            campaignResponse.clicked = True
            campaignResponse.save()
            return campaignResponse.campaign.external_url
        except:
            return None

class RenderAd(DetailView):
    """
    loaded async with js on ad.html, this view will spit out some raw html (with no css) suitable for displaying as part of a template
    """
    template_name = 'ad_internal.html'
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
            if data['auction']:
                auction = Auction.objects.get(slug=data['auction'], is_deleted=False)
        except:
            pass
        try:
            if data['category']:
                category = Category.objects.get(pk=data['category'])
        except:
            pass
        if user and not category:
            # there wasn't a category on this page, pick one of the user's interests instead
            try:
                categories = UserInterestCategory.objects.filter(user=user).order_by("-as_percent")[:5]
                category = sample(categories, 1)
            except:
                pass
        adCampaigns = AdCampaign.objects.filter(begin_date__lte=timezone.now())\
            .filter(Q(end_date__gte=timezone.now())|Q(end_date__isnull=True))\
            .order_by("-bid")
        if auction:
            adCampaigns = adCampaigns.filter(Q(auction__isnull=True)|Q(auction=auction.pk))
        total = adCampaigns.count()
        chanceOfGoogleAd = 50
        if uniform(0, 100) < chanceOfGoogleAd:
            return None
        for campaign in adCampaigns:
            if campaign.category == category:
                campaign.bid = campaign.bid * 2 # Better chance for matching category.  Don't save after this
            if campaign.bid > uniform(0, total-1):
                if campaign.number_of_clicks > campaign.max_clicks or campaign.number_of_impressions > campaign.max_ads:
                    pass#print("not selected -- limit exceeded")
                else:
                    return AdCampaignResponse.objects.create(user=user, campaign=campaign) # fixme, session here: request.session.session_key

class LotListView(AjaxListView):
    """This is a base class that shows lots, with a filter.  This class is never used directly, but it's a parent for several other classes.
    The context is overridden to set the view type"""
    model = Lot
    template_name = 'all_lots.html'
    auction = None
    routeByLastAuction = False # to display the banner telling users why they are not seeing lots for all auctions

    def get_page_template(self):
        try:
            userData = UserData.objects.get(user=self.request.user.pk)
            if userData.use_list_view:
                return 'lot_list_page.html'
            else:
                return 'lot_tile_page.html'
        except:
            pass
        return 'lot_tile_page.html' # tile view as default
        #return 'lot_list_page.html' # list view as default
    
    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        #if len(data) == 0:
        #    data['status'] = "open" # this would show only open lots by default
        context = super().get_context_data(**kwargs)
        if self.request.GET.get('page'):
            del data['page'] # required for pagination to work
        # gotta check to make sure we're not trying to filter by an auction, or no auction
        try:
            if 'auction' in data.keys():
                # now we have tried to search for something, so we should not override the auction
                self.auction = None
        except Exception as e:
            pass
        context['routeByLastAuction'] = self.routeByLastAuction
        context['filter'] = LotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=True, regardingAuction = self.auction)
        context['embed'] = 'all_lots'
        try:
            context['lotsAreHidden'] = len(UserIgnoreCategory.objects.filter(user=self.request.user))
        except:
            # probably not signed in
            context['lotsAreHidden'] = -1
        try:
            context['lastView'] = PageView.objects.filter(user=self.request.user, lot__isnull=False).order_by('-date_start')[0].date_start
        except:
            context['lastView'] = timezone.now()
        try:
            context['auction'] = Auction.objects.get(slug=data['auction'], is_deleted=False)
        except:
            try:
                context['auction'] = Auction.objects.get(slug=data['a'], is_deleted=False)
            except:
                context['auction'] = self.auction
                context['no_filters'] = True
        if context['auction']:
            try:
                context['auction_tos'] = AuctionTOS.objects.get(auction=context['auction'].pk, user=self.request.user.pk)
            except:
                pass
            #     # this message gets added to every scroll event.  Also, it's just noise
            #     messages.error(self.request, f"Please <a href='/auctions/{context['auction'].slug}/'>read the auction's rules and confirm your pickup location</a> to bid")
        else:
            # this will be a mix of auction and non-auction lots
            context['display_auction_on_lots'] = True
        try:
            self.request.COOKIES['longitude']
        except:
            context['location_message'] = "Set your location to see lots near you"
        context['src'] = 'lot_list'
        return context


class LotAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, result):
        if result.high_bidder:
            return format_html('<b>{}</b>: {}<br><small>High bidder:<span class="text-warning">{} (${})</span></small>', result.lot_number_display, result.lot_name, result.high_bidder_for_admins, result.high_bid)
        else:
            return format_html('<b>{}</b>: {}', result.lot_number_display, result.lot_name)

    def dispatch(self, request, *args, **kwargs):
        # we are not using self.is_auction_admin and the AuctionPermissionsMixinMixin here because self.forwarded.get is not available in dispatch
        return super().dispatch(request, *args, **kwargs)
        
    def get_queryset(self):
        auction = self.forwarded.get('auction')
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
        return format_html('<b>{}</b>: {}', result.bidder_number, result.name)

    def dispatch(self, request, *args, **kwargs):
        # we are not using self.is_auction_admin and the AuctionPermissionsMixinMixin here because self.forwarded.get is not available in dispatch
        return super().dispatch(request, *args, **kwargs)
        
    def get_queryset(self):
        auction = self.forwarded.get('auction')
        invoice = self.forwarded.get('invoice')
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
                            Q(status="PAID")| Q(status="READY"),
                            auctiontos_user=OuterRef('pk')
                            )
                        )
                    )
        if self.q:
            qs = AuctionTOSFilter.generic(self, qs, self.q)
        return qs.order_by('-name')

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
                return 'lot_list_page.html'
            else:
                return 'lot_tile_page.html'
        except:
            pass
        return 'lot_tile_page.html' # tile view as default

    def get_queryset(self):
        data = self.request.GET.copy()
        try:
            auction = data['auction']
        except:
            auction = None
        try:
            qty = int(data['qty'])
        except:
            qty = 10
        try:
            keywords = []
            keywordsString = data['keywords'].lower()
            lotWords = re.findall('[A-Z|a-z]{3,}', keywordsString)
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
            context['embed'] = data['embed']
        except:
            # if not specified in get data, assume this will be viewed by itself
            context['embed'] = 'standalone_page'
        try:
            context['lastView'] = PageView.objects.filter(user=self.request.user).order_by('-date_start')[0].date_start
        except:
            context['lastView'] = timezone.now()
        context['src'] = 'recommended'
        return context

class MyWonLots(LotListView):
    """Show all lots won by the current user"""
    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['status'] = "closed"
        context = super().get_context_data(**kwargs)
        context['filter'] = UserWonLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context['view'] = 'mywonlots'
        context['lotsAreHidden'] = -1
        return context

class MyBids(LotListView):
    """Show all lots the current user has bid on"""
    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        context['filter'] = UserBidLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context['view'] = 'mybids'
        context['lotsAreHidden'] = -1
        return context

class MyLots(SingleTableMixin, FilterView):
    """Selling dashboard.  List of lots added by this user."""
    model = Lot
    table_class = LotHTMxTableForUsers
    filterset_class = LotAdminFilter
    paginate_by = 100

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
        context['userdata'], created = UserData.objects.get_or_create(
                user = self.request.user,
                defaults={},
            )
        return context
    
    def get(self, *args, **kwargs):
        if not self.request.htmx:
            if self.request.user.userdata.unnotified_subscriptions_count:
                msg =  f"You've got { self.request.user.userdata.unnotified_subscriptions_count } lot"
                if self.request.user.userdata.unnotified_subscriptions_count > 1:
                    msg += "s"
                msg += f""" with new messages.  <a href="{reverse('messages')}">Go to your messages page to see them</a>"""
                messages.info(self.request, msg)
        return super().get(*args, **kwargs)

        
class MyWatched(LotListView):
    """Show all lots watched by the current user"""
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['filter'] = UserWatchLotFilter(self.request.GET, queryset=self.get_queryset(), request=self.request, ignore=False)
        context['view'] = 'watch'
        context['lotsAreHidden'] = -1
        return context

class LotsByUser(LotListView):
    """Show all lots for the user specified in the filter"""
    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        try:
            context['user'] = User.objects.get(username=data['user'])
            context['view'] = 'user'
        except:
            context['user'] = None
        context['filter'] = LotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=True, regardingUser=context['user'])

        return context

@login_required
def watchOrUnwatch(request, pk):
    if request.method == 'POST':
        watch = request.POST['watch']
        user = request.user
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if not lot:
            return HttpResponse("Failure")
        # have gotten a MultipleObjectsReturned error pointing here, not sure how that is possible,
        # probably a race condition when multiple watches are fired off at once
        # For now I am treating this as a one-off, but we can add some unique_together criteria to the database if it persists
        obj, created = Watch.objects.update_or_create(
            lot_number=lot,
            user=user,
            defaults={},
        )
        if watch == "false": # string not bool...
            obj.delete()
        if obj:
            return HttpResponse("Success")
        else:
            return HttpResponse("Failure")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

@login_required
def lotNotifications(request):
    if request.method == 'POST':
        user = request.user
        new = LotHistory.objects.filter(lot__user=user.pk, seen=False, changed_price=False).exclude(user=request.user).count()
        if not new:
            new = ""
        return JsonResponse(data={
            'new': new
        })
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

@login_required
def ignoreAuction(request):
    if request.method == 'POST':
        auction = request.POST['auction']
        user = request.user
        try:
            auction = Auction.objects.get(slug=auction, is_deleted=False)
            obj, created = AuctionIgnore.objects.update_or_create(
                auction=auction,
                user=user,
                defaults={},
            )
            return HttpResponse("Success")
        except Exception as e:
            return HttpResponse("Failure: " + e)
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def no_lot_auctions(request):
    """POST-only method that returns an empty string if most recent auction you've used accepts lots
    or the name of the auction and the end date
    Used on the lot creation form"""
    if request.method == 'POST':
        result = ""
        if request.user.is_authenticated:
            userData, created = UserData.objects.get_or_create(
                user = request.user,
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
                        lot_list = Lot.objects.filter(user=request.user, banned=False, deactivated=False, auction=auction, is_deleted=False)
                        if auction.allow_additional_lots_as_donation:
                            lot_list = lot_list.filter(donation=False)
                        lot_list = lot_list.count()
                        result = f"You've added {lot_list} of {auction.max_lots_per_user} lots to {auction}"
            if result:
                result += "<br>"
        return JsonResponse(data={
            'result': result,
        })
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')


def auctionNotifications(request):
    """
    POST-only method that will return a count of auctions as well as some info about the closest one.
    Used to put an icon next to auctions button in main view
    This is mostly a wrapper to go around models.nearby_auctions so that all info isn't accessible to anyone
    """
    if request.method == 'POST':
        new = 0
        name = ""
        link = ""
        slug = ""
        distance = 0
        try:
            latitude = request.COOKIES['latitude']
            longitude = request.COOKIES['longitude']
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
        except Exception as e:
            pass
        if not new:
            new = ""
        return JsonResponse(data={
            'new': new,
            'name': name,
            'link': link,
            'slug': slug,
            'distance': distance
        })
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

@login_required
def setCoordinates(request):
    if request.method == 'POST':
        user = request.user
        userData, created = UserData.objects.get_or_create(
            user = request.user,
            defaults={},
            )
        userData.location_coordinates = f"{request.POST['latitude']},{request.POST['longitude']}"
        userData.last_activity = timezone.now()
        userData.save()
        return HttpResponse("Success")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def userBan(request, pk):
    if request.method == 'POST':
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        auctionsList = Auction.objects.exclude(is_deleted=True).filter(created_by=user.pk)
        # delete all bids the banned user has made on active lots or in active auctions created by the request user
        bids = Bid.objects.filter(user=bannedUser)
        for bid in bids:
            lot = Lot.objects.get(pk=bid.lot_number.pk, is_deleted=False)
            if lot.user == user or lot.auction in auctionsList:
                if not lot.ended:
                    print('Deleting bid ' + str(bid))
                    bid.delete()
        # ban all lots added by the banned user.  These are not deleted, just removed from the auction
        for auction in auctionsList:
            buy_now_lots = Lot.objects.exclude(is_deleted=True).filter(winner=bannedUser, auction=auction.pk)
            for lot in buy_now_lots:
                lot.winner=None
                lot.winning_price = None
                lot.save()
            lots = Lot.objects.exclude(is_deleted=True).filter(user=bannedUser, auction=auction.pk)
            for lot in lots:
                if not lot.ended:
                    print(f"User {str(user)} has banned lot {lot}")
                    lot.banned = True
                    lot.ban_reason = "This user has been banned from this auction"
                    lot.save()
        #return #redirect('/users/' + str(pk))
        return redirect(reverse("userpage", kwargs={'slug': bannedUser.username}))
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def lotDeactivate(request, pk):
    if request.method == 'POST':
        lot = Lot.objects.get(pk=pk, is_deleted=False)
        checksPass = False
        if request.user.is_superuser:
            checksPass = True
        if lot.user.pk == request.user.pk:
            checksPass = True
        if lot.auction:
            checksPass = False
        if checksPass:
            if lot.deactivated:
                lot.deactivated = False
            else:
                bids = Bid.objects.filter(lot_number=lot.lot_number)
                for bid in bids:
                    bid.delete()
                lot.deactivated = True
            lot.save()
            return HttpResponse("success")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

# def lotBan(request, pk):
#     if request.method == 'POST':
#         lot = Lot.objects.get(pk=pk)
#         try:
#             ban_reason = request.POST['banned']
#         except:
#             return HttpResponse("specify banned in post data")
#         checksPass = False
#         if request.user.is_superuser:
#             checksPass = True
#         if lot.auction:
#             if lot.auction.created_by.pk == request.user.pk:
#                 checksPass = True
#         if checksPass:
#             if not ban_reason:
#                 lot.banned = False
#             else:
#                 lot.banned = True
#             lot.ban_reason = ban_reason
#             lot.save()
#             # I am debating whether or not to add a LotHistory here
#             # A similar one would need to be added when banning a user,
#             # so it may make more sense to do this with a reciever on save of lot
#             # for now, I'm going to leave this alone
#             return HttpResponse("success")
#     messages.error(request, "Your account doesn't have permission to view this page")
#     return redirect('/')
            

def userUnban(request, pk):
    """Delete the UserBan"""
    if request.method == 'POST':
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        obj.delete()
        #return redirect('/users/' + str(pk))
        return redirect(reverse("userpage", kwargs={'slug': bannedUser.username}))
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def imagesPrimary(request):
    """Make the specified image the default image for the lot
    Takes pk of image as post param
    this does not check lot.can_add_images, which is deliberate (who cares if you rotate...)
    at some point, this function and the rotate function should be converted into classes
    """
    if request.method == 'POST':
        try:
            user = request.user
            pk = int(request.POST['pk'])
        except:
            return HttpResponse("user and pk are required")
        try:
            lotImage = LotImage.objects.get(pk=pk)
        except:
            return HttpResponse(f"Image {pk} not found")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can change images")
            return redirect('/')
        LotImage.objects.filter(lot_number=lotImage.lot_number.pk).update(is_primary=False)
        lotImage.is_primary = True
        lotImage.save()
        return HttpResponse("Success")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def imagesRotate(request):
    """Rotate an image associated with a lot
    Takes pk of image and angle as post params
    """
    if request.method == 'POST':
        try:
            user = request.user
            pk = int(request.POST['pk'])
            angle = int(request.POST['angle'])
        except (KeyError, ValueError):
            return HttpResponse("user, pk, and angle are required")
        try:
            lotImage = LotImage.objects.get(pk=pk)
        except LotImage.DoesNotExist:
            return HttpResponse(f"Image {pk} not found")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can rotate images")
            return redirect('/')
        if not lotImage.image:
            return HttpResponse("No image")
        temp_image = Image.open(BytesIO(lotImage.image.read()))
        temp_image = temp_image.rotate(angle, expand=True)
        if temp_image.mode in ("RGBA", "P"):
            temp_image = temp_image.convert("RGB")
        output = BytesIO()
        temp_image.save(output, format='JPEG', quality=85)
        output.seek(0)
        # Overwrite the original image
        lotImage.image.save(lotImage.image.name.replace("images/", ""), ContentFile(output.read()), save=True)
        return HttpResponse("Success")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def feedback(request, pk, leave_as):
    """Leave feedback on a lot
    This can be done as a buyer or a seller
    api/feedback/lot_number/buyer
    api/feedback/lot_number/seller
    """
    if request.method == 'POST':
        data = request.POST
        try:
            lot = Lot.objects.get(pk=pk, is_deleted=False)
        except:
            raise Http404 (f"No lot found with key {lot}") 
        winner_checks_pass = False
        seller_checks_pass = False
        if leave_as == "winner":
            if lot.winner:
                if lot.winner.pk == request.user.pk:
                    winner_checks_pass = True
            if lot.auctiontos_winner:
                if lot.auctiontos_winner.user:
                    if lot.auctiontos_winner.user.pk == request.user.pk:
                        winner_checks_pass = True
        if winner_checks_pass:
            try:
                lot.feedback_rating = data['rating']
                lot.save()
            except:
                pass
            try:
                lot.feedback_text = data['text']
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
                lot.winner_feedback_rating = data['rating']
                lot.save()
            except:
                pass
            try:
                lot.winner_feedback_text = data['text']
                lot.save()
            except:
                pass
        if not winner_checks_pass and not seller_checks_pass:
            messages.error(request, "Only the seller or winner of a lot can leave feedback")
            return redirect('/')
        return HttpResponse("Success")
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

def clean_referrer(url):
    """Make a URL more human readable"""
    if not url:
        url = ""
    url = re.sub(r'^https?://', '', url) # no http/s at the beginning
    if 'auction.fish/' not in url:
        url = re.sub(r'\?.*', '', url) # remove get params
    url = re.sub(r'^www\.', '', url) # www
    url = re.sub(r'/+$', '', url) # trailing /
    # if someone has facebook.example.com, it would be recorded as FB...
    # can update this if it becomes an issue
    if re.search(r'(facebook)\.', url):
        url = "Facebook"
    if re.search(r'(google)\.', url):
        url = "Google"
    return url

def pageview(request):
    """Record page views"""
    if request.method == 'POST':
        data = request.POST
        auction = data.get("auction", None)
        if auction:
            auction = Auction.objects.filter(pk=auction).first()
        lot_number = data.get("lot", None)
        if lot_number:
            lot_number = Lot.objects.filter(pk=lot_number, is_deleted=False).first()
        url = data.get("url", None)
        url_without_params = re.sub(r'\?.*', '', url)
        url_without_params = url_without_params[:600]
        first_view = data.get("first_view", False)
        if request.user.is_authenticated:
            user = request.user
            session_id = None
        else:
            # anonymous users go by session
            user = None
            session_id = request.session.session_key
        if first_view == 'true':  #good ol Javascript
            user_agent = request.META.get('HTTP_USER_AGENT', '')
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
            # mark auction campaign results if applicable present
            ip = ""
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                ip = x_forwarded_for.split(',')[0]
            else:
                ip = request.META.get('REMOTE_ADDR')
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
            pageview = PageView.objects.create(
                lot_number = lot_number,
                url = url_without_params,
                auction = auction,
                session_id = session_id,
                user = user,
                user_agent = user_agent,
                ip_address = ip[:100],
                platform=parsed_ua.os.family,
                os=os,
                referrer = referrer,
                title = data.get("title", "")[:600],
                source = source,
            )
            if user and lot_number and lot_number.species_category:
                # create interest in this category if this is a new view for this category
                interest, created = UserInterestCategory.objects.get_or_create(
                    category=lot_number.species_category,
                    user=user,
                    defaults={ 'interest': 0 }
                    )
                interest.interest += settings.VIEW_WEIGHT
                interest.save()
            if auction and user:
                if not source:
                    source = referrer
                # there is a timing issue here that allows multiple campaigns to be created
                # https://github.com/iragm/fishauctions/issues/168
                campaign, created = AuctionCampaign.objects.get_or_create(
                    auction = auction,
                    user = user,
                    defaults={ 
                        'email': user.email, 
                        'source': source
                    }
                )
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
    return redirect('/')

def invoicePaid(request, pk, **kwargs):
    if request.method == 'POST':
        try:
            invoice = Invoice.objects.get(pk=pk)
        except:
            raise Http404 (f"No invoice found with key {pk}")  
        checksPass = False
        if invoice.auction:
            if invoice.auction.permission_check(request.user):
                checksPass = True
        if checksPass:
            invoice.status = kwargs['status']
            invoice.save()
            return HttpResponse(render_to_string("invoice_buttons.html", {'invoice':invoice}), status=200) 
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')
    
@login_required
def my_lot_report(request):
    """CSV file showing my lots"""
    lots = add_price_info(Lot.objects.filter(Q(user=request.user)|Q(auctiontos_seller__email=request.user.email)).exclude(is_deleted=True))
    current_site = Site.objects.get_current()
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="my_lots_from_{current_site.domain.replace(".","_")}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Lot number", "Name", "Auction", "Status", "Winning price", "My cut"])
    for lot in lots:
        status = 'Unsold'
        if lot.banned:
            status = 'Removed'
        elif lot.deactivated:
            status = 'Deactivated'
        elif lot.winner or lot.auctiontos_winner:
            status = 'Sold'
        writer.writerow([lot.lot_number_display, lot.lot_name, lot.auction, status, lot.winning_price, lot.your_cut])
    return response

@login_required
def auctionReport(request, slug):
    """Get a CSV file showing all users who are participating in this auction"""
    auction = get_object_or_404(Auction, slug=slug, is_deleted=False)
    if auction.permission_check(request.user):
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type='text/csv')
        end = timezone.now().strftime("%Y-%m-%d")
        response['Content-Disposition'] = 'attachment; filename="' + slug + "-report-" + end + '.csv"'
        writer = csv.writer(response)
        writer.writerow(['Join date', 'Bidder number', 'Username', 'Name', 'Email', 'Phone', 'Address', 'Location', 'Miles to pickup location', 'Club', 'Lots viewed', 'Lots bid', 'Lots submitted', 'Lots won', 'Invoice', 'Total bought', 'Total sold', 'Invoice total due', 'Breeder points', "Number of lots sold outside auction", "Total value of lots sold outside auction", "Seconds spent reading rules", "Other auctions joined", "Users who have banned this user", "Account created on",
                         'Memo', ])
        users = AuctionTOS.objects.filter(auction=auction).select_related('user__userdata').select_related('pickup_location').order_by('createdon')
                #.annotate(distance_traveled=distance_to(\
                #'`auctions_userdata`.`latitude`', '`auctions_userdata`.`longitude`', \
                #lat_field_name='`auctions_pickuplocation`.`latitude`',\
                #lng_field_name="`auctions_pickuplocation`.`longitude`",\
                #approximate_distance_to=1)\
                #)
        for data in users:
            distance = ""
            club = ""
            if data.user and not data.manually_added:
                # these things will only be written out if the user wants you to have it
                lotsViewed = PageView.objects.filter(lot_number__auction=auction, user=data.user)
                lotsBid = Bid.objects.filter(lot_number__auction=auction,user=data.user)
                lot_qs = Lot.objects.exclude(is_deleted=True).filter(user=data.user, auction__isnull=True, date_posted__gte=auction.date_start - datetime.timedelta(days=2))
                if auction.is_online:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=auction.date_end + datetime.timedelta(days=2))
                else:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=auction.date_start + datetime.timedelta(days=5))
                numberLotsOutsideAuction = lotsOutsideAuction.count()
                profitOutsideAuction = lotsOutsideAuction.aggregate(total=Sum('winning_price'))['total']
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
            else:
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
            breederPoints = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=data, auction=auction, i_bred_this_fish=True)
            address = data.address or ""
            try:
                invoice = Invoice.objects.get(auction=auction, auctiontos_user=data)
                invoiceStatus = invoice.get_status_display()
                totalSpent = invoice.total_bought
                totalPaid = invoice.total_sold
                invoiceTotal = invoice.rounded_net
            except:
                invoiceStatus = ""
                totalSpent = "0" 
                totalPaid = "0"
                invoiceTotal = ""
            writer.writerow([data.createdon.strftime("%m-%d-%Y"), data.bidder_number, username, data.name, data.email, \
                data.phone_as_string, address, data.pickup_location, distance,\
                club, len(lotsViewed), len(lotsBid), len(lotsSumbitted), \
                len(lotsWon), invoiceStatus, totalSpent, totalPaid, invoiceTotal, len(breederPoints),\
                numberLotsOutsideAuction, profitOutsideAuction, data.time_spent_reading_rules, previous_auctions, number_of_userbans, account_age, data.memo])
        return response    
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

@login_required
def userReport(request):
    """Get a CSV file showing all users from all auctions you're an admin for"""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=all_auction_contacts.csv"'
    writer = csv.writer(response)
    found = []
    writer.writerow(['Name', 'Email', 'Phone'])
    auctions = Auction.objects.filter(Q(created_by=request.user)|Q(auctiontos__is_admin=True, auctiontos__user=request.user))
    users = AuctionTOS.objects.filter(auction__in=auctions)
    for user in users:
        if user.email not in found:
            writer.writerow([user.name, user.email, user.phone_as_string])
            found.append(user.email)
    return response

@login_required
def auctionInvoicesPaypalCSV(request, slug, chunk):
    """Get a CSV file of all unpaid invoices that owe the club money"""
    auction = Auction.objects.get(slug=slug, is_deleted=False)
    if auction.permission_check(request.user):
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type='text/csv')
        dueDate = timezone.now().strftime("%m/%d/%Y")
        current_site = Site.objects.get_current()
        response['Content-Disposition'] = f'attachment; filename="{slug}-paypal-{chunk}.csv"'
        writer = csv.writer(response)
        writer.writerow(['Recipient Email', 'Recipient First Name', 'Recipient Last Name', 'Invoice Number',\
            'Due Date', 'Reference', 'Item Name', 'Description', 'Item Amount', 'Shipping Amount', 'Discount Amount',\
            'Currency Code', 'Note to Customer', 'Terms and Conditions', 'Memo to Self'])
        invoices = auction.paypal_invoices
        count = 0
        chunkSize = 150 # attention: this is also set in models.auction.paypal_invoice_chunks
        for invoice in invoices:
            invoice.recalculate
            # we loop through everything regardless of which chunk
            if not invoice.user_should_be_paid:
                count += 1
                if count <= chunkSize*chunk and count > chunkSize*(chunk-1):
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
                    currencyCode = "USD"
                    noteToCustomer = f"https://{current_site.domain}/invoices/{invoice.pk}/"
                    termsAndConditions = ""
                    memoToSelf = invoice.auctiontos_user.memo
                    if itemAmount > 0 and email:
                        writer.writerow([email, firstName, lastName, invoiceNumber, dueDate, reference, itemName, description, itemAmount, shippingAmount, discountAmount,\
                            currencyCode, noteToCustomer, termsAndConditions, memoToSelf])
        return response    
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

@login_required
def auctionLotList(request, slug):
    """Get a CSV file showing all sold lots, who bought/sold them, and the winner's location"""
    auction = Auction.objects.get(slug=slug, is_deleted=False)
    if auction.permission_check(request.user):
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="' + slug + '-lot-list.csv"'
        writer = csv.writer(response)
        writer.writerow(['Lot number', 'Lot', 'Seller', 'Seller email', 'Seller phone', 'Seller location', 'Winner', 'Winner email', 'Winner phone',  'Winner location', 'Breeder points'])
        #lots = Lot.objects.exclude(is_deleted=True).filter(auction__slug=slug, auctiontos_winner__isnull=False).select_related('user', 'winner')
        lots = auction.lots_qs.filter(winning_price__isnull=False).select_related('user', 'winner')
        for lot in lots:
            lot_number = lot.custom_lot_number or lot.lot_number
            writer.writerow([lot_number,\
            lot.lot_name,\
            lot.auctiontos_seller.name, \
            lot.auctiontos_seller.email,
            lot.auctiontos_seller.phone_as_string,
            lot.location,
            lot.auctiontos_winner.name,\
            lot.auctiontos_winner.email,
            lot.auctiontos_winner.phone_as_string,
            lot.winner_location,
            lot.i_bred_this_fish_display
            ])
        return response    
    messages.error(request, "Your account doesn't have permission to view this page")
    return redirect('/')

class LeaveFeedbackView(LoginRequiredMixin, ListView):
    """Show all pickup locations belonging to the current user"""
    model = Lot
    template_name = 'leave_feedback.html'
    ordering = ['-date_posted']
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cutoffDate =  timezone.now() - datetime.timedelta(days=90)
        context['won_lots'] = Lot.objects.exclude(is_deleted=True).filter(Q(winner=self.request.user)|Q(auctiontos_winner__user=self.request.user), date_posted__gte=cutoffDate).order_by('-date_posted')
        context['sold_lots'] = Lot.objects.exclude(is_deleted=True).filter(Q(user=self.request.user)|Q(auctiontos_seller__user=self.request.user), date_posted__gte=cutoffDate, winning_price__isnull=False).order_by('-date_posted')
        return context

class AuctionChats(LoginRequiredMixin, ListView, AuctionPermissionsMixin):
    """Auction admins view to show and delete all chats for an auction"""
    model = LotHistory
    template_name = 'chats.html'

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        if not self.auction:
            raise Http404
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # get related auctiontos if the user has joined the auction
        auctiontos_subquery = AuctionTOS.objects.filter(
            user=OuterRef('user'),
            auction=self.auction
        ).values('pk')[:1]
        qs = LotHistory.objects.filter(
            lot__auction=self.auction,
            changed_price=False,
        ).annotate(
            auctiontos_pk=Subquery(auctiontos_subquery, output_field=IntegerField(), null=True)
        ).order_by('-timestamp')
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auction'] = self.auction
        return context

class AuctionChatDeleteUndelete(View, AuctionPermissionsMixin):
    """HTMX for auction admins only"""
    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get('pk')
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
        return HttpResponse(result)

class PickupLocations(ListView, AuctionPermissionsMixin):
    """Show all pickup locations belonging to the current auction"""
    model = PickupLocation
    template_name = 'all_pickup_locations.html'
    ordering = ['name']
    
    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        if not self.auction:
            raise Http404
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = PickupLocation.objects.filter(
            auction=self.auction,
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auction'] = self.auction
        return context
    
class PickupLocationsDelete(DeleteView, AuctionPermissionsMixin):
    model = PickupLocation

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object().auction
        self.success_url = reverse("auction_pickup_location", kwargs={'slug': self.auction.slug})
        if self.get_object().auction.location_qs.count() < 2:
            messages.error(request, "You can't delete the only pickup location in this auction")
            return redirect(self.success_url)
        if self.get_object().number_of_users:
            messages.error(request, "There are already users that have selected this location, it can't be deleted")
            return redirect(self.success_url)
        if not self.is_auction_admin:
            messages.error(request, "You don't have permission to delete a pickup location")
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return self.success_url
    
class PickupLocationForm():
    """Base form for create and update"""
    model = PickupLocation
    template_name = 'location_form.html'
    form_class = PickupLocationForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        kwargs['auction'] = self.auction
        try:
            kwargs['user_timezone'] = self.request.COOKIES['user_timezone']
        except:
            kwargs['user_timezone'] = settings.TIME_ZONE
        return kwargs

    def get_success_url(self):
        data = self.request.GET.copy()
        try:
            return data['next']
        except:
            if self.auction.is_online:
                return reverse('auction_pickup_location', kwargs={'slug': self.auction.slug})    
            else:
                return self.auction.get_absolute_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auction'] = self.auction
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

class PickupLocationsUpdate(PickupLocationForm, UpdateView, AuctionPermissionsMixin):
    """Edit pickup locations"""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['is_edit_form'] = True
        kwargs['pickup_location'] = self.get_object()
        return kwargs
    
    def get(self, *args, **kwargs):
        users = AuctionTOS.objects.filter(pickup_location=self.get_object().pk).count()
        if users:
            messages.info(self.request, f"{users} users have already selected this as a pickup location.  Don't make large changes!")
        return super().get(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object().auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form, **kwargs):
        form = super().form_valid(form)
        messages.info(self.request, "Updated location")
        return form

class PickupLocationsCreate(PickupLocationForm, CreateView, AuctionPermissionsMixin):
    """Create a new pickup location"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['is_edit_form'] = False
        kwargs['pickup_location'] = None
        return kwargs

class AuctionUpdate(UpdateView, AuctionPermissionsMixin):
    """The form users fill out to edit an auction"""
    model = Auction
    template_name = 'auction_edit_form.html'
    form_class = AuctionEditForm    

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object()
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)
    
    def get_success_url(self):
        return "/auctions/" + str(self.kwargs['slug'])
    
    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs['user'] = self.request.user
        kwargs['cloned_from'] = None
        try:
            kwargs['user_timezone'] = self.request.COOKIES['user_timezone']
        except:
            kwargs['user_timezone'] = settings.TIME_ZONE
        return kwargs

    def get_context_data(self, **kwargs):
        existing_lots = Lot.objects.exclude(is_deleted=True).filter(auction=self.get_object()).count()
        if existing_lots:
            messages.info(self.request, "Lots have already been added to this auction.  Don't make large changes!")
        context = super().get_context_data(**kwargs)
        context['title'] = f"{self.auction}"
        context['is_online'] = self.auction.is_online
        return context

class AuctionLots(SingleTableMixin, FilterView, AuctionPermissionsMixin):
    """List of lots associated with an auction.  This is for admins; don't confuse this with the thumbnail-enhanced lot view `AllLots` for users.
    
    At some point, it may make sense to subclass AllLots here, but I think the needs of the two views are so different that it doesn't make sense
    """
    model = Lot
    table_class = LotHTMxTable
    filterset_class = LotAdminFilter
    paginate_by = 50

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "auctions/auction_lot_admin.html"
        return template_name

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        self.queryset = Lot.objects.exclude(is_deleted=True).filter(auction=self.auction).order_by('lot_number')
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_tab'] = 'lots'
        context['auction'] = self.auction
        #context['filter'] = LotAdminFilter(auction = self.auction)
        return context

class AuctionUsers(SingleTableMixin, FilterView, AuctionPermissionsMixin):
    """List of users (AuctionTOS) associated with an auction"""
    model = AuctionTOS
    table_class = AuctionTOSHTMxTable
    filterset_class = AuctionTOSFilter
    paginate_by = 500

    def get_template_names(self):
        if self.request.htmx:
            template_name = "tables/table_generic.html"
        else:
            template_name = "auction_users.html"
        return template_name

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        self.queryset = AuctionTOS.objects.filter(auction=self.auction).order_by('name')
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auction'] = self.auction
        context['active_tab'] = 'users'
        return context

class AuctionStats(DetailView, AuctionPermissionsMixin):
    """Fun facts about an auction"""
    model = Auction
    template_name = 'auction_stats.html'
    allow_non_admins = True
    
    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object()
        auth = False
        if self.get_object().make_stats_public:
            auth = True
        if self.is_auction_admin:
            auth = True
        if auth:
            return super().dispatch(request, *args, **kwargs)
        else:
            messages.error(request, "Stats for this auction are not public")
            return redirect('/')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.get_object().closed and self.get_object().is_online:
            messages.info(self.request, "This auction is still in progress, check back once it's finished for more complete stats")
        if self.get_object().date_posted < timezone.make_aware(datetime.datetime(year=2024, month=1, day=1)):
           messages.info(self.request, "Not all stats are available for old auctions.")
        return context

class QuickSetLotWinner(FormView, AuctionPermissionsMixin):
    """A form to let people record the winners of lots (really just for in-person auctions). Just 3 fields:
        lot number
        winner
        winning price
        """
    template_name = "auctions/quick_set_winner.html"
    form_class = WinnerLot
    model = Lot

    def get_success_url(self):
        return reverse('auction_lot_winners_autocomplete', kwargs={'slug': self.auction.slug})

    def get_queryset(self):
        return self.auction.lots_qs

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs.pop('slug'), is_deleted=False)
        self.is_auction_admin
        undo = self.request.GET.get('undo')
        if undo and request.method == 'GET':
            undo_lot = Lot.objects.filter(custom_lot_number=undo, auction=self.auction).first()
            if undo_lot:
                undo_lot.winner = None
                undo_lot.auctiontos_winner = None
                undo_lot.winning_price = None
                if not self.auction.is_online:
                    undo_lot.date_end = None
                undo_lot.active = False
                undo_lot.save()
                messages.info(request, f"{undo_lot.custom_lot_number} {undo_lot.lot_name} now has no winner and can be sold") 
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs['auction'] = self.auction
        return form_kwargs
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auction'] = self.auction
        return context
        
    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        lot = form.cleaned_data.get("lot")
        winner = form.cleaned_data.get("winner")
        winning_price = form.cleaned_data.get("winning_price")
        lot = Lot.objects.get(pk=lot, is_deleted=False)
        tos = AuctionTOS.objects.get(pk=winner)
        # check auction, find a lot that matches this one, confirm it belongs to this auction
        if lot.auction and lot.auction == self.auction:
            if (not tos and not winning_price) or (tos.auction and tos.auction == self.auction):
                return self.set_winner(lot, tos, winning_price)
        return self.form_invalid(form)

    def set_winner(self, lot, winning_tos, winning_price):
        """Set the winner (or mark lot unsold), 
            add a success message
            This does not do permissions or validation checks, do those first
            Call this once the form is valid"""
        if not winning_price:
            lot.date_end = timezone.now()
            lot.save()
            messages.success(self.request, f"Lot {lot.custom_lot_number} has been ended with no winner")
        else:
            lot.auctiontos_winner = winning_tos
            lot.winning_price = winning_price
            lot.date_end = timezone.now()
            lot.save()
            lot.add_winner_message(self.request.user, winning_tos, winning_price)
            undo_url = self.get_success_url() + f"?undo={lot.custom_lot_number}"
            messages.success(self.request, f"Bidder {winning_tos.bidder_number} is now the winner of lot {lot.custom_lot_number}.  <a href='{undo_url}'>Undo</a>")
        return HttpResponseRedirect(self.get_success_url())

class SetLotWinner(QuickSetLotWinner):
    """Same as QuickSetLotWinner but without the autocomplete, per user requests"""
    form_class = WinnerLotSimple
    
    def get_success_url(self):
        return reverse('auction_lot_winners', kwargs={'slug': self.auction.slug})

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        lot = form.cleaned_data.get("lot")
        winner = form.cleaned_data.get("winner")
        winning_price = form.cleaned_data.get("winning_price")
        if winning_price is None or winning_price < 0:
            form.add_error('winning_price', "How much did the lot sell for?")
        qs = self.auction.lots_qs
        lot = qs.filter(custom_lot_number=lot).first()
        tos = None
        if not lot:
            form.add_error('lot', "No lot found")
        if winning_price is not None and winning_price > 0:
            tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=winner)
            if len(tos) > 1:
                form.add_error('winner', f"{len(tos)} bidders found with this number!")
            else:
                tos = tos.first()
            if not tos:
                form.add_error('winner', "No bidder found")
            else:
                if tos.invoice and tos.invoice.status != "DRAFT":
                    form.add_error('winner', "This user's invoice is not open")
        if lot:
            if lot.auctiontos_seller and lot.auctiontos_seller.invoice and lot.auctiontos_seller.invoice.status != "DRAFT":
                form.add_error('lot', "The seller's invoice is not open")
            else:
                # right now we allow you to mark unsold lots that have already been sold, bad idea but it's what people want
                if lot.auctiontos_winner and lot.winning_price and winning_price != 0:
                #if lot.auctiontos_winner and lot.winning_price: # this would be better, but would confuse
                    error = f'Lot {lot.lot_number_display} has already been sold'
                    try:
                        if tos.invoice.status == "DRAFT" and lot.auctiontos_seller and lot.auctiontos_seller.invoice.status == "DRAFT":
                            undo_url = self.get_success_url() + f"?undo={lot.custom_lot_number}"
                            form.add_error('lot', mark_safe(f"{error}.  <a href='{undo_url}'>Click here to mark unsold</a>."))
                    except:
                        # one invoice or the other doesn't exist, this only happens when the selling the first lot to a given tos
                        form.add_error('lot', mark_safe(f"{error}"))
        if form.is_valid():
            return self.set_winner(lot, tos, winning_price)
        return self.form_invalid(form)

class SetLotWinnerImage(SetLotWinner):
    """Same as QuickSetLotWinner but without the autocomplete, and with images, per user requests"""
    template_name = "auctions/quick_set_winner_images.html"
    form_class = WinnerLotSimpleImages

    def get_success_url(self):
        return reverse('auction_lot_winners_images', kwargs={'slug': self.auction.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['hide_navbar'] = True
        context['menu_tip'] = mark_safe(f"Press Tab to move the to next field, F11 for full screen, control + or control - to zoom.  <a href='{reverse('auction_main', kwargs={'slug': self.auction.slug})}'>{self.auction} home</a>.")
        return context

class BulkAddUsers(TemplateView, ContextMixin, AuctionPermissionsMixin):
    """Add/edit lots of lots for a given auctiontos pk"""
    template_name = "auctions/bulk_add_users.html"
    max_users_that_can_be_added_at_once = 200
    extra_rows = 5
    AuctionTOSFormSet = None

    def get(self, *args, **kwargs):
        # first, try to read in a CSV file stored in session 
        initial_formset_data = self.request.session.get('initial_formset_data', [])
        if initial_formset_data:
            self.extra_rows = len(initial_formset_data) + 1
            del self.request.session['initial_formset_data']
        else:
            # next, check GET to see if they're asking for an import from a past auction
            import_from_auction = self.request.GET.get('import')
            if import_from_auction:
                other_auction = Auction.objects.exclude(is_deleted=True).filter(slug=import_from_auction).first()
                if not other_auction.permission_check(self.request.user):
                    messages.error(self.request, f"You don't have permission to add users from {other_auction}")
                else:
                    auctiontos = AuctionTOS.objects.filter(auction=other_auction)
                    total_skipped = 0
                    total_tos = 0
                    for tos in auctiontos:
                        if not self.tos_is_in_auction(self.auction, tos.name, tos.email):
                            initial_formset_data.append({
                                'bidder_number':tos.bidder_number,
                                'name':tos.name,
                                'phone':tos.phone_number,
                                'email':tos.email,
                                'address':tos.address,
                                'is_club_member':tos.is_club_member
                            })
                            total_tos += 1
                        else:
                            total_skipped += 1
                    if total_tos >= self.max_users_that_can_be_added_at_once:
                        messages.error(self.request, f"You can only add {self.max_users_that_can_be_added_at_once} users from another auction at once; run this again to add additional users.")
                    if total_skipped:
                        messages.info(self.request, f"{total_skipped} users are already in this auction (matched by email, or name if email not set) and do not appear below")
                    if total_tos:
                        self.extra_rows = total_tos + 1
        self.instantiate_formset()
        self.tos_formset = self.AuctionTOSFormSet(form_kwargs={'auction': self.auction, 'bidder_numbers_on_this_form':[]}, queryset=self.queryset, initial=initial_formset_data)
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def handle_csv_file(self, csv_file, *args, **kwargs):
        """If a CSV file has been uploaded, parse it and redirect"""
        
        def extract_info(row, field_name_list, default_response=""):
            """Pass a row, and a lowercase list of field names
            extract the first match found (case insenstive) and return the value from the row
            emptry string returned if the value is not found in the row"""
            case_insensitive_row = {k.lower(): v for k, v in row.items()}
            for name in field_name_list:
                try:
                    return case_insensitive_row[name]
                except:
                    pass
            return default_response
        
        def columns_exist_in_csv(csv_reader, columns):
            """returns True if any value in the list `columns` exists in the file"""
            first_row = next(csv_reader)
            result = extract_info(first_row, columns, None)
            if result == None:
                return False
            else:
                return True        
        csv_file.seek(0)
        csv_reader = csv.DictReader(TextIOWrapper(csv_file.file))
        email_field_names = ['email', 'e-mail', 'email address', 'e-mail address']
        bidder_number_fields = ['bidder number', 'bidder']
        name_field_names = ['name', 'full name', 'first name', 'firstname']
        address_field_names = ['address', 'mailing address']
        phone_field_names = ['phone', 'phone number','telephone','telephone number']
        is_club_member_fields = ['member', 'club member']
        # we are not reading in location here, do we care??
        some_columns_exist = False
        error = ""
        # order matters here - the most important columns should be validated last, 
        # so the error refers to the most important missing column
        if not columns_exist_in_csv(csv_reader, phone_field_names):
            error = "This file does not contain a phone column"
        else:
            some_columns_exist = True
        if not columns_exist_in_csv(csv_reader, address_field_names):
            error = "This file does not contain an address column"
        else:
            some_columns_exist = True
        if not columns_exist_in_csv(csv_reader, name_field_names):
            error = "This file does not contain a name column"
        else:
            some_columns_exist = True
        if not columns_exist_in_csv(csv_reader, email_field_names):
            error = "This file does not contain an email column"
        else:
            some_columns_exist = True
        if not some_columns_exist:
            error = "Unable to read information from this CSV file.  Make sure it contains an email and name column"
        total_tos = 0
        total_skipped = 0
        initial_formset_data = []
        for row in csv_reader:
            bidder_number = extract_info(row, bidder_number_fields)
            email = extract_info(row, email_field_names)
            name = extract_info(row, name_field_names)
            phone = extract_info(row, phone_field_names)
            address = extract_info(row, address_field_names)
            is_club_member = extract_info(row, is_club_member_fields)
            if (email or name or phone or address) and total_tos <= self.max_users_that_can_be_added_at_once:
                if self.tos_is_in_auction(self.auction, name, email):
                    total_skipped += 1
                else:
                    total_tos += 1
                    initial_formset_data.append({
                        'bidder_number':bidder_number,
                        'name':name,
                        'phone':phone,
                        'email':email,
                        'address':address,
                        'is_club_member':is_club_member
                    })
        # this needs to be added to the session in order to persist when moving from POST (this csv processing) to GET
        self.request.session['initial_formset_data'] = initial_formset_data
        if total_tos >= self.max_users_that_can_be_added_at_once:
            messages.error(self.request, f"You can only add {self.max_users_that_can_be_added_at_once} users at once; run this again to add additional users.")
        if total_skipped:
            messages.info(self.request, f"{total_skipped} users are already in this auction (matched by email, or name if email not set) and do not appear below")
        if error:
            messages.error(self.request, error)
        if total_tos:
            self.extra_rows = total_tos
        # note that regardless of whether this is valid or not, we redirect to the same page after parsing the CSV file
        return redirect(reverse("bulk_add_users", kwargs={'slug': self.auction.slug}))

    def post(self, request, *args, **kwargs):
        csv_file = request.FILES.get('csv_file', None)
        if csv_file:
            return self.handle_csv_file(csv_file)
        self.instantiate_formset()
        tos_formset = self.AuctionTOSFormSet(self.request.POST, form_kwargs={'auction': self.auction, 'bidder_numbers_on_this_form':[]}, queryset=self.queryset)
        if tos_formset.is_valid():
            auctiontos = tos_formset.save(commit=False)
            for tos in auctiontos:
                tos.auction = self.auction
                tos.manually_added = True
                tos.save()
            messages.success(self.request, f'Added {len(auctiontos)} users')
            return redirect(reverse("auction_tos_list", kwargs={'slug': self.auction.slug}))
        self.tos_formset = tos_formset
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['formset'] = self.tos_formset
        context['helper'] = TOSFormSetHelper()
        context['auction'] = self.auction
        context['other_auctions'] = Auction.objects.exclude(is_deleted=True).filter(Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True)).distinct().order_by('-date_posted')[:10]
        return context

    def tos_is_in_auction(self, auction, name, email):
        """Return the tos if the name or email are already present in the auction, otherwise None"""
        qs = AuctionTOS.objects.filter(auction=auction)
        if email:
            qs = qs.filter(email=email)
        elif name:
            qs = qs.filter(
                Q(name=name, email=None)
                |
                Q(name=name, email=""))
        else:
            return None
        return qs.first()

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        if not self.auction:
            raise Http404
        if not self.auction.permission_check(self.request.user):
            messages.error(request, "Your account doesn't have permission to add users to this auction")
            return redirect("/")
        self.queryset = AuctionTOS.objects.none() # we don't want to allow editing
        return super().dispatch(request, *args, **kwargs)

    def instantiate_formset(self, *args, **kwargs):
        if not self.AuctionTOSFormSet:
            self.AuctionTOSFormSet = modelformset_factory(AuctionTOS, extra=self.extra_rows, fields = (
                'bidder_number',
                'name',
                'email',
                'phone_number',
                'address',
                'pickup_location',
                'is_club_member',
                ), form=QuickAddTOS)

class BulkAddLots(TemplateView, ContextMixin, AuctionPermissionsMixin):
    """Add/edit lots of lots for a given auctiontos pk"""
    template_name = "auctions/bulk_add_lots.html"
    allow_non_admins = True

    def get(self, *args, **kwargs):
        lot_formset = self.LotFormSet(form_kwargs={'tos':self.tos, 'auction': self.auction, 'custom_lot_numbers_used':[],'is_admin':self.is_admin}, queryset=self.queryset)
        helper = LotFormSetHelper()
        context = self.get_context_data(**kwargs)
        context['formset'] = lot_formset
        context['helper'] = helper
        return self.render_to_response(context)

    def post(self, *args, **kwargs):
        lot_formset = self.LotFormSet(self.request.POST, form_kwargs={'tos':self.tos, 'auction': self.auction, 'custom_lot_numbers_used':[], 'is_admin':self.is_admin}, queryset=self.queryset)
        if lot_formset.is_valid():
            lots = lot_formset.save(commit=False)
            for lot in lots:
                lot.auctiontos_seller = self.tos
                lot.auction = self.auction
                if self.tos.user:
                    lot.user = self.tos.user
                if not lot.description:
                    lot.description = ""
                if not lot.pk:
                    lot.added_by = self.request.user
                    if not self.is_admin:
                        # you are adding lots for yourself, set custom lot number automatically
                        lot.custom_lot_number = None
                        # we need to set lot.user here
                        if self.tos.user:
                            lot.user = self.tos.user
                lot.save()
            if lots:
                messages.success(self.request, f'Updated lots for {self.tos.name}')
                invoice, created = Invoice.objects.get_or_create(auctiontos_user=self.tos, auction=self.auction, defaults={})
                invoice.recalculate
            # when saving labels, it doesn't take you off from the page you're on
            # So we need to go somewhere, and then say "download labels"
            if "print" in str(self.request.GET.get('type', "")):
                print_url = f"printredirect={reverse('print_labels_by_bidder_number', kwargs={'slug': self.auction.slug, 'bidder_number': self.tos.bidder_number})}"
            else:
                print_url = ""
            if self.is_admin:
                redirect_url = reverse("auction_tos_list", kwargs={'slug': self.auction.slug})
                if print_url:
                    redirect_url += '?' + print_url
            else:
                redirect_url = reverse("selling")
                if print_url:
                    redirect_url += '?' + print_url
            return redirect(redirect_url)
            
        context = self.get_context_data(**kwargs)
        context['formset'] = lot_formset
        context['helper'] = LotFormSetHelper()
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tos'] = self.tos
        context['auction'] = self.auction
        context['is_admin'] = self.is_admin
        return context

    def dispatch(self, request, *args, **kwargs):
        
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop('slug')).first()
        self.is_admin = False
        if not self.auction:
            raise Http404
        bidder_number = kwargs.pop('bidder_number', None)
        self.tos = None
        if bidder_number:
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
        if self.is_auction_admin:
            self.is_admin = True
        if not self.tos:
            # if you don't got permission to edit this auction, you can only add lots for yourself
            self.tos = AuctionTOS.objects.filter(auction=self.auction).filter(Q(email=request.user.email)|Q(user=request.user)).first()
        if not self.tos:
            messages.error(request, f"You can't add lots until you join this auction")
            return redirect(f"/auctions/{self.auction.slug}/?next={reverse('bulk_add_lots_for_myself', kwargs={'slug': self.auction.slug})}")
        else:
            if not self.tos.selling_allowed and not self.is_admin:
                messages.error(request, f"You don't have permission to add lots to this auction")
                return redirect(f"/auctions/{self.auction.slug}/")
        if not self.is_admin and not self.auction.can_submit_lots:
            messages.error(request, f"Lot submission has ended for {self.auction}")
            return redirect(f"/auctions/{self.auction.slug}/")
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
            extra = 5 # default rows to show if max_lots_per_user is not set for this auction
        self.LotFormSet = modelformset_factory(Lot, extra=extra, fields = (
            'custom_lot_number',
            'lot_name',
            'species_category',
            'i_bred_this_fish',
            'quantity',
            'donation',
            'reserve_price',
            'buy_now_price',
            ), form=QuickAddLot)
        return super().dispatch(request, *args, **kwargs)

class ViewLot(DetailView):
    """Show the picture and detailed information about a lot, and allow users to place bids"""
    template_name = 'view_lot_images.html'
    model = Lot
    custom_lot_number = None
    auction_slug = None
    enable_404 = True

    def dispatch(self, request, *args, **kwargs):
        self.auction_slug = kwargs.pop('slug', None)
        self.custom_lot_number = kwargs.pop('custom_lot_number', None)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        obj = self.get_queryset().first()
        if not obj and self.enable_404:
            raise Http404
        return obj

    def get_queryset(self):
        pk = self.kwargs.get(self.pk_url_kwarg)
        # print(pk) # this will be set for /lots/1234/
        # print(self.auction_slug) # otherwise, these two will be set for /auctions/abc-def/lots/custom_number/
        # print(self.custom_lot_number)
        qs = Lot.objects.exclude(is_deleted=True)
        try:
            latitude = self.request.COOKIES['latitude']
            longitude = self.request.COOKIES['longitude']
            qs = qs.annotate(distance=distance_to(latitude, longitude))
        except:
            if self.request.user.is_authenticated:
                userData, created = UserData.objects.get_or_create(
                    user = self.request.user,
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
            qs = qs.filter(auction__isnull=False, auction__slug=self.auction_slug, custom_lot_number__isnull=False, custom_lot_number=self.custom_lot_number)
        return qs

    def get_context_data(self, **kwargs):
        lot = self.get_object()
        context = super().get_context_data(**kwargs)
        context['is_auction_admin'] = False
        if lot.auction:
            context['auction'] = lot.auction
            context['is_auction_admin'] = lot.auction.permission_check(self.request.user)
            if lot.auction.first_bid_payout and not lot.auction.invoiced:
                if not self.request.user.is_authenticated or not Bid.objects.filter(user=self.request.user, lot_number__auction=lot.auction):
                    messages.info(self.request, f"Bid on (and win) any lot in the {lot.auction} and get ${lot.auction.first_bid_payout} back!")
        try:
            defaultBidAmount = Bid.objects.get(user=self.request.user, lot_number=lot.pk).amount
            context['viewer_bid'] = defaultBidAmount
            defaultBidAmount = defaultBidAmount + 1
        except:
            defaultBidAmount = 0
            context['viewer_bid'] = None
        if not lot.sealed_bid:
            # reserve price if there are no bids
            if not lot.high_bidder:
                defaultBidAmount = lot.reserve_price 
            else:
                if defaultBidAmount > lot.high_bid:
                    pass
                else:
                    defaultBidAmount = lot.high_bid + 1
        context['viewer_pk'] = self.request.user.pk
        try:
            context['submitter_pk'] = lot.user.pk
        except:
            context['submitter_pk'] = 0
        context['user_specific_bidding_error'] = False
        if not self.request.user.is_authenticated:
            context['user_specific_bidding_error'] = True
        if context['viewer_pk'] == context['submitter_pk']:
            context['user_specific_bidding_error'] = True
        context['amount'] = defaultBidAmount
        context['watched'] = Watch.objects.filter(lot_number=lot.lot_number, user=self.request.user.id)
        context['category'] = lot.species_category
        #context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':lot.pk, "amount":defaultBidAmount}, request=self.request)
        context['user_tos'] = None
        context['user_tos_location'] = None
        if lot.auction and self.request.user.is_authenticated:
            tos = AuctionTOS.objects.filter(user=self.request.user, auction=lot.auction).first()
            if tos:
                context['user_tos'] = True
                context['user_tos_location'] = tos.pickup_location
            else:
                context['user_specific_bidding_error'] = True
        if lot.within_dynamic_end_time and lot.minutes_to_end > 0 and not lot.sealed_bid:
            messages.info(self.request, f"Bidding is ending soon.  Bids placed now will extend the end time of this lot.  This page will update automatically, you don't need to reload it")
        if not context['user_tos'] and not lot.ended and lot.auction:
            if lot.auction.allow_bidding_on_lots:
                messages.info(self.request, f"Please <a href='/auctions/{lot.auction.slug}/?next=/lots/{ lot.pk }/'>read the auction's rules and confirm your pickup location</a> to bid")
        if self.request.user.is_authenticated:
            userData, created = UserData.objects.get_or_create(
                user = self.request.user,
                defaults={},
            )
            userData.last_activity = timezone.now()
            userData.save()
            if userData.last_ip_address:
                if userData.last_ip_address != lot.seller_ip and lot.bidder_ip_same_as_seller:
                    messages.info(self.request, "Heads up: one of the bidders on this lot has the same IP address as the seller of this lot.  This can happen when someone is bidding on their own lots.  Never bid more than a lot is worth to you.")
        if lot.user:
            if lot.user.pk == self.request.user.pk:
                LotHistory.objects.filter(lot=lot.pk, seen=False).update(seen=True)
        context['bids'] = []
        if lot.auction:
            if context['is_auction_admin']:
                bids = Bid.objects.filter(lot_number=lot.pk)
                context['bids'] = bids
        context['debug'] = settings.DEBUG
        try:
            if lot.local_pickup:
                context['distance'] = f"{int(lot.distance)} miles away"
            else:
                distances = [25, 50, 100, 200, 300, 500, 1000, 2000, 3000]
                for distance in distances:
                    if lot.distance < distance:
                        context['distance'] = f"less than {distance} miles away"
                        break
                if lot.distance > 3000:
                    context['distance'] = f"over 3000 miles away"
        except:
            context['distance'] = 0
        # for lots that are part of an auction, it's very handy to show the exchange info right on the lot page
        # this should be visible only to people running the auction or the seller
        if lot.auction:
            if context['is_auction_admin'] or self.request.user == lot.user:
                if lot.sold:
                    context['showExchangeInfo'] = True
        context['show_image_add_button'] = lot.image_permission_check(self.request.user)
        # chat subscription stuff
        if self.request.user.is_authenticated:
            context['show_chat_subscriptions_checkbox'] = True
            context['autocheck_chat_subscriptions'] = 'false'
            existing_subscription = ChatSubscription.objects.filter(lot=lot, user=self.request.user).first()
            if lot.user and lot.user == self.request.user and not self.request.user.userdata.email_me_when_people_comment_on_my_lots:
                context['show_chat_subscriptions_checkbox'] = False
            if lot.user != self.request.user and not self.request.user.userdata.email_me_about_new_chat_replies:
                context['show_chat_subscriptions_checkbox'] = False
            if self.request.user.userdata.email_me_about_new_chat_replies and not existing_subscription:
                context['autocheck_chat_subscriptions'] = 'true'
            if existing_subscription:
                context['chat_subscriptions_is_checked'] = not existing_subscription.unsubscribed
                context['autocheck_chat_subscriptions'] = 'false'
            else:
                context['chat_subscriptions_is_checked'] = False
        return context

class ViewLotSimple(ViewLot):
    """Minimalist view of a lot, just image and description.  For htmx calls"""
    template_name = "view_lot_simple.html"
    enable_404 = False

    def get_context_data(self, **kwargs):
        context = DetailView.get_context_data(self, **kwargs)
        context['lot'] = self.get_object()
        return context

def createSpecies(name, scientific_name, category=False):
    """
    Create a new product/species
    This is really only called by the LotValidation class
    """
    if not category:
        # uncategorized
        category = Category.objects.get(id=21)
    if category.pk == 18 or category.pk == 19 or category.pk == 20 or category.pk == 21:
        # breeder points off for some things
        breeder_points = False
    else:
        breeder_points = True
    return Product.objects.create(
        common_name=name,
        scientific_name=scientific_name,
        breeder_points=breeder_points,
        category=category,
    )

class ImageCreateView(LoginRequiredMixin, CreateView):
    """Add an image to a lot"""
    model = LotImage
    template_name = 'image_form.html'
    form_class = CreateImageForm
    
    def dispatch(self, request, *args, **kwargs):
        try:
            self.lot = Lot.objects.get(lot_number=kwargs['lot'], is_deleted=False)
        except:
            raise Http404
        if not self.lot.image_permission_check(request.user):
            messages.error(request, "You can't add an image to this lot")
            return redirect(self.get_success_url())
        if self.lot.image_count > 5:
            messages.error(request, "You can't add another image to this lot.  Delete one and try again")
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return f'/lots/{self.lot.lot_number}/{self.lot.slug}/'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"Add image to {self.lot.lot_name}"
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = 'RANDOM'
        image.save()
        messages.success(self.request, "New image added")
        return super().form_valid(form)

class ImageUpdateView(UpdateView):
    """Edit an existing image"""
    model = LotImage
    template_name = 'image_form.html'
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
        context['title'] = f"Editing image for {self.get_object().lot_number.lot_name}"
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = 'RANDOM'
        image.save()
        messages.success(self.request, "Image updated")
        return super().form_valid(form)

class LotValidation(LoginRequiredMixin):
    """
    Base class for adding a lot.  This defines the rules for validating a lot
    """
    auction = None # used for specifying which auction via GET param
    
    def dispatch(self, request, *args, **kwargs):
        # if the user hasn't filled out their address, redirect:
        userData, created = UserData.objects.get_or_create(
            user = request.user.pk,
            defaults={},
        )
        if not userData.address or not request.user.first_name or not request.user.last_name:
            messages.error(self.request, "Please fill out your contact info before creating a lot")
            return redirect(f'/contact_info?next=/lots/new/')
            #return redirect(reverse("contact_info"))
        return super().dispatch(request, *args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()

    def form_valid(self, form, **kwargs):
        """
        There is quite a lot that needs to be done before the lot is saved
        """
        lot = form.save(commit=False)
        lot.user = self.request.user
        lot.date_of_last_user_edit = timezone.now()
        # # just in case someone is messing with the hidden fields
        # if form.cleaned_data['create_new_species']:
        #     lot.species = createSpecies(
        #         form.cleaned_data['new_species_name'],
        #         form.cleaned_data['new_species_scientific_name'],
        #         form.cleaned_data['new_species_category'])
        # if lot.species:
        #     lot.species_category = lot.species.category
        #     # if this is not breedable, remove the breeder points
        #     # they can still be added back in by editing the lot
        #     if not lot.species.breeder_points:
        #         lot.i_bred_this_fish = False
        # if lot.image and not lot.image_source:
        #     lot.image_source = 'RANDOM' # default to this pic is from the internet
        if lot.buy_now_price:
            if lot.buy_now_price < lot.reserve_price:
                lot.buy_now_price = lot.reserve_price
                messages.error(self.request, f"Buy now price can't be lower than the reserve price")
        if lot.auction:
            #if not lot.auction.is_online:
            #    if lot.buy_now_price or lot.reserve_price > lot.auction.minimum_bid:
            #        messages.info(self.request, f"Reserve and buy now prices may not be used in this auction.  Read the auction's rules for more information")
            if lot.auction.reserve_price == "disable":
                lot.reserve_price = lot.auction.minimum_bid
            if lot.auction.buy_now == "disable" and lot.buy_now_price:
                lot.buy_now_price = None
            if lot.auction.buy_now == "require" and not lot.buy_now_price:
                lot.buy_now_price = lot.auction.minimum_bid
                messages.error(self.request, f"You need to set a buy now price for this lot!")
            lot.date_end = lot.auction.date_end
            userData, created = UserData.objects.get_or_create(
                user = self.request.user.pk,
                defaults={},
            )
            userData.last_auction_used = lot.auction
            userData.last_activity = timezone.now()
            userData.save()
            auctiontos = AuctionTOS.objects.filter(user=self.request.user, auction=lot.auction).first()
            if not auctiontos:
                # it should not be possible to get here (famous last words...)
                # remember that on form submit in CreateLotForm.clean(), we are validating that the user has an auctiontos
                messages.error(self.request, f"You need to <a href='/auctions/{lot.auction.slug}'>confirm your pickup location for this auction</a> before people can bid on this lot.")
            else:
                lot.auctiontos_seller = auctiontos
                invoice, created = Invoice.objects.get_or_create(auctiontos_user=auctiontos, auction=lot.auction, defaults={})
                invoice.recalculate
        else:
            # this lot is NOT part of an auction
            try:
                run_duration = int(form.cleaned_data['run_duration'])
            except:
                run_duration = 10
            if not lot.date_posted:
                lot.date_posted = timezone.now()
            lot.date_end = lot.date_posted + datetime.timedelta(days=run_duration)
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
            if form.cleaned_data['cloned_from']:
                try:
                    originalLot = Lot.objects.get(pk=form.cleaned_data['cloned_from'], is_deleted=False)
                    if (originalLot.user.pk == self.request.user.pk) or self.request.user.is_superuser:
                        originalImages = LotImage.objects.filter(lot_number=originalLot.lot_number)
                        for originalImage in originalImages:
                            newImage = LotImage.objects.create(createdon=originalImage.createdon, lot_number=lot, image_source=originalImage.image_source, is_primary=originalImage.is_primary)
                            newImage.image = get_thumbnailer(originalImage.image)
                            # if the original lot sold, this picture sure isn't of the actual item
                            if originalLot.winner and originalImage.image_source == "ACTUAL":
                                newImage.image_source = 'REPRESENTATIVE'
                            newImage.save()
                        # we are only cloning images here, not watchers, views, or other related models
                except Exception as e:
                    print(e)
                    #pass
                
            # if there's another lot in the same category already with no bids, warn about it
            # existingLot = Lot.objects.annotate(num_bids=Count('bid'))\
            #     .filter(num_bids=0, species_category=lot.species_category, user=self.request.user.pk, active=True)\
            #     .exclude(lot_number=lot.pk)
            # if existingLot:
            #     messages.info(self.request, "Tip: you've already got lots in this category with no bids.  Don't submit too many similar lots unless you're sure there's interest")
            #messages.success(self.request, f"Created lot!  <a href='/lots/{lot.pk}'>View or edit your last lot</a> or fill out this form again to add another lot.  <a href='/lots/user/?user={self.request.user.pk}'>All submitted lots</a>")
            messages.success(self.request, f"Created lot!  You should probably <a href='/images/add_image/{lot.lot_number}/'>add an image to this lot.</a>  Or, <a href='/lots/new'>create another lot</a>")
        return super().form_valid(form)

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs['auction'] = self.auction
        kwargs['user'] = self.request.user
        kwargs['cloned_from'] = None
        try:
            data = self.request.GET.copy()
            if data['copy']:
                kwargs['cloned_from'] = data['copy']
        except:
            pass        
        return kwargs

class LotCreateView(LotValidation, CreateView):
    """
    Creating a new lot
    """    
    model = Lot
    template_name = 'lot_form.html'
    form_class = CreateLotForm 
    auction = None

    # it's better to take the user to the lot they just added, in case they want to edit it
    #def get_success_url(self):
    #    return "/lots/new/"
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = "New lot"
        context['new'] = True
        return context

    def form_valid(self, form, **kwargs):
        """When a new lot is created, make sure to create an invoice for the seller"""
        lot = form.save(commit=False)
        if lot.auction and lot.auctiontos_seller:
            invoice, created = Invoice.objects.get_or_create(auctiontos_user=lot.auctiontos_seller, auction=lot.auction, defaults={})
        return super().form_valid(form, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        userData, created = UserData.objects.get_or_create(
            user = self.request.user,
            defaults={},
        )
        if userData.last_auction_used:
            if userData.last_auction_used.can_submit_lots and not userData.last_auction_used.is_online:
                messages.info(request, f"Sick of adding lots one at a time?  <a href='{reverse('bulk_add_lots_for_myself', kwargs={'slug': userData.last_auction_used.slug})}'>Add lots of lots to {userData.last_auction_used}</a>")
        data = self.request.GET.copy()
        auction_slug = data.get('auction', None)
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
                    error = f"You haven't joined this auction yet.  Click the green button at the bottom of this page to join the auction.</a>"
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
    template_name = 'lot_form.html'
    form_class = CreateLotForm
    
    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            messages.error(request, "Only the lot creator can edit a lot")
            return redirect('/')
        if not self.get_object().can_be_edited:
            messages.error(request, self.get_object().cannot_be_edited_reason)
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)
    
    def get_success_url(self):
        return reverse("selling")
        #return f"/lots/{self.kwargs['pk']}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"Edit {self.get_object().lot_name}"
        return context

class AuctionDelete(DeleteView, AuctionPermissionsMixin):
    model = Auction
    
    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object()
        if not self.get_object().can_be_deleted:
            messages.error(request, "There are already lots in this auction, it can't be deleted")
            return redirect('/')
        if not self.is_auction_admin:
            messages.error(request, "Only the auction creator can delete an auction")
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return f"/auctions/"

class LotDelete(LoginRequiredMixin, DeleteView):
    model = Lot
    def dispatch(self, request, *args, **kwargs):
        if not self.get_object().can_be_deleted:
            messages.error(request, self.get_object().cannot_be_deleted_reason)
            return redirect('/')
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            messages.error(request, "Only the creator of a lot can delete it")
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return f"/lots/user/?user={self.request.user.pk}"

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
            return redirect(f'/lots/{self.get_object().lot_number.lot_number}/{self.get_object().lot_number.slug}/')
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        if self.get_object().is_primary:
            # in this case, we need to set a new primary image
            try:
                newImage = LotImage.objects.filter(lot_number=self.get_object().lot_number).exclude(pk=self.get_object().pk).order_by('createdon')[0]
                newImage.is_primary = True
                newImage.save()
            except:
                pass
        return f'/lots/{self.get_object().lot_number.lot_number}/{self.get_object().lot_number.slug}/'

class BidDelete(LoginRequiredMixin, DeleteView):
    model = Bid
    
    def dispatch(self, request, *args, **kwargs):
        auth = False
        if not self.get_object().lot_number.bids_can_be_removed:
            messages.error(request, "You can no longer remove bids from this lot.")
            return redirect(self.get_success_url())
        if request.user.is_superuser:
            auth = True
        if self.get_object().lot_number.auction:
            if self.get_object().lot_number.auction.permission_check(self.request.user):
                auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to remove bids from this lot")
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        lot = self.get_object().lot_number
        success_url = self.get_success_url()
        historyMessage = f"{request.user} has removed {self.get_object().user}'s bid"
        if lot.ended:
            lot.winner = None
            lot.auctiontos_winner = None
            lot.winning_price = None
            if lot.auction and lot.auction.date_end:
                lot.date_end = lot.auction.date_end
            else:
                lot.date_end = timezone.now() + datetime.timedelta(days=lot.lot_run_duration)
            lot.save()
        self.get_object().delete()
        LotHistory.objects.create(lot=lot, user=request.user, message=historyMessage, changed_price=True)
        return HttpResponseRedirect(success_url)

    def get_success_url(self):
        return f"/lots/{self.get_object().lot_number.pk}/{self.get_object().lot_number.slug}/"

class LotAdmin(TemplateView, FormMixin, AuctionPermissionsMixin):
    """Creation and management for Lots that are part of an auction"""
    template_name = "auctions/generic_admin_form.html"
    form_class = EditLot
    model = Lot

    def get_queryset(self):
        return Lot.objects.all()

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop('pk')
        try:
            self.lot = Lot.objects.get(pk=pk, is_deleted=False)
        except Exception as e:
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
        form_kwargs['auction'] = self.auction
        form_kwargs['lot'] = self.lot
        form_kwargs['user'] = self.request.user
        return form_kwargs
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tooltip'] = ""
        context['modal_title'] = f"Edit lot {self.lot.lot_number_display}"
        return context
        
    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            obj = self.lot
            obj.custom_lot_number = form.cleaned_data['custom_lot_number']
            obj.lot_name = form.cleaned_data['lot_name'] or "Unknown lot"
            obj.species_category = form.cleaned_data['species_category'] or 21 # uncategorized
            obj.description = form.cleaned_data['description']
            #obj.auctiontos_seller = form.cleaned_data['auctiontos_seller'] or request.user
            obj.quantity = form.cleaned_data['quantity'] or 1
            obj.donation = form.cleaned_data['donation']
            obj.i_bred_this_fish = form.cleaned_data['i_bred_this_fish']
            obj.reserve_price = form.cleaned_data['reserve_price']
            obj.buy_now_price = form.cleaned_data['buy_now_price']
            obj.banned = form.cleaned_data['banned']
            obj.auctiontos_winner = form.cleaned_data['auctiontos_winner']
            obj.winning_price = form.cleaned_data['winning_price']
            obj.save()
            if obj.auctiontos_winner:
                if self.lot_initial_winner != obj.auctiontos_winner:
                    obj.add_winner_message(self.request.user, obj.auctiontos_winner, obj.winning_price)
            return HttpResponse("<script>location.reload();</script>", status=200)
            #return HttpResponse("<script>closeModal();</script>", status=200)
        else:
            return self.form_invalid(form)

class AuctionTOSDelete(TemplateView, FormMixin, AuctionPermissionsMixin):
    """Delete AuctionTOSs"""
    template_name = "auctions/auctiontos_confirm_delete.html"
    form_class = DeleteAuctionTOS
    model = AuctionTOS

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop('pk')
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)
    
    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs['auction'] = self.auction
        form_kwargs['auctiontos'] = self.auctiontos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auctiontos'] = self.auctiontos
        context['tooltip'] = ""
        context['modal_title'] = f"Delete {self.auctiontos.name}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            success_url = reverse("auction_tos_list", kwargs={'slug': self.auctiontos.auction.slug})
            sold_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=self.auctiontos)
            won_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=self.auctiontos)
            if form.cleaned_data['delete_lots']:
                for lot in sold_lots:
                    lot.delete()
                for lot in won_lots:
                    LotHistory.objects.create(
                        lot = lot,
                        user = request.user,
                        message = f"{request.user.username} has removed {self.auctiontos} from this auction, this lot no longer has a winner.",
                        notification_sent = True,
                        bid_amount = 0,
                        changed_price=True,
                        seen=True
                    )
                    lot.auctiontos_winner = None
                    lot.winning_price = None
                    lot.active = True
                    lot.save()
            else:
                if form.cleaned_data['merge_with']:
                    new_auctiontos = AuctionTOS.objects.get(pk=form.cleaned_data['merge_with'])
                    invoice, created = Invoice.objects.get_or_create(auctiontos_user=new_auctiontos, auction=new_auctiontos.auction, defaults={})
                    for lot in sold_lots:
                        lot.auctiontos_seller = new_auctiontos
                        lot.save()
                    for lot in won_lots:
                        lot.auctiontos_winner = new_auctiontos
                        lot.save()
                        lot.add_winner_message(request.user, new_auctiontos, lot.winning_price)
                    invoice.recalculate
            # not needed if we have models.CASCADE on Invoice
            #invoices = Invoice.objects.filter(auctiontos_user=self.auctiontos)
            #for invoice in invoices:
            #    invoice.delete()
            self.auctiontos.delete()
            return redirect(success_url)
        else:
            return self.form_invalid(form)

class AuctionTOSAdmin(TemplateView, FormMixin, AuctionPermissionsMixin):
    """Creation and management for AuctionTOSs"""
    template_name = "auctions/generic_admin_form.html"
    form_class = CreateEditAuctionTOS
    model = AuctionTOS

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop('pk')
        self.is_edit_form = True
        try:
            self.auctiontos = AuctionTOS.objects.get(pk=pk)
        except Exception as e:
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
        form_kwargs['auction'] = self.auction
        form_kwargs['is_edit_form'] = self.is_edit_form
        form_kwargs['auctiontos'] = self.auctiontos
        return form_kwargs
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.is_edit_form and self.auction.is_online:
            context['tooltip'] = "This is an online auction: users should join through this site. You probably don't want to add them here."
        # context['new_form'] = CreateEditAuctionTOS(
        #     is_edit_form=self.is_edit_form,
        #     auctiontos=self.auctiontos,
        #     auction=self.auction
        # )
        context['unsold_lot_warning'] = ""
        if self.auctiontos:
            try:
                invoice = self.auctiontos.invoice
                invoice_string = invoice.invoice_summary_short
                context['top_buttons'] = render_to_string("invoice_buttons.html", {'invoice':invoice})
                context['unsold_lot_warning'] = invoice.unsold_lot_warning
            except:
                invoice = None
                invoice_string = ""
            context['modal_title'] = f"{self.auctiontos.name} {invoice_string}"
        else:
            context['modal_title'] = "Add new user"
        if self.auctiontos:
            context['invoice'] = self.auctiontos.invoice
        return context
        
    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            if self.auctiontos:
                obj = self.auctiontos
            else:
                obj = AuctionTOS.objects.create(
                    auction = self.auction,
                    pickup_location = form.cleaned_data['pickup_location'],
                    manually_added=True
                )
            obj.bidder_number = form.cleaned_data['bidder_number']
            obj.pickup_location = form.cleaned_data['pickup_location']
            obj.name = form.cleaned_data['name']
            obj.email = form.cleaned_data['email']
            obj.phone_number = form.cleaned_data['phone_number']
            obj.address = form.cleaned_data['address']
            obj.is_admin = form.cleaned_data['is_admin']
            obj.selling_allowed = form.cleaned_data['selling_allowed']
            obj.is_club_member = form.cleaned_data['is_club_member']
            obj.memo = form.cleaned_data['memo']
            obj.save()
            return HttpResponse("<script>location.reload();</script>", status=200)
            #return HttpResponse("<script>closeModal();</script>", status=200)
        else:
            name = form.cleaned_data.get("name")
            if not name:
                self.get_form().add_error('name', "Name is required")
            return self.form_invalid(form)

class AuctionCreateView(CreateView, LoginRequiredMixin):
    """
    Creating a new auction
    """    
    model = Auction
    template_name = 'auction_create_form.html'
    form_class = CreateAuctionForm
    redirect_url = None # really only used if this is a cloned auction
    cloned_from = None

    def get_success_url(self):
        if self.redirect_url:
            return self.redirect_url
        else:
            messages.success(self.request, "Auction created!  Now, create a location to exchange lots.")
            return reverse("create_auction_pickup_location", kwargs={'slug': self.object.slug})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = "New auction"
        context['new'] = True
        userData, created = UserData.objects.get_or_create(
            user = self.request.user,
            defaults={},
        )
        # a bit of logic used on auction_create_form.html to suggest auction names
        context['club'] = ""
        club = userData.club
        if club:
            context['club'] = str(club)
            if club.abbreviation:
                context['club'] = club.abbreviation
        return context

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs['user'] = self.request.user
        try:
            kwargs['user_timezone'] = self.request.COOKIES['user_timezone']
        except:
            kwargs['user_timezone'] = settings.TIME_ZONE
        kwargs['cloned_from'] = None
        try:
            data = self.request.GET.copy()
            if data['copy']:
                kwargs['cloned_from'] = data['copy']
                self.cloned_from = kwargs['cloned_from']
        except:
            pass
        return kwargs

    def form_valid(self, form, **kwargs):
        """Rules for new auction creation"""
        auction = form.save(commit=False)
        auction.created_by = self.request.user
        auction.promote_this_auction = False # all auctions start not promoted
        cloned_from = form.cleaned_data['cloned_from']
        auction.date_start = form.cleaned_data['date_start']
        is_online = True
        clone_from_auction = None
        if 'clone' in str(self.request.GET):
            try:
                original_auction = Auction.objects.get(slug=cloned_from, is_deleted=False)
                if original_auction:
                    # you still don't get to clone auctions that aren't yours...
                    if original_auction.permission_check(self.request.user):
                        clone_from_auction = original_auction
            except Exception as e:
                pass #print(e)
        elif 'online' in str(self.request.GET):
            is_online = True
        else:
            is_online = False
        run_duration = timezone.timedelta(days=7) # only set for is_online
        if clone_from_auction:
            fields_to_clone = ['is_online', 
                'notes',
                'lot_entry_fee',
                'unsold_lot_fee',
                'winning_bid_percent_to_club',
                'first_bid_payout',
                'sealed_bid',
                'max_lots_per_user',
                'allow_additional_lots_as_donation',
                'make_stats_public',
                'use_categories',
                'bump_cost',
                'is_chat_allowed',
                'lot_promotion_cost',
                'code_to_add_lots',
                'allow_bidding_on_lots',
                'pre_register_lot_discount_percent',
                'only_approved_sellers',
                'email_users_when_invoices_ready',
                'invoice_payment_instructions',
                'minimum_bid', 
                'winning_bid_percent_to_club_for_club_members',
                'lot_entry_fee_for_club_members',
                'set_lot_winners_url',
                'require_phone_number',
                'buy_now',
                'reserve_price',
                'tax',
                ]
            for field in fields_to_clone:
                setattr(auction, field, getattr(original_auction, field))
            if original_auction.date_end:
                run_duration = original_auction.date_end - original_auction.date_start
            auction.cloned_from = original_auction
        else:
            auction.is_online = is_online
            if not is_online:
                auction.allow_bidding_on_lots = False
                auction.buy_now = 'disable'
                auction.reserve_price = 'disable'
        if not auction.notes:
            auction.notes = "## General information\n\nYou should remove this line and edit this section to suit your auction.  Use the formatting here as an example.\n\n## Prohibited items\n- You cannot sell anything banned by state law.\n\n## Rules\n- All lots must be properly bagged.  No leaking bags!\n- You do not need to be a club member to buy or sell lots."
        if auction.is_online:
            auction.date_end = auction.date_start + run_duration
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_end
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start
        else:
            auction.date_end = None
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_start
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start - run_duration
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
                location.pk = None # duplicate all fields
                if location.name == str(clone_from_auction):
                    location.name = str(auction)
                location.auction = auction
                auction_time = clone_from_auction.date_start
                if clone_from_auction.date_end:
                    auction_time = clone_from_auction.date_end
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
            # we are only cloning pickup locations here, no other models (AuctionTOS would be the first one that comes to mind)
        return super().form_valid(form)

class AuctionInfo(FormMixin, DetailView, AuctionPermissionsMixin):
    """Main view of a single auction"""
    template_name = 'auction.html'
    model = Auction
    form_class = AuctionJoin
    rewrite_url = None
    auction = None
    allow_non_admins = True

    def get_object(self, *args, **kwargs):
        if self.auction:
            return self.auction
        else:
            try:
                auction = Auction.objects.get(slug=self.kwargs.get(self.slug_url_kwarg), is_deleted=False)
                self.auction = auction
                return auction
            except:
                raise Http404("No auctions found matching the query")

    def get_success_url(self):
        data = self.request.GET.copy()
        try:
            if not data['next']:
                data['next'] = self.get_object().view_lot_link
            return data['next']
        except Exception as e:
            return self.get_object().view_lot_link

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs['user'] = self.request.user
        form_kwargs['auction'] = self.get_object()
        return form_kwargs

    # def dispatch(self, request, *args, **kwargs):
    #     if self.get_object().permission_check(request.user):
    #         locations = self.get_object().location_qs.count()
    #         if not locations:
    #             messages.info(self.request, "You haven't added any pickup locations to this auction yet. <a href='/locations/new/'>Add one now</a>")
    #     return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['pickup_locations'] = self.get_object().location_qs
        current_site = Site.objects.get_current()
        context['domain'] = current_site.domain
        context['google_maps_api_key'] = settings.LOCATION_FIELD['provider.google.api_key']
        if self.get_object().closed:
            context['ended'] = True
            messages.info(self.request, f"This auction has ended.  You can't bid on anything, but you can still <a href='{self.get_object().view_lot_link}'>view lots</a>.")
        else:
            context['ended'] = False
        try:
            existingTos = AuctionTOS.objects.get(user=self.request.user, auction=self.get_object())
            existingTos = existingTos.pickup_location
            i_agree = True
            context['hasChosenLocation'] = existingTos.pk
        except:
            context['hasChosenLocation'] = False
            # if not self.get_object().no_location:
            #     # this selects the first location in multi-location auction as the default
            #     existingTos = PickupLocation.objects.filter(auction=self.get_object().pk)[0]
            # else:
            existingTos = None
            if self.get_object().multi_location:
                i_agree = True
            else:
                i_agree = False
                existingTos = PickupLocation.objects.filter(auction=self.get_object()).first()
            # if self.request.user.is_authenticated and not context['ended']:
            #     if not self.get_object().no_location:
            #         messages.add_message(self.request, messages.ERROR, "Please confirm you have read these rules by selecting your pickup location at the bottom of this page.")
        context['active_tab'] = 'main'
        if self.request.user.pk == self.get_object().created_by.pk:
            invalidPickups = self.get_object().pickup_locations_before_end
            if invalidPickups:
                messages.info(self.request, f"<a href='{invalidPickups}'>Some pickup times</a> are set before the end date of the auction")
        
        context['form'] = AuctionJoin(user=self.request.user, auction=self.get_object(), initial={'user': self.request.user.id, 'auction':self.get_object().pk, 'pickup_location':existingTos, "i_agree": i_agree})
        context['rewrite_url'] = self.rewrite_url
        return context

    def post(self, request, *args, **kwargs):
        auction = self.get_object()
        form = self.get_form()
        if form.is_valid():
            userData, created = UserData.objects.get_or_create(
                user = self.request.user,
                defaults={},
            )
            if auction.require_phone_number and not userData.phone_number:
                messages.error(self.request, f"This auction requires a phone number before you can join")
                return redirect(f'/contact_info?next={auction.get_absolute_url()}')
            find_by_email = AuctionTOS.objects.filter(
                email=self.request.user.email,
                auction=auction
                #manually_added=True,
                #user__isnull=True
                ).first()
            if find_by_email:
                obj = find_by_email
                obj.user = self.request.user
            else:
                obj, created = AuctionTOS.objects.get_or_create(
                    user = self.request.user,
                    auction = auction,
                    defaults={'pickup_location': form.cleaned_data['pickup_location']},
                )
            obj.pickup_location = form.cleaned_data['pickup_location']
            # check if mail was chosen
            if obj.pickup_location.pickup_by_mail:
                if not userData.address:
                    messages.error(self.request, f"You have to set your address before you can choose pickup by mail")
                    return redirect(f'/contact_info?next={auction.get_absolute_url()}')
            if form.cleaned_data['time_spent_reading_rules'] > obj.time_spent_reading_rules:
                obj.time_spent_reading_rules = form.cleaned_data['time_spent_reading_rules']
            # even if an auctiontos was originally manually added, if the user clicked join, mark them as not manually added
            obj.manually_added = False
            # fill out some information in the tos if not already filled out
            if not obj.name:
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
            return self.form_valid(form)
        else:
            #print(form.cleaned_data)
            return self.form_invalid(form)

class FAQ(AdminEmailMixin, ListView):
    """Show all questions"""
    model = FAQ
    template_name = 'faq.html'
    ordering = ['category_text']
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_site = Site.objects.get_current()
        context['domain'] = current_site.domain
        return context
        

def aboutSite(request):
    return render(request,'about.html')

class PromoSite(TemplateView):
    template_name = "promo.html"
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['hide_google_login'] = True
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
                return PromoSite.as_view()(request)
        try:
            # Did the user sign the tos yet?
            AuctionTOS.objects.get(user=request.user, auction=auction)
            # If so, redirect them to the lot view
            return AllLots.as_view(rewrite_url=f'/?{auction.slug}', auction=auction, routeByLastAuction=routeByLastAuction)(request)
        except Exception as e:
            # No tos?  Take them there so they can sign
            return AuctionInfo.as_view(rewrite_url=f'/?{auction.slug}', auction=auction)(request)

    data = request.GET.copy()
    routeByLastAuction = False
    try:
        userData, created = UserData.objects.get_or_create(
            user = request.user,
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
        #return tos_check(request, auction, routeByLastAuction)
    except Exception as e:
        # if not, check and see if the user has been participating in an auction
        try:
            auction = UserData.objects.get(user=request.user).last_auction_used
            if timezone.now() > auction.date_end:
                try:
                    invoice = Invoice.objects.get(auctiontos_user__user=request.user, auction=auction)
                    messages.info(request, f'{auction} has ended.  <a href="/invoices/{invoice.pk}">View your invoice</a>, <a href="/feedback/">leave feedback</a> on lots you bought or sold, or <a href="/lots?auction={auction.slug}">view lots</a>')
                    return redirect("/lots/")
                except:
                    pass
                auction = None
            else:
                try:
                    # in progress online auctions get routed
                    AuctionTOS.objects.get(user=request.user, auction=auction, auction__is_online=True)
                    # only show the banner if the TOS is signed
                    #messages.add_message(request, messages.INFO, f'{auction} is the last auction you joined.  <a href="/lots/">View all lots instead</a>')
                    routeByLastAuction = True
                except:
                    pass
        except:
            # probably no userdata or userdata.auction is None
            auction = None
    return tos_check(request, auction, routeByLastAuction)

@login_required
def toAccount(request):
    #response = redirect(f'/users/{request.user.username}/')
    return redirect(reverse("userpage", kwargs={'slug': request.user.username}))

class allAuctions(ListView):
    model = Auction
    template_name = 'all_auctions.html'
    ordering = ['-date_end']
    
    def get_queryset(self):
        qs = Auction.objects.exclude(is_deleted=True).order_by('-date_start')
        next_90_days = timezone.now() + datetime.timedelta(days=90)
        two_years_ago = timezone.now() - datetime.timedelta(days=365*2)
        standard_filter = Q(promote_this_auction=True, date_start__lte=next_90_days, date_posted__gte=two_years_ago)
        latitude = 0
        longitude = 0
        try:
            latitude = self.request.COOKIES['latitude']
            longitude = self.request.COOKIES['longitude']
        except:
            if self.request.user.is_authenticated:
                userData, created = UserData.objects.get_or_create(
                    user = self.request.user,
                    defaults={},
                )
                latitude = userData.latitude
                longitude = userData.longitude
        if latitude and longitude:
            closest_pickup_location_subquery = PickupLocation.objects.filter(
                auction=OuterRef('pk')
            ).annotate(
                distance=distance_to(latitude, longitude)
            ).order_by('distance').values('distance')[:1]
            qs = qs.annotate(
                distance=Subquery(closest_pickup_location_subquery)
                )
        if self.request.user.is_superuser:
            return qs
        if not self.request.user.is_authenticated:
            return qs.filter(standard_filter)
        return qs.filter(Q(auctiontos__user=self.request.user)|
            Q(auctiontos__email=self.request.user.email)|
            Q(created_by=self.request.user)|
            standard_filter
            ).distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            self.request.COOKIES['longitude']
        except:
            if self.request.user.is_authenticated:
                context['location_message'] = "Set your location to get notifications about new auctions near you"
            else:
                context['location_message'] = "Set your location to see how far away auctions are"
        return context

class Leaderboard(ListView):
    model = UserData
    template_name = 'leaderboard.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['total_lots'] = UserData.objects.filter(rank_total_lots__isnull=False).order_by('rank_total_lots')
        context['unique_species'] = UserData.objects.filter(number_unique_species__isnull=False).order_by('rank_unique_species')
        #context['total_spent'] = UserData.objects.filter(rank_total_spent__isnull=False).order_by('rank_total_spent')
        context['total_bids'] = UserData.objects.filter(rank_total_bids__isnull=False).order_by('rank_total_bids')
        return context

class AllLots(LotListView, AuctionPermissionsMixin):
    """Show all lots"""
    rewrite_url = None # use JS to rewrite the shown URL.  This is used only for auctions.
    auction = None
    allow_non_admins = True

    def render_to_response(self, context, **response_kwargs):
        """override the default just to add a cookie -- this will allow us to save ordering for subsequent views"""
        response = super().render_to_response(context, **response_kwargs)
        try:
            response.set_cookie('lot_order', self.ordering)
        except:
            pass
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        data = self.request.GET.copy()
        can_show_unloved_tip = True
        self.ordering = "" # default ordering is set in LotFilter.__init__
        # I don't love having this in two places, but it seems necessary
        if self.request.GET.get('page'):
            del data['page'] # required for pagination to work
        if 'order' in data:
            self.ordering = data['order']
        else:
            if 'lot_order' in self.request.COOKIES:
                data['order'] = self.request.COOKIES['lot_order']
                self.ordering = data['order']
        if self.ordering == 'unloved':
            can_show_unloved_tip = False
            if randint(1, 10) > 9:
                # we need a gentle nudge to remind people not to ALWAYS sort by least popular
                context['search_button_tooltip'] = "Sorting by least popular"
        if not context['auction']:
            context['auction'] = self.auction
        else:
            self.auction = context['auction']
        if self.auction:
            context['is_auction_admin'] = self.is_auction_admin
            if self.auction.minutes_to_end < 1440 and self.auction.minutes_to_end > 0 and can_show_unloved_tip:
                context['search_button_tooltip'] = "Try sorting by least popular to find deals!"
        if self.rewrite_url:
            if 'auction' not in data and 'q' not in data:
                context['rewrite_url'] = self.rewrite_url
        if 'q' in data:
            if data['q']:
                user = None
                if self.request.user.is_authenticated:
                    user = self.request.user
                SearchHistory.objects.create(user=user, search=data['q'], auction=self.auction)
        context['view'] = 'all'
        context['filter'] = LotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=True, regardingAuction = self.auction)
        return context

class Invoices(ListView, LoginRequiredMixin):
    """Get all invoices for the current user"""
    model = Invoice
    template_name = 'all_invoices.html'
    ordering = ['-date']
    
    def get_queryset(self):
        qs = Invoice.objects.filter(
            Q(auctiontos_user__user=self.request.user)|
            Q(auctiontos_user__email=self.request.user.email)
        ).order_by('-date')
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context

# password protected in views.py
class InvoiceView(DetailView, FormMixin, AuctionPermissionsMixin):
    """Show a single invoice"""
    template_name = 'invoice.html'
    model = Invoice
    #form_class = InvoiceUpdateForm
    form_view = 'opened' # expects opened or printed, this field will be set to true when the user the invoice is for opens it
    allow_non_admins = True
    authorized_by_default = False

    def get_object(self):
        """"""
        try:
            return Invoice.objects.get(pk=self.kwargs.get(self.pk_url_kwarg))
        except:
            if self.request.user.is_authenticated:
                return Invoice.objects.filter(
                    auctiontos_user__user = self.request.user,
                    auction__slug = self.kwargs['slug']
                ).first()
        return None
            
    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        auth = self.authorized_by_default
        self.is_admin = False
        invoice = self.get_object()
        if not invoice:
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=self.kwargs['slug']).first()
            if auction:
                messages.error(request, "You don't have an invoice for this auction yet.  Your invoice will be created once you buy or sell lots in this auction.")
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
            messages.error(request, "Your account doesn't have permission to view this invoice. Are you signed in with the correct account?")
            return redirect('/')
        if mark_invoice_viewed_by_user:
            setattr(invoice, self.form_view, True) # this will set printed or opened as appropriate
            invoice.save()
        self.InvoiceAdjustmentFormSet = modelformset_factory(InvoiceAdjustment, extra=1,
            can_delete=True,
            form=InvoiceAdjustmentForm)
        self.queryset = invoice.adjustments
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        invoice = self.get_object()
        context = {}
        context['auction'] = self.auction
        context['is_admin'] = self.is_admin
        context['invoice'] = invoice
        # light theme for some invoices to allow printing
        if 'print' in self.request.GET.copy():
            context['base_template_name'] = "print.html"
            context['show_links'] = False
        else:
            context['base_template_name'] = "base.html"
            context['show_links'] = True
        context['location'] = invoice.location
        context['print_label_link'] = None
        if invoice.auction.is_online:
            context['print_label_link'] = reverse("print_labels_by_bidder_number", kwargs={'slug': invoice.auction.slug, 'bidder_number': invoice.auctiontos_user.bidder_number})
        context['is_auction_admin'] = self.is_auction_admin
        return context
   
    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        adjustment_formset = self.InvoiceAdjustmentFormSet(self.request.POST, form_kwargs={'invoice':self.get_object()}, queryset=self.queryset)
        if adjustment_formset.is_valid() and self.is_admin:
            adjustments = adjustment_formset.save(commit=False)
            for adjustment in adjustments:
                adjustment.invoice = self.get_object()
                adjustment.user = request.user
                adjustment.save()
            if adjustments:
                messages.success(self.request, f'Invoice adjusted')
            for form in adjustment_formset.deleted_forms:
                if form.instance.pk:
                    form.instance.delete()
            return redirect(reverse('invoice_by_pk', kwargs={'pk': self.get_object().pk}))
        context = self.get_context_data(**kwargs)
        context['formset'] = adjustment_formset
        context['helper'] = InvoiceAdjustmentFormSetHelper()
        return self.render_to_response(context)

    def get(self, request, *args, **kwargs):
        if self.get_object().unsold_lot_warning and self.is_auction_admin:
            messages.info(self.request, "This user still has unsold lots, make sure to sell all non-donation lots before marking this ready or paid.")
        self.object = self.get_object()
        invoice_adjustment_formset = self.InvoiceAdjustmentFormSet(form_kwargs={'invoice':self.get_object()}, queryset=self.queryset)
        helper = InvoiceAdjustmentFormSetHelper()
        context = self.get_context_data(object=self.object)
        context['formset'] = invoice_adjustment_formset
        context['helper'] = helper
        # recaluclating slows things down, 
        # I am not sure if it's a good idea to have it here or not
        self.object.recalculate
        return self.render_to_response(context)

class InvoiceNoLoginView(InvoiceView):
    """Enter a uuid, go to your invoice.  This bypasses the login checks"""
    # need a template with a popup
    authorized_by_default = True
    form_view = 'opened'

    def get_object(self):
        if not self.uuid:
            raise Http404
        invoice = Invoice.objects.filter(no_login_link=self.uuid).first()
        if invoice:
            return invoice
        else:
            raise Http404

    def dispatch(self, request, *args, **kwargs):
        self.uuid = kwargs.get('uuid', None)
        invoice = self.get_object()
        invoice.opened = True
        invoice.save()
        return super().dispatch(request, *args, **kwargs)

class LotLabelView(View, AuctionPermissionsMixin):
    """View and print labels for an auction"""
    
    # these are defined in urls.py and used in get_object(), below
    bidder_number = None
    username = None
    template_name = 'invoice_labels.html'
    allow_non_admins = True
    filename = "" # this will be automatically generated in dispatch

    def get_queryset(self):
        return self.tos.print_labels_qs

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs['slug']).first()
        self.bidder_number = kwargs.pop('bidder_number', None)
        self.username = kwargs.pop('username', None)
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
                messages.error(request, "You haven't joined this auction yet.  You need to join this auction and add lots before you can print labels.")
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
                messages.error(request, "This is an online auction; you should print your labels after the auction ends, and before you exchange lots.")
                return redirect(self.auction.get_absolute_url())
        if checks_pass and self.tos:
            if not self.get_queryset():
                if printing_for_self:
                    messages.error(request, "You don't have any lots with printable labels in this auction.")
                else:
                    messages.error(request, "There aren't any lots with printable labels")
                return redirect(self.auction.get_absolute_url())
            return super().dispatch(request, *args, **kwargs)
        else:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect('/')

    def get_context_data(self, **kwargs):
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        context = {}
        context['empty_labels'] = user_label_prefs.empty_labels
        context['print_qr']=True
        if user_label_prefs.preset == "sm":
            # Avery 5160 labels
            context['page_width'] = 8.5
            context['page_height'] = 11
            context['label_width'] = 2.5
            context['label_height'] = 0.96
            context['label_margin_right'] = 0.2
            context['label_margin_bottom'] = 0.03
            context['page_margin_top'] = 0.6
            context['page_margin_bottom'] = 0.1
            context['page_margin_left'] = 0.16
            context['page_margin_right'] = 0.1
            context['font_size'] = 10
            context['unit'] = 'in'
        elif user_label_prefs.preset == "lg":
            # Avery 18262 labels
            context['page_width'] = 8.5
            context['page_height'] = 11
            context['label_width'] = 3.9
            context['label_height'] = 1.2
            context['label_margin_right'] = 0.2
            context['label_margin_bottom'] = 0.125
            context['page_margin_top'] = 0.88
            context['page_margin_bottom'] = 0.6
            context['page_margin_left'] = 0.19
            context['page_margin_right'] = 0.1
            context['font_size'] = 14
            context['unit'] = 'in'
        elif user_label_prefs.preset == "thermal_sm":
            # thermal label printer 3x2
            context['page_width'] = 3
            context['page_height'] = 2
            context['label_width'] = 2.6
            context['label_height'] = 1.4
            context['label_margin_right'] = .1
            context['label_margin_bottom'] = .1
            context['page_margin_top'] = .1
            context['page_margin_bottom'] = .1
            context['page_margin_left'] = .1
            context['page_margin_right'] = .1
            context['font_size'] = 14
            context['unit'] = 'in'
            context['print_qr'] = False
        else:
            context.update({f'{field.name}': getattr(user_label_prefs, field.name) for field in UserLabelPrefs._meta.get_fields()})
        return context

    def create_labels(self, request, *args, **kwargs):
        context = self.get_context_data(kwargs=kwargs)
        response = HttpResponse(content_type='application/pdf')
        label_name = self.filename or "labels"
        label_name = re.sub(r'[^a-zA-Z0-9]', '_', label_name.lower())
        response['Content-Disposition'] = f'attachment; filename="{label_name}.pdf"'
        label_width = context.get('label_width')
        label_height = context.get('label_height')
        label_margin_right = context.get('label_margin_right')
        margin_bottom = context.get('label_margin_bottom')
        page_margin_top = context.get('page_margin_top')
        page_margin_bottom = context.get('page_margin_bottom')
        page_margin_left = context.get('page_margin_left')
        page_margin_right = context.get('page_margin_right')
        unit = context.get('unit') or 'in'
        font_size = context.get("font_size")
        page_width = context.get('page_width')
        page_height = context.get('page_height')
        empty_labels = context['empty_labels']
        labels = self.get_queryset()
        if unit == 'in':
            unit = inch
        else:
            unit = cm
        doc = SimpleDocTemplate(response, pagesize=[page_width*unit, page_height*unit], leftMargin=page_margin_left*unit, rightMargin=page_margin_right*unit, topMargin=page_margin_top*unit, bottomMargin=page_margin_bottom*unit)
        elements = []
        # remove margins from page width
        page_width = page_width*unit - page_margin_left*unit - page_margin_right*unit
        # each label is broken into 3 parts, with a seperate cell for each:
        # first cell
        if context['print_qr']:
            qr_code_width = label_width*unit/4
            if qr_code_width > label_height*unit:
                qr_code_width = label_height*unit
            if label_height*unit > qr_code_width:
                qr_code_height = qr_code_width
            else:
                qr_code_height = label_height*unit
        else:
            qr_code_width = 0
        # second cell
        text_area_width = label_width*unit - qr_code_width
        # third cell
        margin_right_width = label_margin_right*unit
        # total width of each label is the sum of all 3 cells
        column_width = qr_code_width + text_area_width + margin_right_width
        # row height is the same for all 3 parts
        row_height = (label_height + margin_bottom)*unit
        num_cols = int(page_width / column_width)
        labels_row = []
        table_data = []
        style = ParagraphStyle(name='Normal', fontName='Helvetica', fontSize=font_size, leading=font_size*1.3)
        if empty_labels:
            labels = ['empty'] * empty_labels + list(labels)
        for i, label in enumerate(labels):
            if label == "empty":
                labels_row += [[Paragraph('', style), Paragraph('', style), Paragraph('', style)]]*3
            else:
                # currently, we are not trimming the text to fit on a single row
                # this means that lots with a long label_line_1 will spill over onto 2 rows
                # we could trim the length to [:20] in the model or here to "fix" this, but it's not a huge problem IMHO
                if context['print_qr']:
                    label_qr_code = qr_code.qrcode.maker.make_qr_code_image(label.qr_code, QRCodeOptions(size='T', border=4, error_correction='L', image_format="png",))
                    image_stream = BytesIO(label_qr_code)
                    label_qr_code_cell = PImage(image_stream, width=qr_code_width, height=qr_code_height, lazy=0, hAlign="LEFT")
                    labels_row.append([label_qr_code_cell])
                else:
                    labels_row.append([Paragraph('', style)]) # margin left cell is empty
                label_text_cell = Paragraph(f"{label.label_line_0}<br />{label.label_line_1}<br />{label.label_line_2}<br />{label.label_line_3}", style)
                labels_row.append([label_text_cell])
                labels_row.append([Paragraph('', style)]) # margin right cell is empty
            
            # Check if the current label is the last label in the current row or the last label in the list
            if (i+1) % num_cols == 0 or i == len(labels) - 1:
                # print(f'we have reached the end, {len(labels)} in total')
                # Check if the current label is the last label in the list and labels_row is not full
                if i == len(labels) - 1 and len(labels_row) < num_cols*3:
                    # Add empty elements to the labels_row list until it is filled
                    # print(f"adding {(num_cols*3) - len(labels_row)} extra labels, *3 total columns added")
                    labels_row += [[Paragraph('', style), Paragraph('', style), Paragraph('', style)]]*((num_cols*3) - len(labels_row))
                table_data.append(labels_row)
                labels_row = []
            if label != "empty":
                label.label_printed = True
                label.save()
        col_widths = []
        for i in range(num_cols):
            col_widths += [qr_code_width,text_area_width,margin_right_width]
        table = Table(table_data, colWidths=col_widths, rowHeights=row_height)
        table.setStyle([
            #('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP')
        ])
        elements.append(table)
        doc.build(elements)
        return response

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return self.create_labels(request, *args, **kwargs)
        else:
            try:
                return self.create_labels(request, *args, **kwargs)
            #except LayoutError: # some day I will track down all the possible error types and add them here
            except:
                messages.error(request, "Unable to print labels, this is likely caused by an invalid custom setting here")
                return redirect(reverse('printing'))

class UnprintedLotLabelsView(LotLabelView):
    """Print lot labels, but only ones that haven't already been printed"""
    def get_queryset(self):
        return self.tos.unprinted_labels_qs

class SingleLotLabelView(LotLabelView):
    """Reprint labels for just one lot"""

    def get_queryset(self):
        return Lot.objects.filter(pk=self.lot.pk)

    def dispatch(self, request, *args, **kwargs):
        self.lot = get_object_or_404(Lot, pk=kwargs.pop('pk'), is_deleted=False)
        if self.lot.auctiontos_seller and self.lot.auctiontos_seller.user is not request.user:
            self.auction = self.lot.auctiontos_seller.auction
            if not self.is_auction_admin:
                messages.error(request, "You can't print lables for other people's lots unless you are an admin")
                return redirect("/")
        if not self.lot.auctiontos_seller:
            if self.lot.user and self.lot.user is not request.user:
                messages.error(request, "You can only print labels for your own lots")
                return redirect("/")
        # super() would try to find an auction
        return View.dispatch(self, request, *args, **kwargs)

@login_required
def getClubs(request):
    if request.method == 'POST':
        species = request.POST['search']
        result = Club.objects.filter(
            Q(name__icontains=species) | Q(abbreviation__icontains=species)
            ).values('id','name', 'abbreviation')
        return JsonResponse(list(result), safe=False)

class InvoiceBulkUpdateStatus(TemplateView, FormMixin, AuctionPermissionsMixin):
    """Change invoice statuses in bulk"""
    template_name = "auctions/generic_admin_form.html"
    form_class = ChangeInvoiceStatusForm
    show_checkbox = False

    def get_queryset(self):
        return Invoice.objects.filter(auctiontos_user__auction=self.auction, status=self.old_invoice_status)

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop('slug'), is_deleted=False)
        self.is_auction_admin
        self.invoice_count = self.get_queryset().count()
        return super().dispatch(request, *args, **kwargs)
    
    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs['auction'] = self.auction
        form_kwargs['invoice_count'] = self.invoice_count
        if self.invoice_count:
            form_kwargs['show_checkbox'] = self.show_checkbox
        else:
            form_kwargs['show_checkbox'] = False
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
        return HttpResponse("<script>location.reload();</script>", status=200)

class MarkInvoicesReady(InvoiceBulkUpdateStatus):
    old_invoice_status = "DRAFT"
    new_invoice_status = "UNPAID"
    show_checkbox = True

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.auction.email_users_when_invoices_ready = form.cleaned_data.get("send_invoice_ready_notification_emails")
            self.auction.save()
        return super().post(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tooltip'] = f"Changing the invoice status to ready will block users from bidding.  You should make any needed adjustments before setting invoices to ready.  This will change the status of {self.invoice_count} invoices, and allow you to export them to PayPal."
        if not self.auction.closed and self.auction.is_online:
            context['unsold_lot_warning'] = "Don't set invoices ready yet!  This auction hasn't ended."
            if not self.auction.minutes_to_end:
                active_lot_count = self.auction.lots_qs.filter(active=True).count()
                context['unsold_lot_warning'] += f" There are still {active_lot_count} lots with last-minute bids on them"
        if not self.auction.is_online:
            context['unsold_lot_warning'] = "You usually don't need to use this.  Set people's invoices to paid one at a time, as people leave the auction."
        if not self.invoice_count:
            context['modal_title'] = "No open invoices"
            context['tooltip'] = "There aren't any open invoices in this auction.  Invoices are created automatically whenever a user buys or sells a lot."
        else:
            context['modal_title'] = f"Set {self.invoice_count} open invoices to ready"
        return context
    
class MarkInvoicesPaid(InvoiceBulkUpdateStatus):
    old_invoice_status = "UNPAID"
    new_invoice_status = "PAID"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        unchanged_invoices = Invoice.objects.filter(auctiontos_user__auction=self.auction, status="DRAFT").count()
        context['unsold_lot_warning'] = "You probably don't need this.  Instead, set invoices to paid one at a time, as users pay them."
        context['tooltip'] = ""
        if unchanged_invoices:
            context['tooltip'] += f" There are {unchanged_invoices} open invoices in this auction that will not be changed; set them ready first if you want to change them."
        context['tooltip'] += f" This will set {self.invoice_count} ready invoices to paid."
        if not self.invoice_count:
            context['modal_title'] = "No ready invoices"
            if unchanged_invoices:
                context['tooltip'] = f"There's {unchanged_invoices} invoices that are still open.  You should set open invoices to ready before using this."
        else:
            context['modal_title'] = f"Set {self.invoice_count} ready invoices to paid"
        return context

class LotRefundDialog(DetailView, FormMixin, AuctionPermissionsMixin):
    model = Lot
    template_name = "auctions/generic_admin_form.html"
    form_class = LotRefundForm
    winner_invoice = None
    seller_invoice = None

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            refund = form.cleaned_data['partial_refund_percent'] or 0
            self.lot.refund(refund, request.user)
            banned = form.cleaned_data['banned']
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
        self.lot = get_object_or_404(Lot, is_deleted=False, auction__isnull=False, auctiontos_seller__isnull=False, pk=pk)
        self.object = self.lot
        self.auction = self.lot.auction
        self.is_auction_admin
        self.seller_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_seller).first()
        if self.lot.auctiontos_winner:
            self.winner_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_winner).first()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['lot'] = self.lot
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['lot'] = self.lot
        if self.lot.active or not self.lot.winning_price:
            context['tooltip'] = "This lot has not sold, there's nothing to refund.  If there's a problem with this lot, remove it."
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
                tooltip += "Seller's invoice is open; $<span id='seller_refund'></span> will automatically be removed.<br>"
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
            context['extra_script'] = mark_safe(extra_script)
            context['tooltip'] = mark_safe(tooltip)
        context['modal_title'] = f"Remove or refund lot {self.lot.lot_number_display}"
        return context

class UserView(DetailView):
    """View information about a single user"""
    template_name = 'user.html'
    model = User

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'], created = UserData.objects.get_or_create(
            user = self.object,
            defaults={},
        )
        try:
            context['banned'] = UserBan.objects.get(user=self.request.user.pk, banned_user=self.object.pk)
        except:
            context['banned'] = False
        context['seller_feedback'] = context['data'].my_lots_qs.exclude(feedback_text__isnull=True).order_by("-date_posted")
        context['buyer_feedback'] = context['data'].my_won_lots_qs.exclude(winner_feedback_text__isnull=True).order_by("-date_posted")
        return context

class UserByName(UserView):
    """/user/username storefront view"""
    
    def dispatch(self, request, *args, **kwargs):
        self.username = kwargs['slug']
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
    template_name = 'user_username.html'
    model = User
    success_message = 'Username updated'
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
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['next'] = reverse('account')#"/users/" + str(self.kwargs['pk'])
        return data['next']

class UserLabelPrefsView(UpdateView, SuccessMessageMixin):
    template_name = 'user_labels.html'
    model = UserLabelPrefs
    success_message = 'Printing preferences updated'
    form_class = UserLabelPrefsForm
    user_pk = None

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['next'] = reverse("userpage", kwargs={'slug': self.request.user.username})
        return data['next']
    
    def get_object(self, *args, **kwargs):
        label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.request.user, defaults={},)
        return label_prefs
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_tab'] = 'printing'
        userData, created = UserData.objects.get_or_create(
            user = self.request.user,
            defaults={},
        )
        context['last_auction_used'] = userData.last_auction_used
        return context
    
class UserPreferencesUpdate(UpdateView, SuccessMessageMixin):
    template_name = 'user_preferences.html'
    model = UserData
    success_message = 'User preferences updated'
    form_class = ChangeUserPreferencesForm
    user_pk = None

    def dispatch(self, request, *args, **kwargs):
        #self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['next'] = reverse("userpage", kwargs={'slug': self.request.user.username})
            #data['next'] = "/users/" + str(self.kwargs['pk'])
        return data['next']
    
    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk) # get the hack

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_tab'] = 'preferences'
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

class UserLocationUpdate(UpdateView, SuccessMessageMixin):
    template_name = 'user_location.html'
    model = UserData
    success_message = 'Contact info updated'
    form_class = UserLocation
    # such a hack...UserData and User do not have the same pks.
    # This means that if we go to /users/1/edit, we'll get the wrong UserData
    # The fix is to have a self.user_pk, which is set in dispatch and called in get_object
    user_pk = None
    
    def dispatch(self, request, *args, **kwargs):
        #self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['next'] = reverse("userpage", kwargs={'slug': self.request.user.username})
            #"/users/" + str(self.kwargs['pk'])
        return data['next']
    
    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk) # get the hack

    def get_initial(self):
        user = User.objects.get(pk=self.get_object().user.pk)
        return {'first_name': user.first_name, 'last_name': user.last_name}

    def form_valid(self, form):
        userData = form.save(commit=False)
        user = User.objects.get(pk=self.get_object().user.pk)
        user.first_name = form.cleaned_data['first_name']
        user.last_name = form.cleaned_data['last_name']
        user.save()
        userData.last_activity = timezone.now()
        userData.save()
        return super(UserLocationUpdate, self).form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_tab'] = 'contact'
        return context

class UserChartView(View):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            user = self.kwargs.get('pk', None)
            allBids = Bid.objects.select_related('lot_number__species_category').filter(user=user, lot_number__species_category__isnull=False)
            pageViews = PageView.objects.select_related('lot_number__species_category').filter(user=user, lot_number__species_category__isnull=False)
            # This is extremely inefficient
            # Almost all of it could be done in SQL with a more complex join and a count
            # However, I keep changing attributes (views, view duration, bids) and sorting here
            # This code is also only run for admins (and async of page load), so the server load is pretty low

            categories = {}
            for item in allBids:
                category = str(item.lot_number.species_category)
                try:
                    categories[category]['bids'] += 1
                except:
                    categories[category] = {'bids': 1, 'views': 0 }
            for item in pageViews:
                category = str(item.lot_number.species_category)
                try:
                    categories[category]['views'] += 1
                except:
                    # brand new category
                    categories[category] = {'bids': 0, 'views': 1 }
            # sort the result
            sortedCategories = sorted(categories, key=lambda t: -categories[t]['views'] ) 
            #sortedCategories = sorted(categories, key=lambda t: -categories[t]['bids'] ) 
            # format for chart.js
            labels = []
            bids = []
            views = []
            for item in sortedCategories:
                labels.append(item)
                bids.append(categories[item]['bids'])
                views.append(categories[item]['views'])
            return JsonResponse(data={
                'labels': labels,
                'bids': bids,
                'views': views
            })
        messages.error(request, "Your account doesn't have permission to view this page.")
        return redirect('/')

class LotChartView(View):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            lot_number = self.kwargs.get('pk', None)
            queryset = PageView.objects.filter(lot_number=lot_number).exclude(user_id__isnull=True).order_by('-total_time').values()[:20]
            labels = []
            data = []    
            for entry in queryset:
                labels.append(str(User.objects.get(pk=entry['user_id'])))
                data.append(int(entry['total_time']))
            
            return JsonResponse(data={
                'labels': labels,
                'data': data,
            })
        messages.error(request, "Your account doesn't have permission to view this page.")
        return redirect('/')

class AdminDashboard(TemplateView):
    """Provides an at-a-glance view of some interesting stats"""
    template_name = 'dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser):
            messages.error(request, "Only admins can view the dashboard")
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = UserData.objects.filter(user__is_active=True)
        context['total_users'] = qs.count()
        context['unsubscribes'] = qs.filter(has_unsubscribed=True).count()
        context['anonymous'] = qs.filter(username_visible=False).exclude(user__username__icontains="@").count() # inactive users with an email as their username were set to anonymous Nov 2023
        context['light_theme'] = qs.filter(use_dark_theme=False).count()
        context['hide_ads'] = qs.filter(show_ads=False).count()
        context['no_club_auction'] = qs.filter(user__auctiontos__isnull=True).distinct().count()
        context['no_participate'] = qs.exclude(Q(user__winner__isnull=False)|Q(user__lot__isnull=False)).distinct().count()
        context['using_watch'] = qs.exclude(user__watch__isnull=True).distinct().count()
        context['using_buy_now'] = qs.filter(user__winner__buy_now_used=True).count()
        context['using_proxy_bidding'] = qs.filter(has_used_proxy_bidding=True).count()
        context['buyers'] = qs.filter(user__winner__isnull=False).distinct().count()
        context['sellers'] = qs.filter(user__lot__isnull=False).distinct().count()
        context['has_location'] = qs.exclude(latitude=0).count()
        context['new_lots_last_7_days'] = Lot.objects.exclude(is_deleted=True).filter(date_posted__gte=timezone.now() - datetime.timedelta(days=7)).count()
        context['new_lots_last_30_days'] = Lot.objects.exclude(is_deleted=True).filter(date_posted__gte=timezone.now() - datetime.timedelta(days=30)).count()
        context['bidders_last_30_days'] = qs.filter(user__bid__last_bid_time__gte=timezone.now() - datetime.timedelta(days=30)).values('user').distinct().count()
        context['feedback_last_30_days'] = Lot.objects.exclude(feedback_rating=0).filter(date_posted__gte=timezone.now() - datetime.timedelta(days=30)).count()
        invoiceqs = Invoice.objects.filter(date__gte=datetime.datetime(2021, 6, 15)).filter(seller_invoice__winner__isnull=False).distinct()
        context['total_invoices'] = invoiceqs.count()
        context['printed_invoices'] = invoiceqs.filter(printed=True).count()
        context['invoice_percent'] =  context['printed_invoices'] / context['total_invoices'] * 100
        context['users_with_search_history'] = User.objects.filter(searchhistory__isnull=False).distinct().count()
        #source of lot images?
        activity = qs.filter(last_activity__gte=timezone.now() - datetime.timedelta(days=60))\
            .annotate(day=TruncDay('last_activity')).order_by('-day')\
            .values('day')\
            .annotate(c=Count('pk'))\
            .values('day', 'c')
        context['last_activity_days'] = []
        context['last_activity_count'] = []
        for day in activity:
            context['last_activity_days'].append((timezone.now()-day['day']).days)
            context['last_activity_count'].append(day['c'])
        seven_days_ago = timezone.now() - datetime.timedelta(days=7)
        page_view_qs = PageView.objects.filter(date_end__gte=seven_days_ago)
        context['page_views'] = page_view_qs.values('url', 'title').annotate(
            unique_view_count=Count('url'),
            total_view_count=Sum('counter') + F('unique_view_count')
        ).order_by('-total_view_count')[:100]
        referrers = page_view_qs.exclude(referrer__isnull=True).exclude(referrer="").exclude(referrer__startswith='http://127.0.0.1:8000')
        # comment out next line to include internal referrers 
        referrers = referrers.exclude(referrer__startswith="https://" + Site.objects.get_current().domain)
        referrers = referrers.exclude(referrer__startswith="" + Site.objects.get_current().domain)
        context['referrers'] = referrers.values('referrer', 'url', 'title').annotate(
            total_clicks=Count('referrer'),
            #total_view_count=Sum('counter') + F('unique_view_count')
        ).order_by('-total_clicks')[:100]
        return context


class UserMap(TemplateView):
    template_name = 'user_map.html'

    def dispatch(self, request, *args, **kwargs):
        if not self.request.user.is_superuser:
            messages.error(self.request, "Only admins can view the user map")
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['google_maps_api_key'] = settings.LOCATION_FIELD['provider.google.api_key']
        data = self.request.GET.copy()
        try:
            view = data['view']
        except:
            view = None
        try:
            filter1 = data['filter']
        except:
            filter1 = None
        qs = User.objects.filter(userdata__latitude__isnull=False, is_active=True).annotate(lots_sold=Count('lot'), lots_bought=Count('winner'))
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
            qs = qs.filter(userdata__last_activity__gte=timezone.now() - datetime.timedelta(hours=int(filter1)))
        context['users'] = qs
        return context

class ClubMap(AdminEmailMixin, TemplateView):
    template_name = 'clubs.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['google_maps_api_key'] = settings.LOCATION_FIELD['provider.google.api_key']
        context['clubs'] = Club.objects.filter(active=True, latitude__isnull=False)
        context['location_message'] = "Set your location to see clubs near you"
        try:
            context['latitude'] = self.request.COOKIES['latitude']
            context['longitude'] = self.request.COOKIES['longitude']
        except:
            pass
        return context

class UserAgreement(TemplateView):
    template_name = 'tos.html'

class IgnoreCategoriesView(TemplateView):
    template_name = 'ignore_categories.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_tab'] = 'ignore'
        return context

class CreateUserIgnoreCategory(View):
    """Add category with given pk to ignore list"""
    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to ignore categories")
            return redirect('/')
        pk = self.kwargs.get('pk', None)
        category = Category.objects.get(pk=pk)
        result, created = UserIgnoreCategory.objects.update_or_create(category=category, user=self.request.user)
        return JsonResponse(data={'pk': result.pk})

class DeleteUserIgnoreCategory(View):
    """Allow users to see lots in a given category again."""
    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to show categories")
            return redirect('/')
        pk = self.kwargs.get('pk', None)
        category = Category.objects.get(pk=pk)
        try:
            exists = UserIgnoreCategory.objects.get(category=category, user=self.request.user)
            exists.delete()
            return JsonResponse(data={'result': "deleted"})
        except Exception as e:
            return JsonResponse(data={'error': str(e)})    

class GetUserIgnoreCategory(View):
    """Get a list of all user ignore categories for the request user"""
    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to use this feature")
            return redirect('/')
        categories = Category.objects.all().order_by('name')
        results = []
        for category in categories:
            item = {
                "id": category.pk,
                "text": category.name,
            }
            try:
                UserIgnoreCategory.objects.get(user=self.request.user, category=category.pk)
                item['selected'] = True
            except:
                pass
            results.append(item)
        return JsonResponse({'results':results},safe=False)

class BlogPostView(DetailView):
    """Render a blog post"""
    model = BlogPost
    template_name = 'blog_post.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        blogpost = self.get_object()
        # this is to allow the chart# syntax
        context['formatted_contents'] = re.sub(r"chart\d", "<canvas id=\g<0>></canvas>", blogpost.body_rendered)
        return context

class UnsubscribeView(TemplateView):
    """
    Match a UUID in the URL to a UserData, and unsubscribe that user
    """
    template_name = "unsubscribe.html"

    def get_context_data(self, **kwargs):
        userData = UserData.objects.filter(unsubscribe_link=kwargs['slug']).first()
        if not userData:
            raise Http404
        else:
            userData.email_me_about_new_auctions = False
            userData.email_me_about_new_local_lots = False
            userData.email_me_about_new_lots_ship_to_location = False
            userData.email_me_when_people_comment_on_my_lots = False
            userData.email_me_about_new_chat_replies = False
            userData.send_reminder_emails_about_joining_auctions = False
            userData.email_me_about_new_in_person_auctions = False
            userData.has_unsubscribed = True
            userData.last_activity = timezone.now()
            userData.save()
        context = super().get_context_data(**kwargs)
        return context

class AuctionChartView(View, AuctionStatsPermissionsMixin):
    """GET methods for generating auction charts"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs['slug'], is_deleted=False)
        if not self.is_auction_admin:
            return redirect('/')
        return super().dispatch(request, *args, **kwargs)
        
class AuctionFunnelChartData(AuctionChartView):
    """
    Inverted funnel chart showing user participation
    """
    def get(self, *args, **kwargs):
        all_views = PageView.objects.filter(Q(auction=self.auction)|Q(lot_number__auction=self.auction))
        anonymous_views = all_views.values('session_id').annotate(session_count=Count('session_id')).count()
        user_views = all_views.values('user').annotate(user_count=Count('user')).count()
        total_views = anonymous_views + user_views
        total_bidders = User.objects.filter(bid__lot_number__auction=self.auction).annotate(dcount=Count('id')).count()
        total_winners = User.objects.filter(winner__auction=self.auction).annotate(dcount=Count('id')).count()
        labels = [  "Total unique views",
                    "Views from users with accounts",
                    "Users who bid on at least one item",
                    "Users who won at least one item",
                    ]
        data = [total_views,
                user_views,
                total_bidders,
                total_winners,
                ]    
        return JsonResponse(data={
            'labels': labels,
            'data': data,
        })

class AuctionLotBiddersChartData(AuctionChartView):
    """How many bidders were there per lot?"""
    def get(self, *args, **kwargs):
        lots = self.auction.lots_qs
        labels = ['Not sold','Lots with bids from 1 user',
                    'Lots with bids from 2 users',
                    'Lots with bids from 3 users',
                    'Lots with bids from 4 users',
                    'Lots with bids from 5 users',
                    'Lots with bids from 6 or more users']
        data = [0,0,0,0,0,0,0]
        for lot in lots:
            if not lot.winning_price:
                data[0] += 1
            else:
                bids = len(Bid.objects.filter(lot_number=lot))
                if bids > 6:
                    bids = 6
                else:
                    data[bids] += 1
        return JsonResponse(data={
            'labels': labels,
            'data': data,
        })

class AuctionCategoriesChartData(AuctionChartView):
    """Categories by views and lots sold"""
    number_of_categories_to_show = 20
    
    def process_stat(self, n, d):
        """Divide and catch div/0, round result"""
        if n is not None and d:
            result = round(((n / d) * 100),2)
        else:
            result = 0
        return result

    def get(self, *args, **kwargs):
        labels = []
        views = []
        bids = []
        lots = []
        volumes = []
        categories = Category.objects.filter(lot__auction=self.auction).annotate(num_lots=Count('lot')).order_by('-num_lots')
        lot_count = self.auction.lots_qs.count()
        allViews = PageView.objects.filter(lot_number__auction=self.auction).count()
        allBids = Bid.objects.filter(lot_number__auction=self.auction).count()
        allVolume = Lot.objects.exclude(is_deleted=True).filter(auction=self.auction).aggregate(Sum('winning_price'))['winning_price__sum']                
        if lot_count:
            for category in categories[:self.number_of_categories_to_show]:
                labels.append(str(category))
                thisViews = PageView.objects.filter(lot_number__auction=self.auction, lot_number__species_category=category).count()
                thisBids = Bid.objects.filter(lot_number__auction=self.auction, lot_number__species_category=category).count()
                thisVolume = Lot.objects.exclude(is_deleted=True).filter(auction=self.auction, species_category=category).aggregate(Sum('winning_price'))['winning_price__sum']
                percentOfLots = self.process_stat(category.num_lots, lot_count)
                percentOfViews = self.process_stat(thisViews, allViews)
                percentOfBids = self.process_stat(thisBids, allBids)
                percentOfVolume = self.process_stat(thisVolume, allVolume)
                lots.append(percentOfLots)
                views.append(percentOfViews)
                bids.append(percentOfBids)
                volumes.append(percentOfVolume)
        return JsonResponse(data={
            'labels': labels,
            'lots': lots,
            'views': views,
            'bids': bids,
            'volumes': volumes,
        })

class AuctionStatsActivityJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    # these will no doubt need to be tweaked, perhaps differnt for in-person and online auctions?
    bins = 21
    days_before = 16
    days_after = bins - days_before

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs['slug'], is_deleted=False)
        if not self.is_auction_admin:
            return redirect('/')
        if self.auction.is_online:
            self.date_start = self.auction.date_end - timezone.timedelta(days=self.days_before)
            self.date_end = self.auction.date_end + timezone.timedelta(days=self.days_after)
        else: # in person
            self.date_start = self.auction.date_start - timezone.timedelta(days=self.days_before)
            self.date_end = self.auction.date_start + timezone.timedelta(days=self.days_after)
        #self.bin_size = (self.date_end - self.date_start).total_seconds() / self.bins
        #self.bin_edges = [self.date_start + timezone.timedelta(seconds=self.bin_size * i) for i in range(self.bins + 1)]
        return super().dispatch(request, *args, **kwargs)
        
    def get_labels(self):
        #return [(f"Day {i}") for i in range(self.bins + 1)]
        before = [(f"{i} days before") for i in range(self.days_before, 0, -1)]
        after = [(f"{i} days after") for i in range(1, self.days_after)]
        midpoint = 'start'
        if self.auction.is_online:
            midpoint = "end"
        return before + [midpoint] + after

    def get_providers(self):
        return ['Views', 'Joins', 'New lots', 'Searches', 'Bids', 'Watches']

    def get_data(self):
        """Wonder if these qs should be properties of the Auction model...
        Might add invoice views here, but it would require updating the pageview model to have an invoice field similar to how auction and lot currently work"""

        views = PageView.objects.filter(Q(auction=self.auction)|Q(lot_number__auction=self.auction))
        joins = AuctionTOS.objects.filter(auction=self.auction)
        new_lots = Lot.objects.filter(auction=self.auction)
        searches = SearchHistory.objects.filter(auction=self.auction)
        bids = LotHistory.objects.filter(lot__auction=self.auction, changed_price=True)
        watches = Watch.objects.filter(lot_number__auction=self.auction)

        # what follows is a delightful reminder of how important a consistent naming scheme is
        return [bin_data(views, 'date_start', self.bins, self.date_start, self.date_end),
                bin_data(joins, 'createdon', self.bins, self.date_start, self.date_end),
                bin_data(new_lots, 'date_posted', self.bins, self.date_start, self.date_end),
                bin_data(searches, 'createdon', self.bins, self.date_start, self.date_end),
                bin_data(bids, 'timestamp', self.bins, self.date_start, self.date_end),
                bin_data(watches, 'createdon', self.bins, self.date_start, self.date_end)]

class AuctionStatsAttritionJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    ignore_percent = 10

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs['slug'], is_deleted=False)
        if not self.is_auction_admin:
            return redirect('/')
        self.lots = Lot.objects.exclude(Q(date_end__isnull=True)|Q(is_deleted=True)).filter(auction=self.auction, winning_price__isnull=False).order_by('-date_end')
        self.total_lots = self.lots.count()
        start_index = int(self.ignore_percent / 100 * self.total_lots)
        end_index = int((1 - (self.ignore_percent / 100)) * self.total_lots) - 1  # Subtract 1 because indexing is zero-based
        if self.total_lots > 0:
            self.start_date = self.lots[start_index].date_end
            self.end_date = self.lots[end_index].date_end if self.total_lots > 1 else self.start_date  # Handle case with only one lot
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
        data = [{
                'x': (lot.date_end - self.end_date).total_seconds() // 60, # minutes after auction start
                #'x': lot.date_end.timestamp() * 1000, # this one gives js timestamps and would need moment.js to convert to date
                'y': lot.winning_price
            } for lot in self.lots]
        # Prepare the data structure for Chart.js
        # data = {
        #     'data': data
        # }
        #return data
        return [data]

class AuctionStatsBarChartJSONView(BaseColumnsHighChartsView, AuctionPermissionsMixin):
    """This is needed because of https://github.com/peopledoc/django-chartjs/issues/56"""
    allow_non_admins = True
    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs['slug'], is_deleted=False)
        if not self.is_auction_admin:
            return redirect('/')
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
                'labels': self.get_labels(),
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
            raise ValueError(f"self.get_data() return a {len(data)} long array, self.get_providers() returned a {len(providers)} long array.  These need to return the same length array.")
        for i, entry in enumerate(data):
            color = tuple(next(color_generator))
            dataset = {"data": entry,
                       "label": providers[i],}
            dataset.update(self.get_dataset_options(i, color))
            datasets.append(dataset)
        return datasets

    def get_dataset_options(self, index, color):
        default_opt = {
            "backgroundColor": "rgba(%d, %d, %d, 0.5)" % color,
            "borderColor": "rgba(%d, %d, %d, 1)" % color,
            "pointBackgroundColor": "rgba(%d, %d, %d, 1)" % color,
            "pointBorderColor": "#fff",
        }
        return default_opt

    def get_title(self):
        return ""
    
class AuctionStatsLotSellPricesJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        labels = [(f"${i + 1}-{i + 2}") for i in range(0, 37, 2)]
        return ['Not sold'] + labels + ["$40+"]        
    
    def get_providers(self):
        return ['Number of lots']
    
    def get_data(self):
        sold_lots = self.auction.lots_qs.filter(winning_price__isnull=False)
        print("started")
        histogram = bin_data(sold_lots, 'winning_price', number_of_bins=19, start_bin=1, end_bin=39, add_column_for_high_overflow=True)
        return [[self.auction.total_unsold_lots] + histogram]

class AuctionStatsReferrersJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        self.views = PageView.objects.filter(
            Q(auction=self.auction) | Q(lot_number__auction=self.auction)
        ).exclude(referrer__isnull=True).exclude(referrer__startswith='auction.fish').exclude(referrer__exact='').values('referrer').annotate(count=Count('referrer'))
        result = []
        for view in self.views:
            if view['count'] > 1:
                result.append(view['referrer'])
        result.append("Other")
        return result
    
    def get_providers(self):
        return ['Number of clicks']

    def get_data(self):
        result = []
        other = 0
        for view in self.views:
            if view['count'] > 1:
                result.append(view['count'])
            else:
                other += 1
        result.append(other)
        return [result]

class AuctionStatsImagesJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return ['No images', 'One image', 'More than one image']
    
    def get_providers(self):
        return ['Median sell price', "Average sell price", "Number of lots"]

    def get_data(self):
        lots = Lot.objects.filter(auction=self.auction, winning_price__isnull=False).annotate(num_images=Count('lotimage'))
        lots_with_no_images = lots.filter(num_images=0)
        lots_with_one_image = lots.filter(num_images=1)
        lots_with_one_or_more_images = lots.filter(num_images__gt=1)
        medians = []
        averages = []
        counts = []
        for lots in [lots_with_no_images, lots_with_one_image, lots_with_one_or_more_images]:
            try:
                medians.append(median_value(lots, 'winning_price'))
            except:
                medians.append(0)
            averages.append(lots.aggregate(avg_value=Avg('winning_price'))['avg_value'])
            counts.append(lots.count())
        return [medians, averages,counts ]

class AuctionStatsTravelDistanceJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return ['Less than 10 miles', '10-20 miles', '21-30 miles', '31-40 miles', '41-50 miles', '51+ miles']
    
    def get_providers(self):
        return ['Number of users']

    def get_data(self):
        auctiontos = AuctionTOS.objects.filter(auction=self.auction, user__isnull=False)
        histogram = bin_data(auctiontos, 'distance_traveled', number_of_bins=5, start_bin=1, end_bin=51, add_column_for_high_overflow=True,)
        return [histogram]

class AuctionStatsPreviousAuctionsJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return ['First auction', '1 previous auction', '2+ previous auctions']
    
    def get_providers(self):
        return ['Number of users']

    def get_data(self):
        auctiontos = AuctionTOS.objects.filter(auction=self.auction, email__isnull=False)
        histogram = bin_data(auctiontos, 'previous_auctions_count', number_of_bins=2, start_bin=0, end_bin=2, add_column_for_high_overflow=True)
        return [histogram]
    
class AuctionStatsLotsSubmittedJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return ['Buyer only (0 lots sold)', '1-2 lots', '3-4 lots', '5-6 lots', '7-8 lots', '9+ lots' ]
    
    def get_providers(self):
        return ['Number of users']

    def get_data(self):
        invoices = Invoice.objects.filter(auction=self.auction)
        histogram = bin_data(invoices, 'lots_sold', number_of_bins=4, start_bin=1, end_bin=9,
                              add_column_for_low_overflow=True, add_column_for_high_overflow=True)
        return [histogram]

class AuctionStatsLocationVolumeJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        locations = []
        for location in self.auction.location_qs:
            locations.append(location.name)
        return locations
    
    def get_providers(self):
        return ['Total bought', 'Total sold']

    def get_data(self):
        sold = []
        bought = []
        for location in self.auction.location_qs:
            sold.append(location.total_sold)
            bought.append(location.total_bought)
        return [bought, sold]

class AuctionStatsLocationFeatureUseJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        return ['An account', 'Search', "Watch", "Proxy bidding", "Chat", "Buy now", "View invoice", "Leave feedback for sellers"]
    
    def get_providers(self):
        return ['Percent of users']

    def get_data(self):
        auctiontos = AuctionTOS.objects.filter(auction=self.auction)
        auctiontos_with_account = auctiontos.filter(user__isnull=False)
        searches = SearchHistory.objects.filter(user__isnull=False, auction=self.auction).values('user').distinct().count()
        seach_percent = int(searches/auctiontos_with_account.count() * 100)
        watches = Watch.objects.filter(lot_number__auction=self.auction).values('user').distinct().count()
        watch_percent = int(watches/auctiontos_with_account.count() * 100)
        has_used_proxy_bidding = UserData.objects.filter(has_used_proxy_bidding=True, user__in=auctiontos_with_account.values_list('user')).count()
        has_used_proxy_bidding_percent = int(has_used_proxy_bidding/auctiontos_with_account.count() * 100)
        chat = LotHistory.objects.filter(changed_price=False, lot__auction=self.auction, user__in=auctiontos_with_account.values_list('user')).values('user').distinct().count()
        chat_percent = int(chat/auctiontos_with_account.count() * 100)
        if self.auction.is_online:
            lot_with_buy_now = Lot.objects.filter(auction=self.auction, buy_now_used=True).values('auctiontos_winner').distinct().count()
        else:
            lot_with_buy_now = Lot.objects.filter(auction=self.auction, winning_price=F('buy_now_price')).values('auctiontos_winner').distinct().count()
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
        leave_feedback = sold_lots.filter(~Q(feedback_rating=0)).values('auctiontos_winner').distinct().count()
        all_sold_lots = sold_lots.values('auctiontos_winner').distinct().count()
        if all_sold_lots == 0:
            leave_feedback_percent = 0
        else:
            leave_feedback_percent = int(leave_feedback / all_sold_lots * 100)
        return [[account_percent, seach_percent, watch_percent, has_used_proxy_bidding_percent, chat_percent, lot_with_buy_now_percent, view_invoice_percent, leave_feedback_percent]]

class AuctionStatsAuctioneerSpeedJSONView(AuctionStatsAttritionJSONView):
    def get_providers(self):
        """Return names of datasets."""
        return ["Lots per minute"]
    
    def get_data(self):
        data = []
        for i in range(1, len(self.lots)):
            minutes = (self.lots[i - 1].date_end - self.lots[i].date_end).total_seconds() / 60
            ignore_if_more_than = 3 # minutes
            if minutes <= ignore_if_more_than:
                data.append({'x': i, 'y':minutes})
        return [data]

class AuctionBulkPrinting(FormView, AuctionPermissionsMixin):
    model = Auction
    template_name = "auction_printing.html"
    form_class = MultiAuctionTOSPrintLabelForm

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop('slug'), is_deleted=False)
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['all_labels_count'] = self.auction.labels_qs.count()
        context['unprinted_label_count'] = self.auction.unprinted_labels_qs.count()
        context['printed_labels_count'] = context['all_labels_count'] - context['unprinted_label_count']
        context['auction'] = self.auction
        return context
    
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['auctiontos'] = AuctionTOS.objects.filter(auction=self.auction).annotate(
            lots_count=Count('auctiontos_seller', filter=Q(auctiontos_seller__banned=False, auctiontos_seller__is_deleted=False)),
            unprinted_labels_count=Count('auctiontos_seller', filter=Q(auctiontos_seller__banned=False, auctiontos_seller__label_printed=False, auctiontos_seller__is_deleted=False))
        ).filter(lots_count__gt=0)
        return kwargs

    def form_valid(self, form):
        print_only_unprinted = form.cleaned_data['print_only_unprinted']
        selected_tos = []
        for key, value in form.cleaned_data.items():
            if key.startswith('tos_') and value:
                pk = key.split('_')[1]
                selected_tos.append(pk)
        data = {
            'selected_tos': selected_tos,
            'print_only_unprinted': print_only_unprinted
        }
        url = reverse('auction_printing_pdf', kwargs={'slug': self.auction.slug})
        url_with_params = f"{url}?{urlencode(data)}"
        return HttpResponseRedirect(url_with_params)

class AuctionBulkPrintingPDF(LotLabelView):
    """Admin page to print labels for multiple users at once"""
    allow_non_admins = False

    def get_queryset(self):
        return self.queryset

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs['slug']).first()
        self.is_auction_admin

        self.selected_tos = request.GET.get('selected_tos', None)
        self.print_only_unprinted = request.GET.get('print_only_unprinted', 'True') == 'True'
        if not self.selected_tos:
            self.queryset = self.auction.unprinted_labels_qs
        else:
            self.selected_tos = ast.literal_eval(self.selected_tos)
            if self.print_only_unprinted:
                self.queryset = self.auction.unprinted_labels_qs
            else:
                self.queryset = self.auction.labels_qs
            for lot in self.queryset:
                print(lot)
            self.queryset = self.queryset.filter(auctiontos_seller__pk__in=self.selected_tos)
        if not self.get_queryset():
            if not self.selected_tos:
                messages.error(request, "No users selected")
            else:
                messages.error(request, "Couldn't find any labels to print")
            return redirect(reverse('auction_printing', kwargs={'slug':self.auction.slug}))
        return super().dispatch(request, *args, **kwargs)

class PickupLocationsIncoming(View, AuctionPermissionsMixin):
    """All lots destined for this location"""
    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop('pk')).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        queryset = self.location.incoming_lots.order_by('-auctiontos_seller__name')
        response = HttpResponse(content_type='text/csv')
        name = self.location.name.lower().replace(" ","_")
        response['Content-Disposition'] = f'attachment; filename="incoming_lots_destined_for_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(['Lot number', 'Lot name', 'Winner name', 'Origin', 'Seller name',])
        for lot in queryset:
            csv_writer.writerow([lot.lot_number_display, lot.lot_name, lot.winner_name, lot.location,  lot.seller_name])
        return response
    
class PickupLocationsOutgoing(View, AuctionPermissionsMixin):
    """CSV of all lots coming from this location"""
    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop('pk')).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        queryset = self.location.outgoing_lots.order_by('-auctiontos_winner__pickup_location__name')
        response = HttpResponse(content_type='text/csv')
        name = self.location.name.lower().replace(" ","_")
        response['Content-Disposition'] = f'attachment; filename="outgoing_lots_coming_from_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(['Lot number', 'Seller name', 'Lot name', 'Destination',  'Winner name'])
        for lot in queryset:
            csv_writer.writerow([lot.lot_number_display, lot.seller_name, lot.lot_name, lot.winner_location, lot.winner_name])
        return response
    
class CategoryFinder(View, LoginRequiredMixin):
    """API view which will return a category (or none) based on POST keyword lot_name"""
    def get(self, request, *args, **kwargs):
        return redirect('/')

    def post(self, request, *args, **kwargs):
        lot_name = request.POST['lot_name']
        result = guess_category(lot_name)
        if result:
            result = {'name':result.name, 'value':result.pk}
        else:
            result = {'value': None}
        return JsonResponse(result)

class AuctionFinder(View, LoginRequiredMixin):
    """API view which will return information about an auction based on POST keyword auction.  Expects a pk."""
    def get(self, request, *args, **kwargs):
        return redirect('/')

    def post(self, request, *args, **kwargs):
        try:
            self.auction = Auction.objects.filter(pk = request.POST['auction']).first()
        except ValueError:
            self.auction = None
        if not self.auction or not AuctionTOS.objects.filter(user=request.user, auction=self.auction):
            # you don't get to query auctions you haven't joined
            result = {}
        else:
            result = {'use_categories':self.auction.use_categories,
                      'reserve_price':self.auction.reserve_price,
                      'buy_now':self.auction.buy_now,
                      }
        return JsonResponse(result)

class LotChatSubscribe(View, LoginRequiredMixin):
    """Called when a user sends a chat message about a lot to create a ChatSubscription model"""
    def get(self, request, *args, **kwargs):
        return redirect('/')

    def post(self, request, *args, **kwargs):
        try:
            lot = Lot.objects.filter(pk=request.POST['lot']).first()
        except ValueError:
            lot = None
        if not lot:
            raise Http404 (f"No lot found with key {lot}")
        else:
            subscription, created = ChatSubscription.objects.get_or_create(
                user = request.user,
                lot = lot,
            )
            unsubscribed = request.POST['unsubscribed']
            if unsubscribed == "true": # classic javascript, again
                subscription.unsubscribed = True
            else:
                subscription.unsubscribed = False
            subscription.save()
        return JsonResponse({'unsubscribed': subscription.unsubscribed})
    
class ChatSubscriptions(LoginRequiredMixin, TemplateView):
    """Show chat messages on your lots and other lots"""
    template_name = 'chat_subscriptions.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['subscriptions'] = self.request.user.userdata.subscriptions_with_new_message_annotation.order_by('-new_message_count', '-lot__date_posted')
        context['data'] = self.request.user.userdata
        return context
    
class AddTosMemo(View, LoginRequiredMixin, AuctionPermissionsMixin):
    """API view to update the memo field of an auctiontos"""
    
    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop('pk')
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)
    
    def get(self, request, *args, **kwargs):
        return redirect('/')

    def post(self, request, *args, **kwargs):
        memo = request.POST['memo']
        if memo or memo == "":
            self.auctiontos.memo = memo
            self.auctiontos.save()
            return JsonResponse({'result':"ok"})
        raise Http404
    
class AuctionNoShow(TemplateView, LoginRequiredMixin, AuctionPermissionsMixin):
    """When someone doesn't show up for an auction, offer some tools to clean up the situation"""
    template_name = "auctions/noshow.html"

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop('slug'), is_deleted=False)
        self.is_auction_admin
        self.tos = get_object_or_404(AuctionTOS, auction=self.auction, bidder_number=kwargs.pop('tos'))
        return super().dispatch(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['auction'] = self.auction
        context['tos'] = self.tos
        context['bought_lots'] = add_price_info(self.tos.bought_lots_qs)
        context['sold_lots'] = self.tos.lots_qs.annotate(
            full_buyer_refund=ExpressionWrapper(
                F('winning_price') + (F('winning_price') * F('auction__tax') / 100),
                output_field=FloatField()
            )
        )
        return context

class AuctionNoShowAction(AuctionNoShow, FormMixin):
    """Refund lots, leave feedback, and ban this user"""

    template_name = "auctions/generic_admin_form.html"
    form_class = AuctionNoShowForm
    
    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs['auction'] = self.auction
        form_kwargs['tos'] = self.tos
        return form_kwargs
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tooltip'] = f"Check any actions you wish to take against {self.tos.name}"
        context['unsold_lot_warning'] = "These actions cannot be undone!"
        context['modal_title'] = f"Take action against {self.tos.name}"
        return context
        
    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            refund_sold_lots = form.cleaned_data['refund_sold_lots']
            refund_bought_lots = form.cleaned_data['refund_bought_lots']
            leave_negative_feedback = form.cleaned_data['leave_negative_feedback']
            ban_this_user = form.cleaned_data['ban_this_user']
            if refund_sold_lots:
                for lot in self.tos.lots_qs:
                    if lot.winning_price:
                       lot.refund(100, request.user)
                    else:
                       lot.remove(True, request.user)
            if refund_bought_lots:
                for lot in self.tos.bought_lots_qs:
                    lot.refund(100, request.user)
            if leave_negative_feedback:
                for lot in self.tos.bought_lots_qs:
                    lot.winner_feedback_rating = -1
                    lot.winner_feedback_text = "Did not pay"
                    lot.save()
                for lot in self.tos.lots_qs:
                    lot.feedback_rating -1
                    lot.feedback_text = "Did not provide lot"
                    lot.save()
            if ban_this_user:
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
            return HttpResponse("<script>location.reload();</script>", status=200)
        else:
            return self.form_invalid(form)
