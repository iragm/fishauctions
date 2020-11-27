from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import *
from django.core.mail import send_mail
from django.db.models import Count, Case, When, IntegerField
from PIL import Image
from easy_thumbnails.files import get_thumbnailer
from io import BytesIO
from django.core.files import File

import os
#import csv 
class Command(BaseCommand):
    help = 'Just a scaratchpad to do things'

    def handle(self, *args, **options):
        pass
    
        # take a guess at how many lots will sell and what the club's profit will be
        # auction = Auction.objects.get(slug='slug-for-this-auction')
        # lots = Lot.objects.filter(auction=auction)
        # total = 0
        # club = 0
        # for lot in lots:
        # #if lot.number_of_bids:
        #     total += lot.high_bid
        #     if lot.high_bid > 2:
        #         club += 3
        # print(f"total: {total}")
        # print(f"club: {club}")
        
        # this is how many bids we have binned by price
        # labels = []
        # bids = Bid.objects.all()
        # data = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,]
        # last = 0
        # for i in range(len(data)):
        #     labels.append(i)
        # for bid in bids:
        #     if bid.amount > len(data):
        #         data[-1] += 1
        #     else:
        #         data[bid.amount] += 1
        # print(labels)
        # print(data)



                # users = User.objects.filter(email__icontains='gmail')
        # #for user in users:
        # #    print(user.first_name + " " + user.last_name)
        # nonGmailUsers = User.objects.all()
        # print(len(users))
        # print(len(nonGmailUsers))
        
#         
#  auction = Auction.objects.get(title="TFCB Annual Auction")
#        users = User.objects.filter(pageview__lot_number__auction=auction).annotate(dcount=Count('id'))
#        for user in users:
            #print(lot.num_views)
            #print(f'https://auctions.toxotes.org/lots/{lot.lot_number}')
#            print(f"{user.first_name} {user.last_name}")
            
        # auction = Auction.objects.get(title="TFCB Annual Auction")
        
            #print(f'{lot.pk}, {bids}, {}')
        # users = User.objects.all()
        # auction = Auction.objects.get(title="PVAS Fall Auction")
        # for user in users:
        #     #won = len(Lot.objects.filter(winner=user, auction=auction))
        #     views = len(PageView.objects.filter(lot_number__auction=auction, user=user))
        #     if views > 5:
        #         print(f'"{user.first_name} {user.last_name}", {views}')

        # lots = Lot.objects.all()
        # noImageCount = 0
        # internetImageCount = 0
        # representativeImageCount = 0
        # actualImageCount = 0
        
        # noImageSold = 0
        # internetImageSold = 0
        # representativeImageSold = 0
        # actualImageSold = 0

        # noImageValue = []
        # internetImageValue = []
        # representativeImageValue = []
        # actualImageValue = []

        # for lot in lots:
        #     if lot.image:
        #         if lot.image_source == "RANDOM":
        #             internetImageCount += 1
        #             if lot.winner:
        #                 internetImageSold += 1
        #                 internetImageValue.append(lot.winning_price)
        #         if lot.image_source == "ACTUAL":
        #             actualImageCount += 1
        #             if lot.winner:
        #                 actualImageSold += 1
        #                 actualImageValue.append(lot.winning_price)
        #         if lot.image_source == "REPRESENTATIVE":
        #             representativeImageCount += 1
        #             if lot.winner:
        #                 representativeImageSold += 1
        #                 representativeImageValue.append(lot.winning_price)
        #     else:
        #         noImageCount += 1
        #         if lot.winner:
        #             noImageSold += 1
        #             noImageValue.append(lot.winning_price)
        # print("none", noImageSold, noImageCount, sum(noImageValue) / len(noImageValue) )
        # print("internet", internetImageSold, internetImageCount, sum(internetImageValue) / len(internetImageValue) )
        # print("representative", representativeImageSold, representativeImageCount, sum(representativeImageValue) / len(representativeImageValue) )
        # print("actual", actualImageSold, actualImageCount, sum(actualImageValue) / len(actualImageValue) )
            
        # users = User.objects.all()
        # for user in users:
        #     #bids = userdata.total_bids
        #     #lots = userdata.lots_submitted
        #     auction = Auction.objects.get(title='TFCB Annual Auction')
        #     lots = Lot.objects.filter(user=user.pk, auction=auction)
        #     #bid = Bid.objects.filter(user=user.pk, lot_number__auction=auction)
        #     won = Lot.objects.filter(winner=user.pk, auction=auction)
        #     #if bid and not userdata.location:
        #     #    print(user.email)
        #     #viewed = PageView.objects.filter(user=user.pk, lot_number__auction=auction)
        #     if lots or won: #or viewed:# and not lots:
        #         #if user.email == 'cmatuse@gmail.com':
        #         #    print(bid)
        #         confirmed = ""
        #         if user.email not in confirmed:
        #             print(f"{user.first_name} {user.last_name} {user.email}")
        #             #try:
        #             #    userdata = UserData.objects.get(user=user.pk)
        #             #    print(user.email, userdata.phone_number)
        #             #except:
        #             #    pass
        #     # if lots:
        #     #     #if not user.first_name:
        #     #     #    print(user.email)
        #     #     #if bids or lots:
        #     #     print(f"{user.first_name} {user.last_name}` {user.email}` {userdata.location}`Seller")
        #     # elif bid:
        #     #     print(f"{user.first_name} {user.last_name}` {user.email}` {userdata.location}`Bidder")
        #     # elif viewed:
        #     #     print(f"{user.first_name} {user.last_name}` {user.email}` {userdata.location}`No bids or lots sold")
