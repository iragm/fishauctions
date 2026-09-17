---
name: species-list
description: The species list: FishBase, the curated CSV, matching, categories and the name cache. Use when touching auctions/species_matching.py, species_categories.py, fishbase.py, aquarium_species.py, the Species/SpeciesCommonName/SpeciesSearchCache models, the species pickers on lot forms, or the species-lookup API.
---

# Species list

`Lot.species` comes from `Species`, loaded from:

* a **pinned** FishBase snapshot (`FISHBASE_VERSION` in `auctions/fishbase.py`): ~36k fish. The
  **taxonomy**.
* `auctions/data/aquarium_species.csv`: plants, inverts, live foods, cultivars, hybrids. The
  **vocabulary**. Its header says how to add a row. Names-only rows at the bottom attach hobby names
  to FishBase species. Every importer deletes only names with its own `SpeciesCommonName.source`.

SeaLifeBase is deliberately not imported (see `fishbase.py`).

## Rolling out on a live site

Each step is safe to re-run. `-it` only on `--review`. No LLM key needed.

```bash
docker exec django python3 manage.py backfill_lot_species --status
docker exec django python3 manage.py import_fishbase --check-version        # never bump mid-rollout
docker exec django python3 manage.py import_fishbase --only-legacy --dry-run  # read this
docker exec django python3 manage.py import_fishbase                        # ~1 min; read the category table
docker exec django python3 manage.py backfill_lot_species --dry-run
docker exec django python3 manage.py backfill_lot_species                   # exactly-one matches only
docker exec -it django python3 manage.py backfill_lot_species --review --limit 500
docker exec -it django python3 manage.py backfill_lot_species --review --include-unmatched
```

- `--only-legacy` folds old `Product` rows into imported ones and **moves lots**; `--keep-legacy`
  skips it. `--only-curated`, `--only-categories`, `--purge slb` also exist.
- Backfill writes with `update()` so it never re-derives a category across BAP/HAP/CAP tracks.
  `--set-category` opts in for Uncategorized lots with no `BapAward`.
- Only `--review` writes `SpeciesSearchCache`. A question groups spellings by `group_key` **and**
  candidates. `--scan 0` scans every name.

## Species shapes

- **Cultivar**: `variety` set, `parent` is the nominal species. Show `full_scientific_name`, never
  `scientific_name`.
- **Hybrid**: `is_hybrid`, trade name in `variety`, genus/species/parent empty (`save()` enforces).
  No genus override can match it, and the only route to one is `SpeciesCommonName`.

## Adding species and names

- `/admin-dashboard/species-gaps/` (superusers) lists unmatched lot names. Its rows are
  `source="admin"`, never touched by `--only-legacy`.
- `/species/new/` and `/species/name/` need `UserData.runs_an_auction`. A non-superuser's row is
  unapproved and visible only to its author and its club (`visible_species(user, club)`); approving
  shares it. `/species/name/` never writes the cache.
- **Never pass `club=None` expecting a match**: it means "species with no club".
- Common names are scoped exactly like species. A name already on another visible species is refused.
- Duplicates are flagged in `Species.save()`; a superuser merges with `Species.merge_duplicate`.

## The name cache

Written by three places: bulk add-lot's first save (≤5 suggestions), the auction admin's lot editor,
and the LLM. Read before the token search, so `_is_somebody_elses_name` must keep a club-scoped name
from being served site-wide.

- `record_choice` counts accepts and rejects per **lot**, not per save.
- Retiring needs 1-in-10 rejections **and** `MIN_REJECTS_TO_RETIRE` (3). It writes a
  `SpeciesNameRejection` that vetoes the pair for `remember()` and the LLM shortlist only.

## Matching, categories and display

- `species_matching.py`: exact, token/phrase, then LLM. "No match" beats a plausible one.
- A single word answers only if it names ≤5 species and isn't part of >40 other names.
- Club API `/api/v1/clubs/<slug>/species-lookup/` (`can_look_up_species`). LLM budget is
  `SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY` per club; over it is a 429. Only an `answered` result
  reaches `remember()`.
- `Species.trade_rank` orders suggestions; the genus tier needs `recompute_trade_ranks()`.
- `species_categories.py` maps onto existing categories, never creates one. Hints are fine-grained;
  `HINT_FALLBACKS` only coarsens. `CICHLID_REGIONS` goes by genus.
- Both lot-form pickers start closed. Quick-add has no picker: one match goes in, else blank.
- Pickers show only the scientific name; the LLM alone gets `label_with_common_name`.
- On a lot, show the name the seller didn't type (`scientific_name_line` / `common_name_line`).
