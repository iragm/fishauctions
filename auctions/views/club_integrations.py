"""The outside accounts a club connects: Mailchimp, Brevo, Google Calendar, Square links.

Each has a connect, callback, sync and disconnect view; the club event views are here because the
calendar sync writes them. Connecting from the mobile app: see ``docs/app_oauth_connect_flows.md``.
"""

import json
import logging
import secrets
from datetime import timedelta
from datetime import timezone as date_tz
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseForbidden,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView, View
from django.views.generic.edit import (
    DeleteView,
)

from auctions import club_events, discord_events
from auctions.forms import (
    ClubEventForm,
)
from auctions.models import (
    Club,
    ClubEvent,
    ClubHistory,
    Invoice,
    SquareSeller,
)

from .base import ClubViewMixin, check_club_permission
from .payments import SquareAPIMixin

#: The club being connected, carried in the session across the Mailchimp round trip.
MAILCHIMP_OAUTH_CLUB_SESSION_KEY = "mailchimp_oauth_club_slug"

logger = logging.getLogger(__name__)


class MailchimpConnectView(LoginRequiredMixin, View):
    """Start the Mailchimp OAuth flow for a club (requires permission_edit_club)."""

    def get(self, request, slug):
        club = get_object_or_404(Club, slug=slug)
        if not check_club_permission(request.user, club, "permission_edit_club"):
            raise PermissionDenied()
        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        if not settings.MAILCHIMP_CLIENT_ID:
            messages.error(request, "Mailchimp is not configured on this site. Contact your site administrator.")
            return redirect(config_url)
        # The callback has no slug.
        request.session[MAILCHIMP_OAUTH_CLUB_SESSION_KEY] = club.slug
        params = {
            "response_type": "code",
            "client_id": settings.MAILCHIMP_CLIENT_ID,
            "redirect_uri": request.build_absolute_uri(reverse("mailchimp_callback")),
            # The per-user unsubscribe UUID as OAuth state, same as Square.
            "state": request.user.userdata.unsubscribe_link,
        }
        return redirect("https://login.mailchimp.com/oauth2/authorize?" + urlencode(params))


class MailchimpCallbackView(LoginRequiredMixin, View):
    """Mailchimp's OAuth callback: store the token, then pick an audience."""

    def get(self, request):
        from auctions import mailchimp as mc

        slug = request.session.get(MAILCHIMP_OAUTH_CLUB_SESSION_KEY)
        club = Club.objects.filter(slug=slug).first() if slug else None
        if not club or not check_club_permission(request.user, club, "permission_edit_club"):
            messages.error(request, "Your Mailchimp connection session expired. Please try again.")
            return redirect(reverse("home"))

        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        error = request.GET.get("error")
        if error:
            messages.error(request, f"Mailchimp authorization failed: {request.GET.get('error_description', error)}")
            return redirect(config_url)

        code = request.GET.get("code")
        state = request.GET.get("state")
        if not code or state != request.user.userdata.unsubscribe_link:
            messages.error(request, "Invalid Mailchimp authorization response. Please try again.")
            return redirect(config_url)

        try:
            token, dc = mc.exchange_oauth_code(code, request.build_absolute_uri(reverse("mailchimp_callback")))
        except mc.MailchimpError:
            logger.exception("Mailchimp token exchange failed for club %s", club.pk)
            messages.error(request, "Could not connect to Mailchimp. Please try again.")
            return redirect(config_url)

        club.mailchimp_access_token = token
        club.mailchimp_server_prefix = dc
        club.mailchimp_connected_on = timezone.now()
        club.mailchimp_connected_by = request.user
        if not club.mailchimp_webhook_secret:
            club.mailchimp_webhook_secret = secrets.token_urlsafe(32)
        club.save(
            update_fields=[
                "mailchimp_access_token",
                "mailchimp_server_prefix",
                "mailchimp_connected_on",
                "mailchimp_connected_by",
                "mailchimp_webhook_secret",
            ]
        )
        request.session.pop(MAILCHIMP_OAUTH_CLUB_SESSION_KEY, None)
        messages.success(request, "Mailchimp connected! Now choose which audience to sync your members into.")
        return redirect(config_url)


def _prefill_donation_address(club, address, provider):
    """Fill a blank donation mailing address from the marketing provider's required postal address.

    Never overwrites an address the club typed. Returns True if it filled one in.
    """
    from auctions.models import Club

    address = (address or "").strip()
    if not address or club.donation_mailing_address.strip():
        return False
    club.donation_mailing_address = address
    Club.objects.filter(pk=club.pk).update(donation_mailing_address=address)
    ClubHistory.objects.create(
        club=club,
        action=f"Donation mailing address filled in from {provider}",
        applies_to="SETTINGS",
    )
    return True


