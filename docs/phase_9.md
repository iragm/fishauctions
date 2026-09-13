# Phase 9 -- everybody who is not running the auction

Built 2026-09-10. `auctions/lifecycle.py` is 9a-9d, `/admin-lifecycle/` and `/admin-session-replay/`
are the pages, `SignInStitch` (migration 0438) is the one row it adds, `test_lifecycle.py` is the
ratchet.

Phases 0-8 were about the organizer. This is about the buyer (no account, came from Facebook), the
non-user (brings lots on paper, first contact is the invoice email) and the seller (lists lots, gets
paid, never buys). The average time somebody spends in this hobby is about a year, so the question
is never "did they convert" but "did they come back" -- and a year is two or three auctions.

| Section | Where it lives |
|---|---|
| 9a milestones | `lifecycle.MILESTONES`, `milestone_reach` |
| 9a lapsing | `lifecycle.LAPSED_AFTER_AUCTIONS`, `lapsed_participants` |
| 9b session replay | `lifecycle.session_timeline`, `/admin-session-replay/` |
| 9b sign-in stitch | `SignInStitch`, `signals.record_sign_in_stitch`, `lifecycle.stitched_sessions` |
| 9c median member | `lifecycle.median_member`, `median_member_story` |
| 9c share never spoken to | `lifecycle.unreached_share` |
| 9d club cohorts | `lifecycle.club_cohorts`, `club_coverage` |

## The three choices that are not obvious from the code

- **Milestones, not a funnel.** A seller adds lots and never opens a lot page; in a funnel that
  reads as dropping out at the lot page. Only real prerequisites are ordered (`after`); everything
  else is reach. The terminal milestone is a paid invoice, which every auction type has.
- **Lapsing is counted in the club's own auctions**, not in days -- a two-month gap means opposite
  things at a monthly club and an annual one. Anyone whose last auction is the club's most recent is
  right-censored and excluded, not counted as retained.
- **The median member is a real person at the 50th percentile** by lots bought plus sold. An
  averaged journey is nobody's journey and reads as fiction to an organizer who knows their members.

## Traps, each with a test that fails on the simplification

- The stitch reads `request.COOKIES`, not `request.session.session_key`: `auth.login` calls
  `cycle_key()` before sending `user_logged_in`, so reading the session stitches a sign-in to itself.
- "Read the auction rules" is `lot_number IS NULL` **and** route `auction_main`, because since 7a.2 a
  lot page carries its auction's FK too.
- A session key is a credential: only a prefix leaves `lifecycle`, and `busiest_sessions` is bounded
  to `SESSION_INDEX_DAYS` or it scans the never-purged table on every page load.
- Identity for somebody with no account is their `AuctionTOS` email, lowercased. No user and no
  email never matches across two auctions -- correct, there is nothing to match on.

**Why not stitch on IP and user agent:** fifty people on a venue's wifi holding the same handset
merge into one very engaged visitor; CGNAT and a walk through the door over- and under-merge in one
evening; and the error correlates with the number being measured, so it does not cancel when a club
is compared against itself. Deleting an account also nulls `PageView.ip_address`. A session cookie
lasts ~4 years, so the anonymous return visit is already stitched exactly; the only seam left is
anonymous -> identified.

## Not doing

No A/B tests. No new tracking -- every instrument reads rows the site already writes. No third-party
analytics: the argument for this site is that a club's member list is not a product, and that does
not survive a script tag. No engagement mechanics. No survey beyond one question, asked once, after
an invoice is paid.

## Waiting on a decision

1. **The anonymous seam** shipped as stitch-forward plus unstitched history with the date marked. To
   fall back to unstitched only, delete the `record_sign_in_stitch` call in `signals.py`.
2. **`LAPSED_AFTER_AUCTIONS = 2`** is a judgement. The unit is settled; the number is not. The right
   one is what an organizer would recognise as "they have stopped coming", and nobody has been asked.

Blocked on data, not code: about one auction in five has a club, so `club_coverage()` is at the top
of the page. `/admin-unlinked-auctions/` is the fix.
