import csv
from datetime import datetime
from random import randint
from itertools import chain
from django.shortcuts import render,redirect
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.views.generic import ListView, DetailView, View, TemplateView
from django.urls import reverse
from django.views.generic.edit import UpdateView, CreateView, DeleteView, FormMixin
from django.db.models import Count, Case, When, IntegerField, Q
from django.contrib.messages.views import SuccessMessageMixin
from allauth.account.models import EmailAddress
from el_pagination.views import AjaxListView
from easy_thumbnails.templatetags.thumbnail import thumbnail_url
from PIL import Image
import os
from django.conf import settings
from .models import *
from .filters import *
from .forms import *
from io import BytesIO
from django.core.files import File
import re

def index(request):
    return HttpResponse("this page is intentionally left blank")

class LotListView(AjaxListView):
    """This is a base class that shows lots, with a filter.  This class is never used directly, but it's a parent for several other classes.
    The context is overridden to set the view type"""
    model = Lot
    template_name = 'all_lots.html'

    def get_page_template(self):
        try:
            userdata = UserData.objects.get(user=self.request.user.pk)
            if userdata.use_list_view:
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
        if len(data) == 0:
            data['status'] = "open"
        context = super().get_context_data(**kwargs)
        if self.request.GET.get('page'):
            del data['page'] # required for pagination to work
        context['filter'] = LotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=True)
        context['embed'] = 'all_lots'
        try:
            context['lotsAreHidden'] = len(UserIgnoreCategory.objects.filter(user=self.request.user))
        except:
            # probably not signed in
            context['lotsAreHidden'] = -1
        try:
            context['lastView'] = PageView.objects.filter(user=self.request.user).order_by('-date_start')[0].date_start
        except:
            context['lastView'] = timezone.now()
        try:
            context['auction'] = Auction.objects.get(slug=data['auction'])
        except:
            context['auction'] = None
        return context

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
            userdata = UserData.objects.get(user=self.request.user.pk)
            if userdata.use_list_view:
                return 'lot_list_page.html'
            else:
                return 'lot_tile_page.html'
        except:
            pass
        return 'lot_tile_page.html' # tile view as default

    def check_lot(self, lot):
        # weight the chance to see a given lot based on the user's history
        user = self.request.user
        if not user:
            # checks always pass if not signed in
            return True
        try:
            interest = UserInterestCategory.objects.get(category=lot.species_category, user=user).as_percent
        except:
            interest = 10 # low chance to view lots that the user has no history for
        try:
            ignore = UserIgnoreCategory.objects.get(category=lot.species_category, user=user)
            return False
        except:
            pass
        rand = randint(0, 100-settings.WEIGHT_AGAINST_TOP_INTEREST)
        #if lot.promoted:
        if True: # temporarily, we need to collect data on all lots
            interest = interest + lot.promotion_weight
        if interest > rand:
            return interest
        else:
            return False

    def get_queryset(self):
        data = self.request.GET.copy()
        try:
            qs = Lot.objects.filter(auction__slug=data['auction'], active=True, banned=False)
        except:
            qs = Lot.objects.filter(active=True, banned=False)
            #qs = Lot.objects.filter(banned=False) # this will show inactive lots, useful for testing
        if self.request.user:
            # if the user is signed in, exclude viewed lots and lots they submitted
            qs = qs.exclude(pageview__user=self.request.user.pk).exclude(user=self.request.user.pk)
        count = len(qs)
        result = []
        if not count:
            return []
        timeout = 0 # hard cap to prevent an infinite loop
        try:
            desiredResults = int(data['qty'])
        except:
            desiredResults = 10
        while len(result) < desiredResults and timeout < 200:
            # select a random lot
            resultLot = qs[randint(0, count - 1)]
            if resultLot in result:
                # no duplicates
                pass
            else:
                checkInterest = self.check_lot(resultLot)
                if checkInterest:
                    resultLot.your_interest = checkInterest
                    result.append(resultLot)
            timeout += 1
        return result

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
        if len(data) == 0:
            data['status'] = "open"
        context = super().get_context_data(**kwargs)
        context['filter'] = UserBidLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context['view'] = 'mybids'
        context['lotsAreHidden'] = -1
        return context

class MyLots(LotListView):
    """Show all lots submitted by the current user"""
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['filter'] = UserOwnedLotFilter(self.request.GET, queryset=self.get_queryset(), request=self.request, ignore=False)
        context['view'] = 'mylots'
        context['lotsAreHidden'] = -1
        return context
        
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
        context['filter'] = LotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=True)
        try:
            context['user'] = User.objects.get(pk=data['user'])
            context['view'] = 'user'
        except:
            pass
        return context

@login_required
def watchOrUnwatch(request, pk):
    if request.method == 'POST':
        watch = request.POST['watch']
        user = request.user
        lot_number = Lot.objects.get(pk=pk)
        obj, created = Watch.objects.update_or_create(
            lot_number=lot_number,
            user=user,
            defaults={},
        )
        if watch == "false": # string not bool...
            obj.delete()
        if obj:
            return HttpResponse("Success")
        else:
            return HttpResponse("Failure")

def userBan(request, pk):
    if request.method == 'POST':
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        auctionsList = Auction.objects.filter(created_by=user.pk)
        # delete all bids the banned user has made on active lots or in active auctions created by the request user
        bids = Bid.objects.filter(user=bannedUser)
        for bid in bids:
            lot = Lot.objects.get(pk=bid.lot_number.pk)
            if lot.user == user or lot.auction in auctionsList:
                if not lot.ended:
                    print('Deleting bid ' + str(bid))
                    bid.delete()
        # ban all lots added by the banned user.  These are not deleted, just removed from the auction
        for auction in auctionsList:
            lots = Lot.objects.filter(user=bannedUser, auction=auction.pk)
            for lot in lots:
                if not lot.ended:
                    print(f"User {str(user)} has banned lot {lot}")
                    lot.banned = True
                    lot.ban_reason = "This user has been banned from this auction"
                    lot.save()
        return redirect('/users/' + str(pk))

