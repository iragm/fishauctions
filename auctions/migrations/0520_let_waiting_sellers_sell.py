"""Intentionally empty. It once let everyone sell in auctions that had turned "only approved sellers" off;
that is now the admin's call, from a box on the rules form (Auction.let_waiting_sellers_sell). Kept so a
database that already recorded 0520 still lines up."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0519_mcpcall_admin"),
    ]

    operations = []
