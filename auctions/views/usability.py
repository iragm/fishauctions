"""The usability dashboards: the three measurements, and the club outreach queue.

Three panels, one per question in USABILITY.md's "Measuring" section, because they are only useful
next to each other: high reach with no failures is a page that works, the same reach with a run of
unresolved bounces on one field is the thing the campaign exists to find, and a setting nobody has
ever changed is a deletion candidate rather than a redesign candidate.

The queries are in :mod:`auctions.usability_report` and :mod:`auctions.field_adoption` so they can
be tested without a request.

:class:`AdminClubHealth` is the other half of the campaign and deliberately not another chart: it
is a **worklist**. :mod:`auctions.club_health` decides which clubs are overdue against their own
cadence, and this page is where somebody works down that list and marks each one contacted, which
writes ``Club.date_contacted`` -- the outreach field that already existed with nothing feeding it.
"""

import logging

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from auctions import club_health, usability_report
from auctions.field_adoption import auction_field_adoption
from auctions.models import Club, ClubHealth

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
        context["reach_caveats"] = usability_report.REACH_CAVEATS
        context["friction"] = usability_report.friction_by_form(days=days)
        adoption = auction_field_adoption()
        context["adoption"] = sorted(adoption, key=lambda row: (row.off_default, row.edits))
        context["adoption_unused"] = [row for row in adoption if row.verdict == "unused"]
        context["adoption_total"] = adoption[0].total if adoption else 0
        return context


class AdminClubHealth(AdminOnlyViewMixin, TemplateView):
    """Which clubs have gone quiet against their own cadence, and who to contact next"""

    template_name = "dashboard_club_health.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["queue"] = club_health.due_for_checkin()
        labels = dict(club_health.STAGE_CHOICES)
        context["stages"] = [
            {"stage": row["stage"], "label": labels.get(row["stage"], row["stage"]), "clubs": row["clubs"]}
            for row in ClubHealth.objects.values("stage").annotate(clubs=Count("pk")).order_by("-clubs")
        ]
        context["never_computed"] = Club.objects.filter(health__isnull=True).count()
        context["stale"] = ClubHealth.objects.filter(
            computed_on__lt=timezone.now() - timezone.timedelta(days=3)
        ).count()
        context["unreachable"] = sum(1 for row in context["queue"] if not row.is_reachable)
        context["contact_cooldown_days"] = club_health.CONTACT_COOLDOWN_DAYS
        context["tool_names"] = [label for label, _check in club_health.TOOL_CHECKS]
        context["tool_counts"] = _tool_counts(context["tool_names"])
        return context


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
        club.save(update_fields=["date_contacted"])
        club_health.compute_club_health(club)
        messages.success(request, f"{club.name} marked as contacted.")
        return redirect(reverse("admin_club_health"))
