"""Rewrite ``auctions/data/scraper_networks.txt`` from the networks each scraper cloud announces today."""

import ipaddress

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from auctions.crawlers import SCRAPER_NETWORKS_FILE

#: Clouds whose addresses sent thousands of page view beacons a day from stock desktop browsers, one view each,
#: and no visitor who did anything. Tencent and Huawei each announce through an international and a Chinese AS.
SCRAPER_ASNS = {
    132203: "Tencent Cloud",
    45090: "Tencent Cloud, China",
    136907: "Huawei Cloud",
    55990: "Huawei Cloud, China",
    45102: "Alibaba Cloud",
    37963: "Alibaba Cloud, China",
}

ANNOUNCED_PREFIXES = "https://stat.ripe.net/data/announced-prefixes/data.json"


def announced(asn):
    response = requests.get(ANNOUNCED_PREFIXES, params={"resource": f"AS{asn}"}, timeout=60)
    response.raise_for_status()
    return [ipaddress.ip_network(row["prefix"]) for row in response.json()["data"]["prefixes"]]


class Command(BaseCommand):
    help = "Refresh the cloud networks whose page views are not counted, from RIPEstat"

    def handle(self, *args, **options):
        lines = [
            f"# Written by manage.py refresh_scraper_networks on {timezone.now().date()}. Do not edit by hand.",
        ]
        lines += [f"# AS{asn} {name}" for asn, name in SCRAPER_ASNS.items()]
        networks = []
        for asn in SCRAPER_ASNS:
            found = announced(asn)
            if not found:
                # An empty answer would silently start counting that cloud's scrapers again.
                message = f"RIPEstat returned no prefixes for AS{asn}"
                raise RuntimeError(message)
            networks += found
        for version in (4, 6):
            lines += [str(n) for n in ipaddress.collapse_addresses(n for n in networks if n.version == version)]
        SCRAPER_NETWORKS_FILE.write_text("\n".join(lines) + "\n")
        self.stdout.write(f"{len(lines)} lines written to {SCRAPER_NETWORKS_FILE}")
