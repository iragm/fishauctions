import django_filters
from django.contrib import messages
from .models import *
from django.db.models import Q, Count
from django.forms.widgets import TextInput, Select, NumberInput
from django.forms import ModelChoiceField

class LotFilter(django_filters.FilterSet):
    def __init__(self, *args, **kwargs):
        try:
            self.ignore = kwargs.pop('ignore')
        except:
            self.ignore = False
        super(LotFilter, self).__init__(*args, **kwargs)
        # this would require the keyword recommended in the url to show recommended lots. e.g.
        # /lots/?recommended
        # it's turned off so that not specifying any filter will show you just the lots that are recommended for you
        # try:
        #     self.request.GET['recommended']
        #     self.allRecommended = True
        # except:
        #     self.allRecommended = False
        self.allRecommended = True
        try:
            self.filters['auction'].queryset = Auction.objects.filter(auctiontos__user=self.request.user).order_by('title')
        except:
            self.filters['auction'].queryset = Auction.objects.none()
    STATUS = (
        ('open', 'Open'),
        ('closed', 'Ended'),
    )
    VIEWED = (
        ('yes', 'Only viewed'),
        ('no', 'Only unviewed'),
    )
    q = django_filters.CharFilter(label='', method='text_filter', widget=TextInput(attrs={'placeholder': 'Search'}))
    auction = django_filters.ModelChoiceFilter(label='',queryset=None, empty_label='Any auction', to_field_name='slug', method='filter_by_auction_model',)
    category = django_filters.ModelChoiceFilter(label='', queryset=Category.objects.all().order_by('name'), method='filter_by_category', empty_label='Any category', )
    status = django_filters.ChoiceFilter(label='', choices=STATUS, method='filter_by_status', empty_label='Open and ended',)
    distance = django_filters.NumberFilter(label='', method='filter_by_distance', widget=NumberInput(attrs={'placeholder': 'Distance (miles)'}))
    ships = django_filters.ModelChoiceFilter(label='', queryset=Location.objects.all().order_by('name'), method='filter_by_shipping_location', empty_label='Ships to', )
    # this creates a list of every single user and it may get out of hand once we get into the thousands of users
    #user = django_filters.ModelChoiceFilter(label='', queryset=User.objects.all(), method='filter_by_user', widget=Select(attrs={'style': 'display:none'}))
    # better: just use pk of user
    user = django_filters.NumberFilter(label='', method='filter_by_user', widget=NumberInput(attrs={'style': 'display:none'}))
    # this is a bit of a hack to allow filtering by auctions show up even if you haven't selected a pickup location
    a = django_filters.CharFilter(label='', method='filter_by_auction', widget=TextInput(attrs={'style': 'display:none'}))
    viewed = django_filters.ChoiceFilter(label='', choices=VIEWED, method='filter_by_viewed', empty_label='All',)
    
    class Meta:
        model = Lot
        fields = {} # nothing here so no buttons show up
    
    @property
    def qs(self):
        primary_queryset=super(LotFilter, self).qs
        primary_queryset = primary_queryset.filter(banned=False).order_by("-lot_number").select_related('species_category', 'user')
        try:
            # watched lots
            primary_queryset = primary_queryset.annotate(
                    is_watched_by_req_user=Count('watch', filter=Q(watch__user=self.request.user.pk))
                )
        except:
            pass
        # messages for owner of lot
        primary_queryset = primary_queryset.annotate(
            owner_chats=Count('lothistory', filter=Q(lothistory__seen=False, lothistory__changed_price=False), distinct=True)
        )
        # messages for other user
        primary_queryset = primary_queryset.annotate(
            all_chats=Count('lothistory', filter=Q(lothistory__changed_price=False), distinct=True)
        )
        # distance away
        try:
            latitude = self.request.COOKIES['latitude']
            longitude = self.request.COOKIES['longitude']
        except:
            latitude = 0
            longitude = 0
        primary_queryset = primary_queryset.annotate(distance=distance_to(latitude, longitude))
        #applyIgnoreFilter = True
        try:
            if self.ignore and self.request.user.is_authenticated:
                allowedCategories = Category.objects.exclude(userignorecategory__user=self.request.user)
                primary_queryset = primary_queryset.filter(species_category__in=allowedCategories)
            #else:
            #    applyIgnoreFilter = False
        except:
            pass
            #applyIgnoreFilter = False
        if (self.allRecommended):
            # no filters being used, show recommended lots
            #primary_queryset = primary_queryset.exclude(user=self.request.user.pk) # don't show your own lots
            # don't show viewed lots for signed in users
            # try:
            #     primary_queryset = primary_queryset.exclude(pageview__user=self.request.user)
            # except:
            #     pass
            # local lots
            try:
                if self.request.user.userdata.latitude and self.request.user.userdata.local_distance:
                    local = True
                else:
                    local = None
            except:
                local = None
            # lots that can be shipped to the user's location
            try:
                if self.request.user.userdata.location:
                    shipping = True
                else:
                    shipping = None
            except:
                shipping = None
            # put it together and add lots that are part of an auction
            # fixme - this is in desperate need of refactoring 
            if local and shipping:
                primary_queryset = primary_queryset.filter(\
                    Q(shipping_locations=self.request.user.userdata.location, auction__isnull=True)|\
                    Q(distance__lte=self.request.user.userdata.local_distance, local_pickup=True)|\
                    Q(auction__auctiontos__user=self.request.user))
            if local and not shipping:
                primary_queryset = primary_queryset.filter(\
                    Q(distance__lte=self.request.user.userdata.local_distance, local_pickup=True)|\
                    Q(auction__auctiontos__user=self.request.user))
            if not local and shipping:
                primary_queryset = primary_queryset.filter(\
                    Q(shipping_locations=self.request.user.userdata.location, auction__isnull=True)|\
                    Q(auction__auctiontos__user=self.request.user))
            if not local and not shipping:
                try:
                    primary_queryset = primary_queryset.filter(auction__auctiontos__user=self.request.user)
                except:
                    pass
        return primary_queryset

    def filter_by_distance(self, queryset, name, value):
        self.allRecommended = False
        try:
            if self.request.GET['auction']:
                # if both this and auction are specified, auction wins and this does nothing
                return queryset
        except:
            pass
        try:
            if self.request.user.userdata.latitude:
                # seems like we need to annotate twice here
                return queryset.annotate(distance=distance_to(self.request.user.userdata.latitude, self.request.user.userdata.longitude))\
                    .filter(distance__lte=value, auction__isnull=True, local_pickup=True)
        except:
            pass
        return queryset

    def filter_by_user(self, queryset, name, value):
        self.allRecommended = False
        return queryset.filter(user__pk=value)

    def filter_by_category(self, queryset, name, value):
        self.ignore = False
        self.allRecommended = False
        return queryset.filter(species_category=value)

    def filter_by_auction(self, queryset, name, value):
        self.allRecommended = False
        try:
            if self.request.GET['auction']:
                # if both this and auction are specified, auction wins and this does nothing
                return queryset
        except:
            pass
        return queryset.filter(auction=Auction.objects.get(slug=value))

    def filter_by_auction_model(self, queryset, name, value):
        self.allRecommended = False
        return queryset.filter(auction=value)

    def filter_by_status(self, queryset, name, value):
        self.allRecommended = False
        if value == "ended":
            return queryset.filter(active=False)
        if value == "open":
            return queryset.filter(active=True)
        return queryset.filter()

    def filter_by_viewed(self, queryset, name, value):
        self.allRecommended = False
        if not self.request.user.is_authenticated:
            messages.warning(self.request, "Sign in to use this filter")
            return queryset
        if value == "yes":
            return queryset.filter(pageview__user=self.request.user)
        if value == "no":
            return queryset.exclude(pageview__user=self.request.user)
        return queryset.filter()

    def text_filter(self, queryset, name, value):
        self.allRecommended = False
        if value.isnumeric():
            return queryset.filter(Q(lot_number=int(value))|Q(lot_name__icontains=value))
        else:
            return queryset.filter(Q(description__icontains=value)|Q(lot_name__icontains=value))
    
    def filter_by_shipping_location(self, queryset, name, value):
        self.allRecommended = False
        try:
            if self.request.GET['auction']:
                # if both this and auction are specified, auction wins and this does nothing
                return queryset
        except:
            pass
        return queryset.filter(shipping_locations=value, auction__isnull=True)

