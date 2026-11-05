"""Auctions that turned "only approved sellers" off before Auction.let_waiting_sellers_sell existed, and still
take lots: everyone who joined while it was on can sell. Only auctions whose history records the switch, so a
seller stopped one at a time in an auction that never vetted sellers stays stopped."""

from django.db import migrations
from django.utils import timezone


def let_waiting_sellers_sell(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    AuctionHistory = apps.get_model("auctions", "AuctionHistory")
    AuctionTOS = apps.get_model("auctions", "AuctionTOS")
    candidates = Auction.objects.filter(
        only_approved_sellers=False,
        is_deleted=False,
        lot_submission_end_date__gte=timezone.now(),
        auctiontos__selling_allowed=False,
    ).distinct()
    for auction in candidates:
        # Read in Python: a JSON-path lookup is one more thing to differ between MariaDB and the test DB.
        rows = AuctionHistory.objects.filter(auction=auction, applies_to="RULES").values_list(
            "changed_fields", flat=True
        )
        turned_off = any(
            isinstance(row, dict)
            and isinstance(row.get("only_approved_sellers"), dict)
            and row["only_approved_sellers"].get("to") is False
            for row in rows
        )
        if turned_off:
            AuctionTOS.objects.filter(auction=auction, selling_allowed=False).exclude(
                clubmember__selling_allowed=False
            ).update(selling_allowed=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0519_mcpcall_admin"),
    ]

    operations = [
        migrations.RunPython(let_waiting_sellers_sell, migrations.RunPython.noop),
    ]
