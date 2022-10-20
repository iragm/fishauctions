import django_filters
from crispy_forms.helper import FormHelper
from django.contrib import messages
from .models import *
from django.db.models import Q, F, Count, Case, When, Value, IntegerField, OuterRef, Subquery, Exists, Sum, IntegerField
from django.forms.widgets import TextInput, Select, NumberInput
from django.forms import ModelChoiceField
import re

class AuctionTOSFilter(django_filters.FilterSet):
    """This filter is used on any admin views that allow adding users to an auction and on lot creation/winner screens"""
    query = django_filters.CharFilter(method='auctiontos_search',
                                        label="",
                                        widget=TextInput(attrs={
                                        "placeholder":"Filter by bidder number, name, phone, etc...",
                                        'hx-get':'',
                                        'hx-target':"div.table-container",
                                        'hx-trigger':"keyup changed delay:300ms",
                                        'hx-swap':"outerHTML",
                                        #'hx-indicator':".progress",
                                        }))
    class Meta:
        model = AuctionTOS
        fields = [] # nothing here so no buttons show up
    
    def __init__(self, *args, **kwargs):
        result = super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        return result

    def generic(self, qs, value):
        """Pass this a queryset and a value (string) to filter, and it'll return a suitable queryset
        This is getting reused in a couple places now, just import it with `from .filters import AuctionTOSFilter` and then use `AuctionTOSFilter.generic(qs, filter)`
        Some day I will add rhyming names in here (https://github.com/iragm/fishauctions/issues/121), so don't reinvent the wheel, recycle this!
        """
        value = value.strip()
        qs = qs.filter(
            Q(name__icontains=value) | 
            Q(email=value) | 
            Q(phone_number__icontains=value) | 
            Q(address__icontains=value) | 
            Q(bidder_number=value) |
            Q(user__username=value)
        )
        return qs

    def auctiontos_search(self, queryset, name, value):
        return self.generic(queryset, value)

class LotAdminFilter(django_filters.FilterSet):
    """This filter is used on any admin views that manage lots (really just the one...)"""
    query = django_filters.CharFilter(method='lot_search',
                                      label="",
                                      widget=TextInput(attrs={
                                        "placeholder":"Filter by lot number, name, or seller's contact info",
                                        'hx-get':'',
                                        'hx-target':"div.table-container",
                                        'hx-trigger':"keyup changed delay:300ms",
                                        'hx-swap':"outerHTML",
                                        #'hx-indicator':".progress",
                                        }))
    class Meta:
        model = Lot
        fields = [] # nothing here so no buttons show up

    def generic(self, queryset, value):
        if value.isnumeric():
            queryset = queryset.filter(Q(auctiontos_seller__phone_number__icontains=value) | 
            Q(auctiontos_seller__address__icontains=value) | 
            Q(auctiontos_seller__bidder_number=value) |
            Q(auctiontos_winner__bidder_number=value) |
            Q(lot_name__icontains=value) |
            Q(lot_number=value) |
            Q(custom_lot_number=value)
            )
        else:
            queryset = queryset.filter(
            Q(auctiontos_seller__name__icontains=value) | 
            Q(auctiontos_seller__email__icontains=value) | 
            Q(auctiontos_seller__address__icontains=value) | 
            Q(auctiontos_seller__bidder_number=value) |
            Q(auctiontos_winner__bidder_number=value) |
            Q(auctiontos_seller__user__username=value) |
            Q(lot_name__icontains=value) |
            Q(custom_lot_number=value)
        )
        return queryset
        
    def lot_search(self, queryset, name, value):
        return self.generic(queryset, value)


