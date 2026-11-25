from django import template

from auctions import auction_nav

register = template.Library()


@register.simple_tag(takes_context=True)
def auction_nav_groups(context, auction):
    """`auction_nav.groups_for` with this page's `active_tab`."""
    return auction_nav.groups_for(auction, context.get("active_tab"))


#: Auction Fish in ChatGPT, the same link as the AI agents guide's.
CHATGPT_APP_URL = "https://chatgpt.com/plugins/plugin_asdk_app_6ab14fc4a1c88191a16742cc75c9c14e"


@register.inclusion_tag("ai_agent_banner.html", takes_context=True)
def ai_agent_banner(context, kind="auction"):
    """Where the AI box was on the auction and club admin pages: a pointer to ChatGPT and Claude, for
    someone who hasn't connected either, and hasn't said they're not interested. ``kind`` is "auction" or
    "club"."""
    from auctions.mcp import auth as mcp_auth

    request = context.get("request")
    user = getattr(request, "user", None)
    signed_in = bool(user) and user.is_authenticated
    if signed_in and getattr(getattr(user, "userdata", None), "hide_ai_agent_banner", False):
        return {"show": False}
    return {
        "show": bool(user) and not mcp_auth.has_ai_agent(user),
        "kind": kind,
        "chatgpt_url": CHATGPT_APP_URL,
        # "Not interested" is stored on the account, so it is offered only to someone signed in.
        "can_hide": signed_in,
    }


@register.inclusion_tag("club_auction_banner.html", takes_context=True)
def club_auction_banner(context, club):
    """On every club page: "Create a new club auction", for someone who may (services.can_start_club_auction)
    while the club has no current one (services.club_needs_an_auction)."""
    from auctions import services

    request = context.get("request")
    user = getattr(request, "user", None)
    show = bool(
        club
        and getattr(club, "pk", None)
        and user is not None
        and services.can_start_club_auction(user, club)
        and services.club_needs_an_auction(club)
    )
    return {"show": show, "club": club}
