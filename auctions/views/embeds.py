"""The snippets a club puts on its own website, and the pages behind them.

Events, past events, current auction, latest announcement and BAP leaderboard, each with a styled
and an ``_unstyled`` template. ``embed_mode_from_request`` reads ``?format=``. The snippet is a
``<script src>`` (``?format=js``); iframe formats remain for older snippets.
"""

import json
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.template.defaultfilters import date as date_format
from django.template.defaultfilters import pluralize
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import TemplateView, View

from auctions import announcements, club_events
from auctions.forms import (
    ClubAnnouncementForm,
)
from auctions.models import (
    Auction,
    Club,
    ClubAnnouncement,
    ClubHistory,
    ClubMember,
)

from .base import ClubViewMixin
from .club_members import BAP_EMBED_PROGRAM_FIELDS, BAP_EMBED_PROGRAM_LABELS

logger = logging.getLogger(__name__)


def embed_mode_from_request(request):
    """Which representation was asked for: "light", "dark", "unstyled", "script", or None for JSON.

    Unrecognised values fall through to JSON.
    """
    fmt = (request.GET.get("format") or "json").strip().lower()
    if fmt in ("iframelight", "iframedark", "iframdark"):
        return "dark" if fmt in ("iframedark", "iframdark") else "light"
    if fmt == "unstyledhtml":
        return "unstyled"
    if fmt == "js":
        return "script"
    return None


#: The CSS ?format=js adds. Layout only, so rows take the host site's type and colours. Two classes
#: deep to beat theme list styles while staying overridable.
SCRIPT_EMBED_CSS = (
    ".club-embed .club-events,.club-embed .club-announcements{list-style:none;margin:0;padding:0}"
    ".club-embed .club-event,.club-embed .club-announcement,.club-embed .club-events-empty,"
    ".club-embed .club-announcements-empty{list-style:none;margin:0 0 1em;padding:0}"
    ".club-embed .club-event-title,.club-embed .club-auction-title{font-weight:600}"
    ".club-embed .club-event-when,.club-embed .club-event-where,.club-embed .club-auction-when,"
    ".club-embed .club-auction-where,.club-embed .club-auction-lots{display:block;opacity:.75;font-size:.9em}"
    ".club-embed .club-event-cancelled .club-event-title{text-decoration:line-through}"
    ".club-embed .club-event-badge,.club-embed .club-event-repeats{font-size:.75em;border:1px solid;"
    "border-radius:1em;padding:0 .5em;margin-left:.4em;white-space:nowrap}"
    ".club-embed .club-announcement-text{margin:0;white-space:pre-wrap}"
    ".club-embed .bap-leaderboard{width:100%;border-collapse:collapse}"
    ".club-embed .bap-leaderboard th,.club-embed .bap-leaderboard td{text-align:left;padding:.3em .5em;"
    "border-bottom:1px solid rgba(128,128,128,.3)}"
)

#: ?format=js: insert the rows before the script tag that loaded it. Falls back to the last unused
#: ?format=js tag when ``currentScript`` is null; ``data-club-embed`` marks tags as used.
SCRIPT_EMBED_JS = """(function () {
  var html = %s;
  var here = document.currentScript;
  if (!here || here.getAttribute("data-club-embed")) {
    here = null;
    var tags = document.getElementsByTagName("script");
    for (var i = tags.length - 1; i >= 0; i--) {
      if (tags[i].src.indexOf("format=js") !== -1 && !tags[i].getAttribute("data-club-embed")) {
        here = tags[i];
        break;
      }
    }
  }
  if (!here) { return; }
  here.setAttribute("data-club-embed", "1");
  here.insertAdjacentHTML("beforebegin", html);
})();
"""


def embed_response(template_stem, embed_mode, context):
    """Render one of auctions/embeds/*, with the headers a third-party site needs.

    ``Access-Control-Allow-Origin`` is set on every response; these views are public and GET-only.
    "script" wraps the unstyled markup and ``SCRIPT_EMBED_CSS`` in JavaScript that writes it in.
    """
    suffix = "" if embed_mode in ("light", "dark") else "_unstyled"
    html = render_to_string(f"auctions/embeds/{template_stem}{suffix}.html", context)
    if embed_mode == "script":
        html = f'<div class="club-embed club-embed-{template_stem}"><style>{SCRIPT_EMBED_CSS}</style>{html}</div>'
        response = HttpResponse(SCRIPT_EMBED_JS % json.dumps(html), content_type="text/javascript; charset=utf-8")
    else:
        response = HttpResponse(html)
    response["Access-Control-Allow-Origin"] = "*"
    return response


