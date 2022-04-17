from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import *

class FaqAdmin(admin.ModelAdmin):
    model = FAQ 
    list_display = ("category_text",'question')
    #list_filter = ("title",)
    search_fields = ("category_text",'question','answer', )

class AdCampaignResponseInline(admin.TabularInline):
     fields = ['user', 'clicked','timestamp']
     readonly_fields = ['user', 'clicked','timestamp']
     verbose_name = "Response"
     verbose_name_plural = "Responses"
     model = AdCampaignResponse
     extra = 0

class AdCampaignInline(admin.TabularInline):
    fields = ['title', 'begin_date', 'end_date', 'auction', 'category',]
    readonly_fields = ("number_of_impressions", "number_of_clicks", "click_rate",) 
    verbose_name = "Campaign in this group"
    verbose_name_plural = "Campaigns in this group"
    model = AdCampaign
    extra = 0

class AdCampaignAdmin(admin.ModelAdmin):
    list_display = ['title', 'campaign_group', 'begin_date', 'end_date', 'number_of_impressions', 'number_of_clicks', 'click_rate']
    #exclude = []
    readonly_fields = ("number_of_impressions", "number_of_clicks", "click_rate",) 
    
    inlines = [
        #AdCampaignResponseInline, # this is far too noisy
    ]
    search_fields = ("title",'external_url',)

class AdCampaignGroupAdmin(admin.ModelAdmin):
    list_display = ['title', 'contact_user', 'number_of_campaigns', 'number_of_impressions', 'number_of_clicks', 'click_rate']
    #exclude = []
    readonly_fields = ("number_of_impressions", "number_of_clicks", "click_rate",) 
    inlines = [
        AdCampaignInline,
    ]
    search_fields = ("title",)

class BlogPostAdmin(admin.ModelAdmin):
    model = BlogPost

class AuctionTOSInline(admin.TabularInline):
    model = AuctionTOS 
    list_display = ()
    readonly_fields = ("pickup_location", "user", "auction",) 
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
    list_display = ("category", "user", "as_percent",)
    list_filter = ()
    search_fields = ()
    extra = 0

class UserdataInline(admin.StackedInline):
    model = UserData
    can_delete = False
    verbose_name_plural = 'User data'
    exclude = ('unsubscribe_link', 'rank_unique_species','number_unique_species', 'rank_total_lots', 'number_total_lots', 'rank_total_spent', \
        'number_total_spent', 'rank_total_bids', 'number_total_bids', 'number_total_sold', \
        'rank_total_sold', 'total_volume', 'rank_volume', 'seller_percentile', 'buyer_percentile', 'volume_percentile', )
    readonly_fields = ('last_activity', 'dismissed_cookies_tos', 'last_auction_used', 'last_ip_address',)
# Extend Django's base user model
class UserAdmin(BaseUserAdmin):
    list_display = ['username', 'first_name', 'last_name', 'email', 'last_activity', 'date_joined']
    inlines = [
        UserdataInline,
        AuctionTOSInline, # too much noise, but important to have
        InterestInline, # too much noise
    ]
    search_fields = ("first_name",'last_name', 'userdata__club__abbreviation', 'email', 'username')
    def last_activity(self, obj):
        return obj.userdata.last_activity
    last_activity.admin_order_field  = 'userdata__last_activity' # this doesn't seem to work, but you can use this url: admin/auth/user/?o=-4
    last_activity.short_description = 'Last activity'

class GeneralInterestAdmin(admin.ModelAdmin):
    model = GeneralInterest

class UserInline(admin.TabularInline):
     fields = ['__str__',]
     readonly_fields = ['__str__',]
     verbose_name = "Club member"
     verbose_name_plural = "Club members"
     #fk_name = 'userdata'
     model = UserData
     extra = 0

class ClubAdmin(admin.ModelAdmin):
    model = Club 
    list_display = ("name", "contact_email", "date_contacted")
    search_fields = ("name",'abbreviation', 'contact_email', 'homepage',)
    list_filter =  (
        "active",
        ("date_contacted", admin.EmptyFieldListFilter),
        ("contact_email", admin.EmptyFieldListFilter),
        ("notes", admin.EmptyFieldListFilter),
        ("latitude", admin.EmptyFieldListFilter),
        "interests",
    )
    inlines = [
         UserInline,
    ]

