#from wagtail.contrib.modeladmin.options import ModelAdmin, modeladmin_register 
from .models import Lot, Bid, Auction, Invoice


# class AuctionAdmin(ModelAdmin):
#     model = Auction 
#     menu_label = "Auctions"  
#     menu_icon = "plus-inverse"
#     menu_order = 200 
#     add_to_settings_menu = False
#     exclude_from_explorer = False
#     list_display = ("title",)
#     list_filter = ("title",)
#     search_fields = ("title",)
    
# class LotAdmin(ModelAdmin):
#     model = Lot 
#     menu_label = "Lots"  
#     menu_icon = "tag"
#     menu_order = 300
#     add_to_settings_menu = False
#     exclude_from_explorer = False
#     list_display = ("auction", "lot_number", "user", "lot_name", "description",)
#     list_filter = ("user","category")
#     search_fields = ("lot_number","lot_name","description","user")

# class BidAdmin(ModelAdmin):
#     model = Bid 
#     menu_label = "Bids"  
#     menu_icon = "bold"
#     menu_order = 400
#     add_to_settings_menu = False
#     exclude_from_explorer = False
#     list_display = ("user", "lot_number", "bid_time", "amount", )
#     list_filter = ("user","lot_number",)
#     search_fields = ("lot_number","user",)

# # class LotInline(admin.TabularInline):
# #     model = Lot

# class InvoiceAdmin(ModelAdmin):
#     model = Invoice 
#     menu_label = "Invoices"  
#     menu_icon = "download"
#     menu_order = 500
#     add_to_settings_menu = False
#     exclude_from_explorer = False
#     list_display = ("auction", "user", "total_sold", "total_bought", "__str__", )
#     list_filter = ("auction", "user","paid",)
#     search_fields = ("user",)
#     # inlines = [
#     #     LotInline,
#     # ]



#class AuthorAdmin(admin.ModelAdmin):


# modeladmin_register(AuctionAdmin)
# modeladmin_register(InvoiceAdmin)
# modeladmin_register(BidAdmin)
# modeladmin_register(LotAdmin)
# #modeladmin_register(Author, AuthorAdmin)