def lotBan(request, pk):
    if request.method == 'POST':
        lot = Lot.objects.get(pk=pk)
        try:
            ban_reason = request.POST['banned']
        except:
            return HttpResponse("specify banned in post data")
        checksPass = False
        if request.user.is_superuser:
            checksPass = True
        if lot.auction.created_by.pk == request.user.pk:
            checksPass = True
        if checksPass:
            if not ban_reason:
                lot.banned = False
            else:
                lot.banned = True
            lot.ban_reason = ban_reason
            lot.save()
            return HttpResponse("success")
        else:
            raise PermissionDenied()

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
        return redirect('/users/' + str(pk))

def imageRotate(request):
    """Rotate an image associated with a lot"""
    if request.method == 'POST':
        try:
            user = request.user
            lot_number = int(request.POST['lot_number'])
            angle = int(request.POST['angle'])
        except:
            return HttpResponse("user, lot_number and angle are required")
        try:
            lot = Lot.objects.get(pk=lot_number)
        except:
            return HttpResponse(f"Lot {lot_number} not found")
        if not (user.is_superuser or lot.user == user):
            raise PermissionDenied()
        if not lot.image:
            return HttpResponse("No image")
        thisImage = str(lot.image)
        pilImage = Image.open(BytesIO(lot.image.read()))
        pilImage = pilImage.rotate(angle, expand=True)
        output = BytesIO()
        pilImage.save(output, format='JPEG', quality=100)
        output.seek(0)
        lot.image = File(output, str(thisImage))
        lot.save()
        return HttpResponse("Success")

def feedback(request, pk, leave_as):
    """Leave feedback on a lot
    This can be done as a buyer or a seller
    api/feedback/lot_number/buyer
    api/feedback/lot_number/seller
    qqq
    """
    if request.method == 'POST':
        data = request.POST
        try:
            lot = Lot.objects.get(pk=pk)
        except:
            raise Http404 (f"No lot found with key {lot}") 
        checksPass = False
        if leave_as == "winner":
            if lot.winner.pk == request.user.pk:
                checksPass = True
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
            if lot.user.pk == request.user.pk:
                checksPass = True
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
        if not checksPass:
            raise PermissionDenied()
        return HttpResponse("Success")

def pageview(request, pk):
    """
    Record interest in blog posts and lots
    Initial interest (/new/) creates product interest
    Staying on the page will update the time in page views on the PageView model
    """
    if request.method == 'POST':
        user = request.user
        if "pageview" in request.path:
            lot_number = Lot.objects.get(pk=pk)
            blog = None
        else:
            blog = BlogPost.objects.get(pk=pk)
            lot_number = None
        # Initial pageview to record page views to the PageView model
        if user.is_authenticated:
            obj, created = PageView.objects.get_or_create(
                lot_number=lot_number,
                blog_post=blog,
                user=user,
                defaults={},
            )
            if "new" not in request.path:
                obj.total_time += 10
            obj.date_end = timezone.now()
            obj.save()
            if created and lot_number:
                # create interest in this category if this is a new view for this category
                interest, created = UserInterestCategory.objects.get_or_create(
                    category=lot_number.species_category,
                    user=user,
                    defaults={ 'interest': 0 }
                    )
                interest.interest += settings.VIEW_WEIGHT
                interest.save()
        else:
            # anonymous user, always create
            if "new" not in request.path:
                PageView.objects.create(
                    lot_number=lot_number,
                    blog_post=blog,
                    user=None,
                )
        return HttpResponse("Success")

def invoicePaid(request, pk):
    if request.method == 'POST':
        invoice = Invoice.objects.get(pk=pk)
        checksPass = False
        if request.user.is_superuser:
            checksPass = True
        if invoice.auction.created_by.pk == request.user.pk:
            checksPass = True
        if checksPass:
            if invoice.paid:
                invoice.paid = False
                invoice.save()
                result = False
            else:
                invoice.paid = True
                invoice.save()
                result = True
            return JsonResponse(data={
                    'paid': result
                })
        raise PermissionDenied()

@login_required
def auctionReport(request, slug):
    """Get a CSV file showing all users who are participating in this auction"""
    auction = Auction.objects.get(slug=slug)
    checksPass = False
    if request.user.is_superuser:
        checksPass = True
    if auction.created_by.pk == request.user.pk:
        checksPass = True
    if checksPass:
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type='text/csv')
        if auction.invoiced:
            end = 'final'
        else:
            end = timezone.now().strftime("%m-%d-%Y")
        response['Content-Disposition'] = 'attachment; filename="' + slug + "-report-" + end + '.csv"'
        writer = csv.writer(response)
        writer.writerow(['Name', 'Email', 'Phone', 'Address', 'Location', 'Club', 'Lots viewed', 'Lots bid', 'Lots submitted', 'Lots won', 'Total spent', 'Total paid', 'Invoice', 'Breeder points'])
        users = AuctionTOS.objects.filter(auction=auction)
        for data in users:
            lotsViewed = PageView.objects.filter(lot_number__auction=auction, user=data.user)
            lotsBid = Bid.objects.filter(lot_number__auction=auction,user=data.user)
            lotsSumbitted = Lot.objects.filter(user=data.user, auction=auction)
            lotsWon = Lot.objects.filter(winner=data.user, auction=auction)
            breederPoints = Lot.objects.filter(user=data.user, auction=auction, i_bred_this_fish=True)
            try:
                phone = data.user.userdata.phone_number
            except:
                phone = ""
            try:
                address = data.user.userdata.address
            except:
                address = ""
            try:
                club = data.user.userdata.club
            except:
                club = ""
            try:
                invoice = Invoice.objects.get(auction=auction, user=data.user)
                if invoice.paid:
                    paid = "Paid"
                else:
                    paid = "Unpaid"
                totalSpent = invoice.total_bought
                totalPaid = invoice.total_sold
            except:
                paid = "N/A"
                totalSpent = "0" 
                totalPaid = "0"
            writer.writerow([data.user.first_name + " " + data.user.last_name, data.user.email, \
                phone, address, data.pickup_location, \
                club, len(lotsViewed), len(lotsBid), len(lotsSumbitted), \
                len(lotsWon), totalSpent, totalPaid, paid, len(breederPoints)])
        return response    
    raise PermissionDenied()

