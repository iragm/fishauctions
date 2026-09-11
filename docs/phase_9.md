# Phase 9 -- everybody who is not running the auction

Rewritten 2026-09-10 to the human's outline, replacing a first draft that measured the wrong thing.

**Built 2026-09-10.**  `auctions/lifecycle.py` is 9a-9d; `/admin-lifecycle/` and
`/admin-session-replay/` are the two pages; `SignInStitch` (migration 0438) is the single row this
phase adds; `auctions/test_lifecycle.py` is the ratchet.  What each section below settled is now
code, and where the code had to choose something this document left open, the choice is recorded at
the end under **Waiting on a decision** rather than buried in a docstring.

Phases 0-8 were about the person running an auction. This is about the other several thousand
people, and it starts from one sentence at the top of `USABILITY.md`:

> Smooth UX is quite literally the only thing this project offers over a spreadsheet.

A club can run an auction on paper. It cannot give a buyer a lot page on their phone at the table,
an invoice they can pay, or a reason to come back next spring -- and if this site does those three
things badly there is no argument left for using it.

**The number that sets the stakes: the average time somebody spends in this hobby is about a year.**
So the question is not "did this person convert". It is "did this person come back", and a year is
two or three auctions. Everything below is built to answer the second question, because the first
one is nearly worthless at this timescale.

---

## Who we are trying to help

Three people, and they need completely different things. Naming them first because most of the
instrument choices below follow from which one is being watched.

### The buyer

No account. Came from the club's Facebook page. Wants to buy things, in person or online depending
on the auction. Pays afterwards.

They are anonymous for the part of their visit that matters most, which is a measurement problem
before it is anything else -- see **the anonymous seam** below.

### The non-user

**Has no idea this site is involved at all.** Brings lots to sell written on a piece of paper. Their
first contact with the site is the invoice email, and possibly paying with Square. Whether they ever
make an account is irrelevant; whether they get the information they need is not, and the auction
organizers want this person helped more than anybody.

There is also a version of this person **who does not use a computer**. They are, by construction,
invisible to every instrument here. Say so out loud rather than quietly reporting a funnel that
omits them: the honest proxy is an `AuctionTOS` row with no user attached and an invoice that was
never opened, and that number should be shown next to any buyer funnel as the share of the room the
site never spoke to.

### The seller

Lists lots, gets paid at the end. Does not buy, does not care about the rest of the site. The
shortest path through the whole product, and the one most likely to be broken by a change made for
somebody else.

---

## The constraint that decides the method: no A/B testing

The human wanted to and was right to give up on it. Worth writing down *why*, because the temptation
comes back every time a change is arguable.

**The unit of randomisation is not the person.** For anything organizer-facing, n is dozens of
active organizers a year. No split of dozens reaches significance on any effect this site could
produce.

The non-obvious half is that **buyer-facing changes are barely better**, despite there being far
more buyers. Buyers are not independent: they arrive at one auction, run by one club, with one set
of rules, one pickup location and one Facebook post driving the traffic. Two bidders in the same
auction are more like each other than two bidders in different auctions, so the effective sample
size is closer to *the number of auctions* than to the number of bidders.

So the method is: read what individuals actually did, in order, with the gaps; then compare cohorts
across a club's own auctions, where the club is its own control.

---

## 9a -- Milestones, not a funnel

Phase 7b shipped a funnel in `auctions/usability_report.py`: arrival, lot page, join, bid, win,
invoice opened, paid, per auction, anonymous sessions included. That is the starting point and it
already works. What it gets wrong is the word *funnel*.

**The events do not have one order.** These are the conversions worth counting:

| Milestone | What it means |
|---|---|
| First page view | Plus where they came from |
| Account creation | Optional forever, and often skipped entirely |
| Read the auction rules | The one page that answers "am I allowed to do this" |
| Joined the auction | `AuctionTOS`, whether self-service or typed in at the door |
| Viewed a first lot | |
| Added lots | The seller's entire path |
| Won something | Bid placed, *or* a winner set by an organizer at the table |
| Viewed the invoice | |
| Paid | |

A seller adds lots and never views one. A buyer views forty lots and never adds one. The non-user
starts at "viewed the invoice" and has no earlier row at all. Some pairs really are ordered -- you
join before you win, you have an invoice before you pay it -- but most are not, and a strict funnel
reports "did it differently" as "dropped out".

