import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_initial_auction_history(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    AuctionHistory = apps.get_model("auctions", "AuctionHistory")

    histories = [
        AuctionHistory(auction=auction, user=None, applies_to="RULES", action="Enabled admin history")
        for auction in Auction.objects.all()
    ]
    AuctionHistory.objects.bulk_create(histories)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0186_alter_userlabelprefs_preset"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="userlabelprefs",
            name="preset",
            field=models.CharField(
                choices=[
                    ("sm", "Small (Avery 5160) (Not recommended)"),
                    ("lg", "Large (Avery 18262)"),
                    ("thermal_sm", 'Thermal 3"x2"'),
                    ("thermal_very_sm", 'Thermal 1⅛" x 3½" (Dymo 30252)'),
                    ("custom", "Custom"),
                ],
                default="lg",
                max_length=20,
                verbose_name="Label size",
            ),
        ),
        migrations.CreateModel(
            name="AuctionHistory",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(blank=True, max_length=800, null=True)),
                ("timestamp", models.DateTimeField(auto_now_add=True)),
                (
                    "applies_to",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("RULES", "Rules"),
                            ("USERS", "Users"),
                            ("INVOICES", "Invoices"),
                            ("LOTS", "Lots"),
                            ("LOT_WINNERS", "Set lot winners"),
                        ],
                        max_length=100,
                        null=True,
                    ),
                ),
                ("auction", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="auctions.auction")),
                (
                    "user",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.RunPython(create_initial_auction_history),
    ]
