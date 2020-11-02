from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import Auction, User, Lot, Invoice, UserData, Bid, PageView
from django.core.mail import send_mail
#import csv 
class Command(BaseCommand):
    help = 'Just a scaratchpad to do things'

    def handle(self, *args, **options):
        users = User.objects.all()
        for user in users:
            #bids = userdata.total_bids
            #lots = userdata.lots_submitted
            auction = Auction.objects.get(title='TFCB Annual Auction')
            lots = Lot.objects.filter(user=user.pk, auction=auction)
            #bid = Bid.objects.filter(user=user.pk, lot_number__auction=auction)
            won = Lot.objects.filter(winner=user.pk, auction=auction)
            #if bid and not userdata.location:
            #    print(user.email)
            #viewed = PageView.objects.filter(user=user.pk, lot_number__auction=auction)
            if lots or won: #or viewed:# and not lots:
                #if user.email == 'cmatuse@gmail.com':
                #    print(bid)
                confirmed = ""
                if user.email not in confirmed:
                    print(f"{user.first_name} {user.last_name} {user.email}")
                    #try:
                    #    userdata = UserData.objects.get(user=user.pk)
                    #    print(user.email, userdata.phone_number)
                    #except:
                    #    pass
            # if lots:
            #     #if not user.first_name:
            #     #    print(user.email)
            #     #if bids or lots:
            #     print(f"{user.first_name} {user.last_name}` {user.email}` {userdata.location}`Seller")
            # elif bid:
            #     print(f"{user.first_name} {user.last_name}` {user.email}` {userdata.location}`Bidder")
            # elif viewed:
            #     print(f"{user.first_name} {user.last_name}` {user.email}` {userdata.location}`No bids or lots sold")
