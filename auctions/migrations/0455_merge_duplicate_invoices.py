"""Merge duplicate auction invoices ahead of 0456's unique constraint, which can't be added while any exist.

The oldest of each participant's invoices in an auction is kept, as ``Invoice.save`` used to do on every
save. The merge uses the live model on purpose: its ledger reversal (``Invoice.absorb``) is the only
correct way to move booked rows, and repeating it here would be a second copy to keep right. A fresh
database has no invoices, so nothing but the grouping query runs there.
"""

from django.db import migrations
from django.db.models import Count


def merge_duplicates(apps, schema_editor):
    HistoricalInvoice = apps.get_model("auctions", "Invoice")
    groups = (
        HistoricalInvoice.objects.filter(auctiontos_user__isnull=False)
        .values("auctiontos_user", "auction")
        .annotate(copies=Count("id"))
        .filter(copies__gt=1)
        .order_by()
    )
    groups = list(groups)
    if not groups:
        return
    from auctions.models import Invoice

    for group in groups:
        invoices = list(
            Invoice.objects.filter(auctiontos_user_id=group["auctiontos_user"], auction_id=group["auction"]).order_by(
                "date", "pk"
            )
        )
        keep = invoices[0]
        for duplicate in invoices[1:]:
            keep.absorb(duplicate)
            duplicate.delete()
        keep.recalculate()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0454_lot_donation_forced"),
    ]

    operations = [
        migrations.RunPython(merge_duplicates, migrations.RunPython.noop),
    ]
