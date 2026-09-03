"""The snippets a club puts on its own website, and the pages behind them.

Five embeds -- events, past events, the current auction, the latest announcement and the BAP
leaderboard -- sharing one shell, each with a styled and an ``_unstyled`` template.
``embed_mode_from_request`` is the one reader of ``?format=``. Every styled embed measures itself
and posts its height to the parent frame; the snippet the club copies contains the listener.
"""

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
    """Which representation a club asked for: "light", "dark", "unstyled", or None for JSON.

    One reader for the ?format= every embed takes, so the four of them can't drift into
    supporting slightly different spellings. Anything unrecognised falls through to JSON rather
    than to a page -- a typo in a snippet must never hand a stranger's website an unexpected
    document.
    """
    fmt = (request.GET.get("format") or "json").strip().lower()
    if fmt in ("iframelight", "iframedark", "iframdark"):
        return "dark" if fmt in ("iframedark", "iframdark") else "light"
    if fmt == "unstyledhtml":
        return "unstyled"
    return None


def embed_response(template_stem, embed_mode, context):
    """Render one of auctions/embeds/*, with the framing headers a third-party site needs.

    ``Access-Control-Allow-Origin`` is set on every embed response so a club's own JavaScript can
    fetch one instead of iframing it; the views themselves are GET-only and public, so there is
    nothing here CORS could leak that the page it mirrors doesn't already show.
    """
    suffix = "_unstyled" if embed_mode == "unstyled" else ""
    html = render_to_string(f"auctions/embeds/{template_stem}{suffix}.html", context)
    response = HttpResponse(html)
    response["Access-Control-Allow-Origin"] = "*"
    return response


def embed_json(payload):
    """JSON half of an embed endpoint, with the same cross-origin header."""
    response = JsonResponse(payload)
    response["Access-Control-Allow-Origin"] = "*"
    return response


def _bap_embed_leaderboard(club, program):
    """Top-10 leaderboard rows for a program: rank, display name, and points only.

    Deliberately exposes no PII — never emails, member numbers, or database ids. When a
    member has no name we fall back to a generic "Member N" label keyed off their rank.
    """
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
    """Public, embeddable top-10 BAP/HAP/CAP leaderboard for a club.

    A single endpoint serves several representations via ?format= (json, iframelight,
    iframedark, unstyledhtml) and picks the program with ?program= (bap, hap, cap).
    Only the top-10 leaderboard is exposed, and only names + points — never emails,
    member numbers, or other PII. Framing (xframe_options_exempt) and cross-origin
    fetches (Access-Control-Allow-Origin) are allowed so third-party sites can embed it.
    GET-only and public, so CSRF never applies.
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
        # HAP/CAP only have their own standings when the club tracks them separately;
        # otherwise those points roll into BAP and a dedicated board would be misleading.
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


# How many events the embed will ever hand out. Clubs paste this into a sidebar; past ten it
# stops being "what's coming up" and turns into a second copy of the club page.
CLUB_EVENTS_EMBED_MAX = 10


def _club_events_embed_rows(request, club, count, *, past=False):
    """The club's next few events, flattened for the embed: what, when, where, and a link back.

    Everything here is already on the public club page and in the public iCal feed — no member
    data of any kind. Pickup events are left out for the same reason
    ``club_events.next_member_facing_event`` drops them: they're logistics for people who
    already won lots, not something to put on the club's website.

    ``past=True`` is the same rows in the other direction, newest first, so ``count=1`` is the
    thing that happened most recently. One function rather than two because a club pasting both
    embeds onto one page must get two lists that look alike — the moment the formatting lives in
    two places, one of them grows a field the other doesn't.
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
                # Same one-line "when" the club page shows, so a multi-day online auction reads as
                # one on somebody's website too. See ClubEvent.when_display.
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
    """True when the person asking for an embed is one of the people who pasted it.

    Used to keep an admin checking their own snippet out of ``events_website_views``. The same
    three permissions gate the website-integration page the URLs are copied from, so this is
    exactly "somebody who could have been testing it".
    """
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    # One query rather than three calls to check_club_permission: this runs on a public endpoint
    # that a club's own home page hits on every page load.
    return (
        ClubMember.objects.filter(club=club, user=request.user, is_deleted=False)
        .filter(Q(permission_admin=True) | Q(permission_manage_auctions=True) | Q(permission_edit_club=True))
        .exists()
    )


