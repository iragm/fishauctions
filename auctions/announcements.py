"""Club announcements: one message, sent to the places a club's members look.

An admin types a sentence, ticks channels (Discord, push, email, website), and each is delivered
here with its own failure mode kept separate.

Email goes through the club's own Mailchimp or Brevo as a campaign to the club's list, never this
site's mail server, since the provider owns the unsubscribe list. Only one provider may send a given
announcement: members sync to both, so ticking both would mail everyone twice (see
ClubAnnouncementForm.clean).

Every channel carries the text and nothing else -- no "read more" link.
"""

from __future__ import annotations

import datetime
import logging

from django.contrib.sites.models import Site
from django.db.models import F
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import mark_safe

from auctions import discord_events

logger = logging.getLogger(__name__)

# Discord hard-limits messages to 2000 characters; push bodies truncate well before that.
MAX_LENGTH = 1000

# How long an unscheduled announcement waits, so a wrong date can still be retracted.
GRACE_SECONDS = 30


def reachable_members(club):
    """Members a push can reach now: a linked account with a push-enabled device holding a live FCM token,
    not opted out of contact.
    """
    from auctions.models import ClubMember

    members = ClubMember.objects.filter(club=club, is_deleted=False, user__isnull=False).exclude(
        contact_status="do_not_contact"
    )
    return members.filter(
        user__mobile_devices__push_enabled=True,
        user__mobile_devices__fcm_token__gt="",
    ).distinct()


def member_counts(club):
    """(reachable by push, total members) -- shown beside the Push checkbox on the form."""
    from auctions.models import ClubMember

    total = ClubMember.objects.filter(club=club, is_deleted=False).count()
    from auctions.notifications import push_configured

    if not push_configured():
        return (0, total)
    return (reachable_members(club).count(), total)


def discord_ready(club):
    """Whether a Discord post is possible: a linked server and a channel told to receive these."""
    return bool(club.discord_server_id and club.announcement_channel_id)


def mailchimp_ready(club):
    """Whether a Mailchimp campaign is possible: connected, and pointed at an audience."""
    return bool(club.mailchimp_access_token and club.mailchimp_server_prefix and club.mailchimp_audience_id)


def brevo_ready(club):
    """Whether a Brevo campaign is possible: connected, and pointed at a list."""
    return bool(club.brevo_api_key and club.brevo_list_id)


def email_recipient_counts(club):
    """(mailchimp contacts, brevo contacts) as last synced: an estimate of the provider's list, not the
    provider's own answer.
    """
    from auctions.models import ClubMember

    base = ClubMember.objects.filter(club=club, is_deleted=False)
    mailchimp = base.filter(mailchimp_status="subscribed").count() if mailchimp_ready(club) else 0
    brevo = base.filter(brevo_status="subscribed").count() if brevo_ready(club) else 0
    return (mailchimp, brevo)


def club_url(club):
    """Absolute URL of the club's own page -- where tapping a push notification lands."""
    domain = Site.objects.get_current().domain
    return f"https://{domain}{reverse('club_detail', kwargs={'slug': club.slug})}"


def _discord_body(announcement):
    """The announcement text and nothing else: no link, no club name (the server is the club)."""
    return announcement.text.strip()


def deliver(announcement):
    """Send *announcement* to every channel it was created with. Never raises. Returns the announcement."""
    from django.utils import timezone

    from auctions.models import ClubAnnouncement

    # Stamped before any channel is touched, so a crash mid-send never repeats a channel.
    if announcement.sent_at is None:
        announcement.sent_at = timezone.now()
        ClubAnnouncement.objects.filter(pk=announcement.pk).update(sent_at=announcement.sent_at)
    fields = []
    if announcement.send_to_discord and discord_ready(announcement.club):
        message_id = discord_events.send_channel_message(
            announcement.club.announcement_channel_id, _discord_body(announcement)
        )
        announcement.discord_sent = bool(message_id)
        announcement.discord_message_id = message_id
        fields += ["discord_sent", "discord_message_id"]
        if not message_id:
            logger.warning("Announcement %s could not be posted to Discord", announcement.pk)
    if announcement.send_to_push:
        announcement.push_recipients = _send_pushes(announcement)
        fields.append("push_recipients")
    if fields:
        announcement.save(update_fields=fields)
    if announcement.send_to_mailchimp or announcement.send_to_brevo:
        # Out of the request: a campaign send is several round trips to a third-party API.
        from auctions.tasks import send_announcement_emails

        try:
            send_announcement_emails.delay(announcement.pk)
        except Exception:
            logger.exception("Could not enqueue announcement emails for %s", announcement.pk)
            _record_email_error(announcement, "Couldn't queue the email. Nothing was sent.")
    return announcement


def _record_email_error(announcement, message):
    from auctions.models import ClubAnnouncement

    announcement.email_error = (message or "")[:300]
    ClubAnnouncement.objects.filter(pk=announcement.pk).update(email_error=announcement.email_error)


