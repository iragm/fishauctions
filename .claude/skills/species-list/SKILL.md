---
name: species-list
description: The species list: FishBase, the curated CSV, matching, categories and the name cache. Use when touching auctions/species_matching.py, species_categories.py, fishbase.py, aquarium_species.py, the Species/SpeciesCommonName/SpeciesSearchCache models, the species pickers on lot forms, or the species-lookup API.
---

# Species list

`Lot.species` is picked from `Species`, loaded from two sources:

* a **pinned** FishBase snapshot (`FISHBASE_VERSION` in `auctions/fishbase.py`) — ~36k fish, with
  family/order and FishBase's aquarium-trade rating.
* `auctions/data/aquarium_species.csv` — curated plants, freshwater invertebrates, live foods,
  **cultivars** and **hybrids**. Edit the CSV, re-run the import. Its header states the rule for
  adding a row.

FishBase is the **taxonomy**; the CSV is the **vocabulary**. The bottom of the CSV holds
**names-only rows** (scientific name + common names, everything else blank) that attach hobby names
to a species FishBase owns without cloning it. `SpeciesCommonName.source` makes that safe both ways:
every importer deletes only the names it wrote.

A hybrid row in the CSV has an **empty first column**; the trade's name goes in `variety` and the
loader sets `is_hybrid`. `family`/`order` may be filled in when both parents share one. No parentage
is tracked.

SeaLifeBase is **deliberately not imported**; the code is kept and `--databases fb,slb` still works.
See the comment in `auctions/fishbase.py`.

```bash
docker exec django python3 manage.py import_fishbase --check-version   # is the pin stale?
docker exec django python3 manage.py import_fishbase                    # ~36k species, ~1 min
docker exec django python3 manage.py import_fishbase --only-curated     # just the CSV, no download
docker exec django python3 manage.py import_fishbase --only-categories  # re-map family -> Category
docker exec django python3 manage.py import_fishbase --only-legacy --dry-run  # pre-import leftovers
docker exec django python3 manage.py import_fishbase --purge slb        # drop an unused source
```

Historical lots are filled in by `backfill_lot_species`, in three passes: `--status`, then the
automatic pass (the add-lot matcher with the LLM off; assigns only on exactly one answer), then
`--review` (a person at a terminal). Start with `--dry-run`.

- The writing pass uses `update()` so it can never re-derive a lot's category and move it between
  the BAP, HAP and Culture tracks. `--set-category` opts into that for Uncategorized lots with no
  `BapAward`.
- Only the review pass writes to `SpeciesSearchCache`.
- Review keys: number to pick, `s` search the whole list, `a` add a species/strain/cross inline
  (blank scientific name adds a cross), `n` "not a species", enter skip, `q` quit. A decision
  covering several spellings asks before applying to all of them.
- A review question is a group of spellings keyed on `group_key` (lot-name words minus stop words)
  **and** on the candidates found — both halves matter.
- `--review` scans the commonest `--scan` names (5000 default) before asking; `--scan 0` for the
  whole site.

```bash
docker exec django python3 manage.py backfill_lot_species --status      # step 0: what the list covers
docker exec django python3 manage.py backfill_lot_species --dry-run     # print, write nothing
docker exec django python3 manage.py backfill_lot_species --auction my-auction --limit 200
docker exec django python3 manage.py backfill_lot_species --set-category
docker exec django python3 manage.py backfill_lot_species --review --dry-run --limit 500
docker exec django python3 manage.py backfill_lot_species --review --limit 500
docker exec django python3 manage.py backfill_lot_species --review --include-unmatched
```

## Setting it up on a live site

In order; every step is safe to re-run. `-it` matters on the review pass and nowhere else (it is the
only one that reads stdin). Nothing here needs an LLM key.

