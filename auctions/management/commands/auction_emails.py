import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from post_office import mail

from auctions.models import Auction, AuctionHistory

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
                    subject = f"Don't forget to finish setting up {auction}!"
                else:
                    subject = f"Thanks for creating {auction}!"

                mail.send(
                    auction.created_by.email,
                    template="auction_welcome",
                    context={
                        "auction": auction,
                        "domain": current_site.domain,
                        "unsubscribe": userData.unsubscribe_link,
                        "subject": subject,
                        "enable_help": settings.ENABLE_HELP,
                    },
                )
                logger.info("Sent welcome email to %s for auction %s", auction.created_by.email, auction.slug)
                auction.welcome_email_sent = True
                auction.save()

            # Invoice email: sent 1 hour after auction end (online auctions only)
            if not auction.invoice_email_sent and auction.invoice_email_due and now >= auction.invoice_email_due:
                mail.send(
                    auction.created_by.email,
                    template="auction_invoices",
                    context={
                        "auction": auction,
                        "domain": current_site.domain,
                        "unsubscribe": userData.unsubscribe_link,
                    },
                )
                logger.info("Sent invoice email to %s for auction %s", auction.created_by.email, auction.slug)
                auction.invoice_email_sent = True
                auction.save()

            # Follow-up/thanks email: sent 24 hours after auction end (online) or start (in-person)
            if not auction.followup_email_sent and auction.followup_email_due and now >= auction.followup_email_due:
                mail.send(
                    auction.created_by.email,
                    template="auction_thanks",
                    context={
                        "auction": auction,
                        "domain": current_site.domain,
                        "unsubscribe": userData.unsubscribe_link,
                    },
                )
                logger.info("Sent follow-up email to %s for auction %s", auction.created_by.email, auction.slug)
                auction.followup_email_sent = True
                auction.save()

        # Discord auction channel notifications
        self._send_discord_notifications(now, current_site.domain)

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
            auction_url = f"https://{domain}{reverse('auction_main', kwargs={'slug': auction.slug})}"

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
            lines = [f"🐟 **{auction.title}** lot submission is now open!"]
            if auction.lot_submission_end_date:
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
                f"🐟 **{auction.title}** starts in less than 24 hours!",
                f"Auction starts <t:{int(auction.date_start.timestamp())}:f>",
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
            lines = [f"🐟 **{auction.title}** bidding is now open!"]
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

        # SECOND: auction ends (date_end <= now)
        if not auction.second_discord_sent and auction.date_end and auction.date_end <= now:
            lines = [
                f"🐟 **{auction.title}** bidding has ended!",
                auction_url,
            ]
            ok = _send_discord_channel_message(channel_id, "\n".join(lines))
            auction.second_discord_sent = True
            auction.save(update_fields=["second_discord_sent"])
            status = "sent" if ok else "failed (marked sent to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: auction-end notification {status}",
                applies_to="RULES",
            )
            logger.info("Discord auction-end for auction %s: %s", auction.slug, status)
