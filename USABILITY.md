# Usability campaign

A resumable sweep aimed at one thing: **a non-technical person can run an auction here without
being taught.** The site is feature-complete; this is the pass over what those features feel like to somebody who has never seen them.

Human note, Let's also clear up one thing - the goal is not just "a non-technical person can run an auction here without
being taught" - it's "a non-technical person can participate in an auction as a buyer only, or as a seller, with or without an account, and have an excellent experience that makes them want to come back".  Smooth UX is quite literally the only thing this project offers over a spreadsheet.  Once the stuff listed here is done, we need to do this again thinking about how to engage all users, not just auction admins.

(That is phase 7 below.)

### 2. `Auction.create_history` knows which settings changed and stringifies it

`create_history` (`models.py:6566`) already walks `form.changed_data` on every `AuctionUpdate`
(`views/auction_admin.py:304`, `:412`). It then flattens the field list into comma-joined
**verbose names** in `action`, a `CharField(max_length=800)`. Four consequences:

- Keyed on `verbose_name`, so rewording a label silently breaks the history, and two fields sharing
  a verbose name are indistinguishable.
- Free text, so the only query is `LIKE` and hope.
- **Truncated at 800 characters.** With ~90 fields on that form a broad edit loses the tail, and
  truncation follows form-field order -- so the loss is systematically biased toward whatever sits
  at the bottom of the layout.
- It records *that* a field changed, never what it became.

`ClubHistory` (`models.py:2340`) has the identical shape and the identical problem.

### 3. Page-view coverage is opt-in and delayed

- `pageView()` is called by **38 of 247 templates**. It is not automatic in `base.html`; each
  template opts in by hand, and a template that forgets is silently absent from every report.
- It fires behind a 2-second `setTimeout` (`base_page_view.html`). Any page abandoned in under two
  seconds records nothing -- which deletes exactly the confused bounces, mis-clicks and redirect
  hops that a usability pass is looking for. Every report built on `PageView` is therefore biased
  toward pages people did **not** struggle with.

This design decision was done to provide useful data to auction admins: putting page view on admin only pages generates meaningless data for people running auctions, and I stand by it being the right call - 1000x page views on set lot winners is worthless to them (that page now all js, but it was one of the drivers at the time)

Revisited in phase 7a: the goal is right and the layer is wrong. Every organizer-facing read of
`PageView` filters on the `lot_number` or `auction` FK, and only four call sites ever set those,
so recording the admin pages cannot put a single row in front of an organizer. The timer goes too.

**Done, 2026-09-09.** Both numbers above are now history rather than caveats; `usability_report.py`
says so in the past tense and `REACH_CAVEATS` is gone.

Ordered by (unblocks-other-work x value). Status: `todo` | `wip` | `done`.

| # | Phase | Touches | Status |
|---|---|---|---|
| 0 | Store a path in `PageView.url`; delete `AdminUserFlow` + `compute_user_flow_all`; add the One Tap counter | `base_page_view.html`, `views/ajax.py`, `views/site_admin.py`, `tasks.py` | done |
| 1 | Friction instrument: which form, which field, how many attempts, did they finish -- **and whether they gave up without submitting at all** | `friction_models.py`, `form_friction.py`, `static/js/unsaved_changes.js`, 13 views | done |
| 1b | `AuctionHistory.changed_fields` / `ClubHistory.changed_fields` as JSON, written alongside `action` | `history.py`, `models.py`, migration 0426 | done |
| 1.5 | Reach / failure / adoption dashboard at `/admin-usability/` | `usability_report.py`, `views/usability.py` | done |
| 2 | Club health: derived lifecycle rollup + "due for check-in" queue at `/admin-club-health/` | `club_health.py`, `tasks.py`, migration 0428 | done |
| 3 | Progressive disclosure on `AuctionEditForm` -- essentials vs. Advanced | `auction_form_layout.py`, `field_adoption.py` | done |
| 4 | Contextual help in the `help-note` format, one per page | `templates/` | done (first pass) |
| 5 | Accessibility debt: images with no `alt`, icon-only controls with no name, silent HTMx swaps | `templates/`, `template_a11y.py` | done |
| 6 | First paint: defer the head scripts, content-hashed `/static/` cached for a year | `base.html`, `static_storage.py`, `nginx_fishauctions.conf` | done -- jQuery is the one that cannot move, and `base.html` says why |
| 7 | Buyers and sellers: fire `pageView` on every page and drop the 2s delay, then read the funnel off rows that already exist | `base_page_view.html`, `usability_report.py`, `views/lot_pages.py`, `view_lot_images.html` | done |
| 8a | The stage ladder on `Club`, the map gate, the stall reason, and the `aware` rung | `models.py`, `club_health.py`, `views/usability.py`, migrations 0430-0431 | done |
| 8b-8f | The link verifier, umbrella directories, the crawl, city search, the outreach loop | `management/commands/` | todo -- needs the network, see below |

