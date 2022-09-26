import decimal
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice
import datetime
from django.db.models import Q


class Command(BaseCommand):
    help = 'Add lots to invoices'

    def handle(self, *args, **options):
        print("This is no longer used - lots will be automatically added to invoices as soon as an auctiontos_winner or auctiontos_seller is set")
        # # we must fill out the following if not set
        # # auctiontos_seller
        # # user
        # # seller_invoice
        # # winner # including without an auction
        # # auctiontos_winner
        # # winner_invoice
        
        # # some helpful querysets:
        # lots = Lot.objects.filter(is_deleted=False)
        # active = lots.filter(active=True)
        # part_of_auction = lots.filter(auction__isnull=False)
        # needs_auctiontos_seller_filled_out = part_of_auction.filter(auctiontos_seller__isnull=True, )
        # needs_user_filled_out = part_of_auction.filter(Q(auctiontos_seller__isnull=True)|Q(user__isnull=True))
        # for lot in needs_seller:
        #     if 

        # # find all lots without
        # lots = Lot.objects.filter(Q(seller_invoice__isnull=True)|Q(winner_invoice__isnull=True)).exclude(Q())
        # auctions = Auction.objects.filter(invoiced=False, date_end__lt=timezone.now())
        # for auction in auctions:
        #     self.stdout.write(f'Invoicing {auction}')
        #     activeLots = Lot.objects.filter(auction=auction, active=True)
        #     if activeLots:
        #         pass
        #         #self.stdout.write(self.style.ERROR(' There are still active lots, wait for endauctions cron job to close them and declare a winner (this should happen automatically in a few minutes)'))
        #     else:
        #         lots = Lot.objects.filter(auction=auction)
        #         for lot in lots:
        #             if lot.winner:
        #                 winnerInvoice = Invoice.objects.filter(auction=auction, user=lot.winner)
        #                 if winnerInvoice:
        #                     lot.buyer_invoice = winnerInvoice[0]
        #                     lot.save()
        #                 else:
        #                     newWinnerInvoice = Invoice(
        #                         auction=auction,
        #                         user=lot.winner,
        #                     )
        #                     newWinnerInvoice.save()
        #                     lot.buyer_invoice = newWinnerInvoice
        #                     lot.save()
        #             # Regardless of whether the item sold or not, invoice the seller
        #             sellerInvoice = Invoice.objects.filter(auction=auction, user=lot.user)
        #             if sellerInvoice:
        #                 lot.seller_invoice = sellerInvoice[0]
        #                 lot.save()
        #             else:
        #                 newSellerInvoice = Invoice(
        #                     auction=auction,
        #                     user=lot.user,
        #                 )
        #                 newSellerInvoice.save()
        #                 lot.seller_invoice = newSellerInvoice
        #                 lot.save()
        #         auction.invoiced = True
        #         auction.save()
        # # handle lots not associated with an auction
        # # endauctions.py will set active = False and winner/winning price
        # # we need to put them into an invocie, though
        # # nonAuctionLots = Lot.objects.filter(buyer_invoice=None, auction=None, active=False, winner__isnull=False)
        # # for lot in nonAuctionLots:
        # #     try:
        # #         # see if a recent existing invoice exists for this winner/seller
        # #         cutoffDate = timezone.now() - datetime.timedelta(days=30)
        # #         existingInvoices = Invoice.objects.filter(user=lot.winner, seller=lot.user, status="DRAFT", date__gte=cutoffDate).order_by("-date")
        # #         invoice = existingInvoices[0]
        # #     except:
        # #         # create one if not
        # #         invoice = Invoice.objects.create(user=lot.winner, seller=lot.user)
        # #     lot.buyer_invoice = invoice
        # #     lot.save()
