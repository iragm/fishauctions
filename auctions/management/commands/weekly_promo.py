from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from auctions.models import *
from django.core.mail import send_mail
from django.db.models import Count, Case, When, IntegerField, Avg
from django.core.files import File
import datetime
from post_office import mail
from django.template.loader import get_template
import os
import uuid
from django.contrib.sites.models import Site
import csv
from auctions.filters import get_recommended_lots


class Command(BaseCommand):
    help = "Send a promotional email advertising auctions and lots near you"

    def handle(self, *args, **options):
        # get any users who have opted into the weekly email
        exclude_newer_than = timezone.now() - datetime.timedelta(days=6)
        exclude_older_than = timezone.now() - datetime.timedelta(days=400)
        in_person_auctions_cutoff = timezone.now() + datetime.timedelta(days=14)
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
        for user in users:
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
                    .annotate(
                        distance=distance_to(
                            user.userdata.latitude, user.userdata.longitude
                        )
                    )
                    .order_by("distance")
                    .filter(
                        distance__lte=user.userdata.email_me_about_new_auctions_distance
                    )
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
                    .annotate(
                        distance=distance_to(
                            user.userdata.latitude, user.userdata.longitude
                        )
                    )
                    .order_by("distance")
                    .filter(
                        distance__lte=user.userdata.email_me_about_new_in_person_auctions_distance
                    )
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
            template_nearby_lots = []
            if user.userdata.email_me_about_new_local_lots:
                template_nearby_lots = get_recommended_lots(user=user, listType="local")
            template_shippable_lots = []
            if (
                user.userdata.email_me_about_new_lots_ship_to_location
                and user.userdata.location
            ):
                template_shippable_lots = get_recommended_lots(
                    user=user, listType="shipping"
                )
            current_site = Site.objects.get_current()
            if template_auctions or template_nearby_lots or template_shippable_lots:
                # don't send an email if there's nothing of interest
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
                        "special_message": "",
                    },
                )