```bash
docker exec django python3 manage.py showmigrations auctions | tail -3   # 0395 applied?
docker exec django python3 manage.py backfill_lot_species --status       # before: what's there
docker exec django python3 manage.py import_fishbase --check-version     # don't bump mid-rollout
docker exec django python3 manage.py import_fishbase --only-legacy --dry-run  # what will be folded in
docker exec django python3 manage.py import_fishbase --dry-run           # ~20 MB down, parse, write nothing
docker exec django python3 manage.py import_fishbase                     # the real thing, ~1 min
docker exec django python3 manage.py backfill_lot_species --status       # after: what can now match
docker exec django python3 manage.py backfill_lot_species --dry-run
docker exec django python3 manage.py backfill_lot_species                # the certain ones
docker exec -it django python3 manage.py backfill_lot_species --review --dry-run --limit 500
docker exec -it django python3 manage.py backfill_lot_species --review --limit 500
docker exec -it django python3 manage.py backfill_lot_species --review --include-unmatched
```

Three steps to stop at: **`--check-version`** (bumping `FISHBASE_VERSION` swaps the whole species
list; it is a deliberate edit to `fishbase.py`, not a rollout step); **`--only-legacy --dry-run`**
(the full import ends by folding the site's old hand-typed `Product` rows into the imported ones
*and moving lots onto them*; `--keep-legacy` skips that pass); and the **category table** the import
prints at the end (`--only-categories` re-runs just that pass) — read it, the interesting mistake is
a hint that matched something unexpected.

## Species shapes

- **Cultivar** ("Blue Dream", "Halfmoon"): `variety` set, `parent` pointing at the nominal species,
  carrying the parent's genus and epithet. Show `full_scientific_name`, never `scientific_name`,
  wherever a human reads it. `Club.days_between_same_species_lots` matches on the species row
  itself, so blue and red cherry shrimp are two different things to breed.
- **Hybrid** ("Tibee", "Flowerhorn"): `is_hybrid` set, trade name in `variety`, `genus`, `species`
  and `parent` all empty — `Species.save()` enforces it. Reads as `Hybrid 'Tibee'`. Consequences:
  no `ClubBapGenusOverride` can match one (it matches on `genus`), `species_categories` has only
  what the CSV's `family`/`order` say, and the **only** route to a hybrid is `SpeciesCommonName` —
  nothing reads `variety`, so the form writes the strain name into the name table.

## Adding species and names

- `/admin-dashboard/species-gaps/` (superusers only) lists lot names with no species; each row links
  to `/species/new/` prefilled. Rows added there are `source="admin"`, which
  `import_fishbase --only-legacy` never touches.
- `/species/new/` is open to anyone with `UserData.runs_an_auction`. A non-superuser's row is
  `approved=False`; `species_matching.visible_species(user, club)` shows it to the author, anyone at
  `Species.club`, and any caller in that club's context. `remember()` refuses to put it in the
  global name cache, and the page only attaches it to lots in auctions that person administers.
  Approving (gaps-page button, or the Django admin bulk action) makes it everyone's.
- `/species/name/` (`SpeciesCommonNameCreateView`) is the commoner fix — a hobby name on a species
  that is already there. Same gate, same scoping, and it deliberately does **not** write to
  `SpeciesSearchCache`.
- Both species pages honour `?next=` for everybody, superusers included.
- Duplicates: `Species.save()` flags the same scientific name at the same rank, or the same
  **designated** common name (not the synonym table). The two big importers are skipped. A
  superuser merges (`Species.merge_duplicate` moves lots, strains, hobby names and remembered lot
  names) or marks "not a duplicate".
- `Species.club` is filled in only via `UserData.only_club` and is often blank. Pass `club=` to
  `visible_species` / `suggest_species` wherever a view already has one. **Never pass `club=None`
  expecting it to match** — that would read as "every species with no club".
- A **common name is scoped exactly like a species** (`SpeciesCommonName.approved` / `added_by` /
  `club`); `visible_common_names()` requires both the species and the name to be visible. Importers
  and the CSV write `approved=True`. Approving a species approves the names that came in with it
  (`SpeciesApproveView` and the admin action); a name added to an already-shared species is queued
  in the `SpeciesCommonName` Django admin page. A name that already names a different visible
  species is refused (`species_carrying_common_name`).

## The name cache

