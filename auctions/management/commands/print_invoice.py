import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice
from django.core.mail import send_mass_mail, send_mail

# this is a placeholder until we figure out how to handle end-of-auction invoices

def notify(email, messageText):
    send_mail(
    'Complete invoice',
    "",
    'TFCB notifications',
    [email],
    fail_silently=False,
    html_message = messageText,
    )

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        invoices = Invoice.objects.filter(auction=1, paid=False)
        message = ""
        for invoice in invoices:
            user = User.objects.get(pk=invoice.user.pk)
            email = user.email
            bought = invoice.bought.replace("\n","<br>")
            sold = invoice.sold.replace("\n","<br>")
            message += f"<b>{user.username}</b> ({email}).   Check when paid <span style='font-size:2em;'>&#9633;</span><br><br>{invoice}<br><br>Bought:<br>{bought}<br><br>Sold:<br>{sold}<br>Total: $<b>{invoice.net}</b><br><br><br>"
            #self.stdout.write(f'Emailed {user} invoice for {invoice.net}')
            #invoice.email_sent = True
            #invoice.save()
        notify(email="enter your email here", messageText=message)