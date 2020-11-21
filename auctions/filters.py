import django_filters
from .models import Lot, Category, Auction, User, PageView
from django.db.models import Q, Prefetch
from django.forms.widgets import TextInput, Select
from django.forms import ModelChoiceField

class LotFilter(django_filters.FilterSet):
    def __init__(self, *args, **kwargs):
        try:
            self.ignore = kwargs.pop('ignore')
        except:
            self.ignore = False
        super(LotFilter, self).__init__(*args, **kwargs)

    STATUS = (
        ('open', 'Open'),
        ('closed', 'Ended'),
    )
    VIEWED = (
        ('yes', 'Only viewed'),
        ('no', 'Only unviewed'),
    )
    q = django_filters.CharFilter(label='', method='textFilter', widget=TextInput(attrs={'placeholder': 'Search', 'class': 'full-width form-control'}))
    auction = django_filters.ModelChoiceFilter(label='',queryset=Auction.objects.all().order_by('title'), empty_label='Any auction', to_field_name='slug', widget=Select(attrs={'style': 'width:10vw', 'class': 'form-control custom-select'}))
    category = django_filters.ModelChoiceFilter(label='', queryset=Category.objects.all().order_by('name'), method='filter_by_category', empty_label='Any category', widget=Select(attrs={'style': 'width:10vw', 'class': 'form-control custom-select'}))
    status = django_filters.ChoiceFilter(label='', choices=STATUS, method='filter_by_status', empty_label='Open and ended', widget=Select(attrs={'style': 'width:10vw', 'class': 'form-control custom-select'}))
    user = django_filters.ModelChoiceFilter(label='', queryset=User.objects.all(), method='filter_by_user', widget=Select(attrs={'style': 'display:none'}))
    viewed = django_filters.ChoiceFilter(label='', choices=VIEWED, method='filter_by_viewed', empty_label='All', widget=Select(attrs={'style': 'width:10vw', 'class': 'form-control custom-select'}))
    
    class Meta:
        model = Lot
        fields = {} # nothing here so no buttons show up
    
    def filter_by_user(self, queryset, name, value):
        return queryset.filter(user=value)

    def filter_by_category(self, queryset, name, value):
        self.ignore = False
        return queryset.filter(species_category=value)

    def filter_by_auction(self, queryset, name, value):
        return queryset.filter(auction=Auction.objects.get(slug=value))

    def filter_by_status(self, queryset, name, value):
        if value == "ended":
            return queryset.filter(active=False)
        if value == "open":
            return queryset.filter(active=True)
        return queryset.filter()

    def filter_by_viewed(self, queryset, name, value):
        views = Lot.objects.filter(pageview__user=self.request.user)
        if value == "yes":
            return queryset.filter(lot_number__in=views)
        if value == "no":
            return queryset.exclude(lot_number__in=views)
        return queryset.filter()

    def textFilter(self, queryset, name, value):
        if value.isnumeric():
            return queryset.filter(Q(lot_number=int(value))|Q(lot_name__icontains=value))
        else:
            return queryset.filter(Q(description__icontains=value)|Q(lot_name__icontains=value))
    
    @property
    def qs(self):
        primary_queryset=super(LotFilter, self).qs
        #result = primary_queryset.filter(banned=False).order_by("-lot_number")
        #result.filter=Q(species_category=userignorecategory__user=self.request.user)
        applyIgnoreFilter = True
        try:
            if self.ignore:
                allowedCategories = Category.objects.exclude(userignorecategory__user=self.request.user)
                result = primary_queryset.filter(banned=False).filter(species_category__in=allowedCategories).order_by("-lot_number").select_related('species_category')
            else:
                applyIgnoreFilter = False
        except:
            applyIgnoreFilter = False
        if not applyIgnoreFilter:
            result = primary_queryset.filter(banned=False).order_by("-lot_number").select_related('species_category')
        # SELECT * FROM auctions_lot
        # WHERE
        # NOT EXISTS (SELECT category_id FROM auctions_userignorecategory where 
        # auctions_lot.species_category_id = auctions_userignorecategory.category_id and auctions_userignorecategory.user_id = 2)
        return result

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
    
class UserOwnedLotFilter(LotFilter):
    """A version of the lot filter that only shows lots submitted by the current user"""
    @property
    def qs(self):
        primary_queryset=super(UserOwnedLotFilter, self).qs
        return primary_queryset.filter(user=self.request.user)

    def __init__(self, *args, **kwargs):
        self.request = kwargs['request']
        super().__init__(*args,**kwargs)

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