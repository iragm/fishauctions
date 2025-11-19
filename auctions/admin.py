import csv
import datetime

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.http import HttpResponse

from .models import (
    FAQ,
    AdCampaign,
    AdCampaignGroup,
    AdCampaignResponse,
    Auction,
    AuctionCampaign,
    AuctionHistory,
    AuctionTOS,
    Bid,
    BlogPost,
    Category,
    Club,
    GeneralInterest,
    Invoice,
    InvoicePayment,
    Location,
    Lot,
    LotHistory,
    PageView,
    PickupLocation,
    Product,
    SearchHistory,
    UserBan,
    UserData,
    UserInterestCategory,
    UserLabelPrefs,
    Watch,
    guess_category,
)


def export_to_csv(modeladmin, request, queryset):
    opts = modeladmin.model._meta
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f"attachment;filename={opts.verbose_name}.csv"
    writer = csv.writer(response)
    fields = [field for field in opts.get_fields() if not field.many_to_many and not field.one_to_many]
    # Write a first row with header information
    writer.writerow([field.verbose_name for field in fields])
    # Write data rows
    for obj in queryset:
        data_row = []
        for field in fields:
            value = getattr(obj, field.name)
            if isinstance(value, datetime.datetime):
                value = value.strftime("%d/%m/%Y")
            data_row.append(value)
        writer.writerow(data_row)

    return response


export_to_csv.short_description = "Export to CSV"


class FaqAdmin(admin.ModelAdmin):
    model = FAQ
    list_display = ("category_text", "question")
    # list_filter = ("title",)
    search_fields = (
        "category_text",
        "question",
        "answer",
    )


class SearchHistoryAdmin(admin.ModelAdmin):
    model = SearchHistory
    list_display = ("user", "search")
    search_fields = ()


class AdCampaignResponseInline(admin.TabularInline):
    fields = ["user", "clicked", "timestamp"]
    readonly_fields = ["user", "clicked", "timestamp"]
    verbose_name = "Response"
    verbose_name_plural = "Responses"
    model = AdCampaignResponse
    extra = 0


class AdCampaignInline(admin.TabularInline):
    fields = [
        "title",
        "begin_date",
        "end_date",
        "auction",
        "category",
    ]
    readonly_fields = (
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    )
    verbose_name = "Campaign in this group"
    verbose_name_plural = "Campaigns in this group"
    model = AdCampaign
    extra = 0


class AdCampaignAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "campaign_group",
        "begin_date",
        "end_date",
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    ]
    # exclude = []
    readonly_fields = (
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    )

    inlines = [
        # AdCampaignResponseInline, # this is far too noisy
    ]
    search_fields = (
        "title",
        "external_url",
    )


class AdCampaignGroupAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "contact_user",
        "number_of_campaigns",
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    ]
    # exclude = []
    readonly_fields = (
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    )
    inlines = [
        AdCampaignInline,
    ]
    search_fields = ("title",)


class InvoicePaymentInline(admin.TabularInline):
    model = InvoicePayment
    extra = 0

    def get_readonly_fields(self, request, obj=None):
        # make all InvoicePayment fields readonly (except the FK back to Invoice which is implied)
        return tuple(
            f.name
            for f in self.model._meta.get_fields()
            if not (f.many_to_many or f.one_to_many) and f.name != "invoice"
        )

    def has_add_permission(self, request, obj=None):
        # disallow creating payments from the invoice admin inline (payments should come from payment handlers)
        return False


class BlogPostAdmin(admin.ModelAdmin):
    model = BlogPost


class AuctionTOSInline(admin.TabularInline):
    model = AuctionTOS
    list_display = ()
    readonly_fields = (
        "pickup_location",
        "user",
        "auction",
    )
    list_filter = ()
    search_fields = ()
    extra = 0


class PickupLocationAdmin(admin.ModelAdmin):
    model = PickupLocation
    inlines = [
        AuctionTOSInline,
    ]


class InterestInline(admin.TabularInline):
    model = UserInterestCategory
    list_display = (
        "category",
        "user",
        "as_percent",
    )
    list_filter = ()
    search_fields = ()
    extra = 0


