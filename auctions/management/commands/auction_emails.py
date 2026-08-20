import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from post_office import mail

from auctions import club_events, discord_events
from auctions.models import Auction, AuctionHistory
from auctions.notifications import notify_user

logger = logging.getLogger(__name__)


def _send_discord_channel_message(channel_id, content):
    """POST a plain-text message to a Discord channel. Returns True on success."""
    return bool(discord_events.send_channel_message(channel_id, content))


def _create_discord_scheduled_event(guild_id, name, start_time, end_time, location_url):
    """Create a Discord Guild Scheduled Event (external type). Returns its id, or None."""
    return discord_events.create_scheduled_event(
        guild_id=guild_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        location=location_url,
    )


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
        # Only promoted auctions get broadcast to a club's Discord channel. Non-promoted auctions are
        # excluded at the DB level (like _create_discord_events): they're never announced, and their
        # sent flags stay False so they still qualify if the auction is promoted later while in-window.
        pending = (
            Auction.objects.exclude(is_deleted=True)
            .select_related("club")
            .filter(promote_this_auction=True)
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

            # The same window the club calendar uses, so an update later can't disagree with
            # what was created here.
            start_time, end_time = club_events.auction_event_window(auction)

            if not start_time or not end_time:
                logger.info("Discord event skipped for auction %s — missing start/end times", auction.slug)
                continue

            if start_time <= now:
                # Auction has already started (or ended) — Discord rejects past start times.
                auction.discord_event_created = True
                auction.save(update_fields=["discord_event_created"])
                logger.info("Discord event skipped for auction %s — start time is in the past", auction.slug)
                continue

            event_id = _create_discord_scheduled_event(
                guild_id=club.discord_server_id,
                name=auction.title,
                start_time=start_time,
                end_time=end_time,
                location_url=auction_url,
            )
            auction.discord_event_created = True
            # Keeping the id is what lets the event be moved or called off later, instead of
            # sitting in the server at its original time no matter what happens to the auction.
            auction.discord_event_id = event_id or ""
            auction.discord_event_needs_update = False
            auction.save(update_fields=["discord_event_created", "discord_event_id", "discord_event_needs_update"])
            ok = bool(event_id)
            status = "created" if ok else "failed (marked done to prevent retry)"
            AuctionHistory.objects.create(
                auction=auction,
                user=None,
                action=f"Discord: scheduled event {status}",
                applies_to="RULES",
            )
            logger.info("Discord scheduled event for auction %s: %s", auction.slug, status)
