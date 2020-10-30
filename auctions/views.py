from datetime import datetime
from itertools import chain
from django.contrib.auth.models import User
from django.shortcuts import render,redirect
from django.http import HttpResponse
from django.http import JsonResponse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, DetailView, View, TemplateView
from django.views.generic.edit import FormMixin
from django.urls import reverse
from django.views.generic.edit import UpdateView
from django.views.generic.edit import DeleteView
from django.db.models import Q
from django.contrib.messages.views import SuccessMessageMixin
from allauth.account.models import EmailAddress
from el_pagination.views import AjaxListView

from .models import *
from .filters import *
from .forms import *

def index(request):
    return HttpResponse("this page is intentionally left blank")

class LotListView(AjaxListView):
    """This is a base class that shows lots, with a filter.  This class is never used directly, but it's a parent for several other classes.
    The context is overridden to set the view type"""
    model = Lot
    template_name = 'all_lots.html'
    page_template='lot_list_page.html'
    #paginate_by = 50
    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        if len(data) == 0:
            data['status'] = "open"
        context = super().get_context_data(**kwargs)
        if self.request.GET.get('page'):
            del data['page'] # required for pagination to work
        context['filter'] = LotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=True)
        try:
            context['lotsAreHidden'] = len(UserIgnoreCategory.objects.filter(user=self.request.user))
        except:
            # probably not signed in
            context['lotsAreHidden'] = -1

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
                    lot.save()
        return redirect('/users/' + str(pk))

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

def newpageview(request, pk):
    """Initail pageview to record page views to the PageView model"""
    if request.method == 'POST':
        user = request.user
        lot_number = Lot.objects.get(pk=pk)
        if not user.is_authenticated:
            user = None
            PageView.objects.create(
                lot_number=lot_number,
                user=user,
                date_end=timezone.now(),
            )
        else:
            obj, created = PageView.objects.update_or_create(
                lot_number=lot_number,
                user=user,
                defaults={},
            )
            obj.date_end = timezone.now()
            obj.save()
        return HttpResponse("Success")

def pageview(request, pk):
    """Continued interest in a lot will update the time in page views to the PageView model"""
    if request.method == 'POST':
        user = request.user
        if user.is_authenticated:
            # we don't track duration for anonymous views
            lot_number = Lot.objects.get(pk=pk)
            obj, created = PageView.objects.update_or_create(
                lot_number=lot_number,
                user=user,
                defaults={},
            )
            obj.date_end = timezone.now()
            obj.total_time += 10
            obj.save()
        return HttpResponse("Success")

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
        invoices = Invoice.objects.filter(auction=self.auction).order_by('paid','-user__last_name')
        invoices = sorted(invoices, key=lambda t: str(t.user.userdata.location) ) 
        context['invoices'] = invoices
        # also need to create email lists on a per-location basis
        locations = {}
        for invoice in invoices:
            location = str(invoice.user.userdata.location)
            try:
                locations[location]['emails'] += f";{invoice.user.email}"
            except:
                locations[location] = {'emails':invoice.user.email}
        context['locations'] = locations
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
        #owner = self.get_object().created_by
        #context['contact_email'] = User.objects.get(pk=owner.pk).email
        return context

