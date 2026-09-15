---
name: celery-tasks
description: Celery beat, task time limits, locks, and why nothing periodic runs over PageView. Use when adding or changing anything in auctions/tasks.py, fishauctions/celery.py, the beat schedule, or a management command that a task calls.
---

# Celery

Beat schedule in `fishauctions/celery.py`, tasks in `auctions/tasks.py`. Soft limit 300s, hard 600s,
concurrency 2, prefetch 1. Each rule below is a bug that already happened.

- **Isolate each row.** `_per_item(task, label, items, do_one)` collects failures and retries once at
  the end. `_safely` for a nightly step with nothing to retry.
- **One job per beat entry.** Don't stack a yearly reset under thousands of API calls.
- **Yearly jobs catch up, never "if today is Jan 1".** `reset_yearly_bap_counters` compares
  `Club.bap_ytd_reset_year`. A null year is stamped, never zeroed — `exclude(year=…)` matches nulls.
  `recalculate_club_bap_points` rebuilds from `BapAward`.
- **Short intervals take a cache lock** (`endauctions`, `sync_club_calendars`): `cache.add`, timeout
  past the hard limit, delete in `finally`. Two `endauctions` would double-invoice.
- **Self-scheduling needs a watchdog.** `ensure_auction_stats_task_scheduled` judges by
  `clocked_time` only; beat clears `enabled` on dispatch.
- **`.delay()` inside `transaction.on_commit`.**
- **Beat is reconciled against the DB.** `FixedDatabaseScheduler.setup_schedule` prunes rows not in
  `beat_schedule`. `test_celery_tasks` fails on a beat entry naming a missing task.

## PageView

No periodic job runs over it and nothing purges it. `remove_duplicate_views` was deleted: it merged
return visits away. If a query is slow, bound the query. `admin_paginator.py` keeps the admin from
counting it twice.
