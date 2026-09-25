# Code sweep

A pass over the whole codebase for security holes, brittle code, untested views and code that doesn't
do what it says, September 2026. Fixes landed with tests; anything that is a product decision waits
here for review. Delete a line once it's decided.

**Status: wrapped up 2026-09-25.** Full suite green (7036 tests, one expected failure: the Discord
email claim below), `--ci` green. Everything was reviewed at least once, by area: views, mobile API,
palette/MCP writes, models, background jobs, forms/filters/tables, templates' JS, settings. Not
reviewed line by line: `palette_assist.py` / `llm.py` (the LLM loop), `species_matching.py`, the
Django admin, `google_wallet.py` / `apple_wallet.py`, management commands beyond the scheduled ones.

## Waiting for a decision

- **Discord join modal trusts a typed email.** `DiscordInteractionsView._handle_join_modal` links the
  submitter's Discord account to whichever `ClubMember` has the typed address and assigns that
  member's role (paid, BAP tier or an override). Nothing verifies the address, so anyone in the
  server can take a paid member's role, and the real member is then told the address is "already
  linked to another Discord account". Needs a confirmation email before linking.

This is worth a 1 sentence note on the Discord setup page.  Note that each discord account can only be connected once and there's no feedback as to whether you connected an existing membership or created a new one, so someone would need to know the member's email and connect them deliberately, brute force is essentially impossible.  And to what end?  Discord does nothing except a role and allowing you to renew your membership.

  `test_club_integration_views` has the expected-failure test that shows it.
- **Renaming a club changes its slug** (`Club.slug`, `always_update=True`, chosen in April). The slug
  is now in the Mailchimp and Brevo webhook URLs, every website embed, the `.ics` feed and the API
  base, so a rename silently breaks all of them. Proposed: a stable slug (no SQL in the migration).

People will complain that club slugs don't match the new club name, let's go with a random 10 digit number for all future api methods, making sure to also allow slug methods as a fallback for existing clubs

- **In-person auctions end up with two pickup locations.** The placeholder "The <title>" location
  appears on the auction's second save (`finish_new_auction` saves again when the creator has a
  club), then the organizer adds the real one the next page asks for, so the auction reads as
  multi-location and bidders are asked to choose. 11 of 13 in-person auctions on dev have both.
  Proposed: drop an untouched placeholder (moving its participants) when the first real location is
  added, plus a data migration for existing ones.

This does not happen in prod, I have no idea what's going on here but there's no bug that I know of related to this.

- **`make_stats_public` does nothing on the web.** The palette shows stats to non-admins when it is
  set; the stats page and its charts are admin-only either way.

This is an old field and hasn't been used, probably safe to remove but remove field migrations have been problematic in the past.  We can safely remove all references to it everywhere except in the model definition

- **GETs that write.** `InvoiceCreateView` (creates an invoice and checks the person in) and
  `CreateUserIgnoreCategory`/`DeleteUserIgnoreCategory` change data on GET, so a link or an `<img>`
  can trigger them. Moving the ignore-category pair to POST puts them in the palette audit, which
  needs a `NOT_A_SKILL` reason.

move these to post, userignorecategories should probably be a skill.  Something that you didn't find is that there's a ton of ?do_action=True on the auction page, mostly for django admin users but also now for auction admins too, that should get cleaned up with those moving to post methods.

- **PayPal subscription webhook amplification.** An unverifiable request for an unknown subscription
  id makes a token call and a verify call per candidate club.

How exactly would you fix this?  Real risk seems very low to me, a bunch of calls to paypal isn't the end of the world.  IIRC there was a good reason this was done this way.

- `ClubMemberAppleWalletByUUIDView` serves a pass to anyone with the member UUID, while the
  account-bound sibling's docstring says UUID links must not reach somebody else's card.

