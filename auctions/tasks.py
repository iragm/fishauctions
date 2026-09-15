"""Celery tasks for the auctions app. Many wrap the management command of the same name."""

import datetime
import json
import logging
from html import escape

import httpx
import requests
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management import call_command
from django_celery_beat.models import ClockedSchedule, PeriodicTask
from post_office import mail

from auctions import geocoding

# Constants for update_auction_stats scheduling
STATS_UPDATE_LOCK_MINUTES = 5  # Minutes to lock auction before recalculation to prevent concurrent updates
STATS_UPDATE_MAX_DELAY_SECONDS = 3600  # Maximum delay (1 hour) before checking for new auctions
STATS_UPDATE_FALLBACK_DELAY_SECONDS = 3600  # Fallback delay when no auctions need updates
AUCTION_STATS_TASK_NAME = "auction_stats_update"  # Name for the one-off scheduled task

# Constants for BAP recalculation scheduling
BAP_RECALCULATION_TASK_PREFIX = "bap_recalculation_club_"

# One club-calendar sync at a time; see sync_club_calendars.
CALENDAR_SYNC_LOCK_KEY = "sync_club_calendars_running"
CALENDAR_SYNC_LOCK_SECONDS = 60 * 60

# One endauctions at a time. Past the hard time limit so a killed worker can't wedge the lock.
ENDAUCTIONS_LOCK_KEY = "endauctions_running"
ENDAUCTIONS_LOCK_SECONDS = 15 * 60

# One-shot backfill of PageView.auction. SCAN bounds primary keys looked at, so a run over rows with
# no lot views can't turn into a full scan. The beat name must match fishauctions/celery.py: the task
# switches its own PeriodicTask row off.
PAGE_VIEW_BACKFILL_JOB = "page_view_auction"
PAGE_VIEW_BACKFILL_BEAT = "backfill_page_view_auctions"
PAGE_VIEW_BACKFILL_CHUNK = 5000
PAGE_VIEW_BACKFILL_SCAN = 50000
PAGE_VIEW_BACKFILL_LOCK_KEY = "backfill_page_view_auctions_running"
PAGE_VIEW_BACKFILL_LOCK_SECONDS = 20 * 60

logger = logging.getLogger(__name__)


def _per_item(task, label, items, do_one, exceptions=(requests.RequestException,)):
    """Run ``do_one`` over ``items``, continuing past failures, then retry the task once.

    Re-raising on the first failure left everyone after it with a wallet pass still saying "valid". The
    retry re-runs successful items too; they are idempotent PATCHes, which is the accepted trade.
    """
    failures = []
    for item in items:
        try:
            do_one(item)
        except exceptions as e:
            logger.exception("%s failed for %s", label, item)
            failures.append(f"{item}: {e}")
    if failures:
        raise task.retry(exc=RuntimeError(f"{label}: " + "; ".join(failures[:10])))


def _membership_email_reply_to(club):
    fallback_reply_to = settings.DEFAULT_FROM_EMAIL
    if settings.ADMINS:
        fallback_reply_to = settings.ADMINS[0][1]
    return club.contact_email or fallback_reply_to


def _club_member_membership_link(member, current_site=None):
    current_site = current_site or Site.objects.get_current()
    return f"https://{current_site.domain}{member.member_page_url}"


def _greeting_name(member):
    name = (member.name or "").strip()
    return name or "Member"


# Inline style for wallet buttons in email: clients strip <style> and know no Bootstrap.
_WALLET_BUTTON_STYLE = (
    "display:inline-block;padding:10px 16px;margin:0 8px 8px 0;background:#303030;color:#ffffff;"
    "text-decoration:none;border-radius:6px;font-family:sans-serif;font-size:14px;"
)


def wallet_links(member, current_site=None):
    """Return (google_url, apple_url) for this member's wallet card. "" when unconfigured or barcodes are
    off. UUID-keyed capability URLs, so they work from email without signing in.
    """
    if not member.club.show_member_barcode:
        return "", ""
    from django.urls import reverse

    from auctions import apple_wallet
    from auctions.templatetags.membership_tags import google_wallet_save_url

    current_site = current_site or Site.objects.get_current()
    google_url = google_wallet_save_url(member) or ""
    apple_url = ""
    if apple_wallet.is_configured():
        path = reverse("club_member_apple_wallet_by_uuid", kwargs={"slug": member.club.slug, "uuid": member.uuid})
        apple_url = f"https://{current_site.domain}{path}"
    return google_url, apple_url


def _wallet_buttons_html(google_url, apple_url):
    """The "Add to Google/Apple Wallet" buttons that sit under the barcode in membership emails."""
    buttons = []
    if google_url:
        buttons.append(f"<a href='{escape(google_url)}' style='{_WALLET_BUTTON_STYLE}'>Add to Google Wallet</a>")
    if apple_url:
        buttons.append(f"<a href='{escape(apple_url)}' style='{_WALLET_BUTTON_STYLE}'>Add to Apple Wallet</a>")
    if not buttons:
        return ""
    return f"<div>{''.join(buttons)}</div>"


def next_event_fragment(club, current_site, *, include_event=True, as_links=True):
    """Return (text, html) for the "our next event" line, or ('', '').

    Shared by the emails and the settings preview (``as_links=False``). The calendar subscribe link
    rides on it, so a club that turned the next event off gets no calendar pitch either.
    """
    from auctions import club_events

    if not include_event:
        return "", ""
    event = club_events.next_member_facing_event(club)
    if not event:
        return "", ""

    auction = event.auction
    when = event.date_start
    show_time = True
    if auction and auction.is_online:
        # An online auction runs for days, so a start time next to the date is just noise.
        show_time = False
    elif auction:
        # In-person: the pickup time is when members gather; date_start is when bidding opens.
        when = _in_person_auction_time(auction) or event.date_start
    date_str = f"{when:%B %-d, %Y}"
    if show_time:
        date_str = f"{date_str} at {when:%-I:%M %p}"

    details_url = f"https://{current_site.domain}{event.get_absolute_url()}"
    details_label = "Read the auction's rules" if auction else "See the details"
    directions_url = _event_directions_url(event)

    text_parts = [f"Our next event is {event.title}", f"on {date_str}"]
    text = " ".join(text_parts).rstrip() + "."
    if directions_url:
        text += f" Get directions: {directions_url}"
    text += f" {details_label}: {details_url}"

    html = " ".join(escape(part) for part in text_parts).rstrip() + "."
    if directions_url:
        html += (
            f" <a href='{escape(directions_url)}'>Get directions</a>."
            if as_links
            else " <span class='text-info'>Get directions</span>."
        )
    html += (
        f" <a href='{escape(details_url)}'>{escape(details_label)}</a>."
        if as_links
        else f" <span class='text-info'>{escape(details_label)}</span>."
    )

    # The club's Google calendar when shared, else our feed; same rule as the club page.
    subscribe_url = club.calendar_subscribe_url(current_site.domain)
    text += f" Add our calendar: {subscribe_url}"
    html += (
        f" <a href='{escape(subscribe_url)}'>Add our calendar</a>."
        if as_links
        else " <span class='text-info'>Add our calendar</span>."
    )
    return text, html