@method_decorator(xframe_options_exempt, name="dispatch")
class ClubEventsEmbedView(View):
    """Public, embeddable list of a club's next few events, for WordPress and the like.

    Same shape as ``BapEmbedView``: ?format= picks the representation (json, iframelight,
    iframedark, unstyledhtml) and ?count= how many events, 1 to CLUB_EVENTS_EMBED_MAX. count=1
    is the "next event" banner clubs put at the top of a page; the default is the full list.
    Framing and cross-origin fetches are allowed so a third-party site can use it. GET-only and
    public, so CSRF never applies; the snippets that produce these URLs are admin-only, but the
    URLs themselves show nothing the club page doesn't.

    ``ClubPastEventsEmbedView`` is the same view pointed the other way; everything that differs
    between them is one of the three class attributes below.
    """

    #: Newest-first history instead of what's coming up.
    past = False
    #: What to say when there is nothing to list. The two directions are empty for opposite
    #: reasons, and "nothing coming up" under a heading that says "past events" reads as a bug.
    empty_message = "Nothing coming up right now."
    #: The key the JSON representation uses. Named for what it holds, so a club's own script
    #: doesn't have to know which endpoint it fetched.
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
        # Every format counts, JSON included, and an empty answer counts too: what is being
        # recorded is that somebody's website asked us for this club's calendar, which is as true
        # of a club with nothing on as of a club with ten meetings. Admins are left out so that
        # checking your own snippet doesn't look like your members reading it.
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
    """The same embed looking backwards: what this club has been up to, newest first.

    A club's own website usually has room for both — "what's on" at the top of a page and "what
    we've been doing" further down — and the second one is the half a visitor deciding whether to
    join actually reads. ``count=1`` is the thing that happened last.
    """

    past = True
    empty_message = "Nothing here yet."
    json_key = "past_events"


# A club pasting this into a sidebar wants "what's new", not an archive. Past three it stops being
# an announcement and starts being a blog nobody asked us to build.
CLUB_ANNOUNCEMENTS_EMBED_MAX = 3


def _club_announcements_embed_rows(club, count):
    """The club's most recent published announcements, flattened for the embed.

    Only announcements the club ticked "show on website" for ever reach this — the other channels
    are opt-in one at a time on the same form, and a club that chose Discord only must not find
    its message on its own home page.
    """
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
    # Every format counts, JSON included: a club whose site fetches the JSON and renders it itself
    # has put the announcement on a page exactly as much as one using the styled iframe.
    announcements.record_website_views(shown)
    return rows