class LeaveFeedbackView(LoginRequiredMixin, ListView):
    """Show all pickup locations belonging to the current user"""
    model = Lot
    template_name = 'leave_feedback.html'
    ordering = ['-date_posted']
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cutoffDate =  timezone.now() - datetime.timedelta(days=90)
        #new_context = super().get_context_data()
        context['won_lots'] = Lot.objects.filter(winner=self.request.user.pk, date_posted__gte=cutoffDate).order_by('-date_posted')
        context['sold_lots'] = Lot.objects.filter(user=self.request.user.pk, date_posted__gte=cutoffDate, winner__isnull=False).order_by('-date_posted')
        return context

class PickupLocations(LoginRequiredMixin, ListView):
    """Show all pickup locations belonging to the current user"""
    model = PickupLocation
    template_name = 'all_pickup_locations.html'
    ordering = ['name']
    
    def get_queryset(self):
        new_context = PickupLocation.objects.filter(
            user=self.request.user.pk,
        )
        return new_context

class PickupLocationsUpdate(LoginRequiredMixin, UpdateView):
    """Edit pickup locations"""
    def get_form_kwargs(self):
        form_kwargs = super(PickupLocationsUpdate, self).get_form_kwargs()
        form_kwargs['user'] = self.request.user
        return form_kwargs

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            raise PermissionDenied()
        users = AuctionTOS.objects.filter(pickup_location=self.get_object().pk)
        if len(users):
            messages.warning(request, "Users have already selected this as a pickup location.  Don't make large changes!")
        return super().dispatch(request, *args, **kwargs)
    
    def get_success_url(self):
        return "/locations/"
    
    def get_form_kwargs(self):
        form_kwargs = super(PickupLocationsUpdate, self).get_form_kwargs()
        form_kwargs['user'] = self.request.user
        return form_kwargs

    model = PickupLocation
    template_name = 'location_form.html'
    form_class = PickupLocationForm

class PickupLocationsCreate(LoginRequiredMixin, CreateView):
    """Create a new pickup location"""
    def get_form_kwargs(self):
        form_kwargs = super(PickupLocationsCreate, self).get_form_kwargs()
        form_kwargs['user'] = self.request.user
        return form_kwargs

    def get_success_url(self):
        return "/locations/"

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.user = self.request.user
        obj.save()
        return super(PickupLocationsCreate, self).form_valid(form)

    model = PickupLocation
    template_name = 'location_form.html'
    form_class = PickupLocationForm
    
class AuctionUpdate(UpdateView):
    """The form users fill out to edit or create an auction"""
    
    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().created_by == self.request.user):
            raise PermissionDenied()
        lotsAlreadyCreated = Lot.objects.filter(auction=self.get_object().pk)
        if len(lotsAlreadyCreated):
            messages.warning(request, "Lots have already been added to this auction.  Don't make large changes!")
        return super().dispatch(request, *args, **kwargs)
    
    def get_success_url(self):
        return "/auctions/" + str(self.kwargs['slug'])
    
    model = Auction
    template_name = 'auction_form.html'
    form_class = CreateAuctionForm

class AuctionInvoices(DetailView):
    """List of invoices associated with an auction"""
    model = Auction
    template_name = 'auction_invoices.html'
    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().created_by == self.request.user):
            raise PermissionDenied()
        if not self.get_object().invoiced:
            messages.error(request, "This auction hasn't been invoiced yet.  Invoices will be created after the auction closes")
        self.auction = self.get_object()
        return super().dispatch(request, *args, **kwargs)
        
    def get_context_data(self, **kwargs):
        #user = User.objects.get(pk=self.request.user.pk)
        context = super().get_context_data(**kwargs)
        invoices = Invoice.objects.filter(auction=self.auction).order_by('paid','user__last_name')
        invoices = sorted(invoices, key=lambda t: str(t.location) ) 
        context['invoices'] = invoices
        return context

class AuctionStats(DetailView):
    """Fun facts about an auction"""
    model = Auction
    template_name = 'auction_stats.html'
    
    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().created_by == self.request.user):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.get_object().invoiced:
            messages.error(self.request, "This auction is still in progress, check back once it's finished for more complete stats")
        return context

