"""The outside accounts a club connects: Mailchimp, Brevo, Google Calendar, Square links.

Every one of these is an OAuth connect, a callback, a "sync now" and a disconnect, written the same
way four times over. The club event views in the middle are here because they are what the calendar
sync writes to. Connecting any of these from inside the mobile app needs the web-session handoff --
see ``docs/app_oauth_connect_flows.md``.
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
from .payments import MAILCHIMP_OAUTH_CLUB_SESSION_KEY, SquareAPIMixin

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
        # Stash the club so the callback (which has no slug) knows what we're connecting.
        request.session[MAILCHIMP_OAUTH_CLUB_SESSION_KEY] = club.slug
        params = {
            "response_type": "code",
            "client_id": settings.MAILCHIMP_CLIENT_ID,
            "redirect_uri": request.build_absolute_uri(reverse("mailchimp_callback")),
            # Reuse the per-user unsubscribe UUID as the anti-CSRF state, same as Square.
            "state": request.user.userdata.unsubscribe_link,
        }
        return redirect("https://login.mailchimp.com/oauth2/authorize?" + urlencode(params))


class MailchimpCallbackView(LoginRequiredMixin, View):
    """Mailchimp redirects here after the user authorizes. Stores the token, then sends the
    admin back to the config page to pick an audience."""

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
    """Fill in the club's donation mailing address from a marketing provider, if it is still blank.

    Both providers make a club type a real postal address when it signs up, because US bulk
    commercial email has to carry one -- and that is the same address the donation letters need
    (Club.donation_mailing_address, printed under the sign-off of every request a club sends a
    vendor). A club that has already told Mailchimp where it is should not be asked again here.

    Only ever fills a blank. An address the club typed itself is the club's, and a later reconnect
    must never quietly rewrite the return address on its mail. Returns True if it filled one in.
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
    """Pick an existing audience or create '{club} Members', then provision + backfill."""

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
                # Sender and mailing address come from the club's own Mailchimp account, never
                # from here -- see mailchimp.account_defaults.
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
    """Forget the Mailchimp connection. Leaves the audience itself untouched in Mailchimp."""

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
        """Save the checkboxes on the settings page.

        There is deliberately no "this calendar is public" box among them any more. Sharing is a
        fact about the calendar rather than a preference about this site, and we can read it — see
        google_calendar.refresh_public_flag, which every sync runs.
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
    """Start the Google Calendar OAuth flow for a club (requires permission_edit_club)."""

    def get(self, request, slug):
        from auctions import google_calendar as gcal

        club = get_object_or_404(Club, slug=slug)
        if not check_club_permission(request.user, club, "permission_edit_club"):
            raise PermissionDenied()
        config_url = reverse("club_google_calendar_config", kwargs={"slug": club.slug})
        if not gcal.is_configured():
            messages.error(request, "Google Calendar is not configured on this site. Contact your site administrator.")
            return redirect(config_url)
        # Stash the club so the callback (which has no slug) knows what we're connecting.
        request.session[GOOGLE_CALENDAR_OAUTH_CLUB_SESSION_KEY] = club.slug
        # A fresh nonce per attempt, not the per-user unsubscribe UUID: that one is printed in
        # the footer of every email we send, so anyone holding one could hand this club's admin a
        # callback URL that connects their calendar to someone else's Google account.
        state = secrets.token_urlsafe(32)
        request.session[GOOGLE_CALENDAR_OAUTH_STATE_SESSION_KEY] = state
        redirect_uri = request.build_absolute_uri(reverse("google_calendar_callback"))
        return redirect(gcal.authorize_url(redirect_uri, state))


class GoogleCalendarCallbackView(LoginRequiredMixin, View):
    """Google redirects here after the admin authorizes. Stores the tokens, provisions the
    calendar, and pushes whatever the club already has on its event list."""

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
            # Deliberately one message. ensure_calendar only stores a calendar id once the calendar
            # exists, so an id still on the club here is the *old* one from a previous connection --
            # reading it as "the calendar exists" told admins the half that failed had worked.
            club.google_calendar_last_error = str(exc)[:500]
            club.save(update_fields=["google_calendar_last_error"])
            messages.error(request, f"Connected to Google, but we couldn't set up the calendar: {exc}")
            return redirect(config_url)

        # Mirror the club's auctions and push everything, so the calendar isn't empty on arrival.
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
    """Run a full sync right now, so an admin doesn't have to wait for the periodic task."""

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
        # An admin pressing this has usually just changed something in Google Calendar, and one of
        # the things they change is sharing. Skipping the hourly rate limit here is what makes
        # "I ticked the box in Google, why does this still say Private" answerable in one click.
        gcal.refresh_public_flag(club, force=True)
        messages.success(request, "Calendar synced.")
        return redirect(config_url)


class GoogleCalendarDisconnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Forget the Google connection. The calendar itself stays in the club's Google account."""

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
    """The timezone the admin is actually looking at times in.

    base.html renders every page inside {% timezone user_timezone %}, so a form shows its times
    in this zone; the form has to parse them back in the same one.
    """
    return request.COOKIES.get("user_timezone", settings.TIME_ZONE)


class ClubEventUpdateView(LoginRequiredMixin, ClubViewMixin, View):
    """Edit or delete one club event."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        self.event = get_object_or_404(ClubEvent, club=self.club, pk=kwargs.get("pk"), is_deleted=False)
        if request.user.is_authenticated and not self._can_manage():
            raise PermissionDenied()
        # A generated event reaches this form too, and the form narrows itself to the two fields
        # a club owns there. It used to 404, which left "our meeting is at the auction" with
        # nowhere to be typed except Google Calendar, where the next push overwrote it.
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
        # An admin of the auction behind a generated event, who may hold no club role at all --
        # the auction's own creator is the usual one. The wording of that event is the auction's
        # to write: ClubEventForm narrows itself to the title and description on anything
        # generated, and delete is refused below, so this reaches nothing else on the calendar.
        # Without it the "customize this event" prompt on the auction page led to a 403 for
        # exactly the person it was written for.
        related_auction = self.event.related_auction
        return bool(related_auction and related_auction.permission_check(self.request.user))

    def get(self, request, slug, pk):
        form = ClubEventForm(instance=self.event, user_timezone=_browser_timezone(request))
        return render(request, "auctions/club_event_form.html", self._context(form))

    def post(self, request, slug, pk):
        club_url = reverse("club_detail", kwargs={"slug": self.club.slug})
        if request.POST.get("action") == "delete":
            # Never for a generated event: the auction is what put it here, and deleting the row
            # only means the next sync builds it again. Unpromote the auction instead.
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
            # The form edits one occurrence of a series, but the series is what's stored. Moving
            # the occurrence moves the whole thing by the same amount, which is what an admin who
            # pushed a weekly meeting an hour later means.
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
    """Send a just-saved event to Google Calendar and Discord.

    Done inline so an admin sees the result immediately rather than waiting for the periodic
    task. Failures are reported but never block the save — the event is already on the club page,
    and the periodic task retries the push.
    """
    from auctions import google_calendar as gcal

    club = event.club
    if club.google_calendar_connected:
        try:
            gcal.push_event(event)
        except gcal.GoogleCalendarError as exc:
            logger.warning("Could not push event %s to Google Calendar: %s", event.pk, exc)
            messages.warning(request, f"Saved, but Google Calendar didn't accept it yet: {exc}")
    # Creates it, moves it, or takes it back down if the event was just called off.
    discord_events.sync_one_event(club, event)


class ClubEventsICalView(View):
    """A public iCal feed of a club's events, at /clubs/<slug>/events.ics.

    Works whether or not the club has connected Google Calendar, so any club can hand members a
    subscribe link. Anyone with the URL can read it — the same events are already on the public
    club page.
    """

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
            # Without a timezone, all-day events and floating times land on the wrong day for
            # anyone reading the feed from elsewhere.
            f"X-WR-TIMEZONE:{settings.TIME_ZONE}",
            # Both spellings of "check back in an hour": the standard one and Outlook/Google's.
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
            "X-PUBLISHED-TTL:PT1H",
        ]
        for event in list(past) + list(upcoming):
            lines += [
                "BEGIN:VEVENT",
                f"UID:{event.uuid}@{domain}",
                f"DTSTAMP:{_ical_datetime(event.updated_at)}",
                # Clients keep the copy they already imported unless the sequence goes up, so an
                # edit here would never reach them. Seconds since the epoch is monotonic and fits
                # the 32-bit integer the spec asks for.
                f"SEQUENCE:{int(event.updated_at.timestamp())}",
                *_ical_event_times(event),
                # The rule itself, so a subscriber's calendar repeats the event the way Google
                # does instead of receiving one copy of it.
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
    """DTSTART/DTEND for one event.

    A repeating event starts where its series is anchored, not at the occurrence the club page
    happens to be showing — the RRULE that follows is measured from DTSTART, so anything else
    would hand subscribers a different set of dates than Google has.

    All-day events are dates, not times, or a calendar shows them as a midnight-to-midnight
    appointment instead of a day.
    """
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
    """Escape the characters iCal treats as structure. Long-line folding is not needed here —
    every consumer we care about handles long lines, and folding is easy to get wrong."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _log_esp_member_events(club, members, action_for_member):
    """Record what a mailing-list provider just told us about a member, one entry each.

    The member did this at Mailchimp/Brevo rather than on the site, so the club's admins have no
    other way to see it — the on-site equivalent (ClubMemberSelfServiceView) already logs. There is
    no acting user: the actor is the ESP.
    """
    ClubHistory.objects.bulk_create(
        [
            ClubHistory(club=club, user=None, action=action_for_member(member), applies_to="MEMBERS")
            for member in members
        ]
    )


