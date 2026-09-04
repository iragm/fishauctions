# Performance optimization campaign

A long-running, resumable sweep for wasted work: extra queries, missing `select_related`,
`@property` that should be `@cached_property`, and non-DB waste. **No known user-visible problem
started this** -- it is a systematic pass over the whole codebase.

**To resume: read "How a pass works", then take the first area in the queue with status `todo`.**

## How a pass works

One pass = one **area** from the queue below. Keep passes small enough to verify.

1. Read the area's modules and the templates they render. Note what each row of a list actually
   touches.
2. Find the waste. The checklist under "What to look for" is the standing list; add to it when a new
   shape turns up.
3. Fix it. Prefer changes that cannot alter behaviour (`select_related`, `.exists()`, `.count()`).
   Anything that changes *when* a value is computed (`cached_property`) needs the write paths checked
   first -- see "Caching rules" below.
4. **Prove it with a test.** Query-count regressions go in `auctions/test_query_counts.py` using
   `assertNumQueries`. This repo's rule is "no prose about code that a test can't check", and a
   `select_related` with no test is prose.
5. Run `docker compose run --rm test --ci --verbose` and the touched test modules.
6. Update this file: move the area to Done, write what changed under "Pass log", and add any newly
   discovered work to the queue.

### Measuring

There is no query-count baseline infrastructure other than the tests. To measure a page by hand:

```python
# docker exec -it django python3 manage.py shell
from django.test import Client
from django.db import connection, reset_queries
from django.conf import settings
settings.DEBUG = True
c = Client(); c.force_login(User.objects.get(username="..."))
reset_queries(); c.get("/lots/"); print(len(connection.queries))
```

`auctions/test_query_counts.py` is the durable version of that and is where the numbers below come
from.

## Caching rules

`@cached_property` is the single biggest win available (there were **zero** uses of it in this
codebase at the start, against ~180 `@property` methods that run a query). It is also the only
change here that can break something, because the value freezes for the life of the instance.

Before converting a property, check every write path that mutates what it reads:

- **Safe**: the property reads rows that no request that reads the property also writes -- counts and
  aggregates on report/stat pages, display strings, links, thumbnails.
- **Needs invalidation**: something in the same request writes the underlying rows and then reads the
  property again. `auctions/bidding.py` is the standing example: `bid_on_lot` reads `lot.high_bidder`,
  saves a `Bid`, then reads `lot.high_bidder` again and expects the *new* answer. Anything on that
  path either stays a plain property or calls `Lot.invalidate_cached_properties()` after the write.
- **Never**: a property whose value is read in a loop that also writes (bulk actions, invoice
  recalculation).

`Lot`, `Auction`, `AuctionTOS`, `Invoice` and `UserData` have an `invalidate_cached_properties()`
helper for the middle case. Call it after any save that changes what a cached property reads.

## What to look for

DB:
- [ ] `@property` running a query, called more than once per request -> `@cached_property`
- [ ] List views / tables / templates without `select_related` for the FKs each row renders
- [ ] `prefetch_related` for m2m and reverse FKs a row iterates (`lot.shipping_locations.all`)
- [ ] `.count()` where the answer is only tested for truth -> `.exists()`
- [ ] `len(qs)` / `if qs:` where a count is wanted -> `.count()`
- [ ] `qs[0]` + `try/except IndexError` -> `.first()`
- [ ] A queryset sliced twice (`bids[0]`, `bids[1]`) -- each slice is its own query
- [ ] `.values_list().distinct()` without `.order_by()` (see the memory note -- it silently lies)
- [ ] Aggregations done in Python over a queryset instead of `.aggregate()`
- [ ] Repeated identical queries in one request that belong in the queryset as an annotation
- [ ] Missing db_index on a column that every filter uses
- [ ] Unbounded queries (no slice) feeding a page that shows 20 rows

Not DB:
- [ ] Work done for every user that only some users see (compute inside the `{% if %}`, not above it)
- [ ] Module-level or per-request work that belongs in a cache
- [ ] Regexes compiled per call
- [ ] Reading a settings/site row per request instead of per process
- [ ] Templates doing in loops what the view could do once

## Area queue

Ordered by (traffic x cost). Status: `todo` | `wip` | `done` | `n/a`.

