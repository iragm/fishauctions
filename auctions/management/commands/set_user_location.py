import datetime
import json
import logging
from ipaddress import ip_address

import requests
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from auctions.models import Location, PageView, UserData

logger = logging.getLogger(__name__)

#: https, not http: the reply places people on the map, so anyone on the path could otherwise move
#: them anywhere.
BATCH_URL = "https://ip-api.com/batch"

#: Without one, a stalled third party hangs the task until Celery's hard limit kills the worker.
REQUEST_TIMEOUT = 30

#: ip-api caps a batch at 100.
MAX_BATCH = 100

#: The task runs every 2 hours; a day's margin covers a missed run or two.
PAGE_VIEW_WINDOW = datetime.timedelta(days=1)

#: Requests the proxy forwarded without a client address.
DOCKER_GATEWAYS = ("172.21.0.1", "172.22.0.1")


def _batch_body(addresses):
    """The JSON body for one ip-api batch: unique, valid addresses only.

    Built with ``json.dumps`` rather than string concatenation. These values come from a request
    header, so they are whatever somebody sent -- a quote in one used to break the body apart, and
    could inject entries into it. ``ip_address`` also drops the substring de-duplication that was
    here, which treated "1.1.1.1" as already present once "11.1.1.12" had been added.
    """
    seen = []
    for raw in addresses:
        text = (raw or "").strip()
        if not text or text in seen:
            continue
        try:
            ip_address(text)
        except ValueError:
            continue
        seen.append(text)
        if len(seen) >= MAX_BATCH:
            break
    return json.dumps(seen)


class Command(BaseCommand):
    help = "Set user lat/long based on their IP address"

    def handle(self, *args, **options):
        # Users who have been on the site at least a day and still have no location.
        recently = timezone.now() - datetime.timedelta(days=1)
        users = UserData.objects.filter(
            Q(latitude=0, longitude=0) | Q(location__isnull=True),
            last_ip_address__isnull=False,
            user__date_joined__lte=recently,
        ).order_by("-last_activity")[:100]
        if users:
            ip_list = _batch_body(user.last_ip_address for user in users)
            # fields=1106113 is lat, lng and country; see https://ip-api.com/docs/api:batch#test
            r = requests.post(BATCH_URL + "?fields=1106113", data=ip_list, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                ip_addresses = r.json()
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
                                    # Only what this sets: the lookup took seconds, and a full save
                                    # would put back whatever else changed on the row meanwhile.
                                    user.save(
                                        update_fields=[
                                            "latitude",
                                            "longitude",
                                            "location",
                                            "distance_unit",
                                            "preferred_currency",
                                        ]
                                    )
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
                logger.warning("User location lookup failed with HTTP %s: %s", r.status_code, r.text[:500])
            # Limits: 100 lookups a query, 15 a minute -- the daily cron is well inside both. Around
            # 440 older users have no location and no automatic way to get one, and a problematic IP
            # is hard to spot, since the error checking here is minimal.

        # Page views: one lookup per address, then one UPDATE for every view from it in the window.
        # Bounded by date: views whose address never resolves keep latitude 0 forever, and without a
        # window each run sorted all of them. PageView.save copies a location from an earlier view of
        # the same address, so only an address's first view here needs a lookup.
        window_start = timezone.now() - PAGE_VIEW_WINDOW
        unlocated = PageView.objects.filter(
            date_start__gte=window_start, ip_address__isnull=False, latitude=0, longitude=0
        ).exclude(ip_address__in=DOCKER_GATEWAYS)
        # Not .distinct(): with an order_by it is one row per view anyway. _batch_body de-duplicates.
        addresses = list(unlocated.order_by("-date_start").values_list("ip_address", flat=True)[: MAX_BATCH * 10])
        ip_list = _batch_body(addresses)
        if ip_list != "[]":
            # See https://ip-api.com/docs/api:batch#test
            r = requests.post(BATCH_URL + "?fields=25024", data=ip_list, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                for value in r.json():
                    try:
                        if value["status"] == "success":
                            unlocated.filter(ip_address=value["query"]).update(
                                latitude=value["lat"], longitude=value["lon"]
                            )
                        else:
                            logger.info("IP %s could not be located", value.get("query"))
                    except Exception as e:
                        logger.exception(e)
            else:
                logger.warning("Page view location lookup failed with HTTP %s", r.status_code)
