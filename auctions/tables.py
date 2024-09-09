import django_tables2 as tables
from django.urls import reverse
from django.utils.safestring import mark_safe

from .models import AuctionTOS, Lot


class AuctionTOSHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"
    bidder_number = tables.Column(accessor="bidder_number", verbose_name="ID", orderable=True)
    # id = tables.Column(accessor='display_name_for_admins', verbose_name="ID", orderable=False)
    # phone = tables.Column(accessor='phone_as_string', verbose_name="Phone", orderable=False)
    invoice_link = tables.Column(accessor="invoice_link_html", verbose_name="Invoice", orderable=False)
    add_lot_link = tables.Column(accessor="bulk_add_link_html", verbose_name="Add lots", orderable=False)
    print_invoice_link = tables.Column(accessor="print_invoice_link_html", verbose_name="Lot labels", orderable=False)
    email = tables.Column(attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}})

    def render_name(self, value, record):
        # as a button, looks awful
        # result = f"<span class='btn btn-secondary btn-sm' style='cursor:pointer;' hx-get='/api/auctiontos/{record.pk}' hx-target='#modals-here' hx-trigger='click'>{value}</span>"
        # as a link, looks better
        result = f"<a href='' hx-noget hx-get='/api/auctiontos/{record.pk}' hx-target='#modals-here' hx-trigger='click'><i class='bi bi-person-fill-gear me-1'></i>{value}</a>"
        if record.is_admin or (record.user and record.auction.created_by.pk == record.user.pk):
            result += '<span class="badge bg-danger ms-1 me-1" title="Can add users and lot">Admin</span>'
        if record.is_club_member:
            result += (
                '<span class="badge bg-info ms-1 me-1" title="Alternate selling fees will be applied">Member</span>'
            )
        if not record.bidding_allowed:
            result += '<i class="text-danger bi bi-exclamation-octagon-fill" title="Bidding not allowed"></i>'
        # if not record.bidding_allowed:
        #     result += '<i class="text-danger ms-1 bi bi-cash-coin" title="Selling not allowed"></i>'
        return mark_safe(result)

    def render_email(self, value, record):
        email_string = f'<a href="mailto:{value}">{value}</a>'
        # email_string = value
        if record.email_address_status == "BAD":
            email_string += "<i class='bi bi-envelope-exclamation-fill text-danger ms-1' title='Unable to send email to this address'></i>"
        if record.email_address_status == "VALID":
            email_string += "<i class='bi bi-envelope-check-fill ms-1' title='Verified email'></i>"
        return mark_safe(email_string)

    class Meta:
        model = AuctionTOS
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "bidder_number",
            "name",
            "email",
            "print_invoice_link",
            "add_lot_link",
            "invoice_link",
        )
        # row_attrs = {
        #     'style':'cursor:pointer;',
        #     'hx-get': lambda record: "/api/auctiontos/" + str(record.pk),
        #     'hx-target':"#modals-here",

    #     'hx-trigger':"click",
    #     #'_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
    # }


