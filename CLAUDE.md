# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Django 5.x auction platform with Python 3.11.9, Bootstrap 5, jQuery, HTMx. MariaDB, Redis, Nginx, Uvicorn/Gunicorn, Celery, Docker Compose. Main app: `auctions/` (~5k lines models.py, ~8k lines views.py, ~3k lines forms.py).

## Development Setup

```bash
cp .env.example .env && sed -i '1,4d' .env  # Remove first 4 lines (production config)
mkdir -p logs && chmod -R 777 logs
docker compose --profile "*" build          # 5-10 min first time
docker compose up -d
```

Access at `http://127.0.0.1` (port **80**, not 8000).

Create superuser:
```bash
docker exec -it django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
```

## Testing & Linting

```bash
docker compose run --rm test --ci --verbose   # Run before every commit (format + lint + tests)
docker compose run --rm test --format         # Auto-fix formatting
docker compose run --rm test --lint           # Auto-fix linting
docker exec -it django python3 manage.py test # Run Django tests (requires compose up)
```

Ruff config: `ruff.toml` (line-length: 120). Replicate CI locally: `./.github/scripts/prepare-ci.sh && docker compose run --rm test --ci --verbose`

## Django Commands

Always run inside the container:
```bash
docker exec -it django python3 manage.py makemigrations
docker exec -it django python3 manage.py migrate
docker exec -it django python3 manage.py shell
```

Migration permission error? Use `docker exec -u root -it django ...`

## Species list

`Lot.species` is picked from `Species`, which comes from two places:

* a **pinned** FishBase snapshot (`FISHBASE_VERSION` in `auctions/fishbase.py`) — ~36k fish, with
  family/order and FishBase's aquarium-trade rating
* `auctions/data/aquarium_species.csv`, a curated list of plants, freshwater invertebrates, live
  foods and **cultivars** — everything FishBase has never heard of. Edit the CSV, re-run the
  import. Its header states the rule for adding a row.

The trade is where the CSV's plant and shrimp rows come from, because that is who names them:
Tropica's catalogue for plant cultivars ("Rosanervig", "Monte Carlo", "H'ra"), Florida Aquatic
Nurseries for the American common names, and The Garden of Eder's stock list for shrimp strains.
Their hybrids are deliberately **left out** — tibee, tangtai, mischling, ghost bee, "steel" — because
a cross between two species has no binomial, and filing it under one of its parents would put a
wrong genus on a label and in a genus BAP rule.

FishBase is the **taxonomy** and the CSV is the **vocabulary**. FishBase is an ichthyology
database: it is authoritative about which species exist and has no reason to know that
*Labidochromis caeruleus* is a "yellow lab" (it files it under "Blue streak hap"). So the bottom
of the CSV holds **names-only rows** — scientific name and common names, everything else blank —
which attach names to a species another list owns without cloning it or touching its taxonomy.
`SpeciesCommonName.source` is what makes that safe in both directions: every importer deletes only
the names it wrote, so a hobby name survives the next `FISHBASE_VERSION` bump, and FishBase's
49,000 names survive the CSV.

SeaLifeBase is **deliberately not imported** (102k mostly-marine species, and no cherry shrimp);
the code is kept and `--databases fb,slb` still works. See the comment in `auctions/fishbase.py`.

```bash
docker exec django python3 manage.py import_fishbase --check-version   # is the pin stale?
docker exec django python3 manage.py import_fishbase                    # ~36k species, ~1 min
docker exec django python3 manage.py import_fishbase --only-curated     # just the CSV, no download
docker exec django python3 manage.py import_fishbase --only-categories  # re-map family -> Category
docker exec django python3 manage.py import_fishbase --only-legacy --dry-run  # pre-import leftovers
docker exec django python3 manage.py import_fishbase --purge slb        # drop an unused source
```