class MailchimpAudienceSelectView(LoginRequiredMixin, ClubViewMixin, View):
    """Pick an existing audience or create '{club} Members', then provision and backfill."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import mailchimp as mc

        club = self.club
        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        client = mc.get_client(club)
        if not client:
            messages.error(request, "Mailchimp is not connected. Please connect first.")
            return redirect(config_url)

        choice = request.POST.get("audience_id", "")
        try:
            if choice == "__new__":
                # Sender and address come from the club's Mailchimp account; see mailchimp.account_defaults.
                audience_id, audience_name = mc.create_audience(client, club)
            else:
                audience_id = choice
                audience_name = next((a["name"] for a in mc.list_audiences(client) if a["id"] == choice), "")
        except mc.MailchimpError as e:
            messages.error(request, str(e))
            return redirect(config_url)
        except Exception:
            logger.exception("Mailchimp audience selection failed for club %s", club.pk)
            messages.error(
                request,
                "Couldn't create a new Mailchimp audience (Mailchimp requires a mailing address). "
                "Create an audience in Mailchimp, then come back and pick it from the list.",
            )
            return redirect(config_url)

        if not audience_id:
            messages.error(request, "Please choose an audience.")
            return redirect(config_url)

        club.mailchimp_audience_id = audience_id
        club.mailchimp_audience_name = audience_name
        club.save(update_fields=["mailchimp_audience_id", "mailchimp_audience_name"])

        mc.ensure_merge_fields(club)
        mc.ensure_segments(club)
        mc.ensure_webhook(club)
        count = mc.backfill(club)

        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Connected Mailchimp audience '{audience_name}'",
            applies_to="SETTINGS",
        )
        messages.success(request, f"Syncing {count} member(s) into the '{audience_name}' Mailchimp audience.")
        defaults = mc.account_defaults(client) or {}
        if _prefill_donation_address(club, mc.format_mailing_address(defaults.get("contact")), "Mailchimp"):
            messages.info(
                request,
                "We also filled in your donation mailing address from Mailchimp — check it on the "
                "donation settings page.",
            )
        return redirect(config_url)


class MailchimpSyncNowView(LoginRequiredMixin, ClubViewMixin, View):
    """Re-queue a sync for every in-scope member."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import mailchimp as mc

        club = self.club
        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        if not club.mailchimp_connected:
            messages.error(request, "Mailchimp is not connected.")
            return redirect(config_url)
        count = mc.backfill(club)
        messages.success(request, f"Queued {count} member(s) for syncing to Mailchimp.")
        return redirect(config_url)


class MailchimpDisconnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Forget the Mailchimp connection; the audience stays in Mailchimp."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        club = self.club
        club.mailchimp_access_token = None
        club.mailchimp_server_prefix = ""
        club.mailchimp_audience_id = ""
        club.mailchimp_audience_name = ""
        club.mailchimp_connected_on = None
        club.mailchimp_connected_by = None
        club.mailchimp_webhook_secret = ""
        club.mailchimp_last_error = ""
        club.save(
            update_fields=[
                "mailchimp_access_token",
                "mailchimp_server_prefix",
                "mailchimp_audience_id",
                "mailchimp_audience_name",
                "mailchimp_connected_on",
                "mailchimp_connected_by",
                "mailchimp_webhook_secret",
                "mailchimp_last_error",
            ]
        )
        ClubHistory.objects.create(club=club, user=request.user, action="Disconnected Mailchimp", applies_to="SETTINGS")
        messages.success(request, "Mailchimp disconnected.")
        return redirect(reverse("club_mailchimp_config", kwargs={"slug": club.slug}))


class ClubMailchimpConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Mailchimp settings/status panel for a club."""

    active_tab = "mailchimp"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug):
        from auctions import mailchimp as mc
        from auctions.models import ClubMember

        club = self.club
        audiences = []
        # Connected (token) but no audience chosen yet -> offer the chooser.
        if club.mailchimp_access_token and not club.mailchimp_audience_id:
            client = mc.get_client(club)
            if client:
                try:
                    audiences = mc.list_audiences(client)
                except Exception:
                    logger.exception("Could not list Mailchimp audiences for club %s", club.pk)
                    messages.error(request, "Could not load your Mailchimp audiences. Try reconnecting.")

        synced = ClubMember.objects.filter(club=club, is_deleted=False)
        not_synced_count = (
            mc.in_scope_members(club).filter(mailchimp_last_synced__isnull=True).count()
            if club.mailchimp_audience_id
            else 0
        )
        has_emails_enabled = any(
            [
                club.send_welcome_email_to_new_members,
                club.send_membership_expiration_reminders_30_days,
                club.send_membership_expiration_reminders,
                club.send_membership_renewal_confirmation,
            ]
        )
        context = {
            "club": club,
            "view": self,
            "mailchimp_configured": bool(settings.MAILCHIMP_CLIENT_ID),
            "audiences": audiences,
            "in_scope_count": mc.in_scope_members(club).count(),
            "subscribed_count": synced.filter(mailchimp_status="subscribed").count(),
            "unsubscribed_count": synced.filter(mailchimp_status__in=["unsubscribed", "cleaned"]).count(),
            "not_synced_count": not_synced_count,
            "has_emails_enabled": has_emails_enabled,
            "tags": ClubMember.MAILCHIMP_TAGS,
        }
        return render(request, "auctions/club_mailchimp_settings.html", context)


GOOGLE_CALENDAR_OAUTH_CLUB_SESSION_KEY = "google_calendar_oauth_club_slug"
GOOGLE_CALENDAR_OAUTH_STATE_SESSION_KEY = "google_calendar_oauth_state"


class ClubGoogleCalendarConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Google Calendar settings/status panel for a club."""

    active_tab = "google_calendar"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug):
        from auctions import google_calendar as gcal

        club = self.club
        upcoming, _ = club_events.upcoming_events(club)
        context = {
            "club": club,
            "view": self,
            "google_calendar_configured": gcal.is_configured(),
            "upcoming_count": upcoming.count(),
            "auction_event_count": club.events.filter(is_deleted=False, source=ClubEvent.SOURCE_AUCTION).count(),
            "discord_connected": bool(club.discord_server_id),
        }
        return render(request, "auctions/club_google_calendar_settings.html", context)

    def post(self, request, slug):
        """Save the settings checkboxes. Calendar sharing is read from Google, not set here; see
        google_calendar.refresh_public_flag.
        """
        club = self.club
        club.add_auctions_to_calendar = "add_auctions_to_calendar" in request.POST
        club.create_discord_events_for_club_events = "create_discord_events_for_club_events" in request.POST
        club.save(
            update_fields=[
                "add_auctions_to_calendar",
                "create_discord_events_for_club_events",
            ]
        )
        messages.success(request, "Calendar settings saved.")
        return redirect(reverse("club_google_calendar_config", kwargs={"slug": club.slug}))


