"""Shared logic for the command palette.

Single source of truth for the palette's behaviour, imported by the thin JSON views
in ``views.py`` and reused for both the empty-state default items and the phrase->page
shortcut resolution:

  * ``default_items(request)``     -> groups shown when the palette opens with no query
  * ``search(request, q)``         -> grouped search results for a query
  * ``resolve_page(page, user)``   -> expand a ``CommandPalettePage`` row into item(s)
  * ``log_search(...)``            -> upsert a ``CommandPaletteSearch`` row + bump page hits

A dynamic ``target`` resolver may return several items (e.g. one "Members" link per club
the user administers), so resolvers return *lists*. Permission/destination helpers are reused
from the models, from ``views.check_club_permission`` and from ``filters.rhyming_name_q`` so the
palette stays consistent with the rest of the site.
"""

import re
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    Auction,
    AuctionTOS,
    Club,
    ClubMember,
    CommandPalettePage,
    CommandPaletteSearch,
    Invoice,
    Lot,
)

# Max results returned per group for live search.
RESULT_LIMIT = 6
# Page shortcuts can fan out per club, so allow a few more.
PAGE_LIMIT = 10
# Number of distinct recent searches shown in the default view.
RECENT_SEARCH_LIMIT = 3


def _perm(user, club, name):
    """Wrapper around views.check_club_permission (lazy import avoids a circular import)."""
    from .views import check_club_permission

    return check_club_permission(user, club, name)


def _rhyming_name_q(value, name_field="name"):
    from .filters import rhyming_name_q

    return rhyming_name_q(value, name_field=name_field)


def _last_auction(user):
    try:
        return user.userdata.last_auction_used
    except AttributeError:
        return None


def _auction_visibility_filter(user):
    next_90_days = timezone.now() + timedelta(days=90)
    two_years_ago = timezone.now() - timedelta(days=365 * 2)
    promoted_filter = Q(promote_this_auction=True, date_start__lte=next_90_days, date_posted__gte=two_years_ago)
    if not user.is_authenticated:
        return promoted_filter
    return Q(auctiontos__user=user) | Q(auctiontos__email=user.email) | Q(created_by=user) | promoted_filter


def _visible_auctions(user):
    qs = Auction.objects.exclude(is_deleted=True)
    if user.is_superuser:
        return qs
    return qs.filter(_auction_visibility_filter(user)).distinct()


def _use_bulk_add_lots(auction):
    return bool(auction and not auction.is_online and auction.allow_bulk_adding_lots)


def _member_clubs(user):
    """Every club the user belongs to (for member-facing shortcuts like renewals)."""
    return list(Club.objects.filter(members__user=user, members__is_deleted=False).distinct().order_by("name"))


def _admin_clubs(user):
    """Clubs the user can administer (view members). Includes the last auction's club for creators/superusers."""
    clubs = list(
        Club.objects.filter(members__user=user, members__is_deleted=False)
        .filter(Q(members__permission_admin=True) | Q(members__permission_view=True))
        .distinct()
        .order_by("name")
    )
    auction = _last_auction(user)
    if auction and auction.club and auction.club not in clubs and _perm(user, auction.club, "permission_view"):
        clubs.append(auction.club)
    return clubs


def _admin_auction_ids(user):
    """Auctions the user administers: created by them, admin TOS, or club-managed by a club they administer."""
    ids = set(
        Auction.objects.filter(
            Q(created_by=user) | Q(auctiontos__user=user, auctiontos__is_admin=True),
            is_deleted=False,
        ).values_list("id", flat=True)
    )
    club_ids = [c.id for c in _admin_clubs(user)]
    if club_ids:
        ids.update(Auction.objects.filter(club_id__in=club_ids, is_deleted=False).values_list("id", flat=True))
    return ids


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


def _with_query(url, term):
    """Append ?query=<term> so the destination's filter pre-selects the record the user searched for."""
    return url + "?" + urlencode({"query": term}) if term else url


def _invoice_status_label(invoice):
    return dict(invoice._meta.get_field("status").choices).get(invoice.status, invoice.status)


# --- Dynamic target resolvers ------------------------------------------------
# Each builder takes the user and returns a list of {url, title, description, icon} dicts
# (empty when nothing applies). Lists let one target fan out across every club a user administers.


def _last_auction_admin(user):
    auction = _last_auction(user)
    if auction and auction.permission_check(user):
        return auction
    return None


