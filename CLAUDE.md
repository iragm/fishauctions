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
Their **hybrids are in it too**, in a section of their own — tibee, tangtai, mischling, ghost bee,
"steel", stardust, calceo, and the fish the hobby crossed itself (flowerhorn, blood parrot, red
texas, OB and dragon blood peacocks), none of which FishBase has or ever will. A hybrid row is
written with the **first column empty**: a cross between two species has no binomial, so the
trade's name goes in `variety` and the loader sets `is_hybrid`. Filing one under either parent
would put a wrong genus on a label and in a genus BAP rule, so no parentage is kept — see the
hybrid paragraph below. `family` and `order` are the one exception and may be filled in when both
parents share one (every tibee is an atyid, every flowerhorn a cichlid); that is what gives the
row a category, and nothing else reads them. The test for adding one is not "is the
identification settled" — there is nothing to identify — but "does the hobby agree this is a
cross", which is why the mbuna-blooded peacocks are in and "strawberry peacock" is not.

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
`/species/new/`, approved because a person with a shell is not an auction admin; leave the
scientific name blank and it adds a **cross**), `n` remembers "not a species", and a decision
covering several spellings asks before applying to all of them.

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

### Setting it up on a live site

In order, and every step is safe to re-run. `-it` matters on the review pass and nowhere else: it
is the only one that reads stdin. Nothing here needs an LLM key — the backfill runs the matcher
with the model turned off from end to end.

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

Three of those steps are the ones worth stopping at. **`--check-version`** because bumping
`FISHBASE_VERSION` swaps the whole species list and is a deliberate edit to `fishbase.py`, not
something to do while rolling out. **`--only-legacy --dry-run`** because the full import ends by
folding the site's old hand-typed `Product` rows into the imported ones and *moving lots onto
them*; `--keep-legacy` skips that pass entirely. And the **category table** the import prints at
the end (`--only-categories` re-runs just that pass) is the one thing that has to be read rather
than skimmed: it is every hint against the category it found on *this* site, and the interesting
mistake is not a hint that matched nothing but a hint that matched something unexpected.

The backfill is three passes and they go in this order because each one is worth less than the
one before it. `--status` first: it can only be as good as the list, and a run that says the
curated rows are missing is a run whose plant and shrimp lots cannot match anything. Then the
automatic pass, which assigns only where the matcher gives exactly one answer. Then `--review`,
which is a person at a terminal: number to pick, `s` to search the whole list, `a` to add a
species, a strain or a cross inline, `n` for "not a species", enter to skip, `q` to quit — a decision
covering several spellings asks before applying to all of them, and every one of them writes a
`SpeciesSearchCache` row so nothing asks twice. `--include-unmatched` is last because those are
the names with no candidates at all, where the answer is usually a new species.

A **cultivar** ("Blue Dream", "Halfmoon") is a `Species` row with `variety` set and `parent`
pointing at the nominal species, carrying the parent's genus and epithet — so breeder points,
genus BAP rules and the category all still see the plain species. Show `full_scientific_name`,
never `scientific_name`, wherever a human reads it. The one rule that reads the *strain* rather
than the species is `Club.days_between_same_species_lots`, which blocks BAP points for the same
species twice inside a window: it matches on the species row itself, so blue and red cherry shrimp
are two different things to breed and both earn points.

A **hybrid** ("Tibee", "Flowerhorn") is the third shape and the only row on the table with no
scientific name at all: `is_hybrid` set, the trade's name in `variety`, and `genus`, `species` and
`parent` all empty — `Species.save()` enforces that, so a genus put on one by hand is cleared
rather than trusted. It reads as `Hybrid 'Tibee'` everywhere a name is shown, which is the point:
a BAP class that excludes crosses can only do that if the printed label says which lots are
crosses. Deliberately **no parentage is tracked** — a cross has no binomial, and filing it under
one parent would put a wrong genus into a genus BAP rule; what judging needs is *"is this a
hybrid"*, not *"of what"*. It is otherwise an ordinary row: it earns breeder points, it counts once
in `days_between_same_species_lots`, and it is added at `/species/new/` with a checkbox — or
shipped in the curated CSV, which is where the dozen the trade sells by name live.

The flag is a **stored column, not `variety and not parent`**, because `parent__isnull=True`
already means "nominal species" in four places and one column is cheaper than four filters that
have to remember a second meaning. The consequences to keep in mind: no `ClubBapGenusOverride` can
ever match one (it matches on `genus`, which is empty — a club's *Tropheus* rule does not cover a
Tropheus cross, which is right), `species_categories` has only what the CSV's `family`/`order`
columns say (nothing at all for one added on the form, where the person picks the category), and
the **only** route to a hybrid is
`SpeciesCommonName` — nothing reads the `variety` column, so the form writes the strain name into
the name table or the cross would sit on the picker unreachable by typing what it is called.

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

