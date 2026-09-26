"""Merge duplicate auction invoices ahead of 0456's unique constraint, which can't be added while any exist.

The oldest of each participant's invoices in an auction is kept, as ``Invoice.save`` used to do on every
save. Historical models only: the live ``Invoice`` reaches ``Club``/``Auction``/``Lot`` columns that later
migrations add (``auctions_club.number`` from 0459), so a database with duplicates couldn't migrate past
here. This mirrors ``Invoice.absorb``. The kept invoice's ``calculated_total`` is not recalculated here;
opening the invoice or any change to its lots does that.
"""

from django.db import migrations
from django.db.models import Count


def merge_duplicates(apps, schema_editor):
    Invoice = apps.get_model("auctions", "Invoice")
    InvoiceAdjustment = apps.get_model("auctions", "InvoiceAdjustment")
    InvoicePayment = apps.get_model("auctions", "InvoicePayment")
    TapToPayAttempt = apps.get_model("auctions", "TapToPayAttempt")
    ClubMoney = apps.get_model("auctions", "ClubMoney")
    groups = list(
        Invoice.objects.filter(auctiontos_user__isnull=False)
        .values("auctiontos_user", "auction")
        .annotate(copies=Count("id"))
        .filter(copies__gt=1)
        .order_by()
    )
    for group in groups:
        invoices = list(
            Invoice.objects.filter(auctiontos_user_id=group["auctiontos_user"], auction_id=group["auction"]).order_by(
                "date", "pk"
            )
        )
        keep = invoices[0]
        for duplicate in invoices[1:]:
            InvoiceAdjustment.objects.filter(invoice=duplicate).update(invoice=keep)
            InvoicePayment.objects.filter(invoice=duplicate).update(invoice=keep)
            TapToPayAttempt.objects.filter(invoice=duplicate).update(invoice=keep)
            rows = list(ClubMoney.objects.filter(invoice=duplicate))
            reversals = [
                ClubMoney(
                    club_id=row.club_id,
                    invoice=keep,
                    source_auction_id=row.source_auction_id,
                    date=row.date,
                    amount=-row.amount,
                    description=f"Duplicate invoice reversal: {row.description}"[:500],
                    category=row.category,
                )
                for row in rows
            ]
            ClubMoney.objects.filter(invoice=duplicate).update(invoice=keep)
            ClubMoney.objects.bulk_create(reversals)
            duplicate.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0454_lot_donation_forced"),
    ]

    operations = [
        migrations.RunPython(merge_duplicates, migrations.RunPython.noop),
    ]