def _real_physical_locations(auction):
    """The auction's physical locations, minus the placeholder an in-person switch auto-creates."""
    return [
        location
        for location in auction.physical_location_qs
        if (location.address or "").strip() or location.has_coordinates
    ]


def _in_person_auction_time(auction):
    """When an in-person auction gathers, or None. Only with a single location."""
    locations = [location for location in _real_physical_locations(auction) if location.pickup_time]
    if len(locations) == 1:
        return locations[0].pickup_time
    return None


def _event_directions_url(event):
    """A map link for the event, falling back to the auction's location only when it has exactly one."""
    if event.location:
        return event.map_url
    auction = event.related_auction
    if not auction:
        return ""
    locations = _real_physical_locations(auction)
    if len(locations) == 1:
        return locations[0].directions_link
    return ""


def _render_membership_email_html(
    member,
    intro_text,
    message_text,
    membership_link,
    club_icon_url,
    barcode_url,
    next_event_html,
    opening_text="",
    closing_text="",
    wallet_buttons_html="",
):
    html_parts = [f"Dear {escape(_greeting_name(member))},<br><br>"]
    if opening_text:
        html_parts.append(escape(opening_text).replace("\n", "<br>"))
        html_parts.append("<br><br>")
    if intro_text:
        html_parts.append(escape(intro_text).replace("\n", "<br>"))
        html_parts.append("<br><br>")
    html_parts.append(f"{escape(message_text)}<br><br>")
    html_parts.append(f"<a href='{escape(membership_link)}'>View your membership</a><br><br>")
    if barcode_url:
        html_parts.append(
            f"<div><img src='{escape(barcode_url)}' alt='Membership barcode' "
            "style='max-width:320px;width:100%;height:auto;'></div>"
        )
        if wallet_buttons_html:
            html_parts.append(wallet_buttons_html)
        html_parts.append("<br>")
    if next_event_html:
        html_parts.append(f"{next_event_html}<br><br>")
    if closing_text:
        html_parts.append(escape(closing_text).replace("\n", "<br>"))
        html_parts.append("<br><br>")
    if club_icon_url:
        html_parts.append(
            f"<div><img src='{escape(club_icon_url)}' alt='{escape(member.club.name)}' "
            "style='height:32px;width:32px;object-fit:contain;vertical-align:middle;margin-right:8px;'>"
            f"{escape(member.club.name)}</div>"
        )
    else:
        html_parts.append(escape(member.club.name))
    return "".join(html_parts)


def send_club_member_email(member, subject, message_text, email_type="welcome", force_email=False):
    """Send one of the club's membership emails. ``force_email`` skips push, for an admin-confirmed resend."""
    if not member.email or member.contact_status == "do_not_contact":
        return False
    current_site = Site.objects.get_current()
    membership_link = _club_member_membership_link(member, current_site=current_site)
    intro_text = ""
    barcode_url = member.barcode_image_link_png if member.club.show_member_barcode else ""
    google_wallet_url, apple_wallet_url = wallet_links(member, current_site=current_site)

    opening_text = ""
    closing_text = ""
    include_event = member.club.include_next_auction_in_emails

    if email_type == "welcome":
        opening_text = member.club.welcome_opening
        closing_text = member.club.welcome_closing
        include_event = member.club.welcome_include_auction
    elif email_type == "renewal":
        opening_text = member.club.renewal_opening
        closing_text = member.club.renewal_closing
        include_event = member.club.renewal_include_auction
    elif email_type == "expiring_soon":
        opening_text = member.club.expiring_soon_opening
        closing_text = member.club.expiring_soon_closing
        include_event = member.club.expiring_soon_include_auction

    next_text, next_html = "", ""
    if include_event:
        next_text, next_html = next_event_fragment(member.club, current_site, include_event=include_event)

    text_parts = [f"Dear {_greeting_name(member)},", ""]
    if opening_text:
        text_parts.extend([opening_text, ""])
    text_parts.extend([intro_text, ""])
    text_parts.extend([message_text, "", f"View your membership here: {membership_link}"])
    if barcode_url:
        text_parts.extend(["", f"Membership barcode: {barcode_url}"])
        if google_wallet_url:
            text_parts.append(f"Add to Google Wallet: {google_wallet_url}")
        if apple_wallet_url:
            text_parts.append(f"Add to Apple Wallet: {apple_wallet_url}")
    if next_text:
        text_parts.extend(["", next_text])
    if closing_text:
        text_parts.extend(["", closing_text])
    text_parts.extend(["", member.club.name])
    club_icon_url = member.club.icon_display_url or ""
    if club_icon_url and not club_icon_url.startswith("http"):
        club_icon_url = f"https://{current_site.domain}{club_icon_url}"
    html_message = _render_membership_email_html(
        member,
        intro_text=intro_text,
        message_text=message_text,
        membership_link=membership_link,
        club_icon_url=club_icon_url,
        barcode_url=barcode_url,
        next_event_html=next_html,
        opening_text=opening_text,
        closing_text=closing_text,
        wallet_buttons_html=_wallet_buttons_html(google_wallet_url, apple_wallet_url),
    )

    def _send_membership_email():
        mail.send(
            member.email,
            sender=member.club.contact_sender_email_with_name,
            subject=subject,
            message="\n".join(text_parts),
            html_message=html_message,
            headers={"Reply-to": _membership_email_reply_to(member.club)},
        )

    if force_email:
        _send_membership_email()
        return True

    # notify_user pushes only to opted-in users with a live device; everyone else is emailed.
    from auctions.notifications import notify_user

    notify_user(
        member.user,
        category="membership",
        title=subject,
        body=message_text or member.club.name,
        url=membership_link,
        send_email=_send_membership_email,
    )
    return True


def send_membership_card_email(member):
    """Email a member a link to their membership card. True when queued."""
    expiration_text = ""
    expiration = member.effective_expiration_date
    if expiration and member.club.membership_annual_fee:
        if member.is_paid_member:
            expiration_text = f"  Your membership is paid through {expiration.strftime('%B %-d, %Y')}."
        else:
            expiration_text = f"  Your membership expired on {expiration.strftime('%B %-d, %Y')}."
    message_text = (
        f"Here's the link to your {member.club.name} membership card."
        f"{expiration_text}  Scan it at club events, or add it to your phone's wallet."
    )
    return send_club_member_email(
        member,
        subject=f"Your {member.club.name} membership card",
        message_text=message_text,
        email_type="membership_card",
        force_email=True,
    )


def maybe_send_membership_renewal_confirmation(member):
    if not member.club.send_membership_renewal_confirmation:
        return False
    expiration_text = ""
    if member.membership_expiration_date:
        date_str = member.membership_expiration_date.strftime("%B %-d, %Y")
        expiration_text = f"  Your membership is paid through {date_str}."
    message_text = f"Your {member.club.name} membership has been renewed.{expiration_text}"
    if member.paypal_subscription_id:
        # PayPal subscription: renewal is automatic; link to PayPal's automatic payments page.
        message_text += (
            " This renews automatically through your PayPal subscription. To manage or cancel it, "
            "visit your PayPal automatic payments page: https://www.paypal.com/myaccount/autopay/"
        )
    sent = send_club_member_email(
        member,
        subject=f"Your {member.club.name} membership has been renewed",
        message_text=message_text,
        email_type="renewal",
    )
    if sent:
        # Logged here, not at the four call sites, because only this knows the member was told.
        from auctions.models import ClubHistory

        ClubHistory.objects.create(
            club=member.club,
            action=f"Sent renewal confirmation to {member} ({member.email})",
            applies_to="MEMBERSHIP",
        )
    return sent


