import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import Auction, AuctionHistory, AuctionTOS, BapAward, ClubHistory, ClubMember, Lot


class AuctionTOSHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"
    show_on_mobile_string = ""
    # show_on_mobile_string = "d-sm-table-cell d-md-none"
    bidder_number = tables.Column(accessor="bidder_number", verbose_name="ID", orderable=True)
    # id = tables.Column(accessor='display_name_for_admins', verbose_name="ID", orderable=False)
    # phone = tables.Column(accessor='phone_as_string', verbose_name="Phone", orderable=False)
    invoice_link = tables.Column(
        accessor="invoice_link_html",
        verbose_name="Invoice",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    add_lot_link = tables.Column(
        accessor="bulk_add_link_html",
        verbose_name="Add lots",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    print_invoice_link = tables.Column(
        accessor="print_labels_html",
        verbose_name="Lot labels",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    # email = tables.Column(attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}})
    actions = tables.Column(
        accessor="actions_dropdown_html",
        orderable=False,
        attrs={"th": {"class": show_on_mobile_string}, "cell": {"class": show_on_mobile_string}},
    )

    def render_bidder_number(self, value, record):
        if record.bidder_number == "ERROR":
            return mark_safe(
                '<span class="badge bg-danger ms-1 me-1" title="Failed to generate a bidder number for this user">ERROR</span>'
            )
        return value

    def render_name(self, value, record):
        # as a button, looks awful
        # result = f"<span class='btn btn-secondary btn-sm' style='cursor:pointer;' hx-get='/api/auctiontos/{record.pk}' hx-target='#modals-here' hx-trigger='click'>{value}</span>"
        # as a link, looks better
        result = (
            f"<a href='' hx-noget hx-get='/api/auctiontos/{record.pk}' hx-target='#modals-here' hx-trigger='click'>"
        )
        if record.possible_duplicate:
            result += "<i class='text-warning bi bi-people-fill me-1' title='This user may be a duplicate'></i>"
        else:
            result += "<i class='bi bi-person-fill-gear me-1'></i>"
        result += f"{value}</a>"
        if record.is_admin or (record.user and record.auction.created_by.pk == record.user.pk):
            result += '<span class="badge bg-danger ms-1 me-1" title="Can add users and lot">Admin</span>'
        if record.is_club_member:
            label = record.auction.alternative_split_label.capitalize()
            result += (
                f'<span class="badge bg-info ms-1 me-1" title="Alternate selling fees will be applied">{label}</span>'
            )
        if not record.can_bid_in_auction and not (record.auction.use_check_in_mode and not record.checked_in):
            result += '<i class="text-danger bi bi-exclamation-octagon-fill" title="Bidding not allowed"></i>'
        if record.checked_in:
            result += '<span class="badge bg-success ms-1 me-1" title="Checked in">Checked in</span>'
        elif record.auction.use_check_in_mode:
            if self.can_manage_check_in:
                check_in_url = reverse("auction_check_in", kwargs={"pk": record.pk})
                result += (
                    f'<button class="btn btn-sm btn-success ms-1" hx-get="{check_in_url}" '
                    'hx-target="#modals-here" hx-swap="innerHTML" '
                    '_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    "Check in</button>"
                )
        if record.email_address_status == "BAD":
            result += "<i class='bi bi-envelope-exclamation-fill text-danger ms-1' title='Unable to send email to this address'></i>"
        if record.email_address_status == "VALID":
            result += "<i class='bi bi-envelope-check-fill ms-1' title='Verified email'></i>"
        # if not record.bidding_allowed:
        #     result += '<i class="text-danger ms-1 bi bi-cash-coin" title="Selling not allowed"></i>'
        # for mobile, put other columns in a dropdown menu:
        return mark_safe(result)

    # def render_email(self, value, record):
    #     """No longer used, but keeping it here for reference"""
    #     email_string = f'<a href="mailto:{value}">{value}</a>'
    #     # email_string = value
    #     if record.email_address_status == "BAD":
    #         email_string += "<i class='bi bi-envelope-exclamation-fill text-danger ms-1' title='Unable to send email to this address'></i>"
    #     if record.email_address_status == "VALID":
    #         email_string += "<i class='bi bi-envelope-check-fill ms-1' title='Verified email'></i>"
    #     return mark_safe(email_string)

    class Meta:
        model = AuctionTOS
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "bidder_number",
            "name",
            # "email",
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
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.can_manage_check_in = kwargs.pop("can_manage_check_in", False)
        super().__init__(*args, **kwargs)


class AuctionHistoryHTMxTable(tables.Table):
    name = tables.Column(
        accessor="user",
        verbose_name="User",
        default="System",
    )
    action = tables.Column(
        accessor="action",
        verbose_name="Action",
    )
    applies_to = tables.Column(
        accessor="applies_to",
        verbose_name="Modified",
    )
    timestamp = tables.Column(
        accessor="timestamp",
        verbose_name="Time",
    )

    def render_applies_to(self, value, record):
        if record.applies_to == "RULES":
            result = "<i class='bi bi-gear-fill'></i>"
        elif record.applies_to == "USERS":
            result = "<i class='bi bi-people-fill'></i>"
        elif record.applies_to == "INVOICES":
            result = "<i class='bi bi-bag'></i>"
        elif record.applies_to == "LOTS":
            result = "<i class='bi bi-calendar'></i>"
        elif record.applies_to == "LOT_WINNERS":
            result = "<i class='bi bi-calendar-check'></i>"
        else:
            result = ""
        result += f" {value}"
        return mark_safe(result)

    def render_name(self, value, record):
        if record.user:
            result = record.user.get_full_name()
        return mark_safe(result)

    class Meta:
        model = AuctionHistory
        template_name = "tables/bootstrap_htmx.html"

        fields = ()

    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop("auction")
        super().__init__(*args, **kwargs)


class LotHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"
    seller = tables.Column(
        accessor="auctiontos_seller",
        verbose_name="Seller",
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    winner = tables.Column(
        accessor="auctiontos_winner",
        verbose_name="Winner",
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    winning_price = tables.Column(
        accessor="winning_price",
        verbose_name="Price",
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    lot_number = tables.Column(accessor="lot_number_int", verbose_name="Lot number", orderable=True)

    def render_lot_name(self, value, record):
        result = f"""
        <a href='' hx-noget hx-get='/api/lot/{record.pk}' hx-target='#modals-here' hx-trigger='click'><i class='bi bi-calendar-fill me-1'></i>{value}</a>
        <button type="button" class="btn btn-sm bg-secondary dropdown-toggle dropdown-toggle-split" data-bs-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
				</button>
				<div class="dropdown-menu">
					<div><a href='{record.lot_link}?src=admin'><i class="bi bi-calendar ms-1 me-1"></i>Lot page</a></div>
		"""
        if not record.image_count:
            result += f"""<a href="{reverse("add_image", kwargs={"lot": record.pk})}?next={reverse("auction_lot_list", kwargs={"slug": record.auction.slug})}"<i class="bi bi-file-image ms-1 me-1"></i>Add image</a>"""
        result += f"""<div><a href='#' hx-get="{reverse("lot_refund", kwargs={"pk": record.pk})}",
                hx-target="#modals-here",
                hx-trigger="click",
                _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"><i class="bi bi-calendar-x ms-1 me-1"></i>Remove or refund</a></div>
                <div><a class="" href="{reverse("single_lot_label", kwargs={"pk": record.pk})}"><i class="bi bi-tag ms-1 me-1"></i>{"Reprint label" if record.label_printed else "Print label"}</a></div>
                <div><a href="{record.seller_invoice_link}"><i class="bi bi-bag-fill ms-1 me-1"></i>Seller's invoice</a></div>

        """
        if record.winner_invoice_link:
            result += f"""
            <div><a href="{record.winner_invoice_link}"><i class="bi bi-bag ms-1 me-1"></i>Winner's invoice</a></div>
			"""
        result += "</div>"
        if record.banned:
            result += '<span class="badge bg-danger">Removed</span>'
        # on mobile, reduce the number of columns and show info below the lot name
        result += f'<span class="d-block d-md-none"><b>Seller:</b> {record.auctiontos_seller} '
        if record.auctiontos_winner:
            result += f"<b>Winner:</b> {record.auctiontos_winner} (${record.winning_price})"
        result += "</span>"
        return mark_safe(result)

    def render_winning_price(self, value, record):
        return f"${value}"

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
    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop("auction")
        if self.auction and self.auction.use_seller_dash_lot_numbering:
            self.base_columns["lot_number"] = tables.Column(
                accessor="lot_number_display",
                verbose_name="Lot number",
                orderable=False,
            )
        super().__init__(*args, **kwargs)


class AuctionHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"

    auction = tables.Column(accessor="title", verbose_name="Auction")
    date = tables.Column(accessor="date_start", verbose_name="Starts")
    lots = tables.Column(
        accessor="template_lot_link_separate_column",
        verbose_name="Lots",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )

    # def render_date(self, value, record):
    #    localized_date = formats.date_format(record.template_date_timestamp, use_l10n=True)
    #    return mark_safe(f"{record.template_status}{localized_date}{record.ended_badge}")

    def render_auction(self, value, record):
        from auctions.templatetags.distance_filters import convert_distance

        auction = record
        result = f"<a href='{auction.get_absolute_url()}'>{auction.title}</a><br class='d-md-none'>"
        if auction.is_last_used:
            result += " <span class='ms-1 badge bg-success text-black'>Your last auction</span>"
        if auction.is_online and not auction.in_progress:
            result += " <span class='badge bg-primary'>Online</span>"
        if auction.in_progress or auction.in_person_in_progress:
            result += " <span class='badge bg-info'>Online bidding now!</span>"
        if auction.is_deleted:
            result += " <span class='badge bg-danger'>Deleted</span>"
        if not auction.promote_this_auction:
            result += " <span class='badge bg-dark'>Not promoted</span>"
        if auction.distance:
            # Use distance conversion filter
            user = self.request.user if self.request else None
            distance_result = convert_distance(auction.distance, user)
            if distance_result:
                distance_value, distance_unit = distance_result
                result += f" <span class='badge bg-primary'>{distance_value} {distance_unit} from you</span>"
        if auction.joined and not auction.is_last_used:
            result += " <span class='badge bg-success text-black'>Joined</span>"
        result += auction.template_lot_link_first_column + auction.template_promo_info
        return mark_safe(result)

    class Meta:
        model = Auction
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "auction",
            "date",
            "lots",
        )
        row_attrs = {
            # 'class': lambda record: str(record.table_class),
            # 'style':'cursor:pointer;',
            # 'hx-get': lambda record: "/api/lot/" + str(record.pk),
            # 'hx-target':"#modals-here",
            # 'hx-trigger':"click",
            # '_':"on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"
        }


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
        if record.banned:
            return mark_safe('<span class="badge bg-danger">Removed</span>')
        if record.deactivated:
            return mark_safe('<span class="badge bg-secondary">Deactivated</span>')
        if record.winner or record.auctiontos_winner:
            return mark_safe('<span class="badge bg-success text-dark">Sold</span>')
        if record.high_bidder:
            return mark_safe('<span class="badge bg-info text-dark">Bids</span>')
        if value:
            return mark_safe('<span class="badge bg-primary">Active</span>')
        else:
            return mark_safe('<span class="badge bg-secondary">Unsold</span>')

    def render_price(self, value, record):
        return f"${value}"

    def render_actions(self, value, record):
        result = ""
        if not record.image_count:
            result += f' <a href="{reverse("add_image", kwargs={"lot": record.pk})}" class="badge bg-primary"><i class="bi bi-file-image"></i> Add image</a>'
        if record.can_be_edited:
            result += f' <a href="{reverse("edit_lot", kwargs={"pk": record.pk})}" class="badge text-dark bg-warning"><i class="bi bi-calendar"></i> Edit</a>'
        result += f' <a href="{reverse("new_lot")}?copy={record.pk}" class="badge bg-info"><i class="bi bi-calendar-plus"></i> Copy to new lot</a>'
        if record.can_be_deleted:
            result += f' <a href="{reverse("delete_lot", kwargs={"pk": record.pk})}?next={reverse("selling")}" class="badge bg-danger"><i class="bi bi-trash"></i> Delete</a>'
        return mark_safe(result)

    def render_lot_name(self, value, record):
        result = f"<a href='{record.lot_link}?src=my_lots'>{value}"
        if record.owner_chats:
            result += f" <span style='color:black;font-weight:900' class='badge bg-warning'>{record.owner_chats}</span>"
        result += "</a>"
        if getattr(record, "show_bap_badge", False):
            try:
                award = record.bap_award
                parts = []
                if award.points:
                    parts.append(f"{award.points} BAP")
                if award.hap_points:
                    parts.append(f"{award.hap_points} HAP")
                if award.cap_points:
                    parts.append(f"{award.cap_points} CAP")
                pts = "/".join(parts) if parts else "0 pts"
                club_name = award.club_member.club.name if award.club_member_id and award.club_member.club_id else ""
                notes = award.notes or ""
                badge_parts = [pts]
                if club_name:
                    badge_parts.append(club_name)
                if notes:
                    badge_parts.append(notes)
                result += f' <span class="badge bg-success text-dark">{" · ".join(badge_parts)}</span>'
            except Exception:
                pass
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


_PERMISSION_BADGES = [
    ("permission_admin", "Admin"),
    ("permission_edit_club", "Edit club settings"),
    ("permission_manage_auctions", "Manage auctions"),
    ("permission_manage_bap", "Award points"),
    ("permission_export", "Export data"),
    ("permission_add_edit", "Manage membership"),
    ("permission_view", "View members"),
]


class ClubMemberHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"
    name = tables.Column(accessor="display_name", verbose_name="Name", orderable=False, empty_values=())
    bidder_number = tables.Column(accessor="bidder_number", verbose_name="Bidder", orderable=True)
    bap_points = tables.Column(
        accessor="bap_points",
        verbose_name="BAP",
        orderable=True,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    hap_points = tables.Column(
        accessor="hap_points",
        verbose_name="HAP",
        orderable=True,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    membership_last_paid = tables.Column(
        accessor="membership_last_paid",
        verbose_name="Last paid",
        orderable=True,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    membership_expiration_date = tables.Column(
        accessor="membership_expiration_date",
        verbose_name="Expires",
        orderable=True,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    createdon = tables.DateColumn(
        accessor="createdon",
        verbose_name="Joined",
        orderable=True,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    source = tables.Column(
        accessor="source",
        verbose_name="Source",
        orderable=True,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    actions = tables.Column(
        accessor="pk",
        verbose_name="",
        orderable=False,
    )

    def render_name(self, value, record):
        name = record.display_name
        url = reverse("clubmember_admin", kwargs={"pk": record.pk})
        if self.can_add_edit:
            if record.possible_duplicate_id:
                icon = format_html(
                    "<i class='text-warning bi bi-people-fill me-1' title='This member may be a duplicate'></i>"
                )
            else:
                icon = format_html("<i class='bi bi-person-fill-gear me-1'></i>")
        else:
            icon = format_html("<i class='bi bi-person-fill me-1'></i>")
        result = format_html(
            "<a href='' hx-get='{}' hx-target='#modals-here' hx-trigger='click'>{}{}</a>",
            url,
            icon,
            name,
        )
        if record.is_deleted:
            result += format_html(" <span class='badge bg-secondary'>Deactivated</span>")
        if record.email_address_status == "BAD":
            result += format_html(
                "<i class='bi bi-envelope-exclamation-fill text-danger ms-1' title='Unable to send email to this address'></i>"
            )
        if record.email_address_status == "VALID":
            result += format_html("<i class='bi bi-envelope-check-fill ms-1' title='Verified email'></i>")
        for field, label in _PERMISSION_BADGES:
            if getattr(record, field, False):
                result += format_html(
                    " <span class='badge bg-danger' title='{}'>{}</span>",
                    label,
                    label,
                )
                break
        return result

    def render_membership_expiration_date(self, value):
        from django.utils import timezone

        if not value:
            return "—"
        today = timezone.now().date()
        formatted = value.strftime("%b %-d, %Y")
        days_expired = (today - value).days
        if days_expired > 0:
            return format_html(
                "{} <span class='badge bg-danger ms-1'>{} day{} expired</span>",
                formatted,
                days_expired,
                "s" if days_expired != 1 else "",
            )
        return formatted

    def render_membership_last_paid(self, value):
        if not value:
            return "—"
        return value.strftime("%b %-d, %Y")

    _SOURCE_LABELS = {
        "discord": "Discord",
        "manually_added": "Manual",
        "csv": "CSV",
    }

    def render_source(self, value, record):
        if value == "joined":
            from django.conf import settings

            return getattr(settings, "NAVBAR_BRAND", "Website")
        return self._SOURCE_LABELS.get(value, value)

    def render_actions(self, value, record):
        if not self.can_add_edit and not self.can_manage_permissions:
            return ""
        name = record.display_name

        permissions_item = format_html("")
        if self.can_manage_permissions and not record.is_deleted:
            perms_url = reverse("clubmember_permissions", kwargs={"pk": record.pk})
            permissions_item = format_html(
                '<li><a class="dropdown-item" href="javascript:void(0)"'
                ' hx-get="{}" hx-target="#modals-here"'
                ' _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                '<i class="bi bi-shield-lock me-1"></i>Permissions</a></li>'
                "<li><hr class='dropdown-divider'></li>",
                perms_url,
            )

        edit_items = format_html("")
        if self.can_add_edit:
            if record.is_deleted:
                # Deactivated member: offer reactivate (no confirm) and permanent delete
                reactivate_url = reverse("club_member_reactivate", kwargs={"pk": record.pk})
                perm_delete_url = reverse("club_member_confirm", kwargs={"pk": record.pk, "action": "permanent_delete"})
                edit_items = format_html(
                    '<li><a class="dropdown-item" href="javascript:void(0)"'
                    ' hx-post="{}" hx-target="#modals-here" hx-swap="innerHTML">'
                    '<i class="bi bi-person-check me-1"></i>Reactivate</a></li>'
                    '<li><hr class="dropdown-divider"></li>'
                    '<li><a class="dropdown-item text-danger" href="javascript:void(0)"'
                    ' hx-get="{}" hx-target="#modals-here"'
                    ' _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    '<i class="bi bi-trash me-1"></i>Permanently delete</a></li>',
                    reactivate_url,
                    perm_delete_url,
                )
            else:
                renew_confirm_url = reverse("club_member_renew", kwargs={"pk": record.pk})
                set_expiry_url = reverse("club_member_renew_page", kwargs={"slug": record.club.slug, "pk": record.pk})
                confirm_delete_url = reverse("club_member_confirm", kwargs={"pk": record.pk, "action": "delete"})
                merge_url = reverse("club_member_merge", kwargs={"slug": record.club.slug, "pk": record.pk})
                email_item = format_html("")
                if record.email:
                    icon_class = "bi bi-envelope"
                    if record.email_address_status == "BAD":
                        icon_class = "bi bi-envelope-exclamation-fill text-danger"
                    elif record.email_address_status == "VALID":
                        icon_class = "bi bi-envelope-check-fill"
                    email_item = format_html(
                        '<li><a class="dropdown-item" href="mailto:{}"><i class="{} me-1"></i>Email</a></li>',
                        record.email,
                        icon_class,
                    )
                # Member-number action is hidden entirely when the club has the feature disabled.
                membership_number_item = format_html("")
                if record.club.membership_number_mode != "disabled":
                    membership_number_url = reverse("club_member_membership_number", kwargs={"pk": record.pk})
                    membership_number_item = format_html(
                        '<li><a class="dropdown-item" href="javascript:void(0)"'
                        ' hx-get="{}" hx-target="#modals-here"'
                        ' _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                        '<i class="bi bi-credit-card-2-front me-1"></i>Membership number</a></li>',
                        membership_number_url,
                    )
                edit_items = format_html(
                    '<li><a class="dropdown-item" href="javascript:void(0)"'
                    ' hx-get="{}" hx-target="#modals-here"'
                    ' _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    '<i class="bi bi-calendar-check me-1"></i>Renew</a></li>'
                    '<li><a class="dropdown-item" href="{}">'
                    '<i class="bi bi-calendar-range me-1"></i>Set expiration date</a></li>'
                    '<li><a class="dropdown-item" href="{}">'
                    '<i class="bi bi-people me-1"></i>Merge with...</a></li>'
                    "{}"
                    "{}"
                    '<li><hr class="dropdown-divider"></li>'
                    '<li><a class="dropdown-item" href="javascript:void(0)"'
                    ' hx-get="{}" hx-target="#modals-here"'
                    ' _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    '<i class="bi bi-person-dash me-1"></i>Deactivate</a></li>',
                    renew_confirm_url,
                    set_expiry_url,
                    merge_url,
                    membership_number_item,
                    email_item,
                    confirm_delete_url,
                )

        django_admin_item = format_html("")
        if self.request and getattr(self.request.user, "is_staff", False):
            admin_url = f"/admin/auctions/clubmember/{record.pk}/change/"
            django_admin_item = format_html(
                "<li><hr class='dropdown-divider'></li>"
                '<li><a class="dropdown-item" href="{}" target="_blank">'
                '<i class="bi bi-wrench me-1"></i>Django admin</a></li>',
                admin_url,
            )

        return format_html(
            '<div class="dropdown">'
            '<button type="button" class="btn btn-sm btn-secondary dropdown-toggle"'
            ' data-bs-toggle="dropdown" aria-label="Actions for {}">Actions</button>'
            "<ul class='dropdown-menu'>{}{}{}</ul>"
            "</div>",
            name,
            permissions_item,
            edit_items,
            django_admin_item,
        )

    class Meta:
        model = ClubMember
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "name",
            "bidder_number",
            "bap_points",
            "hap_points",
            "membership_last_paid",
            "membership_expiration_date",
            "createdon",
            "source",
            "actions",
        )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.can_add_edit = kwargs.pop("can_add_edit", False)
        self.can_manage_permissions = kwargs.pop("can_manage_permissions", False)
        can_manage_bap = kwargs.pop("can_manage_bap", False)
        can_manage_membership = kwargs.pop("can_manage_membership", False)
        can_manage_auctions = kwargs.pop("can_manage_auctions", False)
        exclude = list(kwargs.pop("exclude", None) or [])
        if not can_manage_bap:
            exclude += ["bap_points", "hap_points"]
        if not can_manage_membership:
            exclude += ["membership_last_paid", "membership_expiration_date"]
        if not can_manage_auctions:
            exclude += ["bidder_number"]
        super().__init__(*args, exclude=exclude, **kwargs)


class ClubHistoryHTMxTable(tables.Table):
    name = tables.Column(accessor="user", verbose_name="User", default="System")
    action = tables.Column(accessor="action", verbose_name="Action")
    applies_to = tables.Column(accessor="applies_to", verbose_name="Modified")
    timestamp = tables.Column(accessor="timestamp", verbose_name="Time")

    def render_applies_to(self, value, record):
        if record.applies_to == "RULES":
            result = "<i class='bi bi-gear-fill'></i>"
        elif record.applies_to == "MEMBERS":
            result = "<i class='bi bi-people-fill'></i>"
        elif record.applies_to == "SETTINGS":
            result = "<i class='bi bi-sliders'></i>"
        else:
            result = ""
        result += f" {value}"
        return mark_safe(result)

    def render_name(self, value, record):
        if record.user:
            name = self._member_names.get(record.user_id)
            if name:
                return name
            return record.user.get_full_name() or record.user.username
        return "System"

    class Meta:
        model = ClubHistory
        template_name = "tables/bootstrap_htmx.html"
        fields = ()

    def __init__(self, *args, **kwargs):
        self.club = kwargs.pop("club", None)
        if self.club:
            self._member_names = {
                m.user_id: m.name or m.email or str(m)
                for m in ClubMember.objects.filter(club=self.club, is_deleted=False).exclude(user=None)
            }
        else:
            self._member_names = {}
        super().__init__(*args, **kwargs)


class BapAwardHTMxTable(tables.Table):
    """Table of BapAward records for the club BAP awards tab."""

    hide_string = "d-md-table-cell d-none"

    member = tables.Column(accessor="club_member", verbose_name="Member", orderable=True)
    date = tables.Column(verbose_name="Date", orderable=True)
    points = tables.Column(verbose_name="BAP", orderable=True)
    hap_points = tables.Column(verbose_name="HAP", orderable=True)
    cap_points = tables.Column(verbose_name="CAP", orderable=True)
    lot_name = tables.Column(accessor="lot", verbose_name="Lot", orderable=False)
    notes = tables.Column(
        verbose_name="Notes",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )

    _MODAL_ATTRS = (
        'hx-target="#modals-here" hx-trigger="click" '
        '_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop"'
    )

    def _edit_link(self, record, content):
        url = reverse("bapaward_admin", kwargs={"pk": record.pk})
        return format_html(
            '<a hx-get="{}" {} class="text-info" style="cursor:pointer;text-decoration:underline">{}</a>',
            url,
            mark_safe(self._MODAL_ATTRS),
            content,
        )

    def render_member(self, value, record):
        return self._edit_link(record, str(value))

    def render_date(self, value, record):
        return self._edit_link(record, value.strftime("%b %-d, %Y"))

    def render_lot_name(self, value, record):
        if value:
            return format_html('<a href="{}" target="_blank">{}</a>', value.lot_link, value.lot_name)
        return "—"

    def render_notes(self, value, record):
        if not value:
            return "—"
        if len(value) > 60:
            return format_html('<span title="{}">{}&hellip;</span>', value, value[:60])
        return value

    class Meta:
        model = BapAward
        template_name = "tables/bootstrap_htmx.html"
        fields = ("member", "date", "points", "hap_points", "cap_points", "lot_name", "notes")

    def __init__(self, *args, **kwargs):
        self.club = kwargs.pop("club", None)
        super().__init__(*args, **kwargs)
        if self.club:
            if not self.club.separate_hap:
                self.columns.hide("hap_points")
            if not self.club.separate_cap:
                self.columns.hide("cap_points")


class ClubBapLotHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"

    lot_name = tables.Column(verbose_name="Lot", orderable=True)
    seller_name = tables.Column(accessor="auctiontos_seller", verbose_name="Seller", orderable=False)
    quantity = tables.Column(verbose_name="Qty", orderable=True)
    date_end = tables.Column(
        verbose_name="Ended", orderable=True, attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}}
    )
    bap_reason = tables.Column(accessor="bap_auto_reason", verbose_name="Reason", orderable=False)
    actions = tables.Column(empty_values=(), verbose_name="Actions", orderable=False)

    def render_lot_name(self, value, record):
        return value

    def render_seller_name(self, value, record):
        return value.name if value else "—"

    def render_bap_reason(self, value, record):
        reason = value or record.unsold_lot_no_bap_reason
        if not reason:
            return ""
        return dict(Lot.BAP_REASON_CHOICES).get(reason, reason)

    def render_date_end(self, value, record):
        return value.strftime("%b %-d, %Y") if value else "—"

    def render_actions(self, record):
        from django.template.loader import render_to_string

        try:
            award = record.bap_award
        except Exception:
            award = None
        record.bap_award_cached = award
        default_points = self.club.points_per_lot if self.club and self.club.points_per_lot > 0 else 5
        return mark_safe(
            render_to_string(
                "auctions/bap_lot_buttons.html",
                {"lot": record, "club": self.club, "default_points": default_points},
            )
        )

    class Meta:
        model = Lot
        template_name = "tables/bootstrap_htmx.html"
        fields = ("lot_name", "seller_name", "quantity", "date_end", "bap_reason", "actions")

    def __init__(self, *args, **kwargs):
        self.club = kwargs.pop("club", None)
        super().__init__(*args, **kwargs)
