from django.core.management.base import BaseCommand, CommandError
from auctions.models import Lot, Auction, Watch, User
from django.core.mail import send_mass_mail, send_mail

def notify(email):
    #link = BASE_URL + "/lots/watched/"
    link = "auctions.toxotes.org/lots/watched/"
    send_mail(
    'Lots you\'ve watched are ending soon',
    f'Make sure to bid on the lots you\'ve watched!\nView your watched lots: {link}\n\nBest, auctions.toxotes.org',
    'Fish auction notifications',
    [email],
    fail_silently=False,
    html_message = f'Make sure to bid on the lots you\'ve watched!<br><a href="{link}">Click here to view your watched lots</a><br><br>Best, auctions.toxotes.org',
    )


class Command(BaseCommand):
    help = 'Send notifications about watched items'

    def handle(self, *args, **options):
        notificationEmails = []
        auctions = Auction.objects.filter(watch_warning_email_sent=False)
        for auction in auctions:
            if auction.ending_soon:
                self.stdout.write(f'{auction} is ending soon')
                lots = Lot.objects.filter(auction=auction)
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
        lots = Lot.objects.filter(watch_warning_email_sent=False, auction=None)
        for lot in lots:
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
            notify(email)