Historical lots — everything sold before the list existed — are filled in by
`backfill_lot_species`, in three passes. `--status` first: the backfill can only be as good as the
list it matches against, and the plants, shrimp and cultures are the half of it that comes from the
CSV rather than FishBase. Then the automatic pass, which runs the same matcher the add-lot form
does with the LLM turned off and assigns only where it gives exactly one answer. Then `--review`,
which puts the names that match *several* species in front of a person, commonest first, and turns
each answer into one write plus one `SpeciesSearchCache` row so nothing asks again.

A review question is a **group of spellings**, keyed on the lot name's words minus the stop words
(`group_key`) *and* on the candidates the matcher found. Both halves matter: the key is what makes
"6 male guppies" and "guppies (pair)" one question, and the candidates are what keep "blue dream
shrimp" and "green dream shrimp" apart — the colours are stop words, so the key alone would merge
two different cultivars. `a` adds a species without leaving the review (the command-line half of
`/species/new/`, approved because a person with a shell is not an auction admin), `n` remembers
"not a species", and a decision covering several spellings asks before applying to all of them.

The writing pass uses `update()` so it can never re-derive a lot's category and move it between the
BAP, HAP and Culture tracks; `--set-category` opts into that for Uncategorized lots with no
`BapAward`. Only the review pass writes to the name cache — an automatic answer came out of the
species list, which is where the next lookup would find it anyway, and a cache row is read *before*
the token search. Start with `--dry-run`.

```bash
docker exec django python3 manage.py backfill_lot_species --status      # step 0: what the list covers
docker exec django python3 manage.py backfill_lot_species --dry-run     # print, write nothing
docker exec django python3 manage.py backfill_lot_species --auction my-auction --limit 200
docker exec django python3 manage.py backfill_lot_species --set-category
docker exec django python3 manage.py backfill_lot_species --review --dry-run --limit 500  # the list
docker exec django python3 manage.py backfill_lot_species --review --limit 500            # sit down with it
docker exec django python3 manage.py backfill_lot_species --review --include-unmatched     # + the missing species
```

`--review` runs the matcher over the commonest `--scan` names (5000 by default) before it asks
anything, so a full-site pass over tens of thousands of names is opt-in (`--scan 0`) rather than
the thing that happens while you wait.

```bash
```

A **cultivar** ("Blue Dream", "Halfmoon") is a `Species` row with `variety` set and `parent`
pointing at the nominal species, carrying the parent's genus and epithet — so breeder points,
genus BAP rules and the category all still see the plain species. Show `full_scientific_name`,
never `scientific_name`, wherever a human reads it. The one rule that reads the *strain* rather
than the species is `Club.days_between_same_species_lots`, which blocks BAP points for the same
species twice inside a window: it matches on the species row itself, so blue and red cherry shrimp
are two different things to breed and both earn points.

Adding a species is an **admin workflow, not a database edit**: `/admin-dashboard/species-gaps/`
lists the lot names that keep showing up with no species (the sibling of the command palette's
bounce list), and each row links to `/species/new/` with the name prefilled — fill in two boxes
and every lot with that name gets the species, plus the matcher learns the name. Rows added there
are `source="admin"`, which `import_fishbase --only-legacy` deliberately never touches.

Because that page is open to every auction admin, the same species gets added twice: somebody
searches for "crypt", finds nothing, and adds a *Cryptocoryne wendtii* the list has had all along.
`Species.save()` flags those the way `AuctionTOS.save()` flags a duplicate member — same scientific
name at the same rank, or the same **designated** common name (not the synonym table: FishBase gives
"Peppered cory" to two different *Corydoras* on purpose, and flagging every shared synonym would
bury the real duplicates). The two big importers are skipped, since a duplicate inside a list that
numbers its own species is impossible and the scan would cost two queries times 36,000. The pair is
listed on the gaps page with lot counts and sources side by side, and a **superuser** either merges
it — `Species.merge_duplicate` moves the lots, the strains, the hobby names and the remembered lot
names onto the row that survives — or says "not a duplicate", because two species really can share
a common name.