class LotFilter(django_filters.FilterSet):
    """This is the core of both the lot view and the recommendation engine
    A single queryset is annotated with information like distance, and sorted based on the user's (or page's) selection

    """
    def __init__(self, *args, **kwargs):
        self.canShowAuction = True
        self.listType = None # a special filter for recommended lot views
        self.latitude = None
        self.longitude = None
        try:
            self.latitude = kwargs.pop('latitude')
            self.longitude = kwargs.pop('longitude')
        except:
            pass
        try:
            self.listType = kwargs.pop('listType')
        except:
            pass
        try:
            self.keywords = kwargs.pop('keywords')
        except:
            self.keywords = []
        try:
            self.request = kwargs['request']
            self.user = self.request.user
            # get location from cookie
            try:
                self.latitude = self.request.COOKIES['latitude']
                self.longitude = self.request.COOKIES['longitude']
            except:
                pass
        except:
            self.user = kwargs.pop('user')
        try:
            if not self.latitude and not self.longitude:
                self.latitude = self.user.userdata.latitude
                self.longitude = self.user.userdata.longitude
        except:
            pass
        self.showLocal = True # annotate lots with the distance to the request user, requires self.latitude and self.longitude
        try:
            if not self.latitude and not self.longitude:
                self.showLocal = False
        except:
            self.showLocal = False
        self.showOwnLots = True # show lots from the request user. I don't think this is used anywhere
        self.maxRange = 70 # only applies if self.showLocal = True
        self.showDeactivated = False # show lots that users have deliberately deactivated
        if self.user.is_superuser:
            self.showBanned = True 
        else:
            self.showBanned = False # this is really only set to true if you are viewing your own lots
        self.showViewed = "all" # could be "yes" or "no", only matters for authenticated users, set to no to only see unseen things
        try:
            if kwargs.pop('onlyUnviewed'):
                self.showViewed = "no"
        except:
            pass            
        self.status = "open" # "all", "open", "unsold", or "ended".  If regarding an auction, should default to all
        self.showShipping = True
        self.shippingLocation = 52 # USA, later we might set this with a cookie like we do with lat and lng
        if self.user.is_authenticated:
            # lots for local pickup
            if self.user.userdata.local_distance:
                self.maxRange = self.user.userdata.local_distance
            # lots that can be shipped to the user's location
            if self.user.userdata.location:
                self.shippingLocation = self.user.userdata.location
        try:
            self.ignore = kwargs.pop('ignore')
        except:
            self.ignore = False
        try:
            self.regardingAuction = kwargs.pop('regardingAuction')
        except:
            self.regardingAuction = None # force only displaying lots from a particular auction
        try:
            self.regardingUser = kwargs.pop('regardingUser')
        except:
            self.regardingUser = None # force only displaying lots for a particular user (not necessarily the request user)
        try:
            self.order = kwargs.pop('order') # default ordering is just the most recent on top
        except:
            self.order = "-lot_number" # default ordering is just the most recent on top
        forceAuction = None
        try:
            forceAuction = kwargs.pop('auction')
            if self.listType == "auction":
                self.regardingAuction = Auction.objects.get(slug=forceAuction)
        except:
            pass
        try:
            forceAuction = self.request.GET['auction']
            if forceAuction != "no_auction":
                self.regardingAuction = Auction.objects.get(slug=forceAuction)
            else:
                self.regardingAuction = None
        except:
            pass
        if self.regardingAuction:
            regardingAuctionSlug = self.regardingAuction.slug
            self.showLocal = False
            self.showShipping = False
            #self.status = "all"
        else:
            regardingAuctionSlug = None
        if self.listType:
            if self.listType == "shipping":
                self.showLocal = False
                self.showShipping = True
                self.canShowAuction = False
            if self.listType == "local":
                self.showLocal = True
                self.showShipping = False
                self.canShowAuction = False
            if self.listType == "auction":
                self.showLocal = False
                self.showShipping = False
        # an extra filter
        specialAuctions = Q(slug=regardingAuctionSlug)
        self.possibleAuctions = Auction.objects.all().order_by('title')
        if self.user.is_authenticated:
            self.possibleAuctions = self.possibleAuctions.filter(Q(auctiontos__user=self.user)|specialAuctions).distinct()
        else:
            self.possibleAuctions = self.possibleAuctions.filter(specialAuctions)
        auction_choices = list((o.slug, o.title) for o in self.possibleAuctions)
        super().__init__(*args, **kwargs) # this must go above filters
        self.filters['auction'].extra['choices'] = [{"no_auction":"noAuction", "No auction":"title"}] + auction_choices
        self.filters['ships'].extra['choices'] = self.ships_choices()
        # if self.regardingAuction:
        #     self.filters['auction'].initial=self.regardingAuction.slug          

    STATUS = (
        ('all', 'Open/ended'),
        ('unsold', 'Open (hide bought now)'),
        ('closed', 'Ended'),
    )
    VIEWED = (
        ('yes', 'Only viewed'),
        ('no', 'Only unviewed'),
    )
    ORDER = (
        ('popularity', 'Most popular'),
        ('unloved', 'Least popular'),
        ('recommended', 'Recommended'),
        ('oldest', 'Oldest'),
    )

    def ships_choices(self):
        choices = list((o.pk, o.name) for o in Location.objects.all().order_by('name'))
        return [{"local_only":"local", "Local pickup":"title"}] + choices

    def generate_attrs(placeholder="", tooltip=""):
        return {'placeholder': placeholder, 'data-toggle':"tooltip", 'data-placement':"bottom", 'title':tooltip}

    q = django_filters.CharFilter(label='', method='text_filter', widget=TextInput(attrs=generate_attrs("Search","Search by title, description, username or lot number.   Use or to search for multiple terms at once")))
    auction = django_filters.ChoiceFilter(label='', empty_label='Any auction', method='filter_by_auction',
        widget=Select(attrs=generate_attrs(None,"Auctions you've confirmed your pickup location for appear here")))
    category = django_filters.ModelChoiceFilter(label='',\
        queryset=Category.objects.all().order_by('name'), method='filter_by_category', empty_label='Any category',\
        widget=Select(attrs=generate_attrs(None,"Picking something here overrides your ignored categories")))
    status = django_filters.ChoiceFilter(label='', choices=STATUS,\
        method='filter_by_status', empty_label='Open',\
        widget=Select(attrs=generate_attrs(None,"Has no effect if you're viewing a single user's lots or an auction that's ended")))
    distance = django_filters.NumberFilter(label='', method='filter_by_distance', widget=NumberInput(attrs=generate_attrs('Distance (miles)',"This only works on lots that aren't in an auction")))
    ships = django_filters.ChoiceFilter(label='',\
        method='filter_by_shipping_location',
        empty_label='Ships to',\
        widget=Select(attrs=generate_attrs(None,"This only works on lots that aren't in an auction")))
    user = django_filters.CharFilter(label='', method='filter_by_user', widget=NumberInput(attrs={'style': 'display:none'}))
    viewed = django_filters.ChoiceFilter(label='', choices=VIEWED, method='filter_by_viewed', empty_label='All',)
    order = django_filters.ChoiceFilter(label='', choices=ORDER, method='filter_by_order', empty_label='Newest',)
    
    class Meta:
        model = Lot
        fields = {} # nothing here so no buttons show up
    
    @property
    def qs(self):
        primary_queryset=super(LotFilter, self).qs
        
        # it's faster without this
        # with these, it's 320 queries in 5500 ms, without them it's 400 queries in 1500 ms
        # primary_queryset = primary_queryset.select_related('species_category', 'user', 'user__userdata', 'auction', 'winner')
        # primary_queryset = primary_queryset.prefetch_related('lot_number__pageview', 'lot_number__lothistory')
        
        if self.user.is_authenticated:
            if self.showViewed == "yes":
                primary_queryset = primary_queryset.filter(pageview__user=self.user)
            if self.showViewed == "no":
                primary_queryset = primary_queryset.exclude(pageview__user=self.user)
        if self.regardingUser:
            # show all lots if you are dealing with a single user
            self.status = "all"
        if self.status == "ended":
            primary_queryset = primary_queryset.filter(active=False)
        if self.status == "open":
            primary_queryset = primary_queryset.filter(active=True)
        if self.status == "unsold":
            primary_queryset = primary_queryset.filter(active=True, winner__isnull=True)

        # if not self.regardingAuction and not self.regardingUser:
        #     # no auction or user selected in the filter
        #     primary_queryset = primary_queryset.exclude(auction__promote_this_auction=False)
        if not self.showBanned:
            primary_queryset = primary_queryset.filter(banned=False)
        if not self.showDeactivated:
            primary_queryset = primary_queryset.filter(deactivated=False)
        if self.user.is_authenticated:
            # watched lots
            primary_queryset = primary_queryset.annotate(
                    is_watched_by_req_user = Exists(Watch.objects.filter(lot_number=OuterRef('lot_number'), user=self.user))
                )
        if self.user.is_authenticated:
            # messages for owner of lot
            primary_queryset = primary_queryset.annotate(
                owner_chats=Count('lothistory', filter=Q(lothistory__seen=False, lothistory__changed_price=False), distinct=True)
            )
        # messages for other user
        primary_queryset = primary_queryset.annotate(
            all_chats=Count('lothistory', filter=Q(lothistory__changed_price=False), distinct=True)
        )
        if self.order == 'popularity' or self.order == '-popularity':
            primary_queryset = primary_queryset.annotate(
                popularity= 2 * Count('pageview', distinct=True) + \
                Count('lothistory', filter=Q(lothistory__changed_price=False), distinct=True) +\
                # this is better than bids
                2.5*Count('lothistory', filter=Q(lothistory__changed_price=True), distinct=True)
            )
        if self.order == "-recommended":
            primary_queryset = primary_queryset.annotate(recommended=Sum(0, output_field=IntegerField()))
            if self.keywords:
                for word in self.keywords:
                    primary_queryset = primary_queryset.annotate(
                        recommended=Case(
                            When(lot_name__icontains=word, then=F('recommended') + 50), # this is how much to increase the weight when a word matches
                            default=F('recommended'),
                        ))
            if self.user.is_authenticated:
                interest = Subquery(UserInterestCategory.objects.filter(category=OuterRef('species_category'), user=self.user).values('interest'))
                primary_queryset = primary_queryset.annotate(recommended=(((F('promotion_weight') + 1) / 10 )* interest / 5 ) + F('recommended'))
            else:
                # if not signed in, recommended = most viewed
                # this sucks because you always see the same damned lots.
                # We could show most viewed here, but that just means that popularity breeds popularity
                #primary_queryset = primary_queryset.annotate(
                #    recommended = Count('pageview', distinct=True)
                #)
                # instead, let's just show newest (unless we have keywords...)
                if not self.keywords:
                    self.order = "-lot_number"
        # don't show very new lots unless they are from this user
        if self.user.is_authenticated:
            primary_queryset = primary_queryset.exclude(~Q(user=self.user), date_posted__gte=timezone.now() - datetime.timedelta(minutes=20))
        else:
            primary_queryset = primary_queryset.exclude(date_posted__gte=timezone.now() - datetime.timedelta(minutes=20))

        # filter by 3 things:
        # local_qs = local lots, within the max range
        # shipping_qs = lots that ship to your location
        # auction_qs = either a single auction or private auctions you've joined + promoted auctions
        
        auction_qs = Q(pk__isnull=True)
        local_qs = Q(pk__isnull=True)
        if self.showLocal:
            # then calculate the distance to each lot
            primary_queryset = primary_queryset.annotate(distance=distance_to(self.latitude, self.longitude, lat_field_name='`auctions_lot`.`latitude`', lng_field_name='`auctions_lot`.`longitude`'))
            # finally, filter by max range
            if self.maxRange:# and not self.regardingAuction:
                # if you specify both range and auction, range does nothing
                local_qs = Q(distance__lte=self.maxRange, auction__isnull=True, local_pickup=True)
        if self.ignore and self.user.is_authenticated:
            allowedCategories = Category.objects.exclude(userignorecategory__user=self.user)
            primary_queryset = primary_queryset.filter(species_category__in=allowedCategories)
        if not self.showOwnLots and self.user.is_authenticated:
            primary_queryset = primary_queryset.exclude(user=self.user.pk) # don't show your own lots
        if self.showShipping and self.shippingLocation:
            shipping_qs = Q(shipping_locations=self.shippingLocation, auction__isnull=True)
        else:
            shipping_qs = Q(pk__isnull=True)
        if self.canShowAuction:
            if self.regardingAuction:
                auction_qs = Q(auction=self.regardingAuction)
            else:
                #auction_qs = Q(pk__isnull=True)
                #auction_qs = Q(auction__promote_this_auction=True)
                if self.user.is_authenticated:
                    # this shows any auction you've joined + any public auction.  Perhaps this should be a preference?
                    # auction_qs = Q(auction__pk__in=self.possibleAuctions)|Q(auction__promote_this_auction=True)
                    # this shows any auction you've joined.  See https://github.com/iragm/fishauctions/issues/66
                    auction_qs = Q(auction__pk__in=self.possibleAuctions)
                else:
                    # anonymous users can see lots from all promoted auctions
                    auction_qs = Q(auction__promote_this_auction=True)
                #auction_qs = Q(auction__auctiontos__user=self.user, auction__promote_this_auction=False)|Q(auction__promote_this_auction=True)
        # putting them all together:
        primary_queryset = primary_queryset.filter(Q(local_qs)|Q(shipping_qs)|Q(auction_qs))
        return primary_queryset.order_by(self.order)

    def filter_by_order(self, queryset, name, value):
        if value == 'oldest':
            self.order = "lot_number"
        if value == 'newest':
            self.order = "-lot_number"
        if value == 'recommended':
            self.order = "-recommended"
        if value == 'popularity':
            self.order = "-popularity"
        if value == "unloved":
            self.order = "popularity"
        return queryset

    def filter_by_distance(self, queryset, name, value):
        self.maxRange = value
        return queryset

    def filter_by_user(self, queryset, name, value):
        # probably should only show banned if request user matches the user filter
        if self.user.username == value:
            self.showBanned = True
            self.showDeactivated = True
            self.regardingUser = True
            self.status = "all"
            self.form.initial['status'] = "all"
        return queryset.filter(user__username=value)

    def filter_by_category(self, queryset, name, value):
        self.ignore = False
        return queryset.filter(species_category=value)

    def filter_by_auction(self, queryset, name, value):
        if value == "no_auction":
            return queryset.filter(auction__isnull=True)
        try:
            self.regardingAuction = Auction.objects.get(slug=value)
            #self.form.initial['auction'] = self.regardingAuction.slug
            if self.regardingAuction.closed:
                self.status = "all"
                self.form.initial['status'] = "all"
            return queryset.filter(auction__slug=value)
        except Exception as e:
            return queryset

    def filter_by_status(self, queryset, name, value):
        self.status = value
        return queryset

    def filter_by_viewed(self, queryset, name, value):
        if not self.user.is_authenticated:
            messages.warning(self.request, "Sign in to use this filter")
        else:
            self.showViewed = value
        return queryset

    def text_filter(self, queryset, name, value):
        if value.isnumeric():
            return queryset.filter(Q(lot_number=int(value))|Q(lot_name__icontains=value)|Q(custom_lot_number=value))
        else:
            split = re.split(r'\bor\b', value)
            qList = Q() # empty
            for fragment in split:
                fragment = fragment.strip()
                qList |= Q(description__icontains=fragment)|Q(lot_name__icontains=fragment)|Q(user__username=fragment)|Q(custom_lot_number=fragment)
            return queryset.filter(qList)
    
    def filter_by_shipping_location(self, queryset, name, value):
        if value:
            self.shippingLocation = value
            self.showShipping = True
        try:
            if self.request.GET['auction']:
                if self.request.GET['auction'] != 'no_auction':
                    # if both this and auction are specified, auction wins and this does nothing
                    self.showShipping = False
                    return queryset
        except:
            pass
        if value == "local_only":
            self.showShipping = False
            return queryset.filter(Q(local_pickup=True)|Q(auction__isnull=False))
        return queryset

