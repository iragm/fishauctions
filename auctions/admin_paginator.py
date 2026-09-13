"""Paginate the admin's biggest changelists without counting the whole table.

Django's changelist asks the paginator for ``count`` on every load, and asks the model admin for a
second, unfiltered ``root_queryset.count()`` on top of it unless ``show_full_result_count`` is off
(``django.contrib.admin.views.main.ChangeList.get_results``).  On ``PageView`` -- the largest table
on this site -- that is two full index scans to render twenty rows, and it gets slower every month
whether or not anybody is adding page views faster than before.

Two things fix it, and both belong on the ``ModelAdmin``::

    show_full_result_count = False
    paginator = EstimatedCountPaginator

The estimate is only ever used for a queryset with **no** ``WHERE`` clause, which is the only case
that is expensive: a filtered or searched changelist is bounded by its own filter and still gets a
real ``COUNT``.  Unfiltered, "how many page views are there" has no exact answer worth two seconds,
and InnoDB already keeps a row estimate in ``information_schema``.  It can be off by a good margin
in either direction -- that is what an estimate is -- so the only thing riding on it is how many
page links are drawn.  If the engine has no estimate to give (a fresh table, a backend that is not
MySQL/MariaDB, a permission problem reading ``information_schema``), this falls back to the real
count rather than reporting zero rows.
"""

from django.core.paginator import Paginator
from django.db import connection
from django.utils.functional import cached_property


class EstimatedCountPaginator(Paginator):
    """A ``Paginator`` that trades an exact unfiltered count for a cheap one."""

    def _table_row_estimate(self):
        """The engine's own guess at the row count, or 0 when it has none to give."""
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_ROWS FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                    [self.object_list.model._meta.db_table],
                )
                row = cursor.fetchone()
        except Exception:
            return 0
        return int(row[0]) if row and row[0] else 0

    @cached_property
    def count(self):
        query = getattr(self.object_list, "query", None)
        if query is None or query.where:
            return super().count
        return self._table_row_estimate() or super().count
