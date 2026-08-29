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
docker compose run --rm test --ci --verbose   # Run before every commit -- format + lint ONLY, no tests
docker compose run --rm test --format         # Auto-fix formatting
docker compose run --rm test --lint           # Auto-fix linting
docker exec -it django python3 manage.py test # Run Django tests (requires compose up)
```

Ruff config: `ruff.toml` (line-length: 120). Replicate CI locally: `./.github/scripts/prepare-ci.sh && docker compose run --rm test --ci --verbose` -- note `prepare-ci.sh` overwrites `.env`.

`--ci` does not run the tests: run `manage.py test` separately. The full suite is ~55 minutes, so
background it, and never run two at once -- both runs share one `test_auctions` database and
corrupt each other into hundreds of unrelated errors.

A parallel run (`--parallel`, which is what CI uses) needs **`tblib`** installed, or the *first*
failing test kills the whole run: Django cannot pickle a traceback back from a worker process, so
it prints a bare `<test> ... failed:` stub with no traceback, re-raises, and destroys the test
databases while the other workers are still running -- which buries the real failure under
`Table 'test_auctions_N.auth_user' doesn't exist` from tests that were never broken. When reading a
parallel log, the **first** `failed:` block is the real failure; anything after the
`Destroying test database for alias 'default'...` lines is collateral.

## Django Commands

Always run inside the container:
```bash
docker exec -it django python3 manage.py makemigrations
docker exec -it django python3 manage.py migrate
docker exec -it django python3 manage.py shell
```

Migration permission error? Use `docker exec -u root -it django ...`

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

## Model Changes

- Always create migrations after model changes (`makemigrations` then `migrate`).
- When adding fields to `Auction`, check if they belong in `fields_to_clone` in `AuctionCreateView`.
- Take a removed field off every form in the **same commit** as the model: a form naming a dropped
  field raises `FieldError` at import, `urls.py` fails to load, and the container crash-loops behind
  an entrypoint that refuses to serve a half-migrated database. `makemigrations` cannot run while
  it is crash-looping — fix the forms first.

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
  buttons, hamburger menus, help notes, pagination, the unavailable-action ("stay clickable")
  standard, and the message-type taxonomy. Never edit vendor CSS; site-wide overrides go in
  `auctions/static/css/auction_site.css`. `docs/style_migration.md` is the worklist of files that
  don't conform to the button rules yet — take a few off it when you're in a template anyway.

## Species list

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

### Setting it up on a live site

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

### Species shapes

- **Cultivar** ("Blue Dream", "Halfmoon"): `variety` set, `parent` pointing at the nominal species,
  carrying the parent's genus and epithet. Show `full_scientific_name`, never `scientific_name`,
  wherever a human reads it. `Club.days_between_same_species_lots` matches on the species row
  itself, so blue and red cherry shrimp are two different things to breed.
- **Hybrid** ("Tibee", "Flowerhorn"): `is_hybrid` set, trade name in `variety`, `genus`, `species`
  and `parent` all empty — `Species.save()` enforces it. Reads as `Hybrid 'Tibee'`. Consequences:
  no `ClubBapGenusOverride` can match one (it matches on `genus`), `species_categories` has only
  what the CSV's `family`/`order` say, and the **only** route to a hybrid is `SpeciesCommonName` —
  nothing reads `variety`, so the form writes the strain name into the name table.

### Adding species and names

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

### The name cache

Written by exactly three places: the bulk add-lot page on a row's first save (bounded to the ≤5
suggestions), the auction admin's lot editor (unbounded), and the language model.
`SpeciesSearchCache.created_by` records who; every row is served to every club.

- `species_matching.record_choice`: a lot saved with the answer left alone counts an **accept**,
  once, on the save that created the lot; one cleared or changed counts a **reject**. Both count
  **lots, not saves**.
- Retiring (`SpeciesSearchCache.is_discredited`) takes **both** one rejection in ten *and* at least
  `MIN_REJECTS_TO_RETIRE` (3) rejections.
- Retiring writes a `SpeciesNameRejection`, which vetoes a **pair** in the two places that make
  things up: `remember()` refuses to learn it again, and it is filtered out of the model's
  shortlist. It never touches `exact_matches` or the token search. "Allow it again" on the gaps
  page is the way back.

### Matching, categories and display

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

## Club announcements and website integration