Both species pages honour **`?next=`** for everybody, superusers included. The gaps page is the
fallback and it is superusers only, so an auction admin sent there loses the species and gets a
permission error; whoever wrote the link knows where the person came from, and the permissions they
happen to hold do not.

The commoner fix is not a new species at all, and `/species/name/`
(`SpeciesCommonNameCreateView`) is where it lives: most lot names with no scientific name are one
of FishBase's 36,000 filed under a name nobody says — *Labidochromis caeruleus* is "Blue streak
hap" there and "yellow lab" everywhere else. Until this page existed the only way to add a name was
the Django admin, which auction admins cannot open, so the workflow they were left with was "add a
second *Labidochromis caeruleus*" — which is what fills the duplicate table. Same gate as
`/species/new/`, same scoping (`approved=False` for a non-superuser), and it deliberately does
**not** write to `SpeciesSearchCache`: the name itself is the teaching and it is scoped, where a
cache row is global.

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
or changed on counts a **reject**, on the save that created the lot or on a later save that
actually moved the species. Both counters count **lots, not saves**, which is what stops one
seller editing one lot three times from outvoting everybody.

Retiring the row (`SpeciesSearchCache.is_discredited`) takes **both** one rejection in ten *and* at
least `MIN_REJECTS_TO_RETIRE` (3) of them. The floor is not optional: on a row with no accepts the
ratio is satisfied by the very first rejection, so a single seller clearing a field because *their*
lot is a mixed bag used to throw the answer away for the next hundred people. The first save is the
one most likely to be a misclick, and that is exactly as true of the clearing as of the answer
being cleared. Three lots is disagreement; one is a Tuesday.

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
  documents the palette, text-on-color rules, the six permitted button classes (no
  `btn-outline-*`, no `btn-warning`, and `btn-secondary` only on a Cancel or a Close), close
  buttons, hamburger menus, help notes, pagination, the unavailable-action ("stay clickable") standard, and the message-type
  taxonomy. Never edit vendor CSS; site-wide overrides go in `auctions/static/css/auction_site.css`.
  `docs/style_migration.md` is the worklist of files that don't conform to the button rules yet —
  take a few off it when you're in a template anyway.

## Club announcements and website integration

A club says one thing to its members and picks where it lands: Discord, push notifications to
phones with the app, an email campaign through its own Mailchimp or Brevo, its own website, or any
combination. `auctions/announcements.py` does the delivering and `/clubs/<slug>/announcements/` is
where an admin writes one, behind `permission_send_announcements` — its own permission because one
press reaches every one of those places at once. The Discord channel is set in Discord with
`/announcements_here`, the same shape as `/auctions_here` and deliberately a second channel.

**Every channel carries the whole announcement and nothing else.** It has no page of its own and
nothing links to one: it is a sentence or two by design, so a "read the rest on our website" link
would only lead somewhere that repeats it. Discord is the club name in bold and the text, the push
body is the entire announcement, and tapping the push opens the club's page. The consequence is
that the **only** honest read receipt is the email provider's open count — Discord has none and a
delivered push is not a read one — and no number is invented to stand in for the others. The
website is the one channel that can count something of its own: `website_views` counts **renders**
(the club page here, plus every format of the embed, admins excluded), which is an impression and
is labelled as one — it answers "is the snippet on my site showing this at all", not "did anybody
read it".

Email always goes as a **campaign** addressed to the provider's list, never through this site's
mail server, which is what makes the provider's unsubscribes apply — and it goes out from a Celery
task, since it is four round trips per provider. Nobody types a from address: Mailchimp's audience
`campaign_defaults` and Brevo's verified senders already hold one, and the same read fills in
`Club.donation_mailing_address` when it is blank. **Nobody types a subject either** — it is always
`"<Club> announcement"`, because the box clubs were given got the announcement typed into it a
second time and the inbox showed the same sentence twice. Mailchimp and Brevo are two checkboxes so
the row records which one carried it, but **only one may be ticked**: members are synced to every
connected provider, so a club with both has the same people on both and sending to both would mail
all of them twice. Only the provider a club has connected is offered at all; the other checkbox is
dropped rather than shown disabled, unless neither is connected, where the pair is a menu. The form
opens with **nothing ticked**, the website box included.

