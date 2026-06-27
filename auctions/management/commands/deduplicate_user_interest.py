from django.core.management.base import BaseCommand
from django.db.models import Count

from auctions.models import UserInterestCategory


class Command(BaseCommand):
    help = (
        "Collapse duplicate UserInterestCategory rows (same user + category) into one. "
        "There is no unique constraint on (user, category) on purpose, so a race between "
        "concurrent requests (e.g. a double-clicked bid) can create a second row; this "
        "merges them, summing the interest so no signal is lost."
    )

    def handle(self, *args, **options):
        # Find (user, category) pairs that have more than one row.
        duplicate_groups = (
            UserInterestCategory.objects.values("user", "category")
            .annotate(count_id=Count("id"))
            .filter(count_id__gt=1)
        )
        for group in duplicate_groups:
            rows = list(
                UserInterestCategory.objects.filter(user=group["user"], category=group["category"]).order_by("id")
            )
            keeper = rows[0]
            keeper.interest = sum(row.interest for row in rows)
            keeper.save()  # save() re-normalizes as_percent
            UserInterestCategory.objects.filter(id__in=[row.id for row in rows[1:]]).delete()