def _t_view_lots(user):
    auction = _last_auction(user)
    if not auction:
        return []
    return [
        {
            "url": auction.view_lot_link,
            "title": f"View lots — {auction.title}",
            "description": "Browse all lots in your most recent auction",
            "icon": "bi-grid",
        }
    ]


def _t_auction_self(user):
    auction = _last_auction(user)
    if not auction:
        return []
    return [
        {
            "url": auction.url,
            "title": auction.title,
            "description": "Your most recent auction",
            "icon": "bi-hammer",
        }
    ]


def _t_view_users(user):
    auction = _last_auction_admin(user)
    if not auction:
        return []
    return [
        {
            "url": auction.user_admin_link,
            "title": f"View users — {auction.title}",
            "description": "Manage participants in your most recent auction",
            "icon": "bi-people-fill",
        }
    ]


def _t_set_winners(user):
    auction = _last_auction_admin(user)
    # Online auctions pick winners automatically from bids; only in-person, still-open auctions apply.
    if not auction or auction.is_online or _auction_ended(auction):
        return []
    return [
        {
            "url": auction.set_lot_winners_link,
            "title": f"Set lot winners — {auction.title}",
            "description": "Record who won each lot",
            "icon": "bi-calendar-check",
        }
    ]


def _t_quick_checkout(user):
    auction = _last_auction_admin(user)
    if not auction:
        return []
    return [
        {
            "url": reverse("auction_quick_checkout", kwargs={"slug": auction.slug}),
            "title": f"Quick checkout — {auction.title}",
            "description": "Check buyers out and take payment",
            "icon": "bi-bag-heart",
        }
    ]


def _t_add_lot(user):
    auction = _last_auction(user)
    if not auction or _use_bulk_add_lots(auction):
        return []
    return [
        {
            "url": f"{reverse('new_lot')}?auction={auction.slug}",
            "title": f"Add a lot — {auction.title}",
            "description": "Sell a single lot in your most recent auction",
            "icon": "bi-plus-circle",
        }
    ]


def _t_bulk_add_lots(user):
    auction = _last_auction(user)
    if not _use_bulk_add_lots(auction):
        return []
    return [
        {
            "url": reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": auction.slug}),
            "title": f"Bulk add lots — {auction.title}",
            "description": "Add several of your lots at once",
            "icon": "bi-card-list",
        }
    ]


def _t_auction_edit(user):
    auction = _last_auction_admin(user)
    if not auction:
        return []
    return [
        {
            "url": reverse("edit_auction", kwargs={"slug": auction.slug}),
            "title": f"Auction rules & settings — {auction.title}",
            "description": "Edit rules, fees, dates and other settings",
            "icon": "bi-gear",
        }
    ]


def _t_auction_custom_fields(user):
    auction = _last_auction_admin(user)
    if not auction:
        return []
    return [
        {
            "url": reverse("edit_auction_custom_fields", kwargs={"slug": auction.slug}),
            "title": f"Custom fields — {auction.title}",
            "description": "Configure the custom fields shown when adding lots",
            "icon": "bi-input-cursor-text",
        }
    ]


def _t_print_labels(user):
    auction = _last_auction(user)
    if not auction:
        return []
    return [
        {
            "url": reverse("print_my_labels", kwargs={"slug": auction.slug}),
            "title": f"Print labels — {auction.title}",
            "description": "Print labels for your lots in your most recent auction",
            "icon": "bi-printer",
        }
    ]


def _t_label_setup(user):
    auction = _last_auction_admin(user)
    if not auction:
        return []
    return [
        {
            "url": reverse("auction_label_config", kwargs={"slug": auction.slug}),
            "title": f"Label setup — {auction.title}",
            "description": "Choose what prints on your auction's lot labels",
            "icon": "bi-tags",
        }
    ]


def _t_bap(user):
    auction = _last_auction(user)
    if not auction or not auction.club:
        return []
    club = auction.club
    if not club.enable_breeder_award_program or not _perm(user, club, "permission_manage_bap"):
        return []
    return [
        {
            "url": _bap_url(club, auction),
            "title": f"BAP — {club.name}",
            "description": "Breeder Award Program points for your most recent auction",
            "icon": "bi-award",
        }
    ]


def _recent_invoices(user, limit=3):
    return list(Invoice.objects.filter(auctiontos_user__user=user).exclude(status="DRAFT").order_by("-pk")[:limit])