class UserdataInline(admin.StackedInline):
    model = UserData
    can_delete = False
    verbose_name_plural = "User data"
    exclude = (
        "unsubscribe_link",
        "rank_unique_species",
        "number_unique_species",
        "rank_total_lots",
        "number_total_lots",
        "rank_total_spent",
        "number_total_spent",
        "rank_total_bids",
        "number_total_bids",
        "number_total_sold",
        "rank_total_sold",
        "total_volume",
        "rank_volume",
        "seller_percentile",
        "buyer_percentile",
        "volume_percentile",
    )
    readonly_fields = (
        "last_activity",
        "dismissed_cookies_tos",
        "last_auction_used",
        "last_ip_address",
    )


class UserLabelPrefsInline(admin.StackedInline):
    model = UserLabelPrefs
    can_delete = False
    verbose_name_plural = "User Label Preferences"


# Extend Django's base user model
class UserAdmin(BaseUserAdmin):
    list_display = [
        "username",
        "first_name",
        "last_name",
        "email",
        "last_activity",
        "date_joined",
    ]
    inlines = [
        UserdataInline,
        UserLabelPrefsInline,
        # AuctionTOSInline,  # too much noise, but important to have
        # InterestInline,  # too much noise
    ]
    search_fields = (
        "first_name",
        "last_name",
        # "userdata__club__abbreviation",
        "email",
        "username",
    )

    readonly_fields = [
        "last_activity",
        "date_joined",
        "last_login",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("userdata__club", "userdata__last_auction_used", "userdata__location")
            .prefetch_related("userlabelprefs")
        )

    def last_activity(self, obj):
        return obj.userdata.last_activity

    # this doesn't seem to work, but you can use this url: admin/auth/user/?o=-4
    last_activity.admin_order_field = "userdata__last_activity"
    last_activity.short_description = "Last activity"


class GeneralInterestAdmin(admin.ModelAdmin):
    model = GeneralInterest


class UserInline(admin.TabularInline):
    fields = [
        "__str__",
    ]
    readonly_fields = [
        "__str__",
    ]
    verbose_name = "Club member"
    verbose_name_plural = "Club members"
    # fk_name = 'userdata'
    model = UserData
    extra = 0


class ClubAdmin(admin.ModelAdmin):
    model = Club
    list_display = ("name", "contact_email", "date_contacted_for_in_person_auctions")
    search_fields = (
        "name",
        "abbreviation",
        "contact_email",
        "homepage",
    )
    list_filter = (
        "active",
        ("date_contacted", admin.EmptyFieldListFilter),
        ("date_contacted_for_in_person_auctions", admin.EmptyFieldListFilter),
        ("contact_email", admin.EmptyFieldListFilter),
        ("notes", admin.EmptyFieldListFilter),
        ("latitude", admin.EmptyFieldListFilter),
        "interests",
    )
    inlines = [
        UserInline,
    ]
    actions = [export_to_csv]


class LocationAdmin(admin.ModelAdmin):
    model = Location
    search_fields = ("name",)


class PickupLocationInline(admin.TabularInline):
    model = PickupLocation
    list_display = (
        "name",
        "user",
        "auction",
        "description",
        "pickup_time",
        "second_pickup_time",
    )
    list_filter = ()
    search_fields = ()
    extra = 0