class LocationAdmin(admin.ModelAdmin):
    model = Location 
    search_fields = ("name",)

class PickupLocationInline(admin.TabularInline):
    model = PickupLocation 
    list_display = ("name", "user", "auction", "description", "pickup_time", "second_pickup_time")
    list_filter = ()
    search_fields = ()
    extra = 0


class AuctionAdmin(admin.ModelAdmin):
    model = Auction 
    list_display = ("title",'created_by')
    #list_filter = ("title",)
    search_fields = ("title",'created_by__first_name','created_by__last_name', )
    inlines = [
         PickupLocationInline,
    ]

class BidInline(admin.TabularInline):
    model = Bid 
    list_display = ("user", "amount",)
    list_filter = ()
    search_fields = ("user__first_name",'user__last_name',)
    extra = 0

class WatchInline(admin.TabularInline):
    model = Watch 
    list_display = ("user",)
    list_filter = ()
    search_fields = ("user__first_name",'user__last_name',)
    extra = 0

class LotAdmin(admin.ModelAdmin):
    model = Lot 
    list_display = ("lot_name", "auction", "lot_number", "user", "species_category",)
    list_filter = ("active","auction","banned")
    search_fields = ("lot_number","lot_name","description","species_category__name","user__first_name","user__last_name")
    inlines = [
         BidInline,
         WatchInline,
    ]
    
class BidAdmin(admin.ModelAdmin):
    model = Bid 
    menu_label = "Bids"  
    menu_icon = "bold"
    menu_order = 400
    add_to_settings_menu = False
    exclude_from_explorer = False
    list_display = ("user", "lot_number", "bid_time", "amount", )
    list_filter = ("user","lot_number",)
    search_fields = ("lot_number","user",)

class SoldLotInline(admin.TabularInline):
     fields = ['__str__', 'your_cut','club_cut']
     readonly_fields = ['__str__','your_cut','club_cut']
     verbose_name = "Lot sold"
     verbose_name_plural = "Lots sold"
     fk_name = 'seller_invoice'
     model = Lot
     extra = 0

class BoughtLotInline(admin.TabularInline):
     fields = ['__str__', 'winning_price']
     readonly_fields = ['__str__','winning_price']
     verbose_name = "Lot bought"
     verbose_name_plural = "Lots bought"
     fk_name = 'buyer_invoice'
     model = Lot
     extra = 0

class InvoiceAdmin(admin.ModelAdmin):
    model = Invoice 
    list_display = ("user", "__str__", 'rounded_net', "status", )
    list_filter = ("auction", "status",)
    search_fields = ("user__first_name", "user__last_name",)
    readonly_fields = ("opened", "printed",)
    inlines = [
         SoldLotInline,
         BoughtLotInline,
    ]

class ChatAdmin(admin.ModelAdmin):
    model = LotHistory 
    list_display = ("lot", "user", "message", 'timestamp',)
    list_filter = ("timestamp",'changed_price',)
    search_fields = ("user__first_name", "user__last_name", "message", "lot__lot_number")
    ordering = ('-timestamp',)

class CategoryAdmin(admin.ModelAdmin):
    model = Category 
    menu_label = "Categories"  
    list_display = ("name", )
    #list_filter = ()
    search_fields = ("name",)

class ProductAdmin(admin.ModelAdmin):
    model = Product 
    menu_label = "Products"  
    list_display = ("category","common_name", "scientific_name", "breeder_points", )
    list_filter = ("category",)
    search_fields = ("common_name", "scientific_name", "category__name")

class BanAdmin(admin.ModelAdmin):
    model = UserBan
    menu_label = "User to user bans"  
    list_display = ("user","banned_user",)
    list_filter = ()
    search_fields = ("user__first_name", "user__last_name", "banned_user__first_name","banned_user__last_name",)


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
admin.site.register(FAQ, FaqAdmin)