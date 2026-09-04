"""A club's public page, and the two links that identify a member on it.

``ClubDetailView`` is what a club hands out as its address here. The by-UUID and by-number views
below it are what a membership card's barcode resolves to.
"""

import logging
import re
from urllib.parse import quote_plus

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.generic import TemplateView

from auctions import announcements, club_events
from auctions.filters import (
    ClubMemberFilter,
)
from auctions.forms import (
    ClubMemberSelfServiceForm,
)
from auctions.models import (
    Auction,
    BapAward,
    ClubHistory,
    ClubMember,
    Invoice,
)
from auctions.tables import (
    ClubMemberHTMxTable,
)

from .base import (
    CLUB_DETAIL_EVENT_LIMIT,
    CLUB_DETAIL_PAST_EVENT_LIMIT,
    ClubViewMixin,
    HTMxTableView,
    _bap_leaderboard,
    _club_points_chart_data,
    _club_top10_chart_data,
    _last_n_month_starts,
    _process_invoice_membership_renewal,
    _ytd_month_starts,
)

logger = logging.getLogger(__name__)


# Club management views
class ClubDetailView(ClubViewMixin, TemplateView):
    """User self-service page for a club"""

    active_tab = "home"
    template_name = "auctions/club_detail.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        member = None
        if self.request.user.is_authenticated:
            member = ClubMember.objects.filter(club=self.club, user=self.request.user, is_deleted=False).first()
        requested_member_uuid = self.request.GET.get("user", "")
        if requested_member_uuid:
            member = ClubMember.objects.filter(club=self.club, uuid=requested_member_uuid, is_deleted=False).first()
        context["member"] = member
        # Only the actual owner — not a holder of the UUID renewal link — may see the
        # Google Wallet save button, since adding to a wallet should never be done on
        # behalf of someone else.
        context["is_membership_owner"] = bool(
            member and self.request.user.is_authenticated and member.user_id == self.request.user.id
        )
        from auctions.apple_wallet import is_configured as _apple_configured

        context["apple_wallet_enabled"] = _apple_configured()
        if member:
            context["update_form"] = ClubMemberSelfServiceForm(instance=member)
        club = self.club
        requested_tab = (self.kwargs.get("tab") or self.request.GET.get("tab") or "auctions").lower()
        available_tabs = {"auctions"}
        if club.enable_breeder_award_program:
            context["show_bap_tabs"] = True
            context["bap_leaderboard"] = _bap_leaderboard(club, "bap_points", member)
            context["bap_leaderboard_ytd"] = _bap_leaderboard(club, "bap_points_ytd", member)
            available_tabs.add("bap")
            context["hap_leaderboard"] = _bap_leaderboard(club, "hap_points", member) if club.separate_hap else []
            context["hap_leaderboard_ytd"] = (
                _bap_leaderboard(club, "hap_points_ytd", member) if club.separate_hap else []
            )
            if club.separate_hap:
                available_tabs.add("hap")
            context["culture_leaderboard"] = (
                _bap_leaderboard(club, "culture_points", member) if club.separate_cap else []
            )
            context["culture_leaderboard_ytd"] = (
                _bap_leaderboard(club, "culture_points_ytd", member) if club.separate_cap else []
            )
            if club.separate_cap:
                available_tabs.add("culture")
            context["can_manage_bap"] = self.user_has_club_permission("permission_manage_bap")
            context["my_bap_awards"] = None
            context["my_points_chart_data"] = None
            if member:
                context["my_bap_awards"] = BapAward.objects.filter(club_member=member).order_by("-date", "-pk")[:25]
                context["my_points_chart_data"] = _club_points_chart_data(club, member)
                available_tabs.add("my-points")
            alltime_months = _last_n_month_starts(60)
            ytd_months = _ytd_month_starts()
            context["bap_top10_chart"] = _club_top10_chart_data(club, "bap_points", "points", member, alltime_months)
            context["bap_top10_chart_ytd"] = _club_top10_chart_data(
                club, "bap_points_ytd", "points", member, ytd_months, is_ytd=True
            )
            if club.separate_hap:
                context["hap_top10_chart"] = _club_top10_chart_data(
                    club, "hap_points", "hap_points", member, alltime_months
                )
                context["hap_top10_chart_ytd"] = _club_top10_chart_data(
                    club, "hap_points_ytd", "hap_points", member, ytd_months, is_ytd=True
                )
            if club.separate_cap:
                context["cap_top10_chart"] = _club_top10_chart_data(
                    club, "culture_points", "cap_points", member, alltime_months
                )
                context["cap_top10_chart_ytd"] = _club_top10_chart_data(
                    club, "culture_points_ytd", "cap_points", member, ytd_months, is_ytd=True
                )
        else:
            context["show_bap_tabs"] = False
            # Legacy flat leaderboard for clubs without BAP enabled
            has_points = (
                ClubMember.objects.filter(club=club, is_deleted=False)
                .filter(Q(bap_points__gt=0) | Q(hap_points__gt=0))
                .exists()
            )
            context["has_points"] = has_points
            if has_points:
                context["bap_leaderboard"] = ClubMember.objects.filter(
                    club=club, is_deleted=False, bap_points__gt=0
                ).order_by("-bap_points")[:10]
                context["hap_leaderboard"] = ClubMember.objects.filter(
                    club=club, is_deleted=False, hap_points__gt=0
                ).order_by("-hap_points")[:10]
        context["active_club_tab"] = requested_tab if requested_tab in available_tabs else "auctions"
        # Four or more tabs ("Events BAP HAP Culture My Points") run off the side of a phone, so
        # everything past BAP moves into a More menu -- the same shape the auction ribbon uses.
        # Three still fit, and a More menu holding one item is worse than the tab it replaced.
        context["club_tabs_overflow"] = len(available_tabs) > 3
        context["can_access_admin"] = self.user_has_club_permission(
            "permission_admin"
        ) or self.user_has_club_permission("permission_view")
        context["can_edit_settings"] = self.user_has_club_permission("permission_edit_club")
        can_manage_auctions = self.user_has_club_permission("permission_admin") or self.user_has_club_permission(
            "permission_manage_auctions"
        )
        context["can_manage_auctions"] = can_manage_auctions
        context["now"] = timezone.now()
        context["current_auction_id"] = self.club.current_auction_id
        membership_expiration_date = member.membership_expiration_date if member else None
        context["membership_expiration_date"] = membership_expiration_date
        membership_invoice = None
        show_membership_payment_button = False
        is_expired = False
        expiring_soon = False
        is_paid_member = False
        if member:
            _process_pending_membership_renewal_for_member(self.club, member)
            member.refresh_from_db()
            is_expired, expiring_soon, should_show_payment, _ = _membership_renewal_state(self.club, member)
            is_paid_member = member.is_paid_member
            if should_show_payment:
                membership_invoice = _get_or_create_membership_invoice(self.club, member)
                show_membership_payment_button = True
        context["membership_invoice"] = membership_invoice
        context["show_membership_payment_button"] = show_membership_payment_button
        context["membership_is_expired"] = is_expired
        context["membership_expiring_soon"] = expiring_soon
        context["membership_is_paid_member"] = is_paid_member
        show_club_map = self.club.latitude is not None and self.club.longitude is not None
        context["show_club_map"] = show_club_map
        if show_club_map:
            context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
            directions_query = self.club.location or f"{self.club.latitude},{self.club.longitude}"
            context["club_map_directions_url"] = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
                directions_query
            )
        # The club page shows a calendar, not a bare auction list: auctions are mirrored into
        # ClubEvents (by signal, and by the periodic task) alongside meetings, swaps, and
        # anything pulled in from the club's Google Calendar.
        upcoming, past = club_events.upcoming_events(
            self.club, limit=CLUB_DETAIL_EVENT_LIMIT, include_past=True, past_limit=CLUB_DETAIL_PAST_EVENT_LIMIT
        )
        context["upcoming_events"] = upcoming
        context["past_events"] = past
        # The one announcement worth putting at the top of the page. Only the "show on website"
        # ones are eligible; see announcements.latest_for_website.
        latest = announcements.latest_for_website(self.club, 1)
        context["latest_announcement"] = latest[0] if latest else None
        # The club's own page here counts as "on your website" -- it is what the globe icon on the
        # announcements page has always meant. Admins are not counted: somebody reloading the page
        # they just posted from would otherwise be most of the number.
        if latest and not self.club_sidebar_can_view:
            announcements.record_website_views(latest)
        context["has_any_events"] = bool(upcoming or past)
        # Both of these *subscribe*, so the calendar keeps updating: webcal:// hands the feed to
        # the desktop or phone calendar app, and Google takes the https URL through its
        # "add by URL" screen. There's deliberately no plain link to the .ics — a relative one
        # only downloads the file, which is a one-time import of events that then never changes.
        absolute_ical_url = self.request.build_absolute_uri(
            reverse("club_events_ical", kwargs={"slug": self.club.slug})
        )
        context["club_ical_subscribe_url"] = re.sub(r"^https?://", "webcal://", absolute_ical_url)
        context["club_ical_google_url"] = "https://calendar.google.com/calendar/r?cid=" + quote_plus(absolute_ical_url)
        if can_manage_auctions:
            # Admins still get the unpromoted auctions, which never become calendar events.
            context["unpromoted_auctions"] = Auction.objects.filter(
                club=self.club, is_deleted=False, promote_this_auction=False
            ).order_by("-date_start")[:CLUB_DETAIL_EVENT_LIMIT]
        # Email button: visible to authenticated users when someone can be reached at this club.
        from auctions.email_routing import email_routing_enabled

        if email_routing_enabled():
            # SES routing active: show button only when a real recipient is configured
            # (permission_add_edit member, admin, or manual override).
            contact_recipient = self.club.contact_email_recipient
            has_club_contact = bool(contact_recipient)
            context["club_contact_email"] = self.club.contact_sender_email if has_club_contact else None
        else:
            # No SES: rely on the plain contact_email address field.
            has_club_contact = bool(self.club.contact_email)
            context["club_contact_email"] = self.club.contact_email or None
        context["has_club_contact"] = has_club_contact
        return context

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        action = request.POST.get("action", "join")
        if action == "make_current":
            can_manage_auctions = self.user_has_club_permission("permission_admin") or self.user_has_club_permission(
                "permission_manage_auctions"
            )
            if can_manage_auctions:
                auction = Auction.objects.filter(
                    pk=request.POST.get("auction"), club=self.club, is_deleted=False
                ).first()
                if auction:
                    self.club.current_auction = auction
                    self.club.save(update_fields=["current_auction"])
                    messages.success(request, f"{auction} is now the current auction.")
            return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))
        if action == "update":
            member = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
            if member:
                form = ClubMemberSelfServiceForm(request.POST, instance=member)
                if form.is_valid():
                    form.save()
                    messages.success(request, "Your info has been updated.")
            return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))
        # join logic
        if not self.club.allow_joining:
            messages.error(request, "This club is not accepting new members right now.")
            return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))
        existing = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
        if existing:
            messages.info(request, "You are already a member of this club.")
        else:
            ClubMember.objects.create(
                club=self.club,
                user=request.user,
                name=f"{request.user.first_name} {request.user.last_name}".strip(),
                email=request.user.email,
                source="joined",
                # The member made this row about themselves: until an admin edits it, it goes away
                # with their account rather than staying in the club's records.
                admin_edited=False,
            )
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"{request.user.get_full_name()} joined the club",
                applies_to="MEMBERS",
            )
            messages.success(request, f"You have joined {self.club.name}!")
        return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))


