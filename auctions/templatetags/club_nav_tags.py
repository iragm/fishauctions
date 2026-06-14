import logging
from urllib.parse import urlencode

from django import template
from django.urls import reverse

register = template.Library()
logger = logging.getLogger(__name__)


@register.inclusion_tag("club_sidebar.html", takes_context=True)
def club_sidebar(context):
    """Render the club navigation sidebar.

    Driven entirely off the current view: `view.club_sidebar_can_view` decides
    whether to render at all, `view.club_sidebar_club` is the club, and
    `view.auction` (when present) provides the in-context auction. Club-level
    sub-links are gated with the same permissions the old club_ribbon used.
    """
    # Lazy import: avoids a circular import at module load (views imports heavily).
    from auctions.views import check_club_permission

    view = context.get("view")
    request = context.get("request")
    user = getattr(request, "user", None)

    can_view = bool(getattr(view, "club_sidebar_can_view", False))
    club = getattr(view, "club_sidebar_club", None)
    if not can_view or not club:
        return {"can_view": False}

    auction = getattr(view, "auction", None)
    # The auction whose admin links populate the sidebar's auction section.
    sidebar_auction = auction or club.current_auction

    def perm(name):
        return check_club_permission(user, club, name)

    can_access_admin = perm("permission_admin") or perm("permission_view")
    can_edit_settings = perm("permission_edit_club")
    can_manage_bap = perm("permission_manage_bap")
    can_manage_money = perm("permission_money") or perm("permission_edit_club")
    can_manage_auctions = perm("permission_admin") or perm("permission_manage_auctions")

    # In managed/check-in mode the auction's participant list is the member list,
    # so "Members" points at the enhanced auction users table (single entry).
    members_through_auction = bool(auction and auction.is_club_managed)

    active_tab = getattr(view, "active_tab", None)
    setup_tabs = {
        "setup",
        "edit",
        "membership",
        "email_settings",
        "bap_settings",
        "discord",
        "mailchimp",
        "brevo",
        "api_keys",
    }

    bap_lots_base = reverse("club_bap_lots", kwargs={"slug": club.slug})
    if sidebar_auction:
        bap_url = bap_lots_base + "?" + urlencode({"query": f"auction:{sidebar_auction.slug} pending"})
    else:
        bap_url = bap_lots_base

    return {
        "can_view": True,
        "club": club,
        "auction": auction,
        "sidebar_auction": sidebar_auction,
        "members_through_auction": members_through_auction,
        "can_access_admin": can_access_admin,
        "can_edit_settings": can_edit_settings,
        "can_manage_bap": can_manage_bap,
        "can_manage_money": can_manage_money,
        "can_manage_auctions": can_manage_auctions,
        "active_tab": active_tab,
        "setup_active": active_tab in setup_tabs,
        "bap_url": bap_url,
    }
