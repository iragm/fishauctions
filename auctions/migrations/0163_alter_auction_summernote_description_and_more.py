# Generated by Django 5.1.1 on 2024-11-13 15:58

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0162_alter_auction_buy_now_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auction",
            name="summernote_description",
            field=models.CharField(blank=True, default="", max_length=10000, verbose_name="Rules"),
        ),
        migrations.AlterField(
            model_name="lot",
            name="summernote_description",
            field=models.CharField(blank=True, default="", max_length=10000, verbose_name="Description"),
        ),
    ]