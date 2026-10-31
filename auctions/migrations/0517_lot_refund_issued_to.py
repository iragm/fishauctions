import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F


def refunds_belong_to_the_current_winner(apps, schema_editor):
    Lot = apps.get_model("auctions", "Lot")
    Lot.objects.filter(partial_refund_percent__gt=0, auctiontos_winner__isnull=False).update(
        refund_issued_to=F("auctiontos_winner")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0516_mcpcall"),
    ]

    operations = [
        migrations.AddField(
            model_name="lot",
            name="refund_issued_to",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="auctions.auctiontos",
            ),
        ),
        migrations.RunPython(refunds_belong_to_the_current_winner, migrations.RunPython.noop),
    ]