def _get_or_create_membership_invoice(club, member):
    """Find or create an unpaid renewal invoice for this member's club."""
    # Match on club_member first: that is what we set when creating the invoice below, so this
    # is the only lookup guaranteed to find a previously created renewal invoice. Without it,
    # members with an email but no linked user account never match the lookups below and a new
    # UNPAID invoice is created on every page view.
    invoice = Invoice.objects.filter(
        club=club,
        auction=None,
        club_member=member,
        renewal_processed=False,
        status="UNPAID",
    ).first()
    if invoice is None and member.email:
        invoice = Invoice.objects.filter(
            club=club,
            auction=None,
            auctiontos_user__email__iexact=member.email,
            renewal_processed=False,
            status="UNPAID",
        ).first()
    if invoice is None and member.user:
        invoice = Invoice.objects.filter(
            club=club,
            auction=None,
            buyer=member.user,
            renewal_processed=False,
            status="UNPAID",
        ).first()
    if invoice is None:
        invoice = Invoice.objects.create(
            club=club,
            club_member=member,
            buyer=member.user or None,
            status="UNPAID",
            renewal_needed=True,
        )
    return invoice


def _membership_renewal_state(club, member):
    """Return (is_expired, expiring_soon, should_show_payment, can_pay)."""
    today = timezone.now().date()
    expiration = member.membership_expiration_date
    is_expired = bool(expiration and expiration < today) or (not expiration and not member.is_paid_member)
    expiring_soon = bool(expiration and not is_expired and (expiration - today).days <= 30)
    can_pay = bool(club.membership_annual_fee and (club.can_accept_paypal or club.can_accept_square))
    should_show_payment = can_pay and (is_expired or expiring_soon or not member.is_paid_member)
    return is_expired, expiring_soon, should_show_payment, can_pay


