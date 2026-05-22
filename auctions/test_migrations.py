from importlib import import_module

from django.apps import apps as django_apps
from django.test import TestCase
from django.utils import timezone

from auctions.models import Auction


class EnableCategoryFieldForAllAuctionsMigrationTests(TestCase):
    def test_migration_enables_category_field_for_existing_auctions(self):
        auction = Auction.objects.create(
            title="Auction with disabled categories",
            date_start=timezone.now(),
            date_end=timezone.now(),
            use_categories=False,
        )

        migration = import_module("auctions.migrations.0270_enable_category_field_for_all_auctions")
        migration.enable_category_field_for_all_auctions(django_apps, None)

        auction.refresh_from_db()
        self.assertTrue(auction.use_categories)