class UserWatchLotFilter(LotFilter):
    """A version of the lot filter that only shows lots watched by the current user"""
    @property
    def qs(self):
        primary_queryset=super().qs
        return primary_queryset.filter(watch__user=self.user)

    def __init__(self, *args, **kwargs):
        super().__init__(*args,**kwargs)
        #self.status = "all"
        self.form.initial['status'] = "ended"

class UserBidLotFilter(LotFilter):
    """A version of the lot filter that only shows lots bid on by the current user"""
    @property
    def qs(self):
        primary_queryset=super().qs
        return primary_queryset.filter(bid__user=self.user).distinct()
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args,**kwargs)
        #self.status = ""
        self.form.initial['status'] = ""

class UserWonLotFilter(LotFilter):
    """A version of the lot filter that only shows lots won by the current user"""
    @property
    def qs(self):
        primary_queryset=super().qs
        return primary_queryset.filter(winner=self.user)

    def __init__(self, *args, **kwargs):
        super().__init__(*args,**kwargs)
        #self.status = "ended"
        self.form.initial['status'] = "closed"

def get_recommended_lots(
		user=None,
		listType=None, # "auction", "local", or "shipping"
        auction=None, # slug for auction, overrides listType to "auction"
        latitude=0,
        longitude=0,
		qty=10,
        keywords=[]):
        """
        This is the core of the recommendation system
        Returns a queryset of lot objects ready for use in a template
        """
        if auction:
            listType = "auction"
        qs = LotFilter(user=user, ignore=True, onlyUnviewed=True, listType=listType, auction=auction, latitude=latitude, longitude=longitude, order='-recommended', keywords=keywords).qs
        return qs[:qty]