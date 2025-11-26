import datetime
import re

import django_filters
from crispy_forms.helper import FormHelper
from django.contrib import messages
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    When,
)
from django.forms.widgets import NumberInput, Select, TextInput
from django.utils import timezone

from .models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Category,
    Location,
    Lot,
    UserInterestCategory,
    Watch,
    add_tos_info,
    distance_to,
)


class AuctionFilter(django_filters.FilterSet):
    """Filter for the main auctions list"""

    query = django_filters.CharFilter(
        method="auction_search",
        label="",
        widget=TextInput(
            attrs={
                "placeholder": "Filter by auction name, or type a number to see nearby auctions",
                "hx-get": "",
                "hx-target": "div.table-container",
                "hx-trigger": "keyup changed delay:300ms",
                "hx-swap": "outerHTML",
                "hx-indicator": ".progress",
            }
        ),
    )

    class Meta:
        model = Auction
        fields = []  # nothing here so no buttons show up

    def auction_search(self, queryset, name, value):
        if value == "joined":
            return queryset.exclude(joined=False).exclude(joined=0)
        if value.isnumeric():
            return queryset.filter(distance__lte=int(value))
        else:
            return queryset.filter(
                Q(title__icontains=value)
                | Q(created_by__userdata__club__name__icontains=value)
                | Q(created_by__userdata__club__abbreviation=value)
            )