def _process_pending_membership_renewal_for_member(club, member):
    """Process any PAID-but-unprocessed renewal invoice for this member.

    Called on member page load so Square payments (webhook may arrive after redirect)
    are picked up synchronously when the member views their page.
    """
    if not member.user:
        return
    paid_invoice = Invoice.objects.filter(
        club=club,
        buyer=member.user,
        renewal_needed=True,
        renewal_processed=False,
        status="PAID",
    ).first()
    if paid_invoice:
        _process_invoice_membership_renewal(paid_invoice)


class ClubMemberByUUIDView(ClubViewMixin, TemplateView):
    """Public, UUID-keyed page that shows a member's name and wallet-add buttons.

    Anyone with the UUID link can view this page and add the membership to their
    Google/Apple wallet — the UUID is the capability token.
    """

    template_name = "auctions/club_member_by_uuid.html"
    allow_non_admins = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        member = get_object_or_404(ClubMember, club=self.club, uuid=kwargs["uuid"], is_deleted=False)
        _process_pending_membership_renewal_for_member(self.club, member)
        member.refresh_from_db()
        member.update_last_club_activity()
        from auctions.apple_wallet import is_configured as _apple_configured

        context["club"] = self.club
        context["member"] = member
        context["apple_wallet_enabled"] = _apple_configured()

        is_expired, expiring_soon, should_show_payment, _ = _membership_renewal_state(self.club, member)
        context["membership_invoice"] = (
            _get_or_create_membership_invoice(self.club, member) if should_show_payment else None
        )
        context["is_expired"] = is_expired
        context["expiring_soon"] = expiring_soon
        context["is_paid_member"] = member.is_paid_member
        return context