class viewAndBidOnLot(FormMixin, DetailView):
    """Show the picture and detailed information about a lot, and allow users to place bids"""
    template_name = 'view_lot.html'
    model = Bid
    form_class = CreateBid
    queryset = Lot.objects.all()
    
    def get_context_data(self, **kwargs):
        lot = Lot.objects.get(pk=self.kwargs['pk'])
        if lot.auction:
            if lot.auction.first_bid_payout and not lot.auction.invoiced:
                if not self.request.user.is_authenticated or not Bid.objects.filter(user=self.request.user, lot_number__auction=lot.auction):
                    messages.success(self.request, f"Bid on (and win) any lot in the {lot.auction} and get ${lot.auction.first_bid_payout} back!")
        if lot.sealed_bid:
            try:
                defaultBidAmount = Bid.objects.get(user=self.request.user, lot_number=lot.pk).amount
            except:
                defaultBidAmount = None
        else:
            if lot.high_bidder:
                defaultBidAmount = lot.high_bid + 1
            else:
                # reserve price if there are no bids
                defaultBidAmount = lot.high_bid
        context = super(viewAndBidOnLot, self).get_context_data(**kwargs)
        context['watched'] = Watch.objects.filter(lot_number=self.kwargs['pk'], user=self.request.user.id)
        context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':self.kwargs['pk'], "amount":defaultBidAmount}, request=self.request)
        try:
            if not self.get_object().auction:
                context['user_tos'] = True
            else:
                AuctionTOS.objects.get(user=self.request.user.id, auction=self.get_object().auction)
                context['user_tos'] = True
        except:
            context['user_tos'] = False
        try:
            obj, created = UserData.objects.get_or_create(
                user = self.request.user,
                defaults={},
            )
            obj.last_activity = timezone.now()
            obj.save()
        except:
            pass
        return context
    
    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form, request)
        else:
            return self.form_invalid(form)

    def form_valid(self, form, request):
        lotNumber = form.cleaned_data['lot_number'].pk
        thisBid = form.cleaned_data['amount']
        form.user = User.objects.get(id=request.user.id)
        if not form.user:
            messages.error(request, "You need to be signed in to bid on a lot")
            return False
        lot = Lot.objects.get(pk=self.kwargs['pk'])
        highBidder = lot.high_bidder
        checksPass = True
        try:
            ban = UserBan.objects.get(banned_user=request.user.id, user=lot.user.pk)
            messages.error(request, "This user has banned you from bidding on their lots")
            checksPass = False
        except:
            pass
        try:
            ban = UserBan.objects.get(banned_user=request.user.id, user=lot.auction.created_by.pk)
            messages.error(request, "The owner of this auction has banned you from bidding")
            checksPass = False
        except:
            pass        
        if lot.ended:
            messages.error(request, "Bidding on this lot has ended.  You can no longer place bids")
            checksPass = False
        if checksPass:
            if lot.sealed_bid:
                was_high_bid = True
                messages.info(request, "Bid placed!  You can change your bid at any time until the auction ends")
            else:
                was_high_bid = False
                if (thisBid > lot.max_bid):
                    messages.info(request, "You're the high bidder!")
                    was_high_bid = True
                    # Send an email to the old high bidder
                    # @fixme, this is slow
                    if highBidder:
                        if request.user.id != highBidder.pk:
                            user = User.objects.get(pk=highBidder.pk)
                            email = user.email
                            link = f"https://auctions.toxotes.org/lots/{self.kwargs['pk']}/"
                            send_mail(
                            'You\'ve been outbid!',
                            f'You\'ve been outbid on lot {lot}!\nBid more here: {link}\n\nBest, auctions.toxotes.org',
                            'TFCB notifications',
                            [email],
                            fail_silently=False,
                            html_message = f'You\'ve been outbid on lot {lot}!<br><a href="{link}">Bid more here</a><br><br>Best, auctions.toxotes.org',
                            )
                else:
                    if ( (lot.max_bid == lot.reserve_price) and (thisBid >= lot.reserve_price) ):
                        messages.info(request, "Tip: bid high!  If no one else bids on this item, you'll still get it for the reserve price.  If someone else bids against you, you'll bid against them until you reach your limit.")
                        was_high_bid = True
                    else:
                        if lot.high_bidder.pk != request.user.id:
                            messages.warning(request, "You've been outbid!")
            # Create or update the bid model
            try:
                # check to see if this user has already bid, and bid more
                existingBid = Bid.objects.get(user=form.user, lot_number=lot)
                if lot.sealed_bid:
                    existingBid.amount = thisBid
                    existingBid.last_bid_time = timezone.now()
                    existingBid.save()
                else:
                    if thisBid >= lot.high_bid:
                        print(f"{request.user} has changed their bid on {lot} from ${existingBid.amount} to ${thisBid}")
                        existingBid.amount = thisBid
                        existingBid.last_bid_time = timezone.now()
                        if was_high_bid:
                            existingBid.was_high_bid = was_high_bid
                        existingBid.save()
                    else:
                        messages.warning(request, f"You can't bid less than ${lot.high_bid}")
            except:
                # create a new bid object
                print(f"{request.user} has bid on {lot}")
                form.was_high_bid = was_high_bid
                form.save() # record the bid regardless of whether or not it's the current high
                # also update category interest
                interest, created = UserInterestCategory.objects.get_or_create(
                    category=lot.species_category,
                    user=request.user,
                    defaults={ 'interest': 0 }
                    )
                interest.interest += settings.BID_WEIGHT
                interest.save()
        return super(viewAndBidOnLot, self).form_valid(form)

def createSpecies(name, scientific_name, category=False):
    """Create a new product/species"""
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

