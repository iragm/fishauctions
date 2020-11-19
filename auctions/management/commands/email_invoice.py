import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice, AuctionTOS
from django.core.mail import send_mail

def notify(email, auction, pk, location):
    subject = "Your auction invoice"
    if auction:
        subject = f"Your auction invoice for {auction}"
    msg = f'Thanks for bidding!  You can view your invoice here: https://auctions.toxotes.org/invoices/{pk}/\n\n'
    if auction:
        thisAuction = Auction.objects.get(pk=auction.pk)
        creator = User.objects.get(pk=thisAuction.created_by.pk)
        contactEmail = creator.email
        msg += f"You must meet at {location} to pay and exchange your lots.\n\nSee you there!\n\nIf you have questions, please contact {contactEmail}\n\n"
    msg += "Please don't reply to this email.\n\nBest,\nauctions.toxotes.org"
    send_mail(
        subject,
        msg,
        'Fish auction notifications',
        [email],
        fail_silently=False,
    )

class Command(BaseCommand):
    help = 'Email the winner to pay up on all invoices'

    def handle(self, *args, **options):
        invoices = Invoice.objects.filter(email_sent=False, paid=False)
        for invoice in invoices:
            user = User.objects.get(pk=invoice.user.pk)
            email = user.email
            location = AuctionTOS.objects.get(auction=invoice.auction, user=user).pickup_location
            notify(email, invoice.auction, invoice.pk, location)
            self.stdout.write(f'Emailed {user} invoice for {invoice.net}')
            invoice.email_sent = True
            invoice.save()