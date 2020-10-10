from django.core.management.base import BaseCommand, CommandError
from auctions.models import Lot

class Command(BaseCommand):
    help = 'Sets the winner, active, and winning price on all ended auctions'

    def handle(self, *args, **options):
        lots = Lot.objects.filter(active=True)
        for lot in lots:
            if lot.ended:
                try:
                    lot.active = False
                    if lot.high_bidder and not lot.banned:
                        lot.winner = lot.high_bidder
                        lot.winning_price = lot.high_bid
                        self.stdout.write(self.style.SUCCESS(f'{lot} has been won by {lot.high_bidder} for ${lot.high_bid}'))
                    else:
                        self.stdout.write(self.style.SUCCESS(f'{lot} did not sell'))
                    lot.save()
                except Exception as e:
                    self.stdout.write(self.style.ERROR('Unable to set winner on "%s"' % lot))
                    self.stdout.write(e)