from django import template

from auctions.fishbase import FISHBASE_VERSION

register = template.Library()


@register.inclusion_tag("auctions/partials/fishbase_citation.html")
def fishbase_citation():
    """Attribution for the species list, shown on every page that offers scientific names."""
    return {"fishbase_version": FISHBASE_VERSION}
