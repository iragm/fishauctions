---
name: celery-tasks
description: Celery beat, task time limits, locks, and why nothing periodic runs over PageView. Use when adding or changing anything in auctions/tasks.py, fishauctions/celery.py, the beat schedule, or a management command that a task calls.
---

# Celery

`fishauctions/celery.py` holds the beat schedule; `auctions/tasks.py` holds the tasks. Global
`CELERY_TASK_SOFT_TIME_LIMIT` is **300s** and `CELERY_TASK_TIME_LIMIT` **600s**, the worker runs at
`--concurrency=2`, and `CELERY_WORKER_PREFETCH_MULTIPLIER=1`. Five rules follow from that, and each
of them is a bug that has already happened here:

- **A task that iterates rows isolates each row.** `_per_item(task, label, items, do_one)` runs the
  whole list, collects failures, and retries the task once at the end. Re-raising on the first bad
  row means everyone before it is redone on every retry and everyone after it is never reached —
  which is how members' wallet passes kept saying "valid" after they lapsed. `_safely` is the same
  idea for a nightly step that has nothing to retry.
- **One job per beat entry.** The nightly membership work used to be five jobs in one task body, so
  the once-a-year award reset sat below a few thousand Discord API calls under a 300-second limit.
  They are `update_expired_membership_discord_roles`, `reset_yearly_bap_counters`,
  `send_club_member_welcome_emails`, `send_membership_expiration_reminders` and
  `backfill_marketing_contacts` now.
- **Never make a once-a-year job depend on running that day.** `reset_yearly_bap_counters` compares
  `Club.bap_ytd_reset_year` to the current year and catches up whenever it next runs; the old
  `if today is January 1` branch lost a whole year's worth of counters if anything above it failed.
  A **null** year is stamped, never zeroed: `exclude(bap_ytd_reset_year=year)` matches nulls
  (`NOT (col = year AND col IS NOT NULL)`), so treating one as overdue wipes the current year's
  points off every club that predates the column, and off every club for its first day. Counters
  that really are stale are repaired by `recalculate_club_bap_points`, which rebuilds them from the
  `BapAward` rows. Both it and the reset read the year off `timezone.localtime()`, because
  `BapAward.date` is a `DateField` somebody typed in their own calendar.
- **Anything on a short interval takes a cache lock.** `endauctions` (60s beat, 300s limit) and
  `sync_club_calendars` each `cache.add` a key with a timeout past the hard limit and delete it in a
  `finally`. Two `endauctions` runs both see a lot as unsold and both invoice it.
- **A self-scheduling task needs a watchdog on the beat.** `update_auction_stats` re-arms itself at
  the end of each run, which a hard-limit SIGKILL never reaches;
  `ensure_auction_stats_task_scheduled` is one indexed lookup every 15 minutes that re-arms it. It
  judges the row by its **scheduled time only** — `enabled` is what beat clears the moment it
  dispatches a one-off, so a disabled row with a recent `clocked_time` is a run in flight, and
  re-arming that starts a second one beside it.

Two more that are not about time limits:

- **`.delay()` goes inside `transaction.on_commit`.** Every call site in `signals.py` does. A
  `post_delete` fires *inside* Django's delete transaction, so enqueuing directly let a rollback
  leave a row pointing at an image already deleted from Cloudflare.
- **`beat_schedule` is reconciled against the database.** `DatabaseScheduler` only writes entries
  *in*; a row that leaves the dict, or one created by hand, keeps being dispatched forever and
  reaches the worker as `NotRegistered`. `FixedDatabaseScheduler.setup_schedule` prunes rows that
  are not in `beat_schedule` (one-off rows and `celery.backend_cleanup` excepted), and
  `test_celery_tasks` fails the build if a beat entry names a task that does not exist.

## PageView is the biggest table on the site

There is no periodic job over it any more. `remove_duplicate_views` ran every 15 minutes merging
repeat views and was deleted: it could only ever reach *anonymous* rows (a signed-in view stores
`session_id=NULL` and the matcher skipped those), it had no time window, and `SESSION_COOKIE_AGE`
is about 230 years -- so one anonymous person's every visit to a page folded into a single row. It
deleted return visits, which is the thing the table is for. The reasoning is in `PageView`'s
docstring; `total_time`, `counter`, `notification_sent` and `duplicate_check_completed` are the
inert columns it left behind.

Nothing purges this table and nothing is meant to. If a query over it is slow, bound the query --
every reader carries a window and an owner, and `auctions/admin_paginator.py` is what keeps the
admin changelist from counting the whole thing twice per load.