def render_email(announcement, *, greeting):
    """(html, plain text) for one provider: the same words with that provider's merge tag in the greeting.

    ``greeting`` is our own constant, marked safe here rather than in the template so Django doesn't
    escape the provider's merge-tag syntax.
    """
    club = announcement.club
    icon = club.icon_thumbnail_url or ""
    if icon.startswith("/"):
        icon = f"https://{Site.objects.get_current().domain}{icon}"
    context = {
        "announcement": announcement,
        "club": club,
        "club_icon_url": icon,
        "greeting": mark_safe(greeting) if greeting else "",  # noqa: S308 - our own constant, see docstring
    }
    html = render_to_string("auctions/announcements/email.html", context)
    text = render_to_string("auctions/announcements/email.txt", context)
    return html, text


# Mailchimp merge tag and Brevo filter for "first name, or something sensible without one".
MAILCHIMP_GREETING = "*|IF:FNAME|*Hi *|FNAME|*,*|ELSE:|*Hi there,*|END:IF|*"
BREVO_GREETING = 'Hi {{ contact.FIRSTNAME | default : "there" }},'


def send_emails(announcement):
    """Send the announcement through whichever email providers were ticked. Never raises.

    Independent on purpose: one failing must not stop the other. Errors go to ``email_error``, since
    this runs long after the admin has left the page.
    """
    from auctions import brevo as brevo_module
    from auctions import mailchimp as mc
    from auctions.models import ClubAnnouncement

    club = announcement.club
    subject = announcement.email_subject
    fields = []
    errors = []
    if announcement.send_to_mailchimp:
        html, text = render_email(announcement, greeting=MAILCHIMP_GREETING)
        try:
            announcement.mailchimp_campaign_id = mc.send_announcement_campaign(
                club, subject=subject, html=html, plain_text=text
            )
            fields.append("mailchimp_campaign_id")
        except Exception as e:
            logger.exception("Mailchimp announcement %s failed", announcement.pk)
            errors.append(f"Mailchimp: {e}")
    if announcement.send_to_brevo:
        html, text = render_email(announcement, greeting=BREVO_GREETING)
        try:
            announcement.brevo_campaign_id = brevo_module.send_announcement_campaign(
                club, subject=subject, html=html, plain_text=text
            )
            fields.append("brevo_campaign_id")
        except Exception as e:
            logger.exception("Brevo announcement %s failed", announcement.pk)
            errors.append(f"Brevo: {e}")
    announcement.email_error = " ".join(errors)[:300]
    fields.append("email_error")
    ClubAnnouncement.objects.filter(pk=announcement.pk).update(
        **{f: getattr(announcement, f) for f in fields},
    )
    return announcement


def refresh_email_opens(announcement):
    """Ask the provider how many people opened the emailed version, and store it.

    Pulled on view rather than pushed at send. ``None`` means "no report yet", not "nobody opened it",
    so the stored number is left alone.
    """
    from auctions import brevo as brevo_module
    from auctions import mailchimp as mc
    from auctions.models import ClubAnnouncement

    total = 0
    answered = False
    if announcement.mailchimp_campaign_id:
        opens = mc.campaign_opens(announcement.club, announcement.mailchimp_campaign_id)
        if opens is not None:
            total += opens
            answered = True
    if announcement.brevo_campaign_id:
        opens = brevo_module.campaign_opens(announcement.club, announcement.brevo_campaign_id)
        if opens is not None:
            total += opens
            answered = True
    if answered and total != announcement.email_opens:
        announcement.email_opens = total
        ClubAnnouncement.objects.filter(pk=announcement.pk).update(email_opens=total)
    return announcement.email_opens


def _send_pushes(announcement):
    """Enqueue one push per reachable member; returns how many it was handed to, not readership.

    Queued rather than sent inline, so a 400-member club doesn't hold the request open.
    """
    from auctions.notifications import CATEGORY_CLUB_ANNOUNCEMENT
    from auctions.tasks import send_push_to_user

    url = club_url(announcement.club)
    sent = 0
    for member in reachable_members(announcement.club).select_related("user"):
        try:
            send_push_to_user.delay(
                member.user_id,
                title=announcement.club.name,
                body=announcement.text.strip(),
                url=url,
                category=CATEGORY_CLUB_ANNOUNCEMENT,
                # One announcement, one notification: a phone off all day shows only the latest.
                collapse_key=f"club_announcement_{announcement.club_id}",
            )
        except Exception:
            logger.exception("Could not enqueue announcement push for member %s", member.pk)
            continue
        sent += 1
    return sent


