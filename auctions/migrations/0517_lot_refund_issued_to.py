import django.db.models.deletion
from django.db import migrations, models


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
    ]