@shared_task(bind=True, ignore_result=True)
def endauctions(self):
    """Set winners and prices on ended lots, send lot-ending websocket messages, deactivate lots.

    Locked: at a big auction's close a run can outlast the 60-second beat, and two runs would both
    invoice the same lots. A skipped tick is picked up next minute from the lots' own state.
    """
    from django.core.cache import cache

    if not cache.add(ENDAUCTIONS_LOCK_KEY, "1", timeout=ENDAUCTIONS_LOCK_SECONDS):
        logger.info("endauctions is already running; skipping this tick.")
        return
    try:
        call_command("endauctions")
    finally:
        cache.delete(ENDAUCTIONS_LOCK_KEY)


@shared_task(bind=True, ignore_result=True)
def sendnotifications(self):
    """Send notifications about watched items."""
    call_command("sendnotifications")


@shared_task(bind=True, ignore_result=True)
def auctiontos_notifications(self):
    """Welcome and print reminder emails."""
    call_command("auctiontos_notifications")


@shared_task(bind=True, ignore_result=True)
def refresh_club_health(self):
    """Recompute every club's lifecycle rollup and outreach queue. Nightly; see auctions/club_health.py."""
    from auctions import club_health

    written = club_health.refresh_all()
    # Keyed on the month: written on the first run, refreshed nightly, never waits for the 1st.
    month = club_health.snapshot_ladder()
    logger.info("refreshed club health for %s clubs, ladder snapshot for %s", written, month)


def _switch_off_beat_entry(name):
    """Disable a beat entry. ``save()``, not ``update()``: only ``post_save`` tells a running beat to reload."""
    row = PeriodicTask.objects.filter(name=name).first()
    if row and row.enabled:
        row.enabled = False
        row.save()
        logger.info("periodic task %s has nothing left to do; disabled it", name)


@shared_task(bind=True, ignore_result=True)
def backfill_page_view_auctions(self):
    """Fill in ``PageView.auction`` on lot views written before the beacon sent it.

    Until done, ``Auction.page_views`` needs ``auction_id OR lot.auction_id``, an OR across a join no
    index serves. Each run scans one primary-key window (``PAGE_VIEW_BACKFILL_SCAN``), writes up to
    ``PAGE_VIEW_BACKFILL_CHUNK`` rows, and keeps its cursor in ``ChunkedJobState``. The ceiling is the
    last pk at the first run; past it the job is marked finished and its beat entry switched off. Rows
    whose lot has no auction are skipped in Python so the cursor moves past them.
    """
    from collections import defaultdict

    from django.core.cache import cache
    from django.db.models import Max
    from django.utils import timezone

    from auctions.models import ChunkedJobState, PageView

    state, _ = ChunkedJobState.objects.get_or_create(name=PAGE_VIEW_BACKFILL_JOB)
    if state.finished:
        _switch_off_beat_entry(PAGE_VIEW_BACKFILL_BEAT)
        return

    if not cache.add(PAGE_VIEW_BACKFILL_LOCK_KEY, "1", timeout=PAGE_VIEW_BACKFILL_LOCK_SECONDS):
        logger.info("backfill_page_view_auctions is already running; skipping this tick.")
        return
    try:
        if not state.ceiling:
            state.ceiling = PageView.objects.aggregate(Max("pk"))["pk__max"] or 0
        window_end = min(state.cursor + PAGE_VIEW_BACKFILL_SCAN, state.ceiling + 1)
        rows = list(
            PageView.objects.filter(
                pk__gte=state.cursor,
                pk__lt=window_end,
                lot_number__isnull=False,
                auction__isnull=True,
            )
            .order_by("pk")
            .values_list("pk", "lot_number__auction")[:PAGE_VIEW_BACKFILL_CHUNK]
        )
        by_auction = defaultdict(list)
        for pk, auction_id in rows:
            if auction_id:
                by_auction[auction_id].append(pk)
        written = 0
        for auction_id, pks in by_auction.items():
            written += PageView.objects.filter(pk__in=pks).update(auction_id=auction_id)

        # A short read finishes the window; a full one resumes after the last row.
        state.cursor = rows[-1][0] + 1 if len(rows) == PAGE_VIEW_BACKFILL_CHUNK else window_end
        if state.cursor > state.ceiling:
            state.finished = timezone.now()
        state.save()
    finally:
        cache.delete(PAGE_VIEW_BACKFILL_LOCK_KEY)

    logger.info(
        "backfilled %s page views with their auction; cursor %s of %s%s",
        written,
        state.cursor,
        state.ceiling,
        " (finished)" if state.finished else "",
    )
    if state.finished:
        _switch_off_beat_entry(PAGE_VIEW_BACKFILL_BEAT)


@shared_task(bind=True, ignore_result=True)
def flush_expired_tokens(self):
    """Delete expired JWT blacklist and outstanding-token rows; rotation writes one per refresh."""
    call_command("flushexpiredtokens")


@shared_task(bind=True, ignore_result=True)
def delete_pending_accounts(self):
    """Delete accounts whose deletion grace period has expired. The promise depends on this running."""
    call_command("delete_pending_accounts")


@shared_task(bind=True, ignore_result=True, retry_backoff=True, retry_backoff_max=600, max_retries=5)
def delete_marketing_contact(self, club_pk, email):
    """Remove one address from a club's Mailchimp audience and Brevo list, on account deletion.

    Takes the address because the member row is already emptied. Retried by hand: neither provider's
    API error descends from ``RequestException``. Both are always attempted; a repeat delete is a 404.
    """
    from auctions import brevo
    from auctions import mailchimp as mc
    from auctions.models import Club

    club = Club.objects.filter(pk=club_pk).first()
    if not club or not email:
        return
    failures = []
    for provider, delete_contact in (
        ("Mailchimp", mc.delete_contact_by_email),
        ("Brevo", brevo.delete_contact_by_email),
    ):
        try:
            delete_contact(club, email)
        except Exception as e:
            logger.exception("Could not delete a deleted user's contact from %s for club %s", provider, club_pk)
            failures.append(f"{provider}: {e}")
    if failures:
        raise self.retry(exc=RuntimeError("; ".join(failures)))


@shared_task(bind=True, ignore_result=True)
def cleanup_mail(self):
    """Delete sent mail older than MAIL_RETENTION_DAYS, attachments included. Otherwise a deleted user's
    address survives in post_office.
    """
    call_command("cleanup_mail", days=settings.MAIL_RETENTION_DAYS, delete_attachments=True)


@shared_task(bind=True, ignore_result=True)
def send_announcement_emails(self, announcement_pk):
    """Send one club announcement through the ticked provider. No retry: a half-created campaign would be
    sent twice. Failures are written on the announcement.
    """
    from auctions import announcements
    from auctions.models import ClubAnnouncement

    announcement = ClubAnnouncement.objects.filter(pk=announcement_pk, is_deleted=False).select_related("club").first()
    if not announcement:
        return
    announcements.send_emails(announcement)