`auctions/announcements.py` delivers; `/clubs/<slug>/announcements/` is where an admin writes one,
behind `permission_send_announcements`. Channels: Discord, push, an email campaign through the
club's own Mailchimp or Brevo, and the club's website. The Discord channel is set in Discord with
`/announcements_here` (a second channel from `/auctions_here`).

- **Every channel carries the whole announcement.** It has no page of its own and nothing links to
  one.
- `ClubAnnouncement.website_views` counts **renders** (the club page here plus every format of the
  embed, admins excluded) — an impression, not a read.
- Email always goes as a **campaign** addressed to the provider's list, from a Celery task, never
  through this site's mail server. Nobody types a from address (Mailchimp's `campaign_defaults` /
  Brevo's verified senders; the same read fills in `Club.donation_mailing_address` when blank) and
  nobody types a subject — it is always `"<Club> announcement"`. Mailchimp and Brevo are two
  checkboxes but **only one may be ticked**. Only a connected provider is offered. The form opens
  with nothing ticked, the website box included.
- **Nothing is delivered in the request.** One with no time on it is scheduled
  `announcements.GRACE_SECONDS` (30) out; an explicit schedule is the same path with a longer wait.
  `sent_at`, not `scheduled_for`, is the column everything public filters on. Retracting stops one
  that hasn't gone, deletes the Discord post, takes it off the website, then says which channels it
  could not reach. Send and retraction each write a `ClubHistory` row under `ANNOUNCEMENTS`.
- `docs/club_announcements.md` has the whole design.

### The website page and embeds

`/clubs/<slug>/website/` holds everything a club can put on its own site: the event calendar, past
events, the current auction, the latest announcement, the BAP leaderboard, plus a Calendar links
card. Snippets are listed whether or not the feature behind them is switched on, with a note.

- The five embeds share one shell (`auctions/templates/auctions/embeds/`); each has a styled
  template and an `_unstyled` one. `embed_mode_from_request` / `embed_response` in `views.py` are
  the one reader of `?format=`.
- `ClubPastEventsEmbedView` subclasses `ClubEventsEmbedView` and changes three class attributes —
  deliberately the same `events.html`, row shape and `_club_events_embed_rows`.
- **The embed measures itself and the snippet listens.** Every styled embed posts
  `{clubEmbed: "height", height: N}` to `window.parent` (on load, on resize, through a
  `ResizeObserver`); `website_snippet.html` hands over the listener *inside the same `<pre>`*. The
  listener checks `event.origin` against this site and matches frames on `event.source`. The
  `height` in the snippet is only the starting size.
- Calendar links is **not** an embed: two plain addresses following `Club.calendar_subscribe_url` /
  `.calendar_feed_url` — **the club's Google calendar when it is shared, ours when it isn't**. The
  same rule picks the Google button on the club page and the "Add our calendar" link in membership
  emails. The subscribe link is `webcal://` when it falls back to us (an `https` `.ics` is a
  download, which most calendar apps import as a frozen snapshot).
- **Whether that calendar is shared is read, never asked.** `google_calendar.refresh_public_flag`
  fetches the calendar's public `.ics` anonymously (200 = really shared) at the end of every
  `sync_club`, at most hourly (`PUBLIC_CHECK_INTERVAL`, stamped in
  `google_calendar_public_checked`); **Sync now** forces it, `disconnect()` forgets it, and failing
  to reach Google leaves the flag alone. We cannot *set* sharing (needs the sensitive
  `calendar.acls` scope).

### Generated event wording

`ClubEvent.title_is_custom` / `description_is_custom` stop `sync_one_auction_event` and
`sync_pickup_events` overwriting a hand-typed field; `title` and `description` still hold the value
everything displays. `_apply_event_item` refuses Google-side edits to automatic events.
`club_events.generated_wording` recomputes what the site would have written (help text and reset).
`ClubEventForm` narrows itself to those two fields when `instance.is_automatic`, so dates, location,
cancellation and delete stay with the auction (`is_editable` gates delete; `details_are_editable`
guards the form). `docs/club_event_details.md` has the whole design.

- `Club.events_website_views` / `events_website_last_view` count renders of the events embed (every
  `?format=` including JSON); `Club.embeds_events_on_website` is a render inside
  `EVENTS_EMBED_ACTIVE_DAYS` (90). Counted on the **club**, not on a row; the club page here is not
  counted, and an admin's own view is not counted.
