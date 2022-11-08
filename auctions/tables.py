import django_tables2 as tables
from .models import *

class AuctionTOSHTMxTable(tables.Table):
    id = tables.Column(accessor='display_name', verbose_name="ID")
    phone = tables.Column(accessor='phone_as_string', verbose_name="Phone")
    invoice_link = tables.Column(accessor='invoice_link_html', verbose_name="Invoice")
    add_lot_link = tables.Column(accessor='bulk_add_link_html', verbose_name="Add lots")
    print_invoice_link = tables.Column(accessor='print_invoice_link_html', verbose_name="Lot labels")

    class Meta:
        model = AuctionTOS
        template_name = "tables/bootstrap_htmx.html"
        fields = ('id', 'name', 'email', 'phone', 'print_invoice_link', 'add_lot_link','invoice_link')
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
    lot_number = tables.Column(accessor='lot_number_display', verbose_name="Lot number")

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