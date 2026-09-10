# Phase 9 -- everybody who is not running the auction

Written 2026-09-09, at the human's request, because it was the one thing in `USABILITY.md` with no
answer in it. Phases 0-8 were about the person running an auction and the instruments that can see
them. This is about the other several thousand people, and it starts from one sentence the human
wrote at the top of that file:

> Smooth UX is quite literally the only thing this project offers over a spreadsheet.

Everything below is downstream of that. A club can run an auction on paper. It cannot give a buyer
a lot page on their phone at the table, an invoice they can pay, or a reason to come back next
spring -- and if this site does those three things badly, there is no argument left for using it.

---

## The constraint that decides the method: you cannot A/B test this site

The human wanted to and was right to give up on it. It is worth writing down *why*, because the
temptation comes back every time a change is arguable.

**The unit of randomisation is not the person.** For anything organizer-facing, n is "dozens of
active organizers a year" -- the human's own estimate. No split of dozens reaches significance on
any effect this site could plausibly produce. That much is obvious.

The non-obvious half is that **buyer-facing changes are barely better**, even though there are far
more buyers than organizers. Buyers are not independent: they arrive at one auction, run by one
club, with one set of rules, one pickup location and one Facebook post driving the traffic. Two
bidders in the same auction are more like each other than like two bidders in different auctions,
so the effective sample size is closer to *the number of auctions* than to the number of bidders.
That is a few hundred a year, split across wildly different auction sizes -- and the variance
between auctions dwarfs anything a button colour does inside one. A test would need to run for
years to say anything, by which time the site has changed underneath it.

So: **no experiments, and no dashboard that pretends to be one.** A conversion rate that moved from
31% to 38% across two months is not evidence here and must never be presented as though it were.

## What replaces it

Four methods that work at this size, in the order they cost effort.

**1. Read the funnel that already exists.** Phase 7b shipped arrival → lot page → join → bid → win
→ invoice opened → paid, per auction, anonymous sessions included. Nobody has read it yet, because
it shipped the same week this was written. A stage that loses 80% of the people who reach it does
not need a significance test; it needs looking at. This is the whole of 9a and it needs no code.

**2. Reconstruct what one person did.** The rows for this already exist and are five years deep:
`PageView` carries `session_id` for signed-out visitors and `user` for signed-in ones, plus a URL
path since phase 0 and a timestamp. Ordering one session's rows is a session replay in the only
sense that matters here -- the sequence of pages, with the gaps. At n=dozens you learn more from
twenty real sessions read end to end than from any aggregate, and this is the instrument that makes
that possible. It is the highest-value thing in this phase.

**3. Before and after, paired on the organizer.** When a change does ship, compare the same club's
next auction with its previous one, and only look at effects large enough to see with the eye. The
pairing is what makes this worth anything: it cancels the between-auction variance that kills the
A/B test, because the same club with the same members is on both sides. It still is not proof. It
is the difference between "we changed something and the number moved" and "we changed something".

**4. Ask, once, at one moment.** Never a survey. One question, at the one moment somebody has an
opinion and no reason to lie -- and under the site's existing rule that nothing gets more than a
sentence. Candidate moment: immediately after an invoice is paid, which is the end of the whole
funnel and the point at which somebody either would or would not come back.

---

## The four people this phase is about

Phase 0-8 had one user. This has four, and they fail in different places.

**The buyer with no account.** Arrives from a club's Facebook post, lands on a lot or an auction
page. Everything up to bidding works signed out and deliberately so -- the Bid button is on the page
whether or not you are signed in (phase 7c, and it stays). The question this phase has to answer is
what fraction of these people ever reach a second page, and the funnel can already say.

**The buyer with an account who has not joined this auction.** The site's most confusing state, and
the one the dialog in 7c exists for. They can see everything and do nothing until they join, and
"join" is a word that means a legal agreement to an auction's rules.

**The seller.** Needs an account (settled, and not revisited: a uuid link would let a forwarded
email write lots as somebody else). The lot gate asks them for a name and address before their first
lot. `FormFailure` and the abandonment beacon from phase 1 already watch the lot form; nobody has
read those either.

