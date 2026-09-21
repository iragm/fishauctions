"""The two tags every emailed template ends with. See :mod:`auctions.email_footer` for why.

Inclusion tags reading the context rather than tags taking arguments: these are used from templates
stored in the database (``post_office.EmailTemplate``), edited by data migrations, and
``{% email_footer %}`` is a line a migration can append to one without knowing what else that
template's send site passes.
"""

from django import template
from django.conf import settings
from django.contrib.sites.models import Site

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