def _t_invoice(user):
    auction = _last_auction(user)
    items = []
    seen = set()
    if auction:
        invoice = (
            Invoice.objects.filter(auctiontos_user__user=user, auctiontos_user__auction=auction)
            .exclude(status="DRAFT")
            .first()
        )
        if invoice:
            seen.add(invoice.pk)
            items.append(
                {
                    "url": reverse("invoice_by_pk", kwargs={"pk": invoice.pk}),
                    "title": f"Invoice — {auction.title}",
                    "description": _invoice_status_label(invoice),
                    "icon": "bi-bag",
                }
            )
    # Otherwise (or in addition) surface the user's most recently created invoices.
    for invoice in _recent_invoices(user):
        if invoice.pk in seen:
            continue
        label = invoice.auctiontos_user.auction.title if invoice.auctiontos_user.auction else "Invoice"
        items.append(
            {
                "url": reverse("invoice_by_pk", kwargs={"pk": invoice.pk}),
                "title": f"Invoice — {label}",
                "description": _invoice_status_label(invoice),
                "icon": "bi-bag",
            }
        )
        if len(items) >= 3:
            break
    return items


def _clubs_items(user, url_name, title_prefix, icon, perm="permission_view", description=""):
    items = []
    for club in _admin_clubs(user):
        if perm and not _perm(user, club, perm):
            continue
        items.append(
            {
                "url": reverse(url_name, kwargs={"slug": club.slug}),
                "title": f"{title_prefix} — {club.name}",
                "description": description,
                "icon": icon,
            }
        )
    return items


def _t_club_members(user):
    return _clubs_items(
        user, "club_admin", "Members", "bi-people-fill", "permission_view", "Manage your club's members"
    )


def _t_club_map(user):
    return _clubs_items(user, "club_member_map", "Member map", "bi-map", "permission_view", "See where members are")


def _t_club_brevo(user):
    return _clubs_items(user, "club_brevo_config", "Email setup (Brevo)", "bi-envelope", "permission_edit_club")


def _t_club_mailchimp(user):
    return _clubs_items(user, "club_mailchimp_config", "Email setup (Mailchimp)", "bi-envelope", "permission_edit_club")


def _t_club_discord(user):
    return _clubs_items(user, "club_discord_config", "Discord setup", "bi-discord", "permission_edit_club")


def _t_club_payments(user):
    return _clubs_items(
        user, "club_membership_settings", "Payment setup", "bi-credit-card", "permission_edit_club", "PayPal / Square"
    )


def _t_clubs_renew(user):
    return [
        {
            "url": reverse("club_membership_pay", kwargs={"slug": club.slug}),
            "title": f"Renew membership — {club.name}",
            "description": "Pay or renew your club membership",
            "icon": "bi-arrow-repeat",
        }
        for club in _member_clubs(user)
    ]


def _t_user_paypal(user):
    return [
        {
            "url": reverse("paypal_seller"),
            "title": "PayPal",
            "description": "Connect or manage your PayPal account",
            "icon": "bi-paypal",
        }
    ]


def _t_user_square(user):
    return [
        {
            "url": reverse("square_seller"),
            "title": "Square",
            "description": "Connect or manage your Square account",
            "icon": "bi-credit-card",
        }
    ]


def _t_account_email(user):
    return [
        {
            "url": reverse("account_email"),
            "title": "Change email",
            "description": "Add or change your email address",
            "icon": "bi-envelope-at",
        }
    ]


def _t_account_google(user):
    return [
        {
            "url": reverse("socialaccount_connections"),
            "title": "Sign in with Google",
            "description": "Connect your Google account",
            "icon": "bi-google",
        }
    ]


def _t_account_password(user):
    return [
        {
            "url": reverse("account_change_password"),
            "title": "Change password",
            "description": "Update your password",
            "icon": "bi-person-fill-lock",
        }
    ]


DYNAMIC_TARGETS = {
    "last_auction:view_lots": _t_view_lots,
    "last_auction:self": _t_auction_self,
    "last_auction:view_users": _t_view_users,
    "last_auction:set_winners": _t_set_winners,
    "last_auction:quick_checkout": _t_quick_checkout,
    "last_auction:add_lot": _t_add_lot,
    "last_auction:bulk_add_lots": _t_bulk_add_lots,
    "last_auction:edit": _t_auction_edit,
    "last_auction:custom_fields": _t_auction_custom_fields,
    "last_auction:print_labels": _t_print_labels,
    "last_auction:label_setup": _t_label_setup,
    "last_auction:bap": _t_bap,
    "last_auction:invoice": _t_invoice,
    "clubs:members": _t_club_members,
    "clubs:map": _t_club_map,
    "clubs:brevo": _t_club_brevo,
    "clubs:mailchimp": _t_club_mailchimp,
    "clubs:discord": _t_club_discord,
    "clubs:payments": _t_club_payments,
    "clubs:renew": _t_clubs_renew,
    "user:paypal": _t_user_paypal,
    "user:square": _t_user_square,
    "account:email": _t_account_email,
    "account:google": _t_account_google,
    "account:password": _t_account_password,
}


