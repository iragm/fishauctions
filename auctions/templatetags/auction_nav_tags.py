from django import template

from auctions import auction_nav

register = template.Library()


@register.simple_tag(takes_context=True)
def auction_nav_groups(context, auction):
    """`auction_nav.groups_for` with this page's `active_tab`."""
    return auction_nav.groups_for(auction, context.get("active_tab"))
