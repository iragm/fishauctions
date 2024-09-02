from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from auctions.models import Bid, PageView, UserInterestCategory


def updateInterest(category, user, weight):
    interest, created = UserInterestCategory.objects.get_or_create(
        category=category, user=user, defaults={"interest": 0}
    )
    interest.interest += weight
    interest.save()
    return


class Command(BaseCommand):
    help = "Update how interested a given user is in all categories. \
        This will reset all user data.  It needs to be run only if the BID_WEIGHT or VIEW_WEIGHT settings change"

    def handle(self, *args, **options):
        self.stdout.write("Creating userdata")
        users = User.objects.all()
        for user in users:
            allBids = Bid.objects.select_related("lot_number__species_category").filter(
                user=user
            )
            pageViews = PageView.objects.select_related(
                "lot_number__species_category"
            ).filter(user=user)
            # remove all user interests
            UserInterestCategory.objects.filter(user=user).delete()
            # regenerate them
            for item in allBids:
                # bids weigh more than views
                updateInterest(
                    item.lot_number.species_category, user, settings.BID_WEIGHT
                )
            for item in pageViews:
                updateInterest(
                    item.lot_number.species_category, user, settings.VIEW_WEIGHT
                )
