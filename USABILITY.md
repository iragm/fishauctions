# Usability campaign

A resumable sweep aimed at one thing: **a non-technical person can run an auction here without
being taught.** The site is feature-complete; this is the pass over what those features feel like
to somebody who has never seen them.

**The standing rule: measure before changing.** This codebase has more usable telemetry than it
looks, and three separate places where it is collected and then thrown away at the last step. The
first phases below fix those, because every design argument after that is otherwise a matter of
taste. **To resume: take the lowest-numbered phase in the queue that is not `done`.**

## What is already right

Worth writing down, so no pass re-invents it:

- A per-auction setup checklist with dismissible banners (`auction_ribbon.html`), gated on
  `Auction.admin_checklist_*` and `UserData.is_experienced`.
- `CreateAuctionForm` is deliberately two fields (`forms.py:2445`). Creation is not the problem.
- `/support/` collects every route to help on one page and works with no session (`SupportView`,
  `views/site_pages.py:95`), which App Review requires.
- `AdminSetupChecklistView` does the same job for whole-site configuration.
- Jargon is contained. "AuctionTOS" appears in comments, not in copy.
- `AdminUserSignupsJSON` (`views/site_admin.py:200`) already plots an activation funnel: total users,
  joined an auction, won or sold a lot, stale.

## Three bugs found while scoping this

These are the perishable part of this document. All three are "the data is being collected and
discarded at the last step", which is why none of them looked broken from the outside.

### 1. `PageView.url` stores an absolute URL, and one consumer assumes a path

`base_page_view.html` sends `data.url = newUrl`, derived from `window.location.href`. The server
strips only the query string (`views/ajax.py:337`), so rows hold `https://auction.fish/lots/123`.

`AdminUserFlow.URL_SECTIONS` (`views/site_admin.py:507`) anchors every pattern at `^/` and matches
with `pattern.match(url)`. **An absolute URL never matches, so 100% of rows classify as `"Other"`.**
The flow page has never worked on any dataset, and there is no test for `classify_url` -- which is
exactly why it shipped that way and stayed.

The fix belongs on the write side (store a path). Old rows stay absolute, so anything reading `url`
has to tolerate both until they are backfilled or aged out.

This is a prerequisite, not a nicety: `url__startswith='/account/'` is what makes "how many people
opened preferences" a query rather than a broken one.

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

Neither is worth fixing globally on its own. Both are worth knowing before trusting a number.

## Decisions already made

Recorded so they are not re-litigated.

### A/B testing: shelved, revisit in years

The engine already exists in disguise -- `AdCampaignGroup` / `AdCampaign` / `AdCampaignResponse`
(`models.py:13407`+), where `bid` is literally traffic allocation and `click_rate` is a conversion
rate. The only missing piece is sticky assignment: `bid` re-rolls per impression, which is correct
for an ad and fatal for an experiment. `hash(experiment_slug + (user_id or session_id))` bucketed
against cumulative weights would fix it in a day.

It is shelved anyway, because of sample size. At 80% power, alpha 0.05, per arm:

| Surface | Metric | Baseline | Lift detectable | n/arm | Verdict |
|---|---|---|---|---|---|
| Promo email | open rate | ~35% (assumed) | +20% rel. | ~750 | ~3 weeks -- viable |
| Promo email | click rate | ~5% | +20% rel. | ~7,600 | ~6 months -- no |
| Promo email | click rate | ~5% | +50% rel. | ~1,200 | ~4 weeks -- large swings only |
| Promo email | joined an auction | ~1% | +50% rel. | ~6,300 | no |
| Organizer flows | anything | -- | -- | -- | dozens of organizers a year; never |

The promo email sends 400-800/week. Sticky assignment means each **person** counts once, so the
ceiling is the size of the eligible pool, not sends x weeks -- re-sending to the same person next
week adds no independent sample. That caps the whole programme at roughly one subject-line test a
month, and buys nothing at all for the organizer-facing surfaces that actually need the work.

Anything organizer-facing gets the friction instrument plus instrumented before/after instead.

### Google One Tap placement: count it, do not test it

`base.html:283` renders One Tap on every page for anonymous users, with six ad-hoc opt-outs
(`context["hide_google_login"] = True` in `views/site_pages.py` and `views/site_admin.py`).

Not testable -- signups are rare events, same wall as above. It does not need to be: One Tap posts
to `google_login_by_token`, a different endpoint from allauth's regular Google login, so One Tap
signups are already distinguishable from sign-in-button signups. Count them for a month with the
originating page.

The decision rule is asymmetric, which is what makes counting sufficient. The cost is known now: a
third-party script on every anonymous page load and a floating prompt over content on mobile. So
only the *existence* of the benefit has to be established, not its size. Near-zero One Tap signups
off login/signup pages -> restrict it to those two. Otherwise keep it.

Whichever way it goes, the six scattered opt-outs become one positive list.

### `AdminUserFlow`: delete it

Fixing bug 1 makes it run, and it still should not exist. Its input is biased by both halves of bug
3, `_compute_flow(None)` pulls every `PageView` ever into Python with no date bound, it is not in
the beat schedule, and it caches with `timeout=None` in Redis so a restart empties it. It infers
"where do people get stuck" from navigation; Phase 1 answers that question directly.

`dashboard_traffic` stays. It groups by raw `url` and `title` and works today, precisely because it
never tries to classify.

## Phase queue

Ordered by (unblocks-other-work x value). Status: `todo` | `wip` | `done`.

| # | Phase | Touches | Status |
|---|---|---|---|
| 0 | Store a path in `PageView.url`; delete `AdminUserFlow` + `compute_user_flow_all`; add the One Tap counter | `base_page_view.html`, `views/ajax.py`, `views/site_admin.py`, `tasks.py` | todo |
| 1 | Friction instrument: which form, which field, how many attempts, did they finish | new model + a `form_invalid` mixin, `views/` | todo |
| 1b | `AuctionHistory.changed_fields` / `ClubHistory.changed_fields` as JSON, written alongside `action` | `models.py:6566`, migration | todo |
| 1.5 | Settings-reach panel over `PageView` (needs Phase 0) | `views/site_admin.py`, a dashboard template | todo |
| 2 | Club health: derived lifecycle rollup + "due for check-in" queue | new model, `tasks.py`, `Club`, `admin.py` | todo |
| 3 | Progressive disclosure on `AuctionEditForm` -- essentials vs. Advanced | `forms.py:2560`, `auction_edit_form.html` | blocked on 1b |
| 4 | Contextual help: `FAQ.help_key` + a template tag rendering relevant answers in place | `models.py` FAQ, `templatetags/` | todo |
| 5 | Accessibility debt: 10 `<img>` with no `alt`, 7 icon-only controls with no name, 3 `aria-live` regions against heavy HTMX use | `templates/` | todo |
| 6 | First paint: defer the six head scripts, `ManifestStaticFilesStorage` | `base.html`, `settings.py` | see OPTIMIZATION.md |

Phase 0 unblocks 1.5. Phase 1b unblocks 3 but needs months of data first, so start it early even
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

*(No passes yet. Scoping notes that produced this file are the three bugs above.)*
