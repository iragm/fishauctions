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

class Command(BaseCommand):
    help = 'Just a scratchpad to do things'

    def handle(self, *args, **options):
        allUsers = User.objects.all().count()
        mikes = User.objects.filter(Q(first_name="mike")| Q(first_name="michael")).count()
        print(allUsers)
        print(mikes)
        # # get any users who have opted into the weekly email
        # users = User.objects.filter(\
        #     #Q(userdata__email_me_about_new_auctions=True) | Q(userdata__email_me_about_new_local_lots=True) | | Q(userdata__email_me_about_new_lots_ship_to_location=True)\ # fixme - why is this busted?
        #     ).filter(pk=2, userdata__latitude__isnull=False) # fixme, remove pk
        # for user in users:
        #     template_auctions = []
        #     if user.userdata.email_me_about_new_auctions:
        #         # fixme - need to filter only active auctions
        #         locations = PickupLocation.objects.filter(auction__date_start__lte=timezone.now()).exclude(auction__date_end__gte=timezone.now().exclude(auction__promote_this_auction=False)\
        #             .annotate(distance=distance_to(user.userdata.latitude, user.userdata.longitude))\
        #             .order_by('distance').filter(distance__lte=user.userdata.email_me_about_new_auctions_distance)
        #         auctions = [] # just the slugs of the auctions, to remove duplicates
        #         distances = {}
        #         titles = {}
        #         for location in locations:
        #             if location.auction.slug in auctions:
        #                 # it's already included, see if this distance is smaller
        #                 if location.distance < distances[location.auction.slug]:
        #                     distances[location.auction.slug] = location.distance
        #             else:
        #                 auctions.append(location.auction.slug)
        #                 distances[location.auction.slug] = location.distance
        #                 titles[location.auction.slug] = location.auction.title
                
        #         for auction in auctions:
        #             template_auctions.append({'slug':auction, 'distance': distances[auction], 'title': titles[auction]})
        #     template_nearby_lots = []
        #     if user.userdata.email_me_about_new_local_lots:
        #         template_nearby_lots = get_recommended_lots(user=user.pk, local=True, location=None, latitude=user.userdata.latitude,longitude=user.userdata.longitude, distance=user.userdata.local_distance)
        #     template_shippable_lots = []
        #     if user.userdata.email_me_about_new_lots_ship_to_location:
        #         template_shippable_lots = get_recommended_lots(user=user.pk, location=user.userdata.location)
        #     current_site = Site.objects.get_current()
        #     if template_auctions or template_nearby_lots or template_shippable_lots:
        #         # don't send an email if there's nothing of interest
        #         mail.send(
        #             'ira@toxotes.org', #user.email, # fixme
        #             template='weekly_promo_email',
        #             context={
        #                 'name': user.first_name,
        #                 'domain': current_site.domain,
        #                 'auctions': template_auctions,
        #                 'nearby_lots': template_nearby_lots,
        #                 'shippable_lots': template_shippable_lots,
        #                 'unsubscribe': user.userdata.unsubscribe_link
        #                 },
        #         )

        #print(PageView.objects.filter(lot_number__auction__slug__icontains='tfcb-an').count())
        #lots = Lot.objects.filter(auction__slug__icontains='pvas')
        # lots = Lot.objects.all()
        # for lot in lots:
        #     print(f"{lot.species_category};{lot.quantity};{lot.winning_price}")
        #     # when is the best time to submit a lot
        #     # duration = lot.auction.date_end - lot.date_posted
        #     # seconds = duration.total_seconds()
        #     # hours = divmod(seconds, 3600)[0]
        #     # if lot.winner:
        #     #     print(lot.promotion_weight, lot.winning_price, lot.page_views)
        # feedback info
        #print(Lot.objects.filter(feedback_rating=1).count())
        #print(Lot.objects.filter(winner_feedback_rating=1).count())
        #lots = Lot.objects.filter(winner_feedback_text__isnull=False)
        #for lot in lots:
        #    print(lot.winner_feedback_text)
        
        # users of the list view
        # users = UserData.objects.all()
        # for user in users:
        #     if user.use_list_view:
        #         print(user.user.first_name)
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
