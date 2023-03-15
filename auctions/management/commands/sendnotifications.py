from django.core.management.base import BaseCommand, CommandError
from auctions.models import Lot, Auction, Watch, User
from post_office import mail
from django.contrib.sites.models import Site

class Command(BaseCommand):
    help = 'Send notifications about watched items'

    def handle(self, *args, **options):
        current_site = Site.objects.get_current()
        notificationEmails = []
        auctions = Auction.objects.exclude(is_deleted=True).filter(watch_warning_email_sent=False)
        for auction in auctions:
            if auction.ending_soon:
                self.stdout.write(f'{auction} is ending soon')
                lots = Lot.objects.exclude(is_deleted=True).filter(banned=False, auction=auction)
                for lot in lots:
                    self.stdout.write(f' +-\ {lot}')
                    watched = Watch.objects.filter(lot_number=lot.lot_number)
                    for watch in watched:
                        self.stdout.write(f' | +-- {watch}')
                        user = User.objects.get(pk=watch.user.pk)
                        email = user.email
                        if email not in notificationEmails:
                            notificationEmails.append(email)
                auction.watch_warning_email_sent = True
                auction.save()
            #else:
            #    self.stdout.write(f'{auction} still in progress')
        # Handle lots that aren't attached to an auction
        lots = Lot.objects.exclude(is_deleted=True).filter(watch_warning_email_sent=False, auction=None, deactivated=False)
        for lot in lots:
            if lot.ending_soon:
                self.stdout.write(f'{lot}')
                watched = Watch.objects.filter(lot_number=lot.lot_number)
                for watch in watched:
                    self.stdout.write(f'+-- {watch}')
                    user = User.objects.get(pk=watch.user.pk)
                    email = user.email
                    if email not in notificationEmails:
                        notificationEmails.append(email)
                lot.watch_warning_email_sent = True
                lot.save()
        # Collected all emails, time to send message
        for email in notificationEmails:
            mail.send(
                email,
                template='watched_items_ending',
                context={'domain': current_site.domain},
            )
            self.stdout.write(f'Emailed {email} about their watched items')