# Generated by Django 5.1.1 on 2025-03-01 17:05

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0172_rename_use_cares_field_auction_use_custom_checkbox_field_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="use_description",
            field=models.BooleanField(blank=True, default=True, help_text="Not shown on the bulk add lots form"),
        ),
        migrations.AddField(
            model_name="auction",
            name="use_reference_link",
            field=models.BooleanField(
                blank=True, default=True, help_text="Not shown on the bulk add lots form.  Especially handy for videos."
            ),
        ),
        migrations.AlterField(
            model_name="auction",
            name="custom_checkbox_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Shown when users add lots",
                max_length=50,
                null=True,
                verbose_name="Custom checkbox name",
            ),
        ),
    ]