**So model this as a set of milestones each person reached, with the few real prerequisites written
down as prerequisites, and report reach-rates rather than step-to-step conversion.** Where a genuine
ordering exists, measure the ordered pair. Everywhere else, count reach and the *time to* reach.

**Bidding is deliberately demoted.** Online bids are specific to online auctions, and only two clubs
run those heavily -- both doing well, both with engaged power users. There is nothing to fix there
and no signal in it for anybody else. **The terminal milestone is a paid invoice**, which exists for
every auction type. The existing funnel already returns `None` rather than zero for bids on
in-person auctions; keep that and stop treating the stage as important.

### Modelling "got bored and moved on"

This is the hard one, and the honest answer is that **an ending cannot be observed, only an
absence**. `PageView` gives a last-seen timestamp and nothing more; somebody quiet for two months
has either left the hobby or is waiting for the spring auction, and no query separates them.

The way out is to stop measuring in days and **measure in the club's own auctions**. A club running
four auctions a year makes a two-month gap meaningless; a club running one makes it enormous. So:

> A person has lapsed when their club has held **N auctions since the last one they took part in**,
> where N is a property of the club's cadence, not a global number of days.

That definition is directly computable from rows the site already has, it is comparable across
clubs with wildly different schedules, and it makes the user's own question -- *who participated in
N previous auctions and did not participate here* -- the same query as the churn definition rather
than a separate report.

Anyone whose last auction is the club's most recent one is **right-censored**: not lapsed, just not
asked yet. They must be excluded from churn rates rather than counted as retained, which is the
mistake that makes every retention number look better than it is.

---

## 9b -- Reconstruct what one person did

**The highest-value item in this phase, and it needs no new table.** The rows exist and are five
years deep: `PageView` carries `session_id` for signed-out visitors and `user` for signed-in ones, a
URL `path` since phase 0, and a timestamp.

Ordering one session's rows by timestamp is a session replay in the only sense that matters here:
the sequence of pages, with the gaps between them. At n=dozens, **twenty real sessions read end to
end teach more than any aggregate**, and this is the instrument that makes that possible.

What to build: given a session id or a user, return their rows in order with the elapsed gap between
each, the referrer on the first, and the auction each page belonged to. A page and a `--limit`, not
a chart.

### The anonymous seam, which is the real limitation

`usability_report._actor()` treats a signed-in view (`user` set) and an anonymous one (`session_id`
set) as different people, and says so in its own docstring: *the site cannot tell that those two
were the same person either*.

For the buyer persona this is not a detail, it is the middle of their story. They arrive from
Facebook anonymously, browse, and only become a user -- if ever -- at the point of paying. Their
session splits in half exactly where it gets interesting.

Two honest options, and the choice should be made before any lifecycle number is quoted:

1. **Accept the seam.** Report anonymous and identified behaviour as two populations, never joined.
   Safe, and undercounts every return visit.
2. **Stitch on first sign-in**, recording the session id a user had when they authenticated, and
   treating earlier rows from that session as theirs. Cheap to add going forward, impossible
   retroactively, and it makes any before/after comparison across the change date invalid.

Recommendation: do (2) going forward *and* keep reporting (1) for history, with the change date
marked, because a stitched year compared against an unstitched year is a fake improvement.

**Why not stitch on IP address and user agent?**  It is the obvious third option, and it is not
hypothetical: `PageView` stores both on every row (a row is only ever written for the first view of
a page, so nothing is missing), and the address is the real client -- `views.ajax` takes the first
`X-Forwarded-For` hop, so Cloudflare does not flatten everybody onto an edge IP.  It is still the
wrong instrument, for reasons that are about this site rather than about fingerprinting in general:

* **It is least reliable exactly where the seam is widest.**  The buyer who is anonymous for the
  part of the visit that matters is standing in a room with fifty other people, on the venue's one
  wifi, holding the same iPhone on the same iOS as a dozen of them.  One address, one user agent,
  fifty people.  What that stitches together is not one person's two halves, it is strangers, and
  the failure is silent: it reads as somebody very engaged.
* **A phone breaks it in both directions within one visit.**  Carrier CGNAT puts thousands of
  unrelated subscribers behind one address, and walking into the venue moves the same person from
  LTE to the wifi mid-session.  Over-merging and under-merging, from the same rows, on one evening.
