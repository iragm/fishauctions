"""The ``django_tables2`` tables behind every list on the site.

Each pairs with a filter in :mod:`auctions.filters` and renders through
``views.base.HTMxTableView``. HTML columns use ``format_html``: lot names are public input.
"""

import re
from urllib.parse import urlencode

import django_tables2 as tables
from django.contrib.humanize.templatetags.humanize import naturalday
from django.db.models import F
from django.urls import reverse
from django.utils.html import format_html, format_html_join, strip_tags
from django.utils.safestring import mark_safe

from . import donations
from .helper_functions import static_html
from .models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    BapAward,
    Club,
    ClubBapCategoryOverride,
    ClubBapGenusOverride,
    ClubHistory,
    ClubMember,
    DonationVendor,
    Invoice,
    Lot,
    Speaker,
)


class AuctionTOSHTMxTable(tables.Table):
    hide_string = "d-md-table-cell d-none"
    show_on_mobile_string = ""
    # show_on_mobile_string = "d-sm-table-cell d-md-none"
    bidder_number = tables.Column(accessor="bidder_number", verbose_name="ID", orderable=True)
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
    membership = tables.Column(
        accessor="pk",
        verbose_name="Membership",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    actions = tables.Column(
        accessor="actions_dropdown_html",
        orderable=False,
        attrs={"th": {"class": show_on_mobile_string}, "cell": {"class": show_on_mobile_string}},
    )

    def render_membership(self, value, record):
        """Expiration, Expired badge and Renew button for club-managed auctions, like ClubMemberHTMxTable."""
        from django.utils import timezone

        cm = record.clubmember
        if not cm:
            return "—"
        today = timezone.localdate()
        has_fee = bool(cm.club.membership_annual_fee)
        renew_btn = static_html("")
        if has_fee and not cm.is_deleted:
            renew_url = reverse("club_member_renew", kwargs={"pk": cm.pk})
            renew_btn = format_html(
                " <a href='javascript:void(0)' hx-get='{}' hx-target='#modals-here'"
                " class='btn btn-sm btn-primary py-0 px-1'>Renew</a>",
                renew_url,
            )
        expires = cm.membership_expiration_date
        if not expires:
            if has_fee and not cm.is_deleted:
                badge = static_html(" <span class='badge bg-danger ms-1'>Expired</span>")
                return format_html("—{}{}", badge, renew_btn)
            return static_html("—")
        formatted = expires.strftime("%b %-d, %Y")
        days_expired = (today - expires).days
        if days_expired > 0:
            return format_html(
                "{} <span class='badge bg-danger ms-1'>{} day{} expired</span>{}",
                formatted,
                days_expired,
                "s" if days_expired != 1 else "",
                renew_btn,
            )
        days_until = (expires - today).days
        if days_until <= 30:
            return format_html("{}{}", formatted, renew_btn)
        return format_html("{}", formatted)

    def render_bidder_number(self, value, record):
        if record.bidder_number == "ERROR":
            return mark_safe(
                '<span class="badge bg-danger ms-1 me-1" title="Failed to generate a bidder number for this user">ERROR</span>'
            )
        return value

    def is_club_auction_admin(self, record):
        """Whether this row administers the auction through club permissions (Auction.permission_check).

        The clubmember is already loaded by the membership column, only present in this mode.
        """
        if not self.is_managed:
            return False
        club_member = record.clubmember
        if not club_member or club_member.is_deleted:
            return False
        return bool(club_member.permission_admin or club_member.permission_manage_auctions)

    def render_name(self, value, record):
        # django_tables2 hands render_*() the raw accessor value, and mark_safe() below turns the
        # whole cell into markup -- so everything a person typed goes through format_html().
        icon = (
            static_html("<i class='text-warning bi bi-people-fill me-1' title='This user may be a duplicate'></i>")
            if record.possible_duplicate
            else static_html("<i class='bi bi-person-fill-gear me-1'></i>")
        )
        result = format_html(
            "<a href='' hx-noget hx-get='/api/auctiontos/{}' hx-target='#modals-here' hx-trigger='click'>{}{}</a>",
            record.pk,
            icon,
            value,
        )
        # created_by is nullable; compare ids to avoid fetching users per row.
        if (
            record.is_admin
            or (record.user_id and record.auction.created_by_id == record.user_id)
            or self.is_club_auction_admin(record)
        ):
            result += static_html('<span class="badge bg-danger ms-1 me-1" title="Can add users and lot">Admin</span>')
        # Different colours for the badge (a fact) and the Check in button (an action).
        if record.is_club_member:
            result += format_html(
                '<span class="badge bg-info ms-1 me-1" title="Alternate selling fees will be applied">{}</span>',
                record.auction.alternative_split_label.capitalize(),
            )
        if not record.can_bid_in_auction and not (record.auction.use_check_in_mode and not record.checked_in):
            result += static_html(
                '<i class="text-danger bi bi-exclamation-octagon-fill" title="Bidding not allowed"></i>'
            )
        if record.checked_in:
            result += static_html('<i class="bi bi-check-circle-fill text-success ms-1" title="Checked in"></i>')
        elif record.auction.use_check_in_mode:
            if self.can_manage_check_in:
                check_in_url = reverse("auction_check_in", kwargs={"pk": record.pk})
                result += format_html(
                    '<button class="btn btn-sm btn-primary ms-1" hx-get="{}" '
                    'hx-target="#modals-here" hx-swap="innerHTML" '
                    '_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    "Check in</button>",
                    check_in_url,
                )
        if record.email_address_status == "BAD":
            result += static_html(
                "<i class='bi bi-envelope-exclamation-fill text-danger ms-1'"
                " title='Unable to send email to this address'></i>"
            )
        if record.email_address_status == "VALID":
            result += static_html("<i class='bi bi-envelope-check-fill ms-1' title='Verified email'></i>")
        return result

    class Meta:
        model = AuctionTOS
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "bidder_number",
            "name",
            # "email",
            "membership",
            "print_invoice_link",
            "add_lot_link",
            "invoice_link",
        )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.can_manage_check_in = kwargs.pop("can_manage_check_in", False)
        self.is_managed = is_managed = kwargs.pop("is_managed", False)
        exclude = list(kwargs.pop("exclude", None) or [])
        if not is_managed:
            exclude.append("membership")
        super().__init__(*args, exclude=exclude, **kwargs)


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
        elif record.applies_to == "STATS":
            result = "<i class='bi bi-graph-up'></i>"
        else:
            result = ""
        return format_html("{} {}", mark_safe(result), value)  # noqa: S308 - result is one of the literals above

    def render_name(self, value, record):
        # A person's own first/last name. django_tables2 skips this for an empty accessor, so
        # record.user was never None here -- but returning the escaped name says so outright.
        if not record.user:
            return "System"
        return record.user.get_full_name()

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
        # Lot names and people's names are public input: every one goes through format_html.
        result = format_html(
            "<a href='' hx-noget hx-get='/api/lot/{}' hx-target='#modals-here' hx-trigger='click'>"
            "<i class='bi bi-calendar-fill me-1'></i>{}</a>"
            '<button type="button" class="btn btn-sm btn-primary dropdown-toggle dropdown-toggle-split"'
            ' data-bs-toggle="dropdown" aria-haspopup="true" aria-expanded="false"></button>'
            '<div class="dropdown-menu">'
            "<div><a href='{}?src=admin'><i class=\"bi bi-calendar ms-1 me-1\"></i>Lot page</a></div>",
            record.pk,
            value,
            record.lot_link,
        )
        if not record.image_count:
            result += format_html(
                '<a href="{}?next={}"><i class="bi bi-file-image ms-1 me-1"></i>Add image</a>',
                reverse("add_image", kwargs={"lot": record.pk}),
                reverse("auction_lot_list", kwargs={"slug": record.auction.slug}),
            )
        result += format_html(
            '<div><a href=\'#\' hx-get="{}" hx-target="#modals-here" hx-trigger="click"'
            ' _="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
            '<i class="bi bi-calendar-x ms-1 me-1"></i>Remove or refund</a></div>'
            '<div><a href="{}"><i class="bi bi-tag ms-1 me-1"></i>{}</a></div>'
            '<div><a href="{}"><i class="bi bi-bag-fill ms-1 me-1"></i>Seller\'s invoice</a></div>',
            reverse("lot_refund", kwargs={"pk": record.pk}),
            reverse("single_lot_label", kwargs={"pk": record.pk}),
            "Reprint label" if record.label_printed else "Print label",
            record.seller_invoice_link,
        )
        if self.auction and not self.auction.is_online:
            result += format_html(
                # Confirmed: the set-winners page has an undo, and this list doesn't.
                '<div><a href=\'#\' hx-post="{}" hx-target="#modals-here" hx-trigger="click"'
                ' hx-confirm="End lot {} unsold?">'
                '<i class="bi bi-slash-circle ms-1 me-1"></i>End lot unsold</a></div>',
                reverse("lot_end_unsold", kwargs={"pk": record.pk}),
                record.lot_number_display,
            )
        if record.winner_invoice_link:
            result += format_html(
                '<div><a href="{}"><i class="bi bi-bag ms-1 me-1"></i>Winner\'s invoice</a></div>',
                record.winner_invoice_link,
            )
        result += static_html("</div>")
        if record.banned:
            result += static_html('<span class="badge bg-danger">Removed</span>')
        if record.ended_unsold:
            result += static_html('<span class="badge bg-secondary">Ended unsold</span>')
        # On mobile, show info below the lot name.
        result += format_html('<span class="d-block d-md-none"><b>Seller:</b> {} ', record.auctiontos_seller)
        if record.auctiontos_winner:
            result += format_html("<b>Winner:</b> {} (${})", record.auctiontos_winner, record.winning_price)
        result += static_html("</span>")
        return result

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

    def render_auction(self, value, record):
        from auctions.templatetags.distance_filters import convert_distance

        auction = record
        # auction.title is public input.
        result = format_html("<a href='{}'>{}</a><br class='d-md-none'>", auction.get_absolute_url(), auction.title)
        if auction.is_last_used:
            result += static_html(" <span class='ms-1 badge bg-success text-dark'>Your last auction</span>")
        if auction.is_online and not auction.in_progress:
            result += static_html(" <span class='badge bg-primary'>Online</span>")
        if auction.in_progress or auction.in_person_in_progress:
            result += static_html(" <span class='badge bg-info'>Online bidding now!</span>")
        if auction.is_deleted:
            result += static_html(" <span class='badge bg-danger'>Deleted</span>")
        if not auction.promote_this_auction:
            result += static_html(" <span class='badge bg-dark'>Not promoted</span>")
        if auction.distance:
            # Use distance conversion filter
            user = self.request.user if self.request else None
            distance_result = convert_distance(auction.distance, user)
            if distance_result:
                distance_value, distance_unit = distance_result
                result += format_html(
                    " <span class='badge bg-primary'>{} {} from you</span>", distance_value, distance_unit
                )
        if auction.joined and not auction.is_last_used:
            result += static_html(" <span class='badge bg-success text-dark'>Joined</span>")
        # Both are safe strings, but template_promo_info is "" when there is no promo, and
        # SafeString + str is a plain str -- which the table would then escape onto the page.
        result += format_html("{}{}", auction.template_lot_link_first_column, auction.template_promo_info)
        return result

    class Meta:
        model = Auction
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "auction",
            "date",
            "lots",
        )
        row_attrs = {}