Written by exactly three places: the bulk add-lot page on a row's first save (bounded to the ≤5
suggestions), the auction admin's lot editor (unbounded), and the language model.
`SpeciesSearchCache.created_by` records who; every row is served to every club **except** one
whose text is a common name somebody added here and scoped —
`species_matching._is_somebody_elses_name`. The cache is read before the token search and answers
on its own, so without that check getting the model asked once was all it took to hand one club's
word for a fish to the whole site, permanently, past a name table that refuses to.

- `species_matching.record_choice`: a lot saved with the answer left alone counts an **accept**,
  once, on the save that created the lot; one cleared or changed counts a **reject**. Both count
  **lots, not saves**.
- Retiring (`SpeciesSearchCache.is_discredited`) takes **both** one rejection in ten *and* at least
  `MIN_REJECTS_TO_RETIRE` (3) rejections.
- Retiring writes a `SpeciesNameRejection`, which vetoes a **pair** in the two places that make
  things up: `remember()` refuses to learn it again, and it is filtered out of the model's
  shortlist. It never touches `exact_matches` or the token search. "Allow it again" on the gaps
  page is the way back.

## Matching, categories and display

- Matching lives in `auctions/species_matching.py` (exact, then token/phrase search, then the LLM,
  every answer cached in `SpeciesSearchCache`). Rules are deliberately strict: "no match" beats a
  plausible one. See `auctions/test_species.py`.
- Synonym tie-break when several species carry one name: the *designated* name
  (`_named_after_the_same_thing`).
- A single word of a lot name answers only when it names ≤5 species, the hobby keeps what it names
  (or the name is ours rather than FishBase's), and it is not a component of more than 40 other
  names.
- The club API is `/api/v1/clubs/<slug>/species-lookup/`, behind the single `can_look_up_species`
  key permission: `GET` matches typed text (≤5 results, `total_matches` for the rest, `?category=`
  by name or `?category_id=` by pk), `POST` on the same URL adds a species, and
  `POST .../<id or scientific name>/common-names/` names one that is already there.
- The LLM runs on every club-API lookup the database could not answer, bounded by
  `SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY` (1000, per club, not per key) through
  `species_matching.LLMBudget`. Budget is spent inside `llm_match`; `X-Species-LLM-Limit` /
  `-Remaining` / `-Reset` ride on every response; out of budget is a 429. `llm_match` returns
  `(species, answered)` and only `answered` reaches `remember()`.
- `Species.trade_rank` (0 = in the hobby, 1 = its genus is, 2 = neither) orders suggestions;
  `in_trade_override` overrules it. `Species.save()` maintains the species tier; the genus tier
  needs `Species.recompute_trade_ranks()`, which the importer runs.
- `Species.category` is derived from genus/family/order by `auctions/species_categories.py`, which
  maps onto categories a site's admins have created and never creates one. A lot with a species
  takes that category.
- Hints are the **fine** ones (`corydoras`, `plecos`, `cichlids malawi`); `HINT_FALLBACKS` walks
  from a fine hint to the coarser one that is still true, never the other way. Names match with
  punctuation and case ignored, then again on the set of words. `CICHLID_REGIONS` decides region by
  genus; an unlisted genus gets no category. `import_fishbase --only-categories` prints the whole
  hint → category table for the site it runs on.
- **Both pickers on the lot form start closed** (`refreshSpeciesUI` in `lot_form.html`): one line of
  text plus a Change button. A name matching several species opens the picker on its own. The
  category box is the **fallback** for the scientific name, not a companion — once a species is
  picked it is off screen. Quick-add pages have no picker
  (`configure_species_field(..., picker=False)`): a single match goes into a hidden input, anything
  less certain is left blank.
- The auction admin's lot editor uses one dal picker over every species including strains
  (`configure_species_field(..., dal_for=user)`) and guesses nothing from the lot name.
- A picker only ever shows the scientific name (`Species.label` is `full_scientific_name`). The one
  reader that gets both is the LLM, through `Species.label_with_common_name`.
- On a lot, show the name the seller didn't type: `Lot.scientific_name_line` blanks when
  `Lot.lot_name_says_the_species`, and `Lot.common_name_line` fills in instead. `Lot.scientific_name`
  is what the CSV exports and the API report use.
