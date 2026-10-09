---
name: staging-flow
description: How finished work reaches master (through the staging branch and one rolling PR), and who fixes a red CI run. Use before merging anything into staging, after any push, or when a CI run fails.
---

# Staging, master, and red CI

`staging` is where reviewed, tested work lands; the owner tests it on staging.auction.fish and merges
the one open **staging → master** PR. Nothing here pushes to `master` directly, and no failed-CI email
should ever be the way a failure is noticed: the agent that pushed fixes it.

## Landing work on staging

1. Branch from `origin/staging`, build, review with the `code-review` skill, commit.
   In a cloud session the test stack comes from `.claude/hooks/cloud-stack.sh`, which only starts
   on its own when the session opens in the repository. If `logs/.stack-ready` is missing and
   `logs/.stack-up.log` isn't growing, run `CLAUDE_CODE_REMOTE=true bash .claude/hooks/cloud-stack.sh`
   from the repository and wait for the marker.
2. Run `docker compose run --rm test --ci --verbose`, the touched modules' tests, then the full suite
   (`docker exec django python3 manage.py test --parallel --noinput`). Never two suites at once.
3. `git fetch origin staging`; merge it in if it moved and rerun the tests. Then
   `git push origin HEAD:staging` and delete the branch, local and remote.
4. Check `git merge-tree --write-tree origin/master origin/staging`. On a conflict, merge `master`
   into `staging`, resolve it, rerun the tests and push; if both sides changed the same logic, stop
   and tell the owner.
5. Make sure a PR from `staging` into `master` is open (create it if not, titled `Staging`), and
   subscribe to it. One rolling PR: never a second one.

A server-error fix is the exception: it branches from `master` and gets its own PR into `master`, so
it can ship without what is waiting on staging. Merge the same branch into `staging` too.

## Who fixes a red run

CI runs on every push, PR or not, so **whoever pushes owns that commit's runs**:

- **A PR you opened:** `subscribe_pr_activity` and drive it green.
- **A push with no PR** (`staging`, a work branch): before ending the turn, read the runs for that
  commit (`actions_list` on the repo, filtered to the branch). If they are still going, schedule a
  look with `send_later` in about 20 minutes. Red is yours to fix, with the same tests first.
- **Nobody's** (a scheduled run, a session that ended, a base branch broken by a merge): the hourly
  health check picks it up, once, by claiming it first.

### Claiming an orphan

Claims live in the project's shared folder, which every session in the project sees:

```bash
mkdir /mnt/project-files/.claims/ci/<repo>-<run_id>   # fails if someone already claimed it
```

Only `mkdir` succeeding makes it yours; then write a line into `owner` in that directory saying
which session and what you are doing. Skip a run that is already claimed, whose branch has an open
PR (its session owns it), or that a newer green run on the same branch has superseded. Remove the
claim once the fix is green.

## Routine state

Scheduled runs share one long-lived session, so nothing a run needs may live only in the
conversation: it gets summarized away. Each routine keeps what it must remember in
`/mnt/project-files/routines/<routine>.md`: read it first, act on it rather than on recollection,
and rewrite it at the end (replace, never append; keep it under about 40 lines). Typical contents:
problems already reported and when, errors with a fix in flight and the PR, the last run's key
numbers.