Every phase above has a test module: `test_usability_instruments.py`, `test_form_friction.py`,
`test_usability_report.py`, `test_club_health.py`, `test_template_a11y.py`,
`test_page_view_beacon.py`.

### What Phase 3 turned on, since it was called blocked

It was not blocked. `auctions/field_adoption.py` reconstructs the answer from the rows that already
exist: compare every auction's stored value against the model default and count the ones that
differ. It cannot tell "deliberately chose the default" from "never looked", and it is blind to a
change somebody made and undid -- but the interesting answer is the zero, and a zero here is real.
That is the retroactive half; `changed_fields` is the exact half, from the day it shipped. The
dashboard shows them side by side, because a field with far more edits than off-default values is
one people keep changing their minds about, which is a different problem from one nobody wants.

The split itself is a declared list (`auction_form_layout.ESSENTIAL_FIELDS`) rather than something
derived per deployment, so the form is the same for everybody and can be tested. Three rules keep
*Advanced* from hiding something somebody needs, each with a test: every field is in exactly one
half, a setting already off its default forces the section open, and so does a field with an error.

### Why Phase 2 is about clubs, not users

Users are already covered: `UserData.last_activity` is genuinely maintained (page views via
`views/ajax.py:375`, lot pages, contact edits), and the signups chart already plots
"Stale (400+ days inactive)".

Clubs have nothing. `Club.active` is a hand-set boolean defaulting True (`models.py:533`);
`date_contacted` and `date_contacted_for_in_person_auctions` are hand-maintained and surfaced only
through an `EmptyFieldListFilter` in Django admin (`admin.py:894`). Nothing is derived.

(Human note: Active is set when a club dissolves and needs to be hidden from the map.  Rare and unrelated to this.)

That is the wrong way round. A dormant bidder is one person; a dormant club is an organizer, its
members, and a recurring auction that stops appearing -- silently, because `active` still says True.

Two design constraints for the rollup:

- **Alert on deviation from a club's own cadence, not a global cutoff.** A club that runs monthly
  and has missed two is in trouble; a club that runs one big annual auction is healthy at eight
  months. One threshold gets both wrong, which is probably why nobody trusts `active` enough to
  maintain it.
- **A threshold with no trigger is another chart nobody opens.** The rollup populates a "due for a
  check-in" queue writing into `date_contacted`, which is the outreach hook that already exists.

Note for whoever builds the re-engagement side: `weekly_promo.py:48` excludes anyone active in the
last 6 days, so that email's audience is by construction the semi-lapsed. That makes it the natural
re-engagement channel -- and worth confirming the exclusion is deliberate before building on it.

Let's talk more about clubs:
There's probably 200-300 clubs out there that I don't know about and they aren't on the list.  I don't know how long the average fish club exists for, I don't know how to get in touch with them.  Medium term I want an ongoing campaign to find new clubs, contact them, onboard them, track if they've made a test auction, if they've done a real auction, how many real auctions, if they've used any club management tools and if so which ones, and if they're not using the site as much, to speculate on why or reach out and try to re-engage

(That is phase 8 below.)

## Phase 7 -- buyers and sellers, and the instrument that can see them

Two rules from the human, first, because between them they cut most of what a usability pass would
otherwise propose:

- **More than one sentence will not be read by anybody.** Nothing in this phase adds a paragraph to
  a page.
- **Silence is the design.** Machinery that just works stays invisible. The `AuctionTOS` auto-link
  on sign-in and the rhyming name search are documented to users nowhere, on purpose; explaining
  them would add a decision to a page where there wasn't one. A feature that needs announcing is a
  feature that isn't finished.

### 7a. Fix `PageView` rather than build a second instrument -- done

The reach numbers are biased two ways (§3), and neither reason survives contact:

- **The two-second timer** (`base_page_view.html`) deletes exactly the fast bounces a funnel is
  about. No rationale for it was ever written down and none is worth keeping. Drop it.
- **The 37-of-248 opt-in was to keep an organizer's own admin page-views out of an organizer's own
  reports** -- a good goal, aimed at the wrong layer. *Every* auction-admin-facing read of
  `PageView` filters on `lot_number` or `auction`: `auction_stats.py:171`, `:182`, `:313`, `:599`,
  `exports.py:199`, `account.py:452`, `:490`, `browse.py:195`. Those two FKs are set only by the
  four enriched call sites (`pageView({'lot': ...})`, `pageView({'auction': ...})`), so a view of
  an edit page records a row with both columns null and **no organizer's report can see it**. The
  separation already exists in the data; the template opt-in was a second, blunter copy of it. No
  admin-only flag is needed on the write side at all -- and if one is ever wanted on the *read*
  side, it belongs on the route, where `usability_report.route_name` classifies with Django's own
  resolver and so cannot drift from `urls.py`.

The change: `base.html` fires one automatic view on load, and a template that calls
`pageView({...})` with an auction or lot **replaces** that automatic one rather than adding a
second -- so all 37 existing call sites keep working untouched. `REACH_CAVEATS` in
`usability_report.py` comes out in the same commit; a caveat that is no longer true is worse than
no caveat.

