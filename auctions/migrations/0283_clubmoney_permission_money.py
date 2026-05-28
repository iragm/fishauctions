import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_club_money(apps, schema_editor):
    # Fetch invoice IDs using the historical (apps-frozen) Invoice model so the SELECT
    # only references columns that exist at this migration's point in history. Then load
    # each invoice via the live model (deferring any fields added in later migrations)
    # so we can call ``create_club_payment_history`` on it.
    Invoice_historical = apps.get_model("auctions", "Invoice")
    invoice_ids = list(
        Invoice_historical.objects.filter(
            models.Q(auction__club__isnull=False)
            | models.Q(auction__isnull=True, auctiontos_user__auction__club__isnull=False)
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    if not invoice_ids:
        return

    from auctions.models import Club, Invoice

    # Defer fields added after this migration so the SELECT does not reference columns
    # that don't yet exist when the migration runs on a fresh database.
    historical_club = apps.get_model("auctions", "Club")
    deferred_club_fields = [
        f.name
        for f in Club._meta.get_fields()
        if getattr(f, "concrete", False)
        and f.name not in {hf.name for hf in historical_club._meta.get_fields() if getattr(hf, "concrete", False)}
    ]
    deferred_lookups = [f"auction__club__{name}" for name in deferred_club_fields] + [
        f"auctiontos_user__auction__club__{name}" for name in deferred_club_fields
    ]

    queryset = Invoice.objects.filter(pk__in=invoice_ids).select_related(
        "auction", "auction__club", "auctiontos_user", "auctiontos_user__auction", "auctiontos_user__auction__club"
    )
    if deferred_lookups:
        queryset = queryset.defer(*deferred_lookups)

    for invoice in queryset.iterator(chunk_size=200):
        invoice.create_club_payment_history(force_current_state=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0282_recalculate_all_auction_stats"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="clubmember",
            name="permission_money",
            field=models.BooleanField(
                default=False,
                help_text="Manage membership/payment settings and view the treasurer's report.",
            ),
        ),
        migrations.AlterField(
            model_name="clubmember",
            name="permission_edit_club",
            field=models.BooleanField(
                default=False,
                help_text="Change club setup, Discord, and API keys.  Nearly as dangerous as admin.",
            ),
        ),
        migrations.CreateModel(
            name="ClubMoney",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(db_index=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("description", models.CharField(blank=True, default="", max_length=500)),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("donation", "Donation"),
                            ("speaker_costs", "Speaker costs"),
                            ("meeting_location_cost", "Meeting location cost"),
                            ("membership", "Membership"),
                            ("auction_profit", "Auction profit"),
                            ("auction_seller_payout", "Auction seller payout"),
                            ("unpaid_invoices", "Unpaid invoices"),
                            ("refunds", "Refunds"),
                            ("adjustment", "Adjustment"),
                        ],
                        max_length=40,
                    ),
                ),
                ("createdon", models.DateTimeField(auto_now_add=True)),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="money", to="auctions.club"
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="club_money",
                        to="auctions.invoice",
                    ),
                ),
            ],
            options={
                "ordering": ["-date", "-pk"],
                "verbose_name_plural": "Club money",
            },
        ),
        migrations.RunPython(backfill_club_money, migrations.RunPython.noop),
    ]
