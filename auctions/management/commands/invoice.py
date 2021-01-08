import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice

class Command(BaseCommand):
    help = 'Sets the winner, active, and winning price on all ended lots'

    def handle(self, *args, **options):
        auctions = Auction.objects.filter(invoiced=False, date_end__lt=timezone.now())
        for auction in auctions:
            self.stdout.write(f'Invoicing {auction}')
            activeLots = Lot.objects.filter(auction=auction, active=True)
            if activeLots:
                pass
                #self.stdout.write(self.style.ERROR(' There are still active lots, wait for endauctions cron job to close them and declare a winner (this should happen automatically in a few minutes)'))
            else:
                lots = Lot.objects.filter(auction=auction)
                for lot in lots:
                    if lot.winner:
                        winnerInvoice = Invoice.objects.filter(auction=auction, user=lot.winner)
                        if winnerInvoice:
                            lot.buyer_invoice = winnerInvoice[0]
                            lot.save()
                        else:
                            newWinnerInvoice = Invoice(
                                auction=auction,
                                user=lot.winner,
                            )
                            newWinnerInvoice.save()
                            lot.buyer_invoice = newWinnerInvoice
                            lot.save()
                    # Regardless of whether the item sold or not, invoice the seller
                    sellerInvoice = Invoice.objects.filter(auction=auction, user=lot.user)
                    if sellerInvoice:
                        lot.seller_invoice = sellerInvoice[0]
                        lot.save()
                    else:
                        newSellerInvoice = Invoice(
                            auction=auction,
                            user=lot.user,
                        )
                        newSellerInvoice.save()
                        lot.seller_invoice = newSellerInvoice
                        lot.save()
                auction.invoiced = True
                auction.save()
                # now prep to send emails:
                invoices = Invoice.objects.filter(auction=auction)
                for invoice in invoices:
                    # this defaults to True
                    invoice.email_sent = False
                    invoice.save()