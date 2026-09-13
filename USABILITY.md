# Usability campaign

**Goal:** a non-technical person can run an auction here, or take part as a buyer or a seller, with
or without an account, without being taught -- and wants to come back. Smooth UX is the only thing
this project offers over a spreadsheet.

## Phases

| # | Phase | Status |
|---|---|---|
| 0 | Path in `PageView.url`; `AdminUserFlow` deleted; One Tap rationed on intent | done |
| 1 | Friction instrument: which form, which field, how many attempts, and whether they gave up without submitting | done |
| 1b | `AuctionHistory.changed_fields` / `ClubHistory.changed_fields` as JSON | done |
| 1.5 | Reach / failure / adoption dashboard, `/admin-usability/` | done |
| 2 | Club health rollup + due-for-check-in queue, `/admin-club-health/` | done |
| 3 | Progressive disclosure on `AuctionEditForm` | done |
| 4 | Contextual help notes | scrapped -- the human writes these page by page |
| 5 | Accessibility debt (`template_a11y.py`) | done |
| 6 | First paint: deferred head scripts, content-hashed static | done |
| 7 | Buyers and sellers: beacon on every page, no timer; funnel read off existing rows | done |
| 8 | Club discovery: the outreach ladder, the map gate, CSV import | done, minus 8f outreach |
| 9 | Everybody who is not running the auction (`lifecycle.py`) | built -- `docs/phase_9.md` |

Each phase has a test module: `test_usability_instruments.py`, `test_form_friction.py`,
`test_usability_report.py`, `test_club_health.py`, `test_template_a11y.py`,
`test_page_view_beacon.py`, `test_lifecycle.py`.

## How a pass works

One pass = one phase or one coherent piece of one.

1. Fix the measurement before the design. A change made ahead of its instrument cannot be evaluated.
2. Make the change.
3. Prove it with a test. Anything that classifies, buckets or thresholds gets one.
4. `docker compose run --rm test --ci --verbose`, plus the touched test modules.
5. Update this file.

## Rules

- **One sentence, or nobody reads it.** No phase adds a paragraph to a page.
- **Silence is the design.** Machinery that works stays invisible -- the `AuctionTOS` auto-link on
  sign-in and the rhyming name search are documented to users nowhere, on purpose.
- **No A/B tests.** Dozens of active organizers a year, and bidders cluster inside auctions, so even
  a buyer-facing test has an effective n near the number of auctions. Compare a club against itself.
- **No unsolicited email to clubs**, at any volume. 8f drafts per club; a person sends it.
- **Nothing self-serve about clubs.** Every pin on the map is a claim this site makes, so approval
  stays required and `Club.objects.listed()` is the only gate.
- **No outbound HTTP to other people's servers.** Scraping and link verification were built, run,
  and deleted on this rule. `club_import.py` reads a CSV; a person checks the links.

## Numbers that set the scope

- **About 20% of auctions are linked to a club**, so every club-grouped number covers a fifth of the
  site. `/admin-unlinked-auctions/` is the fix and it is a person working a queue -- the cheapest
  high-value work left.
- **Dozens of active organizers a year.** Every organizer-facing metric is an anecdote with a
  denominator.
- **About 5% of auctions are online**, so anything about bidding affects almost nobody.
- **`PageView` goes back to 2020**, so a zero in the adoption table is a real five-year zero.
- **A fish club's median life is measured in decades, with front-loaded failures.** No census
  exists. Treat any directory entry older than three years as a coin flip.

## Measuring

Reach (did anybody open this page -- `PageView` by route), then failure, then adoption
(`changed_fields`: has anybody ever changed this setting).

Failure has two halves, and here the obvious one is the rare one. A **rejection** is the server
refusing a submission, which hardly happens -- nearly every field is optional and the rest are
filled in on save. An **abandonment** is somebody editing a form and leaving without saving,
invisible to the server, reported by the page (`unsaved_changes.js`). Rejections without
abandonments means people can see how to fill the form in and keep getting it wrong; abandonments
without rejections means they cannot see how to fill it in at all.

## Still open

- Work `/admin-unlinked-auctions/` down. Prerequisite for every club-grouped number.
- 8f outreach: draft per club, send by hand, record the attempt and the stall reason. The code half
  (`ClubLadderSnapshot`, month-over-month ladder counts) is done.
- The two judgement calls in `docs/phase_9.md` under "Waiting on a decision".
- The one survey question -- allowed once, after an invoice is paid. Needs the human to decide
  whether to ask at all, and what.

## Decided -- do not reopen

- **Advertising the sign-in claim mechanic.** It works silently and is mentioned nowhere.
- **A uuid "add your lots" link for sellers with no account.** A forwarded link writes data as
  somebody else. Selling needs an account.
- **Keeping unmatched `club_affiliation` text.** Ten entries, nine typos.
- **Any new email to people with no account.** `weekly_promo` needs a `User`, an opt-in and a
  location. Its 6-day active exclusion is deliberate: the audience is people who have not been here.
- **The four inert `PageView` columns** (`duplicate_check_completed`, `counter`,
  `notification_sent`, `total_time`) stay for ever. An `ALTER` on the biggest table buys nothing.
- **`PageView` rows are never purged.** They are the only record of what people did before they did
  anything countable. Name the degrading query instead.
- **`remove_duplicate_views` is gone**, and not for its ceiling: with a 230-year session cookie it
  collapsed a visitor's entire anonymous history into one row, merged fields that are always zero,
  and made counts less consistent, not more. `test_page_view_history_is_kept.py` is the ratchet.
- **A page's subject comes from its view** (`page_view_auction` / `page_view_lot`), never from
  whatever `auction` happens to be in the context -- that would fill `Auction.unique_views`, which
  organizers read, with organizers' own admin traffic. Three pages tag, and that list is the
  definition. `test_page_view_beacon.py` fails the build if a template calls the beacon.
- **Prospect club pages stay ungated.** Nothing links to `/clubs/<slug>/` for an unlisted club, and
  the person doing outreach has to be able to look at it.
- **A single-club deployment lists its own club automatically**; `get_single_club` forces it.
- **Ladder order is `dormant` < `slipping` < `active`, and `empty` is not a rung** -- a club that
  stopped still got further than one with its first auction. One tuple, `club_health.LADDER`.
- **The club dedup ignores a derived `abbreviation`.** `Club.save` auto-fills initials, so
  Milwaukee, Minnesota and Missouri Aquarium Societies are all `MAS` and a 300-row import would have
  merged them. Only an abbreviation a person typed counts. A merged club is one society's history
  attached to another.

## Trap worth remembering

`gpt-5-nano` at `minimal` effort is tuned for the palette (one sentence, a person waiting). Pointed
at a page of ~115 clubs it returned 3, 0, 0 and 0; at `low` it returned 54 then 12. `gpt-5-mini` at
`low` returned 117 and 114. And `medium` fails outright at both sizes: reasoning tokens come out of
`max_tokens`, so a budget that thinks harder than it can afford returns an empty reply, surfacing as
`LLMError`. The lever is the token budget, not the effort.