@login_required
def createLot(request):
    """Create a new lot"""
    if request.method == 'POST':
        if request.FILES:
            form = CreateLotForm(request.POST, request.FILES, user=request.user)
        else:
            form = CreateLotForm(request.POST, user=request.user)
        if form.is_valid():
            lot = form.save(commit=False)
            checksPass = True
            try:
                ban = UserBan.objects.get(banned_user=request.user.id, user=lot.auction.created_by.pk)
                messages.error(request, "The owner of this auction has banned you from submitting lots")
                checksPass = False
            except:
                pass   
            if checksPass:
                # If this is a tank, set it to not transportable
                if lot.species:
                    # if this is not breedable, remove the breeder points
                    # they can still be added back in by editing the lot
                    if not lot.species.breeder_points:
                        lot.i_bred_this_fish = False
                    if lot.species.pk == 592:
                        lot.transportable = False
                if "tank" in lot.lot_name.lower():
                    lot.transportable = False
                if "aquarium" in lot.lot_name.lower():
                    lot.transportable = False
                lot.user = User.objects.get(id=request.user.id)
                if form.cleaned_data['create_new_species']:
                    lot.species = createSpecies(form.cleaned_data['new_species_name'], form.cleaned_data['new_species_scientific_name'], form.cleaned_data['species_category'])
                if lot.auction:
                    userData, created = UserData.objects.get_or_create(user=request.user.id)
                    userData.last_auction_used = lot.auction
                    userData.save()
                # someday we may change this to be a field on the form, but for now we need to collect data
                lot.promotion_weight = randint(0, 20)
                lot.save()            
                print(str(lot.user) + " has created a new lot " + lot.lot_name)
                # if there's another lot in the same category already with no bids, warn about it
                existingLot = Lot.objects.annotate(num_bids=Count('bid')).filter(num_bids=0, species_category=lot.species_category, user=request.user.pk, active=True).exclude(lot_number=lot.pk)
                if existingLot:
                    messages.info(request, "Tip: you've already got lots in this category with no bids.  Don't submit too many similar lots unless you're sure there's interest")
                if lot.auction:
                    try:
                        AuctionTOS.objects.get(user=request.user.id, auction=lot.auction)
                    except:
                        messages.error(request, f"You need to <a href='/auctions/{lot.auction.slug}'>confirm your pickup location for this auction</a> before people can bid on this lot.")        
                messages.info(request, f"Created lot!  <a href='/lots/{lot.pk}'>View or edit your last lot</a> or fill out this form again to add another lot.  <a href='/lots/my'>All submitted lots</a>")
                
            form = CreateLotForm(user=request.user) # no post data here to reset the form
    else:
        # if the user hasn't filled out their contact info, redirect:
        userdataComplete = True
        try:
            userData = UserData.objects.get(user=request.user.pk)
            if not userData.phone_number or not userData.address or not userData.location:
                userdataComplete = False
        except:
            userdataComplete = False
        if not userdataComplete:
            messages.warning(request, "Please fill out your phone, address, and location before creating a lot")
            return redirect(f'/users/edit/{request.user.pk}/?next=/lots/new/')
        form = CreateLotForm(user=request.user)
    return render(request,'lot_form.html', {'form':form})

class LotUpdate(UpdateView):
    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            raise PermissionDenied()
        if not self.get_object().can_be_deleted:
            messages.error(request, "It's too late to edit this lot")
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)
    
    def get_success_url(self):
        return "/lots/" + str(self.kwargs['pk'])
    
    def form_valid(self, form):
        lot = form.save(commit=False)
        if form.cleaned_data['create_new_species']:
            lot.species = createSpecies(form.cleaned_data['new_species_name'], form.cleaned_data['new_species_scientific_name'], form.cleaned_data['species_category'])
        lot.save()
        return super(LotUpdate, self).form_valid(form)

    model = Lot
    template_name = 'lot_form.html'
    form_class = CreateLotForm
    
class LotDelete(DeleteView):
    model = Lot
    def dispatch(self, request, *args, **kwargs):
        if not self.get_object().can_be_deleted:
            messages.error(request, "Only new lots can be deleted")
            raise PermissionDenied()
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            raise PermissionDenied()
        if self.get_object().high_bidder:
            messages.error(request, "Bids have already been placed on this lot")
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return "/"

@login_required
def createAuction(request):
    if request.method == 'POST':
        form = CreateAuctionForm(request.POST)
        if form.is_valid():
            auction = form.save(commit=False)
            auction.created_by = User.objects.get(id=request.user.id)
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_end
            auction.save()            
            print(str(auction.created_by) + " has created a new auction " + auction.title)
            messages.info(request, "Auction created!  Now, create a location to exchange lots.")
            response = redirect('/locations/new/')
            # Perhaps we should redirect to the auction edit page?
            return response
    else:
        form = CreateAuctionForm()
    return render(request,'auction_form.html', {'form':form})

class AuctionInfo(FormMixin, DetailView):
    """Main view of a single auction"""
    template_name = 'auction.html'
    model = Auction
    form_class = AuctionTOSForm
    
    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['next'] = f'/lots/?auction={self.get_object().slug}'
        return data['next']

    def get_form_kwargs(self):
        form_kwargs = super(AuctionInfo, self).get_form_kwargs()
        form_kwargs['user'] = self.request.user
        form_kwargs['auction'] = self.get_object()
        return form_kwargs

    def dispatch(self, request, *args, **kwargs):
        if self.get_object().created_by.pk == request.user.pk:
            locations = PickupLocation.objects.filter(auction=self.get_object())
            if not locations:
                messages.add_message(self.request, messages.ERROR, "You haven't added any pickup locations to this auction yet. <a href='/locations/new/'>Add one now</a>")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        owner = self.get_object().created_by
        context['contact_email'] = User.objects.get(pk=owner.pk).email
        context['pickup_locations'] = PickupLocation.objects.filter(auction=self.get_object())
        current_site = Site.objects.get_current()
        context['domain'] = current_site.domain
        if timezone.now() > self.get_object().date_end:
            context['ended'] = True
            messages.add_message(self.request, messages.ERROR, f"This auction has ended.  You can't bid on anything, but you can still <a href='/lots/?auction={self.get_object().slug}'>view lots</a>.")
        else:
            context['ended'] = False
        try:
            existingTos = AuctionTOS.objects.get(user=self.request.user, auction=self.get_object())
            existingTos = existingTos.pickup_location
        except:
            existingTos = None
            if self.request.user.is_authenticated and not context['ended']:
                messages.add_message(self.request, messages.ERROR, "Please confirm you have read these rules by selecting your pickup location at the bottom of this page.")
        context['form'] = AuctionTOSForm(user=self.request.user, auction=self.get_object(), initial={'user': self.request.user.id, 'auction':self.get_object().pk, 'pickup_location':existingTos})
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        if form.is_valid():
            obj, created = AuctionTOS.objects.get_or_create(
                user = self.request.user,
                auction = self.get_object(),
                defaults={'pickup_location': form.cleaned_data['pickup_location']},
            )
            obj.pickup_location = form.cleaned_data['pickup_location']
            obj.save()
            # also update userdata to reflect the last auction
            obj, created = UserData.objects.get_or_create(
                user = self.request.user,
                defaults={},
            )
            obj.last_auction_used = self.get_object()
            obj.save()
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