def queue(announcement, *, acting_user=None):
    """Save a just-built announcement, schedule its send, and say where it is going.

    Extracted from ``views.ClubAnnouncementView.post`` so every caller goes through the grace window and
    the same Celery task rather than straight into ``deliver``. Returns ``(chose_a_time, where)``.
    """
    from django.template.defaultfilters import pluralize
    from django.utils import timezone

    from auctions.models import ClubHistory

    chose_a_time = bool(announcement.scheduled_for)
    if not chose_a_time:
        announcement.scheduled_for = timezone.now() + datetime.timedelta(seconds=GRACE_SECONDS)
    if acting_user is not None and announcement.created_by_id is None:
        announcement.created_by = acting_user
    announcement.save()
    # The beat is the backstop; this is what makes the grace window end on time.
    try:
        from auctions.tasks import send_scheduled_announcements

        send_scheduled_announcements.apply_async(
            countdown=max(1, int((announcement.scheduled_for - timezone.now()).total_seconds()) + 2)
        )
    except Exception:
        logger.warning("Could not queue the send for announcement %s; the beat will get it", announcement.pk)
    going_to = []
    if announcement.send_to_discord:
        going_to.append("Discord")
    if announcement.send_to_push:
        reachable, _total = member_counts(announcement.club)
        going_to.append(f"{reachable} phone{pluralize(reachable)}")
    for name, ticked in (("Mailchimp", announcement.send_to_mailchimp), ("Brevo", announcement.send_to_brevo)):
        if ticked:
            going_to.append(name)
    if announcement.show_on_website:
        going_to.append("your website")
    where = " and ".join(", ".join(going_to).rsplit(", ", 1)) if going_to else "nowhere"
    if chose_a_time:
        ClubHistory.objects.create(
            club=announcement.club,
            user=announcement.created_by,
            action=f"Announcement scheduled: {announcement.short_text}",
            applies_to="ANNOUNCEMENTS",
        )
    return chose_a_time, where


def send_due(now=None):
    """Deliver every scheduled announcement whose time has come; returns how many went.

    Run from the beat. Each is delivered on its own, so one club's broken Discord can't stop the rest.
    """
    from django.utils import timezone

    from auctions.models import ClubAnnouncement, ClubHistory

    now = now or timezone.now()
    due = ClubAnnouncement.objects.filter(
        is_deleted=False, sent_at__isnull=True, scheduled_for__isnull=False, scheduled_for__lte=now
    ).select_related("club")
    sent = 0
    for announcement in due:
        # Claimed with the same UPDATE that marks it sent, so two overlapping beat workers can't
        # both send it.
        claimed = ClubAnnouncement.objects.filter(pk=announcement.pk, sent_at__isnull=True).update(sent_at=now)
        if not claimed:
            continue
        announcement.sent_at = now
        try:
            deliver(announcement)
        except Exception:
            logger.exception("Scheduled announcement %s could not be delivered", announcement.pk)
            continue
        ClubHistory.objects.create(
            club=announcement.club,
            user=announcement.created_by,
            action=f"Announcement sent: {announcement.short_text}",
            applies_to="ANNOUNCEMENTS",
        )
        sent += 1
    return sent


def retract(announcement):
    """Take an announcement back as far as it can go: an unsent one never goes, the Discord post is
    deleted, and the website stops showing it. A delivered push or email cannot be recalled, so this
    returns what is still out there for the caller to say honestly.
    """
    from auctions.models import ClubAnnouncement

    club = announcement.club
    discord_removed = False
    if announcement.discord_message_id and club.announcement_channel_id:
        discord_removed = discord_events.delete_channel_message(
            club.announcement_channel_id, announcement.discord_message_id
        )
        if not discord_removed:
            logger.warning("Could not delete Discord message for announcement %s", announcement.pk)
    announcement.is_deleted = True
    ClubAnnouncement.objects.filter(pk=announcement.pk).update(is_deleted=True)
    return {
        "never_sent": announcement.sent_at is None,
        "discord_removed": discord_removed,
        "discord_left_behind": bool(announcement.discord_message_id) and not discord_removed,
        "push_delivered": announcement.push_recipients,
        "emailed": announcement.sent_by_email,
    }


def record_website_views(shown):
    """Count one impression per announcement rendered on a website: a render count, not a read count. One
    UPDATE for the page, with ``F()`` so concurrent embeds don't lose a count.
    """
    from auctions.models import ClubAnnouncement

    ids = [announcement.pk for announcement in shown if getattr(announcement, "pk", None)]
    if not ids:
        return
    ClubAnnouncement.objects.filter(pk__in=ids).update(website_views=F("website_views") + 1)


def latest_for_website(club, count=1):
    """The most recent announcements a club chose to publish, newest first.

    One function for the club page, the embed and the API. ``sent_at`` keeps a scheduled announcement
    off the website until its time.
    """
    from auctions.models import ClubAnnouncement

    return list(
        ClubAnnouncement.objects.filter(
            club=club, is_deleted=False, show_on_website=True, sent_at__isnull=False
        ).order_by("-sent_at")[:count]
    )
