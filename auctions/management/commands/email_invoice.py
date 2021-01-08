import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice, AuctionTOS
from post_office import mail
from django.contrib.sites.models import Site

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        invoices = Invoice.objects.filter(email_sent=False, status='UNPAID')
        for invoice in invoices:
            user = User.objects.get(pk=invoice.user.pk)
            email = user.email
            subject = "Your invoice"
            location = None
            if invoice.auction:
                contact_email = invoice.auction.created_by.email
                subject += f" for {invoice.auction.title}"
                try:
                    location = AuctionTOS.objects.get(auction=invoice.auction, user=user).pickup_location
                except:
                    pass
            elif invoice.lot:
                contact_email = invoice.lot.user.email
                subject += f" for {invoice.lot.lot_name}"
            current_site = Site.objects.get_current()
            mail.send(
                user.email,
                template='invoice_ready',
                context={'subject': subject, 'name': user.first_name, 'domain': current_site.domain, 'location': location, "invoice": invoice, 'contact_email': contact_email},
            )
            self.stdout.write(f'Emailed {user} invoice {invoice}')
            invoice.email_sent = True
            invoice.save()