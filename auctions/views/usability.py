"""The usability dashboards: the measurements, the buyer funnel, and the club outreach queue.

Three of the four panels are one per question in USABILITY.md's "Measuring" section, because they
are only useful next to each other: high reach with no failures is a page that works, the same reach
with a run of unresolved bounces on one field is the thing the campaign exists to find, and a
setting nobody has ever changed is a deletion candidate rather than a redesign candidate.

The fourth is the buyer funnel, and it is the only panel here about somebody who does not run an
auction. It is on this page rather than an organizer's stats page because it is a campaign
instrument: it exists to say which stage of arriving, joining and paying loses people.

The queries are in :mod:`auctions.usability_report` and :mod:`auctions.field_adoption` so they can
be tested without a request.

:class:`AdminClubHealth` is the other half of the campaign and deliberately not another chart: it
is a **worklist**. :mod:`auctions.club_health` decides which clubs are overdue against their own
cadence, and this page is where somebody works down that list and marks each one contacted, which
writes ``Club.date_contacted`` -- the outreach field that already existed with nothing feeding it.

:class:`UnlinkedAuctions` is the repair job underneath both of them.  Only about one auction in
five has a ``club``, because ``services.finish_new_auction`` sets it from a declared affiliation
the creator almost never has -- so every club number on this site is computed from a fifth of the
auctions that exist.  ``assign_auction_to_club`` could already fix that one substring at a time
from a terminal; this page proposes the links (:mod:`auctions.club_matching`) and takes them in
batches, which is the difference between a job somebody does and a job somebody means to do.
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
    """Where people reach, where they get stuck, and which settings anybody has ever changed"""

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
        # Not windowed by `days`: a funnel is a whole auction from arrival to payment, and cutting
        # it at 30 days reports the people who paid last month as a drop-off. The window chooses
        # which auctions are shown, which buyer_funnel does for itself.
        context["funnel"] = usability_report.buyer_funnel()
        context["friction"] = usability_report.friction_by_form(days=days)
        adoption = auction_field_adoption()
        context["adoption"] = sorted(adoption, key=lambda row: (row.off_default, row.edits))
        context["adoption_unused"] = [row for row in adoption if row.verdict == "unused"]
        context["adoption_total"] = adoption[0].total if adoption else 0
        return context


class AdminClubHealth(AdminOnlyViewMixin, TemplateView):
    """Which clubs have gone quiet against their own cadence, and who to contact next

    Also the whole ladder, both halves of it: the hand-set rungs that end in approving a club for
    the map, and the derived ones that start the moment somebody from that club turns up here.
    """

    template_name = "dashboard_club_health.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["queue"] = club_health.due_for_checkin()
        context["ladder"] = club_health.ladder_counts()
        # The ladder as a trend, which is the only part of phase 8f that is code: the outreach
        # email is drafted per club and sent by hand, and a single column of counts cannot show
        # the one thing outreach produces, which is movement between rungs.
        context["ladder_history"] = club_health.ladder_history()
        context["stall_reason_choices"] = Club.STALL_REASON_CHOICES
        context["stall_reasons"] = _stall_reason_counts()
        context["never_computed"] = Club.objects.filter(health__isnull=True).count()
        # Every number on this page is computed from auctions that have a club, and most do not.
        # Without this the page reads as a measurement rather than as a measurement of the fifth of
        # the site it can see -- and the fix for that is a link, not a footnote.
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
    """``[{reason, label, clubs}]`` -- which objection actually comes back, in order.

    The whole reason this is a fixed vocabulary rather than ``Club.notes``: free text cannot be
    counted, and these counts are the only thing that will ever say which objection is worth
    fixing. Clubs with no reason recorded are not a row here -- "not known" is most of them and
    would drown the ones somebody answered.
    """
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
    """How many clubs have used each management tool.

    ``tools_used`` is a JSON list, so this is one query per tool rather than a GROUP BY. There are
    eleven tools and this page is opened by one person, so eleven indexed-free counts is the right
    trade against an expression index nothing else would use.
    """
    counts = []
    for name in tool_names:
        counts.append({"tool": name, "clubs": ClubHealth.objects.filter(tools_used__contains=name).count()})
    return sorted(counts, key=lambda row: -row["clubs"])


class ClubMarkContacted(AdminOnlyViewMixin, View):
    """Mark one club contacted, which is what takes it off the queue.

    The write is ``Club.date_contacted``, not a column on the rollup: the rollup is derived and is
    rebuilt nightly from scratch, so anything a person decides has to live somewhere that survives
    that. ``club_health._queue_decision`` reads the same field back.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        club = get_object_or_404(Club, pk=kwargs["pk"])
        club.date_contacted = timezone.now()
        fields = ["date_contacted"]
        # Only ever set from the fixed vocabulary: anything else posted here is ignored rather than
        # stored, because a column of one-off strings is Club.notes again. The field has to be
        # *present* to be written, and "" is a legal value in it ("Not known"), so a POST that omits
        # it -- another button on this page, a script -- leaves a recorded reason alone instead of
        # clearing it.
        if "stall_reason" in request.POST:
            reason = request.POST["stall_reason"]
            if reason in dict(Club.STALL_REASON_CHOICES):
                club.stall_reason = reason
                fields.append("stall_reason")
        club.save(update_fields=fields)
        club_health.compute_club_health(club)
        messages.success(request, f"{club.name} marked as contacted.")
        return redirect(reverse("admin_club_health"))


