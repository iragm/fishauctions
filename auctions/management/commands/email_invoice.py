import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice
from django.core.mail import send_mail

def notify(email, auction, pk):
    subject = "Your auction invoice"
    if auction:
        subject = auction
    send_mail(
        subject,
        f'Thanks for bidding!  You can view your invoice here: https://auctions.toxotes.org/invoices/{pk}/',
        'TFCB notifications',
        [email],
        fail_silently=False,
        html_message = f'Thanks for bidding!  <a href="https://auctions.toxotes.org/invoices/{pk}/">View your invoice here</a>'
    )

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        invoices = Invoice.objects.filter(email_sent=False, paid=False)
        for invoice in invoices:
            user = User.objects.get(pk=invoice.user.pk)
            email = user.email
            notify(email, invoice.auction, invoice.pk)
            self.stdout.write(f'Emailed {user} invoice for {invoice.net}')
            invoice.email_sent = True
            invoice.save()