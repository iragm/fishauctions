from django.core.management.base import BaseCommand


def compare_model_instances(instance1, instance2):
    """
    Compare all fields of two Django model instances.

    :param instance1: First model instance
    :param instance2: Second model instance
    :return: Dictionary with field names as keys and tuples of (instance1 value, instance2 value) as values for differing fields.
    """
    if type(instance1) is not type(instance2):
        msg = "Instances are not of the same model."
        raise ValueError(msg)

    differences = {}
    for field in instance1._meta.fields:
        field_name = field.name
        value1 = getattr(instance1, field_name)
        value2 = getattr(instance2, field_name)

        if value1 != value2:
            differences[field_name] = (value1, value2)

    return differences


class Command(BaseCommand):
    help = "Just a scratchpad to do things"
    # def handle(self, *args, **options):
    # campaigns = AuctionCampaign.objects.all()
    # for campaign in campaigns:
    #    campaign.update
    # campaigns = AuctionCampaign.objects.values('result').annotate(count=Count('result'))
    # result_dict = {result['result']: result['count'] for result in campaigns}
    # joined = result_dict['JOINED']
    # no_response = result_dict['NONE']
    # viewed = result_dict['VIEWED']
    # none = result_dict['ERR']
    # total = AuctionCampaign.objects.all().count()
    # total = total-none
    # joined = joined/total * 100
    # print('joined', joined)
    # no_response = no_response/total * 100
    # print('no response', no_response)
    # viewed = viewed/total * 100
    # print('viewed', viewed)
    # none = none/total * 100
    # print('no email sent', none)
    # lots_with_buy_now_available = Lot.objects.filter(is_deleted=False, auction__isnull=False, auction__promote_this_auction=True, buy_now_price__isnull=False)
    # #lots_with_buy_now_used = Lot.objects.filter(is_deleted=False, auction__isnull=False, auction__promote_this_auction=True, buy_now_price__isnull=False, winning_price=F('buy_now_price'))
    # sum_of_buy_now_used = 0
    # sum_of_lots_that_sold_for_more_than_buy_now = 0
    # sum_of_lots_that_sold_for_less_than_buy_now = 0
    # count_of_unsold = 0
    # count_of_buy_now_used = 0
    # count_of_lots_that_sold_for_more_than_buy_now = 0
    # count_of_lots_that_sold_for_less_than_buy_now = 0
    # for lot in lots_with_buy_now_available:
    #     if not lot.winning_price:
    #         count_of_unsold += 1
    #     else:
    #         if lot.winning_price == lot.buy_now_price:
    #             count_of_buy_now_used += 1
    #             sum_of_buy_now_used += lot.winning_price
    #         if lot.winning_price > lot.buy_now_price:
    #             count_of_lots_that_sold_for_more_than_buy_now += 1
    #             sum_of_lots_that_sold_for_more_than_buy_now += lot.winning_price
    #         if lot.winning_price < lot.buy_now_price:
    #             count_of_lots_that_sold_for_less_than_buy_now += 1
    #             sum_of_lots_that_sold_for_less_than_buy_now += lot.winning_price
    # print(f"buy now is used {count_of_buy_now_used/lots_with_buy_now_available.count()*100}% of the time when it's available")
    # print(f"average sell price when buy now is used is ${sum_of_buy_now_used/count_of_buy_now_used}")
    # print(f"average sell price when buy now is not used is ${(sum_of_lots_that_sold_for_less_than_buy_now+sum_of_lots_that_sold_for_more_than_buy_now)/(count_of_lots_that_sold_for_less_than_buy_now+count_of_lots_that_sold_for_more_than_buy_now)}")
    # print(f"when buy now is not used, the lot sells for more than the buy now price {count_of_lots_that_sold_for_more_than_buy_now/(count_of_lots_that_sold_for_less_than_buy_now+count_of_lots_that_sold_for_more_than_buy_now)*100}% of the time")

    # lots = Lot.objects.filter(Q(feedback_text__isnull=False)|Q(winner_feedback_text__isnull=False))
    # for lot in lots:
    #    print(lot.winner_feedback_text, lot.feedback_text)

    # # fix auctiontos_user on invoices:
    # invoices = Invoice.objects.filter(auctiontos_user__isnull=True, auction__isnull=False, user__isnull=False)
    # for invoice in invoices:
    #     tos = AuctionTOS.objects.filter(user=invoice.user, auction=invoice.auction).first()
    #     invoice.auctiontos_user = tos
    #     invoice.save()

    # set auctiontos data from user: this has already been run once and should never need to be run again
    # auctiontos = AuctionTOS.objects.filter(bidder_number="", user__isnull=False)
    # for tos in auctiontos:
    #     tos.name = tos.user.first_name + " " + tos.user.last_name
    #     tos.email = tos.user.email
    #     tos.address = tos.user.userdata.address or ""
    #     tos.phone_number = tos.user.userdata.phone_number or ""
    #     tos.bidder_number = None
    #     tos.save()

    # # set auctiontos winners and sellers on lots, as appropriate.  This is hopefully a one-time thing that won't be needed again
    # lots = Lot.objects.filter(auction__isnull=False)
    # for lot in lots:
    #     if not lot.description:
    #         lot.description = "No description entered"
    #         lot.save()
    #     if lot.winner:
    #         if not lot.auctiontos_winner:
    #             corrected_winner = AuctionTOS.objects.filter(user=lot.winner, auction=lot.auction).first()
    #             if not corrected_winner:
    #                 pass#print(lot)
    #             else:
    #                 lot.auctiontos_winner = corrected_winner
    #                 lot.save()
    #     if lot.user:
    #         if not lot.auctiontos_seller:
    #             corrected_seller = AuctionTOS.objects.filter(user=lot.user, auction=lot.auction).first()
    #             if not corrected_seller:
    #                 pass#print(lot)
    #             else:
    #                 #print(f"setting {corrected_seller} to be the winenr of {lot}")
    #                 lot.auctiontos_seller = corrected_seller
    #                 lot.save()
    # # check to make sure that all worked correctly
    # lots = Lot.objects.filter(auction__isnull=False, auctiontos_seller__isnull=True)
    # for lot in lots:
    #     print(lot)

    # auctions with no pickup location
    # auctions = Auction.objects.all()
    # for auction in auctions:
    #     locations = PickupLocation.objects.filter(auction=auction)
    #     if not locations:
    #         print(auction.get_absolute_url())

    # create a graph of how prices change over time
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
    # allUsers = User.objects.all().count()
    # mikes = User.objects.filter(Q(first_name="mike")| Q(first_name="michael")).count()
    # print(allUsers)
    # print(mikes)


#  auction = Auction.objects.get(title="TFCB Annual Auction")
#        users = User.objects.filter(pageview__lot_number__auction=auction).annotate(dcount=Count('id'))
#        for user in users:
# print(lot.num_views)
#            print(f"{user.first_name} {user.last_name}")

# auction = Auction.objects.get(title="TFCB Annual Auction")

# print(f'{lot.pk}, {bids}, {}')
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
