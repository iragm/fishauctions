# Generated by Django 5.1 on 2024-10-06 23:16

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0150_auction_invoice_rounding_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="lot",
            name="summernote_description",
            field=models.CharField(default="", max_length=2000, verbose_name="Description"),
        ),
    ]
