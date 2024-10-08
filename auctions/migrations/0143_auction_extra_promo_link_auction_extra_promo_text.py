# Generated by Django 5.0.8 on 2024-08-25 18:30

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0142_club_date_contacted_for_in_person_auctions"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="extra_promo_link",
            field=models.URLField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="extra_promo_text",
            field=models.CharField(blank=True, default="", max_length=50, null=True),
        ),
    ]