class LotHTMxTable(tables.Table):
    seller = tables.Column(accessor="auctiontos_seller", verbose_name="Seller")
    winner = tables.Column(accessor="auctiontos_winner", verbose_name="Winner")
    winning_price = tables.Column(accessor="winning_price", verbose_name="Price")
    lot_number = tables.Column(accessor="lot_number_display", verbose_name="Lot number", orderable=False)

    def render_lot_name(self, value, record):
        result = f"""
        <a href='' hx-noget hx-get='/api/lot/{record.pk}' hx-target='#modals-here' hx-trigger='click'><i class='bi bi-calendar-fill me-1'></i>{value}</a>
        <button type="button" class="btn btn-sm bg-secondary dropdown-toggle dropdown-toggle-split" data-bs-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
				</button>
				<div class="dropdown-menu">
					<div><a href='{record.lot_link}?src=admin'><i class="bi bi-calendar ms-1 me-1"></i>Lot page</a></div>
		"""
        if not record.image_count:
            result += f"""<a href="{reverse("add_image", kwargs={"lot": record.pk})}?next={reverse("auction_lot_list", kwargs={"slug":record.auction.slug})}"<i class="bi bi-file-image ms-1 me-1"></i>Add image</a>"""
        result += f"""<div><a href='#' hx-get="{reverse('lot_refund', kwargs={'pk':record.pk})}",
                hx-target="#modals-here",
                hx-trigger="click",
                _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"><i class="bi bi-calendar-x ms-1 me-1"></i>Remove or refund</a></div>
                <div><a class="" href="{reverse("single_lot_label", kwargs={"pk": record.pk})}"><i class="bi bi-tag ms-1 me-1"></i>{"Reprint label" if record.label_printed else "Print label"}</a></div>
                <div><a href="{ record.seller_invoice_link }"><i class="bi bi-bag-fill ms-1 me-1"></i>Seller's invoice</a></div>

        """
        if record.winner_invoice_link:
            result += f"""
            <div><a href="{ record.winner_invoice_link }"><i class="bi bi-bag ms-1 me-1"></i>Winner's invoice</a></div>
			"""
        result += "</div>"
        if record.banned:
            result += '<span class="badge bg-danger">Removed</span>'
        return mark_safe(result)

    class Meta:
        model = Lot
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "lot_number",
            "lot_name",
            "seller",
            "winner",
            "winning_price",
        )
        # row_attrs = {
        #     'class': lambda record: str(record.table_class),
        #     'style':'cursor:pointer;',
        #     'hx-get': lambda record: "/api/lot/" + str(record.pk),
        #     'hx-target':"#modals-here",

    #     'hx-trigger':"click",
    #     '_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
    # }


class LotHTMxTableForUsers(tables.Table):
    hide_string = "d-md-table-cell d-none"
    # seller = tables.Column(accessor='auctiontos_seller', verbose_name="Seller")
    # winner = tables.Column(accessor='auctiontos_winner', verbose_name="Winner")
    # winning_price = tables.Column(accessor='winning_price', verbose_name="Price")
    lot_number = tables.Column(
        accessor="lot_number_display",
        verbose_name="Lot number",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    # lot_number = tables.Column(accessor='lot_number_display', verbose_name="Lot number", orderable=False)
    active = tables.Column(accessor="active", verbose_name="Status")
    price = tables.Column(accessor="high_bid", verbose_name="Price", orderable=False)
    views = tables.Column(
        accessor="page_views",
        verbose_name="Views",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    # bids = tables.Column(accessor='number_of_bids', verbose_name="Bids")
    # chats = tables.Column(accessor='all_chats', verbose_name="Messages")
    actions = tables.Column(accessor="all_chats", verbose_name="Actions")
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
            result += f' <a href="{reverse("add_image", kwargs={"lot": record.pk})}" class="badge bg-primary"><i class="bi bi-file-image"></i> Add image</a>'
        if record.can_be_edited:
            result += f' <a href="{reverse("edit_lot", kwargs={"pk": record.pk})}" class="badge text-dark bg-warning"><i class="bi bi-calendar"></i> Edit</a>'
        result += f' <a href="{reverse("new_lot")}?copy={record.pk}" class="badge bg-info"><i class="bi bi-calendar-plus"></i> Copy to new lot</a>'
        return mark_safe(result)

    def render_lot_name(self, value, record):
        result = f"<a href='{record.lot_link}?src=my_lots'>{value}"
        if record.owner_chats:
            result += f" <span style='color:black;font-weight:900' class='badge bg-warning'>{record.owner_chats}</span>"
        result += "</a>"
        return mark_safe(result)

    def render_auction(self, value, record):
        return mark_safe(f"<small>{value}</small>")

    class Meta:
        model = Lot
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "active",
            "lot_number",
            "lot_name",
            "price",
            "auction",
            "views",
        )
        row_attrs = {
            # 'class': lambda record: str(record.table_class),
            # 'style':'cursor:pointer;',
            # 'hx-get': lambda record: "/api/lot/" + str(record.pk),
            # 'hx-target':"#modals-here",
            # 'hx-trigger':"click",
            # '_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
        }