class AuctionAdmin(admin.ModelAdmin):
    model = Auction
    list_display = ("title", "created_by")
    # list_filter = ("title",)
    search_fields = (
        "title",
        "created_by__first_name",
        "created_by__last_name",
    )
    inlines = [
        PickupLocationInline,
    ]

    actions = ["export_user_emails"]

    def export_user_emails(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = "attachment;filename=auctions.csv"
        writer = csv.writer(response)
        writer.writerow(["Auction", "Name", "Email"])
        for obj in queryset:
            writer.writerow([obj.title, obj.created_by.first_name, obj.created_by.email])
        return response


class BidInline(admin.TabularInline):
    model = Bid
    list_display = (
        "user",
        "amount",
    )
    list_filter = ()
    search_fields = (
        "user__first_name",
        "user__last_name",
    )
    extra = 0

    def get_readonly_fields(self, request, obj=None):
        # Make all Bid fields readonly (except the FK back to Lot which is implied)
        return tuple(
            f.name
            for f in self.model._meta.get_fields()
            if not (f.many_to_many or f.one_to_many) and f.name != "lot_number"
        )


class WatchInline(admin.TabularInline):
    model = Watch
    list_display = ("user",)
    list_filter = ()
    search_fields = (
        "user__first_name",
        "user__last_name",
    )
    extra = 0

    def get_readonly_fields(self, request, obj=None):
        # Make all Watch fields readonly (except the FK back to Lot which is implied)
        return tuple(
            f.name
            for f in self.model._meta.get_fields()
            if not (f.many_to_many or f.one_to_many) and f.name != "lot_number"
        )


class LotAdmin(admin.ModelAdmin):
    model = Lot
    list_display = (
        "lot_name",
        "auction",
        "user",
        "species_category",
    )
    list_filter = ("active", "auction", "banned")
    search_fields = (
        "lot_number",
        "lot_name",
        "species_category__name",
        "user__first_name",
        "user__last_name",
    )
    exclude = (
        "slug",
        "image",
        "i_bred_this_fish",
        "seller_invoice",
        "winner_invoice",
        "image_source",
        "date_posted",
        "last_bump_date",
        "species",
        "auction",
        "refunded",
        "ban_reason",
        "lot_run_duration",
        "relist_countdown",
        "number_of_bumps",
        "watch_warning_email_sent",
        "transportable",
        "promote_this_lot",
        "promotion_budget",
        "promotion_weight",
        "added_by",
    )
    readonly_fields = (
        "user",
        "auctiontos_seller",
        "auctiontos_winner",
        "winner",
        "auction",
        "reference_link",
        "buyer_invoice",
        "seller_invoice",
    )
    inlines = [
        BidInline,
        WatchInline,
    ]


class LotAutoCategory(Lot):
    class Meta:
        proxy = True


class LotAutoCategoryAdmin(admin.ModelAdmin):
    model = LotAutoCategory
    fields = ("lot_name", "species_category", "category_automatically_added")
    list_display = (
        "lot_name",
        "species_category",
    )
    list_filter = ("active", "auction", "banned", "category_automatically_added")
    search_fields = (
        "lot_number",
        "lot_name",
        "description",
        "species_category__name",
        "user__first_name",
        "user__last_name",
    )
    ordering = ("-lot_number",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(category_automatically_added=True)

    actions = ["approve", "retry", "uncategorize"]

    def approve(self, request, queryset):
        """Change category_automatically_added to false to remove the warning banner from these"""
        for lot in queryset:
            lot.category_automatically_added = False
            lot.save()
        self.message_user(request, "Lot categories have been marked as correct")

    def retry(self, request, queryset):
        """When the category isn't right, try again"""
        count = 0
        for lot in queryset:
            category = guess_category(lot.lot_name)
            if category:
                count += 1
                lot.species_category = category
                lot.category_automatically_added = True
                lot.save()
        self.message_user(
            request,
            f"{count} out of {queryset.count()} lots were automatically assigned to a category",
        )

    def uncategorize(self, request, queryset):
        """Change to uncategorized"""
        uncategorized = Category.objects.filter(name="Uncategorized").first()
        for lot in queryset:
            lot.species_category = uncategorized
            lot.category_automatically_added = False
            lot.save()
        self.message_user(request, "Lots have been changed to no category")


class BidAdmin(admin.ModelAdmin):
    model = Bid
    menu_label = "Bids"
    menu_icon = "bold"
    menu_order = 400
    add_to_settings_menu = False
    exclude_from_explorer = False
    list_display = (
        "user",
        "lot_number",
        "bid_time",
        "amount",
    )
    list_filter = (
        "user",
        "lot_number",
    )
    search_fields = (
        "lot_number",
        "user",
    )


class SoldLotInline(admin.TabularInline):
    fields = ["__str__"]
    readonly_fields = ["__str__"]
    verbose_name = "Lot sold"
    verbose_name_plural = "Lots sold"
    fk_name = "seller_invoice"
    model = Lot
    extra = 0


class BoughtLotInline(admin.TabularInline):
    fields = ["__str__", "winning_price"]
    readonly_fields = ["__str__", "winning_price"]
    verbose_name = "Lot bought"
    verbose_name_plural = "Lots bought"
    fk_name = "buyer_invoice"
    model = Lot
    extra = 0


class InvoiceAdmin(admin.ModelAdmin):
    model = Invoice
    list_display = (
        "__str__",
        "rounded_net",
        "status",
    )
    list_filter = (
        "auction",
        "status",
    )
    search_fields = ("auctiontos_user__name",)
    readonly_fields = ()  # overridden by get_readonly_fields
    inlines = [
        SoldLotInline,
        BoughtLotInline,
        InvoicePaymentInline,
    ]

    def get_readonly_fields(self, request, obj=None):
        # make all Invoice model fields readonly in the admin
        return tuple(f.name for f in self.model._meta.get_fields() if not (f.many_to_many or f.one_to_many))


class ChatAdmin(admin.ModelAdmin):
    model = LotHistory
    list_display = (
        "lot",
        "user",
        "message",
        "timestamp",
    )
    list_filter = (
        "timestamp",
        "changed_price",
    )
    search_fields = (
        "user__first_name",
        "user__last_name",
        "message",
        "lot__lot_number",
    )
    ordering = ("-timestamp",)


class CategoryAdmin(admin.ModelAdmin):
    model = Category
    menu_label = "Categories"
    list_display = ("name",)
    # list_filter = ()
    search_fields = ("name",)


class ProductAdmin(admin.ModelAdmin):
    model = Product
    menu_label = "Products"
    list_display = (
        "category",
        "common_name",
        "scientific_name",
        "breeder_points",
    )
    list_filter = ("category",)
    search_fields = ("common_name", "scientific_name", "category__name")


class BanAdmin(admin.ModelAdmin):
    model = UserBan
    menu_label = "User to user bans"
    list_display = (
        "user",
        "banned_user",
    )
    list_filter = ()
    search_fields = (
        "user__first_name",
        "user__last_name",
        "banned_user__first_name",
        "banned_user__last_name",
    )


class AuctionTOSAdmin(admin.ModelAdmin):
    model = AuctionTOS
    list_display = ("name", "auction", "manually_added")
    search_fields = (
        "name",
        "user__email",
        "user__username",
        "bidder_number",
    )


class PageViewAdmin(admin.ModelAdmin):
    model = PageView
    list_display = ("user", "ip_address", "source", "url", "date_start")
    readonly_fields = (
        "user",
        "auction",
        "lot_number",
    )
    ordering = ("-date_start",)


class AuctionCampaignAdmin(admin.ModelAdmin):
    model = AuctionCampaign
    list_display = ("auction", "source", "result")


class AuctionHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "auction",
        "user",
        "action",
        "applies_to",
        "timestamp",
    )
    list_filter = (
        "timestamp",
        "applies_to",
    )
    search_fields = (
        "user__first_name",
        "user__last_name",
        "action",
        "auction__title",
    )
    ordering = ("-timestamp",)

    def get_readonly_fields(self, request, obj=None):
        # make all AuctionHistory model fields readonly in the admin (this is an audit log)
        return tuple(f.name for f in self.model._meta.get_fields() if not (f.many_to_many or f.one_to_many))


admin.site.register(PickupLocation, PickupLocationAdmin)
admin.site.unregister(User)
admin.site.register(User, UserAdmin)
admin.site.register(UserBan, BanAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(Product, ProductAdmin)
admin.site.register(Auction, AuctionAdmin)
admin.site.register(Invoice, InvoiceAdmin)
admin.site.register(LotHistory, ChatAdmin)
admin.site.register(Lot, LotAdmin)
admin.site.register(Location, LocationAdmin)
admin.site.register(Club, ClubAdmin)
admin.site.register(GeneralInterest, GeneralInterestAdmin)
admin.site.register(BlogPost, BlogPostAdmin)
admin.site.register(AdCampaign, AdCampaignAdmin)
admin.site.register(AdCampaignGroup, AdCampaignGroupAdmin)
admin.site.register(SearchHistory, SearchHistoryAdmin)
admin.site.register(FAQ, FaqAdmin)
admin.site.register(LotAutoCategory, LotAutoCategoryAdmin)
admin.site.register(AuctionTOS, AuctionTOSAdmin)
admin.site.register(PageView, PageViewAdmin)
admin.site.register(AuctionCampaign, AuctionCampaignAdmin)
admin.site.register(AuctionHistory, AuctionHistoryAdmin)