**Growth, corrected.** An earlier draft of this section made a retention purge a prerequisite. It
is not one, and the write-rate claim behind it was wrong. 248 is the *template* count; only 138
extend `base.html`, and 33 of those already call `pageView`, so the beacon reaches **105 new
pages** -- and they are the quiet ones. Every page that carries this site's traffic is instrumented
already: the lot page, `all_lots`, `auction`, `all_auctions`, `invoice`, `user`, `chats`. What gets
added is club settings pages, confirm-delete pages, API-key pages, print setup -- plus
`account/login` and `account/signup`, which are the two pages this phase exists to measure.

Crawlers are not a reason to hold back either -- but not because they cannot run the beacon. They
can and they do: `ajax.py:485` drops a view whose user agent contains `Googlebot` or `Baiduspider`
before the row is written, and migrations `0182` and `0232` exist to delete the rows that got in
before that check did. Widening the beacon puts a two-name substring check in front of a hundred
more routes, which is an argument for a better check, not for less instrumentation.

The rows stay. They have already been mined once to backfill auction stats, and phase 8 wants them
for club and user retention; a purge throws away the only record of what people did before they
did anything countable. What growth does cost is four things, none of which is disk and none of
which is fixed by deleting rows:

- **`PageViewAdmin` counted the whole table twice on every page load** -- no
  `show_full_result_count = False` and no cheap paginator, so Django's `ChangeList.get_results` ran
  `paginator.count` *and* `root_queryset.count()`: two unfiltered `SELECT COUNT(*)` over the biggest
  table on the site, to render twenty rows of it. **Done.** `auctions/admin_paginator.py` takes the
  engine's own row estimate when the changelist has no `WHERE` clause, falls back to a real count
  when the engine has none to give, and leaves a filtered or searched changelist counting exactly.
  `test_admin_paginator.py` asserts the changelist issues no `COUNT` against `auctions_pageview` at
  all, so deleting either attribute fails the build rather than quietly restoring the scan.
- **`remove_duplicate_views` had a ceiling** -- 5000 rows per run every fifteen minutes, about 5.5
  writes a second, with `duplicate_check_completed` unindexed. **Gone**, and not for the ceiling:
  the job was destroying the data this phase is for. See 7a.1.
- **Schema changes on that table are expensive.** Migration `0423_query_indexes` says so in its own
  docstring: InnoDB builds in place, budget for it, run it when the site is quiet. That is the real
  price of keeping the rows. It is paid rarely and it is worth paying.
- **Every recorded view costs one extra indexed `SELECT`.** `PageView.save()` looks up the last
  known location for that IP, and that runs on every insert -- so widening the beacon to ~105 more
  pages widens that too. It is one row off `pageview_ip_recent_idx`, on a write that already
  happened for every instrumented page; taking the beacon site-wide is a decision to pay it for the
  quiet pages as well. It is worth paying -- those pages are the ones the funnel could not see --
  and it is the one line to reach for if page-view writes ever become the problem.

The read side is where the size of this table actually bites, and 7b is the example: matching a
page view to an auction is `pageview.auction_id OR lot.auction_id`, an OR across a join no single
index serves, so both funnel queries carry a `date_start` floor. Unbounded they are a full scan of
`PageView` -- the shape behind a past production incident -- from a page an admin opens casually.

The one deletion that is clearly right is the one migration `0232_delete_baiduspider_pageviews`
already made: bots. Bot rows are not retention data.

#### 7a.1. The deduplicator destroyed the data this phase is for -- removed

`remove_duplicate_views` ran every fifteen minutes and was not doing what its name suggests.

- **It never touched a signed-in view.** `PageView.duplicates` returned nothing when `session_id`
  was falsy, and a signed-in view is stored with `user=<id>, session_id=NULL` (`ajax.py:437`). Only
  anonymous rows were ever merged.
- **It had no time window.** It matched on `(user, lot_number, url, auction, session_id)` and
  nothing else, and `SESSION_COOKIE_AGE` is `1209600 * 100` -- about 230 years
  (`settings.py:466`). An anonymous session key is effectively permanent, so *every* anonymous view
  of a page, however far apart, collapsed into one row. Not a double-click: a month of daily visits.
