import django_filters
from .models import Lot, Category, Auction
from django.db.models import Q
from django.forms.widgets import TextInput, Select
from django.forms import ModelChoiceField

class LotFilter(django_filters.FilterSet):
    STATUS = (
        ('open', 'Open'),
        ('closed', 'Ended'),
    )
    q = django_filters.CharFilter(label='', method='textFilter', widget=TextInput(attrs={'placeholder': 'Search', 'class': 'full-width'}))
    auction = django_filters.ModelChoiceFilter(label='',queryset=Auction.objects.all().order_by('title'), empty_label='Any', to_field_name='slug', widget=Select(attrs={'style': 'width:10vw'}))
    category = django_filters.ModelChoiceFilter(label='', queryset=Category.objects.all().order_by('name'), method='filter_by_category', empty_label='Any category', widget=Select(attrs={'style': 'width:10vw'}))
    status = django_filters.ChoiceFilter(label='', choices=STATUS, method='filter_by_status', empty_label='Open and ended')

    class Meta:
        model = Lot
        fields = {} # nothing here so no buttons show up
    
    def filter_by_category(self, queryset, name, value):
        return queryset.filter(species_category=value)

    def filter_by_auction(self, queryset, name, value):
        return queryset.filter(auction=Auction.objects.get(slug=value))

    def filter_by_status(self, queryset, name, value):
        if value == "ended":
            return queryset.filter(active=False)
        if value == "open":
            return queryset.filter(active=True)
        return queryset.filter()

    def textFilter(self, queryset, name, value):
        if value.isnumeric():
            return queryset.filter(Q(lot_number=int(value))|Q(lot_name__icontains=value))
        else:
            return queryset.filter(Q(description__icontains=value)|Q(lot_name__icontains=value))
    @property
    def qs(self):
        primary_queryset=super(LotFilter, self).qs
        return primary_queryset.filter(banned=False)


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