class AuctionTOSFilter(django_filters.FilterSet):
    """This filter is used on any admin views that allow adding users to an auction and on lot creation/winner screens"""

    query = django_filters.CharFilter(
        method="auctiontos_search",
        label="",
        widget=TextInput(
            attrs={
                "placeholder": "Filter by bidder number, name, email...",
                "hx-get": "",
                "hx-target": "div.table-container",
                "hx-trigger": "keyup changed delay:300ms",
                "hx-swap": "outerHTML",
                # 'hx-indicator':".progress",
            }
        ),
    )

    class Meta:
        model = AuctionTOS
        fields = []  # nothing here so no buttons show up

    def __init__(self, *args, **kwargs):
        result = super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        return result

    def generic(self, qs, value, match_names_only=False):
        """Pass this a queryset and a value (string) to filter, and it'll return a suitable queryset
        pass match_names_only=True to filter only name information.  This will give an exact name match (Bob Smith) OR a rhyming name match (Robert Smith)
        This is getting reused in a couple places now, just import it with `from .filters import AuctionTOSFilter` and then use `AuctionTOSFilter.generic(qs, filter)`
        """

        # sketchy users
        pattern = re.compile(r"^sus|\ssus\s|\ssus$")
        if pattern.search(value):
            value = pattern.sub("", value)
            qs = add_tos_info(qs)
            qs = qs.order_by("trust")

        RHYMING_NAMES = [
            [
                "andy",
                "andrew",
                "drew",
            ],
            ["alex", "alexander", "lex", "lexi"],
            ["al", "albert", "bert"],
            ["bart", "bartholomew", "bartie"],
            ["ben", "benjamin", "benny"],
            ["bill", "william", "billy", "will"],
            ["bob", "robert", "bobby", "rob"],
            ["brad", "bradley"],
            ["brandon", "bran"],
            ["brent", "brenton"],
            ["brian", "bryan"],
            ["cal", "calvin"],
            ["carl", "carlton"],
            ["chad", "chadwick"],
            ["charles", "charlie", "chuck"],
            ["chris", "christopher", "chrissy", "christy"],
            ["dick", "richard", "ricky"],
            ["dan", "daniel", "danny"],
            ["dave", "david", "davy"],
            ["dean", "deano"],
            ["don", "donald", "donnie"],
            ["doug", "douglas", "dougie"],
            ["ed", "eddie", "edward", "edwin"],
            ["eli", "elijah"],
            ["eric", "erick"],
            ["frank", "franklin", "frankie"],
            ["fred", "frederick", "freddy"],
            ["gary", "gareth"],
            ["george", "georgie", "geo"],
            ["greg", "gregory", "gregg"],
            ["hank", "henry"],
            ["jack", "jackson", "jackie", "john"],
            ["james", "jamie", "jim", "jimmy"],
            ["jason", "jay", "jase"],
            ["jeff", "jeffrey", "jeffry"],
            ["jerry", "gerald"],
            ["jesse", "jess", "jessie"],
            ["jim", "james", "jimmy"],
            ["joe", "joseph", "joey"],
            ["john", "jonathan", "jon", "johnny", "jack"],
            ["josh", "joshua", "joshie"],
            ["justin", "justine", "jussi"],
            ["keith", "keithan"],
            ["ken", "kenneth", "kenny"],
            ["kevin", "kev"],
            ["larry", "lawrence", "lars"],
            ["lee", "leeland"],
            ["len", "leonard", "lenny"],
            ["leo", "leonard", "leon"],
            ["logan", "logie"],
            ["lou", "louis", "louie"],
            ["mark", "marcus", "markie"],
            ["matt", "matthew", "matty"],
            ["max", "maximilian"],
            ["mike", "michael", "mikey"],
            ["nate", "nathan", "nathaniel", "natey"],
            ["nick", "nicholas", "nicky"],
            ["pat", "patrick", "paddy"],
            ["paul", "paulie"],
            ["pete", "peter", "petey"],
            ["phil", "philip", "phillip", "philly"],
            ["ray", "raymond", "raymie"],
            ["rich", "richard", "richie", "rick"],
            ["rob", "robert", "robbie"],
            ["ron", "ronald", "ronnie"],
            ["russ", "russell"],
            ["ryan", "ry"],
            ["sam", "samuel", "sammie"],
            ["scott", "scottie"],
            ["sean", "shawn", "shaun", "shawny"],
            ["steve", "steven", "stevie"],
            ["ted", "theodore", "teddy"],
            ["tim", "timothy", "timmy"],
            ["tom", "thomas", "tommy"],
            ["tony", "anthony", "tonya", "toni"],
            ["travis", "trav"],
            ["trey", "treyton"],
            ["tyler", "ty tye"],
            ["vern", "vernon"],
            ["vic", "victor"],
            ["vince", "vincent", "vinny"],
            ["walt", "walter", "wally"],
            ["warren", "warrick"],
            ["wayne", "waine"],
            ["wes", "wesley"],
            ["will", "william", "willy"],
            ["zach", "zachary", "zachie"],
            ["abe", "abraham"],
            ["ace", "acer"],
            ["adam", "ad"],
            ["art", "arthur", "artie"],
            ["ash", "ashley", "asher"],
        ]
        # search by invoice status
        invoice_patterns = {
            "open": {"auctiontos__status": "DRAFT"},
            "ready": {"auctiontos__status": "UNPAID"},
            "paid": {"auctiontos__status": "PAID"},
            "exists": {"auctiontos__isnull": False},
            "owes club": {"auctiontos__calculated_total__lt": 0},
            "club owes": {"auctiontos__calculated_total__gt": 0},
            "seen": {"auctiontos__opened": True},
            "unseen": {"auctiontos__opened": False},
            "no bid": {"bidding_allowed": False},
            "no sell": {"selling_allowed": False},
            "email bad": {"email_address_status": "BAD"},
            "email good": {"email_address_status": "VALID"},
            "no email": {"email__isnull": True},
            "duplicate": {"possible_duplicate__isnull": False},
        }

        if not match_names_only:
            # Apply filters based on patterns
            for keyword, filter_data in invoice_patterns.items():
                pattern = re.compile(rf"^{keyword}|\s{keyword}\s|\s{keyword}$")
                if pattern.search(value):
                    value = pattern.sub("", value)
                    qs = qs.filter(**filter_data)

        # search by rhyming names
        qList = Q()
        parts = value.lower().split()
        if len(parts) >= 1:
            first_name = parts[0]
        else:
            first_name = ""
        if len(parts) >= 2:
            last_name = " " + parts[1]
        else:
            last_name = ""
        for name_set in RHYMING_NAMES:
            if first_name in name_set:
                # got a match?  Add all possible matches as OR filters
                for possible_matching_name in name_set:
                    qList |= Q(name__istartswith=possible_matching_name + last_name)

        value = value.strip()

        if match_names_only:
            qs = qs.filter(Q(name=value) | Q(qList))
            return qs

        normal_filter = Q(
            Q(name__icontains=value)
            | Q(email=value)
            |
            # Q(phone_number__icontains=value) |
            #   Q(address__icontains=value) |
            Q(bidder_number=value)
            | Q(user__username=value)
            | Q(auctiontos__invoice__payments__receipt_number__iexact=value)
        )
        qs = qs.filter(Q(normal_filter | Q(qList)))
        return qs

    def auctiontos_search(self, queryset, name, value):
        return self.generic(queryset, value)