class InvoiceHTMxTable(tables.Table):
    """The current user's own invoices, /invoices/, newest first; every column sorts on a real field."""

    #: status code -> badge class. Success fills need dark text; see style_reference.md.
    STATUS_BADGES = {
        "DRAFT": "bg-secondary",
        "UNPAID": "bg-info",
        "PAID": "bg-success text-dark",
    }

    # Accessor `pk`: django-tables2 skips render_*() for empty values, and pk is never empty.
    invoice = tables.Column(accessor="pk", verbose_name="Invoice", order_by=("auction__title",))
    total = tables.Column(accessor="pk", verbose_name="Total", order_by=("calculated_total",))
    status = tables.Column(accessor="status", verbose_name="Status")
    date = tables.Column(accessor="date", verbose_name="Date")

    def render_invoice(self, value, record):
        return format_html("<a href='{}'>{}</a>", record.get_absolute_url(), record.label or str(record))

    def render_total(self, value, record):
        """Owed to the club shows red in parentheses; a payout plain.

        Reads the stamped `calculated_total` so display and sort agree; falls back only for NULL.
        """
        amount = record.calculated_total
        if amount is None:
            amount = record.rounded_net
        # Format the number before format_html(), which escapes it to a string.
        if amount < 0:
            return format_html("<span class='text-danger'>({}{})</span>", record.currency_symbol, f"{abs(amount):.2f}")
        return format_html("{}{}", record.currency_symbol, f"{amount:.2f}")

    def render_status(self, value, record):
        return format_html(
            "<span class='badge {}'>{}</span>",
            self.STATUS_BADGES.get(value, "bg-secondary"),
            record.get_status_display(),
        )

    class Meta:
        model = Invoice
        template_name = "tables/bootstrap_htmx.html"
        fields = (
            "invoice",
            "total",
            "status",
            "date",
        )
        order_by = "-date"