def resolve_page(page, user):
    """Return a list of renderable item dicts for a CommandPalettePage (may be empty)."""
    if page.target:
        builder = DYNAMIC_TARGETS.get(page.target)
        resolved = builder(user) if builder else []
    elif page.url:
        resolved = [{"url": page.url, "title": "", "description": "", "icon": ""}]
    else:
        resolved = []
    single = len(resolved) == 1
    items = []
    for raw in resolved:
        data = dict(raw)
        if page.icon:
            data["icon"] = page.icon
        if page.description:
            data["description"] = page.description
        if single and page.title:
            data["title"] = page.title
        if not data.get("title"):
            data["title"] = page.search_term.title()
        if not data.get("icon"):
            data["icon"] = "bi-arrow-right-short"
        items.append(_item("page", data["title"], data["url"], data["icon"], data.get("description", ""), page.pk))
    return items


def _page_phrases(page):
    phrases = [page.search_term]
    if page.synonyms:
        phrases += [s.strip() for s in re.split(r"[,\n]", page.synonyms) if s.strip()]
    return [p.lower() for p in phrases if p]


def _page_items(user, ql):
    items = []
    seen_urls = set()
    for page in CommandPalettePage.objects.filter(is_active=True).order_by("-hits"):
        if any(phrase in ql or ql in phrase for phrase in _page_phrases(page)):
            for item in resolve_page(page, user):
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                items.append(item)
        if len(items) >= PAGE_LIMIT:
            break
    return items[:PAGE_LIMIT]


def _editable_auction_fields():
    """Names of Auction fields that can actually be changed, split by which form owns them.

    Restricting matches to these prevents advertising settings the user can't edit here, such as
    ``paypal_email_address`` (a model field that lives on no form -> "configure paypal email address").
    """
    from .forms import AuctionCustomFieldsForm, AuctionEditForm

    return set(AuctionEditForm.Meta.fields), set(AuctionCustomFieldsForm.Meta.fields)


def _auction_field_items(user, q):
    """Match the query against the verbose name or help text of editable Auction fields.

    Only fields shown on the auction settings or custom fields forms are considered, and a
    verbose-name match wins over a help-text-only match when choosing the field to name.
    """
    auction = _last_auction_admin(user)
    if not auction or len(q) < 3:
        return []
    ql = q.lower()
    edit_fields, custom_fields = _editable_auction_fields()
    editable = edit_fields | custom_fields
    urls = {
        False: (reverse("edit_auction", kwargs={"slug": auction.slug}), "Auction settings"),
        True: (reverse("edit_auction_custom_fields", kwargs={"slug": auction.slug}), "Custom fields"),
    }
    # url -> (item, matched_on_verbose_name) so a precise verbose-name hit can replace a help-text one.
    by_url = {}
    for field in auction._meta.get_fields():
        if field.name not in editable:
            continue
        verbose = str(getattr(field, "verbose_name", "") or "")
        help_text = str(getattr(field, "help_text", "") or "")
        verbose_match = bool(verbose) and ql in verbose.lower()
        help_match = bool(help_text) and ql in help_text.lower()
        if not (verbose_match or help_match):
            continue
        url, label = urls[field.name in custom_fields]
        existing = by_url.get(url)
        if existing is not None and not (verbose_match and not existing[1]):
            continue
        display = verbose or field.name.replace("_", " ")
        by_url[url] = (
            _item("page", f"{label} — {auction.title}", url, "bi-gear", f"Configure “{display}”"),
            verbose_match,
        )
    return [item for item, _ in by_url.values()]


def _is_email(q):
    return "@" in q


