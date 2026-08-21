"""Club announcements: one message, sent to the places a club's members actually look.

A club already has three ways to reach people and no way to use them together: a Discord server,
a set of phones with the app on them, and its own website. This module is the "and" — an admin
types one sentence, ticks the channels, and each one is delivered here with its own failure mode
kept separate, so a Discord outage never costs the club the push.

Email goes out through the club's own Mailchimp or Brevo, as a **campaign** addressed to the
club's list — never through this site's mail server. That is not a preference: the provider owns
the unsubscribe list, and a message sent from here would reach the people who left it. The two
providers are two checkboxes because they are two accounts to configure, but only one of them may
send a given announcement: this site syncs every member to whichever lists a club has connected, so
a club with both has the *same* people on both, and ticking both would mail all of them twice. The
form refuses the combination — see ClubAnnouncementForm.clean.

Every channel carries the announcement itself and nothing else. There is no "read the rest on our
website" link, because there is no rest: an announcement is a sentence or two by design, and a link
in a message that already contains the whole message is a worse message.
"""

from __future__ import annotations

import logging

from django.contrib.sites.models import Site
from django.db.models import F
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import mark_safe

from auctions import discord_events

logger = logging.getLogger(__name__)

# Discord hard-limits a message to 2000 characters, and a push notification body is truncated by
# the OS long before that. An announcement is a sentence or two by design.
MAX_LENGTH = 1000

# How long an announcement with no schedule waits before it actually goes out. One press reaches
# Discord, every member's phone and the club's mailing list, and the mistake people make is not
# ticking the wrong box -- it is the wrong date in the sentence, which they see the instant the
# page reloads and shows it back to them. Half a minute is long enough to read what you wrote and
# press Retract, and short enough that nobody thinks it is broken.
GRACE_SECONDS = 30


def reachable_members(club):
    """Club members a push notification can actually be delivered to right now.

    Three conditions, all of them necessary: the member is linked to a site account (a name on a
    paper roster has no phone), that account has a push-enabled device with a live FCM token, and
    the member has not asked the club to stop contacting them. "No non-essential emails" is not a
    bar here — it is about email, and a member who installed the club's app and left notifications
    on has opted in to exactly this.
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
    """(reachable by push, total members) — the numbers beside the Push checkbox on the form.

    A club with 4 of 60 members reachable should see that before it decides push is the whole
    announcement, which is the entire reason the count is on the form rather than in a help page.
    """
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
    """(mailchimp contacts, brevo contacts) the club's lists would reach, as this site last saw them.

    Counted from ClubMember rather than asked of the provider, which is what makes it free to
    ask for. It is the count of members *we* have synced and not marked unsubscribed, so it is
    an estimate of the provider's list, not the provider's own answer — someone who signed up
    through the club's own website is on the list and not on this one.
    """
    from auctions.models import ClubMember

    base = ClubMember.objects.filter(club=club, is_deleted=False)
    mailchimp = base.filter(mailchimp_status="subscribed").count() if mailchimp_ready(club) else 0
    brevo = base.filter(brevo_status="subscribed").count() if brevo_ready(club) else 0
    return (mailchimp, brevo)


def club_url(club):
    """Absolute URL of the club's own page — where tapping a push notification lands."""
    domain = Site.objects.get_current().domain
    return f"https://{domain}{reverse('club_detail', kwargs={'slug': club.slug})}"


def _discord_body(announcement):
    """The announcement, and nothing else.

    No link: it is short enough to say in full everywhere it goes, so there is nothing behind one
    to go and read. No club name either -- the whole server belongs to the club, and prefixing
    every message with the name of the place it is already in is the sort of thing only a robot
    would do.
    """
    return announcement.text.strip()


def deliver(announcement):
    """Send *announcement* to every channel it was created with. Never raises.

    Each channel is independent and each records what happened on the row, because the club's
    announcement list is the only place anyone can later find out whether the Discord post landed.
    Returns the announcement.
    """
    from django.utils import timezone

    from auctions.models import ClubAnnouncement

    # Stamped and written *before* a single channel is touched. sent_at is what stops the beat
    # picking a scheduled announcement up twice, so it has to be committed ahead of the sending
    # rather than with the results: a crash halfway through should cost a channel, never repeat
    # the ones that already went to everybody's phone.
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
        # Out of the request. A Discord post is one call and a push is N enqueues, but "create
        # campaign, set content, send" against a provider is four round trips to somebody else's
        # API, and an admin should not watch a spinner while Mailchimp thinks about it.
        from auctions.tasks import send_announcement_emails

        try:
            send_announcement_emails.delay(announcement.pk)
        except Exception:
            # A broker that is down must not lose the announcement: Discord and push have already
            # gone, and the row records that email was asked for and hasn't happened.
            logger.exception("Could not enqueue announcement emails for %s", announcement.pk)
            _record_email_error(announcement, "Couldn't queue the email. Nothing was sent.")
    return announcement


def _record_email_error(announcement, message):
    from auctions.models import ClubAnnouncement

    announcement.email_error = (message or "")[:300]
    ClubAnnouncement.objects.filter(pk=announcement.pk).update(email_error=announcement.email_error)


