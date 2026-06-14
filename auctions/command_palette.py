"""Shared logic for the command palette.

Single source of truth for the palette's behaviour, imported by the thin JSON views
in ``views.py`` and reused for both the empty-state default items and the phrase->page
shortcut resolution:

  * ``default_items(request)``     -> groups shown when the palette opens with no query
  * ``search(request, q)``         -> grouped search results for a query
  * ``resolve_page(page, user)``   -> expand a ``CommandPalettePage`` row into an item
  * ``log_search(...)``            -> upsert a ``CommandPaletteSearch`` row + bump page hits

Permission/destination helpers are reused from the models and from
``views.check_club_permission`` so the palette stays consistent with the rest of the site.
"""

from urllib.parse import urlencode

from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    Auction,
    Club,
    ClubMember,
    CommandPalettePage,
    CommandPaletteSearch,
    Invoice,
    Lot,
)

# Max results returned per group for live search.
RESULT_LIMIT = 6
# Number of distinct recent searches shown in the default view.
RECENT_SEARCH_LIMIT = 3


def _perm(user, club, name):
    """Wrapper around views.check_club_permission (lazy import avoids a circular import)."""
    from .views import check_club_permission

    return check_club_permission(user, club, name)


def _last_auction(user):
    try:
        return user.userdata.last_auction_used
    except AttributeError:
        return None


def _admin_club(user, last_auction=None):
    """A club this user can administer (view members), preferring the last auction's club."""
    if last_auction is None:
        last_auction = _last_auction(user)
    if last_auction and last_auction.club and _perm(user, last_auction.club, "permission_view"):
        return last_auction.club
    member = (
        ClubMember.objects.filter(user=user, is_deleted=False)
        .filter(Q(permission_admin=True) | Q(permission_view=True))
        .select_related("club")
        .first()
    )
    return member.club if member else None


def _item(type_, title, url, icon, subtitle="", obj_id=None):
    return {"type": type_, "title": title, "subtitle": subtitle, "url": url, "icon": icon, "id": obj_id}


def _auction_ended(auction):
    return auction.closed if auction.is_online else auction.in_person_closed


def _bap_url(club, auction):
    return (
        reverse("club_bap_lots", kwargs={"slug": club.slug})
        + "?"
        + urlencode({"query": f"auction:{auction.slug} pending"})
    )


# --- Dynamic target resolvers ------------------------------------------------
# Each builder takes the user and returns a {url, title, description, icon} dict,
# or None when the destination does not apply to this user (so the shortcut is hidden).


def _t_view_lots(user):
    auction = _last_auction(user)
    if not auction:
        return None
    return {
        "url": auction.view_lot_link,
        "title": f"View lots — {auction.title}",
        "description": "Browse all lots in your most recent auction",
        "icon": "bi-grid",
    }


def _t_view_users(user):
    auction = _last_auction(user)
    if not auction or not auction.permission_check(user):
        return None
    return {
        "url": auction.user_admin_link,
        "title": f"View users — {auction.title}",
        "description": "Manage participants in your most recent auction",
        "icon": "bi-people-fill",
    }


def _t_set_winners(user):
    auction = _last_auction(user)
    if not auction or not auction.permission_check(user) or _auction_ended(auction):
        return None
    return {
        "url": auction.set_lot_winners_link,
        "title": f"Set lot winners — {auction.title}",
        "description": "Record who won each lot",
        "icon": "bi-calendar-check",
    }


def _t_quick_checkout(user):
    auction = _last_auction(user)
    if not auction or not auction.permission_check(user):
        return None
    return {
        "url": reverse("auction_quick_checkout", kwargs={"slug": auction.slug}),
        "title": f"Quick checkout — {auction.title}",
        "description": "Check buyers out and take payment",
        "icon": "bi-bag-heart",
    }


def _t_bap(user):
    auction = _last_auction(user)
    if not auction or not auction.club:
        return None
    club = auction.club
    if not club.enable_breeder_award_program or not _perm(user, club, "permission_manage_bap"):
        return None
    return {
        "url": _bap_url(club, auction),
        "title": f"BAP — {club.name}",
        "description": "Breeder Award Program points for your most recent auction",
        "icon": "bi-award",
    }


def _t_club_members(user):
    club = _admin_club(user)
    if not club:
        return None
    return {
        "url": reverse("club_admin", kwargs={"slug": club.slug}),
        "title": f"Members — {club.name}",
        "description": "Manage your club's members",
        "icon": "bi-people-fill",
    }


def _t_club_brevo(user):
    club = _admin_club(user)
    if not club or not _perm(user, club, "permission_edit_club"):
        return None
    return {
        "url": reverse("club_brevo_config", kwargs={"slug": club.slug}),
        "title": f"Email setup — {club.name}",
        "description": "Connect and configure email for your club",
        "icon": "bi-envelope",
    }