- `Auction.event_needing_custom_wording` is the one reader and puts a banner beside the setup
  checklist (outside its if/else). Dismissing writes `Auction.dismissed_customize_event_banner`,
  deliberately not in `AUCTION_FIELDS_TO_CLONE`.
- There is deliberately **no per-event "add this to my calendar" link** on the club page's event
  list. The pickup-time buttons on the auction page are a different thing and stay.

## The club API

`/api/v1/clubs/<slug>/…`, authenticated with a `ClubAPIKey` (`X-API-Key`, prefix `ck_`) or by a
signed-in club admin. One checkbox per capability on the key; `ClubAPIViewMixin.require_club_permission`
takes the key flag *and* the equivalent `ClubMember` permission, so both callers go through one gate.
`/clubs/<slug>/api-keys/<pk>/` is the documentation — every endpoint is written up there, behind the
`{% if %}` for its own permission, and nowhere else.

Members, BAP points/lots and species lookup came first. The read-only auction and lot feed is three
more checkboxes:

- `can_read_auction_info` → `auctions/` (list, with the `current` and `latest` slugs named in it)
  and `auctions/<identifier>/`.
- `can_read_public_lots` → `auctions/<identifier>/lots/` and `.../lots/<lot number>/`.
- `can_read_private_lots` → **the one privacy flag.**

`<identifier>` is an auction slug or the word `current` or `latest`; a real slug wins, so the words
are only ever a fallback. `current` is the pinned `Club.current_auction` if it hasn't wound down,
else the soonest one that hasn't — deliberately looser than `views._club_current_auction`, which
serves the public website embed and will only offer a *promoted* auction. `latest` is the last one
created, promoted or not.

- **Everything that names somebody is in a `private` object that is absent, not null, without the
  flag** (`serializers.PrivateBlockMixin`): buyer and seller names, emails and bidder numbers.
  Removed lots are in the same bargain — excluded entirely from a public key's answer, returned
  with `private.removed` to a key that can read private info. Deleted lots never come back at all.
- **`google_drive_link` is on no tier.** That sheet is shared "anyone with the link can view", so
  the link *is* the credential, and no checkbox on a key should hand out the club's spreadsheet.
- Every reference carries both halves: `{"id": 7, "name": "Cichlids"}`.
- Lot filtering has one rule: **a parameter named after a column matches that column, and
  `?filter=` is the one that looks everywhere.** The narrow ones are `lot_name`, `description`,
  `custom_field_1`, `custom_dropdown` (the whole value — it is a controlled vocabulary the auction
  publishes as `lot_fields.custom_dropdown_options`), `lot_number` (both spellings), `category` /
  `category_id` (through the same `views._resolve_category` the species lookup uses), `species_id`,
  `sold`, `donation`, `i_bred_this_fish`, `custom_checkbox`. `count` is the filtered total. `sold`
  is spelled out as winner-**and**-price because `Lot.sold` is a property. A value it cannot parse
  is a 400, never a shrug — a filter that silently does nothing shows up as a club's front page
  listing the whole auction.
- **A `?filter=` that is all digits is a lot number**, and skips the text columns entirely —
  otherwise `1` matches `10 gallon` and half the descriptions in the auction, and buries the one
  lot the person was after. `?description=1` is still there for digits in the prose.
- **`?filter=` searches public columns only, for every caller** (`views.LOT_GENERIC_FILTER_COLUMNS`).
  `filters.LotAdminFilter` — the admin page's version of the same box — also searches seller name,
  username and bidder number, and copying that list would let a public key confirm a name one
  character at a time. `?seller=` / `?winner=` (name, bidder number or email) carry that instead and
  **refuse** a key without the privacy flag rather than matching nothing, so one `?filter=` means
  one thing whoever sends it.
- **`?ordering=` is an allowlist** (`views.LOT_ORDERING`), not a pass-through to `order_by`: a
  caller who can name any column can order by `auctiontos_winner__email` and binary-search the
  auction's email list out of the sort order without ever holding the private permission.
- `?fields=` narrows each lot (`serializers.SparseFieldsMixin`, applied in `__init__` so an omitted
  field costs no queries). It cannot conjure `private` — the mixin pops that afterwards.