# password protected in urls.py
class viewAndBidOnLot(FormMixin, DetailView):
    """Show the picture and detailed information about a lot, and allow users to place bids"""
    template_name = 'view_lot.html'
    model = Bid
    form_class = CreateBid
    queryset = Lot.objects.all()
    
    def get_context_data(self, **kwargs):
        if Lot.objects.get(pk=self.kwargs['pk']).high_bidder:
            defaultBidAmount = Lot.objects.get(pk=self.kwargs['pk']).high_bid + 1
        else:
            # reserve price if there are no bids
            defaultBidAmount = Lot.objects.get(pk=self.kwargs['pk']).high_bid
        context = super(viewAndBidOnLot, self).get_context_data(**kwargs)
        context['watched'] = Watch.objects.filter(lot_number=self.kwargs['pk'], user=self.request.user.id)
        context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':self.kwargs['pk'], "amount":defaultBidAmount}, request=self.request)
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
                    messages.warning(request, "You've been outbid!")
            # Create or update the bid model
            try:
                # check to see if this user has already bid, and bid more
                existingBid = Bid.objects.get(user=form.user, lot_number=lot)
                if thisBid > existingBid.amount:
                    print(f"{request.user} has upped their bid on {lot} from ${existingBid.amount} to ${thisBid}")
                    existingBid.amount = thisBid
                    if was_high_bid:
                        existingBid.was_high_bid = was_high_bid
                    existingBid.save()
                else:
                    messages.warning(request, "You can't bid less than you've already bid")
            except:
                # create a new bid model
                print(f"{request.user} has bid on {lot}")
                form.was_high_bid = was_high_bid
                form.save() # record the bid regardless of whether or not it's the current high
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
                lot.save()            
                print(str(lot.user) + " has created a new lot " + lot.lot_name)
                messages.info(request, "Created lot!  Fill out this form again to add another lot.  <a href='/lots/my'>All submitted lots</a>")
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
        if self.get_object().high_bidder:
            messages.error(request, "Bids have already been placed on this lot")
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
            auction.save()            
            print(str(auction.created_by) + " has created a new auction " + auction.title)
            messages.info(request, "Auction created")
            response = redirect('/')
            # Perhaps we should redirect to the auction edit page?
            return response
    else:
        form = CreateAuctionForm()
    return render(request,'auction_form.html', {'form':form})

class AuctionInfo(DetailView):
    """Main view of a single auction"""
    template_name = 'auction.html'
    model = Auction
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        owner = self.get_object().created_by
        context['contact_email'] = User.objects.get(pk=owner.pk).email
        return context

def aboutSite(request):
    return render(request,'about.html')

def toDefaultLandingPage(request):
    response = redirect('/auctions/all')
    return response

@login_required
def toAccount(request):
    response = redirect('/users/edit/' + str(request.user.id))
    return response

class allAuctions(ListView):
    model = Auction
    template_name = 'all_auctions.html'
    ordering = ['-date_end']
    
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
                messages.add_message(self.request, messages.INFO, 'Set your name your <a href="/account/">account</a>')
            
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
class InvoiceView(DetailView): #FormMixin
    """Show a single invoice"""
    template_name = 'invoice.html'
    model = Invoice
    
    def dispatch(self, request, *args, **kwargs):
        auth = False
        thisInvoice = Invoice.objects.get(pk=self.get_object().pk)
        if self.get_object().user.pk == request.user.pk:
            auth = True
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
        sold = Lot.objects.filter(seller_invoice=self.get_object()).order_by('winner')
        bought = Lot.objects.filter(buyer_invoice=self.get_object()).order_by('user')
        try:
            sold = sorted(sold, key=lambda t: t.location_as_str ) 
            bought = sorted(bought, key=lambda t: t.location_as_str)
        except:
            pass
        context['sold'] = sold
        context['bought'] = bought
        try:
            context['auction'] = Auction.objects.get(pk=self.get_object().auction.pk)
            context['contact_email'] = User.objects.get(pk=context['auction'].created_by.pk).email
        except:
            context['auction'] = False
            context['contact_email'] = False
        return context
   
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
            print(context['banned'])
        except:
            context['banned'] = False
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
            return {'phone_number': prefs.phone_number, 'club': prefs.club, 'location': prefs.location, 'address': prefs.address, 'email_visible': prefs.email_visible}
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
        #print(results)
            
        return JsonResponse({'results':results},safe=False)
	# lv0=levels0()
	# lv0_list=[]
	# for lv_0 in lv0:
	# 	lv0_list.append({'id':lv_0,'name':lv_0})
	# if request.GET.get('q'):
	# 	q=request.GET['q']
	# 	lv0_list=list(filter(lambda d: d['name'] in q, lv0_list))
	#return JsonResponse({'results':lv0_list},safe=False)

#     {
#             "results": [
#                 {
#                 "id": 1,
#                 "text": "Option 1"
#                 },
#                 {
#                 "id": 2,
#                 "text": "Option 2",
#                 "selected": true
#                 },
#                 {
#                 "id": 3,
#                 "text": "Option 3",
#                 "disabled": true
#                 }
#             ]
# }