class LotAdminFilter(django_filters.FilterSet):
    """This filter is used on any admin views that manage lots (really just the one...)"""

    query = django_filters.CharFilter(
        method="lot_search",
        label="",
        widget=TextInput(
            attrs={
                "placeholder": "Type to filter...",
                "hx-get": "",
                "hx-target": "div.table-container",
                "hx-trigger": "keyup changed delay:300ms",
                "hx-swap": "outerHTML",
                # 'hx-indicator':".progress",
            }
        ),
    )

    class Meta:
        model = Lot
        fields = []  # nothing here so no buttons show up

    def generic(self, queryset, value):
        if value.isnumeric():
            queryset = queryset.filter(
                Q(auctiontos_seller__bidder_number=value)
                | Q(auctiontos_winner__bidder_number=value)
                | Q(lot_name__icontains=value)
                | Q(lot_number=value)
                | Q(custom_lot_number=value)
                | Q(lot_number_int=value)
                | Q(custom_field_1=value)
            )
        else:
            try:
                auction = self.queryset.first().auction
            except (ValueError, AttributeError):
                auction = None
            if (
                auction
                and auction.use_custom_checkbox_field
                and auction.custom_checkbox_name
                and value.lower() == auction.custom_checkbox_name.lower()
            ):
                return queryset.filter(custom_checkbox=True)

            def get_colon_filter(key, val):
                if key == "lot":
                    if val.isnumeric():
                        return Q(lot_number_int=val) | Q(custom_lot_number=val)
                    return Q(custom_lot_number=val)
                elif key == "seller":
                    return Q(auctiontos_seller__bidder_number=val)
                elif key == "winner":
                    return Q(auctiontos_winner__bidder_number=val)
                return Q()

            # this was a one-off for a bug found during an ACM auction.  It's probably safe to remove.
            if value == "broken":
                return queryset.filter(auctiontos_winner__isnull=True, winner__isnull=False)

            if value == "unsold":
                return queryset.filter(auctiontos_winner__isnull=True)
            if value == "removed":
                return queryset.filter(banned=True)
            if ":" in value:
                key, val = (s.strip() for s in value.split(":", 1))
                q_obj = get_colon_filter(key, val)
                if q_obj:
                    return queryset.filter(q_obj)

            queryset = queryset.filter(
                Q(auctiontos_seller__name__icontains=value)
                | Q(auctiontos_seller__bidder_number=value)
                | Q(auctiontos_winner__bidder_number=value)
                | Q(auctiontos_seller__user__username=value)
                | Q(lot_name__icontains=value)
                | Q(custom_lot_number=value)
                | Q(custom_field_1__icontains=value)
                | Q(auction__title__icontains=value)
            )
        return queryset

    def lot_search(self, queryset, name, value):
        return self.generic(queryset, value)


class AuctionHistoryFilter(django_filters.FilterSet):
    """Filter auction history by user, action, or date"""

    query = django_filters.CharFilter(
        method="auction_history_search",
        label="",
        widget=TextInput(
            attrs={
                "placeholder": "Type to filter...",
                "hx-get": "",
                "hx-target": "div.table-container",
                "hx-trigger": "keyup changed delay:300ms",
                "hx-swap": "outerHTML",
                # 'hx-indicator':".progress",
            }
        ),
    )

    class Meta:
        model = AuctionHistory
        fields = []  # nothing here so no buttons show up

    def generic(self, queryset, value):
        return queryset.filter(
            Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
            | Q(action__icontains=value)
            | Q(applies_to__icontains=value)
        )

    def auction_history_search(self, queryset, name, value):
        return self.generic(queryset, value)


