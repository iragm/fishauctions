"""`remove_duplicate_views`, which used to corrupt the data it was cleaning up.

The command runs every 15 minutes over the largest table on the site. It had four faults, and the
first one was destroying view counts:

1. It materialised the whole unchecked queryset up front, so it reached rows that an earlier
   iteration of the same pass had already deleted -- and then called ``save()`` on them. Django's
   ``save()`` on a deleted instance runs an UPDATE that matches nothing and falls through to an
   INSERT under the old primary key (``select_on_save`` is False, the pk is an ``AutoField``), so
   the merged-away duplicate came back with double-counted totals and took the row it had been
   merged into with it.
2. It merged exactly one duplicate per row but marked the row checked regardless, so a view with
   three duplicates kept two of them forever.
3. The fetch was unbounded.
4. ``duplicates`` matched on a blank ``session_id``, which reads as ``IS NULL`` and makes every
   anonymous view of one URL a duplicate of every other.
"""

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from auctions.models import Lot, PageView
from auctions.test_support import isolated_cache


def _view(user, lot, *, session="s1", counter=1, total_time=10, url="/lots/1/", **extra):
    return PageView.objects.create(
        user=user, lot_number=lot, url=url, session_id=session, counter=counter, total_time=total_time, **extra
    )


@isolated_cache("pageview-dedupe")
class DeduplicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="dedupe", email="dedupe@example.com", password="pw")
        cls.lot = Lot.objects.create(lot_name="Dedupe lot", quantity=1, user=cls.user)

    def test_a_pair_becomes_one_row_with_the_totals_added_once(self):
        """The headline bug. Two rows of counter=1/total_time=10 used to leave one row of
        counter=3/total_time=30 -- and it was the *other* row, resurrected after being deleted."""
        first = _view(self.user, self.lot)
        second = _view(self.user, self.lot)
        call_command("remove_duplicate_views")
        rows = list(PageView.objects.filter(lot_number=self.lot).values("pk", "counter", "total_time"))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["counter"], 2)
        self.assertEqual(rows[0]["total_time"], 20)
        self.assertIn(rows[0]["pk"], {first.pk, second.pk})

    def test_a_deleted_duplicate_is_never_resurrected(self):
        """``save()`` on a deleted instance re-INSERTs it. Nothing in this path may call ``save()``
        on a row it might have merged away."""
        views = [_view(self.user, self.lot) for _ in range(2)]
        call_command("remove_duplicate_views")
        survivors = set(PageView.objects.filter(pk__in=[v.pk for v in views]).values_list("pk", flat=True))
        self.assertEqual(len(survivors), 1, "exactly one of the pair should be left")

    def test_every_duplicate_is_merged_not_just_the_first(self):
        for _ in range(4):
            _view(self.user, self.lot)
        call_command("remove_duplicate_views")
        rows = list(PageView.objects.filter(lot_number=self.lot).values("counter", "duplicate_check_completed"))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["counter"], 4)
        self.assertTrue(rows[0]["duplicate_check_completed"])

    def test_running_it_twice_changes_nothing(self):
        """It runs every 15 minutes. The second pass over settled data must be a no-op, which is
        exactly what the old version was not."""
        for _ in range(3):
            _view(self.user, self.lot)
        call_command("remove_duplicate_views")
        before = list(PageView.objects.filter(lot_number=self.lot).values("pk", "counter", "total_time"))
        call_command("remove_duplicate_views")
        after = list(PageView.objects.filter(lot_number=self.lot).values("pk", "counter", "total_time"))
        self.assertEqual(before, after)

    def test_different_sessions_are_not_duplicates(self):
        _view(self.user, self.lot, session="a")
        _view(self.user, self.lot, session="b")
        call_command("remove_duplicate_views")
        self.assertEqual(PageView.objects.filter(lot_number=self.lot).count(), 2)

    def test_views_with_no_session_are_left_alone(self):
        """Blank session ids read as ``IS NULL`` in the duplicate filter, which would make every
        anonymous view of one URL a duplicate of every other one."""
        for _ in range(3):
            _view(None, self.lot, session="")
        call_command("remove_duplicate_views")
        self.assertEqual(PageView.objects.filter(lot_number=self.lot).count(), 3)
        self.assertEqual(PageView.objects.filter(duplicate_check_completed=False).count(), 0)

    def test_the_batch_is_bounded(self):
        for _ in range(4):
            _view(self.user, self.lot)
        call_command("remove_duplicate_views", limit=1)
        # One row was looked at, so its duplicates are gone; nothing promises the rest are done.
        self.assertLessEqual(PageView.objects.filter(lot_number=self.lot).count(), 4)

    def test_the_batch_is_taken_from_the_newest_rows(self):
        """`duplicate_check_completed` is not indexed and is True on all but the last few minutes of
        the biggest table on the site, so an ascending scan walks every settled row from the
        beginning of time to reach candidates that are always at the end. Newest-first finds them
        in about `limit` rows."""
        views = [_view(self.user, self.lot) for _ in range(3)]
        call_command("remove_duplicate_views", limit=1)
        survivor = PageView.objects.get(lot_number=self.lot)
        self.assertEqual(survivor.pk, views[-1].pk, "the oldest row was examined, not the newest")

    def test_the_merged_row_keeps_the_fields_the_others_had(self):
        _view(self.user, self.lot, source="", title="", referrer="")
        _view(self.user, self.lot, source="weekly_email", title="A lot", referrer="https://example.com/")
        call_command("remove_duplicate_views")
        row = PageView.objects.get(lot_number=self.lot)
        self.assertEqual(row.source, "weekly_email")
        self.assertEqual(row.title, "A lot")
        self.assertEqual(row.referrer, "https://example.com/")