DYNAMIC_TARGETS = {
    "last_auction:view_lots": _t_view_lots,
    "last_auction:view_users": _t_view_users,
    "last_auction:set_winners": _t_set_winners,
    "last_auction:quick_checkout": _t_quick_checkout,
    "last_auction:bap": _t_bap,
    "club:members": _t_club_members,
    "club:brevo": _t_club_brevo,
}


def resolve_page(page, user):
    """Expand a CommandPalettePage into a renderable item dict, or None if not applicable."""
    if page.target:
        builder = DYNAMIC_TARGETS.get(page.target)
        if not builder:
            return None
        resolved = builder(user)
        if not resolved:
            return None
        item = dict(resolved)
    elif page.url:
        item = {"url": page.url, "title": "", "description": "", "icon": ""}
    else:
        return None
    # Admin-provided overrides win over the resolver's defaults.
    if page.title:
        item["title"] = page.title
    if page.description:
        item["description"] = page.description
    if page.icon:
        item["icon"] = page.icon
    if not item.get("title"):
        item["title"] = page.search_term.title()
    if not item.get("icon"):
        item["icon"] = "bi-arrow-right-short"
    return _item("page", item["title"], item["url"], item["icon"], item.get("description", ""), page.pk)


def default_items(request):
    """Groups shown when the palette opens with no query."""
    user = request.user
    groups = []
    primary = []
    auction = _last_auction(user)
    if auction:
        is_admin = auction.permission_check(user)
        ended = _auction_ended(auction)
        primary.append(
            _item(
                "auction", f"View lots — {auction.title}", auction.view_lot_link, "bi-grid", "Your most recent auction"
            )
        )
        if ended and not is_admin:
            invoice = (
                Invoice.objects.filter(auctiontos_user__user=user, auctiontos_user__auction=auction)
                .exclude(status="DRAFT")
                .first()
            )
            if invoice:
                primary.append(
                    _item(
                        "invoice",
                        f"Your invoice — {auction.title}",
                        reverse("invoice_by_pk", kwargs={"pk": invoice.pk}),
                        "bi-bag",
                        "View your invoice",
                        invoice.pk,
                    )
                )
        if is_admin:
            primary.append(
                _item(
                    "auction",
                    f"View users — {auction.title}",
                    auction.user_admin_link,
                    "bi-people-fill",
                    "Manage participants",
                )
            )
            if not auction.is_online and not ended:
                primary.append(
                    _item(
                        "auction",
                        f"Set lot winners — {auction.title}",
                        auction.set_lot_winners_link,
                        "bi-calendar-check",
                        "Record who won each lot",
                    )
                )
            primary.append(
                _item(
                    "auction",
                    f"Quick checkout — {auction.title}",
                    reverse("auction_quick_checkout", kwargs={"slug": auction.slug}),
                    "bi-bag-heart",
                    "Check buyers out",
                )
            )
            if (
                auction.club
                and auction.club.enable_breeder_award_program
                and _perm(user, auction.club, "permission_manage_bap")
            ):
                primary.append(
                    _item(
                        "club",
                        f"BAP — {auction.club.name}",
                        _bap_url(auction.club, auction),
                        "bi-award",
                        "Breeder Award Program",
                    )
                )
    club = _admin_club(user, auction)
    if club:
        primary.append(
            _item(
                "club",
                f"Members — {club.name}",
                reverse("club_admin", kwargs={"slug": club.slug}),
                "bi-people-fill",
                "Manage your club",
            )
        )
    next_auction = (
        Auction.objects.filter(
            club__members__user=user,
            club__members__is_deleted=False,
            date_start__gte=timezone.now(),
            is_deleted=False,
        )
        .order_by("date_start")
        .distinct()
        .first()
    )
    if next_auction:
        primary.append(
            _item(
                "auction",
                f"Upcoming: {next_auction.title}",
                next_auction.get_absolute_url(),
                "bi-calendar-event",
                "Next auction from your clubs",
            )
        )
    try:
        userdata = user.userdata
        if not (userdata.address and userdata.phone_number):
            primary.append(
                _item(
                    "page",
                    "Add your contact info",
                    reverse("contact_info"),
                    "bi-telephone-fill",
                    "Complete your address and phone number",
                )
            )
    except AttributeError:
        pass
    if primary:
        groups.append({"label": "Pick up where you left off", "items": primary})

    recent = []
    seen = set()
    for entry in CommandPaletteSearch.objects.filter(user=user).exclude(search="").order_by("-createdon")[:30]:
        key = entry.search.strip().lower()
        if key and key not in seen:
            seen.add(key)
            recent.append(_item("search", entry.search, "", "bi-clock-history"))
        if len(recent) >= RECENT_SEARCH_LIMIT:
            break
    if recent:
        groups.append({"label": "Recent searches", "items": recent})
    return groups