def render_email(announcement, *, greeting):
    """(html, plain text) for one provider — same words, that provider's merge tag in the greeting.

    ``greeting`` arrives as the provider's own template syntax and is marked safe here rather than
    in the template: it is our constant, and Django would otherwise escape ``{{``/``"`` into text
    the provider prints literally at the top of every member's email.
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


# Each provider's "first name, or something sensible when we haven't got one". Mailchimp reads its
# own conditional merge tags; Brevo reads a Django-ish filter. A club's list always has somebody on
# it who signed up with an email address and nothing else, so neither is optional.
MAILCHIMP_GREETING = "*|IF:FNAME|*Hi *|FNAME|*,*|ELSE:|*Hi there,*|END:IF|*"
BREVO_GREETING = 'Hi {{ contact.FIRSTNAME | default : "there" }},'


def send_emails(announcement):
    """Send the announcement through whichever email providers were ticked. Never raises.

    The two are independent on purpose: one failing must not stop the other, and the row records
    which of them actually produced a campaign. Errors are written to ``email_error`` because this
    runs in a task, long after the admin who wrote it has left the page -- the announcement's own
    history row is the only place they will ever find out.
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

    Opens are the one real read receipt any channel has -- Discord has none and a delivered push is
    not a read one -- but they arrive hours later, so this is pulled when somebody looks at the
    history rather than pushed at send time. ``None`` from a provider means "no report yet", which
    is not the same as nobody opening it, so the stored number is left alone.
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
    """Enqueue one push per reachable member. Returns how many members it was handed to.

    The count is *delivery*, not readership: no channel here can tell a club who read something.
    Sending is queued rather than done here: a club with 400 members must not hold a form POST open
    while 400 FCM calls go out.

    The whole announcement is the notification body -- it is capped at MAX_LENGTH precisely so it
    fits in one -- and tapping it opens the club's page, because there is nothing left to read that
    the notification did not already say.
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
                # One announcement, one notification: a phone that was off all day shows the latest
                # one rather than a stack of them.
                collapse_key=f"club_announcement_{announcement.club_id}",
            )
        except Exception:
            logger.exception("Could not enqueue announcement push for member %s", member.pk)
            continue
        sent += 1
    return sent


def send_due(now=None):
    """Deliver every scheduled announcement whose time has come. Returns how many went out.

    Run from the beat (auctions.tasks.send_scheduled_announcements). Each one is delivered on its
    own so a club whose Discord is broken can't stop everybody else's, and ``sent_at`` is what
    stops a second pass sending it again -- ``deliver`` stamps it before any channel is touched,
    so a crash halfway through loses a channel rather than repeating the ones that worked.
    """
    from django.utils import timezone

    from auctions.models import ClubAnnouncement, ClubHistory

    now = now or timezone.now()
    due = ClubAnnouncement.objects.filter(
        is_deleted=False, sent_at__isnull=True, scheduled_for__isnull=False, scheduled_for__lte=now
    ).select_related("club")
    sent = 0
    for announcement in due:
        # Claim it with the same UPDATE that marks it sent. Two beat workers overlapping (a slow
        # tick, a redelivered task) would otherwise both read the same due row and both send it;
        # whoever loses the race gets 0 rows back and leaves it alone.
        claimed = ClubAnnouncement.objects.filter(pk=announcement.pk, sent_at__isnull=True).update(sent_at=now)
        if not claimed:
            continue
        announcement.sent_at = now
        try:
            deliver(announcement)
        except Exception:
            logger.exception("Scheduled announcement %s could not be delivered", announcement.pk)
            continue
        # Every announcement goes out from here -- an unscheduled one waits GRACE_SECONDS so it can
        # be retracted -- so this is the row that says it was said, and it belongs to whoever wrote
        # it rather than to the beat that happened to be running.
        ClubHistory.objects.create(
            club=announcement.club,
            user=announcement.created_by,
            action=f"Announcement sent: {announcement.short_text}",
            applies_to="ANNOUNCEMENTS",
        )
        sent += 1
    return sent


def retract(announcement):
    """Take an announcement back as far as it can be taken back. Returns what is still out there.

    Three of the five channels can genuinely be taken back: a scheduled announcement that has not
    gone yet simply never goes, the Discord post can be deleted, and the club's website stops
    showing it. The other two cannot -- a push notification is on somebody's lock screen and an
    email is in somebody's inbox, and no amount of pressing a button here reaches either. The
    caller tells the admin which of those happened rather than showing "Retracted" and letting them
    believe it was all undone.
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
    """Count one impression per announcement that was actually rendered on a website.

    A **render count, not a read count**, and the difference is the whole reason this is a separate
    number from ``email_opens``: it says the announcement was put on a page somebody loaded. That
    is still worth having, because the question a club actually asks about the website channel is
    not "did they read it" but "is the snippet I pasted into my site showing anything at all", and
    until this existed the answer was a globe icon with nothing beside it.

    One UPDATE for the whole page, and ``F()`` rather than read-modify-write so two embeds served
    at the same moment cannot lose each other's count.
    """
    from auctions.models import ClubAnnouncement

    ids = [announcement.pk for announcement in shown if getattr(announcement, "pk", None)]
    if not ids:
        return
    ClubAnnouncement.objects.filter(pk__in=ids).update(website_views=F("website_views") + 1)


def latest_for_website(club, count=1):
    """The most recent announcements a club chose to publish, newest first.

    ``show_on_website`` is what separates "tell the Discord regulars" from "put this on the front
    page", and the club page, the embed and the API all read this one function so they can never
    disagree about which of the two a given announcement was.
    """
    from auctions.models import ClubAnnouncement

    # sent_at is what keeps a scheduled announcement off the club's website until its time: the
    # row exists from the moment it is written, and "show on website" is about where it goes, not
    # about whether it has gone yet.
    return list(
        ClubAnnouncement.objects.filter(
            club=club, is_deleted=False, show_on_website=True, sent_at__isnull=False
        ).order_by("-sent_at")[:count]
    )
