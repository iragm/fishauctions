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
| 0 | Store a path in `PageView.url`; delete `AdminUserFlow` + `compute_user_flow_all`; add the One Tap counter | `base_page_view.html`, `views/ajax.py`, `views/site_admin.py`, `tasks.py` | todo |
| 1 | Friction instrument: which form, which field, how many attempts, did they finish | new model + a `form_invalid` mixin, `views/` | todo |
| 1b | `AuctionHistory.changed_fields` / `ClubHistory.changed_fields` as JSON, written alongside `action` | `models.py:6566`, migration | todo |
| 1.5 | Settings-reach panel over `PageView` (needs Phase 0) | `views/site_admin.py`, a dashboard template | todo |
| 2 | Club health: derived lifecycle rollup + "due for check-in" queue | new model, `tasks.py`, `Club`, `admin.py` | todo |
| 3 | Progressive disclosure on `AuctionEditForm` -- essentials vs. Advanced | `forms.py:2560`, `auction_edit_form.html` | blocked on 1b -- Claude says it's blocked but it is NOT - we can reconstruct this from existing auctions trivially - walk the fields and see which ones have been changed from the default.  Not perfect but very close. |
| 4 | Contextual help: `FAQ.help_key` + a template tag rendering relevant answers in place | `models.py` FAQ, `templatetags/`
Human note: I have never seen people click on help buttons, have you?  This does not seem like a good idea.  Small contextual help using the help blurb format found on almost every template is perfect.
 | todo |
| 5 | Accessibility debt: 10 `<img>` with no `alt`, 7 icon-only controls with no name, 3 `aria-live` regions against heavy HTMX use | `templates/` | todo |
| 6 | First paint: defer the six head scripts, `ManifestStaticFilesStorage` | `base.html`, `settings.py` | see OPTIMIZATION.md |

Phase 0 - pretty much done now, code review it - unblocks 1.5. Phase 1b unblocks 3 but needs months of data first, so start it early even
though the payoff is late. Phase 2 is independent of everything and can run in parallel. Phases 5
and 6 need no data and can be picked up whenever.

### Why Phase 3 is blocked rather than todo

`Auction` has **105 fields** and `AuctionEditForm` covers ~90 of them in one page of flat `<h4>`
headings with jQuery show/hide. It is the biggest single drop-off surface on the site: a two-field
create hands straight over to it via the checklist's "Edit the rules".

The obvious fix -- essentials, with the rest behind Advanced, split on `UserData.is_experienced` --
requires knowing which fields anyone has *ever* touched. Current state cannot tell "deliberately
chose the default" from "never looked", and `changed_fields` gives nothing retroactive. So the
clock starts when 1b ships, which is the argument for shipping it early.

The expected finding is a long tail of fields changed by nobody, ever. Those are candidates for
hiding, or for deletion.

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
- **Failure** -- did they submit it and get bounced? The Phase 1 instrument. This is the only one
  that catches a page people reach, try, and give up on -- which looks identical to success in
  every reach metric.
- **Adoption** -- did anybody change this setting, ever? `changed_fields` after Phase 1b. The
  question behind every "should this field exist" argument.

A surface with high reach and no failures is fine. High reach with repeated failures on one field
is the whole point of this campaign. Low reach on a setting nobody has ever changed is a deletion
candidate, not a redesign candidate.

## Open questions

- Roughly how many active organizers per year? Decides whether any organizer-facing metric can ever
  be more than anecdote.
- Is the `weekly_promo` 6-day exclusion deliberate? Phase 2's re-engagement half assumes it is.
- `PageView` retention: nothing purges it. Adoption and staleness horizons both depend on how far
  back the rows go.

## Pass log

Newest first.

<!-- PASS LOG START -->

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