def embed_json(payload):
    """JSON half of an embed endpoint, with the same cross-origin header."""
    response = JsonResponse(payload)
    response["Access-Control-Allow-Origin"] = "*"
    return response


def _bap_embed_leaderboard(club, program):
    """Top-10 rows for a program: rank, display name and points only, "Member N" when unnamed."""
    field = BAP_EMBED_PROGRAM_FIELDS[program]
    members = ClubMember.objects.filter(club=club, is_deleted=False, **{f"{field}__gt": 0}).order_by(
        f"-{field}", "name"
    )[:10]
    rows = []
    for i, member in enumerate(members):
        name = (member.name or "").strip() or f"Member {i + 1}"
        rows.append({"rank": i + 1, "name": name, "points": getattr(member, field)})
    return rows


@method_decorator(xframe_options_exempt, name="dispatch")
class BapEmbedView(View):
    """Public, embeddable top-10 BAP/HAP/CAP leaderboard. ?format= and ?program= (bap, hap, cap).

    Names and points only. Framing and cross-origin fetches allowed; GET-only.
    """

    def _json_response(self, club, program, label, rows):
        response = JsonResponse({"club": club.name, "program": program, "program_label": label, "leaderboard": rows})
        response["Access-Control-Allow-Origin"] = "*"
        return response

    def get(self, request, slug):
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).order_by("pk").first()
        if not club or not club.enable_breeder_award_program:
            raise Http404

        program = (request.GET.get("program") or "bap").strip().lower()
        if program not in BAP_EMBED_PROGRAM_FIELDS:
            program = "bap"
        # Without separate tracking, HAP/CAP points roll into BAP.
        if (program == "hap" and not club.separate_hap) or (program == "cap" and not club.separate_cap):
            raise Http404

        rows = _bap_embed_leaderboard(club, program)
        label = BAP_EMBED_PROGRAM_LABELS[program]
        embed_mode = embed_mode_from_request(request)
        if embed_mode is None:
            return self._json_response(club, program, label, rows)
        return embed_response(
            "bap",
            embed_mode,
            {
                "embed_mode": embed_mode,
                "club_name": club.name,
                "program_label": label,
                "leaderboard": rows,
            },
        )


# Most events the embed returns.
CLUB_EVENTS_EMBED_MAX = 10


def _club_events_embed_rows(request, club, count, *, past=False):
    """The club's next events for the embed, without pickup events. Public data only.

    ``past=True`` returns the most recent past events instead, formatted identically.
    """
    if past:
        events = club_events.past_events(club, limit=count, exclude_pickups=True)
    else:
        events, _ = club_events.upcoming_events(club, limit=count, exclude_pickups=True)
    rows = []
    for event in events:
        start = timezone.localtime(event.date_start)
        rows.append(
            {
                "title": event.title,
                # See ClubEvent.when_display.
                "when": event.when_display,
                "starts": start.isoformat(),
                "all_day": event.all_day,
                "location": event.location,
                "cancelled": event.cancelled,
                "repeats": event.recurrence_summary,
                "url": request.build_absolute_uri(event.get_absolute_url()),
            }
        )
    return rows


def _viewer_runs_this_club(request, club):
    """Whether the viewer can manage the club's embeds, so their views aren't counted."""
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    # One query: public endpoint hit on every page load of the club's site.
    return (
        ClubMember.objects.filter(club=club, user=request.user, is_deleted=False)
        .filter(Q(permission_admin=True) | Q(permission_manage_auctions=True) | Q(permission_edit_club=True))
        .exists()
    )