@shared_task(bind=True, ignore_result=True)
def send_scheduled_announcements(self):
    """Deliver due club announcements. Queued by the view with a countdown and by the beat each minute;
    ``send_due`` claims rows atomically, so both are safe.
    """
    from auctions import announcements

    announcements.send_due()


@shared_task(bind=True, ignore_result=True)
def refresh_announcement_opens(self, announcement_pk):
    """Pull the email open count for one announcement from whichever provider sent it."""
    from auctions import announcements
    from auctions.models import ClubAnnouncement

    announcement = ClubAnnouncement.objects.filter(pk=announcement_pk).select_related("club").first()
    if announcement:
        announcements.refresh_email_opens(announcement)


# ignore_result: the highest-volume task; results would pile up in Redis.
@shared_task(ignore_result=True)
def send_push_to_user(user_pk, *, title, body, url, category, collapse_key=None, auction_pk=None, invoice_pk=None):
    """Push to every enabled device of a user, clearing dead tokens.

    Logs one ``PushNotificationSent`` per device. ``collapse_key`` folds chatty categories. If no device
    is reached the notification is emailed instead, since the caller already marked it delivered.
    ``PUSH_ONLY_CATEGORIES`` are dropped instead.
    """
    from django.contrib.auth.models import User

    from auctions import notifications
    from auctions.models import MobileDevice, PushNotificationSent

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        return 0

    devices = MobileDevice.objects.filter(user=user, push_enabled=True).exclude(fcm_token="")
    sent_count = 0
    for device in devices:
        result = notifications.send_fcm_message(
            device.fcm_token,
            title=title,
            body=body,
            url=url,
            category=category,
            collapse_key=collapse_key,
        )
        if result == notifications.SEND_INVALID_TOKEN:
            # A dead token never comes back.
            device.fcm_token = ""
            device.save(update_fields=["fcm_token"])
            logger.info("Cleared dead FCM token for device %s (user %s)", device.pk, user_pk)
        elif result == notifications.SEND_OK:
            PushNotificationSent.objects.create(
                user=user,
                device=device,
                category=category,
                auction_id=auction_pk,
                invoice_id=invoice_pk,
            )
            sent_count += 1
    if not sent_count:
        _email_undelivered_push(user, title=title, body=body, url=url, category=category)
    return sent_count


def _email_undelivered_push(user, *, title, body, url, category):
    """Plain-text email for a push that reached no device; the caller's template is long gone."""
    from auctions import notifications

    if category in notifications.PUSH_ONLY_CATEGORIES or not user.email:
        return
    try:
        mail.send(user.email, subject=title, message=f"{body}\n\n{url}")
        logger.info("Push to user %s was undeliverable (%s); emailed instead", user.pk, category)
    except Exception:
        logger.exception("Could not email the undelivered %s push for user %s", category, user.pk)


def get_invoice_notification_task_name(invoice_pk):
    """Generate a unique task name for an invoice notification."""
    return f"invoice_notification_{invoice_pk}"


def schedule_invoice_notification(invoice_pk, run_at):
    """Schedule (or reschedule) a one-off invoice notification at ``run_at``."""
    schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)

    task_name = get_invoice_notification_task_name(invoice_pk)

    PeriodicTask.objects.update_or_create(
        name=task_name,
        defaults={
            "task": "auctions.tasks.send_invoice_notification",
            "clocked": schedule,
            "one_off": True,
            "enabled": True,
            "kwargs": json.dumps({"invoice_pk": invoice_pk}),
        },
    )


def cancel_invoice_notification(invoice_pk):
    """Cancel a scheduled invoice notification."""
    task_name = get_invoice_notification_task_name(invoice_pk)
    PeriodicTask.objects.filter(name=task_name).delete()


@shared_task(bind=True, ignore_result=True)
def send_invoice_notification(self, invoice_pk):
    """Send a scheduled invoice notification, if still needed. Idempotent; removes its PeriodicTask."""
    from auctions.models import AuctionHistory, Invoice

    try:
        invoice = Invoice.objects.get(pk=invoice_pk)
    except Invoice.DoesNotExist:
        # Invoice was deleted, clean up and return
        _cleanup_invoice_notification_task(invoice_pk)
        return

    # Check if notification is still needed
    if invoice.email_sent:
        # Already sent, clean up and return
        _cleanup_invoice_notification_task(invoice_pk)
        return

    if invoice.status == "DRAFT":
        # Invoice was set back to open, clean up and return
        _cleanup_invoice_notification_task(invoice_pk)
        return

    if not invoice.auction:
        # No auction associated, mark as sent to prevent reprocessing
        invoice.email_sent = True
        invoice.invoice_notification_due = None
        invoice.save()
        _cleanup_invoice_notification_task(invoice_pk)
        return

    should_send_email = (
        invoice.auction.created_by.userdata.is_trusted
        and invoice.auction.email_users_when_invoices_ready
        and invoice.auctiontos_user.email
    )

    if should_send_email:
        from auctions.email_routing import email_routing_enabled

        email = invoice.auctiontos_user.email
        subject = f"Your invoice for {invoice.label} is ready"
        if invoice.status == "PAID":
            subject = f"Thanks for being part of {invoice.label}"
        contact_email = invoice.auction.created_by.email
        current_site = Site.objects.get_current()
        # With SES routing, replies go to the routed sender address, so no Reply-To.
        send_kwargs = {
            "sender": invoice.auction.sender_email_with_name,
            "template": "invoice_ready",
            "context": {
                "subject": subject,
                "name": invoice.auctiontos_user.name,
                "domain": current_site.domain,
                "location": invoice.location,
                "invoice": invoice,
            },
        }
        if not email_routing_enabled():
            send_kwargs["headers"] = {"Reply-to": contact_email}
            send_kwargs["context"]["reply_to_email"] = contact_email

        # notify_user: opted-in app users get a push instead; bookkeeping below is the same.
        from auctions.notifications import notify_user

        push_user = invoice.auctiontos_user.user
        invoice_url = f"https://{current_site.domain}{invoice.get_absolute_url()}"
        pushed = notify_user(
            push_user,
            category="invoice",
            title=subject,
            body="Tap to view your invoice.",
            url=invoice_url,
            send_email=lambda: mail.send(email, **send_kwargs),
            auction_pk=invoice.auction.pk,
            invoice_pk=invoice.pk,
        )
        # Add history entry about the notification being sent
        channel = "push notification" if pushed else "email"
        AuctionHistory.objects.create(
            auction=invoice.auction,
            user=None,
            action=f"Invoice notification {channel} sent to {invoice.auctiontos_user.name} ({email})",
            applies_to="INVOICES",
        )

    # Marked sent either way, so an uncontactable invoice isn't reprocessed.
    invoice.email_sent = True
    invoice.invoice_notification_due = None
    invoice.save()

    # Clean up the PeriodicTask entry now that we're done
    _cleanup_invoice_notification_task(invoice_pk)


def _cleanup_invoice_notification_task(invoice_pk):
    """Remove the PeriodicTask for an invoice notification."""
    task_name = get_invoice_notification_task_name(invoice_pk)
    PeriodicTask.objects.filter(name=task_name).delete()