class GoogleCalendarConnectView(LoginRequiredMixin, View):
    """Start the Google Calendar OAuth flow (requires permission_edit_club)."""

    def get(self, request, slug):
        from auctions import google_calendar as gcal

        club = get_object_or_404(Club, slug=slug)
        if not check_club_permission(request.user, club, "permission_edit_club"):
            raise PermissionDenied()
        config_url = reverse("club_google_calendar_config", kwargs={"slug": club.slug})
        if not gcal.is_configured():
            messages.error(request, "Google Calendar is not configured on this site. Contact your site administrator.")
            return redirect(config_url)
        # The callback has no slug.
        request.session[GOOGLE_CALENDAR_OAUTH_CLUB_SESSION_KEY] = club.slug
        # A fresh nonce, not the unsubscribe UUID, which is in every email footer.
        state = secrets.token_urlsafe(32)
        request.session[GOOGLE_CALENDAR_OAUTH_STATE_SESSION_KEY] = state
        redirect_uri = request.build_absolute_uri(reverse("google_calendar_callback"))
        return redirect(gcal.authorize_url(redirect_uri, state))


class GoogleCalendarCallbackView(LoginRequiredMixin, View):
    """Google's OAuth callback: store tokens, provision the calendar, push existing events."""

    def get(self, request):
        from auctions import google_calendar as gcal

        slug = request.session.get(GOOGLE_CALENDAR_OAUTH_CLUB_SESSION_KEY)
        club = Club.objects.filter(slug=slug).first() if slug else None
        if not club or not check_club_permission(request.user, club, "permission_edit_club"):
            messages.error(request, "Your Google Calendar connection session expired. Please try again.")
            return redirect(reverse("home"))

        config_url = reverse("club_google_calendar_config", kwargs={"slug": club.slug})
        error = request.GET.get("error")
        if error:
            messages.error(request, f"Google authorization failed: {error}")
            return redirect(config_url)

        code = request.GET.get("code")
        state = request.GET.get("state")
        expected_state = request.session.pop(GOOGLE_CALENDAR_OAUTH_STATE_SESSION_KEY, "")
        if not code or not expected_state or not secrets.compare_digest(state or "", expected_state):
            messages.error(request, "Invalid Google authorization response. Please try again.")
            return redirect(config_url)

        redirect_uri = request.build_absolute_uri(reverse("google_calendar_callback"))
        try:
            refresh_token, access_token, expires_in, account_email = gcal.exchange_code(code, redirect_uri)
        except gcal.GoogleCalendarError as exc:
            logger.exception("Google Calendar token exchange failed for club %s", club.pk)
            messages.error(request, str(exc))
            return redirect(config_url)

        club.google_calendar_refresh_token = refresh_token
        club.google_calendar_access_token = access_token
        club.google_calendar_token_expires = timezone.now() + timedelta(seconds=int(expires_in))
        club.google_calendar_account_email = account_email
        club.google_calendar_connected_on = timezone.now()
        club.google_calendar_connected_by = request.user
        club.google_calendar_sync_token = ""
        club.google_calendar_last_error = ""
        club.save(
            update_fields=[
                "google_calendar_refresh_token",
                "google_calendar_access_token",
                "google_calendar_token_expires",
                "google_calendar_account_email",
                "google_calendar_connected_on",
                "google_calendar_connected_by",
                "google_calendar_sync_token",
                "google_calendar_last_error",
            ]
        )
        request.session.pop(GOOGLE_CALENDAR_OAUTH_CLUB_SESSION_KEY, None)

        try:
            gcal.ensure_calendar(club)
        except gcal.GoogleCalendarError as exc:
            logger.exception("Could not set up the Google calendar for club %s", club.pk)
            # One message: a calendar id still on the club is from the previous connection.
            club.google_calendar_last_error = str(exc)[:500]
            club.save(update_fields=["google_calendar_last_error"])
            messages.error(request, f"Connected to Google, but we couldn't set up the calendar: {exc}")
            return redirect(config_url)

        # So the calendar isn't empty on arrival.
        club_events.sync_auction_events(club)
        gcal.sync_club(club)
        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Connected Google Calendar ({account_email or 'account'})",
            applies_to="SETTINGS",
        )
        messages.success(request, "Google Calendar connected! Your events are syncing now.")
        return redirect(config_url)