def _administered_club_ids(user):
    return list(
        ClubMember.objects.filter(user=user, is_deleted=False)
        .filter(Q(permission_admin=True) | Q(permission_view=True))
        .values_list("club_id", flat=True)
    )


def search(request, q):
    """Grouped search results for a query, or the default items when the query is empty."""
    user = request.user
    q = (q or "").strip()
    if not q:
        return default_items(request)
    groups = []
    ql = q.lower()

    # Page shortcuts: matched in Python so we can do bidirectional substring matching
    # ("set winners" matches when the user types "set winners now"). The curated set is small.
    page_items = []
    for page in CommandPalettePage.objects.filter(is_active=True).order_by("-hits"):
        term = page.search_term.lower()
        title = (page.title or "").lower()
        if term in ql or ql in term or (title and ql in title):
            resolved = resolve_page(page, user)
            if resolved:
                page_items.append(resolved)
        if len(page_items) >= RESULT_LIMIT:
            break
    if page_items:
        groups.append({"label": "Go to", "items": page_items})

    auctions = (
        Auction.objects.exclude(is_deleted=True)
        .filter(Q(title__icontains=q) | Q(club__name__icontains=q))
        .select_related("club")
        .order_by("-date_posted")[:RESULT_LIMIT]
    )
    auction_items = [
        _item("auction", a.title, a.get_absolute_url(), "bi-hammer", a.club.name if a.club else "", a.pk)
        for a in auctions
    ]
    if auction_items:
        groups.append({"label": "Auctions", "items": auction_items})

    lots = (
        Lot.objects.exclude(is_deleted=True)
        .filter(Q(lot_name__icontains=q) | Q(summernote_description__icontains=q))
        .select_related("auction")
        .order_by("-date_posted")[:RESULT_LIMIT]
    )
    lot_items = [
        _item("lot", lot.lot_name, lot.get_absolute_url(), "bi-tag", lot.auction.title if lot.auction else "", lot.pk)
        for lot in lots
    ]
    if lot_items:
        groups.append({"label": "Lots", "items": lot_items})

    clubs = (
        Club.objects.filter(active=True)
        .filter(Q(name__icontains=q) | Q(abbreviation__icontains=q))
        .order_by("name")[:RESULT_LIMIT]
    )
    club_items = [
        _item("club", c.name, reverse("club_detail", kwargs={"slug": c.slug}), "bi-people", c.abbreviation or "", c.pk)
        for c in clubs
    ]
    if club_items:
        groups.append({"label": "Clubs", "items": club_items})

    # Club members include email addresses, so only search clubs the user administers.
    if user.is_superuser:
        member_qs = ClubMember.objects.filter(is_deleted=False)
    else:
        club_ids = _administered_club_ids(user)
        member_qs = (
            ClubMember.objects.filter(is_deleted=False, club_id__in=club_ids) if club_ids else ClubMember.objects.none()
        )
    members = member_qs.filter(Q(name__icontains=q) | Q(email__icontains=q)).select_related("club")[:RESULT_LIMIT]
    member_items = [
        _item(
            "clubmember",
            str(m),
            reverse("club_admin", kwargs={"slug": m.club.slug}),
            "bi-person",
            f"{m.club.name} · {m.email}" if m.email else m.club.name,
            m.pk,
        )
        for m in members
    ]
    if member_items:
        groups.append({"label": "Club members", "items": member_items})

    return groups


def log_search(user, *, search_id=None, search="", result=None, result_type="", result_url="", result_object_id=None):
    """Upsert the user's current CommandPaletteSearch row and return its id.

    Keeps a single row per search session (updated as the query is refined) rather than
    one row per keystroke. When a page shortcut is clicked, also bumps that page's hit counter.
    """
    valid_results = dict(CommandPaletteSearch.RESULT_CHOICES)
    if result not in valid_results:
        result = CommandPaletteSearch.RESULT_PENDING

    obj = None
    if search_id:
        obj = CommandPaletteSearch.objects.filter(pk=search_id, user=user).first()
    if obj is None:
        obj = CommandPaletteSearch(user=user)
    if search:
        obj.search = search[:600]
    obj.result = result
    obj.result_type = (result_type or "")[:50]
    obj.result_url = (result_url or "")[:500]
    obj.result_object_id = result_object_id
    obj.save()

    if result == CommandPaletteSearch.RESULT_CLICKED and result_type == "page" and result_object_id:
        CommandPalettePage.objects.filter(pk=result_object_id).update(hits=F("hits") + 1)
    return obj.pk