def aboutSite(request):
    return render(request,'about.html')

def toDefaultLandingPage(request):
    """
    Allow the user to pick up where they left off
    """
    def tos_check(request, auction):
        if not auction:
            return redirect('/auctions/all')
        try:
            # Did the user sign the tos yet?
            AuctionTOS.objects.get(user=request.user, auction=auction)
            # If so, redirect them to the lot view
            return redirect(f'/lots?auction={auction.slug}')
        except:
            # No tos?  Take them there so they can sign
            return redirect(f"/auctions/{auction.slug}/")

    data = request.GET.copy()
    try:
        # if the slug was set in the URL
        auction = Auction.objects.filter(slug=list(data.keys())[0])[0]
        return tos_check(request, auction)
    except:
        # if not, check and see if the user has been participating in an auction
        try:
            auction = UserData.objects.get(user=request.user).last_auction_used
            if timezone.now() > auction.date_end:
                try:
                    invoice = Invoice.objects.get(user=request.user, auction=auction.pk)
                    messages.add_message(request, messages.INFO, f'{auction} has ended.  <a href="/invoices/{invoice.pk}">View your invoice</a>, <a href="/feedback/">leave feedback</a> on lots you bought or sold, or <a href="/lots?auction={auction.slug}">view lots</a>')
                except:
                    pass
                auction = None
        except:
            # probably no userdata or userdata.auction is None
            auction = None
    try:
        obj, created = UserData.objects.get_or_create(
            user = request.user,
            defaults={},
        )
        obj.last_activity = timezone.now()
        obj.save()
    except:
        pass
    return tos_check(request, auction)

@login_required
def toAccount(request):
    response = redirect('/users/edit/' + str(request.user.id))
    return response

class allAuctions(ListView):
    model = Auction
    template_name = 'all_auctions.html'
    ordering = ['-date_end']
    
    def get_queryset(self):
        new_context = Auction.objects.exclude(promote_this_auction=False).order_by('-date_end')
        return new_context

    def get_context_data(self, **kwargs):
        try:
            user = User.objects.get(pk=self.request.user.pk)
            nameSet = True
            if not user.first_name or not user.last_name:
                nameSet = False
            locationSet = True
            try:
                prefs = UserData.objects.get(user=self.request.user.pk)
                if not (prefs.location and prefs.address):
                    locationSet = False
            except:
                locationSet = False
            if not locationSet and not nameSet:
                messages.add_message(self.request, messages.INFO, 'Set your name and location in your <a href="/account/">account</a>')
            elif not locationSet:
                messages.add_message(self.request, messages.INFO, 'Set your location and address in your <a href="/account/">account</a>')
            elif not nameSet:
                messages.add_message(self.request, messages.INFO, 'Set your name in your <a href="/account/">account</a>')
            
        except:
            pass
        # set default values
        # data = self.request.GET.copy()
        # if len(data) == 0:
        #     data['status'] = "open"
        context = super().get_context_data(**kwargs)
        # context['filter'] = LotFilter(data, queryset=self.get_queryset())
        # context['view'] = 'all'
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

class AllLots(LotListView):
    """Show all lots"""
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['view'] = 'all'
        return context

class Invoices(ListView):
    """Get all invoices for the current user"""
    model = Invoice
    template_name = 'all_invoices.html'
    ordering = ['-date']
    
    def get_queryset(self):
        new_context = Invoice.objects.filter(
            user=self.request.user.pk,
        )
        return new_context

    def get_context_data(self, **kwargs):
        #user = User.objects.get(pk=self.request.user.pk)
        context = super().get_context_data(**kwargs)
        # context['filter'] = LotFilter(data, queryset=self.get_queryset())
        # context['view'] = 'all'
        return context

# password protected in views.py
class InvoiceView(DetailView, FormMixin):
    """Show a single invoice"""
    template_name = 'invoice.html'
    model = Invoice
    form_class = InvoiceUpdateForm
    
    def get_object(self):
        """Overridden to allow display of an example"""
        try:
            self.exampleMode = False
            return Invoice.objects.get(pk=self.kwargs.get(self.pk_url_kwarg))
        except:
            self.exampleMode = True
            return Invoice.objects.get(pk=152) # this is a good example
            
    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        auth = False
        thisInvoice = Invoice.objects.get(pk=self.get_object().pk)
        
        if self.exampleMode:
            auth = True
        elif self.get_object().user.pk == request.user.pk:
            auth = True
            # mark the invoice as opened if this is the user it's intended for
            thisInvoice.opened = True
            thisInvoice.save()
        elif request.user.is_superuser :
            auth = True
        else:
            # if this user create the auction, they can see the invoice
            try:
                thisAuction = Auction.objects.get(pk=self.get_object().auction.pk)
                if thisAuction.created_by.pk == request.user.pk: 
                    auth = True
            except:
                pass
        if not auth:
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(InvoiceView, self).get_context_data(**kwargs)
        sold = Lot.objects.filter(seller_invoice=self.get_object()).order_by('lot_number')
        bought = Lot.objects.filter(buyer_invoice=self.get_object()).order_by('lot_number')
        userdata = UserData.objects.get(user=self.get_object().user.pk)
        context['exampleMode'] = self.exampleMode
        # light theme for some invoices to allow printing
        if 'print' in self.request.GET.copy():
            context['base_template_name'] = "print.html"
            context['show_links'] = False
        else:
            context['base_template_name'] = "base.html"
            context['show_links'] = True
        try:
            # sort sold by winner's location
            sold = sorted(sold, key=lambda t: str(t.winner_location) ) 
            # sort bought by lot number
            #bought = sorted(bought, key=lambda t: str(t.location))
            bought = sorted(bought, key=lambda t: str(t.lot_number))
        except:
            pass
        context['sold'] = sold
        context['bought'] = bought
        context['userdata'] = userdata
        try:
            context['auction'] = Auction.objects.get(pk=self.get_object().auction.pk)
            context['contact_email'] = User.objects.get(pk=context['auction'].created_by.pk).email
            context['location'] = AuctionTOS.objects.get(user=self.get_object().user.pk, auction=self.get_object().auction.pk).pickup_location
        except:
            context['auction'] = False
            context['location'] = False
            context['contact_email'] = False
        context['form'] = InvoiceUpdateForm(initial={
            'adjustment_direction': self.get_object().adjustment_direction,
            'adjustment':self.get_object().adjustment,
            "adjustment_notes":self.get_object().adjustment_notes
            })
        return context
   
    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form, request)
        else:
            return self.form_invalid(form)

    def form_valid(self, form, request):
        adjustment_direction = form.cleaned_data['adjustment_direction']
        adjustment = form.cleaned_data['adjustment']
        adjustment_notes = form.cleaned_data['adjustment_notes']
        auth = False
        invoice = self.get_object()
        if invoice.auction.created_by.pk == request.user.pk:
            auth = True
        if request.user.is_superuser :
            auth = True
        if not auth:
            raise PermissionDenied()
        invoice.adjustment_direction = adjustment_direction
        invoice.adjustment = adjustment
        invoice.adjustment_notes = adjustment_notes
        invoice.save()
        return super(InvoiceView, self).form_valid(form)

