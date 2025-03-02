from django.db import migrations, models


def set_quantity_field(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    Auction.objects.filter(is_online=True).update(use_quantity_field=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0169_auction_custom_field_1"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="use_donation_field",
            field=models.BooleanField(blank=True, default=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="use_i_bred_this_fish_field",
            field=models.BooleanField(blank=True, default=True, verbose_name="Use Breed Points field"),
        ),
        migrations.RunPython(set_quantity_field),
    ]
