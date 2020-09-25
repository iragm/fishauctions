from django.contrib import admin
from .models import Lot, Bid, Auction, Invoice

class AuctionAdmin(admin.ModelAdmin):
    model = Auction 
    menu_label = "Auctions"  
    menu_icon = "plus-inverse"
    menu_order = 200 
    add_to_settings_menu = False
    exclude_from_explorer = False
    list_display = ("title",)
    list_filter = ("title",)
    search_fields = ("title",)
    
class LotAdmin(admin.ModelAdmin):
    model = Lot 
    menu_label = "Lots"  
    menu_icon = "tag"
    menu_order = 300
    add_to_settings_menu = False
    exclude_from_explorer = False
    list_display = ("auction", "lot_number", "user", "lot_name", "description",)
    list_filter = ("user","category")
    search_fields = ("lot_number","lot_name","description","user")

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

class BoughtLotInline(admin.TabularInline):
     fields = ['__str__', 'winning_price']
     readonly_fields = ['__str__','winning_price']
     verbose_name = "Lot bought"
     verbose_name_plural = "Lots bought"
     fk_name = 'buyer_invoice'
     model = Lot

class InvoiceAdmin(admin.ModelAdmin):
    model = Invoice 
    list_display = ("auction", "user", "total_sold", "total_bought", "__str__", )
    list_filter = ("auction", "user","paid",)
    search_fields = ("user",)
    inlines = [
         SoldLotInline,
         BoughtLotInline,
    ]


admin.site.register(Auction, AuctionAdmin)
admin.site.register(Invoice, InvoiceAdmin)
admin.site.register(Bid, BidAdmin)
admin.site.register(Lot, LotAdmin)