@method_decorator(xframe_options_exempt, name="dispatch")
class ClubAnnouncementsEmbedView(View):
    """Public, embeddable list of a club's latest announcements.

    Same shape as the events and BAP embeds: ?format= picks the representation and ?count= how
    many, defaulting to **one** — the common use is a single line at the top of a club's home
    page saying what is going on this month. Nothing here is member data; it is the same text the
    club page shows to the public.
    """

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
    """The auction a club would want advertised on its own website, or None.

    The pinned ``current_auction`` first, because an admin picked it on purpose; otherwise the
    soonest promoted auction that hasn't finished. Unpromoted auctions are never offered — that
    flag is the club saying "this one isn't for the public yet", and an embed is as public as it
    gets.
    """
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
    """Public, embeddable "our auction is on" strip for a club's own website.

    Overlaps the events embed on purpose: this one names *the auction*, where the events embed
    shows whatever happens to be next, which for most of the year is a meeting. It is only ever
    the auction that is still ahead or still running -- see _club_current_auction, which drops a
    pinned auction once it is pretty_much_over -- so a club's front page goes quiet between
    auctions rather than advertising last spring's.
    """

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
    """Write an announcement, and see where the last ones went.

    One page rather than a list plus a form, because posting is the reason anybody comes here and
    the history is what tells them whether last month's went anywhere. Each past row carries an
    icon per channel with the only number that channel can honestly report, which for Discord is
    none at all.
    """

    template_name = "auctions/club_announcements.html"
    active_tab = "announcements"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        # Its own permission rather than "manages auctions": posting here reaches Discord, every
        # member's phone and the club's mailing list in one press, with nobody between the person
        # writing and the people reading. That is not the same trust as adding a lot to an auction.
        if not self.user_has_club_permission("permission_send_announcements"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, data=None):
        return ClubAnnouncementForm(data, club=self.club)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context.setdefault("form", self.get_form())
        # Retracted ones are listed too, struck through. is_deleted is what hides an announcement
        # from the public; hiding it from the club as well would make Retract look like Delete and
        # leave the admin who pressed it with nothing on screen saying it worked.
        rows = list(ClubAnnouncement.objects.filter(club=self.club)[:50])
        context["announcements"] = rows
        # A row inside its retract window says "Sending — retract it now", which stops being true
        # a few seconds later. Reload once when it does, so the page ends up showing what actually
        # happened rather than a promise the reader has to refresh to check.
        pending = [r.scheduled_for for r in rows if r.is_in_grace_period]
        if pending:
            seconds = (min(pending) - timezone.now()).total_seconds() + 3
            context["reload_in_seconds"] = max(2, int(seconds))
        self._queue_open_refresh(rows)
        return context

    def _queue_open_refresh(self, rows):
        """Ask the email providers for open counts in the background, never during the page load.

        Opens arrive hours after a send, so the number on screen is always the stored one and this
        only updates it for next time. Bounded to the few recent rows that could still change: an
        admin opening this page must never pay for 50 rows' worth of somebody else's API.
        """
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
        # Saving, scheduling and describing where it goes are ``announcements.queue`` -- shared
        # with the assistant, so an announcement is sent one way rather than two.
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
    """Take an announcement back, and say honestly how much of it could be taken back.

    Clubs send the wrong date, and the first thing they ask for is a way to unsend it. What that
    can mean is different per channel -- the Discord post goes, the page goes, the website listing
    goes with it; the push notification is already on a lock screen and the email is already in an
    inbox -- so the message afterwards names what is still out there instead of saying "retracted"
    and letting the admin believe it was all undone.
    """

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
    """Every "put this on your own website" snippet the site offers, in one place.

    They used to be a collapsed panel on whichever page happened to own the data — the calendar
    for events, the BAP page for the leaderboard — which meant a club had to already know a
    feature existed to find the snippet for it, and a club with the Breeder Award Program turned
    off could never see that one at all. They are all listed here whether or not the feature is
    switched on, with the ones that would currently render nothing labelled as such: a club
    deciding what to put on its website is exactly the person who should find out that turning
    BAP on would give them a leaderboard.
    """

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
                "default_count": 1,
                "heights": {1: 200, 10: 880},
                "available": True,
            },
            {
                "key": "past_events",
                "title": "Past events",
                "icon": "bi-clock-history",
                "blurb": (
                    "The same list looking backwards, newest first — what your club has actually "
                    "been doing. Somebody deciding whether to come to a meeting reads this one. "
                    "Set count=1 for just the most recent."
                ),
                "url": base + reverse("club_past_events_embed", kwargs={"slug": club.slug}),
                "counts": True,
                "max_count": CLUB_EVENTS_EMBED_MAX,
                "default_count": 1,
                "heights": {1: 200, 10: 880},
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
                "heights": {1: 200},
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
                "heights": {1: 160, 3: 340},
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
                "heights": {1: 420},
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
                # Google's when the club has shared its calendar, ours when it hasn't. Same rule
                # as the buttons on the club page: the shared Google calendar is the copy the club
                # itself keeps, so it has whatever an admin typed straight into it, pull or no pull.
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
