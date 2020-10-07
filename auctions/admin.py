from django.contrib import admin
from .models import Lot, Bid, Auction, Invoice, Category, Product, Club, Location

class ClubAdmin(admin.ModelAdmin):
    model = Club 
    search_fields = ("name",)

class LocationAdmin(admin.ModelAdmin):
    model = Location 
    search_fields = ("name",)

class AuctionAdmin(admin.ModelAdmin):
    model = Auction 
    list_display = ("title",'created_by')
    list_filter = ("title",)
    search_fields = ("title",)

class BidInline(admin.TabularInline):
    model = Bid 
    list_display = ("user", "amount",)
    list_filter = ()
    search_fields = ("user",)
    extra = 0

class LotAdmin(admin.ModelAdmin):
    model = Lot 
    menu_label = "Lots"  
    menu_icon = "tag"
    menu_order = 300
    add_to_settings_menu = False
    exclude_from_explorer = False
    list_display = ("lot_name", "auction", "lot_number", "user", "species_category",)
    list_filter = ("active","auction",)
    search_fields = ("lot_number","lot_name","description","user")
    inlines = [
         BidInline,
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
    list_display = ("auction", "user", "total_sold", "total_bought", "__str__", "paid", )
    list_filter = ("auction", "paid",)
    search_fields = ("user",)
    inlines = [
         SoldLotInline,
         BoughtLotInline,
    ]

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
    search_fields = ("common_name", "scientific_name", )

admin.site.register(Category, CategoryAdmin)
admin.site.register(Product, ProductAdmin)
admin.site.register(Auction, AuctionAdmin)
admin.site.register(Invoice, InvoiceAdmin)
#admin.site.register(Bid, BidAdmin)
admin.site.register(Lot, LotAdmin)
admin.site.register(Location, LocationAdmin)
admin.site.register(Club, ClubAdmin)