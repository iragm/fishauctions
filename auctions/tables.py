from django.utils.safestring import mark_safe
import django_tables2 as tables
from .models import *

class AuctionTOSHTMxTable(tables.Table):
    id = tables.Column(accessor='display_name', verbose_name="ID", orderable=False)
    #phone = tables.Column(accessor='phone_as_string', verbose_name="Phone", orderable=False)
    invoice_link = tables.Column(accessor='invoice_link_html', verbose_name="Invoice", orderable=False)
    add_lot_link = tables.Column(accessor='bulk_add_link_html', verbose_name="Add lots", orderable=False)
    print_invoice_link = tables.Column(accessor='print_invoice_link_html', verbose_name="Lot labels", orderable=False)

    class Meta:
        model = AuctionTOS
        template_name = "tables/bootstrap_htmx.html"
        fields = ('id', 'name', 'email', 'print_invoice_link', 'add_lot_link','invoice_link')
        row_attrs = {
            'style':'cursor:pointer;',
            'hx-get': lambda record: "/api/auctiontos/" + str(record.pk),
            'hx-target':"#modals-here",
	        'hx-trigger':"click",
            #'_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
        }


class LotHTMxTable(tables.Table):
    seller = tables.Column(accessor='auctiontos_seller', verbose_name="Seller")
    winner = tables.Column(accessor='auctiontos_winner', verbose_name="Winner")
    winning_price = tables.Column(accessor='winning_price', verbose_name="Price")
    lot_number = tables.Column(accessor='lot_number_display', verbose_name="Lot number", orderable=False)

    class Meta:
        model = Lot
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            'lot_number',
            'lot_name',
            'seller',
            'winner',
            'winning_price',
            )
        row_attrs = {
            'class': lambda record: str(record.table_class),
            'style':'cursor:pointer;',
            'hx-get': lambda record: "/api/lot/" + str(record.pk),
            'hx-target':"#modals-here",
	        'hx-trigger':"click",
            '_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
        }

class LotHTMxTableForUsers(tables.Table):
    hide_string = "d-md-table-cell d-none"
    #seller = tables.Column(accessor='auctiontos_seller', verbose_name="Seller")
    #winner = tables.Column(accessor='auctiontos_winner', verbose_name="Winner")
    #winning_price = tables.Column(accessor='winning_price', verbose_name="Price")
    lot_number = tables.Column(accessor='lot_number_display', verbose_name="Lot number", orderable=False, attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}})
    #lot_number = tables.Column(accessor='lot_number_display', verbose_name="Lot number", orderable=False)
    active = tables.Column(accessor='active', verbose_name="Status")
    price = tables.Column(accessor='price', verbose_name="Price")
    views = tables.Column(accessor='page_views', verbose_name="Views", orderable=False, attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}})
    #bids = tables.Column(accessor='number_of_bids', verbose_name="Bids")
    #chats = tables.Column(accessor='all_chats', verbose_name="Messages")
    actions = tables.Column(accessor='all_chats', verbose_name="Actions")
    auction = tables.Column(attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}})

    def render_active(self, value, record):
        if value:
            return mark_safe('<span class="badge bg-primary">Active</span>')
        else:
            if record.banned:
                return mark_safe('<span class="badge bg-danger">Removed</span>')
            elif record.deactivated:
                return mark_safe('<span class="badge bg-secondary">Deactivated</span>')
            if record.winner or record.auctiontos_winner:
                return mark_safe('<span class="badge bg-success text-dark">Sold</span>')
            return mark_safe('<span class="badge bg-secondary">Unsold</span>')
        
    def render_price(self, value, record):
        return f"${value}.00"
    
    def render_actions(self, value, record):
        result = ""
        if not record.image_count:
            result += f' <a href="{reverse("add_image", kwargs={"lot": record.pk})}" class="badge badge-primary">Add image</a>'
        if record.can_be_edited:
            result += f' <a href="{reverse("edit_lot", kwargs={"pk": record.pk})}" class="badge badge-warning">Edit</a>'
        result += f' <a href="{reverse("new_lot")}?copy={record.pk}" class="badge badge-info text-dark">Copy to new lot</a>'
        return mark_safe(result)

    def render_lot_name(self, value, record):
        result = f"<a href='{record.lot_link}?src=my_lots'>{value}"
        if record.owner_chats:
            result += f" <span style='color:black;font-weight:900' class='badge bg-warning'>{record.owner_chats}</span>"
        result += '</a>'
        return mark_safe(result)

    def render_auction(self, value, record):
        return mark_safe(f"<small>{value}</small>")


    class Meta:
        model = Lot
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            'active',
            'lot_number',
            'lot_name',
            'price',
            'auction',
            'views',
            )
        row_attrs = {
            #'class': lambda record: str(record.table_class),
            #'style':'cursor:pointer;',
            #'hx-get': lambda record: "/api/lot/" + str(record.pk),
            #'hx-target':"#modals-here",
	        #'hx-trigger':"click",
            #'_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
        }