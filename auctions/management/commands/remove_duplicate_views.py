import logging

from django.core.management.base import BaseCommand

from auctions.models import PageView

logger = logging.getLogger(__name__)

#: How many candidate rows one run will look at.
#
# This used to be the whole of ``PageView.objects.filter(duplicate_check_completed=False)``,
# materialised into memory before the loop started -- an unbounded fetch of the largest table on the
# site, which is a worker-sized problem the first time a backlog builds up. The task runs every 15
# minutes, so a bounded batch drains a backlog steadily instead of trying to do it all at once.
BATCH_SIZE = 5000


class Command(BaseCommand):
    help = "Duplicate pageviews appear when the user views the same page twice in rapid succession; this will merge the duplicate views"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=BATCH_SIZE,
            help=f"How many unchecked page views to look at (default {BATCH_SIZE}, 0 for all of them)",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        # Primary keys, not model instances. The old loop held instances fetched before any merging
        # happened, so it reached rows that an earlier iteration had already deleted and merged them
        # a second time -- see PageView.merge_and_delete_duplicates for what that cost. Re-fetching
        # each row by pk means a row that is gone is skipped instead.
        #
        # Newest first, and that is not a preference. `duplicate_check_completed` is not indexed and
        # is True on all but the last few minutes of the largest table on the site, so the default
        # ascending scan walks every settled row from the beginning of time before reaching a single
        # candidate -- a full table scan every 15 minutes to find rows that are always at the end.
        # Walking the primary key backwards finds the batch in about `limit` rows. It also means an
        # unbounded backlog would be drained newest-first; indexing the column is the fix if that
        # ever stops being hypothetical.
        pks = PageView.objects.filter(duplicate_check_completed=False).order_by("-pk").values_list("pk", flat=True)
        if limit:
            pks = pks[:limit]
        merged = 0
        for pk in list(pks):
            view = PageView.objects.filter(pk=pk).first()
            if view is None:
                # Already folded into another row on this same pass.
                continue
            try:
                merged += view.merge_and_delete_duplicates()
            except Exception:
                logger.exception("remove_duplicate_views failed for view %s", pk)
                continue
        if merged:
            logger.info("remove_duplicate_views merged %s duplicate page view(s)", merged)