Fix the docstring.  There's 2 ways to get to a pass, the 10 digit number which only allows renewal and is consider essentially unsecure, and the uuid which allows adding to google or apple wallet.  The UUID has to be available to non logged in users, so it has to be sent via email.  Yes it's gonna get in server logs but what is the real world risk here?  How can someone exploit a fish club membership card?  10 hours setting up a replay attack or collecting UUIDs and you're gonna get a 10% discount at some local fish store?

- A 1.7 GB core dump, `core.1773` (13 Aug), is sitting in the repo root (ignored by git).

Just delete that dump

- **`Lot.refunded` does nothing.** Its help text says the winner isn't charged and the seller isn't
  paid, it is editable in the admin and the API, and nothing that computes money reads it. Implement
  it in `add_price_info`/`bought_lots_queryset`, or remove the field.

It's a tag on the lot list, the actual refund action is done on a form.  Seems fine to me as-is, remember that 95% of transactions are done with cash so the refund percent which is set at the same time is the real load bearing code.

- **A forced donation never reverts.** A price at or under `force_donation_threshold` sets
  `donation=True`; correcting a mistyped $1 to $20 leaves the lot a donation and the seller unpaid.
  Needs a field saying the flag was forced.

Yeah it does need that field, this would also allow it to be shown on the invoice.

- **Duplicate invoices are possible**: `create_update_invoices` checks then creates, with no unique
  constraint on `(auctiontos_user, auction)` (nullable, so MariaDB would take a plain unique).

This seems like an oversight, we used to run dedupe logic on save.  The unique constraint at the db level causes 500s on duplicate, although this was before we started using atomic transactions.  investigate and fix, making sure to respect stuff like the no-auction invoices for renewals.

- `ClubMoney.CATEGORY_REGISTRATION_FEE` is booked automatically but can also be entered by hand.

I need to know why this is an issue, how can a club admin (or anyone really) exploit it?

- `UserData.auctions_i_admin` includes non-club-managed auctions of clubs you run, which
  `permission_check` doesn't; it scopes `runs_an_auction` and the species pages.

This seems like intended behavior, it's also used for linking paypal and square via oauth right?

- Background jobs, each a design choice more than a bug:
  - `Auction.page_views` is an OR no index serves, used unbounded by the stats task.

upside and downside to this?  Auction stats is slow as hell but that's why it's cached.

  - `set_user_location` scans `PageView` every 2 hours and does full-row saves after a 30s HTTP call.

Yeah we need to limit this to the latest page view/user if we don't already.  System working smoothly as designed with no known issues, I guess if a million people signed up in a 2 hour window it would be slow but it would eventually catch up.

  - Deleting an auction or club event calls Google and Discord synchronously in `pre_delete`, before
    the transaction commits.
Seems fine to me, why would it fail to delete?  Move it after and you get the oppostie problem where google fails and the event persists

  - Promo pushes are deduplicated only after delivery, so a backed-up queue repeats them.
That's probably worth fixing

  - Discord 429/5xx is treated as a permanent refusal, and the edit is dropped.
Fine as long as the next edit tries again

  - Calendar `push_event` clears `needs_google_sync` after the call, losing an edit made during it.
Fix

  - `sendnotifications` loads every standalone lot that ever missed its window, every 15 minutes.
Standalone lots are not used in production, worth a comment in that spot but nothing more

  - `purge_bot_users` has no minimum account age.
Add account needs to be at least a week old

  - `update_ar_positions` runs every minute with no lock

Probably worth fixing

  - The inbound donation webhook calls the LLM synchronously, and its Message-ID dedupe can race.
Fix


- **Permission scope questions** (the model allows each today; say which you want):
  - Importing members needs only `permission_export`, but an import creates, updates, deactivates
    members and sets their paid-through dates.
Make sure the labels for permission export all say import/export

  - `permission_edit_club` alone can create an API key with any capability, including reading every
    member's contact details, and can attach its holder's own PayPal/Square to the club.
Intended, this is the highest non-admin level

  - The session member API lets `permission_add_edit` edit BAP/HAP points without
    `permission_manage_bap`.