`/species/new/` is open to **anyone who runs an auction** (`UserData.runs_an_auction`), because
adding a species is a check-in-table job and a workflow that ends in "email the site owner" ends
in no scientific name. What a non-superuser adds is `approved=False`, and
`species_matching.visible_species(user, club)` shows it to **the author or the club** — the author
always, anyone at `Species.club`, and any caller working in that club's context (the club API, a
lot in one of its auctions). `remember()` refuses to put it in the global name cache, and
`/species/new/` only attaches it to lots in auctions that person administers. Approving it — the
button on the gaps page, or the bulk action in the Django admin — is what makes it everyone's and
teaches the matcher the lot names it is already on.

`Species.club` is filled in only when there is an obvious one (`UserData.only_club`: single-club
mode, or the user belongs to exactly one) and is often blank, which is why it can never be the
*only* route — plenty of auctions have no club at all. Pass `club=` to `visible_species` /
`suggest_species` wherever a view already has one; where it doesn't, leave it out. **Never pass
`club=None` expecting it to match** — it is guarded precisely because it would read as "every
species with no club", which is every unapproved species on the site.

The name cache is written by three places, and only three: the bulk add-lot page on a row's first
save (bounded — the seller can only pick from the ≤5 suggestions the matcher produced), the
auction admin's lot editor (unbounded, because that form has the search-every-species box, but the
person is an auction admin correcting a lot on purpose), and the language model.
`SpeciesSearchCache.created_by` records who, because every row is served to every club.

Because sellers write it, it has to be able to be **wrong and recover**, and that is what
`species_matching.record_choice` is for: every lot save reports what happened to the answer this
name was remembered as. A lot saved with it left alone counts an **accept**, once, on the save that
created the lot — re-saving a lot to fix its price is not a second vote. A lot it was cleared from
or changed on counts a **reject**, on any save. One rejection in ten retires the row
(`SpeciesSearchCache.is_discredited`), so a fresh row nobody has agreed with yet is retired by the
first person who disagrees — the first save is the one most likely to have been a misclick — and a
row nine people have left alone survives one.

Retiring writes a `SpeciesNameRejection`, and *that* is the part that matters: deleting the cache
row on its own would send the next lookup back to the same language model with the same shortlist,
which would give the same answer and write the same row back. A rejection vetoes a **pair**, and
only in the two places that make things up — `remember()` refuses to learn it again, and it is
filtered out of the model's shortlist. It never touches `exact_matches` or the token search: those
answer out of the species list itself, and a handful of people clearing a field must not be able to
outvote the list. "Allow it again" on the gaps page is the way back.

A club's own software reaches all of this through `/api/v1/clubs/<slug>/species-lookup/`, behind
the single `can_look_up_species` key permission: `GET` matches typed text (≤5 results,
`total_matches` for the rest, `?category=` by name or `?category_id=` by pk), `POST` on the same
URL adds a species, and `POST .../<id or scientific name>/common-names/` names one that is already
there. One permission covers the writes because of what they cannot do — they only ever create,
and what they create is scoped to the club until a site admin approves it.

**A common name is scoped exactly like a species**: `SpeciesCommonName.approved` / `added_by` /
`club`, and `visible_common_names()` asks for *both* the species and the name to be visible. It
has to be, because a name is read ahead of everything else the matcher does — "yellow lab" is
answered out of that table — so an unscoped one would let a club teach every other club a name for
the wrong fish. Everything the importers and the CSV write is `approved=True`, which is the
default, so FishBase's 49,000 names need no migration. Approving a species approves the names that
came in with it (`SpeciesApproveView` and the admin action, in step); a name added to a species
that is *already* shared has no species approval to ride on, and its queue is the
`SpeciesCommonName` page in the Django admin. A name that already names a different visible
species is refused (`species_carrying_common_name`) — one name on two species is the loss of a
name, not the gain of one.