- **What it merged was empty.** `total_time` and `counter` are only ever raised by the ten-second
  heartbeat, which is commented out in both `base_page_view.html` and `ajax.py` ("not worth the
  extra server effort"), so both are 0 on every row written since. `counter` has no reader in
  production code at all; neither does `PageView.notification_sent`. `date_end` defaults to
  `timezone.now()` at insert and is never updated. The merge widened a date range, summed zeros,
  ORed a dead flag, and deleted rows.
- **It made the counts it was supposed to clean *less* consistent.** The readers that count raw
  rows -- `auction_stats.py:171`, `:182`, `:313`, `:599`, `usability_report.py:87`,
  `account.py:452` -- saw anonymous repeat visits folded to one and signed-in repeat visits counted
  in full. A duplicate from a double-click moves `allViews` by one. This moved it by a visitor's
  entire history.

**Done.** The beat entry, the task, the management command and the three model methods are gone,
with `test_page_view_dedupe.py` and the two cases that covered them. The reasoning now lives in
`PageView`'s own docstring, and `test_page_view_history_is_kept.py` is the ratchet: two views of a
page in one session leave two rows, signed in or not. `FixedDatabaseScheduler` prunes the live
`PeriodicTask` row on its own, so there is nothing to do on the server.

That leaves `duplicate_check_completed`, `counter`, `notification_sent` and `total_time` inert.
Dropping them is an `ALTER` on the biggest table -- the quiet-window job above -- and is worth doing
separately, or not at all: unread columns cost nothing, and this campaign's whole position is that
rows are cheaper than the history they hold.

Tests: a route that renders `base.html` records exactly one view; a template naming a lot records
one row with the FK set, not two; an anonymous request records one; and the auction stats totals are
unchanged by views of pages that name no lot and no auction.

### 7b. The funnel is then a report, not a table -- done

With the beacon on every page, the buyer funnel needs no new model. `PageView` and `FormFailure`
both already carry `session_id` for people who are not signed in, and every other stage has a row
already: `AuctionTOS.createdon` for joining, `Bid` for bidding, `Invoice` for winning, opening and
paying. The report is arrival (with referrer) -> lot page -> join -> bid -> win -> invoice opened ->
paid, per auction, anonymous sessions included, which is the entire point.

Test: a fabricated session walked end to end reports each stage once; one that stops after arriving
reports only that.

### 7c. The bid dialog, corrected -- done

**Preserved, because it works:** the Bid button is on the page whether or not you are signed in.
That is deliberate -- it is what a buyer wants to click, ShopGoodwill does the same -- and the
dialog behind it is what made "why can't I bid?" stop being the most common question organizers
got. Nothing here changes that behaviour or that answer.

Two corrections to the previous draft of this section, which overstated the problem:

- The dialog body is **already one sentence with a link** -- "You have to sign in to place bids."
  (`lot_pages.py:510`).
- **Cancel is already `btn-secondary`**, as `style_reference.md` requires. There is no colour bug;
  the two blue buttons are Sign in and Create an account, and "almost every button is
  `btn-primary`" is the rule, not the exception to it.

What is actually wrong is narrower. The dialog is titled **"Bid failed!"**, red, under an
exclamation icon (`view_lot_images.html:934`), for somebody who never bid. The other reasons that
box carries -- own lot, haven't joined, not checked in, needs approval -- are refusals and should
look like refusals; "you are not signed in yet" is not one. So: title from the reason rather than a
constant, and one primary button in the footer instead of two saying the same thing as the link in
the body (`:942`).

Scope, in the human's numbers: online auctions are about **5% of all auctions**, so most people
never see this dialog at all. Worth twenty minutes, not worth more.

Test: an anonymous GET of a lot page renders the sign-in dialog with a title that is not "Bid
failed" and one primary button; every other reason still renders as an error.

### Struck, with the reason, so they don't come back

- **Advertising the sign-in claim mechanic.** It should keep working silently and be mentioned
  nowhere. See "silence is the design" above.
- **A uuid "add your lots" link for sellers with no account.** A forwarded or stolen link would
  write data as somebody else, and the club would have to send it to an address nobody validated.
  Selling needs an account.
- **Keeping the unmatched `club_affiliation` text.** Ten entries and nine typos: somebody who knows
  about this site is in a club that is already on it.
- **Any new email to people with no account.** `weekly_promo` needs a `User`, an opt-in and a
  location, and stays that way. It exists to tell people about auctions near them, not to be a
  sending engine.

## Phase 8 -- club discovery

**Finding them is the bottleneck.** Adding a club takes ten seconds in the Django admin; getting one
to actually use the site takes months. Approval stays required -- every pin on the club map is a
claim this site is making, and the map is treated as authoritative -- so there is no self-serve club
signup in this phase. That was the wrong target.

### One table, one ladder

No separate lead model. A prospect goes in `Club` with everything else, distinguished by a stage
field. A split lead table is a 1:1 FK that duplicates name, location and URLs, and the boundary it
draws is not where the real one is. The real progression is a ladder with no clean break in it:

> unaware of the site -> aware -> one member has an account here -> several members using it ->
> made a test auction -> abandoned it -> several test auctions -> ran one real auction -> runs them
> to a schedule

`ClubHealth` already computes the right-hand half of that ladder from rows -- `empty`, `aware`,
`trial`, `new`, `active`, `slipping`, `dormant` -- nightly, and has to keep doing so: it is derived,
and it will overwrite anything hand-set. So the left-hand half is one hand-maintained field on
`Club`, the report shows whichever of the two is further along and says which one said so, and
**that same field is the map gate**: a club nobody has approved is not on the map, not in
`GetClubs`, not in any club dropdown. (Built in 8a: `Club.outreach_stage` and
`Club.objects.listed()`.)

Part of the middle *is* derivable, and it is the part that says a club is warm: a `UserData.club` FK
pointing at it, `ClubMember` rows, an `AuctionTOS` belonging to one of its members in somebody
else's auction. That separates "aware" from "unaware" without asking anybody.

### Why they stall is the part no query can answer

The stage says where a club stopped. Nothing in the database says why, and speculating from the
rollup would be fiction. The only source is a reply to an email, so the queue needs somewhere to put
one: a short reason on the club from a small fixed vocabulary -- never replied, no auction coming
up, uses something else, paper works fine, cost, not interested, folded -- set by hand when somebody
answers. `Club.notes` is free text and cannot be counted; these counts are the only thing that will
ever say which objection is worth fixing.

### Sources, in order

1. **Umbrella-organisation directories.** The best of these. FAAS, the NEC (already modelled as
   `is_nec_club`) and the national specialty societies -- ACA, ALA, AKA, AGA, NANFA -- each publish
   a member-club list on one page. On "is this doable": yes, and it is the easy end of scraping.
   Under a dozen static HTML pages, one fetch each, no login, no JavaScript, no pagination, nothing
   that needs a browser. It is a `requests.get` and an extraction step, and `auctions/llm.py`'s
   `complete_json` already does exactly that shape of extraction elsewhere here (donation emails,
   speaker talk lists). The risk is not the fetching; it is that the directories themselves are
   stale, which is what the verifier below is for.
2. **Club link pages, crawled outward.** Clubs link to each other. Seed from the clubs already known
   plus whatever (1) returns, fetch each one's links/friends page, keep what looks like a club,
   repeat. This is the one that gets from sixty to three hundred. It is a real crawler, so it gets a
   fetch budget, a per-host delay, `robots.txt`, and a hard stop at two hops.
3. **Search by city.** Walk the largest US metros against "aquarium club", "fish club", "aquarium
   society". No cleverness, and likely a better return than (5). Google Places text search is one
   API call per city and gives a name, an address and a website; a plain web search is messier and
   needs no key.
4. **Facebook groups and Places listings.** In, not deferred, and for the stated reason: it costs
   almost nothing per club and the data is no worse than anything else on this list. Facebook has no
   public search API, so this one ends at a results page and a human eye rather than a pipeline.
5. **Speaker itineraries.** A maybe. `Speaker.website` exists and a travelling speaker's site
   sometimes lists every club they have spoken to, but the hit rate is a guess and (3) probably
   beats it for less work. Try it after (1) and (2); drop it if the first ten speakers turn up
   nothing new.

### Verify and clean up the list you already have

Before adding three hundred, audit the ones already there: fetch every `Club.homepage` and
`facebook_page`, record whether it answered, and flag the dead. A club with no reachable site, no
Facebook page and no auctions is a candidate for `active=False`, which is what that flag is for.
This is the same fetch-and-record step every source above needs to stay honest, so it gets built
once and pointed at both: **every club, found or existing, carries a last-verified date and a way to
die**, or the queue fills up with clubs that folded in 2004. It also answers "who is still out
there" about the list that exists today, which is the cheapest question here and the only one that
needs no network beyond our own links.

### Automate finding; do not automate the email

Fetching, deduplicating (against `Club` by domain and by fuzzy name), geocoding
(`auctions/geocoding.py`) and extraction all run unattended. The message does not. Three hundred
identical unsolicited emails is spam, it risks the SES sending domain that `SES.md` documents at
length, and it is the wrong pitch anyway -- what works names that club's own next auction and offers
to run it. Draft per club, send by hand, record the attempt, and reuse the cooldown
`club_health.CONTACT_COOLDOWN_DAYS` already implements.

### Order of work

- **8a.** **Done.** The stage field on `Club`, the map gate, and the stall reason.
  `/admin-club-health/` shows the whole ladder instead of only its right-hand half.
- **8b.** The verifier, run against the clubs already on the site. Cheapest, and it is the code
  every later step reuses.
- **8c.** Umbrella directories: one command per organisation, idempotent, dedup-ing on domain and
  fuzzy name.
- **8d.** The link-page crawl, budgeted, two hops.
- **8e.** City search, then Facebook and Places by hand.
- **8f.** The outreach loop: draft per club, send by hand, record the attempt and the stall reason,
  and report the ladder as counts month over month.

Tests: dedup by domain and by fuzzy name against an existing club; an unapproved club appears on no
map, in no `GetClubs` result and in no dropdown; the ladder orders correctly and the derived half
never overwrites a hand-set earlier stage; the verifier marks a 404 host unreachable and leaves a
slow one alone; and extraction, given a saved copy of a real directory page, returns the club names
and their URLs. Network mocked throughout -- `aquarium_species.py`'s scrapers are the precedent.

## How a pass works

One pass = one phase, or one coherent piece of a phase.

1. Read the surface and what it renders. Note what a first-time user sees before they scroll.
2. Check what is already measured about it. Assume the answer is "more than you think, in a form
   that cannot be queried" -- that was true three times out of three while scoping this.
3. Fix the measurement first if there is one. A design change made ahead of its instrument cannot
   be evaluated and cannot be defended later.
4. Make the change.
5. **Prove it with a test.** Same repo rule as everywhere else: no prose about code a test cannot
   check. A URL classifier with no test is what bug 1 is. Anything that classifies, buckets or
   thresholds gets a test with a real-shaped input.
6. Run `docker compose run --rm test --ci --verbose` and the touched test modules.
7. Update this file: move the phase to done, write what changed under "Pass log", add anything newly
   found to the queue.

## Measuring

**Reach, then failure, then adoption.** Three questions, three sources:

- **Reach** -- did anybody open this page? `PageView`, grouped by route. Works after Phase 0, and
  unbiased after 7a: every page that extends `base.html` records one view, with no timer.
- **Failure** -- did they give up? The Phase 1 instrument, and it has **two halves**, because on
  this site the obvious one is the rare one. A *rejection* is the server refusing a submission,
  which hardly ever happens here on purpose: nearly every field is optional and most of the rest
  are filled in on save. An *abandonment* is somebody editing a form and leaving without saving --
  the ordinary way of giving up, invisible to the server, and reported by the page itself
  (`unsaved_changes.js`) as it goes away. A form with rejections and no abandonments is one people
  can see how to fill in and keep getting wrong; one with abandonments and no rejections is one
  they cannot see how to fill in at all, and no validator will ever say so.
- **Adoption** -- did anybody change this setting, ever? `changed_fields` after Phase 1b. The
  question behind every "should this field exist" argument.

A surface with high reach and no failures is fine. High reach with repeated failures on one field
is the whole point of this campaign. Low reach on a setting nobody has ever changed is a deletion
candidate, not a redesign candidate.

## Judgement calls waiting on the human

Made to keep going, each one reversible in one place. Named here rather than buried in a docstring
because they are decisions about what this site should do, not about how to write it.

- **Only `listed` puts a club on the map, and every club already here was moved to `listed`.**
  The alternative was to default new clubs to `listed` and mark prospects by hand, which fails
  the other way: a club found by a crawler would be published before anybody looked at it. The
  cost of this choice is that adding a club in the Django admin now takes one more dropdown, and
  forgetting it means the club is invisible. `Club.objects.listed()` is the only gate, so the
  policy is one method.
- **A club's public page is not gated.** `/clubs/<slug>/` still renders for a prospect club.
  Nothing links to it -- the map, the search and the dropdowns are all gated -- but a guessed URL
  reaches it. Gating it would also hide it from the person doing the outreach, who needs to look
  at it. Say the word and it becomes admin-only for unlisted clubs.
- **Ladder order: `dormant` < `slipping` < `active`, and `empty` is not a rung.** A club that
  stopped still got further than one with its first auction, so both sit above `new`; `empty` is
  the absence of a signal rather than a rung, since every prospect starts there and ranking it
  would report a club nobody has heard of as further along than one somebody just wrote to. The
  order is one tuple, `club_health.LADDER`.
- **The third "aware" signal was folded into the other two.** Phase 8 above names three: a
  `UserData.club` FK, `ClubMember` rows, and an `AuctionTOS` belonging to one of that club's
  members in somebody else's auction. The third is the same set of people seen *doing* something
  rather than a wider set of people, so it says "warm", not "aware", and `people_here` counts the
  first two. If the warm signal is worth its own rung, it is a fourth column, not a wider count.
- **The buyer funnel lives on `/admin-usability/`, not on an organizer's stats page.** It is a
  campaign instrument and it names arrival stages an organizer cannot act on. The same numbers per
  auction would be a reasonable thing to show an organizer later; that is a different design.
- **The funnel ignores the dashboard's day window** and reports each auction's whole life, because
  a funnel cut at 30 days reports the people who paid last month as a drop-off. The window still
  chooses which auctions appear.
- **A single-club deployment lists its own club automatically.** `get_single_club` now forces
  `outreach_stage` to `listed` on every call, so that one club cannot be un-listed by hand. On a
  single-club site the club is the site; anywhere else that would be the wrong rule.

## Still needs a person, not a decision

- **8b-8f are network work.** The verifier, the umbrella directories, the link crawl and the city
  search all fetch pages this repo cannot see from here, and every extraction rule is a guess until
  somebody has looked at the real page. The right first step is 8b, the verifier, run against the
  clubs already on the site: it needs no directory, it is the code every later source reuses, and
  it answers "who is still out there" about the list that exists today.
- **The outreach email itself stays hand-sent** (already settled above), so 8f cannot be finished
  by a machine either.

## Open questions

- Roughly how many active organizers per year? Decides whether any organizer-facing metric can ever
  be more than anecdote.
- Is the `weekly_promo` 6-day exclusion deliberate? Phase 2's re-engagement half assumes it is.
- **Answered:** `PageView` retention. Nothing purges it and nothing will; the rows are worth more
  than the space, and the three costs that do grow with them have fixes that are not deletion
  (7a). `FormFailure` inherits the same answer. Still open: how far back the rows actually go,
  because that is the longest window a "nobody has ever used this" claim can honestly cover.
- **Answered:** nothing new gets emailed to somebody who never signed up. `weekly_promo` keeps
  its `User` + opt-in + location requirement. Still open for phase 8 only: whether an
  unsolicited email to a club's *published* contact address is acceptable, and under what
  footer.
- How long does a fish club last? It decides how stale an umbrella directory has to be before
  reading it costs more than it returns, and how fast a found club should be re-verified.
- The Phase 4 pass added five help notes on the pages a first-timer hits. The other ~50 templates
  with a form have not been looked at. There is no quota either way -- most forms need no note at
  all, and a page that genuinely has three or four things worth saying should say them.

## Pass log

Newest first.

<!-- PASS LOG START -->

### 2026-09-09 -- review round on phase 7 and 8a

Four findings, all cheap and all about the two tables that never shrink. `ladder_position` could not
tell "no rollup" from "not looked up yet", so counting the ladder fired one `SELECT` per club
without one -- which is every club club discovery is about to add; it takes a sentinel now and
`assertNumQueries(2)` holds it there. Both funnel queries match an auction as `pageview.auction_id
OR lot.auction_id`, which no single index serves, so unbounded each was a full scan of `PageView`
from a page an admin opens casually -- they carry a `date_start` floor now, and `funnel_referrers`
has no default for it on purpose. `club_mark_contacted` treated an *absent* `stall_reason` as the
empty one, which is a legal value in that vocabulary ("Not known"), so a POST that said nothing
about the reason erased one somebody had recorded. And the beacon's write-path cost -- one indexed
`SELECT` per view, from `PageView.save()`'s location lookup -- is now written down in 7a as the
fourth thing growth costs, rather than left implied.

### 2026-09-09 -- Phase 7 whole, and phase 8a

**7a.** `base_page_view.html` fires one view at `DOMContentLoaded` on every page that extends
`base.html`, and the two-second timer is gone. The first `pageView()` call of a page wins, so the
37 templates that name a lot or an auction keep working untouched and *replace* the automatic view
rather than adding a second row -- except `view_lot_images.html`, whose call was inside
`window.onload` and therefore arrived after the automatic one had already gone. It moved to parse
time, and `test_page_view_beacon.py` is the ratchet: an enriched call that ends up behind a load
handler fails the build, because the FK it carries is what every organizer-facing report filters on.
`REACH_CAVEATS` came out in the same change.

**7b.** `usability_report.buyer_funnel()` -- arrived, opened a lot, joined, bid, won, opened an
invoice, paid, per auction, over that auction's whole life rather than the dashboard's window,
because people arrive weeks before they pay. Seven `GROUP BY`s for the whole page, not seven per
auction. Anonymous arrivals count as people, which is the entire point of 7a; **Bid** reads blank
rather than zero for an in-person auction, where bidding leaves no row until somebody wins. Writing
its test found a real bug: Django's `Concat` folds a NULL argument to an empty string, so the
obvious `Coalesce(Concat("u", user_id), session_id)` counted every anonymous visitor to an auction
as one person. It is a `Case` now, and the test that caught it says why.

**7c.** The sign-in dialog is titled "Sign in to bid" instead of "Bid failed!" in red under an
exclamation icon, and has one primary button instead of two. Every other reason in that box is a
refusal and still looks like one.

**8a.** `Club.outreach_stage` is the hand-set half of the ladder -- prospect, contacted, listed --
and only `listed` publishes a club: the map, the club autocomplete, the palette's club search and
`clubs_near_me` all go through `Club.objects.listed()`, which asks `active` as well. Migration 0430
moves every club already on the site to `listed` in the same step that adds the column, because the
field's default is `prospect` and deploying without that would empty the map. `Club.stall_reason` is
the fixed vocabulary, set from a select beside the Mark-contacted button on the queue, and anything
outside the vocabulary is ignored rather than stored. `ClubHealth` gained an `aware` stage and a
`people_here` count, which is what separates a club whose members are already here from a name
somebody typed in -- two completely different conversations that used to be one bucket. Existing
rollup rows still say what they said until the nightly task next runs, so the `aware` rung fills in
overnight rather than at deploy; `club_health.refresh_all()` does it now if that matters.


### 2026-09-08 -- review round on the above

Seven findings, one of them fatal: the abandonment beacon sent no CSRF token to a DRF view using
`SessionAuthentication`, so every abandonment by a signed-in organizer would have 403'd in
production. Nothing caught it because Django's test client disables CSRF enforcement unless it is
built with `enforce_csrf_checks=True` -- worth remembering for anything else on this site that
posts without a form. Also: the sitewide submit handler disabled Save on the ten forms that post
with `fetch` and call `preventDefault()`; `due_for_checkin()` sliced before sorting and so cut the
two stages it leads with; `aria-busy` was never cleared for a request that ended without a swap;
deleted club members were counted as members; "edited for" was a mean over a distribution whose
tail is tabs left open over lunch; and an anonymous run was stored under an empty session id, which
one later success would have used to resolve every other anonymous person's failures. All seven
have a regression test.

The one claim in the previous entry that was wrong: the auction edit form does **not** record its
date fields as changed on every save. That was a bug in the test helper, which built its POST from
`form.initial` rather than from what the widgets render. `test_resubmitting_the_form_unchanged_changes_nothing`
now pins the real behaviour.


### 2026-09-08 -- Phases 1, 1b, 1.5, 2, 3, 4 and 5

**1b, and the argument it settles.** `AuctionHistory.changed_fields` and `ClubHistory.changed_fields`
are JSON, keyed on the model field name, carrying before and after; `auctions/history.py` builds
them and says why the prose column could not answer a query. The prose is untouched -- it is still
what the history page renders. `record_club_history` gives the club side the same thing, and the
three club settings views that wrote the constant string "Updated club settings" now say which
settings.

**3, which was not blocked.** `auctions/field_adoption.py` reconstructs "has anybody ever changed
this" from the rows: every auction's stored value against the model default, one aggregate query
for 43 fields. `AuctionEditForm`'s layout moved to `auctions/auction_form_layout.py` (which also
took 294 lines off `forms.py`, ten under its ceiling) and split into 18 essential fields and 25
behind a `<details>`. It opens itself when a hidden field is off its default, when a hidden field
has an error, or for an organizer on their third auction.