@login_required
def getSpecies(request):
    if request.method == 'POST':
        species = request.POST['search']
        result = Product.objects.filter(
            Q(common_name__icontains=species) | Q(scientific_name__icontains=species)
            ).values()
        return JsonResponse(list(result), safe=False)
     

class UserView(DetailView):
    """View information about a single user"""
    template_name = 'user.html'
    model = User

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            context['data'] = UserData.objects.get(user=self.object.pk)
        except:
            context['data'] = False
        try:
            context['banned'] = UserBan.objects.get(user=self.request.user.pk, banned_user=self.object.pk)
            #print(context['banned'])
        except:
            context['banned'] = False
        try:
            context['seller_feedback'] = Lot.objects.filter(user=self.object.pk).exclude(feedback_text__isnull=True).order_by("-date_posted")
        except:
            context['seller_feedback'] = None
        try:
            context['buyer_feedback'] = Lot.objects.filter(winner=self.object.pk).exclude(winner_feedback_text__isnull=True).order_by("-date_posted")
        except:
            context['buyer_feedback'] = None
        return context

class UserUpdate(UpdateView, SuccessMessageMixin):
    """Make changes to a users info"""
    template_name = 'user_form.html'
    model = User
    
    success_message = 'User settings updated'

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().pk == request.user.pk:
            auth = True
        if request.user.is_superuser :
            auth = True
        if not auth:
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data['next'] = "/users/" + str(self.kwargs['pk'])
        return data['next']
    
    def get_initial(self):
        try:
            prefs = UserData.objects.get(user=self.get_object().pk)
            return {'phone_number': prefs.phone_number, 'club': prefs.club, 'location': prefs.location, 'address': prefs.address, 'email_visible': prefs.email_visible, 'use_list_view': prefs.use_list_view}
            #return prefs
        except:
            return

    form_class = UpdateUserForm

    def form_valid(self, form):
        user = form.save(commit=False)
        club = form.cleaned_data['club']
        try:
            prefs = UserData.objects.get(user=user)
        except:
            prefs = UserData.objects.create(
                user=user
            )
        prefs.phone_number=form.cleaned_data['phone_number']
        prefs.club=form.cleaned_data['club']
        prefs.location=form.cleaned_data['location']
        prefs.address=form.cleaned_data['address']
        prefs.email_visible = form.cleaned_data['email_visible']
        prefs.use_list_view = form.cleaned_data['use_list_view']
        prefs.save()
        user.save()
        return super(UserUpdate, self).form_valid(form)

class UserChartView(View):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            user = self.kwargs.get('pk', None)
            allBids = Bid.objects.select_related('lot_number__species_category').filter(user=user)
            pageViews = PageView.objects.select_related('lot_number__species_category').filter(user=user)
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
        raise PermissionDenied()

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
        raise PermissionDenied()

class IgnoreCategoriesView(TemplateView):
    template_name = 'ignore_categories.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context

class CreateUserIgnoreCategory(View):
    """Add category with given pk to ignore list"""
    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            raise PermissionDenied()
        pk = self.kwargs.get('pk', None)
        category = Category.objects.get(pk=pk)
        result, created = UserIgnoreCategory.objects.update_or_create(category=category, user=self.request.user)
        return JsonResponse(data={'pk': result.pk})

class DeleteUserIgnoreCategory(View):
    """Allow users to see lots in a given category again."""
    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            raise PermissionDenied()
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
            raise PermissionDenied()
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