class ClubMemberByNumberView(ClubViewMixin, TemplateView):
    """Public, number-keyed page showing membership number, expiration status, and a
    payment button when applicable. Linked from Discord."""

    template_name = "auctions/club_member_by_number.html"
    allow_non_admins = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        member = get_object_or_404(ClubMember, club=self.club, membership_number=kwargs["number"], is_deleted=False)
        _process_pending_membership_renewal_for_member(self.club, member)
        member.refresh_from_db()
        is_expired, expiring_soon, should_show_payment, _ = _membership_renewal_state(self.club, member)
        context["club"] = self.club
        context["member"] = member
        context["expiration"] = member.membership_expiration_date
        context["is_expired"] = is_expired
        context["expiring_soon"] = expiring_soon
        context["is_paid_member"] = member.is_paid_member
        context["membership_invoice"] = (
            _get_or_create_membership_invoice(self.club, member) if should_show_payment else None
        )
        return context


class ClubAdminView(LoginRequiredMixin, ClubViewMixin, HTMxTableView):
    """Admin panel for a club"""

    active_tab = "members"
    model = ClubMember
    table_class = ClubMemberHTMxTable
    filterset_class = ClubMemberFilter
    template_name = "auctions/club_admin.html"
    htmx_table_header_template = "auctions/partials/club_admin_table_header.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not request.user.is_superuser:
            member = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
            if not member or not any(
                [
                    member.permission_admin,
                    member.permission_view,
                    member.permission_export,
                    member.permission_add_edit,
                    member.permission_edit_club,
                    member.permission_manage_auctions,
                    member.permission_manage_bap,
                ]
            ):
                raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # is_deleted filtering is handled by ClubMemberFilter.filter_queryset (default: hide deactivated)
        # Every row reads its club's membership fee to decide whether to show a Renew button.
        # prefetch, not join: all the rows have the same club, so this way they share one instance
        # of it -- and one copy of everything cached on it.
        return (
            ClubMember.objects.filter(club=self.club).select_related("user").prefetch_related("club").order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["can_edit"] = self.user_has_club_permission("permission_edit_club")
        context["can_export"] = self.user_has_club_permission("permission_export")
        context["can_add_edit"] = self.user_has_club_permission("permission_add_edit")
        context["can_edit_bap"] = self.user_has_club_permission("permission_manage_bap")
        query = (self.request.GET.get("query") or "").strip()
        filterset = context.get("filter")
        filtered_empty = filterset is not None and query and not filterset.qs.exists()
        if filtered_empty and "deactivated" not in query.lower().split():
            context["no_results"] = self._build_no_results_html(query, context["can_add_edit"])
        return context

    def _build_no_results_html(self, query, can_add_edit):
        """Empty-state with a link to expand the search to deactivated members and (optionally) an
        Add member button pre-populated from the search query."""
        from urllib.parse import urlencode

        from django.utils.html import format_html

        deactivated_query = f"{query} deactivated"
        deactivated_qs = ClubMemberFilter({"query": deactivated_query}, queryset=self.get_queryset()).qs
        deactivated_count = deactivated_qs.count()

        bits = [
            format_html(
                '<p class="text-muted mb-2">No active members match <strong>{}</strong>.</p>',
                query,
            )
        ]
        if deactivated_count:
            link_params = self.request.GET.copy()
            link_params["query"] = deactivated_query
            link_href = f"?{urlencode(link_params)}"
            bits.append(
                format_html(
                    '<a href="{}" class="btn btn-sm btn-danger me-2">'
                    '<i class="bi bi-archive"></i> Show {} deactivated</a>',
                    link_href,
                    deactivated_count,
                )
            )
        if can_add_edit:
            import re as _re

            params = {}
            digits_only = _re.sub(r"\D", "", query)
            if len(digits_only) >= 7:
                params["phone"] = query
            elif "@" in query:
                params["email"] = query
            elif _re.fullmatch(r"[A-Za-z\s\-'.]+", query) and len(query) >= 2:
                params["name"] = query
            create_url = reverse("clubmember_create", kwargs={"slug": self.club.slug})
            if params:
                create_url += f"?{urlencode(params)}"
            bits.append(
                format_html(
                    '<button class="btn btn-info btn-sm" '
                    'hx-get="{}" hx-target="#modals-here" hx-trigger="click" '
                    '_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    '<i class="bi bi-person-fill-add"></i> Add member</button>',
                    create_url,
                )
            )
        body = "".join(str(b) for b in bits)
        return format_html('<div class="text-center py-3">{}</div>', mark_safe(body))

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["can_add_edit"] = self.user_has_club_permission("permission_add_edit")
        kwargs["can_manage_permissions"] = self.user_has_club_permission("permission_admin")
        kwargs["club_has_fee"] = bool(self.club.membership_annual_fee)
        kwargs["can_manage_discord"] = bool(
            self.club.discord_server_id
            and (
                self.user_has_club_permission("permission_admin")
                or self.user_has_club_permission("permission_edit_club")
            )
        )
        # Column visibility uses direct field checks — permission_admin alone doesn't reveal all columns
        if self.request.user.is_superuser:
            kwargs["can_manage_bap"] = True
            kwargs["can_manage_membership"] = True
            kwargs["can_manage_auctions"] = True
            kwargs["can_manage_discord"] = bool(self.club.discord_server_id)
        else:
            member = ClubMember.objects.filter(club=self.club, user=self.request.user, is_deleted=False).first()
            kwargs["can_manage_bap"] = bool(member and member.permission_manage_bap)
            kwargs["can_manage_membership"] = bool(member and member.permission_add_edit)
            kwargs["can_manage_auctions"] = bool(member and member.permission_manage_auctions)
        return kwargs

    def get_possible_filters(self):
        """Clickable chips that inject ClubMemberFilter search tokens, modeled on the auction
        users page. Each chip's key is normalized (underscores -> spaces) into a search token."""
        filters = []
        # Membership status only exists when the club charges dues (a 0 fee means no membership
        # system, so hide the paid/unpaid chips).
        if self.club.membership_annual_fee:
            filters.extend(
                [
                    ("<small class='text-muted'>Membership:</small>", ""),
                    ("<i class='bi bi-person-badge'></i> Paid club member", "current"),
                    ("<i class='bi bi-person'></i> Unpaid", "expired"),
                    ("<i class='bi bi-hourglass-split'></i> Expiring soon", "expiring"),
                    ("<i class='bi bi-person-x'></i> Never paid", "never"),
                ]
            )
        filters.append(("<small class='text-muted'>Source:</small>", ""))
        filters.extend(
            [
                ("<i class='bi bi-globe'></i> Website signup", "joined"),
                ("<i class='bi bi-pencil'></i> Manually added", "manual"),
            ]
        )
        if self.club.discord_server_id:
            filters.append(("<i class='bi bi-discord'></i> Discord", "discord"))
        filters.append(("<small class='text-muted'>Other:</small>", ""))
        filters.append(("<i class='bi bi-people-fill'></i> Possible duplicate", "duplicate"))
        if self.club.mailchimp_connected:
            filters.append(("<i class='bi bi-envelope-exclamation'></i> Not in Mailchimp", "nonmailchimp"))
        if self.club.brevo_connected:
            filters.append(("<i class='bi bi-envelope-exclamation'></i> Not in Brevo", "nonbrevo"))
        filters.append(("<i class='bi bi-archive'></i> Deactivated", "deactivated"))
        return filters
