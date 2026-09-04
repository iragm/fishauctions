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
| 4 | AuctionTOS admin table | `views/auction_admin.py`, `tables.py` AuctionTOSHTMxTable, `AuctionTOS` properties | done |
| 5 | Invoices | `views/invoices.py`, `Invoice` properties, `invoice.html` | done |
| 6 | PageView write path + middleware | `middleware.py`, `signals.py`, `PageView`, `base_page_view.html` | done |
| 7 | UserData / account pages | `views/account.py`, `UserData` properties, `account_sidebar*.html` | done |
| 8 | Context processors (run on every request) | `context_processors.py`, `account_nav.py`, `templatetags/` | done |
| 9 | Auction stats + admin checklist | `views/auction_stats.py`, `views/admin_checklist.py` | done |
| 10 | Club pages + members table | `views/club_pages.py`, `views/club_members.py`, `tables.py` | done |
| 11 | Mobile API | `mobile/views.py`, `mobile/serializers.py`, `mobile/services/` | done -- measured flat; the offline snapshot was already batched |
| 12 | Club REST API | `views/club_api.py`, `serializers.py` | done -- checked, already batched |
| 13 | Celery tasks + management commands | `tasks.py`, `management/commands/` | partial -- sendnotifications done; the once-a-day imports and backfills are deliberately left alone |
| 14 | Command palette / MCP / assist | `command_palette.py`, `palette_*.py`, `mcp/` | done -- measured flat |
| 15 | Selling / bulk add / bulk actions | `views/selling.py`, `views/bulk_add*.py`, `views/bulk_actions.py` | partial -- /selling/ and /feedback/ done; the bulk write paths are not measured |
| 16 | Payments / webhooks / integrations | `views/payments.py`, `views/webhooks.py`, `views/club_integrations.py` | done -- measured flat |
| 17 | Exports, printing, labels | `views/exports.py`, `views/printing.py`, `printer_*.py` | done |
| 18 | Species matching + search cache | `species_matching.py`, `species_categories.py`, `views/species.py` | done -- measured, 3-4 queries a call |
| 19 | Forms | `forms.py` (7089 lines -- querysets built per form instance) | done -- the form-heavy pages measured flat |
| 20 | Django admin | `admin.py` | not done -- staff-only, and not on the measured URL map |
| 21 | Indexes + model `Meta` sweep | `models.py` Meta classes vs. the filters actually used | done |
| 22 | Static/template rendering waste | `templates/`, `base.html`, vendored JS/CSS payload | partial -- caching headers and the broken `defer` done; the head scripts need an inline-script audit first |
| 23 | Settings / infra | `settings.py`, `gunicorn.conf.py`, cache config, `docker-compose.yaml` | done |

## Pass log

Newest first.

<!-- PASS LOG START -->

### Pass 17 -- Front end: caching headers and a `defer` that never was  *(areas 18, 22, done)*

Not the database for once.

- **`/static/` and `/media/` had no caching policy at all.** Every navigation revalidated the six
  scripts and stylesheets `base.html` loads, and every lot image. They now say
  `Cache-Control: public, max-age=3600` and `max-age=2592000` respectively
  (`nginx_fishauctions.conf`, which both dev and prod include). One hour on static and not longer
  because these filenames are **not content-hashed** -- Django's plain `StaticFilesStorage` -- so a
  deploy has to wait out whatever we set. Media is thirty days because an uploaded file is written
  once under a name Django makes unique and never edited in place.
- **`{% static 'js/htmx.min.js' defer %}`** -- `defer` inside the tag is an extra argument the
  `static` tag silently ignores, so the two scripts somebody meant to defer had been loading
  render-blocking ever since. The attribute is on the `<script>` element now.

Species matching (area 18) was measured rather than changed: the suggestion endpoint the lot form
calls is 4 queries and `exact_matches` is 3, which is what that work costs.

**The next front-end win, deliberately not taken:** `base.html` loads six render-blocking scripts
in `<head>` (jQuery, Bootstrap, and four of ours). Deferring them would measurably improve first
paint, but templates all over the site have inline `<script>` blocks that call `$(...)` while the
page parses, and every one of those would break. It needs an audit of the inline scripts first,
which is its own piece of work. Content-hashed static filenames
(`ManifestStaticFilesStorage` + `max-age=31536000, immutable`) belong in the same piece.


### Pass 16 -- Code review, and two things it caught

A full review of the campaign's diff. Two real defects, both from caching:

