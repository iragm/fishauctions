import datetime
import logging

from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from post_office import mail

from auctions.models import Auction, UserData

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Drip marketing style emails send to the creator of an auction.  No emails are sent for in-person auctions right now."

    def handle(self, *args, **options):
        auctions = Auction.objects.exclude(is_deleted=True)
        auctions = auctions.exclude(is_online=False).filter(
            Q(email_first_sent=False)
            | Q(email_second_sent=False)
            | Q(email_third_sent=False)
            | Q(email_fourth_sent=False)
            | Q(email_fifth_sent=False)
        )
        for auction in auctions:
            current_site = Site.objects.get_current()
            userData, created = UserData.objects.get_or_create(
                user=auction.created_by,
                defaults={},
            )
            if userData.has_unsubscribed:
                # that's the end of that
                auction.email_first_sent = True
                auction.email_second_sent = True
                auction.email_third_sent = True
                auction.email_fourth_sent = True
                auction.email_fifth_sent = True
                auction.save()
            else:
                if not auction.email_first_sent:
                    # first email is sent ~ an hour after the auction has started, regardless of how long it will run for
                    if timezone.now() > auction.date_posted + datetime.timedelta(hours=1):
                        mail.send(
                            auction.created_by.email,
                            template="auction_first",
                            context={
                                "auction": auction,
                                "domain": current_site.domain,
                                "unsubscribe": userData.unsubscribe_link,
                            },
                        )
                        auction.email_first_sent = True
                        auction.save()
                if timezone.now() > auction.date_start:
                    runtime = auction.date_end - auction.date_start
                    percentComplete = timezone.now() - auction.date_start
                    percentComplete = percentComplete.total_seconds() / runtime.total_seconds() * 100
                    if percentComplete > 70:
                        if not auction.email_second_sent:
                            logger.info("sending auction_second to %s ", auction.created_by.email)
                            mail.send(
                                auction.created_by.email,
                                template="auction_second",
                                context={
                                    "auction": auction,
                                    "domain": current_site.domain,
                                    "unsubscribe": userData.unsubscribe_link,
                                },
                            )
                            auction.email_second_sent = True
                            auction.save()
                    if auction.invoiced and not auction.email_third_sent:
                        mail.send(
                            auction.created_by.email,
                            template="auction_invoices",
                            context={
                                "auction": auction,
                                "domain": current_site.domain,
                                "unsubscribe": userData.unsubscribe_link,
                            },
                        )
                        auction.email_third_sent = True
                        auction.save()
                    if percentComplete > 100:
                        if not auction.email_fifth_sent:
                            # no emails are sent on auction close.
                            # It might make sense to send a follow-up ~10 months after the auction to encourage people to use the site again
                            # but we don't do this right now
                            auction.email_fifth_sent = True
                            auction.save()
                    if percentComplete > 120:
                        if not auction.email_fourth_sent:
                            mail.send(
                                auction.created_by.email,
                                template="auction_thanks",
                                context={
                                    "auction": auction,
                                    "domain": current_site.domain,
                                    "unsubscribe": userData.unsubscribe_link,
                                },
                            )
                            auction.email_fourth_sent = True
                            auction.save()
