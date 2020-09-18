import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice
from django.core.mail import send_mass_mail, send_mail

def notify(email, itemsBought, itemsSold, amount):
    itemsBoughtHtml = itemsBought.replace("\n", "<br>")
    itemsSoldHtml = itemsSold.replace("\n", "<br>")
    send_mail(
    'TFCB auction ended',
    f'Thanks for bidding in the TFCB auction!\n\nTotal: ${amount}\nItems Bought:\n{itemsBought}\n\nItems Sold:{itemsSold}\n\nMeet for pickup/delivery on Sunday, September 13th at 7 PM in front of Sears entrance in parking garage at mall:https://goo.gl/maps/Az4KqjQb3yiMYZnr8\n\nBest, auctions.toxotes.org',
    'TFCB notifications',
    [email],
    fail_silently=False,
    html_message = f'Thanks for bidding in the TFCB auction!<br><br><b>Total: ${amount}</b><br><b>Items Bought:</b><br>{itemsBoughtHtml}<br><br><b>Items Sold:</b><br>{itemsSoldHtml}<br><br>Meet for pickup/delivery on Sunday, September 13th at 7 PM in front of <a href="https://goo.gl/maps/Az4KqjQb3yiMYZnr8">Sears entrance in parking garage at mall<a><br<br><br>Best, auctions.toxotes.org',
    )

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        invoices = Invoice.objects.filter(email_sent=False, paid=False)
        for invoice in invoices:
            user = User.objects.get(pk=invoice.user.pk)
            email = user.email
            notify(email, invoice.bought, invoice.sold, invoice.net)
            self.stdout.write(f'Emailed {user} invoice for {invoice.net}')
            invoice.email_sent = True
            invoice.save()