@shared_task(bind=True, ignore_result=True)
def cleanup_oauth_tokens(self):
    """Delete expired OAuth tokens and stale registered clients. Daily.

    Dynamic client registration is open by necessity, so ``clearcimdapplications`` removes expired CIMD
    clients holding no tokens. A no-op without ``oauth2_provider`` installed.
    """
    from django.apps import apps
    from django.core.management import call_command

    if not apps.is_installed("oauth2_provider"):
        return
    for command in ("cleartokens", "clearcimdapplications"):
        try:
            call_command(command)
        except Exception:
            # Neither failure stops the other or is worth an alert; it runs again tomorrow.
            logger.exception("OAuth cleanup command %s failed", command)


@shared_task(bind=True, ignore_result=True)
def cleanup_old_invoice_notification_tasks(self):
    """Daily safety net: delete invoice_notification_* tasks more than 24 hours old."""
    from datetime import timedelta

    from django.utils import timezone

    cutoff_time = timezone.now() - timedelta(hours=24)

    old_tasks = PeriodicTask.objects.filter(
        name__startswith="invoice_notification_",
        clocked__clocked_time__lt=cutoff_time,
    )
    old_tasks.delete()


@shared_task(bind=True, ignore_result=True)
def sync_discord_member_roles_for_club(self, club_pk):
    """Push Discord roles for every member of one club with a discord_id, after a role sync."""
    from auctions.models import ClubMember

    members = (
        ClubMember.objects.filter(
            club_id=club_pk,
            discord_id__isnull=False,
            is_deleted=False,
        )
        .select_related("club", "last_discord_role_assigned")
        .prefetch_related("club__discord_roles")
    )
    for member in members:
        member.maybe_assign_discord_role()


def _safely(label, do_it):
    """Run one step of a nightly job, logging and swallowing whatever it raises."""
    try:
        do_it()
    except Exception:
        logger.exception("Nightly step %s failed", label)


@shared_task(bind=True, ignore_result=True)
def update_expired_membership_discord_roles(self):
    """Re-evaluate Discord roles for members whose auto-managed role no longer matches. Daily.

    Kept its name when the other nightly jobs split out: a renamed beat entry orphans its PeriodicTask.
    """
    from auctions.models import ClubMember

    members = (
        ClubMember.objects.filter(
            discord_id__isnull=False,
            discord_role_auto_managed=True,
            is_deleted=False,
            club__discord_server_id__isnull=False,
        )
        .select_related("club", "last_discord_role_assigned")
        .prefetch_related("club__discord_roles")
    )
    for member in members:
        if member.discord_role != member.last_discord_role_assigned:
            _safely(f"discord role for member={member.pk}", member.maybe_assign_discord_role)


@shared_task(bind=True, ignore_result=True)
def reset_yearly_bap_counters(self):
    """Zero year-to-date BAP/HAP/CAP counters once per year. Daily; a no-op most days.

    Compares ``Club.bap_ytd_reset_year`` rather than checking for January 1, so a missed day catches up.
    **A null year is stamped, not zeroed**: ``exclude(bap_ytd_reset_year=year)`` matches NULL, which
    would wipe every pre-existing and brand-new club. Stale counters are for
    ``recalculate_club_bap_points``.
    """
    from django.utils import timezone

    from auctions.models import Club, ClubMember

    # localtime: award dates are DateFields in the admin's calendar.
    year = timezone.localtime().year
    clubs = Club.objects.filter(enable_breeder_award_program=True).exclude(bap_ytd_reset_year=year)
    for club in clubs:
        if club.bap_ytd_reset_year is None:
            Club.objects.filter(pk=club.pk).update(bap_ytd_reset_year=year)
            logger.info("Recorded %s as the award-points year for club %s; nothing zeroed", year, club.pk)
            continue
        ClubMember.objects.filter(club=club, is_deleted=False).update(
            bap_points_ytd=0, hap_points_ytd=0, culture_points_ytd=0
        )
        Club.objects.filter(pk=club.pk).update(bap_ytd_reset_year=year)
        logger.info("Reset year-to-date award points for club %s (%s)", club.pk, year)


@shared_task(bind=True, ignore_result=True)
def send_club_member_welcome_emails(self):
    """Welcome letters for members who joined more than 24 hours ago. Daily."""
    from django.utils import timezone

    from auctions.models import ClubMember

    members = ClubMember.objects.filter(
        is_deleted=False,
        welcome_email_sent=False,
        createdon__lte=timezone.now() - datetime.timedelta(hours=24),
    ).select_related("club")
    for member in members:
        _safely(f"welcome email for member={member.pk}", lambda member=member: _send_one_welcome(member))


def _send_one_welcome(member):
    from auctions.models import ClubHistory

    update_fields = ["welcome_email_sent"]
    member.welcome_email_sent = True
    if member.source == "csv":
        if member.send_welcome_email:
            member.send_welcome_email = False
            update_fields.append("send_welcome_email")
        member.save(update_fields=update_fields)
        return
    if member.send_welcome_email and member.club.send_welcome_email_to_new_members:
        sent = send_club_member_email(
            member,
            subject=f"Welcome to the {member.club.name}!",
            message_text="",
            email_type="welcome",
        )
        if sent:
            # System action, so no user on the history entry
            ClubHistory.objects.create(
                club=member.club,
                action=f"Sent welcome letter to {member} ({member.email})",
                applies_to="MEMBERS",
            )
    member.save(update_fields=update_fields)


@shared_task(bind=True, ignore_result=True)
def send_membership_expiration_reminders(self):
    """The 30-day and final membership expiration emails. Daily. Each member isolated."""
    from django.utils import timezone

    now = timezone.now()
    today = now.date()
    for due_field, subject, message, label in (
        (
            "membership_expiration_reminder_30_days_due",
            "Your {club} membership expires in 30 days",
            "Your {club} membership expires in 30 days.",
            "30-day expiration reminder",
        ),
        (
            "membership_expiration_reminder_due",
            "Your {club} membership expires tomorrow",
            "Your {club} membership expires tomorrow.",
            "final expiration reminder",
        ),
    ):
        _safely(
            label,
            lambda due_field=due_field, subject=subject, message=message, label=label: _run_reminder_pass(
                now, today, due_field, subject, message, label
            ),
        )


def _run_reminder_pass(now, today, due_field, subject, message, label):
    from auctions.models import ClubMember

    members = ClubMember.objects.filter(
        is_deleted=False,
        membership_last_paid__isnull=False,
        membership_expiration_date__isnull=False,
        membership_expiration_date__gte=today,
        # PayPal subscribers auto-renew; their due timestamp stays, so reminders resume if cancelled.
        paypal_subscription_id="",
        **{f"{due_field}__lte": now},
    ).select_related("club")
    for member in members:
        _safely(
            f"{label} for member={member.pk}",
            lambda member=member: _send_one_reminder(member, due_field, subject, message, label),
        )


