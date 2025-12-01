import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Trigger the auction stats update task. The task is self-scheduling and will continue to run automatically."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run the task synchronously (directly) instead of via Celery",
        )

    def handle(self, *args, **options):
        from auctions.tasks import schedule_auction_stats_update, update_auction_stats

        if options["sync"]:
            # Run the task directly (useful for testing or when Celery is not available)
            self.stdout.write("Running auction stats update synchronously...")
            update_auction_stats()
            self.stdout.write(self.style.SUCCESS("Auction stats update completed."))
        else:
            # Trigger the Celery task
            self.stdout.write("Triggering auction stats update task...")
            schedule_auction_stats_update()
            self.stdout.write(self.style.SUCCESS("Auction stats update task has been scheduled."))
