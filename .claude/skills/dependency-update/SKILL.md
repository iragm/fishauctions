---
name: dependency-update
description: The weekly upgrade of every pinned package, in this repository (landed on staging) and in the app repository (its own weekly PR, driven green). Use when running as the dependency routine, or when asked to update packages.
---

# Weekly dependency update

## This repository

1. Branch `claude/deps-<date>` from `origin/staging`. In a cloud session, wait for `logs/.stack-ready`.
2. `./.github/scripts/update-packages.sh --upgrade`, then `./download_vendor_resources.sh`. Never edit
   `requirements.txt` by hand.
3. If nothing changed, delete the branch and stop.
4. Land it exactly as the `staging-flow` skill says. When an upgrade breaks something, fix the code
   for the new version if the fix is small and clear. Otherwise hold that one package back in
   `requirements.in` (`pkg<N`, with a comment saying why and what would lift it), recompile, and say
   so at the end.
5. End with the notable upgrades (majors, anything security-related) and anything held back.

## The app repository (iragm/fishauctions-app)

Its own workflow does the upgrade every Monday at 06:23 UTC: it verifies the result (Dart checks,
Android build, iOS build) and opens or refreshes one PR from `automation/dependency-update` into
`main`. It has no staging branch, and a cloud session can't build iOS, so the PR stays the way in.

1. Find that PR. Subscribe to it.
2. Its body says the tier. `all` with CI green: nothing to do but say it is ready for the owner.
   `safe` or `failed`, or red CI: the PR body has the reverted constraint diff and the failing step;
   fix the app for the new version on that branch (a merge commit, never a force-push), following
   that repository's `CLAUDE.md`, and drive CI green. Its CI is the test: Flutter isn't installed
   in the cloud session.
3. Never merge it; the owner does, because a merge there ends up in a store release.
