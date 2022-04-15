from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import *
from django.core.mail import send_mail
from django.db.models import Count, Case, When, IntegerField, Avg
from django.core.files import File
from datetime import datetime
from post_office import mail
from django.template.loader import get_template
import os
import uuid
from django.contrib.sites.models import Site
import csv
from auctions.filters import get_recommended_lots
from easy_thumbnails.files import get_thumbnailer
import re
from collections import Counter


class Command(BaseCommand):
    help = 'Just a scratchpad to do things'
    def handle(self, *args, **options):
        auctions = Auction.objects.filter(promote_this_auction=True, slug__icontains="TFCB")
        total = 0
        for auction in auctions:
            print(auction.title)
            #total += auction.total_unsold_lots * auction.unsold_lot_fee
            total += auction.club_profit
        print(total)

        # f = open("views.txt", "a")
        # histories = LotHistory.objects.filter(lot__auction__slug='nec-virtual-convention-auction', changed_price=True)
        # for history in histories:
        #     f.write(history.timestamp.strftime("%b %d %Y %H:%M:%S"))
        #     f.write(',')
        #     f.write(str(history.bid_amount))
        #     f.write(',')
        #     f.write(str(history.current_price))
        #     f.write('\n')

        # f.close()

        # some info to add to the dashboard at some point
        # qs = UserData.objects.filter(user__is_active=True)
        # print(qs.exclude(user__lot__isnull=False).distinct().count()) # sellers
        # print(qs.exclude(user__winner__isnull=False).distinct().count()) # buyers
        # print(qs.exclude(user__bid__isnull=True).distinct().count()) # bidders
        # print(qs.exclude(Q(user__lot__isnull=False)|Q(user__winner__isnull=False)|Q(user__bid__isnull=True)).distinct().count()) # bidders


        # list page views by time
        # views = PageView.objects.filter(user__isnull=False)
        # f = open("views.txt", "a")
        # for view in views:
        #     print(view.total_time)
        #     f.write(f"{view.total_time}\n")
        # f.close()

        # sortedList = [item for items, c in Counter(words).most_common() for item in [items] * c]
        # f = open("words.txt", "a")
        # alreadyWritten = []
        # for word in sortedList:
        #     if word not in alreadyWritten:
        #         alreadyWritten.append(word)
        #         f.write(word + "\n")
        # f.close()



        # why are there so many mikes??
        #allUsers = User.objects.all().count()
        #mikes = User.objects.filter(Q(first_name="mike")| Q(first_name="michael")).count()
        #print(allUsers)
        #print(mikes)
        
#  auction = Auction.objects.get(title="TFCB Annual Auction")
#        users = User.objects.filter(pageview__lot_number__auction=auction).annotate(dcount=Count('id'))
#        for user in users:
            #print(lot.num_views)
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
