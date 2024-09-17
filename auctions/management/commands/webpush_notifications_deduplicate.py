from django.core.management.base import BaseCommand
from django.db.models import Count, Min
from webpush.models import SubscriptionInfo


class Command(BaseCommand):
    help = "Duplicate subscriptions can be created, this removes them.  See https://github.com/safwanrahman/django-webpush/issues/135"

    def handle(self, *args, **options):
        # Step 1: Annotate each WebpushSubscriptionInfo with the minimum id for each group of auth and p256dh.
        duplicates = (
            SubscriptionInfo.objects.values("auth", "p256dh")
            .annotate(min_id=Min("id"), count_id=Count("id"))
            .filter(count_id__gt=1)
        )
        # Step 2: Collect the IDs of the entries with the lowest id in each group.
        min_ids_to_delete = [entry["min_id"] for entry in duplicates]
        # Step 3: Delete these entries.
        SubscriptionInfo.objects.filter(id__in=min_ids_to_delete).delete()