class AuctionChartView(View):
    """GET methods for generating auction charts"""
    def get(self, request, *args, **kwargs):
        data = request.GET.copy()
        try:
            auction = slug=data['auction']
        except:
            return HttpResponse('auction is required')
        if auction == "none":
            pass
        else:
            try:
                auction = Auction.objects.get(slug=auction)
            except:
                return HttpResponse(f'auction {auction} not found')
        if not self.permissionCheck(request, auction):
            raise PermissionDenied
        try:
            chart = data['chart']
        except:
            return HttpResponse('chart not specified')
        if chart == "funnel":
            """
            Inverted funnel chart showing user participation
            """
            try:
                allViews = Lot.objects.filter(auction=auction).annotate(num_views=Count('pageview')).order_by("-num_views")
                maxAllViews = allViews[0].num_views
                medianAllViews = median_value(allViews, 'num_views')
                signedInViews = Lot.objects.filter(auction=auction).annotate(
                        num_views=Count(Case(
                            When(pageview__user__isnull=False, then=1),
                            output_field=IntegerField(),
                        ))
                    ).order_by("-num_views")
                maxSignedInViews = signedInViews[0].num_views
                medianSignedInViews = median_value(signedInViews, 'num_views')
                # this gets a little tricky
                # We don't have a way to record the total number of unique visitors without an account for a given auction
                # But, we can calculate the ratio of median signed in to all:
                maxRatio = maxAllViews / maxSignedInViews
                medianRatio = medianAllViews / medianSignedInViews
                # then get the average of those:
                ratio = ( maxRatio + medianRatio ) / 2
                usersWhoViewed = len(User.objects.filter(pageview__lot_number__auction=auction).annotate(dcount=Count('id')))
                totalUsers = int(usersWhoViewed * ratio)
            except:
                totalUsers = 0
                usersWhoViewed = 0
            try:
                userWhoBid = len(User.objects.filter(bid__lot_number__auction=auction).annotate(dcount=Count('id')))
                #usersWhoSold = len(User.objects.filter(lot__auction=auction).annotate(dcount=Count('id')))
            except:
                userWhoBid = 0
            try:
                usersWhoWon = len(User.objects.filter(winner__auction=auction).annotate(dcount=Count('id')))
                # could add filtering for only sold lots here
                #soldLots = Lot.objects.filter(auction=auction, winner__isnull=False)
                #len(User.objects.filter(lot__in=soldLots).annotate(dcount=Count('id')))
            except:
                usersWhoWon = 0
            labels = [  "Total unique views (estimated)",
                        "Users who viewed lots",
                        "Users who bid on at least one item",
                        "Users who won at least one item",
                        ]
            data = [totalUsers,
                    usersWhoViewed,
                    userWhoBid,
                    usersWhoWon,
                    ]    
            return JsonResponse(data={
                'labels': labels,
                'data': data,
            })
        if chart == "lotprice":
            """
            Lot sell price, broken out into bins
            """
            try:
                binSize = int(data['bin'])
            except:
                binSize = 2
            if binSize == 0:
                binSize = 2
            labels = ["Not sold"]
            lots = Lot.objects.filter(auction=auction)
            data = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
            last = 1
            for i in range(len(data)-2):
                nextNumber = (i*binSize)+binSize
                labels.append(f"${last}-{nextNumber}")
                last = nextNumber
            labels.append(f"More than ${last}")
            for lot in lots:
                if not lot.winning_price:
                    lot.winning_price = 0
                    priceBin = 0
                else:
                    priceBin = int(lot.winning_price / binSize)
                    if priceBin == 0:
                        priceBin = 1
                if priceBin > 21:
                    priceBin = 21
                data[priceBin] += 1
            return JsonResponse(data={
                'labels': labels,
                'data': data,
            })
        if chart == "lotbids":
            """
            How many bidders were there per lot
            """
            lots = Lot.objects.filter(auction=auction)
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
        if chart == "categories":
            """
            Categories by views and lots sold
            """
            try:
                top = int(data['top'])
            except:
                top = 20
            labels = []
            views = []
            bids = []
            lots = []
            volumes = []
            if auction == "none":
                # public view to get data for all auctions
                categories = Category.objects.all().annotate(num_lots=Count('lot')).order_by('-num_lots')
                allLots = len(Lot.objects.all())
                allViews = len(PageView.objects.all())
                allBids = len(Bid.objects.all())
                allVolume = Lot.objects.all().aggregate(Sum('winning_price'))['winning_price__sum']
            else:
                categories = Category.objects.filter(lot__auction=auction).annotate(num_lots=Count('lot')).order_by('-num_lots')
                allLots = len(Lot.objects.filter(auction=auction))
                allViews = len(PageView.objects.filter(lot_number__auction=auction))
                allBids = len(Bid.objects.filter(lot_number__auction=auction))
                allVolume = Lot.objects.filter(auction=auction).aggregate(Sum('winning_price'))['winning_price__sum']                
            if allLots:
                for category in categories[:top]:
                    labels.append(str(category))
                    if auction == "none":
                        thisViews = len(PageView.objects.filter(lot_number__species_category=category))
                        thisBids = len(Bid.objects.filter(lot_number__species_category=category))
                        thisVolume = Lot.objects.filter(species_category=category).aggregate(Sum('winning_price'))['winning_price__sum']
                    else:
                        thisViews = len(PageView.objects.filter(lot_number__auction=auction, lot_number__species_category=category))
                        thisBids = len(Bid.objects.filter(lot_number__auction=auction, lot_number__species_category=category))
                        thisVolume = Lot.objects.filter(auction=auction, species_category=category).aggregate(Sum('winning_price'))['winning_price__sum']
                    percentOfLots = round(((category.num_lots / allLots) * 100),2)
                    percentOfViews = round(((thisViews / allViews) * 100),2)
                    percentOfBids = round(((thisBids / allBids) * 100),2)
                    if allVolume and thisVolume:
                        percentOfVolume = round(((thisVolume / allVolume) * 100),2)
                    else:
                        percentOfVolume = 0
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
        return JsonResponse(data={
                'club_profit_raw': auction.club_profit_raw,
                'club_profit': auction.club_profit,
                'total_to_sellers': auction.total_to_sellers,
                'percent_to_club': auction.percent_to_club,
                'notes': "no chart type set, here's some fun info about the auction",
            }) 

    def permissionCheck(self, request, auction):
        if auction == "none":
            return True
        if request.user.is_superuser:
            return True
        elif auction.created_by.pk == request.user.pk:
            return True
        return False