| # | Area | Modules | Status |
|---|---|---|---|
| 1 | Lot list pages (browse) | `views/browse.py`, `filters.py` LotFilter, `lot_tile_page.html`, `lot_list_page.html`, `Lot` read properties | done |
| 2 | Lot detail page | `views/lot_pages.py`, `view_lot.html` | done |
| 3 | Auction landing + auction pages | `views/auction_pages.py`, `auction.html`, `Auction` properties | done |
| 4 | AuctionTOS admin table | `views/auction_admin.py`, `tables.py` AuctionTOSHTMxTable, `AuctionTOS` properties | todo |
| 5 | Invoices | `views/invoices.py`, `Invoice` properties, `invoice.html` | todo |
| 6 | PageView write path + middleware | `middleware.py`, `signals.py`, `PageView`, `base_page_view.html` | todo |
| 7 | UserData / account pages | `views/account.py`, `UserData` properties, `account_sidebar*.html` | todo |
| 8 | Context processors (run on every request) | `context_processors.py`, `account_nav.py`, `templatetags/` | todo |
| 9 | Auction stats + admin checklist | `views/auction_stats.py`, `views/admin_checklist.py` | todo |
| 10 | Club pages + members table | `views/club_pages.py`, `views/club_members.py`, `tables.py` | todo |
| 11 | Mobile API | `mobile/views.py`, `mobile/serializers.py`, `mobile/services/` | todo |
| 12 | Club REST API | `views/club_api.py`, `serializers.py` | todo |
| 13 | Celery tasks + management commands | `tasks.py`, `management/commands/` | todo |
| 14 | Command palette / MCP / assist | `command_palette.py`, `palette_*.py`, `mcp/` | todo |
| 15 | Selling / bulk add / bulk actions | `views/selling.py`, `views/bulk_add*.py`, `views/bulk_actions.py` | todo |
| 16 | Payments / webhooks / integrations | `views/payments.py`, `views/webhooks.py`, `views/club_integrations.py` | todo |
| 17 | Exports, printing, labels | `views/exports.py`, `views/printing.py`, `printer_*.py` | todo |
| 18 | Species matching + search cache | `species_matching.py`, `species_categories.py`, `views/species.py` | todo |
| 19 | Forms | `forms.py` (7089 lines -- querysets built per form instance) | todo |
| 20 | Django admin | `admin.py` | todo |
| 21 | Indexes + model `Meta` sweep | `models.py` Meta classes vs. the filters actually used | todo |
| 22 | Static/template rendering waste | `templates/`, `base.html`, vendored JS/CSS payload | todo |
| 23 | Settings / infra | `settings.py`, `gunicorn.conf.py`, cache config, `docker-compose.yaml` | todo |

## Pass log

Newest first.

<!-- PASS LOG START -->

### Pass 3 -- Lot detail page  *(area 2, done)*

**40 -> 23 queries**, and the page no longer gets more expensive as a lot collects bids.

- **`ViewLot.get_object()` is memoized.** It was running `get_queryset().first()` three times --
  three queries, and three *different* `Lot` instances, so every `cached_property` on the lot
  (`bids`, `images`, `currency`, `high_bidder`) was computed once per copy.
- **`ViewLot.get_queryset()` select_relates** the auction, its creator's userdata, the club, the
  category, the submitter's userdata, both `AuctionTOS` rows and their pickup locations.
  `distance_to` had to be given the lot's own column names to do it: it defaults to bare
  `latitude`/`longitude`, and `userdata` and `club` both have a `latitude`, so MariaDB rejected the
  query as ambiguous the moment it joined them. `_lot_distance_to` in `views/lot_pages.py` does that.
- **`Lot.bids` takes the bidders with it** when it is not prefetched -- the page lists bids with
  who placed them, which was a query per bid. It checks `_result_cache` first so a prefetched lot
  list does not throw the prefetch away by re-fetching with a join.
- **`UserData.save()` does one `UPDATE`** instead of a `SELECT` plus a save per row. It runs on
  ordinary page views (the location context processor saves userdata when a cookie changes), and it
  was walking the user's chat subscriptions every time.
- `not Bid.objects.filter(...)` -> `.exists()`.

New guard: `LotDetailQueryCountTests` gives a lot three bids and three images and asserts the page
does not cost more than it did before.


### Pass 2 -- Auction pages and the location property family  *(area 3, done)*

**`/` went 54 -> 29 queries and an auction page 48 -> 23.** Twenty-eight of the auction page's 48
were one family of properties asking the same question over and over.

- **`Auction.locations`** -- a cached list of every `PickupLocation` for the auction. `location_qs`
  stays a queryset (form fields, slicing and `.filter()` callers need one), but the twelve things
  derived from it now read the list: `physical_locations`, `locations_with_coordinates`,
  `number_of_locations`, `all_location_count`, `allow_mailing_lots`, `multi_location`,
  `no_location`, `location_link`, `set_location_link`, `admin_checklist_location_set`. `auction.html`
  alone reads `multi_location` six times and `pickup_locations` six times.
- **`PickupLocation.save()` invalidates its auction**, the same shape as `Bid.save()` invalidating
  its lot. `test_auction_props` caught this immediately: it adds a location and re-reads
  `all_location_count` on an `Auction` it is still holding.
- **`views/auction_pages.py`**: `context["pickup_locations"]` is the cached list, not the queryset
  (six template loops, six queries); `dispatch` calls `get_object()` once and asks for
  `all_location_count` rather than `location_qs.count()`.
- **`PickupLocation`**: `number_of_users`, `email_list`, `number_of_incoming_lots`,
  `number_of_outgoing_lots`, `total_sold`, `total_bought` cached; `email_list` uses `.only("email")`.
- **`Auction.admin_checklist_*`** (9 properties) cached -- the ribbon and the checklist page read
  the whole family, and each one re-derives the ones before it.