**The person who came once.** The only one of the four with no instrument at all pointed at them,
and the only one whose absence is the actual business problem. See 9c.

---

## Order of work

### 9a. Read the funnel. No code.

Open `/admin-usability/`, take the funnel for the ten largest auctions of the last year, and write
down which stage loses most. Everything after this is conditional on the answer, which is why this
document does not specify 9d yet. **Do this before writing anything.**

Expected output: one paragraph in `USABILITY.md`'s pass log naming the worst stage, and whether it
is the same stage for online auctions as for in-person ones -- online is about 5% of auctions, so a
finding that only applies there is worth much less.

### 9b. Session reconstruction, off rows that already exist.

An admin page that takes a session id or a user and renders that visitor's pages in order, with the
gap between each. Built entirely on `PageView`; no new model, no new beacon, no new column.

Three things it must do, each of which is the difference between a toy and an instrument:

* **Order by time and show the gaps.** A four-second hop from the lot page to the login page and a
  four-minute one are different stories.
* **Mark the stages.** A row that is the auction's rules page, a lot page in that auction, the join
  page, an invoice -- these are the funnel stages, and seeing them inline is what makes a session
  legible without reading URLs.
* **Find sessions by outcome, not by id.** "Twenty sessions that reached a lot page and never
  joined" is the query somebody actually has. Being able to type a session id is not.

Cost note, and it is the reason this needs care: `PageView` is the largest table on the site and its
history goes back to 2020. Every query here must be bounded by a date floor and hit an index --
`usability_report`'s funnel queries carry one for exactly this reason, and the shape behind a past
production incident was an unbounded scan of this table from a page an admin opened casually.

### 9c. Does anybody come back?

The question with no instrument, and the one the "excellent experience that makes them want to come
back" sentence is really asking. It is answerable *retrospectively for five years* off `PageView`
plus `Bid` and `Invoice`, because nothing has ever purged those rows.

Define a cohort as everybody whose first recorded activity falls in one month, and report what
fraction of them did anything at all in each following month. Then the same thing split three ways
that the site can act on:

* buyers who won something versus buyers who bid and lost -- if losing bidders never come back, the
  whole outbid-notification design is worth revisiting;
* people whose first auction was in person versus online;
* people who paid an invoice online versus at the table.

No new writes. This is a report, and like the funnel it belongs on `/admin-usability/` rather than
in front of an organizer: it names things an organizer cannot act on.

### 9d. Fix the worst stage. One change, then measure.

Deliberately unspecified until 9a says what it is. The rules it inherits are the ones the campaign
already runs on and they are worth repeating because this is the phase most likely to break them:

* **More than one sentence will not be read.** No explanatory paragraph gets added to any page.
* **Silence is the design.** Machinery that works stays invisible; a feature that needs announcing
  is a feature that is not finished.
* **One change at a time**, or method 3 above cannot attribute anything.

### 9e. The one question.

After an invoice is paid, one question, one line, dismissible, asked of a given person at most once
a year. Something whose answer changes what gets built -- not a satisfaction score, which is a
number nobody can act on at this sample size.

Do this **last**. It is the only item here that costs a user anything, and it should not be spent
before 9a-9c have said what is worth asking about.

---

## Not doing, with the reason

* **A/B tests, feature flags for experiments, or any significance test.** See the top. If a
  proposal's justification is "we can measure which is better", it is not doing that here.
* **A redesign.** Nothing in phases 0-8 found a page that is wrong all over; they found specific
  stages that lose specific people. A redesign is how you lose the parts that were working.
* **A third-party analytics or session-replay script.** The rows are already here, the site sets no
  third-party cookies, and adding one would be a privacy change made for the convenience of a report.
* **Any new email.** `weekly_promo` keeps its `User` + opt-in + location requirement. Settled twice.
* **A satisfaction score.** At this sample size it is a number that moves for no reason and gets
  argued about.

## What would make this phase a failure

It ships four dashboards and changes nothing on any page a buyer sees. Phases 0-8 were allowed to be
mostly instrumentation because there was nothing to see with; that argument is now spent. **9a and
9b exist to be acted on in 9d, and the pass log should show that happening.**