The language model runs on **every** club-API lookup the database could not answer — that is what
the endpoint is for — bounded by `SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY` (1000, per club, not
per key) through `species_matching.LLMBudget`. A budget is spent *inside* `llm_match`, at the
moment of the call, so the free steps cost nothing; `X-Species-LLM-Limit` / `-Remaining` /
`-Reset` ride on every response, and a lookup that needed the model with nothing left is a 429
rather than a fabricated "no species". `llm_match` returns `(species, answered)` and only
`answered` reaches `remember()` — a name the model never actually saw (no provider, no budget,
a failed call) must never be cached as "not a species" for the whole site.

`Species.trade_rank` (0 = in the hobby, 1 = its genus is, 2 = neither) is what suggestions are
ordered by. FishBase's own `Aquarium` column is not enough on its own — it files *Chindongo
saulosi* under "never/rarely" — so the genus gets a say and `in_trade_override` lets a person
overrule it. `Species.save()` maintains the species tier; the genus tier needs
`Species.recompute_trade_ranks()`, which the importer runs.

`Species.category` is derived from genus/family/order by `auctions/species_categories.py`, which
maps onto whatever categories a site's admins have actually created (it never creates one). A lot
with a species takes that category.

The hints are the **fine** ones — `corydoras`, `plecos`, `catfish`; `cichlids malawi`, `cichlids
rift`, `cichlids south america` — because that is how a fish club's category list is actually
split, and `HINT_FALLBACKS` walks from a fine hint to the coarser one that is still true (a cory is
a catfish, a Malawi fish is a rift fish). Never the other way: the old generic `catfish` hint
listed "Corydoras" among its spellings, so on this site every pleco and every synodontis was filed
as a Corydoras. Names are matched with punctuation and case ignored, and then again on the set of
words, so "Cichlids - Rift Lake" and "Rift Lake Cichlids" are one name. The cichlids need
`CICHLID_REGIONS` because all 1,790 of them are Cichlidae, so only the genus can say where a fish
comes from; a genus that isn't listed gets no category rather than one of four guesses.
`import_fishbase --only-categories` prints the whole hint → category table for the site it is run
on, which is the way to check a club's list.

**Both pickers on the lot form start closed.** The seller types a lot name and gets one line of
text under it — the scientific name when the matcher identified one, the category when it didn't —
with a Change button that opens the real controls (`refreshSpeciesUI` in `lot_form.html`). A name
matching *several* species is the one thing that opens the picker on its own. The **category box is
the fallback for the scientific name, not a companion to it**: once a species is picked it is not on
screen at all, because `Species.category` already answers it and the server derives it on save
anyway; it comes back the moment the species goes away, which is the case it exists for (a sponge
filter, a mixed bag), and there the old keyword guesser fills it in. The quick-add pages have no
picker at all (`configure_species_field(..., picker=False)`): a single match goes into a
hidden input and is shown as text with a clear button, anything less certain is left blank, and
correcting one is a job for the full editor. FishBase's citation lives behind the `?` next to the
name on the lot page, and is on no form.

The **auction admin's lot editor** is the opposite of all that: one dal picker over every species
including the strains (`configure_species_field(..., dal_for=user)`), nothing guessed from the lot
name, and a "New species" button beside it that opens `/species/new/` in a new tab with the lot name
prefilled. Somebody is on that form *because* a lot has the wrong species or none, so a guess is
what they came to overrule.

A picker only ever shows the **scientific name** — `Species.label` is `full_scientific_name` and
nothing else. The bracketed common name it used to carry was noise: the field is called "scientific
name" and it sits under a lot name the seller has already written in plain English. The one reader
that still gets both is the language model, through `Species.label_with_common_name`, because a
shortlist row is being matched against a typed lot name and the common name is the half that
resembles it.

Where a **lot** is displayed, the rule is "show the name the seller didn't type":
`Lot.scientific_name_line` is the italic line under the lot name and goes blank when the lot name
already contains the binomial (`Lot.lot_name_says_the_species`), and `Lot.common_name_line` fills in
instead. The lot page, the printed label, the AR overlay and the lot map all read that pair;
`Lot.scientific_name` stays as it was and is what the CSV exports and the API report.