@method_decorator(xframe_options_exempt, name="dispatch")
class ClubEventsEmbedView(View):
    """Public, embeddable list of a club's next events. ?format= and ?count= (1 to
    CLUB_EVENTS_EMBED_MAX). Framing and cross-origin fetches allowed; GET-only.

    ``ClubPastEventsEmbedView`` overrides the three class attributes below.
    """

    #: Newest-first history instead of what's coming up.
    past = False
    #: Shown when there's nothing to list.
    empty_message = "Nothing coming up right now."
    #: The JSON representation's key.
    json_key = "events"

    def get(self, request, slug):
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).order_by("pk").first()
        if not club:
            raise Http404

        try:
            count = int(request.GET.get("count") or CLUB_EVENTS_EMBED_MAX)
        except (TypeError, ValueError):
            count = CLUB_EVENTS_EMBED_MAX
        count = max(1, min(count, CLUB_EVENTS_EMBED_MAX))

        rows = _club_events_embed_rows(request, club, count, past=self.past)
        # Every format and empty results count; the club's own admins don't.
        if not _viewer_runs_this_club(request, club):
            club_events.record_website_view(club)
        embed_mode = embed_mode_from_request(request)
        if embed_mode is None:
            return embed_json({"club": club.name, self.json_key: rows})
        return embed_response(
            "events",
            embed_mode,
            {
                "embed_mode": embed_mode,
                "club_name": club.name,
                "events": rows,
                "past": self.past,
                "empty_message": self.empty_message,
            },
        )


@method_decorator(xframe_options_exempt, name="dispatch")
class ClubPastEventsEmbedView(ClubEventsEmbedView):
    """The same embed for past events, newest first."""

    past = True
    empty_message = "Nothing here yet."
    json_key = "past_events"


# Most announcements the embed returns.
CLUB_ANNOUNCEMENTS_EMBED_MAX = 3


def _club_announcements_embed_rows(club, count):
    """The club's latest announcements marked "show on website", for the embed."""
    rows = []
    shown = announcements.latest_for_website(club, count)
    for announcement in shown:
        created = timezone.localtime(announcement.created_at)
        rows.append(
            {
                "text": announcement.text.strip(),
                "when": date_format(created, "N j, Y"),
                "posted": created.isoformat(),
            }
        )
    # Every format counts, JSON included.
    announcements.record_website_views(shown)
    return rows


@method_decorator(xframe_options_exempt, name="dispatch")
class ClubAnnouncementsEmbedView(View):
    """Public, embeddable list of a club's latest announcements; ?count= defaults to one."""

    def get(self, request, slug):
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).order_by("pk").first()
        if not club:
            raise Http404
        try:
            count = int(request.GET.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, CLUB_ANNOUNCEMENTS_EMBED_MAX))

        rows = _club_announcements_embed_rows(club, count)
        embed_mode = embed_mode_from_request(request)
        if embed_mode is None:
            return embed_json({"club": club.name, "announcements": rows})
        return embed_response(
            "announcements",
            embed_mode,
            {
                "embed_mode": embed_mode,
                "club_name": club.name,
                "announcements": rows,
            },
        )


def _club_current_auction(club):
    """The pinned ``current_auction``, else the soonest promoted auction that hasn't finished, or None."""
    now = timezone.now()
    pinned = club.current_auction
    if pinned and not pinned.is_deleted and pinned.promote_this_auction and not pinned.pretty_much_over:
        return pinned
    return (
        Auction.objects.filter(club=club, is_deleted=False, promote_this_auction=True, date_start__isnull=False)
        .filter(Q(date_end__gte=now) | Q(date_end__isnull=True, date_start__gte=now))
        .order_by("date_start")
        .first()
    )


def _club_auction_embed_row(request, auction):
    """The handful of facts about an auction worth putting on somebody else's website."""
    if not auction:
        return None
    start = timezone.localtime(auction.date_start)
    when = f"{date_format(start, 'D, N j, Y')} at {date_format(start, 'g:i A')}"
    if auction.is_online and auction.date_end and auction.date_end > auction.date_start:
        end = timezone.localtime(auction.date_end)
        when += f" – {date_format(end, 'D, N j, Y')} at {date_format(end, 'g:i A')}"
    lots_open = ""
    if auction.lot_submission_end_date and auction.lot_submission_end_date > timezone.now():
        deadline = timezone.localtime(auction.lot_submission_end_date)
        lots_open = f"Lots can be entered until {date_format(deadline, 'N j, Y')}"
    return {
        "title": auction.title,
        "when": when,
        "starts": timezone.localtime(auction.date_start).isoformat(),
        "is_online": auction.is_online,
        "location": club_events.auction_display_location(auction),
        "lots_open": lots_open,
        "url": request.build_absolute_uri(auction.get_absolute_url()),
    }


