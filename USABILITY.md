# Usability campaign

A resumable sweep aimed at one thing: **a non-technical person can run an auction here without
being taught.** The site is feature-complete; this is the pass over what those features feel like to somebody who has never seen them.

Human note, Let's also clear up one thing - the goal is not just "a non-technical person can run an auction here without
being taught" - it's "a non-technical person can participate in an auction as a buyer only, or as a seller, with or without an account, and have an excellent experience that makes them want to come back".  Smooth UX is quite literally the only thing this project offers over a spreadsheet.  Once the stuff listed here is done, we need to do this again thinking about how to engage all users, not just auction admins.

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
| 6 | First paint: defer the six head scripts, `ManifestStaticFilesStorage` | `base.html`, `settings.py` | see OPTIMIZATION.md |

Every phase above has a test module: `test_usability_instruments.py`, `test_form_friction.py`,
`test_usability_report.py`, `test_club_health.py`, `test_template_a11y.py`.

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

- **Reach** -- did anybody open this page? `PageView`, grouped by url. Works after Phase 0. Read it
  knowing the 2-second and 38/247 caveats above.
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

## Open questions

- Roughly how many active organizers per year? Decides whether any organizer-facing metric can ever
  be more than anecdote.
- Is the `weekly_promo` 6-day exclusion deliberate? Phase 2's re-engagement half assumes it is.
- `PageView` retention: nothing purges it, and `FormFailure` now has the same problem. Adoption and
  staleness horizons both depend on how far back the rows go.
- The Phase 4 pass added five help notes on the pages a first-timer hits. The other ~50 templates
  with a form have not been looked at. There is no quota either way -- most forms need no note at
  all, and a page that genuinely has three or four things worth saying should say them.

## Pass log

Newest first.

<!-- PASS LOG START -->

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