**Nothing is delivered in the request.** One with no time on it is scheduled
`announcements.GRACE_SECONDS` (30) out, which is the window in which Retract still means
something — the mistake clubs make is the wrong date in the sentence, and they see it the moment
the page reloads. An explicit schedule is the same path with a longer wait. `sent_at`, not
`scheduled_for`, is the column everything public filters on: the row exists from the moment it is
written, and the club page and the embed must stay blind to it until it has actually gone.
Retracting stops one that hasn't gone, deletes the Discord post and takes it off the website, then
says which channels it could not reach; the send and the retraction each write a `ClubHistory` row
under `ANNOUNCEMENTS`, which for a retraction is the only surviving record.
`docs/club_announcements.md` has the whole design.

Everything a club can put on **its own website** is on one page, `/clubs/<slug>/website/`: the
event calendar, the current auction, the latest announcement and the BAP leaderboard. Snippets are
listed whether or not the feature behind them is switched on, with a note saying so — somebody
choosing what to put on the club website is exactly who should find out that turning BAP on would
give them a leaderboard. The four embeds share one shell (`auctions/templates/auctions/embeds/`)
so their palette can't drift; each has a styled template and an `_unstyled` one, and
`embed_mode_from_request` / `embed_response` in `views.py` are the one reader of `?format=`.

## MCP endpoint and the command palette's skills

The site is a Model Context Protocol server at **`/mcp/`**, so Claude, Claude Code and any other
agent can do the things the command palette can do. There is **one** catalogue behind both: every
capability is an `Action` in `auctions/palette_actions.py`, `auctions/mcp/tools.py` turns the
registry into MCP tool descriptors, and `palette_actions.run_action` is the single dispatcher. A
skill cannot exist for one surface and not the other, and a permission cannot be checked
differently depending on who asked — the resolvers call the same form, view or service the web page
calls, so a lot added by an agent goes through the identical gauntlet as one added by clicking.

**The schema is derived from the prose that was already there.** All 117 parameter descriptions in
the registry open the same way — `"integer, optional, default 1."`, `"string, required. The lot
number."` — because that is what the old system prompt needed in order to be readable. `param_schema`
reads the type and the required flag off that prefix and keeps the whole sentence as the JSON Schema
`description`. There is no second table of types to write, and
`test_mcp.RegistryConformance.test_every_parameter_declares_its_type` fails the build the day
somebody writes one that doesn't fit, because that parameter would silently lose its type.