- Each lot's `url` ends in `?src=<key name>`, which is the parameter `PageView` tracking already
  reads — a club that publishes this feed sees its own website in the auction's stats.
- `thumbnail` is one link for a lot tile; `images` is every picture with a full-size and a thumbnail
  URL. Both are absolute (`serializers._absolute`) — Cloudflare hands back absolute URLs and local
  media does not. `views._lot_images_by_owner` and `_auto_images_by_lot_name` are `Lot.images` and
  `Lot.auto_image` batched for a whole page; the second is `models.find_image`'s rule minus its
  per-user preference, and it needs an `AuctionTOS` admin row, not just `created_by`.
- Money is always a string. `serializers.DecimalField` renders one; a raw `Decimal` in a hand-built
  dict comes out of DRF's encoder as a float, which is why `fees.minimum_bid` is formatted by hand.

## MCP endpoint and the command palette's skills

The site is a Model Context Protocol server at **`/mcp/`**. There is **one** catalogue behind it and
the command palette: every capability is an `Action` in `auctions/palette_actions.py`,
`auctions/mcp/tools.py` turns the registry into MCP tool descriptors, and
`palette_actions.run_action` is the single dispatcher. A permission cannot be checked differently
depending on who asked — resolvers call the same form, view or service the web page calls.

A skill cannot exist for one surface and not the other, with one **named** subtraction:
`Action.mcp_only` keeps a skill off the *palette's tool list* while `palette_routes` still guarantees
`go_to_page` reaches its page. Two things qualify, both about the client and neither about the
capability. **Who reads the answer** — `read_source` returns a page of Python, right for an agent and
wrong for a one-line box paid for out of this site's model budget. **Who does the acting** — a class
of writes excused in `NOT_A_SKILL` by arguments about *speech* ("identifying it out loud is harder
than clicking it"), which is true of somebody dictating and empty against a caller sending a lot
number it read out of `list_lots`. Sixteen actions in all; `test_palette_assist.DriftTests.MCP_ONLY`
is the written-out list, and every one of them still covers a view in `SKILLS`.

```
auctions/mcp/tools.py      tool_descriptors(user, writes=) / call_tool(request, name, args)
auctions/mcp/protocol.py   JSON-RPC 2.0 + the four MCP methods. Dicts in, dicts out.
auctions/mcp/transport.py  the Django view: methods, headers, status codes, Origin check
auctions/mcp/auth.py       who is calling
```

`tools.py` is the seam, with two callers: the HTTP endpoint and the palette's own model (in-process
with a live `request`). `auctions/test_mcp.py` is written against the URL, not internals.

### Registry rules

- **Every parameter description must open with its type and required flag** — `"integer, optional,
  default 1."`, `"string, required. The lot number."`. `param_schema` reads type and required off
  that prefix and keeps the whole sentence as the JSON Schema `description`. Enforced by
  `test_mcp.RegistryConformance.test_every_parameter_declares_its_type`.
