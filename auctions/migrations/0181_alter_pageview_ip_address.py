# Generated by Django 5.1.6 on 2025-04-07 15:05

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0180_alter_auction_use_seller_dash_lot_numbering_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pageview",
            name="ip_address",
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True),
        ),
    ]
