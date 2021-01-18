import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice, AuctionTOS
from post_office import mail
from django.contrib.sites.models import Site

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        seller_invoices = Invoice.objects.filter(seller_email_sent=False, status='DRAFT', seller__isnull=False)
        for invoice in seller_invoices:
            seller = invoice.seller
            invoice.seller_email_sent = True
            invoice.save()
            subject = f"New invoice for {invoice.user}"
            current_site = Site.objects.get_current()
            mail.send(
                seller.email,
                template='invoice_ready_seller',
                headers={'Reply-to': invoice.user.email},
                context={'subject': subject, 'name': seller.first_name, 'domain': current_site.domain, "invoice": invoice},
            )
            # email the buyer and tell them that their invoice is in draft now
            subject = f"Your draft invoice for {invoice.seller}"
            mail.send(
                invoice.user.email,
                template='invoice_draft_buyer',
                headers={'Reply-to': invoice.seller.email},
                context={'subject': subject, 'name': invoice.user.first_name, 'domain': current_site.domain, "invoice": invoice},
            )
            self.stdout.write(f"Emailed {seller} and {invoice.user} that their invoice {invoice} has been created")

        invoices = Invoice.objects.filter(email_sent=False, status='UNPAID')
        for invoice in invoices:
            user = User.objects.get(pk=invoice.user.pk)
            email = user.email
            subject = f"Your invoice for {invoice.label} is ready for payment"
            location = None
            if invoice.auction:
                contact_email = invoice.auction.created_by.email
                try:
                    location = AuctionTOS.objects.get(auction=invoice.auction, user=user).pickup_location
                except:
                    pass
            elif invoice.seller:
                contact_email = invoice.seller.email
            current_site = Site.objects.get_current()
            mail.send(
                user.email,
                headers={'Reply-to': contact_email},
                template='invoice_ready',
                context={'subject': subject, 'name': user.first_name, 'domain': current_site.domain, 'location': location, "invoice": invoice},
            )
            self.stdout.write(f'Emailed {user} that their invoice {invoice} is ready for payment')
            invoice.email_sent = True
            invoice.save()