#: How many unlinked auctions one page of the repair queue holds.  The backlog is in the hundreds
#: and every group on the page renders a full list of clubs to pick from, so the page is bounded by
#: what it costs to render rather than by what anybody wants to read at once.
UNLINKED_PAGE_SIZE = 100


class UnlinkedAuctions(AdminOnlyViewMixin, TemplateView):
    """The auctions belonging to no club, grouped by the club they probably belong to

    Grouped rather than listed because that is the shape of the work: an organizer who ran eleven
    auctions before their club was on the site has eleven rows with one answer between them, and
    the club picker is rendered once per group instead of once per row.
    """

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
    """Attach the ticked auctions to one club, and hand their creators the run of it

    The write is :func:`auctions.services.link_auction_to_club`, shared with the
    ``assign_auction_to_club`` command so the two routes cannot drift.  Nothing here is a
    suggestion: a suggestion is what the page showed, and this is somebody agreeing with it.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        club = get_object_or_404(Club, pk=request.POST.get("club") or 0)
        # Only ever auctions that still have no club.  Two admins working the same page, or a
        # double submit, would otherwise re-file an auction somebody has already answered for.
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
    """Milestones, one club's cohorts, and the median member of one of its auctions

    Phase 9's report page, and the first one here that is not about the person running the auction.
    Three panels because ``docs/phase_9.md`` argues they are only worth anything next to each
    other: a milestone table says what share of a room got to each thing, the cohort table says
    whether that share is moving between one auction and the next, and the median member says who
    those numbers were actually about.

    **Not a funnel, and the difference is not cosmetic.**  ``AdminUsability`` shows
    ``buyer_funnel``, which is seven stages in a fixed order; a seller adds lots and never opens
    one, and a strict funnel reports that as a drop-out.  This page reports reach.

    The club selector defaults to the club with the most auctions rather than to the most recent
    one, because a cohort table needs a history to be a table at all, and the club with one auction
    renders a row that cannot say anything.
    """

    template_name = "dashboard_lifecycle.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["milestones"] = lifecycle.MILESTONES
        context["coverage"] = lifecycle.club_coverage()
        context["stitching_began"] = lifecycle.stitching_began()
        context["lapsed_after"] = lifecycle.LAPSED_AFTER_AUCTIONS

        clubs = list(
            # ``auctions`` is the reverse accessor, so it is what the filter traverses -- and it
            # is also why the annotation cannot be called that: Django refuses one that shadows a
            # field rather than quietly winning.
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
        # Newest first for the milestone table, which is read as "how did the last one go"; the
        # cohort table keeps the oldest-first order it is computed in, because a trend read
        # backwards is a different trend.
        shown = list(reversed(auctions))
        context["auctions"] = shown
        reach = lifecycle.milestone_reach(auctions)
        # Pivoted here rather than in the template: a milestone is a row and an auction is a
        # column, and Django's template language cannot index a dict by a variable key at all --
        # the alternative is a filter that exists only to make one table render.
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
    """One person's pages, in the order they opened them, with the gaps

    ``docs/phase_9.md`` calls this the highest-value item in the phase, and the argument is about
    sample size rather than about features: with dozens of active organizers and no split that
    could ever reach significance, twenty real sessions read end to end teach more than any
    aggregate.  It needs no new table -- ``PageView`` has carried a session key, a path and a
    timestamp for five years.

    A page and a limit, not a chart.  ``?session=`` reads one anonymous session; ``?user=`` reads
    somebody's signed-in rows **plus** the anonymous rows from every session they were holding when
    they signed in, which for a buyer is the half of the visit that matters.  Before the first
    ``SignInStitch`` there are none of those to find, and the page says so rather than presenting a
    short timeline as a complete one.
    """

    template_name = "dashboard_session_replay.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session_id = (self.request.GET.get("session") or "").strip()
        user = None
        if self.request.GET.get("user"):
            user = User.objects.filter(pk=self.request.GET["user"]).first()
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