- **The lot page's websocket held a stale price.** `LotConsumer` fetches its `Lot` in `connect()`
  and keeps that one instance for as long as the page is open -- minutes, on a lot being bid on the
  whole time. `lot.high_bid` used to be live because `bids` was a fresh queryset per access; cached,
  it froze at whatever the first chat message saw, and every message after that was filed at the
  price from before. `receive()` invalidates the instance now, which is exactly the old behaviour
  and costs nothing on a connection nobody types into.
- **`Lot.images` raised on an unsaved lot.** Reading a reverse relation on a pk-less instance is a
  `ValueError`, and `bulk_add_lots` and the offline sync both build `Lot`s before saving them.
  `bids` had the guard from the day it changed; `images` did not. Both now covered by
  `test_an_unsaved_lot_answers_rather_than_raising`.

Checked and found correct: the Python rewrite of `Lot.bids` matches the old subquery exactly
(including tie-breaking on `-bid_time, -pk` and deleted bids); the read-after-write ordering in
`bid_on_lot`; the annotations in `AuctionTOS.annotate_lot_counts` and `_report_counts` against the
properties they replace; the list-vs-queryset conversions against every caller; and the MRO of the
two new mixins against `ContactRecord` and `CloudflareImageMixin`.


### Pass 15 -- One declarative rule for stale caches  *(review of the campaign so far)*

Five models had grown a hand-written `save()` (and sometimes `delete()`) that dropped the caches on
the row they hang off. Reviewing them together showed the same twelve lines five times over, and
**two gaps**: nothing invalidated `Lot.images` when a `LotImage` was written, or
`Lot.number_of_watchers` when somebody watched a lot.

`InvalidatesRelatedCache` in `auctions/model_caching.py` replaces all five with a declaration:

```python
class Bid(InvalidatesRelatedCache, models.Model):
    invalidates_cache_on = ("lot_number",)
```

Now on `Bid`, `PickupLocation`, `AuctionTOS`, `InvoiceAdjustment`, `InvoicePayment`,
`VolunteerSignup`, and -- newly -- `LotImage` and `Watch`. It reaches the related object through
`fields_cache`, so it never fetches a row just to invalidate a copy nobody is holding, and it
handles `delete()` (reading the relation before the row goes).

`test_invalidates_cache_on_names_real_foreign_keys` guards it: a typo in that tuple is otherwise
completely silent -- nothing is invalidated and nothing complains.


### Pass 14 -- The whole URL map, measured  *(areas 9, 11, 14, 16, 17, 19, 20 checked)*

Rather than reading more code, this pass **measured every GET-able URL**: hit each one with the
standard fixture, add four more people / lots / bids / invoices / page views to the auction, hit
them all again, and list what got more expensive. That is a reusable technique; the throwaway
sweep is not in the tree but the recipe is here.

Fixed, all of them per-row growth:

- **`/api/auctionstats/<slug>/lots_submitted`** and **`previous_auctions`** -- both feed
  `helper_functions.bin_data`, which reads `getattr(item, field_name)` for every row, and both were
  handing it the *name of a property that runs a query*. Both querysets annotate the number now.
- **`/auctions/<slug>/lotlist/`** (the lot list CSV) and the two **`/locations/<pk>/…-lots`** CSVs
  select_related the seller, the winner, their pickup locations and their auctions -- each row
  names both people and says where each collects, which was four queries a row.
- **`Auction.permission_check`** compares `created_by_id` to the user's pk instead of the objects,
  which no longer fetches the creator to read its id, and **`AuctionViewMixin`** caches the *result*
  of that check for the request. Not `is_auction_admin` itself: that one raises `PermissionDenied`
  depending on `allow_non_admins`, which `can_add_edit_people` flips while it asks, so caching the
  decision rather than the query would let a read after that flip return an answer without raising.

Measured and left alone: everything else in the URL map is flat. The stats page and the auction
report still cost about six queries per person, and both are the invoice number tree, which each
person genuinely needs; `Auction.club_profit`'s fallback loop (only invoices nobody has
recalculated) select_relates what that tree reads.

**A known flake to expect:** `test_species.ClubSpeciesCommonNameAPITests.test_another_club_is_not_answered_with_it`
fails about one full `--parallel` run in five and passes on a rerun. It predates this work -- the
test right below it is named "The route that made test_another_club_is_not_answered_with_it flake".