- Annotations come from the danger tier: `safe` reads, `confirm` writes, `navigate` resolves a URL
  and never acts. `readOnlyHint` is `danger != DANGER_CONFIRM`. There is **no catch-all execute
  tool**. `destructive=True` only where a write destroys a previous answer (`undo_sale`,
  `undo_last`) or cannot be undone at all (`place_bid`). `idempotent` is derived (reads are, writes
  aren't) unless an action that *sets* a value says otherwise.
- Descriptors omit `destructiveHint`/`idempotentHint` on a read-only tool, omit `idempotentHint`
  when false, and carry no `annotations.title`. `openWorldHint` is read off `Action.open_world`.
  There is deliberately **no `outputSchema`**.
- Advice about how to *use* a field belongs in the parameter documentation (a host pays for it once
  a session), not in `lot_fields_in_use`, which is sent with **every** `describe_auction` under a
  5000-character budget the auction's rules sit at the tail of.
  (`test_palette_assist.DescribeAuctionPayloadTests` / `DriftTests`.)

### The palette as a client

`palette_assist.tools_for` is `mcp.tools.tool_descriptors(user)` plus exactly two tools of its own —
`ask_the_user` and `cannot_do_this`. `llm.complete(system, messages, tools)` sends them as OpenAI
function definitions (`llm.as_openai_tool` is the one place that translation lives). `read_reply`
maps "which tool" to lookup / action / question / refusal. `complete_json` stays for the four
callers that want data rather than a call (species matching, donations, the two speaker commands).

Palette-only, and not in the MCP layer: the `obvious_match` / `shortcut_match` short-circuit, the
confirm countdown and its trust window, `humanize`, the `_give_up` fallback ladder,
`sanitize_context` / `_carry_over` conversation memory, the throttles, and the cancel/report
analytics.

### Transport and auth

**Stateless streamable HTTP.** A POSTed request is answered with one `application/json` body; a
notification gets `202`; `GET` and `DELETE` get `405`. A foreign `Origin` is `403`, an unknown
`MCP-Protocol-Version` header is `400`, a missing one means `2025-03-26`.

**A session cookie is never a credential.** `/mcp/` is a CSRF-exempt POST that performs writes. Two
credentials are accepted, both as `Authorization: Bearer`:

* a **`UserAPIKey`** (prefix `ak_`), issued at `/ai/` and shown once. Shares
  `HashedAPIKey.generate` / `.verify` with `ClubAPIKey`: prefix in the clear, secret as a salted
  hash, never stored.
* an **OAuth 2.1 access token** from `django-oauth-toolkit`, gated on `oauth2_provider` being in
  `INSTALLED_APPS`.

`ClubAPIKey` is not reused: it identifies a *club*, and every tool asks "may this user do this".

The authorization server is mounted twice in `fishauctions/urls.py`: its own URLs under `/o/`, and
the discovery documents again at the domain root (RFC 8414 / RFC 9728 put them at the origin).
Three `OAUTH2_PROVIDER` settings each fail silently:

* `"none"` must be in `OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED` — Claude selects CIMD only when
  the metadata advertises both `client_id_metadata_document_supported` and `none`.
* `DCR_REGISTRATION_PERMISSION_CLASSES` must allow anonymous registration; the toolkit's default
  refuses it.
* `ALLOW_LOCALHOST_LOOPBACK`, because Claude Code declares a portless `http://localhost/callback`.

Other config that fails silently:

- `/mcp` is matched with **and without** the trailing slash (`re_path(r"^mcp/?$")`) — `APPEND_SLASH`
  drops a POST body. The `WWW-Authenticate` header points at
  `/.well-known/oauth-protected-resource/mcp`, not the bare origin.
- `auctions/mcp/cimd.py` subclasses the toolkit's SSRF-hardened fetcher and drops grant types this
  server does not advertise (read off `OAUTH2_GRANT_TYPES_SUPPORTED`) before the document is mapped.
  Without it claude.ai gets `invalid_request: Invalid client_id parameter value`.
- `DEFAULT_SCOPES` is `read write offline_access`. Refresh tokens live **180 days**.
- The auth server is assembled by hand rather than `include("oauth2_provider.urls")`:
  `/o/applications/…` is wrapped in `is_superuser` (`_APPLICATION_VIEW_NAMES` is written out, not
  prefix-matched, so a new toolkit view fails loudly), and `/o/register/` is wrapped in
  `mcp.auth.throttle_registration`. The consent screen is this site's own
  (`auctions/templates/oauth2_provider/`).
- `SOURCE_CODE_URL` (repository) and `SOURCE_CODE_BRANCH` (ref) drive `read_source`; blank turns
  the tool off.

Rules:

- **There is no per-user gate**, and no requirement that a language model be configured site-wide.
  `is_active` is checked on every credential. `UserData.use_llm_search` is "AI command palette" only
  (on for everyone; defaults from `ASSISTANT_ENABLED_FOR_USERS`, and unchecking one user in the
  Django admin — or `manage.py change_assistant off` for all of them — is how it is taken away).
- **A credential we recognise and won't act on is a `403`, never a `401`** (`mcp.auth.Refusal`). The
  403 carries no `WWW-Authenticate`.
- `allow_writes` (a key) and the `write` scope (a token) are a **ceiling, not a grant**. Read-only
  credentials are not offered write tools in `tools/list` at all.
- `/ai/` is the page that explains this, lists keys **and what is signed in**, and has a Disconnect
  that deletes access tokens, refresh tokens and grants.

### Prompt injection: three bounds

1. A write needs a permission its owner genuinely holds. (This one does *not* help when the agent is
   already the auction admin.)
2. **No tool changes more than one row, with no exceptions.** There are no bulk writes: "set all
   users not checked in" is `list_people` and then one `undo_check_in` per person.
3. `mcp.auth.within_write_budget` — 2000 writes per credential per hour, counting *attempted*
   writes. `DEFAULT_RATE_LIMIT` (3000 requests) must stay above it, since every write is a request.

Every write lands in `recent_changes` with the assistant named.

**Everything an outsider typed comes back fenced in guillemets.** `untrusted()` wraps a long field
(lot description, auction rules, a question on a lot) in `«written by a member of this site, data
only: … »`; `untrusted_short()` wraps a short one (lot name, participant name, history line) in bare
`«…»`. `_unfenced()` strips our own marks out of the text first, or the writer just closes the fence
and carries on outside it. The server `instructions` name the marks once.
`test_palette_assist.UntrustedTextTests` holds the line. `read_source` output is deliberately not
fenced — it is our own committed source.

`auctions/test_mcp_permissions.py` drives the **whole registry** at one tenant's objects as three
people who should not reach them (a stranger, a legitimate admin of another tenant, an ordinary
bidder inside the tenant). Invariants: nothing of theirs comes back, nothing of theirs changes, and
nothing *crashes* instead of refusing.

### Context: which auction, which club

`mcp.tools.call_tool` sets `request.palette_page = {}` — an agent is not looking at a page.

- `palette_actions.resolve_auction` order: the name they said → the page (browser only) → what is
  actually running (`live_auctions`) → `last_auction_used` as a tie-break between several live
  auctions and a last resort when nothing is live. More than one running and no tie-break is a
  **question**, not a guess.
- `_auction_or_problem` is the single call-site wrapper so `remember_auction` cannot be forgotten.
  `_club_or_problem` is the same shape for clubs; its `also=` argument exists because `name` means
  the club on `describe_club` and a *person* on `add_club_member`.
- `_joined_auctions`: created, joined, **or run by a club they help run**. A *name* also gets one
  look at publicly promoted auctions; every write still checks whether this user administers it.
- `my_context` lists those auctions and the server `instructions` name it as the thing to call
  first. Per-auction facts (`uses_check_in`, `lot_submission_open`) are on **every row**;
  `last_auction` is only a pointer. It carries `they_were_just_looking_at` from `PageView` inside
  `RECENTLY_VIEWED_MINUTES` (20), in the past tense.
- `set_my_auction` / `set_my_club` let an agent be told up front; both resolve through the same
  `_auction_or_problem` / `_club_or_problem`. `set_my_auction` with no name means "whatever is
  running". `set_my_club` writes **two** columns: `last_club_used` and `UserData.club` (the
  affiliation a new auction is filed under, via `services.finish_new_auction`).