This seems like a bug, it should require bap permission for that

  - The palette's `update_person` lets an auction admin edit the club's member record, where the web
    needs club `permission_add_edit`.
Intended, worth a comment

  - `add_lots` makes up to 40 lots in one call, against the "one row per tool call" rule.
Intended, that 'rule' is stupid (Opus 5.0 hallucination) and more of a guideline

- **Payments**: no unique constraint on `InvoicePayment (invoice, external_id)`, so the Tap to Pay
  confirm and the Square webhook can both record one charge; the webhook doesn't lock the invoice.
Seems like we shoudl fix it, gotta prevent duplicates.  Make sure this doesn't mess with the app and multiple taps.

- **Sign-in flows** (mobile): the web-session handoff and the social "continue" link can be opened by
  someone else's browser (login CSRF / linking an attacker's social identity). Needs a confirm step.

Exactly how can this be exploited?  Confirm step adds friction, let's add it only if absolutely needed.  I would rather do other stuff like invalidate the link after the first click or add CSRF headers if possible.

- **AR observations**: any signed-in user can post up to 500 rows a request to any auction's lot map.

Intended

- Mailchimp's `upemail` webhook changes member emails with `.update()`, skipping the member→auction
  sync; its OAuth state is the user's unsubscribe UUID (in every email footer).
Fix

- `SelfServeContactLinkView` changes contact preferences on GET (link scanners).
Fix

- `propagate_contact_info` overwrites admin-edited club member names/phones.
Intended,club history preserves both

- `retract_announcement` (palette) always takes the newest, with no time limit.
Allow the user to specify which, current behavior as default

- **Check production for summernote uploads.** Until this sweep anyone could upload files through
  `/summernote/upload_attachment/` with an extension of their choosing (now refused). Run
  `Attachment.objects.count()` (django_summernote) on prod and look in `mediafiles/django-summernote/`;
  also consider `X-Content-Type-Options: nosniff` on `/media/` (commented out in `swag/nginx/ssl.conf`).
Couple images.  I would rather not allow any kind of summernote uploads I think.

- `AuctionInfo.get` acts on query-string flags (`?trust_user=1&make_club_admin=1` for a superuser,
  `?enable_online_payments=1` for the creator): a plain link can make a superuser trust someone.

Yeah this was noted above too, worth changing to post

- Label PDFs over 500 labels now ask the admin to print in batches (thermal stays 100).
Fine

- The page-view beacon allows 300 rows a minute per address (a venue's wifi is one address).
Fine
- `undo sale` on the set-winners page now refuses a lot on a settled invoice, and the page has no
  way to send `force` (the palette does).
This seems almost impossible to trigger in the real world

- `_lot_invoices` / `_recalculate_invoices` live in `views/selling.py` and are imported by three
  other view modules; by `views/CLAUDE.md` they belong in `base.py`.
Yeah, move them

## Fixed

- Stored XSS: a participant's name in the renewal-toggle modal title, and in every
  `generic_admin_form.html` title and tooltip (both rendered `|safe`); a seller's lot name in the
  dynamic set-winners banner; names and messages arriving over the lot websocket.
- Reflected XSS: the set-lots-won dialog's `?query=`.
- Template injection: crispy `HTML()` renders its string as a template, so a lot name reading
  `{% debug %}` ran in the BAP award form. `forms.LiteralHTML` is the layout object for markup
  that carries typed text.
- Open redirect through `?next=` in five `get_success_url`s, which also 500'd on any other query
  parameter. `views.base.safe_next_url`.
- An account with no email matched every hand-added participant with none: signing in claimed
  them, and their invoices, won lots and auctions listed as the account's own. `models.email_q`.
- Chat longer than the 400-character column was refused by MariaDB and lost (the palette posts up
  to 1000). An auction seller opening their own lot never cleared its unread chat (a pk was compared
  with a User).
- A failed bid rolled back nothing it had already written.
- Prices: `NaN`, `Infinity`, negative and over-limit prices are refused when setting a winner (web,
  palette, offline sync) and when bidding, instead of 500ing or reaching the invoice.
- A deleted placeholder pickup location came back on every save of an in-person auction.
- A club slug now always wins over another club's matching abbreviation (`views.base.club_from_url`).
- 500s on made-up ids: ban/unban/deactivate, stats charts, new pickup location, ignore-category,
  merge-with in the delete-participant dialog.
- Bare `except:` → `except Exception:` everywhere; ruff E722 is on.
- Stored XSS: an auction title in the new-lot page's notices; a lot name (or seller) in the bid
  confirmation dialog, and a pickup location's name in the auction map, via `\x3c` surviving HTML
  escaping inside JS; a participant's name in the check-in modal; names in barcode-scan toasts (the
  toast plugin now escapes everything it's given); names and emails on the superuser user map; a
  stored email in the add-participant autofill button; `javascript:` vendor contact URLs.
- Invoices 500'd while an online auction was running (`Invoice.dynamic_end` doesn't exist).
- Merging duplicate participants orphaned the duplicate's `ClubMoney` rows, double-booking lots.
- A lot refund on a club auction went to the creator's Square account instead of the club's.
- Selling and invoicing a lot at close are one transaction on a fresh row: a lot whose invoice
  failed was deactivated and never retried. `endauctions` no longer saves rows read minutes earlier.