### Pass 13 -- The member's own pages  *(area 15 partial)*

- **/feedback/: 21 -> 6 queries.** Each row names the other party and links to the lot, so it read
  the lot's auction, the `AuctionTOS`, that TOS's auction (for the online/in-person display rule)
  and the person's userdata -- six queries a row, with no `select_related` on either list.
- **/selling/: the Views column is annotated.** `Lot.page_views` is a `COUNT` on the biggest table
  on the site and the table prints it for every row. `LotFilter` adds it as a subquery when the
  list is scoped to one person (which is exactly when the template shows it), and `page_views`
  reads the annotation when it is there.

Measured and already flat, so left alone: `/auctions/`, `/bids/`, `/lots/watched/`, `/invoices/`,
`/leaderboard/`, `/messages/`. The club REST API's lot list already select_relates everything its
serializer touches and batches its images and auto-images through the serializer context -- that
work was done before this campaign.


### Pass 12 -- Settings and indexes  *(areas 21, 23, done)*

- **Sessions live in Redis with the database behind them** (`SESSION_ENGINE = cached_db`). The
  default engine reads `django_session` on every request that carries a cookie -- one query on
  every page anybody loads -- and Redis was already here for the cache and the channel layer.
  `cached_db`, not `cache`: a Redis restart or eviction loses nobody's session, the read just falls
  back to the row. Together with the session-write fix in Pass 6 that is one query *and* one write
  off every page.
- **Two indexes** (`0423_query_indexes`), both backing a `filter(...).order_by(...).first()` that
  had an index for the filter and nothing for the order, so MariaDB found the rows and then sorted
  them by hand to return one:
  - `PageView(user, -date_start)` -- every lot list a signed-in person opens asks for the date of
    their most recent lot view, to badge lots as new. **Expensive to apply**: PageView is the
    biggest table here. InnoDB builds it in place, but run it when the site is quiet.
  - `Invoice(auctiontos_user, -date)` -- `AuctionTOS.invoice`. Small table, cheap.

Checked and found already right: the **cached template loader** is on in production (Django adds it
automatically for `APP_DIRS` when template debug is off), `CONN_MAX_AGE = 0` is correct under ASGI
and documented as such, and `re` caches compiled patterns so the module-level-`re.compile` question
does not arise.


### Pass 11 -- Queries inside loops  *(areas 13, 17 partial)*

A scan for `.objects.` calls inside `for` loops, then the ones on a request or cron path:

- **`AuctionBidsChartData`** counted distinct bidders with a query *per lot*. A five-hundred-lot
  auction drew that chart with five hundred queries; it is one `GROUP BY` now.
- **`AuctionCategoriesChartData`** ran three queries per category, twenty categories deep. Three
  `GROUP BY`s for the whole chart now.
- **"Add auction users to club"** did two `ClubMember` lookups per person in the auction. It loads
  who is already a member in one query and keeps the map current as it adds, so two people sharing
  an email still can't both be added.
- **`manage.py sendnotifications`** (a cron) ran a `Watch` query per lot and then **two** user
  fetches per watch -- the FK, and then a `User.objects.get` for the object the FK had just
  returned. One `select_related` query per auction now.

The rest of the hits are management commands that run once (imports, backfills, migrations of
data), where a query per row is the readable choice and the cost is paid by nobody waiting.


### Pass 10 -- The remaining HTMx tables  *(area 10, done; area 4 extended)*

The tables an auction or a club is administered from render a hundred rows at a time and none of
their querysets said anything about what a row touches.

- **The auction's lot table** (`auction_lot_list`) prints the seller and the winner -- each of which
  reads the auction and that person's userdata to build a display name -- links to *both* of their
  invoices, and asks whether the lot has an image. It now select_relates the two people and
  prefetches the auction, their auctions, their invoices and the lot's images. Guarded by
  `AuctionLotAdminTableQueryCountTests`.
- **`Lot.sellers_invoice` / `winner_invoice` take the `AuctionTOS` route first.** They used to
  build one `Q(auctiontos_user=...) | Q(auctiontos_user__user=..., auction=...)` query, which
  nothing can prefetch. Going through `AuctionTOS.invoice` (which is prefetchable) and falling back
  to the user query keeps the same answer and costs the table nothing.
- **The club members table** prefetches its club: every row reads `club.membership_annual_fee` to
  decide whether to draw a Renew button, and prefetch (rather than a join) means all the rows share
  one `Club` instance and one copy of everything cached on it.
