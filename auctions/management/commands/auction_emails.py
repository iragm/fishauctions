import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, UserData
from django.db.models import Q
from post_office import mail
from django.contrib.sites.models import Site
import datetime

class Command(BaseCommand):
    help = 'Drip marketing style emails send to the creator of an auction'

    def handle(self, *args, **options):
        auctions = Auction.objects.filter(
                    Q(email_first_sent=False)|\
                    Q(email_second_sent=False)|\
                    Q(email_third_sent=False)|\
                    Q(email_fourth_sent=False)|\
                    Q(email_fifth_sent=False))
        for auction in auctions:
            current_site = Site.objects.get_current()
            userData, created = UserData.objects.get_or_create(
                user = auction.created_by,
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
                            template='auction_first',
                            context={'auction': auction, 'domain': current_site.domain, 'unsubscribe': userData.unsubscribe_link},
                        )
                        auction.email_first_sent = True
                        auction.save()
                if timezone.now() > auction.date_start:
                    runtime = auction.date_end - auction.date_start
                    percentComplete = timezone.now() - auction.date_start
                    percentComplete = percentComplete.total_seconds()/runtime.total_seconds()*100
                    if percentComplete > 70:
                        if not auction.email_second_sent:
                            #print(f'sending auction_second to {auction.created_by.email} ')
                            mail.send(
                                auction.created_by.email,
                                template='auction_second',
                                context={'auction': auction, 'domain': current_site.domain, 'unsubscribe': userData.unsubscribe_link},
                            )
                            auction.email_second_sent = True
                            auction.save()
                    if auction.invoiced and not auction.email_third_sent:
                            mail.send(
                                auction.created_by.email,
                                template='auction_invoices',
                                context={'auction': auction, 'domain': current_site.domain, 'unsubscribe': userData.unsubscribe_link},
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
                                template='auction_thanks',
                                context={'auction': auction, 'domain': current_site.domain, 'unsubscribe': userData.unsubscribe_link},
                            )
                            auction.email_fourth_sent = True
                            auction.save()

        # for invoice in seller_invoices:
        #     seller = invoice.seller
        #     invoice.seller_email_sent = True
        #     invoice.save()
        #     subject = f"New invoice for {invoice.user}"
        #     current_site = Site.objects.get_current()
        #     mail.send(
        #         seller.email,
        #         template='invoice_ready_seller',
        #         headers={'Reply-to': invoice.user.email},
        #         context={'subject': subject, 'name': seller.first_name, 'domain': current_site.domain, "invoice": invoice},
        #     )
        #     self.stdout.write(f"Emailed {seller} their seller's copy of {invoice}")
        #     # fixme - we should email the buyer and tell them that their invoice is in draft now

        # invoices = Invoice.objects.filter(email_sent=False, status='UNPAID')
        # for invoice in invoices:
        #     user = User.objects.get(pk=invoice.user.pk)
        #     email = user.email
        #     subject = f"Your invoice for {invoice.label} is ready for payment"
        #     location = None
        #     if invoice.auction:
        #         contact_email = invoice.auction.created_by.email
        #         try:
        #             location = AuctionTOS.objects.get(auction=invoice.auction, user=user).pickup_location
        #         except:
        #             pass
        #     elif invoice.seller:
        #         contact_email = invoice.seller.email
        #     current_site = Site.objects.get_current()
        #     mail.send(
        #         user.email,
        #         headers={'Reply-to': contact_email},
        #         template='invoice_ready',
        #         context={'subject': subject, 'name': user.first_name, 'domain': current_site.domain, 'location': location, "invoice": invoice},
        #     )
        #     self.stdout.write(f'Emailed {user} invoice {invoice}')
        #     invoice.email_sent = True
        #     invoice.save()