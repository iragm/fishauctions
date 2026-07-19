import datetime
import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from auctions.models import AuctionTOS
from auctions.signals import link_unattached_tos_for_user

logger = logging.getLogger(__name__)

# The email-change guard in AuctionTOS.save() that used to clear the user link was added on
# 2026-05-17 (commit 1ded2cc). Only records created on or after that date could have been
# orphaned by it, so we scope the repair to that window.
CUTOFF = datetime.datetime(2026, 5, 17, tzinfo=datetime.timezone.utc)


class Command(BaseCommand):
    help = (
        "One-off repair: relink AuctionTOS rows that were orphaned (user=None) by the email-change "
        "guard when a user joined an auction through the UI. Matches each orphaned record to an active "
        "user by email and links it, merging duplicates in the same auction."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without modifying the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        orphans = AuctionTOS.objects.filter(
            user__isnull=True,
            manually_added=False,
            email__isnull=False,
            createdon__gte=CUTOFF,
        )
        total = orphans.count()

        # Resolve each orphaned email to an active user, mirroring the creation-time matching in
        # AuctionTOS.save() (User.objects.filter(is_active=True, email=self.email)).
        users_by_email = {}
        for email in set(orphans.values_list("email", flat=True)):
            if not email:
                continue
            user = User.objects.filter(is_active=True, email=email).first()
            if user:
                users_by_email[email] = user

        relinkable = [tos for tos in orphans if tos.email in users_by_email]

        if dry_run:
            for tos in relinkable:
                user = users_by_email[tos.email]
                self.stdout.write(
                    f"Would relink AuctionTOS {tos.pk} ({tos.email}) -> user {user.pk} in auction {tos.auction}"
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"[dry-run] {len(relinkable)} of {total} orphaned AuctionTOS records would be relinked"
                )
            )
            return

        processed_users = set()
        for tos in relinkable:
            user = users_by_email[tos.email]
            if user.pk in processed_users:
                # link_unattached_tos_for_user handles all of a user's orphaned rows at once,
                # so we only need to call it once per user.
                continue
            try:
                link_unattached_tos_for_user(user, reason="orphaned by email-change guard")
                processed_users.add(user.pk)
            except Exception:
                logger.exception("relink_auctiontos_users failed for user %s", user.pk)
                continue

        remaining = AuctionTOS.objects.filter(
            user__isnull=True,
            manually_added=False,
            email__isnull=False,
            createdon__gte=CUTOFF,
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Relinked orphaned AuctionTOS records for {len(processed_users)} users "
                f"({total - remaining} of {total} orphaned records addressed; {remaining} remain unmatched)"
            )
        )