@method_decorator(xframe_options_exempt, name="dispatch")
class ClubAuctionEmbedView(View):
    """Public, embeddable strip for the club's current auction; empty between auctions."""

    def get(self, request, slug):
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).order_by("pk").first()
        if not club:
            raise Http404
        row = _club_auction_embed_row(request, _club_current_auction(club))
        embed_mode = embed_mode_from_request(request)
        if embed_mode is None:
            return embed_json({"club": club.name, "auction": row})
        return embed_response(
            "auction",
            embed_mode,
            {
                "embed_mode": embed_mode,
                "club_name": club.name,
                "auction": row,
            },
        )


class ClubAnnouncementsView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Write an announcement, and see where past ones went, with each channel's reportable numbers."""

    template_name = "auctions/club_announcements.html"
    active_tab = "announcements"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        # Its own permission: one press reaches Discord, phones and the mailing list.
        if not self.user_has_club_permission("permission_send_announcements"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, data=None):
        return ClubAnnouncementForm(data, club=self.club)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context.setdefault("form", self.get_form())
        # Retracted ones are listed struck through, so Retract doesn't look like Delete.
        rows = list(ClubAnnouncement.objects.filter(club=self.club)[:50])
        context["announcements"] = rows
        # Reload once the retract window closes.
        pending = [r.scheduled_for for r in rows if r.is_in_grace_period]
        if pending:
            seconds = (min(pending) - timezone.now()).total_seconds() + 3
            context["reload_in_seconds"] = max(2, int(seconds))
        self._queue_open_refresh(rows)
        return context

    def _queue_open_refresh(self, rows):
        """Refresh open counts for the few recent rows in the background, never during the page load."""
        from auctions.tasks import refresh_announcement_opens

        cutoff = timezone.now() - timedelta(days=30)
        recent = [r for r in rows if r.sent_by_email and not r.is_deleted and r.created_at >= cutoff][:5]
        for announcement in recent:
            try:
                refresh_announcement_opens.delay(announcement.pk)
            except Exception:
                logger.warning("Could not queue an open-count refresh for announcement %s", announcement.pk)
                break

    def post(self, request, *args, **kwargs):
        form = self.get_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        announcement = form.save(commit=False)
        announcement.club = self.club
        announcement.created_by = request.user
        # ``announcements.queue`` is shared with the assistant.
        chose_a_time, where = announcements.queue(announcement, acting_user=request.user)
        if chose_a_time:
            when = timezone.localtime(announcement.scheduled_for)
            messages.success(
                request,
                f"Going to {where} on {when.strftime('%A, %B %-d at %-I:%M %p')}. "
                "Retract it before then and it never goes out.",
            )
        else:
            messages.success(
                request,
                f"Going to {where} in {announcements.GRACE_SECONDS} seconds. Read it back — "
                "Retract now and nobody sees it.",
            )
        return redirect(reverse("club_announcements", kwargs={"slug": self.club.slug}))


