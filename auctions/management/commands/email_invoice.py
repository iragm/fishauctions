import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice, AuctionTOS
from post_office import mail
from django.contrib.sites.models import Site

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        invoices = Invoice.objects.exclude(status='DRAFT').filter(auction__isnull=False, email_sent=False, auctiontos_user__email__isnull=False)
        for invoice in invoices:
            if invoice.auction.email_users_when_invoices_ready:
                email = invoice.auctiontos_user.email
                subject = f"Your invoice for {invoice.label} is ready for payment"
                contact_email = invoice.auction.created_by.email
                current_site = Site.objects.get_current()
                mail.send(
                    email,
                    headers={'Reply-to': contact_email},
                    template='invoice_ready',
                    context={'subject': subject, 'name': invoice.auctiontos_user.name, 'domain': current_site.domain, 'location': invoice.location, "invoice": invoice},
                )
            invoice.email_sent = True
            invoice.save()