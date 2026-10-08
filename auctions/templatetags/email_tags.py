"""Tags for emailed templates: the footer, the club header, the button and the greeting's first name.

Every emailed HTML template extends ``email/base.html``, which ends with ``{% email_footer %}``. See
:mod:`auctions.email_footer` for why the footer is on all of them.

Tags reading the context rather than taking arguments: these are used from templates stored in the
database (``post_office.EmailTemplate``), edited by data migrations, and a migration can add one to a
template without knowing what else that template's send site passes.
"""

import re

from django import template
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.utils.html import escape
from django.utils.safestring import mark_safe

from auctions.email_footer import mailing_address

register = template.Library()


def _footer_context(context):
    # NAVBAR_BRAND, not Site.name: the brand is what the site calls itself everywhere else, and
    # Site.name is left at "example.com" on a deployment that only ever set the domain.
    return {
        "site_name": settings.NAVBAR_BRAND,
        "domain": context.get("domain") or Site.objects.get_current().domain,
        "mailing_address": mailing_address(),
        "unsubscribe": context.get("unsubscribe") or "",
    }


@register.inclusion_tag("email/footer.html", takes_context=True)
def email_footer(context):
    """The HTML footer."""
    return _footer_context(context)


@register.inclusion_tag("email/footer.txt", takes_context=True)
def email_footer_text(context):
    """The plain-text footer."""
    return _footer_context(context)


@register.filter
def first_name(name):
    """First word of a full name, for "Hey Jamie,". Same rule as ``ClubMember.first_name``.

    "Unknown" is what ``AuctionTOS.save`` stores for a blank name, so it counts as none: "Hey there,".
    """
    first = str(name or "").strip().split(" ", 1)[0]
    return "" if first == "Unknown" else first


#: The site's primary color, which is white text at 7:1. Inline, since some clients drop ``<style>``.
BUTTON_CELL_STYLE = "border-radius:6px;background-color:#375a7f;"
BUTTON_LINK_STYLE = (
    "display:inline-block;padding:12px 22px;border-radius:6px;background-color:#375a7f;color:#ffffff;"
    "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:16px;"
    "font-weight:600;line-height:1.2;text-decoration:none;"
)


def button_html(link):
    """Wrap an ``<a>`` as a button: a table cell carries the fill, so Outlook's Word renderer shows it."""
    link = re.sub(r"<a\b", f'<a style="{BUTTON_LINK_STYLE}"', link.strip(), count=1)
    return mark_safe(  # noqa: S308 - link is already-rendered template output
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px;">'
        f'<tr><td style="{BUTTON_CELL_STYLE}">{link}</td></tr></table>'
    )


def link_button(href, label):
    """:func:`button_html` for a send site building HTML in Python."""
    return button_html(f'<a href="{escape(href)}">{escape(label)}</a>')


class ButtonNode(template.Node):
    def __init__(self, nodelist):
        self.nodelist = nodelist

    def render(self, context):
        return button_html(self.nodelist.render(context))


@register.tag
def email_button(parser, token):
    """``{% email_button %}<a href="...">Label</a>{% endemail_button %}``.

    A block around an ordinary link rather than a tag taking a URL: the links in these templates are
    built from several variables, and a block keeps them readable and editable in the admin.
    """
    nodelist = parser.parse(("endemail_button",))
    parser.delete_first_token()
    return ButtonNode(nodelist)


def club_for_email(context):
    """The club an email is about, from whatever the send site passed: a club, auction, tos, invoice or lot."""
    if context.get("club"):
        return context["club"]
    auction = context.get("auction")
    for holder in ("tos", "invoice", "lot"):
        if auction is None and context.get(holder) is not None:
            auction = getattr(context[holder], "auction", None)
    return getattr(auction, "club", None)


def club_icon_url(club, domain):
    """Absolute URL of the club's square icon, or "" without one.

    Cached: a promo renders this once per recipient, and a local icon's URL is a thumbnail lookup. The
    key names the file and the Cloudflare id, so a new icon is a new key rather than a stale URL.
    """
    if not club:
        return ""
    if getattr(club, "pk", None):
        key = f"email_club_icon:{club.pk}:{club.icon.name}:{club.cloudflare_image_id}"
        url = cache.get_or_set(key, lambda: club.icon_thumbnail_url or "", 60 * 60 * 24)
    else:
        url = club.icon_thumbnail_url or ""
    if url.startswith("/"):
        url = f"https://{domain}{url}"
    return url


@register.inclusion_tag("email/club_header.html", takes_context=True)
def email_club_header(context):
    """The club's logo and name above the email, for a club that has a logo; nothing otherwise."""
    club = club_for_email(context)
    domain = context.get("domain") or Site.objects.get_current().domain
    icon_url = club_icon_url(club, domain)
    return {"club": club if icon_url else None, "icon_url": icon_url}


#: The survey's two buttons. Success needs dark text and danger white, as on the site (style_reference.md).
SURVEY_BUTTON_COLORS = {"great": ("#00bc8c", "#222222"), "not_fun": ("#a93226", "#ffffff")}


def _survey_links(context, mode):
    from auctions.auction_survey import email_links

    domain = context.get("domain") or Site.objects.get_current().domain
    return email_links(context.get("invoice"), mode, domain)


@register.simple_tag(takes_context=True)
def email_survey(context, mode):
    """``{% email_survey "invoice" %}``: "How was <auction>?" and its two buttons, when the auction asks
    that way (``Auction.post_auction_survey``) and the person hasn't answered; nothing otherwise.
    """
    links = _survey_links(context, mode)
    if not links:
        return ""
    from auctions.models import AuctionTOS

    cells = []
    for answer, label in AuctionTOS.SURVEY_ANSWERS:
        fill, text = SURVEY_BUTTON_COLORS[answer]
        style = BUTTON_LINK_STYLE.replace(
            "background-color:#375a7f;color:#ffffff;", f"background-color:{fill};color:{text};"
        )
        cells.append(
            f'<td style="border-radius:6px;background-color:{fill};">'
            f'<a href="{escape(links[answer])}" style="{style}">{escape(label)}</a></td><td style="width:12px;"></td>'
        )
    return mark_safe(  # noqa: S308 - every value is escaped above
        f'<p style="font-weight:600;margin:24px 0 8px;">{escape(links["question"])}</p>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;"><tr>'
        + "".join(cells)
        + "</tr></table>"
    )


@register.simple_tag(takes_context=True)
def email_survey_text(context, mode):
    """:func:`email_survey` for the plain-text part."""
    links = _survey_links(context, mode)
    if not links:
        return ""
    from auctions.models import AuctionTOS

    lines = [links["question"], *(f"{label} {links[answer]}" for answer, label in AuctionTOS.SURVEY_ANSWERS)]
    # Safe: plain text, where escaping would turn the query string's & into &amp;.
    return mark_safe("\n".join(lines) + "\n\n")  # noqa: S308