* **The error is correlated with the number being measured.**  The question is "did they come back
  in a year", and over a year addresses churn -- new phone, new carrier, moved house -- so a real
  returner reads as a new person.  Option 1 undercounts too, but it undercounts the *same way in
  both years*, so a club compared against itself still moves in the right direction.  Fingerprint
  error does not cancel: it depends on how many people were in the room and what handsets they
  carried, which is precisely what differs between two auctions.
* **It is not needed for the half people assume it is.**  `SESSION_COOKIE_AGE` is about four years,
  so an anonymous visitor's `session_id` already survives from one spring auction to the next --
  the anonymous *return visit* is stitched today, by a cookie, exactly.  The only seam left is
  anonymous -> identified, and that one has an exact key: the session the person was holding when
  they signed in.  That is option 2, and it costs one row per login.

There is also a commitment already made that a fingerprint would quietly break: deleting an account
nulls `PageView.ip_address` (`auctions/account_deletion.py`).  A stitched identity stored as data
outlives the column it was derived from, and no later run can unmake a merge.

The register an IP address belongs in here is the one this site already uses it in:
`AuctionTOS.trying_to_avoid_ban` shows an organizer *a link to another account* and lets a person
judge it, recording no conclusion of its own.  A metric cannot do that.

If a historical number is wanted anyway, use IP and user agent as a **bound, not a link**: compute
the return rate twice, once unstitched and once with same-address-same-agent rows folded together,
and report the interval between the two.  That is a script somebody runs once, and it says out loud
that the answer is somewhere in a range.  It is not a column, and nothing reads it afterwards.

---

## 9c -- One member's lifecycle, shown to the club

Pick the **median** member of an auction and show that club's organizers what that person actually
did, end to end.

"Median" needs a definition that cannot be argued with, because the mean of this distribution is
meaningless -- a handful of power users dominate every total. Proposal: rank the auction's
participants by lots bought plus lots sold, take the person at the 50th percentile, and show **their
real session**, not a synthetic average of everybody's. An averaged journey is nobody's journey and
reads as fiction to anyone who knows their own members.

Show it next to the same club's previous auction, so the organizer sees a person and a change rather
than a statistic. This is the piece most likely to change what a club actually does, because it is
the only output here that a volunteer running a fish auction will read without being asked to.

---

## 9d -- The club cohort view, which is the real prize

Modelling one person's lifecycle is good. **Modelling how it changes between auctions inside one
club is better**, because the club is its own control: same members, same venue, same rules, one
auction to the next. That is as close to an experiment as this site will ever get.

The four numbers the human asked for, which together are the report:

1. **New people in this auction** -- first `AuctionTOS`, or first `PageView`, for this club.
2. **Where they came from** -- referrer on their first page view. `funnel_referrers` already does
   this per auction; it needs to be per person and kept.
3. **New people who actually participated** -- reached a real milestone rather than only arriving.
   "Participated" is per persona: bought something, or sold something. Not "logged in".
4. **People who took part in N previous auctions and not this one** -- the churn definition from 9a,
   pointed at a specific auction so the organizer can see who they lost *this time*.

Read across a club's auction history, those four say whether a club is growing, replacing its
members, or quietly shrinking behind a flat headline attendance -- and the pattern the human is
after (people who come, then spend less, then stop coming) is a trend in (3) and (4) together, not
a single number.

### What will block this on day one

**Only about 20% of auctions are linked to a club.** Every query in 9d groups by club, so on today's
data it covers a fifth of the auctions. `/admin-unlinked-auctions/` and the `assign_auction_to_club`
command exist to work that backlog down, and **doing so is a prerequisite for this section**, not a
nice-to-have. It is the cheapest high-value work in the whole phase.

Two smaller ones:

- `PageView.auction` is being backfilled on the beat and switches itself off when done. Until then,
  per-auction queries over old rows need the `auction_id OR lot.auction_id` form, which no single
  index can serve.
- A club whose auctions are mostly in-person will have far more `AuctionTOS` rows than page views,
  because an organizer typed them in at the door. That is a real join and a real person, and it is
  not a self-service one; any "arrival" stage above it will legitimately be smaller.

---

## Not doing

- **No A/B tests.** Settled above.
- **No new tracking of any kind.** Every instrument here reads rows the site already writes. The one
  proposed addition (the sign-in stitch in 9b) records a session id the site already has.
- **No third-party analytics.** The whole argument for this site is that a club's member list is not
  a product, and that argument does not survive a script tag.
- **No engagement mechanics.** Streaks, badges, nudge emails. The retention question here is whether
  the auction was worth attending, and a badge cannot answer it.
