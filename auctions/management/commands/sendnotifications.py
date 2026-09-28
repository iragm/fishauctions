from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils.http import urlencode
from post_office import mail

from auctions.models import Auction, Lot, Watch
from auctions.views.browse import ALL_AUCTIONS


class Command(BaseCommand):
    help = "Send notifications about watched items"

    def handle(self, *args, **options):
        current_site = Site.objects.get_current()
        # Keyed by user pk so each watcher is notified once, and so opted-in app users can get a push
        # instead of the email (notify_user, below). Value is the User for the routing decision.
        notify_targets = {}
        # The auctions each watcher's ending lots are in (None for a lot outside one), so the link can
        # open the buying dashboard on that auction rather than whichever one they last joined.
        watched_auctions = {}
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
                        watched_auctions.setdefault(watch.user.pk, set()).add(auction.slug)
                auction.watch_warning_email_sent = True
                auction.save(update_fields=["watch_warning_email_sent"])
            # else:
            #    self.stdout.write(f'{auction} still in progress')
        # Lots that aren't attached to an auction. This loads every one that ever missed its window,
        # every run; fine only because production has no standalone lots. Filter on date_end if it does.
        lots = Lot.objects.exclude(is_deleted=True).filter(
            watch_warning_email_sent=False, auction=None, deactivated=False
        )
        ending_soon = [lot for lot in lots if lot.ending_soon]
        for watch in Watch.objects.filter(lot_number__in=ending_soon).select_related("user"):
            self.stdout.write(f"+-- {watch}")
            if watch.user:
                notify_targets[watch.user.pk] = watch.user
                watched_auctions.setdefault(watch.user.pk, set()).add(None)
        for lot in ending_soon:
            self.stdout.write(f"{lot}")
            lot.watch_warning_email_sent = True
            lot.save(update_fields=["watch_warning_email_sent"])
        # Collected all watchers; push for opted-in app users, otherwise email exactly as before.
        from auctions.notifications import notify_user

        for user in notify_targets.values():
            watched_url = self.watched_url(current_site.domain, watched_auctions.get(user.pk, set()))
            notify_user(
                user,
                category="watched",
                title="Watched lots ending soon",
                body="Lots you're watching are ending soon — tap to place a bid.",
                url=watched_url,
                send_email=lambda user=user, watched_url=watched_url: mail.send(
                    user.email,
                    template="watched_items_ending",
                    context={
                        "domain": current_site.domain,
                        "name": user.first_name,
                        "watched_url": f"{watched_url}&src=email",
                    },
                ),
            )
            self.stdout.write(f"Notified {user.email} about their watched items")

    @staticmethod
    def watched_url(domain, auctions):
        """The buying dashboard's watched lots: in the one auction they're in, or all of them."""
        only = next(iter(auctions)) if len(auctions) == 1 else None
        query = urlencode({"query": "watched", "auction": only or ALL_AUCTIONS})
        return f"https://{domain}{reverse('buying')}?{query}"