class ClubAnnouncementRetractView(LoginRequiredMixin, ClubViewMixin, View):
    """Retract an announcement, and say what couldn't be taken back (push, email)."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if not self.user_has_club_permission("permission_send_announcements"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, uuid):
        announcement = get_object_or_404(ClubAnnouncement, uuid=uuid, club=self.club, is_deleted=False)
        result = announcements.retract(announcement)
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Announcement retracted: {announcement.short_text}",
            applies_to="ANNOUNCEMENTS",
        )
        if result["never_sent"]:
            messages.success(request, "Announcement cancelled. It was never sent.")
            return redirect(reverse("club_announcements", kwargs={"slug": self.club.slug}))
        still_out_there = []
        if result["discord_left_behind"]:
            still_out_there.append("the Discord post couldn't be deleted — remove it by hand")
        if result["push_delivered"]:
            still_out_there.append(
                f"{result['push_delivered']} phone{pluralize(result['push_delivered'])} already got the notification"
            )
        if result["emailed"]:
            still_out_there.append("the email has already been sent and can't be recalled")
        if still_out_there:
            messages.warning(
                request,
                "Announcement retracted, but " + "; ".join(still_out_there) + ".",
            )
        else:
            messages.success(request, "Announcement retracted.")
        return redirect(reverse("club_announcements", kwargs={"slug": self.club.slug}))


class ClubWebsiteIntegrationView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Every "put this on your website" snippet in one place, including ones for features that are off."""

    template_name = "auctions/club_website_integration.html"
    active_tab = "website_integration"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if not (self.can_manage_auctions or self.can_edit_settings):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        club = self.club
        context["club"] = club
        base = f"{self.request.scheme}://{self.request.get_host()}"
        context["snippets"] = [
            {
                "key": "events",
                "title": "Upcoming events",
                "icon": "bi-calendar-event",
                "blurb": (
                    "Your club calendar, live. Auctions, meetings, swaps and anything pulled in from "
                    "your Google Calendar. Only the name, date and place — never anything about your members."
                ),
                "url": base + reverse("club_events_embed", kwargs={"slug": club.slug}),
                "counts": True,
                "max_count": CLUB_EVENTS_EMBED_MAX,
                "default_count": 5,
                "available": True,
            },
            {
                "key": "past_events",
                "title": "Past events",
                "icon": "bi-clock-history",
                "blurb": (
                    "The same list looking backwards, newest first — what your club has actually "
                    "been doing. Somebody deciding whether to come to a meeting reads this one."
                ),
                "url": base + reverse("club_past_events_embed", kwargs={"slug": club.slug}),
                "counts": True,
                "max_count": CLUB_EVENTS_EMBED_MAX,
                "default_count": 5,
                "available": True,
            },
            {
                "key": "auction",
                "title": "Current auction",
                "icon": "bi-hammer",
                "blurb": (
                    "The auction you have pinned as current, or the soonest promoted one if you "
                    "haven't pinned any. It clears itself a day after that auction is over — until "
                    "the next one is promoted, the snippet says there's nothing on."
                ),
                "url": base + reverse("club_auction_embed", kwargs={"slug": club.slug}),
                "counts": False,
                "available": True,
            },
            {
                "key": "announcements",
                "title": "Latest announcement",
                "icon": "bi-megaphone",
                "blurb": (
                    "Whatever you last announced with the Website box ticked. Defaults to one — the "
                    "usual use is a single line at the top of a home page."
                ),
                "url": base + reverse("club_announcements_embed", kwargs={"slug": club.slug}),
                "counts": True,
                "max_count": CLUB_ANNOUNCEMENTS_EMBED_MAX,
                "default_count": 1,
                "available": True,
            },
            {
                "key": "bap",
                "title": "Breeder Award leaderboard",
                "icon": "bi-award",
                "blurb": (
                    "Your current top ten. Names and points only — never emails or member numbers. "
                    "Add &program=hap or &program=cap to the URL for a separate program."
                ),
                "url": base + reverse("bap_embed", kwargs={"slug": club.slug}),
                "counts": False,
                "available": club.enable_breeder_award_program,
                "unavailable_reason": "The Breeder Award Program is turned off for this club.",
                "settings_url": reverse("club_bap_settings", kwargs={"slug": club.slug}),
            },
            {
                "key": "calendar",
                "title": "Calendar links",
                "icon": "bi-calendar-check",
                "blurb": (
                    "Not an embed — just the two addresses behind your events, to put on whatever "
                    "your site already has: a button, a menu item, a line of text. The first one "
                    "adds your calendar to somebody's own; the second is the raw feed, for "
                    "anything that reads one."
                ),
                # The club's shared Google calendar if there is one, otherwise ours.
                "links": [
                    {
                        "label": "Add to calendar",
                        "url": club.calendar_subscribe_url(self.request.get_host()),
                        "note": (
                            "Opens Google Calendar."
                            if club.google_calendar_public_url
                            else "Opens whatever calendar app the visitor uses."
                        ),
                    },
                    {
                        "label": "Calendar feed (.ics)",
                        "url": club.calendar_feed_url(self.request.get_host()),
                        "note": "For a website plugin or anything else that reads a calendar feed.",
                    },
                ],
                "available": True,
            },
        ]
        return context