class UserWatchLotFilter(LotFilter):
    """A version of the lot filter that only shows lots watched by the current user"""
    @property
    def qs(self):
        primary_queryset=super(UserWatchLotFilter, self).qs
        return primary_queryset.filter(watch__user=self.request.user)

    def __init__(self, *args, **kwargs):
        self.request = kwargs['request']
        super().__init__(*args,**kwargs)

class UserBidLotFilter(LotFilter):
    """A version of the lot filter that only shows lots bid on by the current user"""
    @property
    def qs(self):
        primary_queryset=super(UserBidLotFilter, self).qs
        return primary_queryset.filter(bid__user=self.request.user).distinct()

    def __init__(self, *args, **kwargs):
        self.request = kwargs['request']
        super().__init__(*args,**kwargs)
    
# class UserOwnedLotFilter(LotFilter):
#     """A version of the lot filter that only shows lots submitted by the current user  I have removed this in favor of just using the lots/user/?user=pk view"""
#     @property
#     def qs(self):
#         primary_queryset=super(UserOwnedLotFilter, self).qs
#         return primary_queryset.filter(user=self.request.user)

#     def __init__(self, *args, **kwargs):
#         self.allRecommended = False
#         self.request = kwargs['request']
#         super().__init__(*args,**kwargs)

class UserWonLotFilter(LotFilter):
    """A version of the lot filter that only shows lots won by the current user"""
    @property
    def qs(self):
        primary_queryset=super(UserWonLotFilter, self).qs
        return primary_queryset.filter(winner=self.request.user)

    def __init__(self, *args, **kwargs):
        self.request = kwargs['request']
        super().__init__(*args,**kwargs)
        self.form.initial['status'] = "closed"