"""Tags for writing help guides (``auctions/templates/help/guides/``).

``{% page "url_name" %}`` and ``{% rule "field" %}`` are also how ``help_guides`` finds out what a
guide covers, so use them rather than hand-typing a page's name or a rule's label. ``{% ui "Button" %}``
names a button, menu or label on the site, and ``test_help`` fails when the site no longer has it.

Inside ``{% with rule_usage=True %}`` a rule also says how many auctions use it
(:mod:`auctions.field_adoption`). The rules guide turns that on; the other guides name rules in passing.
"""

from decimal import Decimal

from django import template
from django.conf import settings
from django.core.cache import cache
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse
from django.utils import formats, timezone
from django.utils.html import format_html, format_html_join

from auctions import palette_routes
from auctions.helper_functions import static_html

register = template.Library()


def _help(context):
    return context.get("help")


def _page_url(route, ctx):
    """A link into the reader's own auction or club where the page needs one, else None."""
    kwargs = dict(route.fixed)
    if route.scope == palette_routes.SCOPE_AUCTION:
        if not (ctx and ctx.auction):
            return None
        if route.admin == palette_routes.ADMIN_AUCTION and not ctx.is_admin:
            return None
        kwargs["slug"] = ctx.auction.slug
    elif route.scope == palette_routes.SCOPE_CLUB:
        if not (ctx and ctx.club):
            return None
        if route.admin == palette_routes.ADMIN_CLUB and not ctx.is_club_admin:
            return None
        kwargs["slug"] = ctx.club.slug
    elif route.scope != palette_routes.SCOPE_NONE:
        return None
    if route.admin == palette_routes.ADMIN_SUPERUSER:
        return None
    try:
        return reverse(route.key, kwargs=kwargs)
    except NoReverseMatch:
        return None


@register.simple_tag(takes_context=True)
def page(context, url_name, text=""):
    """A page's name, linked into the reader's auction or club when that's possible.

    ``{% page "auction_invoices" %}`` or ``{% page "auction_invoices" "the invoices page" %}``.
    """
    # get_route lowercases what a model sends; a guide spells the name exactly (``allLots``).
    route = palette_routes.ROUTES.get(url_name) or palette_routes.get_route(url_name)
    if route is None:
        msg = f"{url_name!r} is not in palette_routes.ROUTES"
        raise template.TemplateSyntaxError(msg)
    label = text or route.label
    url = _page_url(route, _help(context))
    if url:
        return format_html('<a class="help-page" href="{}">{}</a>', url, label)
    return format_html('<strong class="help-page">{}</strong>', label)


@register.simple_tag
def ui(label):
    """A button, menu, tab or label as the site words it: ``{% ui "Copy to new auction" %}``.

    Checked against the site's own templates, code and form labels (``help_guides.missing_ui_labels``), so
    renaming the button fails the build until the guide says the new name. For somebody else's screens
    (the browser's print dialog, ChatGPT's settings) use plain ``<strong>``.
    """
    return format_html('<strong class="help-ui">{}</strong>', label)


def _rule_label(name):
    """The label the rules page shows for ``name``, or None when neither rules form has the field."""
    from auctions.forms import AuctionCustomFieldsForm, AuctionEditForm

    for form in (AuctionEditForm, AuctionCustomFieldsForm):
        if name in form.base_fields:
            return form.LABELS.get(name) or form.base_fields[name].label or name
    return None


#: Rules with no usage to report: every auction has dates.
NO_USAGE = frozenset(
    {
        "date_start",
        "date_end",
        "lot_submission_start_date",
        "lot_submission_end_date",
        "date_online_bidding_starts",
        "date_online_bidding_ends",
    }
)


def _usage(context, name):
    """The usage badge's text, or blank. Counted by ``help_stats.refresh``, never here: this page is public,
    and the count is two full table scans. Read once per page, not once per rule.
    """
    if name in NO_USAGE:
        return ""
    rows = context.render_context.get("help_rule_usage")
    if rows is None:
        from auctions.field_adoption import CACHE_KEY
        from auctions.help_stats import request_refresh

        try:
            counted = cache.get(CACHE_KEY)
        except Exception:
            counted = []
        if counted is None:
            request_refresh()
        rows = {row.name: row for row in counted or []}
        context.render_context["help_rule_usage"] = rows
    row = rows.get(name)
    return row.usage if row else ""