def _send_one_reminder(member, due_field, subject, message, label):
    from auctions.models import ClubHistory

    reminders_on = getattr(
        member.club,
        "send_membership_expiration_reminders_30_days"
        if due_field == "membership_expiration_reminder_30_days_due"
        else "send_membership_expiration_reminders",
    )
    if reminders_on and member.club.membership_payment_emails_enabled:
        sent = send_club_member_email(
            member,
            subject=subject.format(club=member.club.name),
            message_text=message.format(club=member.club.name),
            email_type="expiring_soon",
        )
        if sent:
            ClubHistory.objects.create(
                club=member.club,
                action=f"Sent {label} to {member} ({member.email})",
                applies_to="MEMBERSHIP",
            )
    # Cleared whether or not it sent, so nothing is re-checked nightly forever.
    type(member).objects.filter(pk=member.pk).update(**{due_field: None})


@shared_task(bind=True, ignore_result=True)
def backfill_marketing_contacts(self):
    """Nightly Mailchimp/Brevo re-sync so lifecycle tags stay accurate. Enqueues one task per member."""
    from auctions import brevo
    from auctions import mailchimp as mc
    from auctions.models import Club

    connected_clubs = (
        Club.objects.filter(active=True).exclude(mailchimp_audience_id="").exclude(mailchimp_server_prefix="")
    )
    for club in connected_clubs:
        if club.mailchimp_connected:
            _safely(f"mailchimp backfill for club={club.pk}", lambda club=club: mc.backfill(club))

    brevo_clubs = Club.objects.filter(active=True).exclude(brevo_list_id="")
    for club in brevo_clubs:
        if club.brevo_connected:
            _safely(f"brevo backfill for club={club.pk}", lambda club=club: brevo.backfill(club))


@shared_task(bind=True, ignore_result=True)
def sync_club_calendars(self):
    """Keep every club's events, Google Calendar and Discord scheduled events in step.

    Each club isolated. One run at a time: two would push events twice or provision two calendars.
    """
    from django.core.cache import cache

    from auctions import club_events

    if not cache.add(CALENDAR_SYNC_LOCK_KEY, "1", timeout=CALENDAR_SYNC_LOCK_SECONDS):
        logger.info("Club calendar sync is already running; skipping this run.")
        return
    try:
        count = club_events.sync_all()
        logger.info("Synced calendars for %s club(s)", count)
    finally:
        cache.delete(CALENDAR_SYNC_LOCK_KEY)


@shared_task(bind=True, ignore_result=True)
def auction_emails(self):
    """Send auction-related drip marketing emails."""
    call_command("auction_emails")


@shared_task(bind=True, ignore_result=True)
def email_unseen_chats(self):
    """Send notifications about unread chat messages."""
    call_command("email_unseen_chats")


@shared_task(bind=True, ignore_result=True)
def weekly_promo(self):
    """Send the weekly promotional email for nearby auctions and lots."""
    call_command("weekly_promo")


@shared_task(bind=True, ignore_result=True)
def promo_push_notifications(self):
    """Push nearby auction promotions to opted-in app users; each auction to each user at most once. Hourly."""
    call_command("promo_push_notifications")


@shared_task(bind=True, ignore_result=True)
def update_ar_positions(self):
    """Fuse AR lot sightings for flagged auctions; prune observations older than 24 hours. Every minute."""
    call_command("update_ar_positions")


@shared_task(bind=True, ignore_result=True)
def set_user_location(self):
    """Set user lat/long from IP address."""
    call_command("set_user_location")


@shared_task(bind=True, ignore_result=True)
def webpush_notifications_deduplicate(self):
    """Deduplicate web push subscriptions."""
    call_command("webpush_notifications_deduplicate")


@shared_task(bind=True, ignore_result=True)
def deduplicate_user_interest(self):
    """Merge duplicate UserInterestCategory rows (no unique constraint; request races create them)."""
    call_command("deduplicate_user_interest")


@shared_task(bind=True, ignore_result=True)
def migrate_to_cloudflare_images(self):
    """Move pending local original images to Cloudflare Images. Every minute; a no-op unless configured."""
    try:
        call_command("migrate_to_cloudflare_images")
    except SoftTimeLimitExceeded:
        logger.info("migrate_to_cloudflare_images hit the task time limit; the next run will resume")


@shared_task(bind=True, ignore_result=True)
def delete_cloudflare_image(self, image_id):
    """Delete a Cloudflare image unless a row still references it (relisted lots share images)."""
    from auctions import cloudflare_images
    from auctions.models import AdCampaign, Club, LotImage, Speaker

    if not cloudflare_images.enabled():
        return
    for model in (LotImage, Club, AdCampaign, Speaker):
        if model.objects.filter(cloudflare_image_id=image_id).exists():
            return
    try:
        cloudflare_images.delete(image_id)
    except cloudflare_images.CloudflareImagesError:
        logger.exception("Could not delete Cloudflare image %s", image_id)


@shared_task(bind=True, ignore_result=True)
def purge_edge_cache(self, urls):
    """Drop these URLs from the edge cache after the file behind them was deleted.

    Why a deletion has to, and why nothing here raises, is in :mod:`auctions.cloudflare_cache`.
    """
    from auctions import cloudflare_cache

    cloudflare_cache.purge_urls(urls)


def schedule_auction_stats_update(run_at=None):
    """Schedule the one-off stats update at ``run_at`` (now if None): delete and recreate atomically, so
    exactly one enabled task exists.
    """
    from datetime import timedelta

    from django.db import transaction
    from django.utils import timezone

    if run_at is None:
        run_at = timezone.now()

    # Cap the delay to ensure we check periodically for new auctions
    max_run_at = timezone.now() + timedelta(seconds=STATS_UPDATE_MAX_DELAY_SECONDS)
    if run_at > max_run_at:
        run_at = max_run_at

    with transaction.atomic():
        # Create or get the schedule for this run time
        schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)

        # Recreate rather than update: beat disables one-off tasks after they run.
        old_tasks = PeriodicTask.objects.filter(name=AUCTION_STATS_TASK_NAME)
        old_schedule_ids = [task.clocked_id for task in old_tasks if task.clocked_id]
        old_tasks.delete()

        # Remove orphaned schedules, never the one just fetched.
        if old_schedule_ids:
            ClockedSchedule.objects.filter(id__in=old_schedule_ids).exclude(id=schedule.id).delete()

        task = PeriodicTask.objects.create(
            name=AUCTION_STATS_TASK_NAME,
            task="auctions.tasks.update_auction_stats",
            clocked=schedule,
            one_off=True,
            enabled=True,
        )

    logger.info(
        "Scheduled auction stats update task (id=%s) to run at %s", task.id, run_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    )


#: How stale the self-scheduling stats task may look before the watchdog re-arms it.
STATS_WATCHDOG_GRACE_SECONDS = 15 * 60


@shared_task(bind=True, ignore_result=True)
def ensure_auction_stats_task_scheduled(self):
    """Re-arm the self-scheduling stats chain if it has stopped. One indexed lookup on the beat.

    A hard-limit SIGKILL skips the self-reschedule, and beat has already disabled the row, so stats
    silently stopped. Re-armed when the row is missing or overdue by the grace period. ``enabled`` is
    ignored: beat clears it on dispatch, so a disabled row with a recent ``clocked_time`` is a run in
    flight, and re-arming it would start a second.
    """
    from django.utils import timezone

    task = PeriodicTask.objects.filter(name=AUCTION_STATS_TASK_NAME).select_related("clocked").first()
    if task and task.clocked:
        overdue_since = timezone.now() - datetime.timedelta(seconds=STATS_WATCHDOG_GRACE_SECONDS)
        if task.clocked.clocked_time > overdue_since:
            return
    logger.warning("The auction stats task was missing or overdue; re-arming it.")
    schedule_auction_stats_update()


