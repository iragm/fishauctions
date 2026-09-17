"""The usability dashboards: measurements, the buyer funnel, and club outreach.

:class:`AdminUsability` shows reach, failures, adoption and the buyer funnel side by side. Queries
live in :mod:`auctions.usability_report` and :mod:`auctions.field_adoption`.

:class:`AdminClubHealth` is the outreach worklist from :mod:`auctions.club_health`; marking a club
contacted writes ``Club.date_contacted``.

:class:`UnlinkedAuctions` proposes club links for auctions with none (:mod:`auctions.club_matching`),
since only about a fifth of auctions have a club.
"""

import logging

from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from auctions import club_health, club_matching, lifecycle, usability_report
from auctions.field_adoption import auction_field_adoption
from auctions.models import Auction, Club, ClubHealth
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
        adoption = auction_field_adoption()
        context["adoption"] = sorted(adoption, key=lambda row: (row.off_default, row.edits))
        context["adoption_unused"] = [row for row in adoption if row.verdict == "unused"]
        context["adoption_total"] = adoption[0].total if adoption else 0
        return context


class AdminClubHealth(AdminOnlyViewMixin, TemplateView):
    """Clubs gone quiet against their own cadence, who to contact next, and the full ladder."""

    template_name = "dashboard_club_health.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["queue"] = club_health.due_for_checkin()
        context["ladder"] = club_health.ladder_counts()
        context["ladder_history"] = club_health.ladder_history()
        context["stall_reason_choices"] = Club.STALL_REASON_CHOICES
        context["stall_reasons"] = _stall_reason_counts()
        context["never_computed"] = Club.objects.filter(health__isnull=True).count()
        # Most auctions have no club, so link to the repair page.
        context["unlinked_auctions"] = Auction.objects.filter(club__isnull=True, is_deleted=False).count()
        context["stale"] = ClubHealth.objects.filter(
            computed_on__lt=timezone.now() - timezone.timedelta(days=3)
        ).count()
        context["unreachable"] = sum(1 for row in context["queue"] if not row.is_reachable)
        context["contact_cooldown_days"] = club_health.CONTACT_COOLDOWN_DAYS
        context["tool_names"] = [label for label, _check in club_health.TOOL_CHECKS]
        context["tool_counts"] = _tool_counts(context["tool_names"])
        return context


def _stall_reason_counts():
    """``[{reason, label, clubs}]``: counts of recorded stall reasons, excluding unknown."""
    labels = dict(Club.STALL_REASON_CHOICES)
    rows = Club.objects.exclude(stall_reason="").values("stall_reason").annotate(clubs=Count("pk")).order_by("-clubs")
    return [
        {
            "reason": row["stall_reason"],
            "label": labels.get(row["stall_reason"], row["stall_reason"]),
            "clubs": row["clubs"],
        }
        for row in rows
    ]


def _tool_counts(tool_names):
    """How many clubs have used each tool; one query per tool since ``tools_used`` is JSON."""
    counts = []
    for name in tool_names:
        counts.append({"tool": name, "clubs": ClubHealth.objects.filter(tools_used__contains=name).count()})
    return sorted(counts, key=lambda row: -row["clubs"])


class ClubMarkContacted(AdminOnlyViewMixin, View):
    """Mark one club contacted via ``Club.date_contacted``, which survives the nightly rollup rebuild."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        club = get_object_or_404(Club, pk=kwargs["pk"])
        club.date_contacted = timezone.now()
        fields = ["date_contacted"]
        # Only fixed choices are stored, and only when the field is posted ("" is valid).
        if "stall_reason" in request.POST:
            reason = request.POST["stall_reason"]
            if reason in dict(Club.STALL_REASON_CHOICES):
                club.stall_reason = reason
                fields.append("stall_reason")
        club.save(update_fields=fields)
        club_health.compute_club_health(club)
        messages.success(request, f"{club.name} marked as contacted.")
        return redirect(reverse("admin_club_health"))


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
        groups: dict[int, dict] = {}
        unmatched: list = []
        for auction in page:
            suggestion = suggestions.get(auction.pk)
            if not suggestion:
                unmatched.append(auction)
                continue
            group = groups.setdefault(
                suggestion.club.pk,
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
        club = get_object_or_404(Club, pk=request.POST.get("club") or 0)
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


class AdminLifecycle(AdminOnlyViewMixin, TemplateView):
    """Milestones, one club's cohorts, and the median member of one of its auctions.

    Reach, not a funnel: sellers never open lots. See ``docs/phase_9.md``. Defaults to the club with
    the most auctions, since cohorts need history.
    """

    template_name = "dashboard_lifecycle.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["milestones"] = lifecycle.MILESTONES
        context["coverage"] = lifecycle.club_coverage()
        context["stitching_began"] = lifecycle.stitching_began()
        context["lapsed_after"] = lifecycle.LAPSED_AFTER_AUCTIONS

        clubs = list(
            # An annotation can't shadow the ``auctions`` reverse accessor.
            Club.objects.annotate(auction_count=Count("auctions", filter=Q(auctions__is_deleted=False)))
            .filter(auction_count__gt=0)
            .order_by("-auction_count", "name")
        )
        context["clubs"] = clubs
        club = None
        if self.request.GET.get("club"):
            club = next((item for item in clubs if str(item.pk) == self.request.GET["club"]), None)
        club = club or (clubs[0] if clubs else None)
        context["club"] = club

        auctions = lifecycle.club_auctions(club, limit=None)[-lifecycle.COHORT_AUCTIONS :] if club else []
        # Newest first for milestones; cohorts stay oldest first.
        shown = list(reversed(auctions))
        context["auctions"] = shown
        reach = lifecycle.milestone_reach(auctions)
        # Templates can't index a dict by a variable key.
        context["milestone_rows"] = [
            {
                "milestone": milestone,
                "cells": [reach.get(auction.pk, {}).get(milestone.key, 0) for auction in shown],
            }
            for milestone in lifecycle.MILESTONES
        ]
        context["cohorts"] = lifecycle.club_cohorts(club) if club else []

        auction = context["auctions"][0] if context["auctions"] else None
        if self.request.GET.get("auction"):
            auction = next((item for item in auctions if str(item.pk) == self.request.GET["auction"]), auction)
        context["auction"] = auction
        context["median"] = lifecycle.median_member_story(auction) if auction else None
        context["unreached"] = lifecycle.unreached_share(auction) if auction else None
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