Matching a typed lot name to a species lives in `auctions/species_matching.py` (exact, then
token/phrase search, then the LLM, with every answer cached in `SpeciesSearchCache`). Its rules are
deliberately strict — a wrong species ends up on a printed label and in breeder points, so "no
match" is a better answer than a plausible one. See `auctions/test_species.py`.

When a **synonym is carried by several species** and nothing stronger matched, the tie-break is the
*designated* name (`_named_after_the_same_thing`): FishBase lists "Peppered cory" for both
*Corydoras paleatus* and *C. julii*, and only one of them is called "Peppered corydoras". Two
candidates is not an answer — the quick-add pages fill nothing in unless there is exactly one — so
that rule is the difference between the commonest cory in the hobby being reachable by the name
everybody types and not.

Where a bound is needed there, it is **read off the data rather than kept as a list of words**. A
single word of a lot name ("male guppy", "L046 pleco") answers only when it names ≤5 species, the
hobby keeps what it names — or the name is ours rather than FishBase's — and it is not a component
of more than 40 other names. That last one is what separates a fish from a kind of fish: "guppy"
is used inside 16 other names and answers, "barb" 218 and "catfish" 480 and they do not. A
whitelist would need maintaining and would always be behind the hobby.

## Dependencies

Never edit `requirements.txt` directly. Edit `requirements.in` or `requirements-test.in`, then:
```bash
./.github/scripts/update-packages.sh           # Add new packages
./.github/scripts/update-packages.sh --upgrade # Upgrade all
```

## Architecture

```
auctions/            # Main app: models, views, forms, templates, static, migrations (180+)
  management/commands/  # Cron jobs: endauctions, sendnotifications, email_invoice, etc.
  tests.py             # Extend StandardTestCase for test setup (users, auctions, lots)
fishauctions/        # Project settings (reads .env), ASGI, URLs, Celery config
docker-compose.yaml  # Services: web (Django), db (MariaDB), redis, nginx, celery-worker, celery-beat, test
Dockerfile           # Multi-stage: builder → test → dev → final
entrypoint.sh        # Auto-runs migrate, collectstatic on start; uvicorn (dev) / gunicorn (prod)
ruff.toml            # Linting/format config
```

**Key models:** User/UserData, Auction, Lot, Bid, Invoice, AuctionTOS, PickupLocation, Category, Species, ChatMessage, PageView

**URLs:** `auctions/urls.py` and `fishauctions/urls.py`

## Frontend / Templates

- **Template tags must open and close on one line.** Django's lexer has no `re.DOTALL`, so a
  `{# … #}`, `{% … %}`, or `{{ … }}` split across two lines is not an error — Django doesn't
  recognize it and renders it onto the page as text for users to read. Use
  `{% comment %} … {% endcomment %}` for any note longer than one line. Enforced by
  `auctions/template_lint.py` (a pre-commit hook, part of `--ci`/`--lint`, and
  `auctions/test_template_hygiene.py`).

- Read `style_reference.md` before making any frontend/template/CSS change. It
  documents the palette, text-on-color rules, outline-button and pagination
  fixes, the unavailable-action ("stay clickable") standard, and the message-type
  taxonomy. Never edit vendor CSS; site-wide overrides go in `auctions/static/css/auction_site.css`.

## Model Changes

- Always create migrations after model changes (`makemigrations` then `migrate`).
- When adding fields to `Auction`, check if they belong in `fields_to_clone` in `AuctionCreateView`.

## Common Issues

| Problem | Fix |
|---|---|
| Won't start | First 4 lines of `.env` not removed |
| Port 80 in use | Add `HTTP_PORT=81` to `.env` |
| Migration permission error | Use `docker exec -u root -it django ...` |
| Static files missing | `docker exec -it django python3 manage.py collectstatic --no-input` |
| DB out of sync | `docker exec -it django python3 manage.py migrate` |
| Build fails | `docker compose down && docker system prune -a -f && docker compose --profile "*" build --no-cache` |