class GoogleCalendarSyncNowView(LoginRequiredMixin, ClubViewMixin, View):
    """Run a full sync now."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import google_calendar as gcal

        club = self.club
        config_url = reverse("club_google_calendar_config", kwargs={"slug": club.slug})
        if not club.google_calendar_connected:
            messages.error(request, "Google Calendar is not connected.")
            return redirect(config_url)
        club_events.sync_club(club)
        club.refresh_from_db()
        if club.google_calendar_last_error:
            messages.error(request, f"Sync failed: {club.google_calendar_last_error}")
            return redirect(config_url)
        # Force the sharing check past its rate limit.
        gcal.refresh_public_flag(club, force=True)
        messages.success(request, "Calendar synced.")
        return redirect(config_url)


class GoogleCalendarDisconnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Forget the Google connection; the calendar stays in the club's Google account."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import google_calendar as gcal

        club = self.club
        gcal.disconnect(club)
        ClubHistory.objects.create(
            club=club, user=request.user, action="Disconnected Google Calendar", applies_to="SETTINGS"
        )
        messages.success(
            request,
            "Google Calendar disconnected. The calendar itself is still in your Google account — "
            "delete it there if you no longer want it.",
        )
        return redirect(reverse("club_google_calendar_config", kwargs={"slug": club.slug}))


class ClubEventCreateView(LoginRequiredMixin, ClubViewMixin, View):
    """The 'Add event' button on the club page."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self._can_manage():
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def _can_manage(self):
        return (
            self.user_has_club_permission("permission_admin")
            or self.user_has_club_permission("permission_manage_auctions")
            or self.user_has_club_permission("permission_edit_club")
        )

    def get(self, request, slug):
        form = ClubEventForm(user_timezone=_browser_timezone(request))
        return render(request, "auctions/club_event_form.html", self._context(form))

    def post(self, request, slug):
        form = ClubEventForm(request.POST, user_timezone=_browser_timezone(request))
        if not form.is_valid():
            return render(request, "auctions/club_event_form.html", self._context(form))
        event = form.save(commit=False)
        event.club = self.club
        event.created_by = request.user
        event.source = ClubEvent.SOURCE_MANUAL
        event.save()
        _push_event_to_integrations(request, event)
        messages.success(request, f"Added {event.title}.")
        return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))

    def _context(self, form):
        return {"club": self.club, "view": self, "form": form, "is_edit": False}


def _browser_timezone(request):
    """The admin's timezone, which forms render and must parse in."""
    return request.COOKIES.get("user_timezone", settings.TIME_ZONE)