def _member_search_items(user, q):
    """Club members, scoped to clubs the user administers. Email match is exact; names use rhyming match."""
    if user.is_superuser:
        member_qs = ClubMember.objects.filter(is_deleted=False)
    else:
        club_ids = [c.id for c in _admin_clubs(user)]
        member_qs = (
            ClubMember.objects.filter(is_deleted=False, club_id__in=club_ids) if club_ids else ClubMember.objects.none()
        )
    if _is_email(q):
        member_qs = member_qs.filter(email__iexact=q)
    else:
        member_qs = member_qs.filter(Q(name__icontains=q) | _rhyming_name_q(q))
    items = []
    for member in member_qs.select_related("club").distinct()[:RESULT_LIMIT]:
        term = member.email if _is_email(q) else (member.name or member.email or "")
        items.append(
            _item(
                "clubmember",
                str(member),
                _with_query(reverse("club_admin", kwargs={"slug": member.club.slug}), term),
                "bi-person",
                f"{member.club.name} · {member.email}" if member.email else member.club.name,
                member.pk,
            )
        )
    return items


def _auctiontos_search_items(user, q):
    """Auction participants the user administers, excluding those tied 1:1 to a club member (shown above)."""
    auction_ids = _admin_auction_ids(user)
    if not auction_ids:
        return []
    tos_qs = AuctionTOS.objects.filter(auction_id__in=auction_ids, clubmember__isnull=True)
    if _is_email(q):
        tos_qs = tos_qs.filter(email__iexact=q)
    else:
        tos_qs = tos_qs.filter(Q(name__icontains=q) | _rhyming_name_q(q))
    items = []
    for tos in tos_qs.select_related("auction").distinct()[:RESULT_LIMIT]:
        term = tos.email if _is_email(q) else (tos.name or tos.email or "")
        items.append(
            _item(
                "auctiontos",
                str(tos.name or tos.email or f"Bidder {tos.bidder_number}"),
                _with_query(reverse("auction_tos_list", kwargs={"slug": tos.auction.slug}), term),
                "bi-person-badge",
                tos.auction.title if tos.auction else "",
                tos.pk,
            )
        )
    return items


def default_items(request):
    """Groups shown when the palette opens with no query."""
    user = request.user
    groups = []
    primary = []
    auction = _last_auction(user)
    if auction:
        is_admin = auction.permission_check(user)
        ended = _auction_ended(auction)
        # A ready/paid invoice for the most recent auction is the most useful thing to surface first.
        ready_invoice = Invoice.objects.filter(
            auctiontos_user__user=user, auctiontos_user__auction=auction, status__in=["UNPAID", "PAID"]
        ).first()
        if ready_invoice:
            primary.append(
                _item(
                    "invoice",
                    f"Your invoice — {auction.title}",
                    reverse("invoice_by_pk", kwargs={"pk": ready_invoice.pk}),
                    "bi-bag-check",
                    _invoice_status_label(ready_invoice),
                    ready_invoice.pk,
                )
            )
        primary.append(
            _item(
                "auction", f"View lots — {auction.title}", auction.view_lot_link, "bi-grid", "Your most recent auction"
            )
        )
        if ended and not is_admin and not ready_invoice:
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
    for club in _admin_clubs(user)[:3]:
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


def search(request, q):
    """Grouped search results for a query, or the default items when the query is empty."""
    user = request.user
    q = (q or "").strip()
    if not q:
        return default_items(request)
    groups = []
    ql = q.lower()

    page_items = _page_items(user, ql) + _auction_field_items(user, q)
    if page_items:
        groups.append({"label": "Go to", "items": page_items[:PAGE_LIMIT]})

    auctions = _visible_auctions(user).filter(Q(title__icontains=q) | Q(club__name__icontains=q)).select_related("club")
    auctions = auctions.order_by("-date_posted")[:RESULT_LIMIT]
    auction_items = [
        _item("auction", a.title, a.get_absolute_url(), "bi-hammer", a.club.name if a.club else "", a.pk)
        for a in auctions
    ]
    if auction_items:
        groups.append({"label": "Auctions", "items": auction_items})

    lots = (
        Lot.objects.exclude(is_deleted=True)
        .exclude(auction__is_deleted=True)
        .filter(auction__in=_visible_auctions(user))
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

    member_items = _member_search_items(user, q)
    if member_items:
        groups.append({"label": "Club members", "items": member_items})

    tos_items = _auctiontos_search_items(user, q)
    if tos_items:
        groups.append({"label": "Auction users", "items": tos_items})

    return groups


def log_search(user, *, search_id=None, search="", result=None, result_type="", result_url="", result_object_id=None):
    """Upsert the user's current CommandPaletteSearch row and return its id.

    Keeps a single row per search session (updated as the query is refined) rather than
    one row per keystroke. ``result`` records the outcome: clicked, abandoned, or bounce
    (no results). When a page shortcut is clicked, also bumps that page's hit counter.
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
