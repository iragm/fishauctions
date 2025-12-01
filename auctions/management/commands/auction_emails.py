import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from post_office import mail

from auctions.models import Auction

logger = logging.getLogger(__name__)


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
