# Rework Square/PayPal <-> Club integration:
# - drop Club.payment_user / Club.allow_integrated_payments
# - add Club.use_site_paypal_account
# - add PayPalSeller.club / SquareSeller.club (one seller per club)
# - migrate existing data so previously linked sellers/superuser-clubs keep working.

import django.db.models.deletion
from django.db import migrations, models


def link_sellers_to_clubs(apps, schema_editor):
    """For each Club that previously had a payment_user, attach the user's seller
    to the club via the new FK. Superuser payment_users with allow_integrated_payments
    are migrated to use_site_paypal_account=True.
    """
    Club = apps.get_model("auctions", "Club")
    PayPalSeller = apps.get_model("auctions", "PayPalSeller")
    SquareSeller = apps.get_model("auctions", "SquareSeller")

    for club in Club.objects.filter(payment_user_id__isnull=False, allow_integrated_payments=True):
        payment_user = club.payment_user
        if payment_user.is_superuser:
            club.use_site_paypal_account = True
            club.save(update_fields=["use_site_paypal_account"])

        paypal_seller = PayPalSeller.objects.filter(user=payment_user, club__isnull=True).first()
        if paypal_seller:
            paypal_seller.club = club
            paypal_seller.save(update_fields=["club"])

        square_seller = SquareSeller.objects.filter(user=payment_user, club__isnull=True).first()
        if square_seller:
            square_seller.club = club
            square_seller.save(update_fields=["club"])


def unlink_sellers(apps, schema_editor):
    PayPalSeller = apps.get_model("auctions", "PayPalSeller")
    SquareSeller = apps.get_model("auctions", "SquareSeller")
    PayPalSeller.objects.update(club=None)
    SquareSeller.objects.update(club=None)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0284_clubmoney_source_auction"),
    ]

    operations = [
        # 1. Add new fields up front so the data migration can populate them.
        migrations.AddField(
            model_name="club",
            name="use_site_paypal_account",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When checked, PayPal payments for this club use the site's own "
                    "merchant account (PAYPAL_CLIENT_ID / PAYPAL_SECRET from settings) "
                    "instead of any linked PayPalSeller. Only useful for site admins; "
                    "Square has no platform-account equivalent."
                ),
                verbose_name="Use site PayPal account",
            ),
        ),
        migrations.AddField(
            model_name="paypalseller",
            name="club",
            field=models.OneToOneField(
                blank=True,
                help_text="If set, this PayPal account is the one used for the club's payments.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="paypal_seller",
                to="auctions.club",
            ),
        ),
        migrations.AddField(
            model_name="squareseller",
            name="club",
            field=models.OneToOneField(
                blank=True,
                help_text="If set, this Square account is the one used for the club's payments.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="square_seller",
                to="auctions.club",
            ),
        ),
        # 2. Data migration: copy payment_user -> seller.club / use_site_paypal_account.
        migrations.RunPython(link_sellers_to_clubs, unlink_sellers),
        # 3. Drop the old fields.
        migrations.RemoveField(
            model_name="club",
            name="payment_user",
        ),
        migrations.RemoveField(
            model_name="club",
            name="allow_integrated_payments",
        ),
    ]
