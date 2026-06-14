from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0300_add_no_min_bids_to_club"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="club",
            name="membership_number_mode",
        ),
        migrations.AddField(
            model_name="club",
            name="show_member_barcode",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When checked, members receive a 10-digit barcode that can be scanned "
                    "at auctions, added to Google/Apple Wallet, and included in emails."
                ),
                verbose_name="Show member barcodes",
            ),
        ),
    ]
