"""Push notifications promoting nearby auctions to app users who opted into push.

The push analogue of ``weekly_promo``: users with ``push_notifications_instead_of_email`` are skipped
by the weekly promo email and instead get a per-auction push as each promoted auction crosses its
send-at gate. Each user is notified at most once per auction, ever (``PushNotificationSent`` is the
dedupe ledger). Runs hourly.
"""

import datetime
import logging

from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone

from auctions.models import Auction, PushNotificationSent

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send push notifications promoting nearby auctions to app users who opted into push"

    def handle(self, *args, **options):
        now = timezone.now()
        current_site = Site.objects.get_current()

        # Promoted, category-enabled auctions that haven't started yet.
        auctions = Auction.objects.filter(
            is_deleted=False,
            promote_this_auction=True,
            use_categories=True,
            date_start__gt=now,
        )
        total = 0
        for auction in auctions:
            if not auction.date_start or not auction.date_posted:
                continue
            # Give a new auction 24h to be corrected before promoting it, and don't promote earlier
            # than 7 days before it starts. Never after it starts (excluded by the queryset above).
            send_at = max(
                auction.date_posted + datetime.timedelta(hours=24),
                auction.date_start - datetime.timedelta(days=7),
            )
            if now < send_at:
                continue
            total += self._promote(auction, current_site)
        logger.info("promo_push_notifications: sent %s notification(s)", total)

    def _promote(self, auction, current_site):
        # App users who opted into push, are subscribed, have a known location, and want this kind
        # of auction. distances are checked per-user below.
        candidates = (
            User.objects.filter(userdata__push_notifications_instead_of_email=True)
            .filter(userdata__has_unsubscribed=False)
            .exclude(userdata__latitude=0, userdata__longitude=0)
            .exclude(userdata__latitude__isnull=True)
            .exclude(userdata__longitude__isnull=True)
        )
        if auction.is_online:
            candidates = candidates.filter(userdata__email_me_about_new_auctions=True)
        else:
            candidates = candidates.filter(userdata__email_me_about_new_in_person_auctions=True)
        # One push per user per auction, ever.
        already_sent = PushNotificationSent.objects.filter(category="promo", auction=auction).values_list(
            "user_id", flat=True
        )
        candidates = candidates.exclude(pk__in=already_sent)

        from auctions.tasks import send_push_to_user

        auction_url = f"https://{current_site.domain}{auction.get_absolute_url()}"
        kind = "online auction" if auction.is_online else "in-person auction"
        sent = 0
        for user in candidates:
            userdata = user.userdata
            # Re-check the full gate (device present + push configured globally), not just opt-in.
            if not userdata.user_prefers_push():
                continue
            distance = self._distance(auction, userdata)
            if distance is None:
                continue
            if auction.is_online:
                max_distance = userdata.email_me_about_new_auctions_distance
            else:
                max_distance = userdata.email_me_about_new_in_person_auctions_distance
            if max_distance is not None and distance > max_distance:
                continue
            send_push_to_user.delay(
                user.pk,
                title=auction.title,
                body=f"A promoted {kind} near you — tap for details.",
                url=auction_url,
                category="promo",
                auction_pk=auction.pk,
            )
            # Count the promotion attempt (mirrors weekly_promo_emails_sent).
            Auction.objects.filter(pk=auction.pk).update(
                promo_push_notifications_sent=F("promo_push_notifications_sent") + 1
            )
            sent += 1
        return sent

    @staticmethod
    def _distance(auction, userdata):
        """Distance (miles) from the user to the auction's closest pickup location, or None."""
        row = (
            Auction.objects.filter(pk=auction.pk)
            .annotate(distance=Auction.get_closest_location_distance_subquery(userdata.latitude, userdata.longitude))
            .first()
        )
        return row.distance if row else None
