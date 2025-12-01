"""
Management command to load demo data for development environments.
This command loads a fixture with sample auctions, lots, bids, and users
when DEBUG=True and no auctions exist in the database.
"""

import sys

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from auctions.models import Auction


class Command(BaseCommand):
    help = "Load demo data fixture when in DEBUG mode and database is empty"

    def handle(self, *args, **options):
        # Only run in DEBUG mode
        if not settings.DEBUG:
            self.stdout.write(
                self.style.WARNING("Skipping demo data load - DEBUG=False (production mode)")
            )
            return

        # Check if any auctions already exist
        if Auction.objects.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"Skipping demo data load - {Auction.objects.count()} auction(s) already exist in database"
                )
            )
            return

        # Load the demo data fixture
        self.stdout.write("=" * 80)
        self.stdout.write(self.style.SUCCESS("Loading demo data because DEBUG=True and no auctions exist..."))
        self.stdout.write("=" * 80)
        self.stdout.write("")
        self.stdout.write("This will create:")
        self.stdout.write("  - 3 demo auctions (in-person, active online, and ended online)")
        self.stdout.write("  - Multiple pickup locations including mail shipping")
        self.stdout.write("  - Demo users (admin, sellers, bidders)")
        self.stdout.write("  - Sample lots in various states")
        self.stdout.write("  - Sample bids and auction participation")
        self.stdout.write("")

        try:
            # Load the fixture
            call_command("loaddata", "demo_data.json", verbosity=2, stdout=self.stdout, stderr=sys.stderr)

            self.stdout.write("")
            self.stdout.write("=" * 80)
            self.stdout.write(self.style.SUCCESS("Demo data loaded successfully!"))
            self.stdout.write("=" * 80)
            self.stdout.write("")
            self.stdout.write("Demo accounts created:")
            self.stdout.write("  - demo_admin (admin user)")
            self.stdout.write("  - demo_seller1, demo_seller2 (sellers)")
            self.stdout.write("  - demo_bidder1 (bidder)")
            self.stdout.write("")
            self.stdout.write("Demo auctions created:")
            self.stdout.write("  1. Demo In-Person Auction - Spring 2024")
            self.stdout.write("  2. Demo Online Auction - Active Now! (ends Dec 25, 2025)")
            self.stdout.write("  3. Demo Ended Auction - Fall 2024 (already ended)")
            self.stdout.write("")
            self.stdout.write("You can update this demo data by editing:")
            self.stdout.write("  auctions/fixtures/demo_data.json")
            self.stdout.write("")

        except Exception as e:
            self.stdout.write("")
            self.stdout.write("=" * 80)
            self.stdout.write(self.style.ERROR(f"Error loading demo data: {e}"))
            self.stdout.write("=" * 80)
            self.stdout.write("")
            self.stdout.write("Possible causes:")
            self.stdout.write("  - Fixture file not found (check auctions/fixtures/demo_data.json exists)")
            self.stdout.write("  - Invalid JSON in fixture file")
            self.stdout.write("  - Database constraint violations")
            self.stdout.write("  - Missing required model fields")
            self.stdout.write("")
            raise
