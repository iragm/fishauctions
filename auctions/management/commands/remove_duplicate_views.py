from django.core.management.base import BaseCommand

from auctions.models import PageView


class Command(BaseCommand):
    help = "Duplicate pageviews appear when the user views the same page twice in rapid succession; this will merge the duplicate views"

    def handle(self, *args, **options):
        views = PageView.objects.filter(duplicate_check_completed=False)
        for view in views:
            view.merge_and_delete_duplicate()
            view.duplicate_check_completed = True
            view.save()