- Membership expiry, Discord roles, BAP award dates and wallet refreshes used the UTC date; an
  expiring member lapsed at 8pm Eastern. `timezone.localdate()` everywhere.
- `Auction.invoice_recalculate` did the full-row save `recalculate()` exists to avoid.
- One wrong Google client secret disconnected every club's calendar (only `invalid_grant` does now).
- The nightly jobs and calendar sync swallowed Celery's soft time limit and ran to the hard kill.
- `auction_emails` stopped for everyone at the first auction whose creator was deleted, and
  full-saved stale auctions; the TOS reminders full-saved and pushed stale contact data to members.
- A retracted announcement could still go out; welcome emails could repeat nightly; the join
  reminder listed a lot once per visit; an uploaded file was deleted before its row's delete committed.
- Smaller: `bids_can_be_removed` crashed on standalone lots; `total_sold_gross`/`total_donations`
  counted banned lots; `UserData.lots_sold` missed in-person wins; a pickup location with no name
  raised in `__str__`; a dead "very new lots" branch.
- A participant could add and edit lots under another bidder's number on the classic bulk-add page
  (`/auctions/<slug>/users/<number>/`): the number was looked up for anyone, not just admins.
- No-show actions: the negative feedback on lots they sold was never saved (`x - 1`, not `x = -1`),
  and "ban" looked the account up by email only, so a linked account with no email wasn't banned.
- Banning yourself removed your own lots from every auction you run.
- Ads: a campaign ran one more time than its limit, and an auction-only campaign showed site-wide.
- A garbage location cookie 500'd the lot list and lot pages (`helper_functions.cookie_coordinates`).
- More 500s: an unknown invoice pk, a non-UTF-8 CSV upload, `?import=` of a missing auction, a
  feature-use chart for an auction with no participants, `?days=0` on the traffic chart,
  non-numeric ids in four admin views.

- Club member merge: add/edit alone could merge the president into their own row and come out club
  admin (roles carry over only for a club admin now); a member could be "merged" into themselves.
- BAP: `?lot_pk=` accepted another club's lot and took it off that club's points queue; editing an
  award left the lot's points stale.
- API keys wrote every member column not on a denylist: the expiration date, the Apple Wallet auth
  token, sync ids, coordinates. Now an allowlist.
- Checking in an existing member copied the posted (unsaved) edits onto their auction record, and
  could take another bidder's number.
- The day's last donation draft could never be sent (drafts counted against the send).
- Mobile: the legacy Google sign-in linked an existing unverified account and kept the squatter's
  password; a club officer could pull an auction creator's personal Square token; a password reset
  left the app's refresh tokens working; `nan` coordinates, PassKit bodies and device reassignment.