class LotFilter(django_filters.FilterSet):
    """This is the core of both the lot view and the recommendation engine
    A single queryset is annotated with information like distance, and sorted based on the user's (or page's) selection

    """

    def __init__(self, *args, **kwargs):
        self.canShowAuction = True
        self.latitude = kwargs.pop("latitude", None)
        self.longitude = kwargs.pop("longitude", None)
        self.listType = kwargs.pop("listType", None)  # a special filter for recommended lot views
        self.keywords = kwargs.pop("keywords", [])

        # Get request and user
        if "request" in kwargs:
            self.request = kwargs["request"]
            self.user = self.request.user
            # get location from cookie
            self.latitude = self.request.COOKIES.get("latitude", self.latitude)
            self.longitude = self.request.COOKIES.get("longitude", self.longitude)
        else:
            self.user = kwargs.pop("user")

        # Try to get location from userdata if not set
        if not self.latitude and not self.longitude and self.user.is_authenticated:
            self.latitude = self.user.userdata.latitude
            self.longitude = self.user.userdata.longitude

        # annotate lots with the distance to the request user, requires self.latitude and self.longitude
        self.showLocal = bool(self.latitude and self.longitude)
        self.showOwnLots = True  # show lots from the request user. I don't think this is used anywhere
        self.maxRange = 70  # only applies if self.showLocal = True
        self.showDeactivated = False  # show lots that users have deliberately deactivated
        if self.user.is_superuser:
            self.showBanned = True
        else:
            # this is really only set to true if you are viewing your own lots
            self.showBanned = False
        # could be "yes" or "no", only matters for authenticated users, set to no to only see unseen things
        self.showViewed = "all"
        if kwargs.pop("onlyUnviewed", False):
            self.showViewed = "no"
        # "all", "open", "unsold", or "ended".  If regarding an auction, should default to all
        self.status = "open"
        self.showShipping = True
        self.shippingLocation = 52  # USA, later we might set this with a cookie like we do with lat and lng
        if self.user.is_authenticated:
            # lots for local pickup
            if self.user.userdata.local_distance:
                self.maxRange = self.user.userdata.local_distance
            # lots that can be shipped to the user's location
            if self.user.userdata.location:
                self.shippingLocation = self.user.userdata.location
        self.ignore = kwargs.pop("ignore", False)
        self.regardingAuction = kwargs.pop(
            "regardingAuction", None
        )  # force only displaying lots from a particular auction
        # force only displaying lots for a particular user (not necessarily the request user)
        self.regardingUser = kwargs.pop("regardingUser", None)
        self.order = kwargs.pop("order", "-lot_number")  # default ordering is just the most recent on top
        forceAuction = kwargs.pop("auction", None)
        if forceAuction and self.listType == "auction":
            try:
                self.regardingAuction = Auction.objects.get(slug=forceAuction, is_deleted=False)
            except Auction.DoesNotExist:
                pass
        forceAuction = self.request.GET.get("auction") if hasattr(self, "request") else None
        if forceAuction:
            if forceAuction != "no_auction":
                try:
                    self.regardingAuction = Auction.objects.get(slug=forceAuction, is_deleted=False)
                except Auction.DoesNotExist:
                    pass
            else:
                self.regardingAuction = None
        if self.regardingAuction:
            regardingAuctionSlug = self.regardingAuction.slug
            self.showLocal = False
            self.showShipping = False
            # self.status = "all"
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
        self.possibleAuctions = Auction.objects.exclude(is_deleted=True).order_by("title")
        if self.user.is_authenticated:
            self.possibleAuctions = self.possibleAuctions.filter(
                Q(auctiontos__user=self.user) | specialAuctions
            ).distinct()
        else:
            self.possibleAuctions = self.possibleAuctions.filter(specialAuctions)
        auction_choices = [(o.slug, o.title) for o in self.possibleAuctions]
        super().__init__(*args, **kwargs)  # this must go above filters
        self.filters["auction"].extra["choices"] = [
            {"no_auction": "noAuction", "No auction": "title"}
        ] + auction_choices
        self.filters["ships"].extra["choices"] = self.ships_choices()
        # if self.regardingAuction:
        #     self.filters['auction'].initial=self.regardingAuction.slug
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "search-form"
        self.helper.form_tag = True
        for name, field in self.filters.items():
            widget = field.field.widget
            widget.attrs["class"] = "col-12 form-control"

    STATUS = (
        ("all", "Open/ended"),
        ("unsold", "Open (hide bought now)"),
        ("closed", "Ended"),
    )
    VIEWED = (
        ("yes", "Only viewed"),
        ("no", "Only unviewed"),
    )
    ORDER = (
        ("popularity", "Most popular"),
        ("unloved", "Least popular"),
        ("recommended", "Recommended"),
        ("oldest", "Oldest"),
    )

    def ships_choices(self):
        choices = [(o.pk, o.name) for o in Location.objects.all().order_by("name")]
        return [{"local_only": "local", "Local pickup": "title"}] + choices

    def generate_attrs(placeholder="", tooltip=""):
        return {
            "class": " col-12",
            "placeholder": placeholder,
            "data-bs-toggle": "tooltip",
            "data-placement": "bottom",
            "title": tooltip,
        }

    q = django_filters.CharFilter(
        label="",
        method="text_filter",
        widget=TextInput(attrs=generate_attrs("Search here", "")),
        help_text="Looks in title, description, seller or lot number",
    )
    auction = django_filters.ChoiceFilter(
        label="",
        empty_label="Any auction",
        method="filter_by_auction",
        widget=Select(attrs=generate_attrs(None, "")),
        help_text="Auctions you've joined appear here",
    )
    category = django_filters.ModelChoiceFilter(
        label="",
        queryset=Category.objects.all().order_by("name"),
        method="filter_by_category",
        empty_label="Any category",
        widget=Select(attrs=generate_attrs(None, "Picking something here overrides your ignored categories")),
    )
    status = django_filters.ChoiceFilter(
        label="",
        choices=STATUS,
        method="filter_by_status",
        empty_label="Open",
        widget=Select(
            attrs=generate_attrs(
                None,
                "Has no effect if you're viewing a single user's lots or an auction that's ended",
            )
        ),
    )
    distance = django_filters.NumberFilter(
        label="",
        method="filter_by_distance",
        widget=NumberInput(attrs=generate_attrs("Distance (miles)", "")),
        help_text="Doesn't apply to lots in an auction",
    )
    ships = django_filters.ChoiceFilter(
        label="",
        method="filter_by_shipping_location",
        empty_label="Ships to",
        widget=Select(attrs=generate_attrs(None, "")),
        help_text="Doesn't apply to lots in an auction",
    )
    user = django_filters.CharFilter(
        label="",
        method="filter_by_user",
        widget=TextInput(attrs={"style": "display:none"}),
    )
    viewed = django_filters.ChoiceFilter(
        label="",
        choices=VIEWED,
        method="filter_by_viewed",
        empty_label="All",
    )
    order = django_filters.ChoiceFilter(
        label="",
        choices=ORDER,
        method="filter_by_order",
        empty_label="Newest",
        help_text="Sort by least popular to find deals",
    )

    class Meta:
        model = Lot
        fields = {}  # nothing here so no buttons show up

    @property
    def qs(self):
        primary_queryset = super().qs
        primary_queryset = primary_queryset.filter(is_deleted=False)
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
            primary_queryset = primary_queryset.filter(active=True, winning_price__isnull=True)
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
                is_watched_by_req_user=Exists(Watch.objects.filter(lot_number=OuterRef("lot_number"), user=self.user))
            )
        if self.user.is_authenticated:
            # messages for owner of lot
            primary_queryset = primary_queryset.annotate(
                owner_chats=Count(
                    "lothistory",
                    filter=Q(
                        lothistory__seen=False,
                        lothistory__changed_price=False,
                        lothistory__removed=False,
                    ),
                    distinct=True,
                )
            )
        # messages for other user
        primary_queryset = primary_queryset.annotate(
            all_chats=Count(
                "lothistory",
                filter=Q(lothistory__changed_price=False, lothistory__removed=False),
                distinct=True,
            )
        )
        if self.order == "popularity" or self.order == "-popularity":
            primary_queryset = primary_queryset.annotate(
                popularity=2 * Count("pageview", distinct=True)
                + Count(
                    "lothistory",
                    filter=Q(lothistory__changed_price=False),
                    distinct=True,
                )
                +
                # this is better than bids
                2.5
                * Count(
                    "lothistory",
                    filter=Q(lothistory__changed_price=True),
                    distinct=True,
                )
            )
        if self.order == "-recommended":
            primary_queryset = primary_queryset.annotate(recommended=Sum(0, output_field=IntegerField()))
            if self.keywords:
                for word in self.keywords:
                    primary_queryset = primary_queryset.annotate(
                        recommended=Case(
                            When(
                                lot_name__icontains=word, then=F("recommended") + 50
                            ),  # this is how much to increase the weight when a word matches
                            default=F("recommended"),
                        )
                    )
            if self.user.is_authenticated:
                interest = Subquery(
                    UserInterestCategory.objects.filter(category=OuterRef("species_category"), user=self.user).values(
                        "interest"
                    )
                )
                primary_queryset = primary_queryset.annotate(
                    recommended=(((F("promotion_weight") + 1) / 10) * interest / 5) + F("recommended")
                )
            else:
                # if not signed in, recommended = most viewed
                # this sucks because you always see the same damned lots.
                # We could show most viewed here, but that just means that popularity breeds popularity
                # primary_queryset = primary_queryset.annotate(
                #    recommended = Count('pageview', distinct=True)
                # )
                # instead, let's just show newest (unless we have keywords...)
                if not self.keywords:
                    self.order = "-lot_number"
        show_very_new_lots = False
        if self.user.is_superuser:
            show_very_new_lots = True
        if self.regardingAuction:
            # might want to change this to be `and self.regardingAuction.online_bidding == 'disable'`
            if not self.regardingAuction.is_online:
                show_very_new_lots = True
        if not show_very_new_lots:
            if self.user.is_authenticated:
                # DO show very new lots from this user
                primary_queryset = primary_queryset.exclude(
                    ~Q(user=self.user),
                    date_posted__gte=timezone.now() - datetime.timedelta(minutes=20),
                )
            else:
                primary_queryset = primary_queryset.exclude(
                    date_posted__gte=timezone.now() - datetime.timedelta(minutes=20)
                )

        # filter by 3 things:
        # local_qs = local lots, within the max range
        # shipping_qs = lots that ship to your location
        # auction_qs = either a single auction or private auctions you've joined + promoted auctions

        auction_qs = Q(pk__isnull=True)
        local_qs = Q(pk__isnull=True)
        if self.showLocal:
            # then calculate the distance to each lot
            primary_queryset = primary_queryset.annotate(
                distance=distance_to(
                    self.latitude,
                    self.longitude,
                    lat_field_name="`auctions_lot`.`latitude`",
                    lng_field_name="`auctions_lot`.`longitude`",
                )
            )
            # finally, filter by max range
            if self.maxRange:  # and not self.regardingAuction:
                # if you specify both range and auction, range does nothing
                local_qs = Q(distance__lte=self.maxRange, auction__isnull=True, local_pickup=True)
        if self.ignore and self.user.is_authenticated:
            allowedCategories = Category.objects.exclude(userignorecategory__user=self.user)
            primary_queryset = primary_queryset.filter(species_category__in=allowedCategories)
        if not self.showOwnLots and self.user.is_authenticated:
            primary_queryset = primary_queryset.exclude(user=self.user.pk)  # don't show your own lots
        if self.showShipping and self.shippingLocation:
            shipping_qs = Q(shipping_locations=self.shippingLocation, auction__isnull=True)
        else:
            shipping_qs = Q(pk__isnull=True)
        if self.canShowAuction:
            if self.regardingAuction:
                auction_qs = Q(auction=self.regardingAuction)
            else:
                # auction_qs = Q(pk__isnull=True)
                # auction_qs = Q(auction__promote_this_auction=True)
                if self.user.is_authenticated:
                    # this shows any auction you've joined + any public auction.  Perhaps this should be a preference?
                    # auction_qs = Q(auction__pk__in=self.possibleAuctions)|Q(auction__promote_this_auction=True)
                    # this shows any auction you've joined.  See https://github.com/iragm/fishauctions/issues/66
                    auction_qs = Q(auction__pk__in=self.possibleAuctions) | Q(auction__isnull=True)
                else:
                    # anonymous users can see lots from all promoted auctions
                    auction_qs = Q(auction__promote_this_auction=True) | Q(auction__isnull=True)
                # auction_qs = Q(auction__auctiontos__user=self.user, auction__promote_this_auction=False)|Q(auction__promote_this_auction=True)
        # putting them all together:
        primary_queryset = primary_queryset.filter(Q(local_qs) | Q(shipping_qs) | Q(auction_qs))
        return primary_queryset.order_by(self.order)

    def filter_by_order(self, queryset, name, value):
        if value == "oldest":
            self.order = "lot_number"
        if value == "newest":
            self.order = "-lot_number"
        if value == "recommended":
            self.order = "-recommended"
        if value == "popularity":
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
            self.form.initial["status"] = "all"
        return queryset.filter(user__username=value)

    def filter_by_category(self, queryset, name, value):
        self.ignore = False
        return queryset.filter(species_category=value)

    def filter_by_auction(self, queryset, name, value):
        if value == "no_auction":
            return queryset.filter(auction__isnull=True)
        try:
            self.regardingAuction = Auction.objects.get(slug=value, is_deleted=False)
            # self.form.initial['auction'] = self.regardingAuction.slug
            if self.regardingAuction.closed:
                self.status = "all"
                self.form.initial["status"] = "all"
            return queryset.filter(auction__slug=value)
        except Exception:
            return queryset

    def filter_by_status(self, queryset, name, value):
        self.status = value
        return queryset

    def filter_by_viewed(self, queryset, name, value):
        if not self.user.is_authenticated:
            messages.error(self.request, "Sign in to use this filter")
        else:
            self.showViewed = value
        return queryset

    def text_filter(self, queryset, name, value):
        if value.isnumeric():
            return queryset.filter(
                Q(lot_number=int(value))
                | Q(lot_name__icontains=value)
                | Q(lot_number_int=value)
                | Q(custom_lot_number=value)
                | Q(custom_field_1=value)
                | Q(auctiontos_seller__bidder_number=value)
            )
        else:
            split = re.split(r"\bor\b", value)
            qList = Q()  # empty
            for fragment in split:
                fragment = fragment.strip()
                qList |= (
                    Q(summernote_description__icontains=fragment)
                    | Q(lot_name__icontains=fragment)
                    | Q(user__username=fragment)
                    | Q(custom_lot_number=fragment)
                    | Q(custom_field_1__icontains=fragment)
                    | Q(auctiontos_seller__bidder_number=fragment)
                )
            return queryset.filter(qList)

    def filter_by_shipping_location(self, queryset, name, value):
        if value:
            self.shippingLocation = value
            self.showShipping = True
        auction_param = self.request.GET.get("auction")
        if auction_param and auction_param != "no_auction":
            # if both this and auction are specified, auction wins and this does nothing
            self.showShipping = False
            return queryset
        if value == "local_only":
            self.showShipping = False
            return queryset.filter(Q(local_pickup=True) | Q(auction__isnull=False))
        return queryset


