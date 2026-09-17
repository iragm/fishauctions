from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from auctions.models import UserData

# from models import User


class Command(BaseCommand):
    help = "Sets rank of all users in the leaderboards"

    def handle(self, *args, **options):
        self.stdout.write("Creating userdata")
        users = User.objects.all()
        numberOfUsers = len(users)
        for user in users:
            # UserData is auto-created on save now; this only covers accounts older than that.
            UserData.objects.get_or_create(user_id=user.pk)
        userData = UserData.objects.all()
        self.stdout.write("Updating total lots sold")
        sortedList = sorted(userData, key=lambda t: -t.lots_sold)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.lots_sold:
                data.rank_total_lots = rank
                data.seller_percentile = rank / numberOfUsers * 100
                data.number_total_lots = newData.lots_sold
            else:
                data.rank_total_lots = None
                data.seller_percentile = None
                data.number_total_lots = None
            data.save()
            rank = rank + 1
        self.stdout.write("Updating total spent")
        sortedList = sorted(userData, key=lambda t: -t.total_spent)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.total_spent:
                data.rank_total_spent = rank
                data.buyer_percentile = rank / numberOfUsers * 100
                data.number_total_spent = newData.total_spent
            else:
                data.rank_total_spent = None
                data.buyer_percentile = None
                data.number_total_spent = None
            data.save()
            rank = rank + 1
        self.stdout.write("Updating total sold")
        sortedList = sorted(userData, key=lambda t: -t.total_sold)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.total_sold:
                data.rank_total_sold = rank
                data.number_total_sold = newData.total_sold
            else:
                data.rank_total_sold = None
                data.number_total_sold = None
            data.save()
            rank = rank + 1
        self.stdout.write("Updating total volume")
        sortedList = sorted(userData, key=lambda t: -t.calc_total_volume)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.calc_total_volume:
                data.rank_volume = rank
                data.volume_percentile = rank / numberOfUsers * 100
                data.total_volume = newData.calc_total_volume
            else:
                data.rank_volume = None
                data.volume_percentile = None
                data.total_volume = None
            data.save()
            rank = rank + 1
        self.stdout.write("Updating bids placed")
        sortedList = sorted(userData, key=lambda t: -t.total_bids)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.total_bids:
                data.rank_total_bids = rank
                data.number_total_bids = newData.total_bids
            else:
                data.rank_total_bids = None
                data.number_total_bids = None
            data.save()
            rank = rank + 1