- The two history tables (`AuctionHistory`, `ClubHistory`) select_related the user each row names.


### Pass 9 -- Sweep: the rest of the model properties, and the `len`/`count` shapes  *(areas 20, 21 partial)*

Not a page, a sweep. The scan that started this campaign found ~180 `@property` methods on models
that run a query; **52 are left**, and most of those are deliberate (the `*_qs` builders that exist
to be a queryset, and the nine volatile `UserData` ones).

- **`ClubMember`** (`discord_role` -- seven queries -- `has_auction_checkin`, the wallet and
  barcode links), **`Lot`** (`unsold_lot_no_bap_reason` at twenty queries,
  `page_view_source_breakdown`, `square_refund_possible`, `winner_invoice`, `sellers_invoice`,
  `winner_location`, `max_bid`, ...), **`PageView`**, **`AdCampaign`**, **`AdCampaignGroup`**,
  **`AuctionCampaign`**, **`VolunteerJob`**, **`Speaker`** and **`AssistantSkillRequest`** all cache
  now, and all gained the mixin.
- **`VolunteerSignup.save()/delete()` invalidate their job.** `test_volunteers` caught it: the
  signup view asks whether a job is full both before and after creating a signup.
- `.exists()` where a `.count()` was only tested for truth (`helper_functions.bin_data`,
  `forms.py` duplicate checks, `Auction.admin_checklist_*`).
- `Invoice.unsold_lots` counted in SQL instead of `len()` over a cloned queryset.
- `Auction.set_stat_*` and `views/auction_stats.py` held the same duplicated block, each calling
  `auctiontos.count()` three times and `invoices.count()` twice per render; both count once now.

`sell_to_online_high_bidder` and `AuctionCampaign.update` are properties that *write*, and are
deliberately left uncached.


### Pass 8 -- Auction report CSV and auction stats  *(area 9, done)*

**The auction report went 298 -> 75 queries**, and the stats page 50 -> 43.

`AuctionReportView` wrote a CSV a row at a time and asked the database about each person nine
times: six `len(queryset)` calls (each pulling every matching row into Python only to count it), an
invoice lookup, a second invoice lookup behind `gross_sold`, and a count of that person's other
auctions. An auction of five hundred people ran four and a half thousand queries for one file.

- **`_report_counts(auction, users)`** works out every per-person number in five `GROUP BY`
  queries, keyed by `AuctionTOS` pk (lots submitted / sold / bred / won) or user pk (page views,
  bids, other auctions joined), and the loop looks each person up.
- **The invoice is prefetched** with its auction and club, and `data.invoice` is what
  `gross_sold` and `total_club_cut` read too -- that was two invoice queries per row and is now
  none.
- **47 more `Auction` properties cached**: the whole stats family (`total_lots`,
  `number_of_participants`, `median_lot_price`, `club_profit`, `gross`, the campaign counts, ...).
  The stats page reads most of them two or three times while deriving percentages.
- **`AuctionTOS.save()` invalidates its auction**, so those participant counts cannot go stale --
  the fourth model to do this, after `Bid` -> `Lot`, `PickupLocation` -> `Auction` and
  `Lot` -> seller/winner/auction.


### Pass 6+7 -- The per-request path, and UserData  *(areas 6, 7, 8, done)*

- **`add_location` no longer writes the session on every request.** It set
  `request.session["status"] = "started"` unconditionally to force a session key into existence,
  which marks the session modified and so writes a `django_session` row for *every page anybody
  loads*, signed in or not. It only sets it when it is missing now.
- **`once_per_request`** in `context_processors.py`. Django binds context processors to each new
  `RequestContext`, so a view that renders a partial as well as its page runs the querying ones
  twice. `user_clubs`, `label_print_method` and `site_config` are memoized on the request.
- **`user_clubs` is one query**, through the membership rows, instead of a `values_list` of club
  ids followed by a query for the clubs.
- **`UserData`: 36 properties cached**, and the user page went 29 -> 18 queries. Three of them were
  `len(queryset)` -- `lots_viewed` pulled every one of the user's `PageView` rows into Python to
  count them, five times per render, and `PageView` is the biggest table on the site. `total_sold`
  and `total_spent` summed in a Python loop over every lot; they `aggregate()` now.

Nine `UserData` properties are deliberately **not** cached -- see "Deliberately not done".


### Pass 5 -- Invoices  *(area 5, done)*

