from datetime import datetime
from itertools import chain
from django.contrib.auth.models import User
from django.shortcuts import render,redirect
from django.http import HttpResponse
from django.http import JsonResponse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, DetailView
from django.views.generic.edit import FormMixin
from django.urls import reverse
from django.views.generic.edit import UpdateView
from django.views.generic.edit import DeleteView
from django.db.models import Q
from django.contrib.messages.views import SuccessMessageMixin
from allauth.account.models import EmailAddress

from .models import *
from .filters import *
from .forms import *

def index(request):
    return HttpResponse("this page is intentionally left blank")

# password protected in urls.py
class myWonLots(ListView):
    model = Lot
    template_name = 'all_lots.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # set default values
        data = self.request.GET.copy()
        if len(data) == 0:
            data['status'] = "closed"
        context['filter'] = UserWonLotFilter(data, queryset=self.get_queryset(), request=self.request)
        context['view'] = 'mywonlots'
        return context

# password protected in urls.py
class myBids(ListView):
    model = Lot
    template_name = 'all_lots.html'

    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        if len(data) == 0:
            data['status'] = "open"
        context = super().get_context_data(**kwargs)
        context['filter'] = UserBidLotFilter(data, queryset=self.get_queryset(), request=self.request)
        context['view'] = 'mybids'
        return context

# password protected in urls.py
class myLots(ListView):
    model = Lot
    template_name = 'all_lots.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['filter'] = UserOwnedLotFilter(self.request.GET, queryset=self.get_queryset(), request=self.request)
        context['view'] = 'mylots'
        return context
        
# password protected in urls.py
class myWatched(ListView):
    model = Lot
    template_name = 'all_lots.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['filter'] = UserWatchLotFilter(self.request.GET, queryset=self.get_queryset(), request=self.request)
        context['view'] = 'watch'
        return context

@login_required
def watchOrUnwatch(request, pk):
    if request.method == 'POST':
        watch = request.POST['watch']
        user = request.user
        lot_number = Lot.objects.filter(pk=pk)[0]
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

class AuctionUpdate(UpdateView):
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
            messages.warning(request, "You need to be signed in to bid on a lot")
            return False
        lot = Lot.objects.get(pk=self.kwargs['pk'])
        highBidder = lot.high_bidder
        
        if lot.ended:
            messages.error(request, "")
        else:
            if (thisBid > lot.max_bid):
                messages.info(request, "")
                # Send an email to the old high bidder
                # @fixme, this is slow
                if highBidder:
                    if request.user.id != highBidder.pk:
                        user = User.objects.get(pk=highBidder.pk)
                        email = user.email
                        link = f"auctions.toxotes.org/lots/{self.kwargs['pk']}/"
                        send_mail(
                        'You\'ve been outbid!',
                        f'You\'ve been outbid on lot {lot}!\nBid more here: {link}\n\nBest, auctions.toxotes.org',
                        'TFCB notifications',
                        [email],
                        fail_silently=False,
                        html_message = f'You\'ve been outbid on lot {lot}!<br><a href="{link}">Bid more here</a><br><br>Best, auctions.toxotes.org',
                        )
            else:
                messages.info(request, "BidFailed")
            
            # Create or update the bid model
            try:
                # check to see if this user has already bid, and bid more
                existingBid = Bid.objects.get(user=form.user, lot_number=lot)
                if thisBid > existingBid.amount:
                    print(f"{request.user} has upped their bid on {lot} from ${existingBid.amount} to ${thisBid}")
                    existingBid.amount = thisBid
                    existingBid.save()
                else:
                    messages.warning(request, "You can't bid less than you've already bid")
            except:
                # create a new bid model
                print(f"{request.user} has bid on {lot}")
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
            form = CreateLotForm(request.POST, request.FILES)
        else:
            form = CreateLotForm(request.POST)
        if form.is_valid():
            lot = form.save(commit=False)
            # If this is a tank, set it to not transportable
            if lot.species:
                if lot.species.pk == 592:
                    lot.transportable = False
            if "tank" in lot.lot_name.lower():
                lot.transportable = False
            if "aquarium" in lot.lot_name.lower():
                lot.transportable = False
            lot.user = User.objects.get(id=request.user.id)
            if form.cleaned_data['create_new_species']:
                lot.species = createSpecies(form.cleaned_data['new_species_name'], form.cleaned_data['new_species_scientific_name'], form.cleaned_data['species_category'])
            lot.save()            
            print(str(lot.user) + " has created a new lot " + lot.lot_name)
            messages.info(request, "Created lot!  Fill out this form again to add another lot.  <a href='/lots/my'>All submitted lots")
            form = CreateLotForm() # no post data here to reset the form
    else:
        form = CreateLotForm()
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

class auction(DetailView):
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
                prefs = UserPreferences.objects.get(user=self.request.user.pk)
                if not UserPreferences.location:
                    locationSet = False
            except:
                locationSet = False
            if not locationSet and not nameSet:
                messages.add_message(self.request, messages.INFO, 'Set your name and location in your <a href="/account/">account</a>')
            elif not locationSet:
                messages.add_message(self.request, messages.INFO, 'Set your location in your <a href="/account/">account</a>')
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

class allLots(ListView):
    model = Lot
    template_name = 'all_lots.html'

    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        if len(data) == 0:
            data['status'] = "open"
        context = super().get_context_data(**kwargs)
        context['filter'] = LotFilter(data, queryset=self.get_queryset())
        context['view'] = 'all'
        return context

class invoices(ListView):
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
class invoice(DetailView): #FormMixin
    """Show a single invoice"""
    template_name = 'invoice.html'
    model = Invoice
    
    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if request.user.is_superuser :
            auth = True
        if not auth:
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(invoice, self).get_context_data(**kwargs)
        context['sold'] = Lot.objects.filter(seller_invoice=self.get_object())
        context['bought'] = Lot.objects.filter(buyer_invoice=self.get_object())
        context['auction'] = Auction.objects.get(pk=self.get_object().auction.pk)
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
            context['preferences'] = UserPreferences.objects.get(user=self.object.pk)
        except:
            pass
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
        return "/users/" + str(self.kwargs['pk'])
    
    def get_initial(self):
        try:
            prefs = UserPreferences.objects.get(user=self.get_object().pk)
            return {'phone': prefs.phone_number, 'club': prefs.club, 'location': prefs.location, 'address': prefs.address}
        except:
            return

    form_class = UpdateUserForm

    def form_valid(self, form):
        user = form.save(commit=False)
        phone = form.cleaned_data['phone']
        address = form.cleaned_data['address']
        location = form.cleaned_data['location']
        club = form.cleaned_data['club']
        try:
            prefs = UserPreferences.objects.get(user=user)
        except:
            prefs = UserPreferences.objects.create(
                user=user
            )
        prefs.phone_number=phone
        prefs.club=form.cleaned_data['club']
        prefs.location=location
        prefs.address=address
        prefs.save()
        user.save()
        return super(UserUpdate, self).form_valid(form)
