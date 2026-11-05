"""Auctions that turned "only approved sellers" off before Auction.let_waiting_sellers_sell existed, and still
take lots: everyone who joined before it was switched off can sell. Only auctions whose history records the
switch, so a seller stopped one at a time in an auction that never vetted sellers stays stopped, and only
rows from before the switch, so one stopped by hand after it stays stopped too."""

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def let_waiting_sellers_sell(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    AuctionHistory = apps.get_model("auctions", "AuctionHistory")
    AuctionTOS = apps.get_model("auctions", "AuctionTOS")
    candidates = (
        Auction.objects.filter(only_approved_sellers=False, is_deleted=False, auctiontos__selling_allowed=False)
        # No end date is still taking lots (Auction.can_submit_lots).
        .filter(Q(lot_submission_end_date__isnull=True) | Q(lot_submission_end_date__gte=timezone.now()))
        .distinct()
    )
    for auction in candidates:
        # Read in Python: a JSON-path lookup is one more thing to differ between MariaDB and the test DB.
        rows = AuctionHistory.objects.filter(auction=auction, applies_to="RULES").values_list(
            "changed_fields", "timestamp"
        )
        switched_off = [
            when
            for changed, when in rows
            if isinstance(changed, dict)
            and isinstance(changed.get("only_approved_sellers"), dict)
            and changed["only_approved_sellers"].get("to") is False
        ]
        if not switched_off:
            continue
        released = AuctionTOS.objects.filter(
            auction=auction, selling_allowed=False, createdon__lte=max(switched_off)
        ).update(selling_allowed=True)
        if released:
            AuctionHistory.objects.create(
                auction=auction,
                applies_to="USERS",
                action=f"Let {released} {'person' if released == 1 else 'people'} sell: only approved sellers was turned off",
            )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0519_mcpcall_admin"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auction",
            name="only_approved_sellers",
            field=models.BooleanField(
                default=False,
                help_text="Require admin approval before users can add lots.  Turning this on doesn't change anyone who already joined; turning it off lets everyone sell.",
            ),
        ),
        migrations.RunPython(let_waiting_sellers_sell, migrations.RunPython.noop),
    ]
