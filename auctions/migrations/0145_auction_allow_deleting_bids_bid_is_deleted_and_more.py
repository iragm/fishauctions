# Generated by Django 5.0.8 on 2024-09-08 18:40

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0144_auction_date_online_bidding_ends_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="allow_deleting_bids",
            field=models.BooleanField(
                blank=True, default=False, help_text="Allow users to delete their own bids until the auction ends"
            ),
        ),
        migrations.AddField(
            model_name="bid",
            name="is_deleted",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="lothistory",
            name="changed_price",
            field=models.BooleanField(
                default=False,
                help_text="Was this a bid that changed the price?  If False, this lot will show up in the admin chat system",
            ),
        ),
    ]