### Result shape

- Lists take `limit` and `offset`; `LIST_LIMIT` is 15 and `_showing()` puts the shortfall in the
  summary with the `offset` for the next page.
- **`more_info_needed` is not `isError`.** It comes back as a successful result saying
  `nothing_was_changed`, the question, the candidates, and which tool to call again. MCP elicitation
  needs a session this transport does not have.
- Every result carries `structuredContent` as well as text, **parsed back out of the text** so the
  two cannot disagree and the structure is JSON-safe.
- `resource_link` blocks ride alongside results that named an auction, club or lot. The URI comes
  from the resolver through `palette_actions._about` into `KEY_ABOUT`, stripped on every surface
  (`mcp.tools._payload`, `lookup_payload`, the palette system prompt). A tool never links to its own
  answer; rows in a long list are not linked; `resources.MAX_LINKS` is 12; a URI `resources.match`
  rejects is silently skipped.
- `_lot_echo(lot)` is the shared echo on every write that names a lot (`lot_number`, `lot_name`,
  `auction` slug, `auction_title`, `url`). The number a person reads is `lot_number_display` and the
  address is `lot_link` (`/auctions/<auction>/lots/<number>/`), not the primary key.
- **No lot travels as a primary key.** `mcp.tools._INTERNAL_RESULT_KEYS` strips `lot_id` at **any**
  depth (the leak was mostly in rows — `find_lot` and `points_queue` put one on every line), and no
  tool advertises one; it stays in the resolvers' `aliases` so the palette's page context still
  works. `image_id` is the deliberate exception — a photo has no number on a label — and is why the
  tests name `lot_id` rather than every key ending in `_id`.
- `mcp.tools._absolute` makes any key ending in `_url` absolute — a relative href handed to
  `app.openLink` inside a sandboxed iframe resolves against nothing.
