import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice
import datetime

class Command(BaseCommand):
    help = 'Add lots to invoices'

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
        # handle lots not associated with an auction
        # endauctions.py will set active = False and winner/winning price
        # we need to put them into an invocie, though
        nonAuctionLots = Lot.objects.filter(buyer_invoice=None, auction=None, active=False, winner__isnull=False)
        for lot in nonAuctionLots:
            try:
                # see if a recent existing invoice exists for this winner/seller
                cutoffDate = timezone.now() - datetime.timedelta(days=30)
                existingInvoices = Invoice.objects.filter(user=lot.winner, seller=lot.user, status="DRAFT", date__gte=cutoffDate).order_by("-date")
                invoice = existingInvoices[0]
            except:
                # create one if not
                invoice = Invoice.objects.create(user=lot.winner, seller=lot.user)
            lot.buyer_invoice = invoice
            lot.save()