- **No survey instrument beyond one question.** If anything is asked at all, ask it once, after an
  invoice is paid, when the person has just finished something and knows whether it went well.

---

## What shipped, section by section

| Section | Where it lives |
|---|---|
| 9a milestones | `lifecycle.MILESTONES`, `lifecycle.milestone_reach` |
| 9a lapsing | `lifecycle.LAPSED_AFTER_AUCTIONS`, `lifecycle.lapsed_participants` |
| 9b session replay | `lifecycle.session_timeline`, `/admin-session-replay/` |
| 9b the sign-in stitch | `SignInStitch`, `signals.record_sign_in_stitch`, `lifecycle.stitched_sessions` |
| 9c the median member | `lifecycle.median_member`, `lifecycle.median_member_story` |
| 9c the share never spoken to | `lifecycle.unreached_share` |
| 9d club cohorts | `lifecycle.club_cohorts`, `lifecycle.club_coverage` |

Three implementation notes that are not obvious from the code and are expensive to rediscover:

- **The stitch reads the cookie, not the session.**  `django.contrib.auth.login` calls
  `request.session.cycle_key()` *before* it sends `user_logged_in`, so a receiver reading
  `request.session.session_key` records the key issued a moment ago and stitches a sign-in to
  itself.  `request.COOKIES[SESSION_COOKIE_NAME]` is what the browser sent, which is the key every
  anonymous `PageView` in that visit carries.  `test_the_stitch_records_the_key_the_browser_sent`
  fails on the simplification.
- **"Read the auction rules" cannot be answered from `PageView.auction` alone.**  Since 7a.2 a lot
  page carries its auction's FK too, so the milestone is `lot_number IS NULL` *and* the route
  `auction_main`, classified with `usability_report.route_name` -- Django's own resolver, so a
  rename in `urls.py` cannot leave a stale classifier behind.
- **A session key is a credential, so only a prefix ever leaves `lifecycle`.**  For an anonymous
  visitor `PageView.session_id` is the live session cookie; printing one on the replay index and
  posting it back in `?session=` would put it in the access log, the admin's browser history and the
  `Referer` of every link on the page.  `SESSION_KEY_PREFIX` characters identify the session inside
  `PageView` and are not a cookie anybody can paste back, and `busiest_sessions` is bounded to
  `SESSION_INDEX_DAYS` besides -- ungrouped it is a scan of the whole never-purged table on every
  render of the page's default view.  `test_the_index_never_prints_a_whole_session_key` and
  `test_the_index_is_bounded_to_recent_history_and_hands_back_a_prefix_only` fail on the
  simplification.
- **Identity for somebody with no account is their `AuctionTOS` email, lowercased.**  That is what
  makes the non-user persona the same person at two auctions.  A row with neither a user nor an
  email is its own auction's bidder number and therefore never matches across two -- correct, and
  deliberate: there is nothing there to match on.

## Waiting on a decision

Neither of these blocks anything that shipped.  Both are choices about what this site should do,
which is why they are here rather than settled in a docstring.

1. **The anonymous seam: this shipped as option (2) plus (1), which is what the section above
   recommends -- stitch going forward, keep reporting unstitched history, mark the date.**  If the
   preference is the safer option (1) alone, deleting the `record_sign_in_stitch` call in
   `signals.py` is the whole of the reversal and the model can stay unread.  Say the word.
2. **`LAPSED_AFTER_AUCTIONS = 2` is a judgement, not a derivation.**  The *unit* is the argument
   and it is settled; the number is not.  Two of a club's own auctions is roughly a season for a
   monthly club and roughly two years for an annual one, which is the intended behaviour, but the
   right number is the one an organizer would recognise as "they have stopped coming" and nobody
   has been asked.  One constant, one place.

## Not built, on purpose

- **The one survey question.**  Allowed above -- once, after an invoice is paid -- and nothing has
  been written for it.  It needs the human to decide whether to ask at all, and what; a question
  chosen by anybody else is the thing this campaign is against.
- **The IP-and-user-agent bound.**  The section above describes it as "a script somebody runs once"
  that reports an interval rather than a number, and it stays that: not a column, and nothing reads
  it afterwards.  It is worth writing only when somebody actually wants a historical figure.
- **A per-organizer version of any of this.**  `/admin-lifecycle/` is a campaign instrument and it
  names things an organizer cannot act on.  The same numbers shown to a club would be a different
  design, and 9c is the part of it that is ready to be that.