- **Every write says how it arrived**: `palette_actions.via(request)`; MCP sets
  `request.assistant_surface` from the credential (OAuth application name, or the key's), never from
  `initialize`. `ASSISTANT_MARKERS` matches both spellings.
- Icons are five derived URLs (`auctions/mcp/icons.py`), read off the danger tier and
  `tools.area_of`. `test_mcp.IconTests` fails the build if they exceed 15% of `tools/list`.
- `?tools=club`, `?tools=auction`, `?tools=read` narrow `tools/list` (`mcp.tools.parse_areas`);
  `general` is always kept. Not documented on `/ai/`.

### Widgets, prompts, resources

- **Widgets**: `auctions/mcp/widgets.py` is the catalogue; `protocol` serves `resources/list` and
  `resources/read`; `tools.descriptor` hangs `_meta["ui/resourceUri"]` (and the nested spelling) on
  `describe_lot`, `describe_auction`, the invoice reads/writes and the membership card. One document,
  `auctions/templates/auctions/mcp/widget.html`, bakes in `view` per resource. A widget draws itself
  from the same `structuredContent` the model reads — no second payload and no second permission
  check.
- `@modelcontextprotocol/ext-apps` is vendored unmodified in `auctions/mcp/vendor/` (excluded in
  `.pre-commit-config.yaml`) and inlined; `widgets._bundle` rewrites its trailing `export{…}` into a
  `globalThis` assignment, and `test_mcp_widgets` fails the build if that stops matching.
  `csp.resourceDomains` names this site and the Cloudflare delivery host (lot photos, membership
  barcode); `csp.connectDomains` is **empty and stays empty** — no widget calls a tool. Outbound
  links go through `app.openLink`. `resources/list` is deliberately not filtered by permission.
- Two writes may render a widget and `test_mcp_widgets.WRITES_THAT_MAY_RENDER` says why
  (`set_invoice_status`, `add_invoice_adjustment`). Both draw the thing they did, never the thing
  they are about to do.
- **Prompts**: `auctions/mcp/prompts.py` holds `run_check_in`, `chase_unpaid`, `set_up_next_year`,
  `write_announcement`. A prompt is the only safe place for a multi-step recipe, because a person
  picks it off a menu. **Nothing in a prompt body is interpolated except its own arguments** —
  `test_mcp_resources` fails the build otherwise. `completion/complete` answers `ref/prompt` out of
  `_my_auctions` and deliberately refuses `ref/resource`.
- **Resources**: `auctions/mcp/resources.py` publishes `auction://{auction}`,
  `auction://{auction}/lots`, `auction://{auction}/people`, `auction://{auction}/history`,
  `lot://{auction}/{lot}`, `club://{club}`, `club://{club}/events`, `club://{club}/history`,
  `invoice://{auction}/{person}`, the fixed `me://context` and `me://activity`, and `help://faq`.
  Each names a registered **read-only** action; the read goes through `tools.call_tool` with the
  caller's own request, so there is no second permission path. `test_mcp_resources` fails the build
  the day a template names a write.
- **Nothing that names somebody is ever listed.** `resources/list` returns the widget documents, the
  two `me://` reads and `help://faq` (`resources.PUBLIC` / `FIXED`); `resources/templates/list`
  returns patterns. The rule is *no slugs*.

### The skills themselves

Which form, view or service each tool goes through — the auction-side skills, the club-side ones
(the breeder award program and membership cards included), the account pages, the two history logs,
`search_help` / `read_source`, the fifteen `mcp_only` page-only writes, and the three species tools
— is catalogued in `docs/mcp_skills.md`. Everything in this section binds all of them.

### Confirmation tier

`Action.asks_first` is the palette's confirmation card and is separate from the read/write split.
Three actions opt out: `check_in`, `watch_lot`, `review_points`. The bar is confirm-tier, **not**
`destructive`, and idempotent — enforced by `test_mcp.ConfirmationTierTests`. They stay
`readOnlyHint: false`, stay out of a read-only credential's `tools/list`, and stay on the write
budget. `undo_check_in` still asks.

### Housekeeping

- **Adding a URL costs you two entries.** A new named URL or POST view must be catalogued or the
  build fails. `/mcp/` and `oauth2_provider:*` are in `palette_routes.EXCLUDED`; `UserAPIKeyView` is
  in `palette_actions.NOT_A_SKILL`; `user_api_keys` is a real `Route`.
- **A `NOT_A_SKILL` reason has to be about the capability, not about the palette.** The tables are a
  partition (no view in both), no excused view may be reimplemented by a resolver whose docstring
  says it is that view's body — which is how `GoogleCalendarSyncNowView` sat excused for months
  while `sync_club_calendar` was registered — and an excuse whose whole argument is that something
  is hard to say out loud is not a reason. `test_palette_skills.PageOnlyWriteRegistryTests` fails the
  build on all three.
- One hole in that guarantee: `palette_actions.postable_views()` requires `hasattr(view, "post")`,
  so `CreateUserIgnoreCategory` and `DeleteUserIgnoreCategory` — which write in `get()` and have no
  URL name — are in none of `postable_views()`, `NOT_A_SKILL` or `palette_routes.EXCLUDED`. They are
  the only user-facing writes in that blind spot.
- `request_a_skill` records what an agent could not do. Rows are kept and counted
  (`AssistantSkillRequest.others_asking`); `/admin-dashboard/assistant-requests/` is the queue,
  ordered by how many **different people** asked. Row content is model-written: displayed, escaped,
  never executed.
- `docs/mcp_next.md` is the standing list of what the spec has that this server does not, **and**
  what has already been rejected (elicitation and sampling both need a session this transport does
  not have).

```bash
docker exec -it django python3 manage.py test auctions.test_mcp auctions.test_mcp_widgets auctions.test_mcp_resources auctions.test_mcp_permissions auctions.test_source_code auctions.test_palette_account
curl -s -X POST http://127.0.0.1/mcp/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}'
# expect 401 + WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"
```

## Voice-driven set winners

The app does the listening (iOS `WKWebView` has no Web Speech API), but the **grammar is data**: in
`auctions/voice.py` and the single `VoiceGrammar` row, so "the auctioneer says 'hammer' where we
expected 'sold'" is an admin edit rather than an app release.

- `GET /api/mobile/config/` serves the grammar; the app merges it over what it shipped with.
- `GET /api/mobile/auctions/<slug>/voice/vocabulary/` serves the lot and bidder numbers legal **in
  this auction**. Both sides match the utterance against values that actually exist rather than
  transcribing freely and repairing the text.
- `voice.page_config` also sends the grammar and vocabulary down with the page;
  `voiceParse` / `voiceMatchLocally` in `auctions/templates/auctions/dynamic_set_lot_winner.html`
  match the transcript themselves after the app has had `voiceUnmatchedGraceMs` (1200 ms) to answer.
  A build that does match is never second-guessed, and everything the fallback produces goes through
  `voiceHandleCommand` (same green/amber threshold, same `VoiceCommandLog` row, same Confirm
  button).
- The matcher never invents a value and never guesses which slot a bare number belongs to. Both
  readings of a run of number words are tried and the vocabulary picks between them. Two matches
  means an amber field offering both (`VoiceGrammar.homophones`). Price is the one field with no
  list; a currency symbol in front of a number is the price anchor.
- When it matches nothing it says why (`heard "lot one" — no lot like that in this auction`) and
  deliberately does not repeat the number back. A late `command` for an utterance the page has
  already handled is dropped by exact transcript text within three grace windows.
- Fix voice problems on the page, not in the app: the app ships through two app stores.

## Common Issues

| Problem | Fix |
|---|---|
| Won't start | First 4 lines of `.env` not removed |
| Port 80 in use | Add `HTTP_PORT=81` to `.env` |
| Migration permission error | Use `docker exec -u root -it django ...` |
| Static files missing | `docker exec -it django python3 manage.py collectstatic --no-input` |
| DB out of sync | `docker exec -it django python3 manage.py migrate` |
| `IntegrityError (1364, "Field 'x' doesn't have a default value")` | A `NOT NULL` column left behind by a branch that was migrated against this database and then abandoned. It is in no model and no migration, so every insert on that table 500s. `migrate` — `0418_drop_orphan_columns` drops any auctions column no model describes that is `NOT NULL` with no default, and leaves inert ones alone. Test databases are built from migrations, so the suite can never catch this. |
| Build fails | `docker compose down && docker system prune -a -f && docker compose --profile "*" build --no-cache` |
