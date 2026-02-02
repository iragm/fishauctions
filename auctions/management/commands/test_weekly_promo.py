"""
Management command to test the weekly_promo task manually.

This command can be used to test the weekly_promo functionality without waiting for
the scheduled time or manually triggering via Celery Beat.
"""

from django.core.management.base import BaseCommand

from auctions.tasks import weekly_promo


class Command(BaseCommand):
    help = "Manually test the weekly_promo Celery task"

    def add_arguments(self, parser):
        parser.add_argument(
            "--celery",
            action="store_true",
            help="Run via Celery (async) instead of directly",
        )

    def handle(self, *args, **options):
        use_celery = options.get("celery", False)

        if use_celery:
            self.stdout.write(self.style.WARNING("Triggering weekly_promo task via Celery..."))
            result = weekly_promo.delay()
            self.stdout.write(self.style.SUCCESS(f"Task queued with ID: {result.id}"))
            self.stdout.write("Check celery_worker logs for task execution:")
            self.stdout.write("  docker logs celery_worker -f | grep -i 'weekly promo'")
        else:
            self.stdout.write(self.style.WARNING("Running weekly_promo task directly (not via Celery)..."))
            weekly_promo()
            self.stdout.write(self.style.SUCCESS("Task completed"))
