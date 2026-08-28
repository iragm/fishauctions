"""
Celery tasks for the auctions app.

This module contains all Celery tasks that were previously run as cron jobs.
Each task wraps a management command to maintain backward compatibility.
"""

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

logger = logging.getLogger(__name__)


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


# Inline style for the wallet buttons in membership emails.  Email clients strip <style> blocks
# and know nothing about Bootstrap, so this hand-rolls what btn-dark looks like on the web
# membership card (see partials/club_member_uuid_card.html).
_WALLET_BUTTON_STYLE = (
    "display:inline-block;padding:10px 16px;margin:0 8px 8px 0;background:#303030;color:#ffffff;"
    "text-decoration:none;border-radius:6px;font-family:sans-serif;font-size:14px;"
)


def wallet_links(member, current_site=None):
    """Return (google_url, apple_url) for adding this member's card to a phone wallet.

    Either is "" when that wallet isn't configured on this site, or when the club has member
    barcodes turned off (there is no card to add).  Both links are UUID-keyed capability URLs,
    so they work from an email without the recipient being logged in.
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

    Covers auctions and anything else on the club's calendar — meetings, swaps, talks. Shared by
    the real emails and the settings-page preview so the two can't drift; the preview passes
    ``as_links=False`` because a preview shouldn't contain working links.

    It ends with a subscribe link, because one event in an email is a club's whole calendar in
    miniature and the member is never going to be sent this email again — a welcome goes out once.
    It rides on this fragment rather than sitting on its own so that a club which turned the next
    event off (``welcome_include_auction`` and friends) gets no calendar pitch either: that switch
    means "don't advertise what we're doing next", and a subscribe link is exactly that.
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
        # An in-person auction gathers at its pickup location's time, which is the time members
        # actually need; the auction's own date_start is only "when bidding opens".
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

    # The club's Google calendar when they've shared it, our own feed when they haven't — the same
    # choice the club page's buttons make, for the same reason: a club that keeps its calendar in
    # Google keeps things there we only see after the next pull.
    subscribe_url = club.calendar_subscribe_url(current_site.domain)
    text += f" Add our calendar: {subscribe_url}"
    html += (
        f" <a href='{escape(subscribe_url)}'>Add our calendar</a>."
        if as_links
        else " <span class='text-info'>Add our calendar</span>."
    )
    return text, html


def _real_physical_locations(auction):
    """The auction's physical locations, ignoring placeholders.

    Switching an auction to in-person auto-creates a location with no address or coordinates.
    It isn't somewhere anyone can go, so it must not count when deciding whether an auction has
    a single location.
    """
    return [
        location
        for location in auction.physical_location_qs
        if (location.address or "").strip() or location.has_coordinates
    ]


def _in_person_auction_time(auction):
    """When an in-person auction actually gathers, or None.

    Only meaningful with a single location — with several there's no one time to advertise.
    """
    locations = [location for location in _real_physical_locations(auction) if location.pickup_time]
    if len(locations) == 1:
        return locations[0].pickup_time
    return None


def _event_directions_url(event):
    """A map link for the event, falling back to the auction's single location.

    Online auction events carry no location of their own — the addresses live on the pickup
    events — but a member reading "our next event is the Spring Auction" still wants directions.
    Only ever offered when the auction has exactly one real location: with several, a single
    "Get directions" link would send people to the wrong one.
    """
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
    """Send one of the club's membership emails.

    ``force_email`` skips the push-notification path in notify_user; use it when an admin has
    explicitly asked for an email to go out (the resend-membership-card action), where silently
    turning it into a push would not be what they confirmed.
    """
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

    # A member may or may not be a linked site user; notify_user pushes only when that user opted in
    # (and has a live device), otherwise it emails — so guest members always get the email.
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
    """Email a member a link to their membership card (admin-triggered resend).

    Returns True when the email was queued, False when the member can't be emailed.
    """
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
        # PayPal-subscription renewal: tell them it's automatic and where to manage/cancel it.
        # paypal.com/myaccount/autopay is PayPal's "Automatic payments" page for subscribers.
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
        # Logged here rather than at the four call sites: each one already records the renewal
        # itself, and this says whether the member was actually told about it.
        from auctions.models import ClubHistory

        ClubHistory.objects.create(
            club=member.club,
            action=f"Sent renewal confirmation to {member} ({member.email})",
            applies_to="MEMBERSHIP",
        )
    return sent


@shared_task(bind=True, ignore_result=True)
def endauctions(self):
    """
    Set the winner and winning price on all ended lots.
    Send lot ending soon and lot ended messages to websocket connected users.
    Sets active to false on lots.

    Previously run every minute via cron.
    """
    call_command("endauctions")


@shared_task(bind=True, ignore_result=True)
def sendnotifications(self):
    """
    Send notifications about watched items.

    Previously run every 15 minutes via cron.
    """
    call_command("sendnotifications")


@shared_task(bind=True, ignore_result=True)
def auctiontos_notifications(self):
    """
    Welcome and print reminder emails.

    Previously run every 15 minutes via cron.
    """
    call_command("auctiontos_notifications")


@shared_task(bind=True, ignore_result=True)
def flush_expired_tokens(self):
    """
    Delete expired JWT blacklist / outstanding-token rows.

    Mobile JWT refresh-token rotation (ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION) writes a row
    per login and per refresh; without periodic cleanup the token_blacklist tables grow unbounded.
    """
    call_command("flushexpiredtokens")


@shared_task(bind=True, ignore_result=True)
def delete_pending_accounts(self):
    """
    Delete accounts whose deletion grace period has expired.

    Daily. Nothing happens on the day someone asks (see auctions.account_deletion); this is the task
    that makes it real, so it must keep running for the site to keep its promise.
    """
    call_command("delete_pending_accounts")


@shared_task(bind=True, ignore_result=True, retry_backoff=True, retry_backoff_max=600, max_retries=5)
def delete_marketing_contact(self, club_pk, email):
    """Remove one address from a club's Mailchimp audience and Brevo list.

    Account deletion only: an ordinary unsubscribe archives the contact (which still holds the
    address), so it can't be reused here. Takes the address rather than a member pk because by the
    time this runs the member row has been emptied or unlinked.

    Each provider gets its own attempt and the retry is driven by hand rather than by
    ``autoretry_for``. Both of them raise their own exception type for an API error (mailchimp's
    ApiClientError, brevo's BrevoApiError) and neither descends from requests.RequestException, so a
    provider-class list is the thing most likely to quietly stop matching. Whatever goes wrong, the
    other provider still gets called and the whole task is retried — deleting an already-deleted
    contact is a 404 both helpers swallow.
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
    """
    Delete sent mail (and its attachments) older than MAIL_RETENTION_DAYS.

    post_office keeps every message it has ever sent, body and recipient address included, which
    makes it the one place a deleted user's address would otherwise survive their deletion — and an
    ever-growing table nobody reads. Recent mail is worth keeping: it's how a bounce, a missing
    invoice or a "did the site email me?" question gets answered.
    """
    call_command("cleanup_mail", days=settings.MAIL_RETENTION_DAYS, delete_attachments=True)


@shared_task(bind=True, ignore_result=True)
def send_announcement_emails(self, announcement_pk):
    """Mail one club announcement through whichever of Mailchimp/Brevo the club ticked.

    Out of the request because it is four round trips to somebody else's API per provider. No
    retry: a campaign that half-created and then failed would be sent twice by a retry, and the
    failure is written onto the announcement where the admin can see it and press send again.
    """
    from auctions import announcements
    from auctions.models import ClubAnnouncement

    announcement = ClubAnnouncement.objects.filter(pk=announcement_pk, is_deleted=False).select_related("club").first()
    if not announcement:
        return
    announcements.send_emails(announcement)


@shared_task(bind=True, ignore_result=True)
def send_scheduled_announcements(self):
    """Deliver club announcements whose send time has arrived.

    Queued twice over: once by the view with a ``countdown`` for the exact moment (which is what
    makes a 30-second retract window 30 seconds), and once a minute by the beat as a backstop. It
    is safe to run either way round or twice at once -- ``send_due`` claims each row with the same
    UPDATE that marks it sent.
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


@shared_task
def send_push_to_user(user_pk, *, title, body, url, category, collapse_key=None, auction_pk=None, invoice_pk=None):
    """Send a push notification to every push-enabled device of a user; prune dead tokens.

    FCM data-message keys: title, body, url (absolute), category. On an unregistered / invalid
    token the offending device's token is cleared. One ``PushNotificationSent`` row is logged per
    successful device send (dedupe + stats). ``collapse_key`` folds chatty categories so a phone
    that was off shows one notification, not many.

    If nothing reaches a device, the notification is emailed instead rather than dropped. That
    matters most the moment someone uninstalls the app: the send that *discovers* the dead token is
    the one that would otherwise vanish, and by then the caller has already recorded the
    notification as delivered (invoice.email_sent, tos.confirm_email_sent, ...) so nothing would
    ever retry it. Categories in PUSH_ONLY_CATEGORIES have no email form and are dropped instead.
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
            # Token follows the app install; a dead one never comes back, so clear it.
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
    """Last-resort email for a push that reached no device (dead token, or FCM was down).

    Plain text rather than the caller's original template -- by the time we get here the caller is
    long gone and only the notification's own title/body/url survive. An unstyled email that arrives
    beats a silent drop.
    """
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
    """
    Schedule a one-off task to send an invoice notification.

    Uses django-celery-beat's ClockedSchedule and PeriodicTask to schedule
    a task to run at a specific time. If a task already exists for this
    invoice, it will be updated with the new scheduled time.

    Args:
        invoice_pk: The primary key of the invoice
        run_at: datetime when the notification should be sent
    """
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
    """
    Cancel a scheduled invoice notification task.

    Args:
        invoice_pk: The primary key of the invoice
    """
    task_name = get_invoice_notification_task_name(invoice_pk)
    PeriodicTask.objects.filter(name=task_name).delete()


@shared_task(bind=True, ignore_result=True)
def send_invoice_notification(self, invoice_pk):
    """
    Send an invoice notification for a specific invoice.

    This task is scheduled as a one-off task when an invoice status changes
    to "ready" or "paid". It will:
    - Check if the invoice still needs notification (not already sent, not draft)
    - Send email if conditions are met (trusted user, has email, notifications enabled)
    - Mark the invoice as email_sent=True
    - Add history entry if email was sent
    - Clean up the PeriodicTask entry after execution

    The task is idempotent - if called multiple times or after the notification
    is already sent, it will simply do nothing.
    """
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
        # When SES routing is active, replies go to the auction sender address
        # automatically (Lambda routes them). Skip the Reply-To header so users
        # reply to the routed address rather than the creator's personal inbox.
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

        # Route through the notify_user choke point: an app user who opted into push gets a
        # notification instead of the email; everyone else is emailed exactly as before. The
        # bookkeeping below (email_sent, AuctionHistory) is identical on both paths.
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

    # Mark as sent regardless of whether we actually sent an email
    # This prevents re-processing invoices that can't receive emails
    invoice.email_sent = True
    invoice.invoice_notification_due = None
    invoice.save()

    # Clean up the PeriodicTask entry now that we're done
    _cleanup_invoice_notification_task(invoice_pk)


def _cleanup_invoice_notification_task(invoice_pk):
    """
    Remove the PeriodicTask entry for an invoice notification.

    This is called after the task runs to clean up the database.
    """
    task_name = get_invoice_notification_task_name(invoice_pk)
    PeriodicTask.objects.filter(name=task_name).delete()


@shared_task(bind=True, ignore_result=True)
def cleanup_oauth_tokens(self):
    """Delete expired OAuth tokens and stale registered clients. Daily.

    Two things grow on their own here, and one of them grows because of a deliberate decision:

    * **Expired access and refresh tokens.** Ordinary accumulation — an access token lives an hour
      and Claude refreshes on a 401, so an active connection leaves a row behind several times a
      day. ``cleartokens`` removes the ones past their expiry, which are already useless.
    * **Registered clients.** Dynamic client registration is deliberately open (it has to be: DCR
      is the first call a client makes, before anyone has signed in — see the OAUTH2_PROVIDER block
      in settings.py), so anybody can POST to /o/register/ and create an Application row. CIMD is
      advertised precisely so Claude does not need to, but the fallback is reachable.
      ``clearcimdapplications`` removes CIMD entries whose cached metadata has expired and that
      nothing holds a token for.

    A no-op on an install that isn't an authorization server: without ``oauth2_provider`` in
    INSTALLED_APPS the commands don't exist, and this returns rather than raising every night.
    """
    from django.apps import apps
    from django.core.management import call_command

    if not apps.is_installed("oauth2_provider"):
        return
    for command in ("cleartokens", "clearcimdapplications"):
        try:
            call_command(command)
        except Exception:
            # One of the two failing must not stop the other, and neither is worth waking anyone
            # for: the next run is in 24 hours and nothing is broken in the meantime.
            logger.exception("OAuth cleanup command %s failed", command)


@shared_task(bind=True, ignore_result=True)
def cleanup_old_invoice_notification_tasks(self):
    """
    Clean up old invoice notification PeriodicTask entries from the database.

    This task runs daily to remove any invoice_notification_* tasks that are
    more than 24 hours old. These tasks should normally be cleaned up after
    execution, but this provides a safety net for any orphaned entries.
    """
    from datetime import timedelta

    from django.utils import timezone

    cutoff_time = timezone.now() - timedelta(hours=24)

    # Find and delete old invoice notification tasks
    # The clocked schedule's clocked_time indicates when the task was scheduled to run
    old_tasks = PeriodicTask.objects.filter(
        name__startswith="invoice_notification_",
        clocked__clocked_time__lt=cutoff_time,
    )
    old_tasks.delete()


@shared_task(bind=True, ignore_result=True)
def sync_discord_member_roles_for_club(self, club_pk):
    """Re-evaluate and push Discord roles for every member of a single club who has a discord_id.

    Called after a role sync so that newly configured roles are immediately reflected.
    """
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


@shared_task(bind=True, ignore_result=True)
def update_expired_membership_discord_roles(self):
    """
    Re-evaluate and update Discord roles for all members whose auto-managed role
    no longer matches what was last assigned (e.g. after membership expiration or renewal).

    Also sends membership expiration reminder emails.

    Runs daily. Only members whose computed role differs from last_discord_role_assigned
    will trigger Discord API calls.
    """
    from django.utils import timezone

    from auctions.models import ClubHistory, ClubMember

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
            member.maybe_assign_discord_role()

    # Zero out YTD BAP/HAP/CAP counters at the start of each new year
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    if today.month == 1 and today.day == 1:
        ClubMember.objects.filter(
            is_deleted=False,
            club__enable_breeder_award_program=True,
        ).update(bap_points_ytd=0, hap_points_ytd=0, culture_points_ytd=0)

    now = timezone.now()
    today = now.date()

    welcome_qs = ClubMember.objects.filter(
        is_deleted=False,
        welcome_email_sent=False,
        createdon__lte=now - datetime.timedelta(hours=24),
    ).select_related("club")
    for member in welcome_qs:
        update_fields = ["welcome_email_sent"]
        member.welcome_email_sent = True
        if member.source == "csv":
            if member.send_welcome_email:
                member.send_welcome_email = False
                update_fields.append("send_welcome_email")
            member.save(update_fields=update_fields)
            continue
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

    reminder_30_days_qs = ClubMember.objects.filter(
        is_deleted=False,
        membership_last_paid__isnull=False,
        membership_expiration_date__isnull=False,
        membership_expiration_date__gte=today,
        membership_expiration_reminder_30_days_due__lte=now,
        # PayPal-subscription members auto-renew, so don't nag them to renew (their due timestamp is
        # left intact, so reminders resume if the subscription is later cancelled).
        paypal_subscription_id="",
    ).select_related("club")
    for member in reminder_30_days_qs:
        if member.club.send_membership_expiration_reminders_30_days and member.club.membership_payment_emails_enabled:
            sent = send_club_member_email(
                member,
                subject=f"Your {member.club.name} membership expires in 30 days",
                message_text=f"Your {member.club.name} membership expires in 30 days.",
                email_type="expiring_soon",
            )
            if sent:
                ClubHistory.objects.create(
                    club=member.club,
                    action=f"Sent 30-day expiration reminder to {member} ({member.email})",
                    applies_to="MEMBERSHIP",
                )
        member.membership_expiration_reminder_30_days_due = None
        member.save(update_fields=["membership_expiration_reminder_30_days_due"])

    reminder_qs = ClubMember.objects.filter(
        is_deleted=False,
        membership_last_paid__isnull=False,
        membership_expiration_date__isnull=False,
        membership_expiration_date__gte=today,
        membership_expiration_reminder_due__lte=now,
        # PayPal-subscription members auto-renew; skip the nag (see the 30-day query above).
        paypal_subscription_id="",
    ).select_related("club")
    for member in reminder_qs:
        if member.club.send_membership_expiration_reminders and member.club.membership_payment_emails_enabled:
            sent = send_club_member_email(
                member,
                subject=f"Your {member.club.name} membership expires tomorrow",
                message_text=f"Your {member.club.name} membership expires tomorrow.",
                email_type="expiring_soon",
            )
            if sent:
                ClubHistory.objects.create(
                    club=member.club,
                    action=f"Sent final expiration reminder to {member} ({member.email})",
                    applies_to="MEMBERSHIP",
                )
        member.membership_expiration_reminder_due = None
        member.save(update_fields=["membership_expiration_reminder_due"])

    # Nightly Mailchimp catch-up: re-sync members of connected clubs so lifecycle tags
    # (expiring-soon, expired, new-member -> long-term-member, probably-inactive) stay
    # accurate even when no edit happened. backfill() enqueues one async task per member.
    from auctions import brevo
    from auctions import mailchimp as mc
    from auctions.models import Club

    connected_clubs = (
        Club.objects.filter(active=True).exclude(mailchimp_audience_id="").exclude(mailchimp_server_prefix="")
    )
    for club in connected_clubs:
        if club.mailchimp_connected:
            mc.backfill(club)

    # Same nightly catch-up for Brevo.
    brevo_clubs = Club.objects.filter(active=True).exclude(brevo_list_id="")
    for club in brevo_clubs:
        if club.brevo_connected:
            brevo.backfill(club)


@shared_task(bind=True, ignore_result=True)
def sync_club_calendars(self):
    """Keep every club's events, Google Calendar, and Discord scheduled events in step.

    Does three things per club: mirror promoted auctions into events, exchange changes with
    Google Calendar (push ours, pull theirs), and reconcile Discord scheduled events. Each club
    is isolated, so one broken connection doesn't stop the rest.

    Only one run at a time: this is scheduled every 15 minutes but a slow Google or a big club
    can take longer than that, and two runs racing would push the same event twice and could
    provision two calendars for one club.
    """
    from django.core.cache import cache

    from auctions import club_events

    # Times out well past the beat interval, so a worker that dies mid-run can't wedge the lock.
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
    """
    Send auction-related drip marketing emails.

    Previously run every 4 minutes via cron.
    """
    call_command("auction_emails")


@shared_task(bind=True, ignore_result=True)
def email_unseen_chats(self):
    """
    Send notifications about unread chat messages.

    Previously run daily at 10:00 via cron.
    """
    call_command("email_unseen_chats")


@shared_task(bind=True, ignore_result=True)
def weekly_promo(self):
    """
    Send weekly promotional email advertising auctions and lots near you.

    Previously run weekly on Wednesday at 9:30 via cron.
    """
    call_command("weekly_promo")


@shared_task(bind=True, ignore_result=True)
def promo_push_notifications(self):
    """
    Push promotions for nearby auctions to app users who opted into push instead of the weekly email.

    Runs hourly; each promoted auction is pushed to each nearby opted-in user at most once, ever.
    """
    call_command("promo_push_notifications")


@shared_task(bind=True, ignore_result=True)
def update_ar_positions(self):
    """
    Fuse AR lot sightings into a 2D map for flagged auctions, and prune the observation buffer.

    Runs every 60 seconds; solves auctions the observations endpoint flagged dirty and deletes
    LotObservation rows older than 24 hours.
    """
    call_command("update_ar_positions")


@shared_task(bind=True, ignore_result=True)
def set_user_location(self):
    """
    Set user lat/long based on their IP address.

    Previously run every 2 hours via cron.
    """
    call_command("set_user_location")


@shared_task(bind=True, ignore_result=True)
def remove_duplicate_views(self):
    """
    Remove duplicate page views.

    Previously run every 15 minutes via cron.
    """
    call_command("remove_duplicate_views")


@shared_task(bind=True, ignore_result=True)
def webpush_notifications_deduplicate(self):
    """
    Deduplicate web push notification subscriptions.

    Previously run daily at 10:00 via cron.
    """
    call_command("webpush_notifications_deduplicate")


@shared_task(bind=True, ignore_result=True)
def deduplicate_user_interest(self):
    """
    Merge duplicate UserInterestCategory rows created by request races.

    There's no unique constraint on (user, category); the write paths tolerate
    the occasional duplicate and this reconciles them, summing the interest.
    """
    call_command("deduplicate_user_interest")


@shared_task(bind=True, ignore_result=True)
def migrate_to_cloudflare_images(self):
    """
    Move all pending locally stored images (originals only, never the generated
    thumbnails) to Cloudflare Images.

    Runs every minute to pick up new uploads; a no-op unless the CLOUDFLARE_IMAGES_*
    settings in .env are configured.  A large initial migration is chunked by the
    task time limit and resumes on the next run.
    """
    try:
        call_command("migrate_to_cloudflare_images")
    except SoftTimeLimitExceeded:
        logger.info("migrate_to_cloudflare_images hit the task time limit; the next run will resume")


@shared_task(bind=True, ignore_result=True)
def delete_cloudflare_image(self, image_id):
    """
    Delete an image from Cloudflare Images, unless a row still references it
    (lots copied with "relist" share the Cloudflare image of the original).
    """
    from auctions import cloudflare_images
    from auctions.models import AdCampaign, Club, LotImage

    if not cloudflare_images.enabled():
        return
    for model in (LotImage, Club, AdCampaign):
        if model.objects.filter(cloudflare_image_id=image_id).exists():
            return
    try:
        cloudflare_images.delete(image_id)
    except cloudflare_images.CloudflareImagesError:
        logger.exception("Could not delete Cloudflare image %s", image_id)


def schedule_auction_stats_update(run_at=None):
    """
    Schedule a one-off task to update auction stats.

    Uses django-celery-beat's ClockedSchedule and PeriodicTask to schedule
    a task to run at a specific time. Deletes and recreates the task to ensure
    it's properly enabled and picked up by celery-beat.

    This function uses a database transaction to ensure atomicity and prevent
    race conditions, guaranteeing there is always exactly one task with the
    given name.

    Args:
        run_at: datetime when the update should run. If None, runs immediately.
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

    # Use atomic transaction to ensure delete+create is atomic and prevent race conditions
    # Moving ClockedSchedule creation inside the transaction to prevent race conditions
    with transaction.atomic():
        # Create or get the schedule for this run time
        schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)

        # Delete the existing task if it exists to ensure clean state
        # This prevents issues with one-off tasks being disabled by celery-beat
        old_tasks = PeriodicTask.objects.filter(name=AUCTION_STATS_TASK_NAME)
        old_schedule_ids = [task.clocked_id for task in old_tasks if task.clocked_id]
        old_tasks.delete()

        # Clean up orphaned ClockedSchedule objects from previous runs,
        # but never delete the schedule we just created/fetched.
        if old_schedule_ids:
            ClockedSchedule.objects.filter(id__in=old_schedule_ids).exclude(id=schedule.id).delete()

        # Create a fresh task that's guaranteed to be enabled
        # The transaction ensures this is atomic with schedule creation and cleanup above
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


@shared_task(bind=True, ignore_result=True)
def update_auction_stats(self):
    """
    Update cached auction statistics for auctions whose next_update_due is past due.

    This task is self-scheduling: it processes one auction, then schedules itself
    to run again when the next auction's stats are due, rather than running on a
    fixed periodic interval.
    """
    from datetime import timedelta

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.utils import timezone

    from auctions.models import Auction

    now = timezone.now()

    logger.info("Auction stats update task started at %s", now.strftime("%Y-%m-%d %H:%M:%S %Z"))

    # Process only one auction per run, ordered by most overdue first
    # Only process auctions that have next_update_due set and are past due
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

            # Set next_update_due before recalculating to prevent concurrent recalculations
            # This ensures that if the recalculation takes longer than expected,
            # subsequent task runs won't try to recalculate the same auction again
            auction.next_update_due = now + timedelta(minutes=STATS_UPDATE_LOCK_MINUTES)
            auction.save(update_fields=["next_update_due"])

            auction.recalculate_stats()

            # Send WebSocket notification to users viewing the stats page
            # This is a best-effort notification - if it fails, we don't want to fail the entire stats update
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
            # Reschedule far enough out to skip this auction temporarily and unblock the queue
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
    """
    Schedule a one-off BAP recalculation task for a club.

    If a task already exists for this club:
    - Same run_at: re-enable it in place (no delete/recreate).
    - Different run_at: delete the task; delete the ClockedSchedule only if no other
      task still references it (prevents disrupting sibling club tasks).

    Args:
        club_pk: Primary key of the Club to recalculate.
        run_at: datetime when the recalculation should run.
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
    """Create the Google Wallet GenericClass for a club. Idempotent (409 = OK).

    On success (Wallet confirms the class exists), flips the club's
    `google_wallet_class_created` flag so we don't re-run on every save.
    """
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
    for member in members:
        try:
            update_generic_object_for_member(member)
        except requests.RequestException:
            logger.exception(
                "Google Wallet object refresh failed for club=%s member=%s",
                club.pk,
                member.pk,
            )
            raise


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def update_google_wallet_object_for_member(self, member_pk):
    """Patch the Google Wallet GenericObject for a single club member.

    Used when wallet-visible fields on ClubMember change (name,
    membership_number, membership_expiration_date).  If the member has never
    added the pass to their Wallet the object won't exist yet — that is fine,
    update_generic_object_for_member returns False on 404 without raising.
    """
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
    """Push one club member into their club's connected Mailchimp audience.

    No-op when the club has no Mailchimp connection. Reused for member edits, auction joins,
    paid invoices, the initial backfill, and the nightly catch-up. Deactivated/opted-out
    members are archived by sync_member rather than skipped, so we don't filter is_deleted here.
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
    """Push one club member into their club's connected Brevo list.

    The Brevo equivalent of sync_club_member_to_mailchimp: no-op when the club has no Brevo
    connection. Reused for member edits, auction joins, paid invoices, the initial backfill, and
    the nightly catch-up. Deactivated/opted-out members are archived by sync_member, not skipped.
    """
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
    """Expire (state=EXPIRED) every active Wallet pass for a club's members.

    When `unpaid_only=True` only members whose dues are currently lapsed are
    touched — used when the club switches to "paid members only" mode.
    """
    from auctions.google_wallet import expire_generic_object_for_member, is_configured
    from auctions.models import Club, ClubMember

    if not is_configured():
        return
    club = Club.objects.filter(pk=club_pk).first()
    if not club:
        return
    members = ClubMember.objects.filter(club=club, is_deleted=False)
    for member in members:
        if unpaid_only and member.is_paid_member:
            continue
        try:
            expire_generic_object_for_member(member)
        except requests.RequestException:
            # Let Celery's autoretry handle transient failures on the outer task.
            raise


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def refresh_google_wallet_membership_status(self):
    """Daily: refresh Google Wallet passes for members whose membership status just
    changed by the passage of time (i.e. their membership recently expired).

    Keeps the on-pass status line ("Valid through …" / "Unpaid/expired") and the
    expired red styling accurate even when no member edit fired the change signal.
    Only members who lapsed in the last few days are touched, so this stays cheap.

    The Apple-side equivalent is refresh_apple_wallet_membership_status below.
    """
    from auctions.google_wallet import is_configured, update_generic_object_for_member
    from auctions.models import ClubMember

    if not is_configured():
        return
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    # Members whose explicit expiration date passed in the last few days just flipped to
    # expired (is_paid_member goes False the day after membership_expiration_date). The
    # small window gives slack for a missed daily run.
    window_start = today - datetime.timedelta(days=3)
    members = ClubMember.objects.filter(
        is_deleted=False,
        club__google_wallet_class_created=True,
        club__membership_system__in=["january_first", "rolling"],
        membership_expiration_date__gte=window_start,
        membership_expiration_date__lt=today,
    ).select_related("user", "club")
    for member in members:
        try:
            update_generic_object_for_member(member)
        except requests.RequestException:
            logger.exception("Google Wallet daily status refresh failed for member=%s", member.pk)
            raise


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def notify_apple_wallet_devices_for_member(self, member_pk):
    """Bump a member's Apple pass version and poke every registered device via APNs.

    The bump (apple_pass_updated=now) is what makes the PassKit web service serve
    fresh content — Last-Modified on pass delivery and the passesUpdatedSince filter
    both read it — so it happens even when no device is registered (a manual
    pull-to-refresh on the pass must still see the change). Deleted members are NOT
    skipped: the update they push is the voided pass.
    """
    from django.utils import timezone

    from auctions.apple_wallet import is_configured, send_pass_update_notification
    from auctions.models import AppleDeviceRegistration, ClubMember

    if not is_configured():
        return
    if not ClubMember.objects.filter(pk=member_pk).update(apple_pass_updated=timezone.now()):
        return
    for registration in AppleDeviceRegistration.objects.filter(member_id=member_pk):
        send_pass_update_notification(registration)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def notify_apple_wallet_devices_for_club(self, club_pk):
    """Club-wide Apple pass refresh: bump every member and poke every registered device.

    Used when something on the club touches all passes at once — name/icon change
    (pass visuals) or flipping show_member_barcode (which voids/unvoids every pass).
    """
    from django.utils import timezone

    from auctions.apple_wallet import is_configured, send_pass_update_notification
    from auctions.models import AppleDeviceRegistration, ClubMember

    if not is_configured():
        return
    ClubMember.objects.filter(club_id=club_pk).update(apple_pass_updated=timezone.now())
    registrations = AppleDeviceRegistration.objects.filter(member__club_id=club_pk)
    for registration in registrations:
        send_pass_update_notification(registration)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def refresh_apple_wallet_membership_status(self):
    """Daily: push Apple pass updates to members whose membership just lapsed.

    The Apple analogue of refresh_google_wallet_membership_status — when a
    membership expires by the mere passage of time no signal fires, but the pass's
    status line and red styling need to change. Only members who lapsed in the last
    few days AND have at least one registered device are touched, so this stays cheap.
    """
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
    for member in members:
        ClubMember.objects.filter(pk=member.pk).update(apple_pass_updated=timezone.now())
        for registration in AppleDeviceRegistration.objects.filter(member=member):
            send_pass_update_notification(registration)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def geocode_club_member(self, pk):
    """Geocode a ClubMember's address and store lat/lng.

    Only runs when GOOGLE_MAPS_SERVER_API_KEY is configured. Skips members
    with no address. Falls back to copying coordinates from the linked
    user's UserData if the address is empty but the user has joined an
    auction (manually_added=False).
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
        # No address — copy coords from UserData if the user has voluntarily joined an auction
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
    """
    Schedule BAP recalculation tasks for all eligible clubs on worker startup.

    Only clubs with enable_breeder_award_program=True and next_bap_recalculation
    set are scheduled. Overdue clubs are scheduled to run at run_at; future clubs
    are scheduled at their next_bap_recalculation time.

    Args:
        run_at: datetime representing "now" — overdue clubs use this as their run time.
    """
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


@shared_task(bind=True, ignore_result=True, time_limit=None, soft_time_limit=None)
def compute_user_flow_all(self, sleep_seconds=2):
    """Pre-compute user flow data for every auction and store results in the cache.

    Processes one auction at a time, sleeping between each to stay low-CPU.
    The final step aggregates all page views into a combined "all auctions" result.
    Trigger via the admin user-flow page; results persist indefinitely in Redis.
    """
    import time

    from django.core.cache import cache
    from django.utils import timezone

    from auctions.models import Auction
    from auctions.views import AdminUserFlow

    auctions = list(Auction.objects.filter(is_deleted=False).order_by("-date_end"))
    logger.info("compute_user_flow_all: starting for %d auctions (sleep=%ss)", len(auctions), sleep_seconds)

    for i, auction in enumerate(auctions, 1):
        try:
            freq, trans = AdminUserFlow._compute_flow(auction)
            cache.set(
                f"user_flow_{auction.pk}",
                {"frequency_table": freq, "transition_table": trans, "computed_at": timezone.now().isoformat()},
                timeout=None,
            )
            logger.info("compute_user_flow_all: %d/%d done — %s", i, len(auctions), auction.slug)
        except Exception:
            logger.exception("compute_user_flow_all: failed for auction pk=%s", auction.pk)
        time.sleep(sleep_seconds)

    # Combined view across all auctions
    try:
        freq, trans = AdminUserFlow._compute_flow(None)
        now_iso = timezone.now().isoformat()
        cache.set("user_flow_all", {"frequency_table": freq, "transition_table": trans}, timeout=None)
        cache.set("user_flow_all_computed_at", now_iso, timeout=None)
        logger.info("compute_user_flow_all: combined all-auctions result cached")
    except Exception:
        logger.exception("compute_user_flow_all: failed to compute combined result")

    logger.info("compute_user_flow_all: complete")


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def geocode_speaker(self, pk):
    """Geocode a Speaker's free-text location into lat/lng.

    Mirrors geocode_club_member: only runs when GOOGLE_MAPS_SERVER_API_KEY is configured,
    and does nothing without a location to work from.  Speakers whose coordinates were
    dropped on the map by hand already have a location_coordinates value, and the pre_save
    signal has written those into lat/lng before this ever runs -- so this only fills gaps.
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