**Annotations come out of the danger tier**, which the registry has always carried: `safe` reads,
`confirm` writes, `navigate` resolves a URL and never acts. So `readOnlyHint` is
`danger != DANGER_CONFIRM`, and the read/write split a connector review demands is already enforced
by a design that predates it. There is deliberately **no catch-all execute tool** — a single tool
covering both reads and writes is the first thing a review rejects. `destructive=True` is set only
where a write destroys a previous answer (`undo_sale`, `undo_last`), and `idempotent` is derived
(reads are, writes aren't) unless an action that *sets* a value rather than appending one says
otherwise.

Four modules, and only the first knows anything about auctions:

```
auctions/mcp/tools.py      tool_descriptors(user, writes=) / call_tool(request, name, args)
auctions/mcp/protocol.py   JSON-RPC 2.0 + the four MCP methods. Dicts in, dicts out.
auctions/mcp/transport.py  the Django view: methods, headers, status codes, Origin check
auctions/mcp/auth.py       who is calling
```

`tools.py` is the seam and it has two callers: the HTTP endpoint, and the command palette's own
model, in-process with a live `request`. `protocol` and `transport` are hand-written because the
stateless shape MCP allows is small and the auth path is worth owning outright; swapping them for
FastMCP or the MCP Python SDK later is those two files and nothing else, which is why
`auctions/test_mcp.py` is written against the URL rather than against internals.

**The palette is a client of that catalogue, not a second copy of it.** `palette_assist.tools_for`
is `mcp.tools.tool_descriptors(user)` plus exactly two tools of its own — `ask_the_user` and
`cannot_do_this`, which have no MCP equivalent because a host does its own asking and a failing
tool says so through `isError`. `llm.complete(system, messages, tools)` sends them as OpenAI
function definitions (`llm.as_openai_tool` is the one place that translation lives) and the
provider enforces them.

That is what deleted ~200 lines from `palette_assist`. Under JSON mode the model could reply in any
shape, so `parse_reply` had to *repair* the near-misses: a page key in the `action` slot, an auction
title where a lookup name goes, a call written as `{"go_to_page": {…}}`, a lookup named in the
action slot — plus a correction round for everything else. None of those can happen when the name
has to be one of the tools we sent, so `read_reply` is now a short mapping from "which tool" to
"lookup / action / question / refusal", and the correction round is gone: a reply that still gets
here is one the endpoint didn't validate, and asking again does not fix that. `complete_json` stays
for the four callers that want data rather than a call (species matching, donations, the two
speaker commands).

What is *kept* is deliberately palette-only and has no business in the MCP layer: the
`obvious_match` / `shortcut_match` short-circuit that answers "invoice" with no model call at all,
the confirm countdown and its trust window, `humanize`, the `_give_up` fallback ladder,
`sanitize_context` / `_carry_over` conversation memory, the throttles, and the cancel/report
analytics. The countdown is the browser's answer to the same question `destructiveHint` answers for
an MCP host — one danger tier, two audiences.

**Stateless streamable HTTP.** A POSTed request is answered with one `application/json` body (the
spec allows this instead of an SSE stream); a notification gets `202`; `GET` and `DELETE` get `405`,
because no server-initiated stream and no session are offered. A foreign `Origin` is `403`
(DNS-rebinding), an unknown `MCP-Protocol-Version` header is `400`, and a missing one means
`2025-03-26`.

**A session cookie is never a credential, and that is the load-bearing line.** `/mcp/` is a
CSRF-exempt POST that performs writes; if it honoured cookies, any page on the internet could drive
it as whoever was signed in. Two credentials are accepted, both as `Authorization: Bearer`:

* a **`UserAPIKey`** (prefix `ak_`), issued at `/account/api-keys/` and shown once. This is what
  `claude mcp add --transport http … --header "Authorization: Bearer ak_…"` uses, and what the
  `static_headers` beta on claude.ai uses. It shares `HashedAPIKey.generate` / `.verify` with
  `ClubAPIKey`: prefix in the clear, secret as a salted hash, never stored.
* an **OAuth 2.1 access token** from `django-oauth-toolkit`, which is the only thing claude.ai,
  Desktop and mobile can do — they run a real authorization-code flow with PKCE and have nowhere to
  paste a key. Gated on `oauth2_provider` being in `INSTALLED_APPS`, so a deployment that doesn't
  want to be an authorization server simply isn't one and the key path still works.

The authorization server is mounted twice in `fishauctions/urls.py`: its own URLs under `/o/`, and
the discovery documents **again at the domain root**, because RFC 8414 and RFC 9728 put them at the
origin and that is the first place Claude looks. Three settings in `OAUTH2_PROVIDER` are the ones
worth knowing, because each fails silently:

* `"none"` must be in `OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED`. Claude selects CIMD only when
  the metadata advertises *both* `client_id_metadata_document_supported` **and** `none` — its CIMD
  client authenticates as a public client. Leave it out and everything still works, by falling back
  to DCR and registering a fresh client on every connection.
* `DCR_REGISTRATION_PERMISSION_CLASSES` must allow anonymous registration. DCR is the first call a
  client makes, before any user is signed in; the toolkit's default refuses it.
* `ALLOW_LOCALHOST_LOOPBACK`, because Claude Code declares a portless `http://localhost/callback`
  and listens on an ephemeral port. RFC 8252 exempts the `127.0.0.1` literal out of the box; this
  extends it to the `localhost` spelling, which is the one Claude Code actually sends.

`/mcp` is matched with **and without** the trailing slash (`re_path(r"^mcp/?$")`), because
`APPEND_SLASH` cannot rescue a POST — the 301 drops the body — and the RFC 9728 document names the
resource without one. The `WWW-Authenticate` header points at the path-component form
(`/.well-known/oauth-protected-resource/mcp`), not the bare origin: Claude requires the document's
`resource` to match the URL the user typed, and the origin's document describes the whole site.

`allow_writes` (a key) and the `write` scope (a token) are a **ceiling, not a grant**: they can only
ever narrow what their owner may do. Read-only credentials are not offered write tools in
`tools/list` at all, because an agent that spends its turn picking a tool it is about to be refused
has been told the wrong thing.

`ClubAPIKey` is the wrong shape for this and was not reused: it identifies a *club*, and every tool
asks "may **this user** do this to this auction". It also carries one boolean per integration
feature, which is a list that would have to grow with every new tool.

**Adding a URL still costs you two entries.** `/mcp/` and `oauth2_provider:*` are in
`palette_routes.EXCLUDED`; `UserAPIKeyView` is in `palette_actions.NOT_A_SKILL`; `user_api_keys` is a
real `Route` because it is a page a person asks to be taken to.

```bash
docker exec -it django python3 manage.py test auctions.test_mcp
curl -s -X POST http://127.0.0.1/mcp/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}'
# expect 401 + WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"
```

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
