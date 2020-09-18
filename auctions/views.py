from datetime import datetime
from itertools import chain
from django.contrib.auth.models import User
from django.shortcuts import render,redirect
from django.http import HttpResponse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, DetailView
from django.views.generic.edit import FormMixin
from django.urls import reverse
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

# @login_required
# def bidView(request):
#     if request.method == 'POST':
#         form = CreateBid(request.POST, request=request, lot=1)
#         if form.is_valid():
#             form.save()            
#             lot = form.save(commit=False)
#             lot.user = User.objects.get(id=request.user.id)
#             lot.save()            
#             print(str(lot.user) + " has added a new lot " + lot.lot_name)
#             messages.info(request, "Bid submitted")
#             form = CreateBid() # no post data here to reset the form
#     else:
#         form = CreateBid(request=request)
#     context = {
#         'form':form,
#     }
#     return render(request,'submit_bid.html', context)
    # class Meta:
    #     model = Bid
    #     fields = 'user', lot_number
    # form = BidForm()
    # # return render(request,'lot.html', {'form':form})

    # if request.method == 'POST':
    #      form = BidForm(request.POST)
    #      if form.is_valid():
    #          name = form.cleaned_data['name']
    #          name = form.cleaned_data['text']
    #          name = form.cleaned_data['body']


# password protected in urls.py
class viewAndBidOnLot(FormMixin, DetailView):
    """Show the picture and detailed information about a lot, and allow users to place bids"""
    template_name = 'view_lot.html'
    model = Bid
    form_class = CreateBid
    queryset = Lot.objects.all()
    
    def get_context_data(self, **kwargs):
        context = super(viewAndBidOnLot, self).get_context_data(**kwargs)
        #highBid = getHighBid(self.kwargs['pk'])
        context['watched'] = Watch.objects.filter(lot_number=self.kwargs['pk'], user=self.request.user.id)
        #context['currentPrice'] = highBid['amount']
        #context['highBidder'] = highBid['name']
        context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':self.kwargs['pk'], "amount":Lot.objects.get(pk=self.kwargs['pk']).high_bid + 1}, request=self.request)
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
        lot = Lot.objects.get(pk=self.kwargs['pk'])
        highBidder = lot.high_bidder
        
        if lot.ended:
            messages.info(request, "LotEnded")
        else:
            if (thisBid > lot.max_bid):
                messages.info(request, "BidSuccess")
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
                    messages.info(request, "UnderbidYourself")
            except:
                # create a new bid model
                print(f"{request.user} has bid on {lot}")
                form.save() # record the bid regardless of whether or not it's the current high
        return super(viewAndBidOnLot, self).form_valid(form)

# def getHighBid(lot_number):
#     # wrong lot getting passed?
#     """returns the high bidder name and amount"""
#     # fixme - add an additional filter  for bid_time lt lot end time
#     allBids = Bid.objects.filter(lot_number=lot_number).order_by('-amount')[:2]
#     # highest bid is the winner, but the second highest determines the price
#     try:
#         bidPrice = allBids[1].amount + 1 # $1 more than the second highest bid
#     except:
#         #print("no bids for this item")
#         bidPrice = Lot.objects.get(pk=lot_number).reserve_price
#     try:
#         highBidder = allBids[0].user
#     except:
#         highBidder = "No bids yet"
#     #print(f"hightest bid is {highBidder}, amount is {bidPrice}")
    #return {"name": highBidder, "amount": bidPrice}

@login_required
def createLot(request):
    #return HttpResponse("Page for new lot")
    if request.method == 'POST':
        # without this check, the crispy form is rendering wrong
        if request.FILES:
            form = CreateLotForm(request.POST, request.FILES)
        else:
            form = CreateLotForm(request.POST)
        if form.is_valid():
            lot = form.save(commit=False)
            lot.user = User.objects.get(id=request.user.id)
            lot.auction = Auction.objects.get(pk=1) # this is hard coded to only allow adding things to one auction
            lot.save()            
            print(str(lot.user) + " has added a new lot " + lot.lot_name)
            messages.info(request, "Lot added")
            form = CreateLotForm() # no post data here to reset the form
    else:
        form = CreateLotForm()
    return render(request,'new_lot.html', {'form':form})

def aboutAuction(request):
    return render(request,'current_auction.html')

def toAllLots(request):
    response = redirect('/all_lots/')
    return response

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

# password protected in views.py
class invoice(DetailView): #FormMixin
    """Get you invoice by auction"""
    template_name = 'invoice.html'
    model = Invoice
    #form_class = ChooseAuction
    #queryset = Auction.objects.all()
    
    def get_context_data(self, **kwargs):
        context = super(invoice, self).get_context_data(**kwargs)
        #context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':self.kwargs['pk'], "amount":Lot.objects.get(pk=self.kwargs['pk']).high_bid + 1}, request=self.request)
        return context
    
    def get_success_url(self):
        return ""

    def form_valid(self, form, request):
        lotNumber = form.cleaned_data['lot_number'].pk
        thisBid = form.cleaned_data['amount']
        form.user = User.objects.get(id=request.user.id)
        lot = Lot.objects.get(pk=self.kwargs['pk'])
        highBidder = lot.high_bidder
        if lot.ended:
            messages.info(request, "LotEnded")
        else:
            if (thisBid > lot.high_bid) and (request.user.id != highBidder.pk):
                messages.info(request, "BidSuccess")
                # Send an email to the old high bidder
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
        form.save() # record the bid regardless of whether or not it's the current high
        return super(viewAndBidOnLot, self).form_valid(form)