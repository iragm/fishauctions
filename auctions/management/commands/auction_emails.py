import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from post_office import mail

from auctions.models import Auction, AuctionHistory
from auctions.notifications import notify_user

logger = logging.getLogger(__name__)


def _send_discord_channel_message(channel_id, content):
    """POST a plain-text message to a Discord channel. Returns True on success."""
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token or not channel_id:
        return False
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json={"content": content}, timeout=10)
        return resp.status_code in (200, 201)
    except Exception:
        return False


def _create_discord_scheduled_event(guild_id, name, start_time, end_time, location_url):
    """Create a Discord Guild Scheduled Event (external type). Returns True on success."""
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token or not guild_id:
        return False
    url = f"https://discord.com/api/v10/guilds/{guild_id}/scheduled-events"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    payload = {
        "name": name,
        "scheduled_start_time": start_time.isoformat(),
        "scheduled_end_time": end_time.isoformat(),
        "privacy_level": 2,  # GUILD_ONLY
        "entity_type": 3,  # EXTERNAL
        "entity_metadata": {"location": location_url},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code not in (200, 201):
            logger.warning(
                "Discord scheduled event creation failed: status=%s body=%s",
                resp.status_code,
                resp.text,
            )
            return False
        return True
    except Exception as exc:
        logger.exception("Discord scheduled event creation error: %s", exc)
        return False


class Command(BaseCommand):
    help = "Send reminder emails to auction creators: welcome, invoice, and follow-up emails."

    def handle(self, *args, **options):
        current_site = Site.objects.get_current()
        now = timezone.now()

        # Get auctions that have at least one email that needs to be sent
        auctions = Auction.objects.exclude(is_deleted=True).filter(
            Q(welcome_email_sent=False, welcome_email_due__lte=now)
            | Q(invoice_email_sent=False, invoice_email_due__lte=now)
            | Q(followup_email_sent=False, followup_email_due__lte=now)
        )

        for auction in auctions:
            userData = auction.created_by.userdata
            if userData.has_unsubscribed:
                # Mark all emails as sent for unsubscribed users
                auction.welcome_email_sent = True
                auction.invoice_email_sent = True
                auction.followup_email_sent = True
                auction.save()
                continue

            # Welcome email: sent 24 hours after auction creation
            if not auction.welcome_email_sent and auction.welcome_email_due and now >= auction.welcome_email_due:
                # Determine subject based on admin checklist completion
                if not (
                    auction.admin_checklist_location_set
                    and auction.admin_checklist_rules_updated
                    and auction.admin_checklist_joined
                ):
                    subject = f"Finish setting up {auction}!"
                else:
                    subject = f"Thanks for creating {auction}!"

                notify_user(
                    auction.created_by,
                    category="auction_admin",
                    title=subject,
                    body=f"Tap to manage {auction}.",
                    url=f"https://{current_site.domain}{auction.get_absolute_url()}",
                    send_email=lambda: mail.send(
                        auction.created_by.email,
                        template="auction_welcome",
                        context={
                            "auction": auction,
                            "domain": current_site.domain,
                            "unsubscribe": userData.unsubscribe_link,
                            "subject": subject,
                            "enable_help": settings.ENABLE_HELP,
                        },
                    ),
                    auction_pk=auction.pk,
                )
                logger.info("Sent welcome notification to %s for auction %s", auction.created_by.email, auction.slug)
                auction.welcome_email_sent = True
                auction.save()

            # Invoice email: sent 1 hour after auction end (online auctions only)
            if not auction.invoice_email_sent and auction.invoice_email_due and now >= auction.invoice_email_due:
                notify_user(
                    auction.created_by,
                    category="auction_admin",
                    title=f"Invoices are ready for {auction}",
                    body=f"Tap to review invoices for {auction}.",
                    url=f"https://{current_site.domain}{auction.get_absolute_url()}",
                    send_email=lambda: mail.send(
                        auction.created_by.email,
                        template="auction_invoices",
                        context={
                            "auction": auction,
                            "domain": current_site.domain,
                            "unsubscribe": userData.unsubscribe_link,
                        },
                    ),
                    auction_pk=auction.pk,
                )
                logger.info("Sent invoice notification to %s for auction %s", auction.created_by.email, auction.slug)
                auction.invoice_email_sent = True
                auction.save()

            # Follow-up/thanks email: sent 24 hours after auction end (online) or start (in-person)
            if not auction.followup_email_sent and auction.followup_email_due and now >= auction.followup_email_due:
                notify_user(
                    auction.created_by,
                    category="auction_admin",
                    title=f"Thanks for running {auction}",
                    body=f"Tap to see how {auction} went.",
                    url=f"https://{current_site.domain}{auction.get_absolute_url()}",
                    send_email=lambda: mail.send(
                        auction.created_by.email,
                        template="auction_thanks",
                        context={
                            "auction": auction,
                            "domain": current_site.domain,
                            "unsubscribe": userData.unsubscribe_link,
                        },
                    ),
                    auction_pk=auction.pk,
                )
                logger.info("Sent follow-up notification to %s for auction %s", auction.created_by.email, auction.slug)
                auction.followup_email_sent = True
                auction.save()

        # Discord auction channel notifications
        self._send_discord_notifications(now, current_site.domain)
        # Discord scheduled event creation
        self._create_discord_events(now, current_site.domain)

    def _send_discord_notifications(self, now, domain):
        pending = (
            Auction.objects.exclude(is_deleted=True)
            .select_related("club")
            .filter(Q(first_discord_sent=False) | Q(second_discord_sent=False))
        )

        for auction in pending:
            club = auction.club
            has_discord = club and club.discord_server_id and club.auction_channel_id
            if not has_discord:
                # Silently mark sent — no channel configured, nothing to send.
                update_fields = []
                if not auction.first_discord_sent:
                    auction.first_discord_sent = True
                    update_fields.append("first_discord_sent")
                if not auction.second_discord_sent:
                    auction.second_discord_sent = True
                    update_fields.append("second_discord_sent")
                auction.save(update_fields=update_fields)
                continue

            channel_id = club.auction_channel_id
            auction_url = f"https://{domain}/?{auction.slug}"

            if not auction.is_online:
                self._notify_inperson(auction, channel_id, auction_url, now)
            else:
                self._notify_online(auction, channel_id, auction_url, now)

    def _notify_inperson(self, auction, channel_id, auction_url, now):
        # FIRST: lot submission opens
        if (
            not auction.first_discord_sent
            and auction.lot_submission_start_date
            and auction.lot_submission_start_date <= now
        ):
            lines = [f"**{auction.title}** lot submission is now open!"]
            if auction.lot_submission_end_date and (
                not auction.date_start or auction.lot_submission_end_date < auction.date_start
            ):
                lines.append(f"Submit lots before <t:{int(auction.lot_submission_end_date.timestamp())}:f>")
            if auction.date_start:
                lines.append(f"Auction starts <t:{int(auction.date_start.timestamp())}:f>")
            lines.append(auction_url)
            ok = _send_discord_channel_message(channel_id, "\n".join(lines))
            auction.first_discord_sent = True
            auction.save(update_fields=["first_discord_sent"])
            status = "sent" if ok else "failed (marked sent to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: lot-submission-open notification {status}",
                applies_to="RULES",
            )
            logger.info("Discord lot-submission-open for auction %s: %s", auction.slug, status)

        # SECOND: 24 hours before auction starts
        if (
            not auction.second_discord_sent
            and auction.date_start
            and auction.date_start - timedelta(hours=24) <= now < auction.date_start
        ):
            lines = [
                f"**{auction.title}** starts <t:{int(auction.date_start.timestamp())}:R>",
                auction_url,
            ]
            ok = _send_discord_channel_message(channel_id, "\n".join(lines))
            auction.second_discord_sent = True
            auction.save(update_fields=["second_discord_sent"])
            status = "sent" if ok else "failed (marked sent to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: 24h-before-start notification {status}",
                applies_to="RULES",
            )
            logger.info("Discord 24h-before-start for auction %s: %s", auction.slug, status)

    def _notify_online(self, auction, channel_id, auction_url, now):
        # FIRST: auction starts (date_start <= now)
        if not auction.first_discord_sent and auction.date_start and auction.date_start <= now:
            lines = [f"**{auction.title}** bidding is now open!"]
            if auction.date_end:
                lines.append(f"Bidding closes <t:{int(auction.date_end.timestamp())}:f>")
            lines.append(auction_url)
            ok = _send_discord_channel_message(channel_id, "\n".join(lines))
            auction.first_discord_sent = True
            auction.save(update_fields=["first_discord_sent"])
            status = "sent" if ok else "failed (marked sent to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: auction-start notification {status}",
                applies_to="RULES",
            )
            logger.info("Discord auction-start for auction %s: %s", auction.slug, status)

        # SECOND: 24 hours before bidding ends
        if (
            not auction.second_discord_sent
            and auction.date_end
            and auction.date_end - timedelta(hours=24) <= now < auction.date_end
        ):
            lines = [
                f"**{auction.title}** — bidding ends <t:{int(auction.date_end.timestamp())}:R>",
                auction_url,
            ]
            ok = _send_discord_channel_message(channel_id, "\n".join(lines))
            auction.second_discord_sent = True
            auction.save(update_fields=["second_discord_sent"])
            status = "sent" if ok else "failed (marked sent to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: 24h-before-end reminder {status}",
                applies_to="RULES",
            )
            logger.info("Discord 24h-before-end for auction %s: %s", auction.slug, status)

    def _create_discord_events(self, now, domain):
        """Create Discord scheduled events for promoted auctions that haven't had one yet."""
        cutoff = now - timedelta(hours=24)
        pending = (
            Auction.objects.exclude(is_deleted=True)
            .select_related("club")
            .filter(
                discord_event_created=False,
                promote_this_auction=True,
                date_posted__lte=cutoff,
            )
        )

        for auction in pending:
            club = auction.club
            if not club or not club.discord_server_id:
                # No Discord server — mark done so we don't revisit
                auction.discord_event_created = True
                auction.save(update_fields=["discord_event_created"])
                continue

            if not club.create_events_for_auctions:
                # Feature disabled for this club — mark done to avoid a backlog when later enabled
                auction.discord_event_created = True
                auction.save(update_fields=["discord_event_created"])
                continue

            auction_url = f"https://{domain}/?{auction.slug}"

            if auction.is_online:
                start_time = auction.date_start
                end_time = auction.date_end
            else:
                start_time = auction.date_start
                end_time = auction.date_start + timedelta(hours=2) if auction.date_start else None

            if not start_time or not end_time:
                logger.info("Discord event skipped for auction %s — missing start/end times", auction.slug)
                continue

            if start_time <= now:
                # Auction has already started (or ended) — Discord rejects past start times.
                auction.discord_event_created = True
                auction.save(update_fields=["discord_event_created"])
                logger.info("Discord event skipped for auction %s — start time is in the past", auction.slug)
                continue

            ok = _create_discord_scheduled_event(
                guild_id=club.discord_server_id,
                name=auction.title,
                start_time=start_time,
                end_time=end_time,
                location_url=auction_url,
            )
            auction.discord_event_created = True
            auction.save(update_fields=["discord_event_created"])
            status = "created" if ok else "failed (marked done to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: scheduled event {status}",
                applies_to="RULES",
            )
            logger.info("Discord scheduled event for auction %s: %s", auction.slug, status)