class MailchimpWebhookView(View):
    """Receive Mailchimp unsubscribe/cleaned/upemail/profile callbacks.

    One-way sync means we only honor unsubscribe-style events here: we record the member's
    Mailchimp status so we never re-subscribe them, but we never touch their site email prefs.
    The shared secret lives in the URL path (Mailchimp does not sign webhooks).
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def _get_club(self, slug, secret):
        club = Club.objects.filter(slug=slug).first()
        if not club or not club.mailchimp_webhook_secret:
            return None
        # Constant-time compare: this URL-path secret is the only thing authenticating the webhook.
        if not secrets.compare_digest(club.mailchimp_webhook_secret.encode(), (secret or "").encode()):
            return None
        return club

    def get(self, request, slug, secret):
        # Mailchimp GETs the URL once to verify it when the webhook is created.
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
                # Reflect the new address locally; explicitly NOT a site-wide account change.
                renamed = list(members.filter(email__iexact=old_email))
                members.filter(email__iexact=old_email).update(email=new_email)
                _log_esp_member_events(
                    club, renamed, lambda member: f"{member} changed their email to {new_email} via Mailchimp"
                )
            return HttpResponse("ok")

        email = request.POST.get("data[email]") or request.POST.get("data[email_address]")
        if not email:
            # Every real unsubscribe/cleaned event names a contact. Without one there is nobody to
            # act on, and acting on the unfiltered queryset would mark the whole club unsubscribed.
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
    """Public, UUID-keyed self-service email-preference links embedded in Mailchimp merge fields.

    These only change the member's *club* contact status; they never touch the user's site
    account or other clubs. ``action`` is set per-URL.
    """

    action = None  # "unsubscribe" | "resubscribe" | "nocomm"

    def _get_member(self, slug, uuid):
        from auctions.models import ClubMember

        return get_object_or_404(ClubMember, uuid=uuid, club__slug=slug, is_deleted=False)

    def get(self, request, slug, uuid):
        # Read-only. The write happens in post() so that email link-scanners and prefetchers
        # (Outlook SafeLinks, etc.), which routinely GET links, can't silently flip a member's
        # contact status. GET just renders a confirmation page with a button that POSTs.
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
            # Clear the remembered opt-out so the next sync actually re-subscribes them.
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


# --- Brevo: built the same way as the Mailchimp views above (OAuth connect, list select,
# sync/disconnect, status page, and an inbound unsubscribe webhook). See auctions/brevo.py. ---


class BrevoConnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Store the club's Brevo API key (validated against Brevo) — the API-key analog of an OAuth
    connect, since Brevo's public OAuth program is private/org-scoped. The key is held encrypted."""

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

        # Validate the key with a lightweight authenticated call before saving it.
        club.brevo_api_key = api_key
        try:
            brevo.list_contact_lists(brevo.get_client(club))
        except brevo.BrevoApiError as e:
            blocked_ip = brevo.blocked_ip_from_error(e)
            if blocked_ip is not None:
                # Valid-looking key, but Brevo is blocking this server's IP. Tell them what to allow.
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
            # Only looked up while showing the connect form, so admins can pre-authorize the IP.
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
    """Receive Brevo marketing unsubscribe/bounce/spam/delete callbacks.

    One-way sync means we only honor opt-out-style events here: we record the member's Brevo
    status so we never re-subscribe them, but we never touch their site email prefs. Brevo sends
    a JSON body and does not sign it, so (like Mailchimp) the shared secret lives in the URL path.
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def _get_club(self, slug, secret):
        club = Club.objects.filter(slug=slug).first()
        if not club or not club.brevo_webhook_secret:
            return None
        # Constant-time compare: this URL-path secret is the only thing authenticating the webhook.
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

        # Brevo's inbound event names use snake_case (unsubscribe / hard_bounce / contact_deleted),
        # unlike the camelCase used when registering the webhook. Normalize before matching.
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
        # Square payment link can include order_id or reference_id in query params
        # For now, show processing message and redirect to home
        # The webhook will update the invoice status
        messages.info(request, "Square payment processing... Your invoice will be updated shortly.")

        # Try to get invoice reference if available
        # Square may pass back custom data in query params depending on configuration
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