@shared_task(bind=True, ignore_result=True)
def update_auction_stats(self):
    """Update stats for the most overdue auction, then schedule the next run for when the next is due."""
    from datetime import timedelta

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.utils import timezone

    from auctions.models import Auction

    now = timezone.now()

    logger.info("Auction stats update task started at %s", now.strftime("%Y-%m-%d %H:%M:%S %Z"))

    auction = (
        Auction.objects.filter(
            next_update_due__lte=now,
            is_deleted=False,
        )
        .order_by("next_update_due")
        .first()
    )

    if auction:
        logger.info("Found auction needing stats update: %s (id=%s)", auction.title, auction.pk)
        try:
            logger.info("Recalculating stats for auction: %s (%s)", auction.title, auction.slug)

            # Pushed out first so a concurrent run skips this auction.
            auction.next_update_due = now + timedelta(minutes=STATS_UPDATE_LOCK_MINUTES)
            auction.save(update_fields=["next_update_due"])

            auction.recalculate_stats()

            # Best-effort websocket notice to anyone on the stats page.
            try:
                logger.info("Sending WebSocket notification for auction: %s", auction.title)
                auction_websocket = get_channel_layer()
                async_to_sync(auction_websocket.group_send)(
                    f"auctions_{auction.pk}",
                    {
                        "type": "stats_updated",
                    },
                )
                logger.info("Successfully sent WebSocket notification for auction: %s", auction.title)
            except Exception as websocket_error:
                # Log the error but don't fail the stats update
                logger.error("Failed to send WebSocket notification for auction %s: %s", auction.title, websocket_error)

            logger.info("Successfully updated stats for auction: %s", auction.title)
        except Exception as e:
            logger.error("Failed to update stats for auction %s (%s): %s", auction.title, auction.slug, e)
            logger.exception(e)
            try:
                auction.create_history("STATS", f"Stats update failed: {e}")
            except Exception:
                logger.exception("Failed to record stats failure history for auction %s", auction.pk)
            # Push it a day out so it doesn't block the queue.
            try:
                auction.next_update_due = now + timedelta(days=1)
                auction.save(update_fields=["next_update_due"])
            except Exception:
                logger.exception("Failed to reschedule stats update for auction %s", auction.pk)
    else:
        logger.info("No auctions need stats update at this time")

    # Schedule the next run based on when the next auction update is due
    next_auction = (
        Auction.objects.filter(is_deleted=False, next_update_due__isnull=False).order_by("next_update_due").first()
    )

    if next_auction and next_auction.next_update_due:
        logger.info(
            "Scheduling next stats update for auction '%s' at %s",
            next_auction.title,
            next_auction.next_update_due.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        schedule_auction_stats_update(next_auction.next_update_due)
    else:
        # No auctions with scheduled updates, check again later
        fallback_time = now + timedelta(seconds=STATS_UPDATE_FALLBACK_DELAY_SECONDS)
        logger.info(
            "No auctions need stats update, checking again at %s", fallback_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        schedule_auction_stats_update(fallback_time)


def schedule_bap_recalculation(club_pk, run_at):
    """Schedule a one-off BAP recalculation for a club. An existing task with the same time is re-enabled;
    otherwise it is replaced, keeping any ClockedSchedule another club still uses.
    """
    from django.db import transaction

    task_name = f"{BAP_RECALCULATION_TASK_PREFIX}{club_pk}"

    with transaction.atomic():
        old_task = PeriodicTask.objects.filter(name=task_name).select_related("clocked").first()
        if old_task:
            old_schedule = old_task.clocked
            if old_schedule and old_schedule.clocked_time == run_at:
                old_task.enabled = True
                old_task.save(update_fields=["enabled"])
                return
            is_shared = old_schedule and PeriodicTask.objects.filter(clocked=old_schedule).count() > 1
            old_task.delete()
            if old_schedule and not is_shared:
                ClockedSchedule.objects.filter(pk=old_schedule.pk).delete()

        schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)
        PeriodicTask.objects.create(
            name=task_name,
            task="auctions.tasks.recalculate_club_bap_points",
            clocked=schedule,
            one_off=True,
            enabled=True,
            kwargs=json.dumps({"club_pk": club_pk}),
        )


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def create_google_wallet_class_for_club(self, club_pk):
    """Create a club's Google Wallet GenericClass (409 = exists), then set ``google_wallet_class_created``."""
    from auctions.google_wallet import create_generic_class, is_configured
    from auctions.models import Club

    if not is_configured():
        return
    club = Club.objects.filter(pk=club_pk).first()
    if not club:
        return
    if create_generic_class(club) and not club.google_wallet_class_created:
        # update() avoids re-firing the signal we're inside.
        Club.objects.filter(pk=club.pk).update(google_wallet_class_created=True)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def update_google_wallet_objects_for_club(self, club_pk):
    """Patch existing Google Wallet objects for all active members in a club."""
    from auctions.google_wallet import is_configured, update_generic_object_for_member
    from auctions.models import Club, ClubMember

    if not is_configured():
        return
    club = Club.objects.filter(pk=club_pk).first()
    if not club:
        return
    members = ClubMember.objects.filter(club=club, is_deleted=False).select_related("user", "club")
    _per_item(self, f"Google Wallet object refresh for club={club.pk}", members, update_generic_object_for_member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def update_google_wallet_object_for_member(self, member_pk):
    """Patch one member's Google Wallet object after wallet-visible fields change. A 404 (never added) is fine."""
    from auctions.google_wallet import is_configured, update_generic_object_for_member
    from auctions.models import ClubMember

    if not is_configured():
        return
    member = ClubMember.objects.filter(pk=member_pk, is_deleted=False).select_related("user", "club").first()
    if not member:
        return
    try:
        update_generic_object_for_member(member)
    except requests.RequestException:
        logger.exception("Google Wallet object refresh failed for member=%s", member_pk)
        raise


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def sync_club_member_to_mailchimp(self, member_pk):
    """Push one member into the club's Mailchimp audience. Deleted/opted-out members are archived by
    ``sync_member``, so they aren't filtered here.
    """
    from auctions import mailchimp as mc
    from auctions.models import ClubMember

    member = ClubMember.objects.select_related("club", "user").filter(pk=member_pk).first()
    if not member or not member.club.mailchimp_connected:
        return
    mc.sync_member(member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def sync_club_member_email_change(self, member_pk, old_email):
    """Move a member's Mailchimp contact to a new email address, then refresh their data."""
    from auctions import mailchimp as mc
    from auctions.models import ClubMember

    member = ClubMember.objects.select_related("club", "user").filter(pk=member_pk).first()
    if not member or not member.club.mailchimp_connected:
        return
    mc.change_member_email(member, old_email)
    mc.sync_member(member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def sync_club_member_to_brevo(self, member_pk):
    """Push one member into the club's Brevo list. Same contract as ``sync_club_member_to_mailchimp``."""
    from auctions import brevo
    from auctions.models import ClubMember

    member = ClubMember.objects.select_related("club", "user").filter(pk=member_pk).first()
    if not member or not member.club.brevo_connected:
        return
    brevo.sync_member(member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def sync_club_member_email_change_brevo(self, member_pk, old_email):
    """Move a member's Brevo contact to a new email address, then refresh their data."""
    from auctions import brevo
    from auctions.models import ClubMember

    member = ClubMember.objects.select_related("club", "user").filter(pk=member_pk).first()
    if not member or not member.club.brevo_connected:
        return
    brevo.change_member_email(member, old_email)
    brevo.sync_member(member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def expire_google_wallet_objects_for_club(self, club_pk, unpaid_only=False):
    """Expire every active Wallet pass for a club's members, or only lapsed ones with ``unpaid_only``."""
    from auctions.google_wallet import expire_generic_object_for_member, is_configured
    from auctions.models import Club, ClubMember

    if not is_configured():
        return
    club = Club.objects.filter(pk=club_pk).first()
    if not club:
        return
    members = [
        member
        for member in ClubMember.objects.filter(club=club, is_deleted=False)
        if not (unpaid_only and member.is_paid_member)
    ]
    _per_item(self, f"Google Wallet expiry for club={club.pk}", members, expire_generic_object_for_member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def refresh_google_wallet_membership_status(self):
    """Daily: refresh Google Wallet passes for members who lapsed in the last few days, since no signal
    fires when time passes. Apple's is ``refresh_apple_wallet_membership_status``.
    """
    from auctions.google_wallet import is_configured, update_generic_object_for_member
    from auctions.models import ClubMember

    if not is_configured():
        return
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    # A three-day window gives slack for a missed run.
    window_start = today - datetime.timedelta(days=3)
    members = ClubMember.objects.filter(
        is_deleted=False,
        club__google_wallet_class_created=True,
        club__membership_system__in=["january_first", "rolling"],
        membership_expiration_date__gte=window_start,
        membership_expiration_date__lt=today,
    ).select_related("user", "club")
    _per_item(self, "Google Wallet daily status refresh", members, update_generic_object_for_member)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def notify_apple_wallet_devices_for_member(self, member_pk):
    """Bump a member's Apple pass version and notify registered devices via APNs.

    The bump happens even with no devices, so a manual refresh sees it. Deleted members aren't skipped:
    their update is the voided pass.
    """
    from django.utils import timezone

    from auctions.apple_wallet import is_configured, send_pass_update_notification
    from auctions.models import AppleDeviceRegistration, ClubMember

    if not is_configured():
        return
    if not ClubMember.objects.filter(pk=member_pk).update(apple_pass_updated=timezone.now()):
        return
    _per_item(
        self,
        f"Apple Wallet push for member={member_pk}",
        AppleDeviceRegistration.objects.filter(member_id=member_pk),
        send_pass_update_notification,
        exceptions=(httpx.HTTPError,),
    )


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def notify_apple_wallet_devices_for_club(self, club_pk):
    """Bump and notify every member's Apple pass, after a club-wide change (name, icon, barcode toggle)."""
    from django.utils import timezone

    from auctions.apple_wallet import is_configured, send_pass_update_notification
    from auctions.models import AppleDeviceRegistration, ClubMember

    if not is_configured():
        return
    ClubMember.objects.filter(club_id=club_pk).update(apple_pass_updated=timezone.now())
    registrations = AppleDeviceRegistration.objects.filter(member__club_id=club_pk)
    _per_item(
        self,
        f"Apple Wallet club push for club={club_pk}",
        registrations,
        send_pass_update_notification,
        exceptions=(httpx.HTTPError,),
    )


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def refresh_apple_wallet_membership_status(self):
    """Daily: push Apple pass updates to members who lapsed in the last few days and have a device."""
    from django.utils import timezone

    from auctions.apple_wallet import is_configured, send_pass_update_notification
    from auctions.models import AppleDeviceRegistration, ClubMember

    if not is_configured():
        return
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    window_start = today - datetime.timedelta(days=3)
    members = ClubMember.objects.filter(
        is_deleted=False,
        club__membership_system__in=["january_first", "rolling"],
        membership_expiration_date__gte=window_start,
        membership_expiration_date__lt=today,
        apple_device_registrations__isnull=False,
    ).distinct()
    member_pks = list(members.values_list("pk", flat=True))
    if not member_pks:
        return
    ClubMember.objects.filter(pk__in=member_pks).update(apple_pass_updated=timezone.now())
    # One query for all registrations; one failing device can't block the rest (see _per_item).
    registrations = AppleDeviceRegistration.objects.filter(member_id__in=member_pks)
    _per_item(
        self,
        "Apple Wallet daily status refresh",
        registrations,
        send_pass_update_notification,
        exceptions=(httpx.HTTPError,),
    )


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def geocode_club_member(self, pk):
    """Geocode a ClubMember's address. With no address, copy coordinates from a linked user who joined an
    auction themselves. Needs GOOGLE_MAPS_SERVER_API_KEY.
    """
    from auctions.models import AuctionTOS, ClubMember, UserData

    if not geocoding.configured():
        return

    member = ClubMember.objects.filter(pk=pk).first()
    if not member:
        return

    if member.address:
        found = geocoding.geocode(member.address)
        if found:
            ClubMember.objects.filter(pk=pk).update(lat=found["latitude"], lng=found["longitude"])
    elif member.user_id and not (member.lat and member.lng):
        has_self_joined = AuctionTOS.objects.filter(user=member.user, manually_added=False).exists()
        if has_self_joined:
            ud = UserData.objects.filter(user=member.user).values("latitude", "longitude").first()
            if ud and ud["latitude"] and ud["longitude"]:
                ClubMember.objects.filter(pk=pk).update(lat=ud["latitude"], lng=ud["longitude"])


@shared_task(bind=True, ignore_result=True)
def recalculate_club_bap_points(self, club_pk):
    """Recalculate BAP/HAP/CAP point totals for all active members of a club."""
    from auctions.models import BapAward, Club, ClubMember

    club = Club.objects.filter(pk=club_pk).first()
    if not club:
        return
    for member in ClubMember.objects.filter(club=club, is_deleted=False):
        BapAward.recalculate_member_points(member)


def bootstrap_bap_recalculation_tasks(run_at):
    """Schedule BAP recalculations for eligible clubs at worker startup; overdue ones at ``run_at``."""
    from auctions.models import Club

    clubs = Club.objects.filter(
        enable_breeder_award_program=True,
        next_bap_recalculation__isnull=False,
    )
    for club in clubs:
        if club.next_bap_recalculation <= run_at:
            schedule_bap_recalculation(club.pk, run_at=run_at)
        else:
            schedule_bap_recalculation(club.pk, run_at=club.next_bap_recalculation)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def geocode_speaker(self, pk):
    """Geocode a Speaker's free-text location. Hand-placed map coordinates are already set, so this only
    fills gaps. Needs GOOGLE_MAPS_SERVER_API_KEY.
    """
    from auctions.models import Speaker

    if not geocoding.configured():
        return

    speaker = Speaker.objects.filter(pk=pk).first()
    if not speaker or not speaker.location:
        return

    found = geocoding.geocode(speaker.location)
    if found:
        Speaker.objects.filter(pk=pk).update(latitude=found["latitude"], longitude=found["longitude"])