class LotHTMxTableForUsers(tables.Table):
    hide_string = "d-md-table-cell d-none"
    lot_number = tables.Column(
        accessor="lot_number_display",
        verbose_name="Lot number",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    active = tables.Column(accessor="active", verbose_name="Status")
    price = tables.Column(accessor="high_bid", verbose_name="Price", orderable=False)
    views = tables.Column(
        accessor="page_views",
        verbose_name="Views",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
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
        result = static_html("")
        if not record.image_count:
            result += format_html(
                ' <a href="{}" class="badge bg-primary"><i class="bi bi-file-image"></i> Add image</a>',
                reverse("add_image", kwargs={"lot": record.pk}),
            )
        if record.can_be_edited:
            result += format_html(
                ' <a href="{}" class="badge text-dark bg-warning"><i class="bi bi-calendar"></i> Edit</a>',
                reverse("edit_lot", kwargs={"pk": record.pk}),
            )
        result += format_html(
            ' <a href="{}?copy={}" class="badge bg-info"><i class="bi bi-calendar-plus"></i> Copy to new lot</a>',
            reverse("new_lot"),
            record.pk,
        )
        if record.can_be_deleted:
            result += format_html(
                ' <a href="{}?next={}" class="badge bg-danger"><i class="bi bi-trash"></i> Delete</a>',
                reverse("delete_lot", kwargs={"pk": record.pk}),
                reverse("selling"),
            )
        return result

    def render_lot_name(self, value, record):
        result = format_html("<a href='{}?src=my_lots'>{}", record.lot_link, value)
        if record.owner_chats:
            result += format_html(
                " <span style='color:black;font-weight:900' class='badge bg-warning'>{}</span>", record.owner_chats
            )
        result += static_html("</a>")
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
                result += format_html(' <span class="badge bg-success text-dark">{}</span>', " · ".join(badge_parts))
            except Exception:
                pass
        return result

    def render_auction(self, value, record):
        return format_html("<small>{}</small>", value)

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
        row_attrs = {}


_PERMISSION_BADGES = [
    ("permission_admin", "Admin"),
    ("permission_edit_club", "Edit club settings"),
    ("permission_money", "Manage membership and payments"),
    ("permission_manage_auctions", "Manage auctions"),
    ("permission_manage_bap", "Award points"),
    ("permission_manage_donations", "Manage donations"),
    ("permission_send_announcements", "Send announcements"),
    ("permission_export", "Import/export data"),
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
        empty_values=(),
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
                icon = static_html(
                    "<i class='text-warning bi bi-people-fill me-1' title='This member may be a duplicate'></i>"
                )
            else:
                icon = static_html("<i class='bi bi-person-fill-gear me-1'></i>")
        else:
            icon = static_html("<i class='bi bi-person-fill me-1'></i>")
        result = format_html(
            "<a href='' hx-get='{}' hx-target='#modals-here' hx-trigger='click'>{}{}</a>",
            url,
            icon,
            name,
        )
        if record.is_deleted:
            result += static_html(" <span class='badge bg-secondary'>Deactivated</span>")
        if record.discord_id:
            result += static_html("<i class='bi bi-discord ms-1' title='Linked to Discord'></i>")
        if record.email_address_status == "BAD":
            result += static_html(
                "<i class='bi bi-envelope-exclamation-fill text-danger ms-1' title='Unable to send email to this address'></i>"
            )
        if record.email_address_status == "VALID":
            result += static_html("<i class='bi bi-envelope-check-fill ms-1' title='Verified email'></i>")
        for field, label in _PERMISSION_BADGES:
            if getattr(record, field, False):
                result += format_html(
                    " <span class='badge bg-danger' title='{}'>{}</span>",
                    label,
                    label,
                )
                break
        return result

    def render_membership_expiration_date(self, value, record):
        from django.utils import timezone

        today = timezone.localdate()
        has_fee = bool(record.club.membership_annual_fee)

        renew_btn = static_html("")
        if has_fee and not record.is_deleted:
            renew_url = reverse("club_member_renew", kwargs={"pk": record.pk})
            renew_btn = format_html(
                " <a href='javascript:void(0)' hx-get='{}' hx-target='#modals-here'"
                " class='btn btn-sm btn-primary py-0 px-1'>Renew</a>",
                renew_url,
            )

        # A last-paid date alone still implies an expiration; show it marked as derived.
        derived = False
        if not value:
            effective = record.effective_expiration_date
            if effective:
                value = effective
                derived = True

        if not value:
            if has_fee and not record.is_deleted:
                badge = static_html(" <span class='badge bg-danger ms-1'>Expired</span>")
                return format_html("—{}{}", badge, renew_btn)
            return static_html("—")

        formatted = value.strftime("%b %-d, %Y")
        if derived:
            formatted = format_html(
                "<span title='No expiration date is set for this member, so this is one membership "
                "period after {}'>{}*</span>",
                record.membership_last_paid.strftime("%b %-d, %Y"),
                formatted,
            )
        days_expired = (today - value).days
        if days_expired > 0:
            return format_html(
                "{} <span class='badge bg-danger ms-1'>{} day{} expired</span>{}",
                formatted,
                days_expired,
                "s" if days_expired != 1 else "",
                renew_btn,
            )
        days_until = (value - today).days
        if days_until <= 30:
            return format_html("{}{}", formatted, renew_btn)
        return format_html("{}", formatted)

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
        if not self.can_add_edit and not self.can_manage_permissions and not self.can_manage_discord:
            return ""
        name = record.display_name

        permissions_item = static_html("")
        if self.can_manage_permissions and not record.is_deleted:
            perms_url = reverse("clubmember_permissions", kwargs={"pk": record.pk})
            permissions_item = format_html(
                '<li><a class="dropdown-item" href="javascript:void(0)"'
                ' hx-get="{}" hx-target="#modals-here">'
                '<i class="bi bi-shield-lock me-1"></i>Permissions</a></li>'
                "<li><hr class='dropdown-divider'></li>",
                perms_url,
            )

        edit_items = static_html("")
        if self.can_add_edit:
            if record.is_deleted:
                reactivate_url = reverse("club_member_reactivate", kwargs={"pk": record.pk})
                perm_delete_url = reverse("club_member_confirm", kwargs={"pk": record.pk, "action": "permanent_delete"})
                edit_items = format_html(
                    '<li><a class="dropdown-item" href="javascript:void(0)"'
                    ' hx-post="{}" hx-target="#modals-here" hx-swap="innerHTML">'
                    '<i class="bi bi-person-check me-1"></i>Reactivate</a></li>'
                    '<li><hr class="dropdown-divider"></li>'
                    '<li><a class="dropdown-item text-danger" href="javascript:void(0)"'
                    ' hx-get="{}" hx-target="#modals-here">'
                    '<i class="bi bi-trash me-1"></i>Permanently delete</a></li>',
                    reactivate_url,
                    perm_delete_url,
                )
            else:
                confirm_delete_url = reverse("club_member_confirm", kwargs={"pk": record.pk, "action": "delete"})
                merge_url = reverse("club_member_merge", kwargs={"slug": record.club.slug, "pk": record.pk})
                if self.request:
                    merge_url += "?" + urlencode({"next": self.request.get_full_path()})
                email_item = static_html("")
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
                # No card to show or send when the barcode feature is off.
                membership_number_item = static_html("")
                if record.club.show_member_barcode:
                    membership_number_url = reverse("club_member_membership_number", kwargs={"pk": record.pk})
                    resend_card_url = reverse("club_member_confirm", kwargs={"pk": record.pk, "action": "resend_card"})
                    membership_number_item = format_html(
                        '<li><a class="dropdown-item" href="javascript:void(0)"'
                        ' hx-get="{}" hx-target="#modals-here">'
                        '<i class="bi bi-credit-card-2-front me-1"></i>Membership number</a></li>'
                        '<li><a class="dropdown-item" href="javascript:void(0)"'
                        ' hx-get="{}" hx-target="#modals-here">'
                        '<i class="bi bi-send me-1"></i>Resend membership card</a></li>',
                        membership_number_url,
                        resend_card_url,
                    )
                # Only with a membership fee.
                renewal_items = static_html("")
                if record.club.membership_annual_fee:
                    renew_confirm_url = reverse("club_member_renew", kwargs={"pk": record.pk})
                    set_expiry_url = reverse(
                        "club_member_renew_page", kwargs={"slug": record.club.slug, "pk": record.pk}
                    )
                    if self.request:
                        set_expiry_url += "?" + urlencode({"next": self.request.get_full_path()})
                    renewal_items = format_html(
                        '<li><a class="dropdown-item" href="javascript:void(0)"'
                        ' hx-get="{}" hx-target="#modals-here">'
                        '<i class="bi bi-calendar-check me-1"></i>Renew</a></li>'
                        '<li><a class="dropdown-item" href="{}">'
                        '<i class="bi bi-calendar-range me-1"></i>Set expiration date</a></li>',
                        renew_confirm_url,
                        set_expiry_url,
                    )
                edit_items = format_html(
                    "{}"
                    '<li><a class="dropdown-item" href="{}">'
                    '<i class="bi bi-people me-1"></i>Merge with...</a></li>'
                    "{}"
                    "{}"
                    '<li><hr class="dropdown-divider"></li>'
                    '<li><a class="dropdown-item" href="javascript:void(0)"'
                    ' hx-get="{}" hx-target="#modals-here">'
                    '<i class="bi bi-person-dash me-1"></i>Deactivate</a></li>',
                    renewal_items,
                    merge_url,
                    membership_number_item,
                    email_item,
                    confirm_delete_url,
                )

        django_admin_item = static_html("")
        discord_item = static_html("")
        if self.can_manage_discord and not record.is_deleted:
            discord_url = reverse("clubmember_discord", kwargs={"pk": record.pk})
            discord_item = format_html(
                '<li><a class="dropdown-item" href="javascript:void(0)"'
                ' hx-get="{}" hx-target="#modals-here">'
                '<i class="bi bi-discord me-1"></i>Discord</a></li>',
                discord_url,
            )

        mailchimp_item = static_html("")
        if self.can_add_edit and record.mailchimp_web_id and record.club.mailchimp_server_prefix:
            mailchimp_url = (
                f"https://{record.club.mailchimp_server_prefix}.admin.mailchimp.com"
                f"/lists/members/view?id={record.mailchimp_web_id}"
            )
            mailchimp_item = format_html(
                '<li><a class="dropdown-item" href="{}" target="_blank" rel="noopener">'
                '<i class="bi bi-envelope-paper me-1"></i>View in Mailchimp</a></li>',
                mailchimp_url,
            )

        brevo_item = static_html("")
        if self.can_add_edit and record.brevo_contact_id:
            brevo_url = f"https://app.brevo.com/contact/index/{record.brevo_contact_id}"
            brevo_item = format_html(
                '<li><a class="dropdown-item" href="{}" target="_blank" rel="noopener">'
                '<i class="bi bi-send me-1"></i>View in Brevo</a></li>',
                brevo_url,
            )

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
            '<button type="button" class="btn btn-sm btn-primary dropdown-toggle"'
            ' data-bs-toggle="dropdown" aria-label="Actions for {}">Actions</button>'
            "<ul class='dropdown-menu'>{}{}{}{}{}{}</ul>"
            "</div>",
            name,
            permissions_item,
            discord_item,
            mailchimp_item,
            brevo_item,
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
        self.can_manage_discord = kwargs.pop("can_manage_discord", False)
        can_manage_bap = kwargs.pop("can_manage_bap", False)
        can_manage_membership = kwargs.pop("can_manage_membership", False)
        can_manage_auctions = kwargs.pop("can_manage_auctions", False)
        club_has_fee = kwargs.pop("club_has_fee", True)
        exclude = list(kwargs.pop("exclude", None) or [])
        if not can_manage_bap:
            exclude += ["bap_points", "hap_points"]
        if not can_manage_membership or not club_has_fee:
            exclude += ["membership_last_paid", "membership_expiration_date"]
        if not can_manage_auctions:
            exclude += ["bidder_number"]
        super().__init__(*args, exclude=exclude, **kwargs)


class ClubHistoryHTMxTable(tables.Table):
    name = tables.Column(accessor="user", verbose_name="User", default="System")
    action = tables.Column(accessor="action", verbose_name="Action")
    applies_to = tables.Column(accessor="applies_to", verbose_name="Modified")
    timestamp = tables.Column(accessor="timestamp", verbose_name="Time")

    # One icon per ClubHistory.applies_to choice.
    APPLIES_TO_ICONS = {
        "RULES": "bi-gear-fill",
        "MEMBERS": "bi-people-fill",
        "MEMBERSHIP": "bi-card-checklist",
        "SETTINGS": "bi-sliders",
        "BAP": "bi-award-fill",
        "DONATIONS": "bi-gift-fill",
        "ANNOUNCEMENTS": "bi-megaphone-fill",
    }

    def render_applies_to(self, value, record):
        icon = self.APPLIES_TO_ICONS.get(record.applies_to)
        # icon comes from APPLIES_TO_ICONS, never from the row.
        prefix = format_html("<i class='bi {}'></i>", icon) if icon else static_html("")
        return format_html("{} {}", prefix, value)

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
            mark_safe(self._MODAL_ATTRS),  # noqa: S308 - a module constant, no row data in it
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
        url = record.get_absolute_url()
        category_name = record.species_category.name if record.species_category else "Uncategorized"
        category_url = reverse("club_bap_lot_category", kwargs={"pk": record.pk})
        badges = format_html(
            '<button type="button" class="badge bg-secondary border-0" '
            'hx-get="{}" hx-target="#modals-here" hx-swap="innerHTML">{}</button>',
            category_url,
            category_name,
        )
        if (
            self.club
            and self.club.points_for_custom_checkbox > 0
            and record.custom_checkbox
            and record.auction
            and record.auction.custom_checkbox_name
        ):
            badges = badges + format_html(
                ' <span class="badge bg-info text-dark">{}</span>',
                record.auction.custom_checkbox_name,
            )
        return format_html('<a href="{}">{}</a><div class="mt-1">{}</div>', url, value, badges)

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
        # Same precedence as Lot.bap_points_for_club, from prefetched dicts.
        genus = record.species.genus if record.species_id else ""
        override = self._genus_override_cache.get(genus) if genus else None
        if override is None and record.species_category_id:
            override = self._override_cache.get(record.species_category_id)
        if override is not None:
            default_points = override.points
        elif not self.club:
            default_points = 0
        elif self.club.points_per_lot is not None:
            # 0 means 0, not "use the category".
            default_points = self.club.points_per_lot
        else:
            default_points = record.species_category.bap_points if record.species_category_id else 5
        if self.club and self.club.points_for_custom_checkbox > 0 and record.custom_checkbox:
            default_points += self.club.points_for_custom_checkbox
        return mark_safe(  # noqa: S308 - render_to_string output; the template autoescapes
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
        if self.club:
            self._override_cache = {o.category_id: o for o in ClubBapCategoryOverride.objects.filter(club=self.club)}
            self._genus_override_cache = {o.genus: o for o in ClubBapGenusOverride.objects.filter(club=self.club)}
        else:
            self._override_cache = {}
            self._genus_override_cache = {}


class SpeakerHTMxTable(tables.Table):
    """The speaker directory list; rows open the same htmx panel as the map markers."""

    hide_string = "d-md-table-cell d-none"
    photo = tables.Column(accessor="pk", verbose_name="", orderable=False)
    name = tables.Column(accessor="name", verbose_name="Speaker", orderable=True)
    location = tables.Column(accessor="location", verbose_name="Location", orderable=True, empty_values=())
    topics = tables.Column(
        accessor="pk",
        verbose_name="Topics",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    speaker_tags = tables.Column(
        accessor="pk",
        verbose_name="Tags",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )

    class Meta:
        model = Speaker
        fields = ("photo", "name", "location", "topics", "speaker_tags")
        # The default django-tables2 template lacks the site's pagination classes and reloads the page.
        template_name = "tables/bootstrap_htmx.html"
        row_attrs = {"class": "speaker-row"}

    def __init__(self, *args, **kwargs):
        # Without an origin there's no distance suffix.
        self.has_origin = kwargs.pop("has_origin", False)
        super().__init__(*args, **kwargs)

    def order_location(self, queryset, is_descending):
        """Sort Location by distance when there's an origin, nulls last."""
        if not self.has_origin:
            return queryset.order_by(("-" if is_descending else "") + "location"), True
        distance = F("distance").desc(nulls_last=True) if is_descending else F("distance").asc(nulls_last=True)
        return queryset.order_by(distance, "name"), True

    def render_photo(self, record):
        url = record.thumbnail_url
        if not url:
            return static_html(
                "<span class='d-inline-flex align-items-center justify-content-center bg-secondary rounded-circle' "
                "style='width:40px;height:40px;'><i class='bi bi-person-fill'></i></span>"
            )
        return format_html(
            "<img src='{}' alt='' class='rounded-circle' style='width:40px;height:40px;object-fit:cover;'>", url
        )

    def render_name(self, value, record):
        """Open the panel over htmx, but push the speaker page URL rather than the fragment's."""
        page_url = reverse("speaker_detail", kwargs={"slug": record.slug})
        link = format_html(
            "<a href='{}' class='speaker-open' hx-get='{}' hx-target='#speaker-panel' "
            "hx-swap='innerHTML' hx-push-url='{}'>{}</a>",
            page_url,
            reverse("speaker_panel", kwargs={"slug": record.slug}),
            page_url,
            record.display_name,
        )
        if not record.is_recently_added:
            return link
        # bg-success needs dark text for contrast; see style_reference.md.
        return format_html("{} <span class='badge bg-success text-dark'>New</span>", link)

    def render_location(self, value, record):
        distance = getattr(record, "distance", None)
        if not value:
            if self.has_origin:
                return static_html("<span class='text-muted'>No location set</span>")
            return static_html("<span class='text-muted'>—</span>")
        if distance is not None and record.latitude is not None:
            return format_html("{} <small class='text-muted'>· {} miles</small>", value, int(distance))
        return value

    def render_topics(self, record):
        names = [topic.name for topic in record.topics.all()[:3]]
        if not names:
            return static_html("<span class='text-muted'>—</span>")
        badges = format_html_join(" ", "<span class='badge bg-secondary'>{}</span>", ((name,) for name in names))
        extra = record.topics.count() - len(names)
        if extra > 0:
            badges += format_html(" <small class='text-muted'>+{}</small>", extra)
        return badges

    def render_speaker_tags(self, record):
        counts = record.tag_counts()[:2]
        if not counts:
            return static_html("<span class='text-muted'>—</span>")
        return format_html_join(
            " ",
            "<span class='badge bg-primary'>{} {}</span>",
            ((label, count) for _value, label, _group, count in counts),
        )


class ClubHTMxTable(tables.Table):
    """The public club finder list. Public columns only: no address, nothing about members."""

    hide_string = "d-md-table-cell d-none"
    icon = tables.Column(accessor="pk", verbose_name="", orderable=False)
    name = tables.Column(accessor="name", verbose_name="Club", orderable=True)
    next_event = tables.Column(
        accessor="pk",
        verbose_name="Coming up",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    interests = tables.Column(
        accessor="pk",
        verbose_name="Interests",
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    distance = tables.Column(accessor="pk", verbose_name="Distance", orderable=True, empty_values=())

    class Meta:
        model = Club
        fields = ("icon", "name", "next_event", "interests", "distance")
        # See SpeakerHTMxTable.
        template_name = "tables/bootstrap_htmx.html"
        row_attrs = {"class": "club-row"}

    def __init__(self, *args, **kwargs):
        self.has_origin = kwargs.pop("has_origin", False)
        super().__init__(*args, **kwargs)

    def order_distance(self, queryset, is_descending):
        """Sort by distance when there's an origin, nulls last; otherwise by name."""
        if not self.has_origin:
            return queryset.order_by(("-" if is_descending else "") + "name"), True
        distance = F("distance").desc(nulls_last=True) if is_descending else F("distance").asc(nulls_last=True)
        return queryset.order_by(distance, "name"), True

    def render_icon(self, record):
        if not record.icon:
            return static_html(
                "<span class='d-inline-flex align-items-center justify-content-center bg-secondary rounded' "
                "style='width:40px;height:40px;'><i class='bi bi-people-fill'></i></span>"
            )
        return format_html(
            "<img src='{}' alt='' class='rounded' style='width:40px;height:40px;object-fit:cover;'>",
            record.icon_thumbnail_url,
        )

    def render_name(self, value, record):
        """A plain link to the club's page. See :mod:`auctions.views.club_finder`."""
        link = format_html(
            "<a href='{}'>{}</a>",
            reverse("club_detail", kwargs={"slug": record.slug}),
            record.name,
        )
        if not record.allow_joining:
            return link
        # bg-success needs dark text for contrast; see style_reference.md.
        return format_html("{} <span class='badge bg-success text-dark'>Taking members</span>", link)

    def render_next_event(self, record):
        title = getattr(record, "next_event_title", None)
        if not title:
            return static_html("<span class='text-muted'>—</span>")
        return format_html(
            "{} <small class='text-muted'>{}</small>", title, naturalday(getattr(record, "next_event_start", None))
        )

    def render_interests(self, record):
        names = [interest.name for interest in record.interests.all()[:3]]
        if not names:
            return static_html("<span class='text-muted'>—</span>")
        return format_html_join(" ", "<span class='badge bg-secondary'>{}</span>", ((name,) for name in names))

    def render_distance(self, record):
        distance = getattr(record, "distance", None)
        if distance is None:
            return static_html("<span class='text-muted'>—</span>")
        return format_html("{} miles", int(distance))


class DonationVendorHTMxTable(tables.Table):
    """Vendors a club is asking for donations, with where each conversation stands."""

    hide_string = "d-md-table-cell d-none"

    name = tables.Column(accessor="name", verbose_name="Vendor")
    contact_name = tables.Column(
        accessor="contact_name",
        verbose_name="Contact",
        default="—",
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    email = tables.Column(
        accessor="email",
        verbose_name="Email",
        default="—",
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    status = tables.Column(accessor="status", verbose_name="Status")
    latest_reply = tables.Column(
        accessor="latest_reply_summary",
        verbose_name="Latest reply",
        default="—",
        # Annotated by ClubDonationVendorsView.
        orderable=False,
        attrs={"th": {"class": hide_string}, "cell": {"class": hide_string}},
    )
    last_contact = tables.Column(accessor="last_contact", verbose_name="Last contact", default="—")
    followup_due = tables.Column(accessor="followup_due", verbose_name="Follow-up", default="—")
    contact = tables.Column(accessor="pk", verbose_name="Contact", orderable=False)

    #: Badge background per status.
    STATUS_BADGES = {
        "new": "bg-secondary",
        "sent": "bg-info",
        "interested": "bg-primary",
        # Success and warning fills need dark text; see style_reference.md.
        "promised": "bg-warning text-dark",
        "received": "bg-success text-dark",
        "not_interested": "bg-dark",
        "do_not_contact": "bg-danger",
    }

    class Meta:
        model = DonationVendor
        template_name = "tables/bootstrap_htmx.html"
        fields = ()

    def __init__(self, *args, **kwargs):
        # Counted once by the view rather than per row.
        self.quota = kwargs.pop("quota", None)
        super().__init__(*args, **kwargs)

    def render_name(self, value, record):
        """The vendor's name, under the icon for however they're reached: one shop icon on every row
        said nothing, and how to ask them is the fact you want before clicking.
        """
        return format_html(
            "<a href='' hx-noget hx-get='{}' hx-target='#modals-here' hx-trigger='click'>"
            "<i class='bi {} me-1' title='{}'></i>{}</a>",
            reverse("club_donation_vendor", kwargs={"pk": record.pk}),
            record.contact_method_icon,
            record.get_contact_method_display(),
            value,
        )

    #: Preview length; the full summary is in the title and the vendor panel.
    SUMMARY_PREVIEW_LENGTH = 120

    def render_latest_reply(self, value):
        """The vendor's latest reply summary; ``default`` covers vendors with none."""
        summary = str(value).strip()
        shown = summary
        if len(shown) > self.SUMMARY_PREVIEW_LENGTH:
            shown = shown[: self.SUMMARY_PREVIEW_LENGTH - 1].rstrip() + "…"
        return format_html('<span class="text-muted" title="{}">{}</span>', summary, shown)

    def render_status(self, value, record):
        badge = self.STATUS_BADGES.get(record.status, "bg-secondary")
        label = record.get_status_display()
        if record.unsubscribed:
            return format_html(
                "<span class='badge {}' title='This vendor unsubscribed'>"
                "<i class='bi bi-slash-circle me-1'></i>{}</span>",
                badge,
                label,
            )
        return format_html("<span class='badge {}'>{}</span>", badge, label)

    def render_last_contact(self, value, record):
        if not record.last_contact:
            return "—"
        return format_html("<span title='{}'>{}</span>", record.last_contact, naturalday(record.last_contact))

    def order_followup_due(self, queryset, is_descending):
        """Sort by follow-up date with empty dates last either way."""
        field = F("followup_due")
        return (
            queryset.order_by(field.desc(nulls_last=True) if is_descending else field.asc(nulls_last=True), "name"),
            True,
        )

    def render_followup_due(self, value, record):
        if not record.followup_due:
            return "—"
        formatted = naturalday(record.followup_due)
        if record.is_followup_due:
            return format_html(
                "<span class='text-warning' title='{}'><i class='bi bi-exclamation-circle me-1'></i>{}</span>",
                record.followup_due,
                formatted,
            )
        return format_html("<span title='{}'>{}</span>", record.followup_due, formatted)

    def render_contact(self, value, record):
        """A button that opens the right dialog, or explains why the vendor can't be contacted.

        Two dialogs: the email one for a vendor we can write to, and the dossier for a vendor whose own
        form, phone or counter is where the asking happens.
        """
        reason = donations.contact_blocked_reason(record, self.quota)
        if reason:
            # The toast handler is delegated from club_donation_vendors.html, surviving htmx swaps.
            return format_html(
                "<button type='button' class='btn btn-sm btn-primary donation-contact-blocked' "
                "data-reason='{}'><i class='bi bi-envelope-slash me-1'></i>Contact</button>",
                reason,
            )
        off_site = record.contacted_off_site
        return format_html(
            "<button type='button' class='btn btn-sm btn-primary' hx-get='{}' "
            "hx-target='#modals-here' hx-trigger='click'>"
            "<i class='bi {} me-1'></i>Contact</button>",
            reverse("club_donation_dossier" if off_site else "club_donation_contact", kwargs={"pk": record.pk}),
            "bi-clipboard-check" if off_site else "bi-envelope",
        )


def natural_sort_key(text):
    """Sort "Table 2" before "Table 10": runs of digits compare as numbers, the rest ignoring case.

    ``re.split`` with a group puts text at even indexes and digits at odd ones, so two keys never
    compare a number with a string.
    """
    parts = re.split(r"(\d+)", str(text))
    return tuple(int(part) if index % 2 else part.casefold() for index, part in enumerate(parts))


class PrintableLotListTable(tables.Table):
    """Every lot in an auction, on paper, with the fields its labels print.

    The columns come from ``Auction.label_print_fields``, and a cell is filled only when the lot's own
    label would print it: a sold lot's label drops its minimum bid, so its row does too. Sold lots show
    their winner, as a sold label does. A column no lot has anything for is left off, and the auction
    date goes in the page heading instead of on every row.

    The rows are a list sorted in Python, not a queryset, so that every column sorts naturally with
    blanks last -- tables are often a custom random field, and "Table 10" belongs after "Table 9".
    """

    class Meta:
        template_name = "tables/bootstrap_htmx.html"
        attrs = {"class": "table table-sm lot-list"}
        # Blank cells stay blank on paper, rather than a column of dashes.
        default = ""

    def __init__(self, data, *, auction, **kwargs):
        fields = set((auction.label_print_fields or "").split(","))
        headers = {
            "lot_number": "Lot",
            "lot_name": "Name",
            "category": "Category",
            "quantity": "Qty",
            "donation": "Donation",
            "min_bid": "Min bid",
            "buy_now": "Buy now",
            "custom_checkbox": auction.custom_checkbox_name or "Custom checkbox",
            "custom_dropdown": auction.custom_dropdown_name or "Custom dropdown",
            "custom_random": auction.custom_random_name or "Custom random field",
            "i_bred_this_fish": "Breeder",
            "custom_field_1": auction.custom_field_1_name or "Notes",
            "description": "Description",
            "seller": "Seller",
            "winner": "Winner",
        }
        rows = list(data)
        for lot in rows:
            cells = self.cells(lot, fields)
            lot.list_cells = {name: display for name, (display, _sort) in cells.items()}
            lot.list_sort = {name: (not sort, natural_sort_key(sort)) for name, (_display, sort) in cells.items()}
        in_use = {name for lot in rows for name, display in lot.list_cells.items() if display}
        kwargs["extra_columns"] = [
            (
                name,
                tables.Column(verbose_name=header, accessor=f"list_cells__{name}", order_by=f"list_sort__{name}"),
            )
            for name, header in headers.items()
            # Always a lot number, even when nothing else is filled in.
            if name in in_use or name == "lot_number"
        ]
        super().__init__(rows, **kwargs)

    @staticmethod
    def cells(lot, fields):
        """``{column: (display, sort text)}`` for one lot; a blank display is a blank cell."""

        def on(field, value):
            return value if field in fields and value else ""

        def price(label, amount):
            return f"{lot.currency_symbol}{amount}" if label else ""

        name = on("lot_name", lot.lot_name)
        # Species under the name, as the label prints it -- see Lot.scientific_name_line.
        species = ""
        if "scientific_name" in fields:
            if lot.scientific_name_line:
                species = format_html("<i>{}</i>", lot.scientific_name_line)
            else:
                species = lot.common_name_line
        seller = on("seller_name", lot.seller_name)
        email = on("seller_email", lot.seller_email)
        description = lot.description_label if "description_label" in fields else ""
        location = lot.winner_location if lot.auction.multi_location and lot.winner_name else ""
        return {
            "lot_number": (lot.lot_number_display, lot.lot_number_display),
            "lot_name": (_stacked(name, species), name or strip_tags(species)),
            "category": (on("category", lot.category and str(lot.category)),) * 2,
            "quantity": (on("quantity_label", lot.quantity_label) and str(lot.quantity),) * 2,
            "donation": (on("donation_label", lot.donation_label) and "Yes",) * 2,
            "min_bid": (price(on("min_bid_label", lot.min_bid_label), lot.reserve_price),) * 2,
            "buy_now": (price(on("buy_now_label", lot.buy_now_label), lot.buy_now_price),) * 2,
            "custom_checkbox": (on("custom_checkbox_label", lot.custom_checkbox_label) and "Yes",) * 2,
            "custom_dropdown": (on("custom_dropdown_label", lot.custom_dropdown_label),) * 2,
            "custom_random": (on("custom_random_label", lot.custom_random_label),) * 2,
            "i_bred_this_fish": (on("i_bred_this_fish_label", lot.i_bred_this_fish_label) and "Yes",) * 2,
            "custom_field_1": (on("custom_field_1", lot.custom_field_1),) * 2,
            "description": (
                mark_safe(description),  # noqa: S308 - Lot.description_label is sanitized down to <br>
                strip_tags(description),
            ),
            "seller": (_stacked(seller, email), seller or email),
            "winner": (_stacked(lot.winner_name, location), lot.winner_name),
        }


def _stacked(first, second):
    """Two values in one cell, the second small underneath; either may be blank."""
    if first and second:
        return format_html("{}<br><small>{}</small>", first, second)
    return first or (format_html("<small>{}</small>", second) if second else "")
