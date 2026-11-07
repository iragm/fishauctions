"""Who uses the custom checkbox for CARES: ``manage.py cares_checkbox_auctions``.

One CSV row per auction creator whose auctions have a custom checkbox named like "CARES", newest
auction first, for telling them the site now marks CARES species by itself.
"""

import csv

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from auctions.models import Auction


class Command(BaseCommand):
    help = "List auction creators whose custom checkbox is used for CARES, as CSV."

    def handle(self, *args, **options):
        auctions = (
            Auction.objects.filter(
                is_deleted=False,
                use_custom_checkbox_field=True,
                custom_checkbox_name__icontains="cares",
                created_by__isnull=False,
            )
            .select_related("created_by")
            .annotate(ticked=Count("lot", filter=Q(lot__custom_checkbox=True, lot__is_deleted=False)))
            .order_by("-date_start")
        )
        creators = {}
        for auction in auctions:
            row = creators.setdefault(
                auction.created_by_id,
                {
                    "email": auction.created_by.email,
                    "name": auction.created_by.get_full_name() or auction.created_by.username,
                    "auctions": 0,
                    "lots_ticked": 0,
                    "latest_auction": auction.title,
                    "latest_date": auction.date_start.date().isoformat() if auction.date_start else "",
                    "checkbox_name": auction.custom_checkbox_name,
                },
            )
            row["auctions"] += 1
            row["lots_ticked"] += auction.ticked
        fields = ["email", "name", "auctions", "lots_ticked", "latest_auction", "latest_date", "checkbox_name"]
        writer = csv.DictWriter(self.stdout, fieldnames=fields)
        writer.writeheader()
        writer.writerows(creators.values())
