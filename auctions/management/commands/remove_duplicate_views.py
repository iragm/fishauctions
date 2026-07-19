import logging

from django.core.management.base import BaseCommand

from auctions.models import PageView

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Duplicate pageviews appear when the user views the same page twice in rapid succession; this will merge the duplicate views"

    def handle(self, *args, **options):
        views = PageView.objects.filter(duplicate_check_completed=False)
        for view in views:
            try:
                view.merge_and_delete_duplicate()
                view.duplicate_check_completed = True
                view.save()
            except Exception:
                logger.exception("remove_duplicate_views failed for view %s", view.pk)
                continue
