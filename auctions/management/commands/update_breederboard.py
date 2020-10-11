from django.core.management.base import BaseCommand, CommandError
from auctions.models import UserData, User
#from models import User

class Command(BaseCommand):
    help = 'Sets rank of all users in the leaderboards'

    def handle(self, *args, **options):
        self.stdout.write("Creating userdata")
        users = User.objects.all()
        for user in users:
            try:
                data = UserData.objects.get(user_id=user.pk)
            except:
                data = UserData.objects.create(user_id=user.pk)
        userData = UserData.objects.all()
        self.stdout.write("Updating total lots sold")
        sortedList = sorted(userData, key=lambda t: -t.lots_sold)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.lots_sold:
                data.rank_total_lots = rank
                data.number_total_lots = newData.lots_sold
            else:
                data.rank_total_lots = None
                data.number_total_lots = None
            data.save()
            #self.stdout.write(f"Rank {rank}: {newData.user} with {newData.lots_sold} lots sold")
            rank = rank + 1
        self.stdout.write("Updating total unique species sold")
        sortedList = sorted(userData, key=lambda t: -t.lots_sold)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.species_sold:
                data.rank_unique_species = rank
                data.number_unique_species = newData.species_sold
            else:
                data.rank_unique_species = None
                data.number_unique_species = None
            data.save()
            #self.stdout.write(f"Rank {rank}: {newData.user} with {newData.species_sold} species")
            rank = rank + 1
        self.stdout.write("Updating total spent")
        sortedList = sorted(userData, key=lambda t: -t.total_spent)
        rank = 1
        for newData in sortedList:
            data = UserData.objects.get(user_id=newData.user.pk)
            if newData.total_spent:
                data.rank_total_spent = rank
                data.number_total_spent = newData.total_spent
            else:
                data.rank_total_spent = None
                data.number_total_spent = None
            data.save()
            #self.stdout.write(f"Rank {rank}: {newData.user} with ${newData.total_spent} spent")
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
            #self.stdout.write(f"Rank {rank}: {newData.user} with ${newData.total_bids} bids")
            rank = rank + 1