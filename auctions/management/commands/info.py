from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import *
from django.core.mail import send_mail
from django.db.models import Count
#import csv 
class Command(BaseCommand):
    help = 'Just a scaratchpad to do things'

    def handle(self, *args, **options):
        data = UserData.objects.filter(use_list_view=True)
        for user in data:
            print(user.user.first_name + " " + user.user.last_name)
        #     obj.pickup_location = form.cleaned_data['pickup_location']
        #     obj.save()
        #    if lot.description:
                #print(lot.description)
        #        lot.description_rendered = lot.description
        #        lot.save()
        #lots = Lot.objects.filter(user=2).annotate(num_bids=Count('bid')).filter(num_bids=0)
        #for lot in lots:
        #    print(lot, lot.num_bids)
        #print(Bid.objects.filter(lot_number__species_category=30, lot_number__user=4))
        # Lot.objects.annotate(
        #     has_bid=FilteredRelation(
        #         'bid', condition=Q(bid__lot_number=)
        # ),
        # ).filter(
        #     has_tag__isnull=True,
        # )

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
