"""Existing refunds belong to the lot's current winner. Its own migration: MariaDB has no transactional
DDL, so a failed backfill next to the AddField would leave the column behind with 0517 unrecorded."""

from django.db import migrations
from django.db.models import F


def refunds_belong_to_the_current_winner(apps, schema_editor):
    Lot = apps.get_model("auctions", "Lot")
    Lot.objects.filter(partial_refund_percent__gt=0, auctiontos_winner__isnull=False).update(
        refund_issued_to=F("auctiontos_winner")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0517_lot_refund_issued_to"),
    ]

    operations = [
        migrations.RunPython(refunds_belong_to_the_current_winner, migrations.RunPython.noop),
    ]
