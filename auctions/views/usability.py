"""The usability reports, and linking auctions to clubs.

:class:`AdminUsability`, :class:`AdminFreeTextUsage` and :class:`AdminSessionReplay` have no URL: the
admin MCP endpoint's ``read_admin_page`` renders them (``mcp.admin.MCP_ONLY_PAGES``), for the agents
that read them, and nobody else.

:class:`UnlinkedAuctions` proposes club links for auctions with none (:mod:`auctions.club_matching`),
since only about a fifth of auctions have a club.
"""

import logging

from django.contrib import messages
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from auctions import club_health, club_matching, free_text_usage, lifecycle, usability_report
from auctions.models import Auction, Club
from auctions.services import link_auction_to_club

from .base import AdminOnlyViewMixin

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30


class AdminUsability(AdminOnlyViewMixin, TemplateView):
    """Where people reach, where they get stuck, and which settings anyone has changed."""

    template_name = "dashboard_usability.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            days = int(self.request.GET.get("days", DEFAULT_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_DAYS
        days = max(1, min(days, 400))
        context["days"] = days
        context["reach"] = usability_report.reach_by_route(days=days)
        # Not windowed: a funnel spans a whole auction.
        context["funnel"] = usability_report.buyer_funnel()
        context["friction"] = usability_report.friction_by_form(days=days)
        return context


#: Unlinked auctions per page, bounded by rendering cost.
UNLINKED_PAGE_SIZE = 100


class UnlinkedAuctions(AdminOnlyViewMixin, TemplateView):
    """Auctions with no club, grouped by their likely club."""

    template_name = "dashboard_unlinked_auctions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        unlinked = Auction.objects.filter(club__isnull=True, is_deleted=False).select_related("created_by")
        context["unlinked_total"] = unlinked.count()
        context["linked_total"] = Auction.objects.filter(club__isnull=False, is_deleted=False).count()
        page = list(unlinked.order_by("-date_start", "-pk")[:UNLINKED_PAGE_SIZE])
        clubs = list(Club.objects.all().order_by("name"))
        suggestions = club_matching.suggest_clubs(page, clubs)
        # Keyed by confidence too, so a guess never shares a header (and ticked boxes) with a sure thing.
        groups: dict[tuple, dict] = {}
        unmatched: list = []
        for auction in page:
            suggestion = suggestions.get(auction.pk)
            if not suggestion:
                unmatched.append(auction)
                continue
            group = groups.setdefault(
                (suggestion.club.pk, suggestion.confidence),
                {"club": suggestion.club, "reason": suggestion.reason, "confidence": suggestion.confidence, "rows": []},
            )
            group["rows"].append({"auction": auction, "reason": suggestion.reason})
        context["groups"] = sorted(groups.values(), key=lambda group: -len(group["rows"]))
        context["unmatched"] = unmatched
        context["clubs"] = clubs
        context["page_size"] = UNLINKED_PAGE_SIZE
        context["shown"] = len(page)
        return context


class LinkAuctionsToClub(AdminOnlyViewMixin, View):
    """Attach the ticked auctions to a club via :func:`auctions.services.link_auction_to_club`."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        wanted = request.POST.get("club") or ""
        club = Club.objects.filter(pk=wanted).first() if wanted.isdigit() else None
        if club is None:
            messages.info(request, "Pick a club first.")
            return redirect(reverse("admin_unlinked_auctions"))
        # Only still-unlinked auctions, against double submits.
        auctions = Auction.objects.filter(
            pk__in=request.POST.getlist("auction"), club__isnull=True, is_deleted=False
        ).select_related("created_by")
        grant_admin = "grant_admin" in request.POST
        linked = admins = 0
        for auction in auctions:
            if link_auction_to_club(
                auction, club, note="from the unlinked auctions page", actor=request.user, grant_admin=grant_admin
            ):
                admins += 1
            linked += 1
        if linked:
            club_health.compute_club_health(club)
            granted = f", and made {admins} of their organizers a club admin" if admins else ""
            messages.success(request, f"Linked {linked} auction(s) to {club.name}{granted}.")
        else:
            messages.info(request, "Nothing to link -- those auctions already have a club.")
        return redirect(reverse("admin_unlinked_auctions"))


class AdminFreeTextUsage(AdminOnlyViewMixin, TemplateView):
    """What invoice adjustments and custom fields are used for, grouped by common terms."""

    template_name = "dashboard_free_text_usage.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["adjustments"] = free_text_usage.adjustments()
        context["fields"] = free_text_usage.custom_fields()
        return context


class AdminSessionReplay(AdminOnlyViewMixin, TemplateView):
    """One person's pages in order, with the gaps.

    ``?session=`` takes a prefix of an anonymous session key (:data:`~auctions.lifecycle.SESSION_KEY_PREFIX`
    characters), never the full cookie. ``?user=`` includes anonymous rows from sessions stitched at
    sign-in (``SignInStitch``).
    """

    template_name = "dashboard_session_replay.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session_id = (self.request.GET.get("session") or "").strip()
        user = None
        # Non-digit ?user= would raise.
        requested_user = (self.request.GET.get("user") or "").strip()
        if requested_user.isdigit():
            user = User.objects.filter(pk=int(requested_user)).first()
        context["session_id"] = session_id
        context["subject"] = user
        context["visit_gap_minutes"] = int(lifecycle.VISIT_GAP.total_seconds() // 60)
        context["stitching_began"] = lifecycle.stitching_began()
        if user is not None:
            context["timeline"] = lifecycle.session_timeline(user=user)
            context["stitched"] = lifecycle.stitched_sessions(user)
        elif session_id:
            context["timeline"] = lifecycle.session_timeline(session_id=session_id)
            context["stitched"] = []
        else:
            context["timeline"] = []
            context["stitched"] = []
            context["recent"] = lifecycle.busiest_sessions()
        return context
