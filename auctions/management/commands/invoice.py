import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice

class Command(BaseCommand):
    help = 'Sets the winner, active, and winning price on all ended auctions'

    def handle(self, *args, **options):
        auctions = Auction.objects.filter(invoiced=False, date_end__lt=timezone.now())
        for auction in auctions:
            self.stdout.write(f'Invoicing {auction}')
            activeLots = Lot.objects.filter(auction=auction, active=True)
            if activeLots:
                self.stdout.write(self.style.ERROR(' There are still still active lots, wait for endauctions cron job to close them (this should happen automatically in a few minutes) and declare a winner'))
            else:
                lots = Lot.objects.filter(auction=auction)
                for lot in lots:
                    if not lot.winner:
                        self.stdout.write(f' +-- {lot} did not sell')
                        if not lot.donation:
                            clubCut = auction.unsold_lot_fee # bill the seller even if the item didn't sell
                        else:
                            clubCut = 0
                        sellerCut = 0 - clubCut
                        sellEntryString = f"{lot} not sold\n"
                    else:
                        buyEntryString = f"{lot} for ${lot.winning_price}\n"
                        # Buyer (lot winner)
                        winnerInvoice = Invoice.objects.filter(auction=auction, user=lot.winner)
                        if winnerInvoice:
                            winnerInvoice[0].bought += buyEntryString
                            winnerInvoice[0].total_bought += decimal.Decimal(lot.winning_price)
                            winnerInvoice[0].save()
                            lot.buyer_invoice = winnerInvoice[0]
                            lot.save()
                        else:
                            newWinnerInvoice = Invoice(
                                auction=auction,
                                user=lot.winner,
                                sold="",
                                total_sold = 0,
                                bought=buyEntryString,
                                total_bought=lot.winning_price
                            )
                            newWinnerInvoice.save()
                            lot.buyer_invoice = newWinnerInvoice
                            lot.save()
                        # Seller - need to take club's cut
                        if lot.donation:
                            clubCut = lot.winning_price
                        else:
                            clubCut = ( lot.winning_price * auction.winning_bid_percent_to_club / 100 ) + auction.lot_entry_fee
                        sellerCut = lot.winning_price - clubCut
                        sellEntryString = f"{lot} for ${lot.winning_price} (${clubCut} to club)\n"
                        self.stdout.write(f' +-- {lot} sold for ${lot.winning_price}. ${clubCut} to club')
                    sellerInvoice = Invoice.objects.filter(auction=auction, user=lot.user)
                    if sellerInvoice:
                        sellerInvoice[0].sold += sellEntryString
                        sellerInvoice[0].total_sold += decimal.Decimal(sellerCut)
                        sellerInvoice[0].save()
                        lot.seller_invoice = sellerInvoice[0]
                        lot.save()
                    else:
                        newSellerInvoice = Invoice(
                            auction=auction,
                            user=lot.user,
                            bought="",
                            total_bought = 0,
                            sold=sellEntryString,
                            total_sold=sellerCut
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