**1, and what the first attempt would have missed.** The `form_invalid` mixin is on 13 views. On its
own it would have measured almost nothing: this site makes nearly every field optional and fills the
rest in on save, so the server rarely gets to refuse anything, and somebody who edits a form and
closes the tab leaves no trace at all. So the page reports that too. `leave_page_warning.js` -- a
per-template include, on 7 of the 99 templates that render a form -- is now
`static/js/unsaved_changes.js` in `base.html`, finding its own forms: POST, two or more editable
fields, no opt-out. It draws a fixed bar with Save and Discard when a value actually differs from
the one it was rendered with (the old one armed the browser's unload dialog on *blur*, which trains
people to click through those dialogs), and beacons the field names -- never values -- to
`FormAbandonedBeacon` on the way out. HTMx is most of the forms here and breaks every page-lifecycle
assumption, so four cases are handled separately: forms that arrive in a swap, forms saved without
an unload, forms swapped away while dirty (reported there, since nothing else ever will), and an
hx-get link about to replace a dirty form (asked about, since the browser sees no navigation).

**1.5** is `/admin-usability/`: reach by *route* rather than by URL -- the classifier is Django's own
resolver, so it cannot drift from `urls.py` -- failure by form with both kinds side by side, and the
adoption table. **2** is `/admin-club-health/`: a nightly rollup judging each club against its own
median gap between auctions, so an annual club is healthy at eight months and a monthly one that has
missed two is not, ending in a worklist that writes `Club.date_contacted`. Test auctions are counted
separately, because a club that set the site up and never ran a real auction is the most recoverable
case on the list and used to be indistinguishable from a working one.

