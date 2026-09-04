from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.urls import reverse
from post_office import mail

from auctions.models import Auction, Lot, Watch


class Command(BaseCommand):
    help = "Send notifications about watched items"

    def handle(self, *args, **options):
        current_site = Site.objects.get_current()
        # Keyed by user pk so each watcher is notified once, and so opted-in app users can get a push
        # instead of the email (notify_user, below). Value is the User for the routing decision.
        notify_targets = {}
        auctions = Auction.objects.exclude(is_deleted=True).filter(watch_warning_email_sent=False, is_online=True)
        for auction in auctions:
            if auction.ending_soon:
                self.stdout.write(f"{auction} is ending soon")
                lots = Lot.objects.exclude(is_deleted=True).filter(banned=False, auction=auction)
                # One query for the whole auction's watchers, with the users attached. This used to
                # be a Watch query per lot and then *two* user fetches per watch -- the FK, and then
                # a User.objects.get for the object the FK had already returned.
                watched = Watch.objects.filter(lot_number__in=lots).select_related("user")
                for watch in watched:
                    self.stdout.write(f" | +-- {watch}")
                    if watch.user:
                        notify_targets[watch.user.pk] = watch.user
                auction.watch_warning_email_sent = True
                auction.save()
            # else:
            #    self.stdout.write(f'{auction} still in progress')
        # Handle lots that aren't attached to an auction
        lots = Lot.objects.exclude(is_deleted=True).filter(
            watch_warning_email_sent=False, auction=None, deactivated=False
        )
        ending_soon = [lot for lot in lots if lot.ending_soon]
        for watch in Watch.objects.filter(lot_number__in=ending_soon).select_related("user"):
            self.stdout.write(f"+-- {watch}")
            if watch.user:
                notify_targets[watch.user.pk] = watch.user
        for lot in ending_soon:
            self.stdout.write(f"{lot}")
            lot.watch_warning_email_sent = True
            lot.save()
        # Collected all watchers; push for opted-in app users, otherwise email exactly as before.
        from auctions.notifications import notify_user

        watched_url = f"https://{current_site.domain}{reverse('watched')}"
        for user in notify_targets.values():
            notify_user(
                user,
                category="watched",
                title="Watched lots ending soon",
                body="Lots you're watching are ending soon — tap to place a bid.",
                url=watched_url,
                send_email=lambda user=user: mail.send(
                    user.email,
                    template="watched_items_ending",
                    context={"domain": current_site.domain},
                ),
            )
            self.stdout.write(f"Notified {user.email} about their watched items")