class UserLotFilter(LotFilter):
    """For the selling dashboard"""

    @property
    def qs(self):
        primary_queryset = super().qs
        return primary_queryset.filter(Q(user=self.request.user) | Q(auctiontos_seller__user=self.request.user))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.regardingUser = self.request.user
        self.showDeactivated = True
        self.showBanned = True


class UserWatchLotFilter(LotFilter):
    """A version of the lot filter that only shows lots watched by the current user"""

    @property
    def qs(self):
        primary_queryset = super().qs
        return primary_queryset.filter(watch__user=self.user)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.status = "all"
        self.form.initial["status"] = "ended"


class UserBidLotFilter(LotFilter):
    """A version of the lot filter that only shows lots bid on by the current user"""

    @property
    def qs(self):
        primary_queryset = super().qs
        return primary_queryset.filter(bid__user=self.user).distinct()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.status = ""
        self.form.initial["status"] = ""


class UserWonLotFilter(LotFilter):
    """A version of the lot filter that only shows lots won by the current user"""

    @property
    def qs(self):
        primary_queryset = super().qs
        return primary_queryset.filter(Q(winner=self.user) | Q(auctiontos_winner__user=self.user))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.status = "ended"
        self.form.initial["status"] = "closed"


def get_recommended_lots(
    user=None,
    listType=None,  # "auction", "local", or "shipping"
    auction=None,  # slug for auction, overrides listType to "auction"
    latitude=0,
    longitude=0,
    qty=10,
    keywords=[],
):
    """
    This is the core of the recommendation system
    Returns a queryset of lot objects ready for use in a template
    """
    if auction:
        listType = "auction"
    qs = LotFilter(
        user=user,
        ignore=True,
        onlyUnviewed=True,
        listType=listType,
        auction=auction,
        latitude=latitude,
        longitude=longitude,
        order="-recommended",
        keywords=keywords,
    ).qs
    return qs[:qty]