- Palette: `remove_bid` left the bidder the winner of an ended lot (now shares `services.remove_bid`
  with the page); `resend_member_card` read the member's name as the club; editing a lot failed for a
  seller at the lot limit (also on the bulk-add page); "half" refunded 100%; `set_current_auction`
  was wider than the web; `lot_id` reached lots outside the caller's auctions; a second `_money`
  replaced the sentence formatter module-wide. Names people typed are fenced as untrusted in ~25 more
  results; the string "false" was True for a dozen flags (including `send_club_announcement`'s
  email, which would have mailed the club, and `ignore_errors` on set-winner); km preferences
  reported and undid in miles; undo couldn't restore a blank field or a description; several writes
  left no history line.
- Brevo/Mailchimp: archiving a member with no email called `DELETE /contacts/`.
- Invalid `user_timezone` cookies, bad UUIDs, `inf` label amounts, renewing deactivated members, UTC
  "date joined" in the member export.

- **Summernote uploads were open to anyone**, signed in or not, keeping the client's file extension:
  an image/HTML polyglot named `x.html` was served as a page on the site's own origin (stored XSS).
- **Any connected Square seller could mark any invoice paid** by creating an order in their own
  account naming it; the webhook now requires the payment's merchant to be the invoice's payee.
- Full-row saves that put back concurrent changes: opening your invoice (over a webhook's PAID),
  peeking at a lot's max bid (over a bid's extension or a buy-now), the refund dialog (on GET),
  bulk invoice status changes (now per-row conditional updates; the history line always said 0).
- Club add/edit-people could tick "auction admin" on a participant row.
- "Sell to online high bidder" re-sold lots already sold on the floor.

- Money paths that took a lot from a winner (undo sale, re-set winner, lot admin, no-show refunds,
  deleting a participant) now recalculate both invoices; undo sale refuses settled invoices and
  online auctions; the invoice-create link merged duplicates by deleting them, payments included.
- Label PDFs marked labels printed before rendering; single-lot labels 500'd without a seller.
- Volunteer bounties went to anyone with a job URL, onto closed invoices.
- The lot queue raced on concurrent scans and could head with a sold lot.
- Forms: sellers could edit sold lots via bulk add; moving a lot between auctions skipped the lot
  limit; "buy now required" was never enforced (`"require"` vs `"required"`); enabling club-managed
  mode copied the roster for any auction admin; EditLot trusted a posted auction; negative winning
  prices; unbounded adjustments that made an invoice permanently un-recalculable; tax over 100%.
- Filters: `/lots/` popularity sort multiplied page views by history rows; unbounded OR search;
  `½`/`²` 500s; "Ended" status did nothing; staff keywords matched name prefixes ("openshaw").
- Site: unthrottled page-view/abandon beacons (rows kept forever); social logins kept the 14-day
  anonymous session; stale UserData saves on every page; the Discord bot posted to any channel;
  account deletion left API keys, OAuth tokens, sign-in stitches and palette text; admin traffic
  pages iterated every PageView; CSRF trusted localhost in production.
- ChatGPT couldn't connect over MCP: its client metadata document says `private_key_jwt`, which the
  toolkit refuses; `mcp.cimd.narrow_auth_method` maps it to `none` (it also offers that).

## Tests added this sweep

`test_untrusted_text`, `test_blank_email`, `test_money_views`, `test_stats_and_browse_views`,
`test_bulk_import_views`, `test_club_integration_views`, `test_auction_admin_views`,
`test_payment_views`, `test_marketing_sync_and_tasks`, `test_sweep_round_two`,
`test_palette_sweep_fixes`, `test_sweep_round_three`, `test_lot_money_fixes`,
`test_form_filter_fixes`, `test_site_account_fixes`: about 650 tests over the code coverage showed
was least tested. Each bug above that a test found has the test that found it. Existing tests that
asserted a bug were changed to assert the fix (merge roles, Square payee, toast escaping, Discord
channel, club-management permission, summernote uploads).
