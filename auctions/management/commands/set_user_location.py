import datetime
import logging

import requests
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from auctions.models import Location, PageView, UserData

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Set user lat/long based on their IP address"

    def handle(self, *args, **options):
        # get users that have been on the site for at least 1 days, but have not set their location
        recently = timezone.now() - datetime.timedelta(days=1)
        users = UserData.objects.filter(
            Q(latitude=0, longitude=0) | Q(location__isnull=True),
            last_ip_address__isnull=False,
            user__date_joined__lte=recently,
        ).order_by("-last_activity")[:100]
        # build a list of IPs - bit awkward as we can't use single quotes here, and it has to be a string, not a list
        ip_list = "["
        if users:
            for user in users:
                ip_list += f'"{user.last_ip_address}",'
            ip_list = ip_list[:-1] + "]"  # trailing , breaks things
            # See here for more documentation: https://ip-api.com/docs/api:batch#test
            # lat and lng only
            # r = requests.post("http://ip-api.com/batch?fields=25024", data=ip_list)
            # lat lng and country
            r = requests.post("http://ip-api.com/batch?fields=1106113", data=ip_list)
            if r.status_code == 200:
                ip_addresses = r.json()
                # now, we cycle through users again and assign their location based on IP
                for user in users:
                    for value in ip_addresses:
                        try:
                            if user.last_ip_address == value["query"]:
                                if value["status"] == "success":
                                    if not user.latitude:
                                        user.latitude = value["lat"]
                                    if not user.longitude:
                                        user.longitude = value["lon"]
                                    if not user.location:
                                        continent = Location.objects.filter(name=value["continent"]).first()
                                        country = Location.objects.filter(name=value["country"]).first()
                                        default = Location.objects.filter(name="Other").first()
                                        user.location = next(
                                            value for value in [continent, country, default] if value is not None
                                        )
                                    # Set distance_unit and preferred_currency based on country
                                    country_name = value.get("country", "")
                                    if country_name == "United States":
                                        user.distance_unit = "mi"
                                        user.preferred_currency = "USD"
                                    elif country_name == "Canada":
                                        user.distance_unit = "km"
                                        user.preferred_currency = "CAD"
                                    elif country_name == "United Kingdom":
                                        user.distance_unit = "km"
                                        user.preferred_currency = "GBP"
                                    elif country_name == "Australia":
                                        user.distance_unit = "km"
                                        user.preferred_currency = "AUD"
                                    elif country_name == "Japan":
                                        user.distance_unit = "km"
                                        user.preferred_currency = "JPY"
                                    elif country_name == "China":
                                        user.distance_unit = "km"
                                        user.preferred_currency = "CNY"
                                    elif country_name == "Switzerland":
                                        user.distance_unit = "km"
                                        user.preferred_currency = "CHF"
                                    elif country_name in [
                                        "Austria",
                                        "Belgium",
                                        "Cyprus",
                                        "Estonia",
                                        "Finland",
                                        "France",
                                        "Germany",
                                        "Greece",
                                        "Ireland",
                                        "Italy",
                                        "Latvia",
                                        "Lithuania",
                                        "Luxembourg",
                                        "Malta",
                                        "Netherlands",
                                        "Portugal",
                                        "Slovakia",
                                        "Slovenia",
                                        "Spain",
                                    ]:
                                        # Eurozone countries
                                        user.distance_unit = "km"
                                        user.preferred_currency = "EUR"
                                    else:
                                        # Default to km and USD for all other countries
                                        user.distance_unit = "km"
                                        user.preferred_currency = "USD"
                                    user.save()
                                    logger.info(
                                        "assigning %s with IP %s a location", user.user.email, user.last_ip_address
                                    )
                                    break
                                else:
                                    logger.info(
                                        "IP %s may not be valid - verify it and set their location manually",
                                        user.last_ip_address,
                                    )
                        except Exception as e:
                            logger.exception(e)
            else:
                logger.warning("Query failed for this IP list:")
                logger.warning(ip_list)
                logger.warning(r["text"])
            # some limitations to note:
            # we are capped at 100 lookups per query
            # Looks like the cap on this service is 15 per minute, so it's easily able to meet our needs if the cron job is run more often.
            # right now, this is run once per day via cron.  Not a big deal for current user loads, and this can be run twice a day if needed
            # older users don't have a location assigned (around 440 users, I do not have a way to assign a location to these automatically)
            # if there is a problematic IP, it may be hard to spot, the error checking is minimal here

        # now, we handle pageviews separately.  There is some duplicate code here that could get merged with the user lookup above
        # first check is to see if we've already got this IP address in the system somewhere
        pageviews = (
            PageView.objects.exclude(ip_address="172.21.0.1")
            .exclude(ip_address="172.22.0.1")
            .filter(ip_address__isnull=False, latitude=0, longitude=0)
            .order_by("-date_start")[:100]
        )
        # now that we've cycled
        ip_list = "["
        if pageviews:
            for view in pageviews:
                if view.ip_address not in ip_list:
                    ip_list += f'"{view.ip_address}",'
            ip_list = ip_list[:-1] + "]"  # trailing , breaks things
            # See here for more documentation: https://ip-api.com/docs/api:batch#test
            r = requests.post("http://ip-api.com/batch?fields=25024", data=ip_list)
            if r.status_code == 200:
                ip_addresses = r.json()
                # now, we cycle through views again and assign their location based on IP
                for view in pageviews:
                    for value in ip_addresses:
                        try:
                            if view.ip_address == value["query"]:
                                if value["status"] == "success":
                                    view.latitude = value["lat"]
                                    view.longitude = value["lon"]
                                    view.save()
                                    break
                                else:
                                    logger.warning(
                                        "IP %s may not be valid - verify it and set their location manually",
                                        view.ip_address,
                                    )
                        except Exception as e:
                            logger.exception(e)