#: Rules whose value is an amount of money, and rules that are a percentage.
MONEY_RULES = frozenset(
    {
        "lot_entry_fee",
        "registration_fee",
        "unsold_lot_fee",
        "minimum_bid",
        "lot_entry_fee_for_club_members",
        "registration_fee_for_club_members",
        "club_member_discount",
        "force_donation_threshold",
    }
)
PERCENT_RULES = frozenset(
    {
        "winning_bid_percent_to_club",
        "winning_bid_percent_to_club_for_club_members",
        "pre_register_lot_discount_percent",
        "tax",
    }
)
#: Rules where leaving the box empty means the feature is off, so "Yours:" says Off rather than Blank.
OFF_WHEN_BLANK = frozenset({"force_donation_threshold"})
#: Rules the auction form hides for the other kind of auction (``AuctionEditForm.__init__``). No "Yours:" there.
IN_PERSON_ONLY = frozenset(
    {
        "online_bidding",
        "message_users_when_lots_sell",
        "pre_register_lot_discount_percent",
        "date_online_bidding_starts",
        "date_online_bidding_ends",
    }
)
ONLINE_ONLY = frozenset({"date_end"})
#: Rules with no short value to show: the rules text is a page long.
NO_VALUE = frozenset({"summernote_description"})


def _rule_value(auction, name):
    """How ``auction`` has ``name`` set, in a few words, or blank when it has no short answer."""
    if name in NO_VALUE:
        return ""
    if name in (IN_PERSON_ONLY if auction.is_online else ONLINE_ONLY):
        return ""
    if name == "user_cut":
        return f"{100 - auction.winning_bid_percent_to_club}%"
    if name == "club_member_cut":
        return f"{100 - auction.winning_bid_percent_to_club_for_club_members}%"
    display = getattr(auction, f"get_{name}_display", None)
    value = display() if display else getattr(auction, name, None)
    if isinstance(value, bool):
        return "On" if value else "Off"
    if value in (None, ""):
        return "Off" if name in OFF_WHEN_BLANK else "Blank"
    if name in PERCENT_RULES:
        return f"{value}%"
    if name in MONEY_RULES:
        return (
            f"{auction.currency_symbol}{value:g}"
            if isinstance(value, (int, float))
            else f"{auction.currency_symbol}{value}"
        )
    if hasattr(value, "tzinfo"):
        return formats.date_format(timezone.localtime(value), "M j, g:i A")
    text = str(value)
    return text if len(text) <= 30 else text[:29] + "…"


@register.simple_tag(takes_context=True)
def rule(context, name, text=""):
    """An auction rule's label as it appears on the rules page, anchored so other pages can link here.

    On the rules guide (``rule_usage``) it also says how many auctions use it, and, to the people running
    the reader's auction, how that auction has it set.
    """
    label = _rule_label(name)
    if label is None:
        msg = f"{name!r} is not a field on the auction rules forms"
        raise template.TemplateSyntaxError(msg)
    html = format_html('<strong class="help-rule" id="rule-{}">{}</strong>', name, text or label)
    if not context.get("rule_usage"):
        return html
    usage = _usage(context, name)
    if usage:
        html = format_html('{} <span class="badge bg-secondary fw-normal">{}</span>', html, usage)
    ctx = _help(context)
    if ctx and ctx.auction and ctx.is_admin:
        value = _rule_value(ctx.auction, name)
        if value:
            html = format_html('{} <span class="badge bg-primary fw-normal help-yours">Yours: {}</span>', html, value)
    return html


def _money(symbol, amount):
    amount = Decimal(amount).quantize(Decimal("0.01"))
    sign = static_html("&minus;") if amount < 0 else ""
    return format_html("{}{}{}", sign, symbol, f"{abs(amount):,.2f}")


