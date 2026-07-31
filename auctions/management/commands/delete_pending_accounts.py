"""Delete accounts whose deletion grace period has expired.

Run daily by Celery beat (``auctions.tasks.delete_pending_accounts``). See
:mod:`auctions.account_deletion` for what deletion means and what it deliberately keeps.
"""

from django.core.management.base import BaseCommand

from auctions.account_deletion import GRACE_PERIOD_DAYS, process_due_deletions


class Command(BaseCommand):
    help = f"Delete accounts that asked to be deleted more than {GRACE_PERIOD_DAYS} days ago."

    def handle(self, *args, **options):
        count = process_due_deletions()
        if count:
            self.stdout.write(self.style.SUCCESS(f"Deleted {count} account(s)."))