**New guard: `CachedPropertyWiringTests`.** Adding `@cached_property` to a model that is not a
`CachedPropertiesMixin` compiles, passes every test, and serves values from before the row's own
save forever. The test walks every model in the app and fails if any has one without the mixin.

**`auctions/html_sanitize.py`** -- `sanitize_summernote_html` and the two tag allowlists moved out
of `models.py` (112 lines, no model dependencies). `models.py` is on a 15000-line ratchet and this
campaign keeps adding to it; this buys the headroom for the next few passes. Take something else out
when it runs out again -- do not raise the number.


### Pass 1 -- Lot list pages (browse)  *(area 1, done)*

**Result: one more lot on a lot list went from ~10 queries to 1.** Guarded by
`auctions/test_query_counts.py::LotListQueryCountTests`, which measures growth per row rather than
a total, so it does not break every time an unrelated page cost changes.

The infrastructure this pass had to build first:

- **`auctions/model_caching.py`** -- `CachedPropertiesMixin`, mixed into `Auction`, `AuctionTOS`,
  `Lot`, `Invoice`, `PageView` and `UserData`. Gives them `invalidate_cached_properties()` and drops
  every cached value after `save()`. There were **zero** uses of `cached_property` in this codebase
  and ~180 `@property` methods that run a query, so this is the lever for most of what follows.
- **`Bid.save()` invalidates the `Lot` it was built with.** `bid_on_lot` reads `lot.high_bidder`,
  writes a `Bid`, and reads it again expecting the new answer. Doing this at the write rather than
  at each call site is what makes caching `Lot.bids` safe; `test_bidding`'s increment tests are the
  end-to-end guard and did catch it when only some call sites invalidated.

Changed:

| Where | What | Was |
|---|---|---|
| `Lot.bids` | `cached_property`, a **list**, read through `self.bid_set` so it prefetches | queryset rebuilt and re-sliced -- 3-4 bid queries per row |
| `Lot.images` / `thumbnail` / `auto_image` / `image_count` | `cached_property`; `images` reads the reverse relation and sorts in Python so it prefetches; `thumbnail` picks the primary out of it | 2 image queries per row, ×3 for the three `lot.thumbnail` in the tile template |
| `find_image` | one query -- "the user's own image, else the newest" decided in `ORDER BY` | two queries, per image-less row |
| `Auction.auction_admins_qs` | `Q(user_id=self.created_by_id)` | `Q(user=self.created_by)` fetched the creator to read its pk, once per row |
| `Lot.currency` / `currency_symbol` | `cached_property` | the auction creator + their userdata, per row |
| `AuctionTOS.display_name` / `display_name_for_admins` | `cached_property` | the auction + the user's userdata, per row, for seller *and* winner |
| `Lot.page_views` / `anonymous_views` | `COUNT(*)` | `len(qs)` pulled every `PageView` row into Python, per row |
| `Lot.seller_as_str`, `winner_as_str`, `high_bidder_display`, `high_bidder_for_admins`, `lot_link`, `full_lot_link`, `qr_code`, `number_of_watchers`, `number_of_bids`, `all_page_views` | `cached_property` | each read 2-4× per row |
| `LotFilter.qs` | 12 `prefetch_related` lookups (see below) | nothing -- an earlier `select_related` attempt was reverted as slower |
| `views/browse.py` | `.count()` not `len(qs)`; `values_list(...).first()` not `qs[0]` + `except IndexError` | |
| `Invoice.bought_lots_queryset_old` | deleted | dead since it was superseded |

**`prefetch_related`, not `select_related`, for the relations that repeat down the page.** A join
copies the same auction row into all 50 result rows and -- the part that matters -- hands every
`Lot` its *own* `Auction` instance, so every `cached_property` on `Auction` is recomputed once per
row. `prefetch_related` fetches each related row once and assigns **the same instance** to every lot
pointing at it, which is what makes the caches above worth having. Measured: `select_related`
version 3 queries/row, `prefetch_related` version 1.

The one query per row left is `Lot.auto_image`: a lot with no picture borrows one from another lot
with the same name, and the name is per row, so there is nothing to prefetch. A lot with its own
image costs nothing.

`models.py` is at its 15000-line ceiling, which is why the mixin went in its own module and the
docstrings here are terse. Anything added to it now has to take something out.

<!-- PASS LOG END -->

## Deliberately not done

Things that look like waste but are not, so nobody "fixes" them twice.

<!-- WONTFIX START -->

- **`Lot.bids` doing its dedupe in Python.** It looks like work that belongs in SQL, and it was in
  SQL. As a correlated subquery it cost one query per row on every lot list and could not be
  prefetched. The Python version applies the same rule in the same order and is covered by
  `test_bids_keeps_only_each_users_latest_bid`.
- **`Lot.images` / `Lot.bids` returning lists rather than querysets.** Deliberate: every caller
  iterates or indexes them, several do so more than once, and a queryset re-queries on each slice.
  `.count()` / `.first()` on them is a `TypeError`, which is the point -- it fails loudly at the
  call site rather than quietly costing a query.
- **`prefetch_related` where `select_related` would do.** See Pass 1: instance sharing across rows
  is worth more here than saving the round trip.

<!-- WONTFIX END -->