@register.simple_tag(takes_context=True)
def fee_example(context):
    """A worked example of the club's cut on one lot: the reader's own auction's fees if they run it,
    otherwise "$1 + 30%". A column each for a seller who added their own lots and for the alternate fees,
    when the auction has them.
    """
    ctx = _help(context)
    auction = ctx.auction if ctx and ctx.auction and ctx.is_admin else None
    symbol = auction.currency_symbol if auction else "$"
    pct = auction.winning_bid_percent_to_club if auction else 30
    entry = auction.lot_entry_fee if auction else 1
    discount = auction.pre_register_lot_discount_percent if auction else 0
    threshold = (auction.force_donation_threshold or 0) if auction else 0
    price = 10 if threshold < 10 else (threshold // 10 + 1) * 10
    columns = [("Everyone", pct, entry, 0)]
    if discount:
        columns.append(("Added their own lot", pct, entry, discount))
    if auction and auction.alternate_split_mode != "off":
        columns.append(
            (
                auction.alternative_split_label or "Alternate fees",
                auction.winning_bid_percent_to_club_for_club_members,
                auction.lot_entry_fee_for_club_members,
                0,
            )
        )
    heading = (
        format_html("How {}'s fees work out on a {}{} lot:", auction.title, symbol, price)
        if auction
        else static_html('How "$1 + 30%" works out on a $10 lot:')
    )
    head = format_html_join("", '<th class="text-end">{}</th>', ((name,) for name, *_ in columns))
    cut_cells = format_html_join(
        "",
        '<td class="text-end">{} <span class="text-muted small">({}%)</span></td>',
        ((_money(symbol, -price * (p - d) / 100), p - d) for _n, p, _e, d in columns),
    )
    entry_cells = format_html_join(
        "", '<td class="text-end">{}</td>', ((_money(symbol, -e),) for _n, _p, e, _d in columns)
    )
    gets_cells = format_html_join(
        "",
        '<th class="text-end">{}</th>',
        ((_money(symbol, price * (100 - p + d) / 100 - e),) for _n, p, e, d in columns),
    )
    price_cells = format_html_join("", '<td class="text-end">{}</td>', ((_money(symbol, price),) for _c in columns))
    return format_html(
        '<p class="mb-1">{}</p><div class="table-responsive"><table class="table table-sm help-table">'
        "<thead><tr><th></th>{}</tr></thead><tbody>"
        "<tr><td>Sold for</td>{}</tr><tr><td>Club cut</td>{}</tr><tr><td>Lot entry fee</td>{}</tr>"
        "<tr><th>The seller gets</th>{}</tr></tbody></table></div>",
        heading,
        head,
        price_cells,
        cut_cells,
        entry_cells,
        gets_cells,
    )


class _WrapNode(template.Node):
    def __init__(self, nodelist, opening, closing):
        self.nodelist, self.opening, self.closing = nodelist, opening, closing

    def render(self, context):
        body = self.nodelist.render(context).strip()
        return f"{self.opening}{body}{self.closing}" if body else ""


def _icon_argument(parser, token, default):
    bits = token.split_contents()
    return template.Variable(bits[1]).resolve({}) if len(bits) > 1 else default


@register.tag
def helptip(parser, token):
    """``{% helptip %}…{% endhelptip %}``: a sentence about the reader's own auction or club.

    Personal text goes here and nowhere else, so the guide reads the same with every tip removed.
    Renders nothing when the body is blank, so wrap the ``{% if %}`` inside it. An icon name is optional:
    ``{% helptip "graph-up" %}`` for a number from the reader's own auction.
    """
    icon = _icon_argument(parser, token, "person-check")
    nodelist = parser.parse(("endhelptip",))
    parser.delete_first_token()
    return _WrapNode(nodelist, f'<div class="help-note help-tip"><i class="bi bi-{icon}"></i><div>', "</div></div>")


@register.tag
def tip(parser, token):
    """``{% tip %}…{% endtip %}``: advice worth pulling out of the text. The same for every reader."""
    nodelist = parser.parse(("endtip",))
    parser.delete_first_token()
    return _WrapNode(
        nodelist,
        '<div class="help-note help-hint"><i class="bi bi-lightbulb"></i><div><span class="help-hint-label">Tip</span> ',
        "</div></div>",
    )


@register.tag
def aitip(parser, token):
    """``{% aitip %}…{% endaitip %}``: what to ask an AI agent, after a section. The same for every reader.

    Opens with the robot and "AI:", the look of the connect-an-agent line at the top of every page.
    """
    nodelist = parser.parse(("endaitip",))
    parser.delete_first_token()
    return _WrapNode(
        nodelist,
        '<div class="help-note help-ai"><i class="bi bi-robot"></i><div><span class="help-ai-label">AI:</span> ',
        "</div></div>",
    )


class _MikeNode(_WrapNode):
    def render(self, context):
        self.opening = format_html(
            '<aside class="help-mike"><img class="help-mike-icon" src="{}" alt="" width="20" height="20"><div>',
            static("favicon-32x32.png"),
        )
        return super().render(context)


@register.tag
def mike(parser, token):
    """``{% mike %}…{% endmike %}``: a short story about Mike, the club member who means well."""
    nodelist = parser.parse(("endmike",))
    parser.delete_first_token()
    return _MikeNode(nodelist, "", "</div></aside>")


@register.inclusion_tag("help/partials/square_account.html", takes_context=True)
def square_account(context):
    """The reader's Square connection: connect, reconnect for Tap to Pay, disconnect, or ask for access."""
    from auctions.models import SquareSeller

    user = context.get("user")
    signed_in = bool(user and user.is_authenticated)
    return {
        "user": user,
        "seller": SquareSeller.objects.filter(user=user).first() if signed_in else None,
        "csrf_token": context.get("csrf_token"),
    }


@register.inclusion_tag("help/partials/paypal_account.html", takes_context=True)
def paypal_account(context):
    """The reader's PayPal connection, if they made one, with its disconnect button."""
    from auctions.models import PayPalSeller

    user = context.get("user")
    signed_in = bool(user and user.is_authenticated)
    return {"seller": PayPalSeller.objects.filter(user=user).first() if signed_in else None}


@register.inclusion_tag("help/partials/push_status.html", takes_context=True)
def push_status(context):
    """Turn on "your lot is about to sell" notifications and test them, for the app or this browser.

    Only the page can tell whether this device is set up: the app's bridge in the app, the browser's own
    subscription outside it. With the app on the account, a browser isn't asked (``user_has_app_push``).
    """
    ctx = _help(context)
    if not (ctx and ctx.signed_in):
        return {"show": False}
    request = context.get("request")
    return {
        "show": True,
        "lot_alerts": ctx.account["lot_alerts"],
        "app_push": ctx.account["app_push"],
        "in_app": bool(getattr(request, "is_mobile_app", False)),
        "vapid_public_key": settings.WEBPUSH_SETTINGS.get("VAPID_PUBLIC_KEY", ""),
        "csrf_token": context.get("csrf_token"),
    }


@register.simple_tag(takes_context=True)
def mcp_url(context):
    """The address an AI agent connects to: absolute when there's a request, else the path."""
    path = reverse("mcp")
    request = context.get("request")
    return request.build_absolute_uri(path) if request else path


@register.inclusion_tag("help/partials/ai_connections.html", takes_context=True)
def ai_connections(context, part):
    """The reader's connected agents (``part="apps"``) or keys (``"keys"``), with the forms /ai/ handles.

    A new key's secret is taken out of the session here, on the one page load that shows it.
    """
    from auctions.mcp.auth import connected_apps
    from auctions.models import UserAPIKey

    request = context.get("request")
    user = getattr(request, "user", None)
    signed_in = bool(user and user.is_authenticated)
    values = {
        "part": part,
        "signed_in": signed_in,
        "csrf_token": context.get("csrf_token"),
        "guide_path": request.path if request else "",
    }
    if signed_in and part == "apps":
        values["connected_apps"] = connected_apps(user)
    elif signed_in:
        values["keys"] = UserAPIKey.objects.filter(user=user).order_by("-created_at")
        values["new_raw_key"] = request.session.pop("new_user_api_key", None)
    return values
