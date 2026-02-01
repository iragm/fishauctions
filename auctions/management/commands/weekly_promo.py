import datetime
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone
from post_office import mail

from auctions.filters import get_recommended_lots
from auctions.models import Auction, PickupLocation, distance_to

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send a promotional email advertising auctions and lots near you"

    def handle(self, *args, **options):
        # get any users who have opted into the weekly email
        exclude_newer_than = timezone.now() - datetime.timedelta(days=6)
        exclude_older_than = timezone.now() - datetime.timedelta(days=400)
        in_person_auctions_cutoff = timezone.now() + datetime.timedelta(days=7)
        users = (
            User.objects.filter(
                Q(userdata__email_me_about_new_in_person_auctions=True)
                | Q(userdata__email_me_about_new_auctions=True)
                | Q(userdata__email_me_about_new_local_lots=True)
                | Q(userdata__email_me_about_new_lots_ship_to_location=True)
            )
            .exclude(userdata__latitude=0, userdata__longitude=0)
            .exclude(userdata__last_activity__gte=(exclude_newer_than))
            .exclude(userdata__last_activity__lte=(exclude_older_than))
        )
        # users = User.objects.filter(pk=1)

        user_count = users.count()
        logger.info("Weekly promo: Found %s eligible users", user_count)
        self.stdout.write(self.style.SUCCESS(f"Found {user_count} eligible users"))

        emails_sent = 0
        emails_skipped = 0

        for user in users:
            try:
                template_auctions = []
                if user.userdata.email_me_about_new_auctions:
                    locations = (
                        PickupLocation.objects.filter(
                            Q(
                                auction__date_start__lte=timezone.now(),
                                auction__is_online=True,
                            )
                            & ~Q(auction__date_end__lte=timezone.now())
                        )
                        .exclude(auction__use_categories=False)
                        .exclude(auction__promote_this_auction=False)
                        .exclude(auction__is_deleted=True)
                        .annotate(distance=distance_to(user.userdata.latitude, user.userdata.longitude))
                        .order_by("distance")
                        .filter(distance__lte=user.userdata.email_me_about_new_auctions_distance)
                    )
                    auctions = []  # just the slugs of the auctions, to remove duplicates
                    distances = {}
                    titles = {}
                    for location in locations:
                        if location.auction.slug in auctions:
                            # it's already included, see if this distance is smaller
                            if location.distance < distances[location.auction.slug]:
                                distances[location.auction.slug] = location.distance
                        else:
                            auctions.append(location.auction.slug)
                            distances[location.auction.slug] = location.distance
                            titles[location.auction.slug] = location.auction.title
                    for auction in auctions:
                        template_auctions.append(
                            {
                                "slug": auction,
                                "distance": distances[auction],
                                "title": titles[auction],
                            }
                        )
                        # Increment the weekly promo email counter for this auction
                        Auction.objects.filter(slug=auction).update(
                            weekly_promo_emails_sent=F("weekly_promo_emails_sent") + 1
                        )
                # see #130; request to differentiate between online and in-person
                if user.userdata.email_me_about_new_in_person_auctions:
                    locations = (
                        PickupLocation.objects.filter(
                            # any in person auctions before the cutoff, excluding any that have already started
                            Q(
                                auction__date_start__lte=in_person_auctions_cutoff,
                                auction__is_online=False,
                            )
                            & ~Q(auction__date_start__lte=timezone.now())
                        )
                        .exclude(auction__use_categories=False)
                        .exclude(auction__promote_this_auction=False)
                        .exclude(auction__is_deleted=True)
                        .annotate(distance=distance_to(user.userdata.latitude, user.userdata.longitude))
                        .order_by("distance")
                        .filter(distance__lte=user.userdata.email_me_about_new_in_person_auctions_distance)
                    )
                    auctions = []  # just the slugs of the auctions, to remove duplicates
                    distances = {}
                    titles = {}
                    for location in locations:
                        if location.auction.slug in auctions:
                            # it's already included, see if this distance is smaller
                            if location.distance < distances[location.auction.slug]:
                                distances[location.auction.slug] = location.distance
                        else:
                            auctions.append(location.auction.slug)
                            distances[location.auction.slug] = location.distance
                            titles[location.auction.slug] = location.auction.title
                    for auction in auctions:
                        template_auctions.append(
                            {
                                "slug": auction,
                                "distance": distances[auction],
                                "title": titles[auction],
                            }
                        )
                        # Increment the weekly promo email counter for this auction
                        Auction.objects.filter(slug=auction).update(
                            weekly_promo_emails_sent=F("weekly_promo_emails_sent") + 1
                        )
                template_nearby_lots = []
                if user.userdata.email_me_about_new_local_lots:
                    try:
                        template_nearby_lots = get_recommended_lots(user=user, listType="local")
                    except Exception as e:
                        logger.error("Error getting local lots for user %s: %s", user.username, e)

                template_shippable_lots = []
                if user.userdata.email_me_about_new_lots_ship_to_location and user.userdata.location:
                    try:
                        template_shippable_lots = get_recommended_lots(user=user, listType="shipping")
                    except Exception as e:
                        logger.error("Error getting shippable lots for user %s: %s", user.username, e)

                current_site = Site.objects.get_current()
                if template_auctions or template_nearby_lots or template_shippable_lots:
                    # don't send an email if there's nothing of interest
                    try:
                        logger.info(
                            "Sending weekly promo to %s (auctions: %s, nearby lots: %s, shippable lots: %s)",
                            user.email,
                            len(template_auctions),
                            len(template_nearby_lots),
                            len(template_shippable_lots),
                        )
                        mail.send(
                            user.email,
                            template="weekly_promo_email",
                            context={
                                "name": user.first_name,
                                "domain": current_site.domain,
                                "auctions": template_auctions,
                                "nearby_lots": template_nearby_lots,
                                "shippable_lots": template_shippable_lots,
                                "unsubscribe": user.userdata.unsubscribe_link,
                                "special_message": settings.WEEKLY_PROMO_MESSAGE,
                                "mailing_address": settings.MAILING_ADDRESS,
                            },
                        )
                        emails_sent += 1
                    except Exception as e:
                        logger.error("Error sending email to %s: %s", user.email, e)
                else:
                    logger.debug(
                        "Skipping user %s - no content (auctions: %s, nearby lots: %s, shippable lots: %s)",
                        user.username,
                        len(template_auctions),
                        len(template_nearby_lots),
                        len(template_shippable_lots),
                    )
                    emails_skipped += 1
            except Exception:
                logger.exception("Error processing user %s", user.username)

        logger.info("Weekly promo complete: %s sent, %s skipped (no content)", emails_sent, emails_skipped)
        self.stdout.write(
            self.style.SUCCESS(
                f"Weekly promo complete: {emails_sent} emails sent, {emails_skipped} skipped (no content)"
            )
        )