class ClubEventUpdateView(LoginRequiredMixin, ClubViewMixin, View):
    """Edit or delete one club event."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        self.event = get_object_or_404(ClubEvent, club=self.club, pk=kwargs.get("pk"), is_deleted=False)
        if request.user.is_authenticated and not self._can_manage():
            raise PermissionDenied()
        # Generated events can be edited too; the form narrows to the fields the club owns.
        if not self.event.details_are_editable:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def _can_manage(self):
        if (
            self.user_has_club_permission("permission_admin")
            or self.user_has_club_permission("permission_manage_auctions")
            or self.user_has_club_permission("permission_edit_club")
        ):
            return True
        # An admin of the generated event's auction may edit its wording without a club role.
        related_auction = self.event.related_auction
        return bool(related_auction and related_auction.permission_check(self.request.user))

    def get(self, request, slug, pk):
        form = ClubEventForm(instance=self.event, user_timezone=_browser_timezone(request))
        return render(request, "auctions/club_event_form.html", self._context(form))

    def post(self, request, slug, pk):
        club_url = reverse("club_detail", kwargs={"slug": self.club.slug})
        if request.POST.get("action") == "delete":
            # Never for a generated event: the next sync would rebuild it.
            if not self.event.is_editable:
                raise Http404
            title = self.event.title
            club_events.retire_event(self.event)
            messages.success(request, f"Deleted {title}.")
            return redirect(club_url)
        was_cancelled = self.event.cancelled
        previous_start = self.event.date_start
        form = ClubEventForm(request.POST, instance=self.event, user_timezone=_browser_timezone(request))
        if not form.is_valid():
            return render(request, "auctions/club_event_form.html", self._context(form))
        event = form.save(commit=False)
        if event.is_recurring and event.date_start != previous_start:
            # Moving one occurrence moves the stored series by the same amount.
            event.recurrence_start += event.date_start - previous_start
        event.needs_google_sync = True
        event.needs_discord_sync = True
        event.save()
        _push_event_to_integrations(request, event)
        if event.cancelled and not was_cancelled:
            messages.success(request, f"{event.title} is marked cancelled. Everyone subscribed has been told.")
        else:
            messages.success(request, f"Updated {event.title}.")
        return redirect(club_url)

    def _context(self, form):
        return {"club": self.club, "view": self, "form": form, "is_edit": True, "event": self.event}


def _push_event_to_integrations(request, event):
    """Push a just-saved event to Google Calendar and Discord inline. Failures are reported, never
    block the save; the periodic task retries.
    """
    from auctions import google_calendar as gcal

    club = event.club
    if club.google_calendar_connected:
        try:
            gcal.push_event(event)
        except gcal.GoogleCalendarError as exc:
            logger.warning("Could not push event %s to Google Calendar: %s", event.pk, exc)
            messages.warning(request, f"Saved, but Google Calendar didn't accept it yet: {exc}")
    discord_events.sync_one_event(club, event)


class ClubEventsICalView(View):
    """Public iCal feed of a club's events at /clubs/<slug>/events.ics, Google connected or not."""

    def get(self, request, slug):
        club = get_object_or_404(Club, slug=slug)
        upcoming, past = club_events.upcoming_events(club, include_past=True, past_limit=25)
        domain = Site.objects.get_current().domain
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:-//{domain}//Club events//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            f"X-WR-CALNAME:{_ical_escape(club.name)} events",
            # Without it, all-day and floating times land on the wrong day elsewhere.
            f"X-WR-TIMEZONE:{settings.TIME_ZONE}",
            # The standard spelling and Outlook/Google's.
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
            "X-PUBLISHED-TTL:PT1H",
        ]
        for event in list(past) + list(upcoming):
            lines += [
                "BEGIN:VEVENT",
                f"UID:{event.uuid}@{domain}",
                f"DTSTAMP:{_ical_datetime(event.updated_at)}",
                # Clients only take edits when the sequence increases; epoch seconds fit 32 bits.
                f"SEQUENCE:{int(event.updated_at.timestamp())}",
                *_ical_event_times(event),
                *event.recurrence_lines,
                f"SUMMARY:{_ical_escape(event.title)}",
                f"URL:https://{domain}{event.get_absolute_url()}",
                "STATUS:CANCELLED" if event.cancelled else "STATUS:CONFIRMED",
            ]
            if event.description:
                lines.append(f"DESCRIPTION:{_ical_escape(event.description)}")
            if event.location:
                lines.append(f"LOCATION:{_ical_escape(event.location)}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        response = HttpResponse("\r\n".join(lines), content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = f'inline; filename="{club.slug}-events.ics"'
        return response


def _ical_event_times(event):
    """DTSTART/DTEND for one event: a series starts at its anchor, and all-day events are dates."""
    start = event.recurrence_start if event.is_recurring else event.date_start
    end = start + event.occurrence_length
    if not event.all_day:
        return [f"DTSTART:{_ical_datetime(start)}", f"DTEND:{_ical_datetime(end)}"]
    start_day = timezone.localtime(start).date()
    end_day = timezone.localtime(end).date()
    if end_day <= start_day:
        end_day = start_day + timedelta(days=1)
    return [f"DTSTART;VALUE=DATE:{start_day:%Y%m%d}", f"DTEND;VALUE=DATE:{end_day:%Y%m%d}"]


def _ical_datetime(value):
    return f"{value.astimezone(date_tz.utc):%Y%m%dT%H%M%SZ}"


def _ical_escape(value):
    """Escape iCal structural characters. No line folding."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _log_esp_member_events(club, members, action_for_member):
    """Log mailing-list provider events about members to ClubHistory, with no acting user."""
    ClubHistory.objects.bulk_create(
        [
            ClubHistory(club=club, user=None, action=action_for_member(member), applies_to="MEMBERS")
            for member in members
        ]
    )


class MailchimpWebhookView(View):
    """Mailchimp unsubscribe/cleaned/upemail/profile callbacks.

    One-way sync: we record Mailchimp status so we never resubscribe them, but never touch site email
    prefs. The secret is in the URL path, since Mailchimp doesn't sign webhooks.
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def _get_club(self, slug, secret):
        club = Club.objects.filter(slug=slug).first()
        if not club or not club.mailchimp_webhook_secret:
            return None
        # Constant-time: the path secret is the only authentication.
        if not secrets.compare_digest(club.mailchimp_webhook_secret.encode(), (secret or "").encode()):
            return None
        return club

    def get(self, request, slug, secret):
        # Mailchimp GETs the URL to verify it.
        if not self._get_club(slug, secret):
            return HttpResponseForbidden("invalid")
        return HttpResponse("ok")

    def post(self, request, slug, secret):
        from auctions.models import ClubMember

        club = self._get_club(slug, secret)
        if not club:
            return HttpResponseForbidden("invalid")

        event_type = request.POST.get("type")
        members = ClubMember.objects.filter(club=club, is_deleted=False)

        if event_type == "upemail":
            # Mailchimp sends old_email/new_email for address changes.
            old_email = request.POST.get("data[old_email]") or request.POST.get("data[email]")
            new_email = request.POST.get("data[new_email]")
            if old_email and new_email:
                # Local only, not a site account change.
                renamed = list(members.filter(email__iexact=old_email))
                members.filter(email__iexact=old_email).update(email=new_email)
                _log_esp_member_events(
                    club, renamed, lambda member: f"{member} changed their email to {new_email} via Mailchimp"
                )
            return HttpResponse("ok")

        email = request.POST.get("data[email]") or request.POST.get("data[email_address]")
        if not email:
            # Without an email, the unfiltered queryset would unsubscribe the whole club.
            return HttpResponse("ok")
        members = members.filter(email__iexact=email)
        if event_type == "unsubscribe":
            affected = list(members)
            members.update(mailchimp_status="unsubscribed")
            _log_esp_member_events(club, affected, lambda member: f"{member} unsubscribed at Mailchimp")
        elif event_type == "cleaned":
            affected = list(members)
            members.update(mailchimp_status="cleaned")
            _log_esp_member_events(
                club, affected, lambda member: f"{member} marked undeliverable (cleaned) by Mailchimp"
            )
        # 'profile' events need no action under one-way sync.
        return HttpResponse("ok")


class ClubMemberSelfServiceView(View):
    """Public UUID-keyed email-preference links in Mailchimp merge fields. Changes club contact status only."""

    action = None  # "unsubscribe" | "resubscribe" | "nocomm"

    def _get_member(self, slug, uuid):
        from auctions.models import ClubMember

        return get_object_or_404(ClubMember, uuid=uuid, club__slug=slug, is_deleted=False)

    def get(self, request, slug, uuid):
        # GET only renders a confirmation; link scanners GET links, so the write is in post().
        member = self._get_member(slug, uuid)
        prompts = {
            "unsubscribe": (
                "Unsubscribe",
                f"Stop receiving marketing emails from {member.club.name}?",
            ),
            "resubscribe": (
                "Resubscribe",
                f"Start receiving emails from {member.club.name} again?",
            ),
            "nocomm": (
                "Do not contact me",
                f"Ask {member.club.name} to stop contacting you entirely?",
            ),
        }
        confirm_label, confirm_prompt = prompts.get(self.action, prompts["nocomm"])
        return render(
            request,
            "auctions/mailchimp_self_service.html",
            {
                "club": member.club,
                "member": member,
                "confirm_label": confirm_label,
                "confirm_prompt": confirm_prompt,
            },
        )

    def post(self, request, slug, uuid):
        from auctions.tasks import sync_club_member_to_brevo, sync_club_member_to_mailchimp

        member = self._get_member(slug, uuid)
        if self.action == "unsubscribe":
            member.contact_status = "non_essential"
            member.save(update_fields=["contact_status"])
            heading, body = "Unsubscribed", f"You will no longer receive marketing emails from {member.club.name}."
            history_action = f"{member} unsubscribed from marketing emails (self-service)"
        elif self.action == "resubscribe":
            member.contact_status = "contact"
            # Clear the remembered opt-out so the next sync resubscribes.
            member.mailchimp_status = ""
            member.brevo_status = ""
            member.save(update_fields=["contact_status", "mailchimp_status", "brevo_status"])
            heading, body = "Resubscribed", f"You will once again receive emails from {member.club.name}."
            history_action = f"{member} resubscribed to emails (self-service)"
        else:  # nocomm
            member.contact_status = "do_not_contact"
            member.save(update_fields=["contact_status"])
            heading, body = "Done", f"{member.club.name} will no longer contact you."
            history_action = f"{member} opted out of all contact (self-service)"
        ClubHistory.objects.create(
            club=member.club,
            user=None,
            action=history_action,
            applies_to="MEMBERS",
        )
        transaction.on_commit(lambda: sync_club_member_to_mailchimp.delay(member.pk))
        transaction.on_commit(lambda: sync_club_member_to_brevo.delay(member.pk))
        return render(
            request,
            "auctions/mailchimp_self_service.html",
            {"club": member.club, "heading": heading, "body": body, "member": member},
        )


# --- Brevo: same structure as the Mailchimp views. See auctions/brevo.py. ---


class BrevoConnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Store the club's Brevo API key, validated and encrypted; Brevo's OAuth isn't public."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import brevo

        club = self.club
        config_url = reverse("club_brevo_config", kwargs={"slug": club.slug})
        api_key = (request.POST.get("api_key") or "").strip()
        if not api_key:
            messages.error(request, "Please paste your Brevo API key.")
            return redirect(config_url)

        club.brevo_api_key = api_key
        try:
            brevo.list_contact_lists(brevo.get_client(club))
        except brevo.BrevoApiError as e:
            blocked_ip = brevo.blocked_ip_from_error(e)
            if blocked_ip is not None:
                # Valid key, but Brevo is blocking this server's IP.
                where = blocked_ip or brevo.outbound_ip() or "this server's IP address"
                logger.warning("Brevo blocked IP for club %s: %s", club.pk, e.detail)
                messages.error(
                    request,
                    "Your key looks valid, but Brevo is blocking this server's IP address. In Brevo, go to "
                    f"Settings → Security → Authorized IPs and add {where}, wait ~5 minutes, then try again.",
                )
            else:
                logger.warning("Brevo API key validation failed for club %s", club.pk)
                messages.error(request, "That Brevo API key didn't work. Double-check it and try again.")
            return redirect(config_url)
        except Exception:
            logger.exception("Brevo connect failed for club %s", club.pk)
            messages.error(request, "Couldn't reach Brevo right now. Please try again in a moment.")
            return redirect(config_url)

        club.brevo_connected_on = timezone.now()
        club.brevo_connected_by = request.user
        if not club.brevo_webhook_secret:
            club.brevo_webhook_secret = secrets.token_urlsafe(32)
        club.save(update_fields=["brevo_api_key", "brevo_connected_on", "brevo_connected_by", "brevo_webhook_secret"])
        ClubHistory.objects.create(club=club, user=request.user, action="Connected Brevo", applies_to="SETTINGS")
        messages.success(request, "Brevo connected! Now choose which list to sync your members into.")
        return redirect(config_url)


class BrevoListSelectView(LoginRequiredMixin, ClubViewMixin, View):
    """Pick an existing list or create '{club} Members', then provision + backfill."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import brevo

        club = self.club
        config_url = reverse("club_brevo_config", kwargs={"slug": club.slug})
        client = brevo.get_client(club)
        if not client:
            messages.error(request, "Brevo is not connected. Please connect first.")
            return redirect(config_url)

        choice = request.POST.get("list_id", "")
        try:
            if choice == "__new__":
                list_id, list_name = brevo.create_contact_list(client, club)
            else:
                list_id = choice
                list_name = next(
                    (lst["name"] for lst in brevo.list_contact_lists(client) if str(lst["id"]) == choice), ""
                )
        except (brevo.BrevoApiError, brevo.BrevoError):
            logger.exception("Brevo list selection failed for club %s", club.pk)
            messages.error(
                request, "Couldn't set up your Brevo list. Please try again, or create a list in Brevo first."
            )
            return redirect(config_url)

        if not list_id:
            messages.error(request, "Please choose a list.")
            return redirect(config_url)

        if not list_name:
            messages.error(request, "That list was not found in your Brevo account. Please choose a valid list.")
            return redirect(config_url)

        club.brevo_list_id = str(list_id)
        club.brevo_list_name = list_name
        club.save(update_fields=["brevo_list_id", "brevo_list_name"])

        brevo.ensure_attributes(club)
        brevo.ensure_webhook(club)
        count = brevo.backfill(club)

        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Connected Brevo list '{list_name}'",
            applies_to="SETTINGS",
        )
        messages.success(request, f"Syncing {count} member(s) into the '{list_name}' Brevo list.")
        info = brevo.account_info(brevo.get_client(club))
        if _prefill_donation_address(club, brevo.format_mailing_address(info.get("address")), "Brevo"):
            messages.info(
                request,
                "We also filled in your donation mailing address from Brevo — check it on the donation settings page.",
            )
        return redirect(config_url)


class BrevoSyncNowView(LoginRequiredMixin, ClubViewMixin, View):
    """Re-queue a sync for every in-scope member."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import brevo

        club = self.club
        config_url = reverse("club_brevo_config", kwargs={"slug": club.slug})
        if not club.brevo_connected:
            messages.error(request, "Brevo is not connected.")
            return redirect(config_url)
        count = brevo.backfill(club)
        messages.success(request, f"Queued {count} member(s) for syncing to Brevo.")
        return redirect(config_url)


class BrevoDisconnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Forget the Brevo connection. Leaves the list itself untouched in Brevo."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        club = self.club
        club.brevo_api_key = None
        club.brevo_list_id = ""
        club.brevo_list_name = ""
        club.brevo_folder_id = ""
        club.brevo_connected_on = None
        club.brevo_connected_by = None
        club.brevo_webhook_secret = ""
        club.brevo_webhook_id = ""
        club.brevo_last_error = ""
        club.save(
            update_fields=[
                "brevo_api_key",
                "brevo_list_id",
                "brevo_list_name",
                "brevo_folder_id",
                "brevo_connected_on",
                "brevo_connected_by",
                "brevo_webhook_secret",
                "brevo_webhook_id",
                "brevo_last_error",
            ]
        )
        ClubHistory.objects.create(club=club, user=request.user, action="Disconnected Brevo", applies_to="SETTINGS")
        messages.success(request, "Brevo disconnected.")
        return redirect(reverse("club_brevo_config", kwargs={"slug": club.slug}))


class ClubBrevoConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Brevo settings/status panel for a club."""

    active_tab = "brevo"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug):
        from auctions import brevo
        from auctions.models import ClubMember

        club = self.club
        lists = []
        # Connected (API key) but no list chosen yet -> offer the chooser.
        if club.brevo_api_key and not club.brevo_list_id:
            client = brevo.get_client(club)
            if client:
                try:
                    lists = brevo.list_contact_lists(client)
                except (brevo.BrevoApiError, brevo.BrevoError):
                    logger.exception("Could not list Brevo lists for club %s", club.pk)
                    messages.error(request, "Could not load your Brevo lists. Try reconnecting.")

        synced = ClubMember.objects.filter(club=club, is_deleted=False)
        not_synced_count = (
            brevo.in_scope_members(club).filter(brevo_last_synced__isnull=True).count() if club.brevo_list_id else 0
        )
        has_emails_enabled = any(
            [
                club.send_welcome_email_to_new_members,
                club.send_membership_expiration_reminders_30_days,
                club.send_membership_expiration_reminders,
                club.send_membership_renewal_confirmation,
            ]
        )
        context = {
            "club": club,
            "view": self,
            "lists": lists,
            # Only for the connect form, so admins can allowlist the IP.
            "server_ip": brevo.outbound_ip() if not club.brevo_api_key else "",
            "in_scope_count": brevo.in_scope_members(club).count(),
            "subscribed_count": synced.filter(brevo_status="subscribed").count(),
            "unsubscribed_count": synced.filter(brevo_status__in=["unsubscribed", "cleaned"]).count(),
            "not_synced_count": not_synced_count,
            "has_emails_enabled": has_emails_enabled,
            "tags": ClubMember.MAILCHIMP_TAGS,
        }
        return render(request, "auctions/club_brevo_settings.html", context)


class BrevoWebhookView(View):
    """Brevo unsubscribe/bounce/spam/delete callbacks.

    One-way sync like Mailchimp's: record status, never touch site prefs. Unsigned, so the secret is
    in the URL path.
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def _get_club(self, slug, secret):
        club = Club.objects.filter(slug=slug).first()
        if not club or not club.brevo_webhook_secret:
            return None
        # Constant-time: the path secret is the only authentication.
        if not secrets.compare_digest(club.brevo_webhook_secret.encode(), (secret or "").encode()):
            return None
        return club

    def get(self, request, slug, secret):
        if not self._get_club(slug, secret):
            return HttpResponseForbidden("invalid")
        return HttpResponse("ok")

    def post(self, request, slug, secret):
        from auctions.models import ClubMember

        club = self._get_club(slug, secret)
        if not club:
            return HttpResponseForbidden("invalid")

        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            return HttpResponse("ok")

        # Inbound events are snake_case, unlike registration's camelCase.
        event = (payload.get("event") or "").lower().replace("_", "")
        email = payload.get("email")
        if not email:
            return HttpResponse("ok")
        members = ClubMember.objects.filter(club=club, is_deleted=False, email__iexact=email)

        if event in ("unsubscribe", "unsubscribed"):
            affected = list(members)
            members.update(brevo_status="unsubscribed")
            _log_esp_member_events(club, affected, lambda member: f"{member} unsubscribed at Brevo")
        elif event in ("hardbounce", "spam"):
            affected = list(members)
            members.update(brevo_status="cleaned")
            _log_esp_member_events(club, affected, lambda member: f"{member} marked undeliverable ({event}) by Brevo")
        elif event == "contactdeleted":
            affected = list(members)
            members.update(brevo_status="archived")
            _log_esp_member_events(club, affected, lambda member: f"{member} deleted from the Brevo list")
        return HttpResponse("ok")


class CreateSquarePaymentLinkView(SquareAPIMixin, View):
    """Create a Square payment link for an invoice"""

    def _invoice_error_redirect(self, invoice):
        if invoice.club:
            return redirect(reverse("club_membership_pay", kwargs={"slug": invoice.club.slug}))
        return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))

    def dispatch(self, request, *args, **kwargs):
        self.invoice = get_object_or_404(Invoice, no_login_link=kwargs.pop("uuid"))
        if not self.invoice.show_square_button:
            messages.error(request, "Square payments are not available for this invoice")
            return self._invoice_error_redirect(self.invoice)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """Create the payment link"""
        member_pk = request.POST.get("member_pk") or request.GET.get("member_pk") or ""
        payment_url, error_message = self.create_payment_link(self.invoice, member_pk=member_pk)
        if not payment_url:
            messages.error(
                request, error_message or "Failed to create Square payment link. Please try again or contact support."
            )
            return self._invoice_error_redirect(self.invoice)

        # Add processing message and redirect to invoice to show status
        messages.info(
            request,
            "You'll see the payment confirmation on your invoice.  Payment generally confirms within a few minutes.",
        )
        return redirect(payment_url)


class SquareSuccessView(View):
    """Handle redirect after Square payment"""

    def get(self, request, *args, **kwargs):
        # The webhook updates the invoice.
        messages.info(request, "Square payment processing... Your invoice will be updated shortly.")

        return redirect(reverse("home"))


class SquareInfoView(TemplateView):
    template_name = "auctions/square_seller.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context["seller"] = SquareSeller.objects.filter(user=self.request.user).first()
            context["auction"] = self.request.user.userdata.last_auction_created
        else:
            context["seller"] = None
            context["auction"] = None
        return context


class SquareSellerDeleteView(LoginRequiredMixin, DeleteView):
    template_name = "auctions/square_seller_confirm_delete.html"
    model = SquareSeller

    def get_object(self, queryset=None):
        return get_object_or_404(SquareSeller, user=self.request.user)

    def get_success_url(self):
        return reverse("square_seller")