**5** fixed ten images with no `alt` and five icon-only controls with no accessible name, and added
`auctions/template_a11y.py` so they cannot come back -- a pre-commit hook, a `--ci` step and a test,
the same three-way shape as `template_lint`. HTMx swaps now announce themselves into one live region
in `base.html`; there were three `aria-live` regions on the site and none of them was on the HTMx
surface. **4** added five help notes in the existing `help-note` format, one per page, on the pages
a first-timer actually lands on.


### 2026-09-08 -- Phase 0, third half: One Tap rationed on intent

`context_processors.google_one_tap` replaces the `hide_google_login` flag and the six views that set
it (`FAQ`, `PromoSite`, `AllAuctions`, `AllLots`, `ClubMap`, `UserAgreement`). The prompt now needs
one page load behind the visitor, or the sign-in/sign-up page; HTMx fragments and crawlers do not
count, the session counter stops at the threshold so a visitor costs one extra write once, and the
app is excluded because it signs in natively. Eleven tests in `test_helpers.py`, including an
end-to-end pair over `base.html`.

### 2026-09-08 -- Phase 0, second half: `AdminUserFlow` deleted

The view, `dashboard_user_flow.html`, the `admin_user_flow` URL and both of its palette entries,
the `compute_user_flow_all` task with its heartbeat lock, and the links to it in `base.html` and the
app's admin menu. `test_user_flow.py` kept the half of itself that is about the shape of
`PageView.url` -- the endpoint, migration 0425 -- and is now `test_page_view_url.py`.