**189 -> 40 queries for one invoice.** 54 of the original were the same `SUM` over four adjustment
rows.

The invoice's numbers are a tree -- `net` reads `subtotal` reads `total_sold` and `total_bought`;
`manual_adjustment_amount` reads `subtotal` again; `tax` re-aggregates the bought lots -- and none
of it was cached, so every top-level read re-derived the whole thing.

- **55 `Invoice` properties cached**, the whole tree from `sold_lots_queryset` up to
  `rounded_net_after_payments`.
- **`Invoice.adjustment_totals`** -- one `GROUP BY` for all four adjustment types, where
  `sum_adjusments` ran a separate `SUM` per type per call.
- **`InvoiceView.get_object()` memoized** and select_related. `dispatch`, `get` and
  `get_context_data` each fetched the invoice again -- five instances, five copies of the tree.
- **`Invoice.changed_adjustments` hands each adjustment the invoice it came from.**
  `InvoiceAdjustment.display` reads a currency symbol off `self.invoice`, which was four queries
  per line of the table (its own invoice, the auction, the creator, their userdata).
- **`sold_lots_queryset` / `bought_lots_queryset` select_related** the auction, category and both
  pickup locations -- every row prints a lot number (which reads the auction) and where its winner
  collects it.
- **Writes invalidate:** `recalculate()` drops the tree before re-deriving (that is its whole job),
  and `InvoiceAdjustment.save()/delete()` and `InvoicePayment.save()` drop the invoice they point at.
- **`refresh_from_db()` now invalidates too**, for every model with the mixin. It reloads columns
  and nothing else, so without this an instance comes back from the database carrying answers
  derived before the reload -- worse than not refreshing, because the caller asked for current data
  and got a mix. `test_paypal` caught exactly that.
- `Auction.wind_down_time`, `pickup_locations_before_end` and `set_stat_location_volume` read the
  cached location list; `closed`, `started`, `ended`, `pretty_much_over`, `is_club_managed` cached.


### Pass 4 -- The auction users / invoices table  *(area 4, done)*

**292 -> 25 queries for 25 people, and flat: another person on the page now costs nothing.** This
is the page an organiser runs an auction from, at 100 rows a page.

Each row renders `actions_dropdown_html`, and that read `unbanned_lot_count` three times,
`unprinted_label_count` twice, `bought_lots_qs.count()`, `lots_qs.count()` and `invoice` five
times -- none of them cached, all of them a query.

- **`AuctionTOS.annotate_lot_counts(queryset, auction=...)`** puts the five per-person counts in
  the queryset as subqueries: lots sold, lots won, unbanned lots, unprinted labels, printable
  labels. Subqueries rather than `Count(..., distinct=True)` over joins, because five joins to
  `auctions_lot` against one another multiply rows and `distinct` then has to undo it. The matching
  properties read the annotation when it is there and fall back to their own query when it is not,
  so every other caller is unchanged.
- **`AuctionTOS.invoice` reads the reverse relation** and sorts in Python, so the table can
  `prefetch_related(Prefetch("auctiontos", queryset=Invoice.objects.order_by("-date")))`.
- **The auction is prefetched, not joined.** `select_related("auction__created_by")` gave every row
  its own `Auction` instance, so `self.auction.club` was a query per row and no `Auction`
  cached_property survived from one row to the next.
- 17 more `AuctionTOS` properties cached (`club_member_record`, `distance_traveled`,
  `trying_to_avoid_ban`, `closest_location_for_this_user`, the label links, ...).
- **`Lot.invalidate_cached_properties()` cascades** to the seller, the winner and the auction it is
  holding -- a lot being sold or its label printed changes counts they cache. `test_mobile_features`
  caught the one case this cannot reach: a test holding an `AuctionTOS` from before an HTTP request,
  which nothing inside that request could invalidate. It re-reads now.

**`auctions/queryset_annotations.py`** -- `nearby_auctions`, `add_tos_info` and
`add_tos_distance_info` moved out of `models.py` (176 lines). `models.py` never called them, so the
dependency runs one way and there is no cycle. This is the second such split; when the ceiling comes
back, take another group out rather than raising the number.


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

- **`UserData.has_push_device` and the eight subscription properties are not cached.** They change
  inside the request that reads them: a notification run registers a device and then asks whether
  there is one; the chat pages read the subscription list, write a message, and read it again.
  `test_mobile_features` and `test_lot_models` both catch it, which is how they got back on this list.


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
