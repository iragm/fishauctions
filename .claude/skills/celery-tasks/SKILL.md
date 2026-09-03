---
name: celery-tasks
description: Celery beat, task time limits, locks and the PageView dedupe job. Use when adding or changing anything in auctions/tasks.py, fishauctions/celery.py, the beat schedule, or a management command that a task calls.
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
- **Anything on a short interval takes a cache lock.** `endauctions` (60s beat, 300s limit),
  `sync_club_calendars` and `compute_user_flow_all` each `cache.add` a key with a timeout past the
  hard limit and delete it in a `finally`. Two `endauctions` runs both see a lot as unsold and both
  invoice it; two `compute_user_flow_all` runs occupy both worker slots and stop everything else.
- **A self-scheduling task needs a watchdog on the beat.** `update_auction_stats` re-arms itself at
  the end of each run, which a hard-limit SIGKILL never reaches;
  `ensure_auction_stats_task_scheduled` is one indexed lookup every 15 minutes that re-arms it. It
  judges the row by its **scheduled time only** — `enabled` is what beat clears the moment it
  dispatches a one-off, so a disabled row with a recent `clocked_time` is a run in flight, and
  re-arming that starts a second one beside it.
- **A lock on a task with no time limit is a heartbeat, not a ceiling.** `compute_user_flow_all`
  re-stamps `USER_FLOW_LOCK_KEY` after every auction, so the lock outlives a run of any length and a
  worker killed mid-run wedges the admin button for 30 minutes rather than for however long the
  longest imaginable run is. The view asks the same lock so the page says which of the two happened.

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

`remove_duplicate_views` runs every 15 minutes over it and had four faults at once. What the fixed
version guarantees, all of it enforced by `auctions/test_page_view_dedupe.py`:

- It iterates **primary keys and re-fetches**, bounded to `BATCH_SIZE`, **newest first**. The old
  loop materialised the whole unchecked queryset and so reached rows an earlier iteration had
  already deleted. `duplicate_check_completed` is unindexed and true on all but the last few
  minutes of the table, so the default ascending scan walked every settled row from the beginning
  of time to reach candidates that are always at the end; `order_by("-pk")` finds them in about
  `BATCH_SIZE` rows. Indexing the column is the fix if a backlog ever becomes real.
- **Nothing calls `save()` on a row it may have merged away.** `PageView._meta.select_on_save` is
  False and the pk is an `AutoField`, so `save()` on a deleted instance runs an UPDATE that matches
  nothing and Django **re-INSERTs it** under its old pk — resurrecting the duplicate with
  double-counted totals and deleting the row it had just been merged into.
  `merge_and_delete_duplicates` writes with `update()` for that reason.
- It merges **every** duplicate, not `duplicates.first()`, in one transaction, and marks the
  survivor itself rather than trusting the caller.
- A blank `session_id` is **not** a match. `filter(session_id="")` would make every anonymous view
  of one URL a duplicate of every other.
