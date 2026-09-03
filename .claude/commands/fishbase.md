---
description: Import or refresh the species list from FishBase and the curated CSV
argument-hint: "[--check-version | --only-curated | --only-categories | --dry-run]"
allowed-tools: Bash(docker exec *django python3 manage.py import_fishbase*), Bash(docker exec *django python3 manage.py backfill_lot_species*), Read
---

```
docker exec django python3 manage.py import_fishbase $ARGUMENTS
```

Order for a real rollout, every step safe to re-run:

```
docker exec django python3 manage.py backfill_lot_species --status       # before
docker exec django python3 manage.py import_fishbase --check-version
docker exec django python3 manage.py import_fishbase --only-legacy --dry-run
docker exec django python3 manage.py import_fishbase --dry-run
docker exec django python3 manage.py import_fishbase                     # ~36k species, ~1 min
docker exec django python3 manage.py backfill_lot_species --status       # after
docker exec django python3 manage.py backfill_lot_species --dry-run
docker exec django python3 manage.py backfill_lot_species                # the certain ones
docker exec -it django python3 manage.py backfill_lot_species --review --limit 500
```

Three places to stop and read rather than carry on:

- **`--check-version`** tells you the pin is stale. Bumping `FISHBASE_VERSION` in
  `auctions/fishbase.py` swaps the entire species list; that is a deliberate edit, not a step.
- **`--only-legacy --dry-run`** shows what the full import will fold the site's old hand-typed rows
  into, *and it moves lots onto them*. `--keep-legacy` skips that pass.
- **The category table printed at the end.** Read it. The interesting mistake is a hint that
  matched something unexpected. `--only-categories` re-runs just that pass.

`-it` matters only on `--review`, which is the one that reads stdin -- and it is therefore the one
step here that cannot be run from an agent or a script at all: `docker exec -t` fails outright
without a terminal. Everything else is plain `docker exec`. None of this needs an LLM key.
