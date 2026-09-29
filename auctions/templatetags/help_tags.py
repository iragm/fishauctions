"""Tags for writing help guides (``auctions/templates/help/guides/``).

``{% page "url_name" %}`` and ``{% rule "field" %}`` are also how ``help_guides`` finds out what a
guide covers, so use them rather than hand-typing a page's name or a rule's label.
"""

from django import template
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from auctions import palette_routes

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
    route = palette_routes.get_route(url_name)
    if route is None:
        msg = f"{url_name!r} is not in palette_routes.ROUTES"
        raise template.TemplateSyntaxError(msg)
    label = text or route.label
    url = _page_url(route, _help(context))
    if url:
        return format_html('<a class="help-page" href="{}">{}</a>', url, label)
    return format_html('<strong class="help-page">{}</strong>', label)


def _rule_field(name):
    from auctions.forms import AuctionCustomFieldsForm, AuctionEditForm

    return AuctionEditForm.base_fields.get(name) or AuctionCustomFieldsForm.base_fields.get(name)


@register.simple_tag
def rule(name, text=""):
    """An auction rule's label as it appears on the rules page, anchored so other pages can link here."""
    field = _rule_field(name)
    if field is None:
        msg = f"{name!r} is not a field on the auction rules forms"
        raise template.TemplateSyntaxError(msg)
    return format_html('<strong class="help-rule" id="rule-{}">{}</strong>', name, text or field.label or name)


@register.simple_tag(takes_context=True)
def rule_value(context, name):
    """How the reader's auction has ``name`` set, as a help tip -- only for that auction's admins."""
    ctx = _help(context)
    if not (ctx and ctx.auction and ctx.is_admin):
        return ""
    auction = ctx.auction
    display = getattr(auction, f"get_{name}_display", None)
    value = display() if display else getattr(auction, name, None)
    if isinstance(value, bool):
        value = "on" if value else "off"
    elif value in (None, ""):
        value = "blank"
    return format_html(
        '<div class="help-note help-tip"><i class="bi bi-person-check"></i><div>{} has this set to “{}”.</div></div>',
        auction.title,
        value,
    )


class _WrapNode(template.Node):
    def __init__(self, nodelist, opening, closing):
        self.nodelist, self.opening, self.closing = nodelist, opening, closing

    def render(self, context):
        body = self.nodelist.render(context).strip()
        return f"{self.opening}{body}{self.closing}" if body else ""


@register.tag
def helptip(parser, token):
    """``{% helptip %}…{% endhelptip %}``: a sentence about the reader's own auction or club.

    Personal text goes here and nowhere else, so the guide reads the same with every tip removed.
    Renders nothing when the body is blank, so wrap the ``{% if %}`` inside it.
    """
    nodelist = parser.parse(("endhelptip",))
    parser.delete_first_token()
    return _WrapNode(
        nodelist, '<div class="help-note help-tip"><i class="bi bi-person-check"></i><div>', "</div></div>"
    )


@register.tag
def mike(parser, token):
    """``{% mike %}…{% endmike %}``: a short story about Mike, the club member who means well."""
    nodelist = parser.parse(("endmike",))
    parser.delete_first_token()
    return _WrapNode(nodelist, '<aside class="help-mike"><i class="bi bi-emoji-smile"></i><div>', "</div></aside>")
