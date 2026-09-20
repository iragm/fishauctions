# MCP skills: decisions a resolver's code won't tell you

`auctions/mcp/CLAUDE.md` has the rules every skill shares. Each resolver's docstring names the form,
view or service it goes through. This file keeps only choices that look like mistakes.

## Auctions

- `create_auction` only ever **copies** an auction this person already ran. Nothing to copy → the
  create page. A club-managed auction ignores `copy_users_when_copying_this_auction`;
  `services.PER_RUN_TOS_STATE` blanks the per-run columns otherwise.
- `update_auction_setting` validates through the whole `AuctionEditForm`, so another field's broken
  rule refuses the change and the answer names it.
- `join_auction` takes two calls: `agree_to_rules` is explicit.
- `place_bid` is `bidding.place_bid_and_broadcast`, the only bidding path. `destructive`, no `undo`.
- `answer_question` covers only the seller's own lots.
- There is no `remove_invoice_adjustment` in the palette; it is `mcp_only` (below).
- **`refund_lot` is two refunds.** `paid_by=seller` is `LotRefundDialog`: a split that shrinks the
  club's cut too, refunds Square itself, and is not stopped by a settled invoice (reported in
  `settled_invoices`). `paid_by=club` is a `DISCOUNT` line on the buyer's invoice plus a
  `LotHistory` row — whole units only, refused on a settled invoice, no Square refund. It has no
  column of its own on purpose: every invoice, payout, report and export would need to learn it.
- `price_history` / `suggest_starting_prices` search the caller's **joined auctions only**. Site-wide
  would be a price oracle over other clubs. A lot resolves to its species first. Suggestions are the
  lower quartile rounded down, floored at the minimum bid, and absent under 2 past sales.
- `lot_queue` and `list_pickup_locations` are deliberately not admin-only.
- `add_lots`: `quantity` is fish per bag, `count` is bags. `MAX_LOTS_PER_BATCH` (40) is not the
  runaway bound; `mcp.auth.within_write_budget` is.
- `_lot_field_switched_off` is load-bearing: `QuickAddLot` hides a disabled field rather than
  removing it, so a submitted value would be saved and printed.
- `update_person` on a club-managed auction goes through `ClubMemberAdminForm`, because
  `CreateEditAuctionTOS` disables those fields and a disabled field cleans to its initial value.

## Clubs

- `club_api` renders `_club_api_endpoints.html` with an unsaved key, so the key page stays the one
  write-up. Four topics because the whole page overflows `MAX_RESULT_CHARS`. It never reads a secret.
- `_CLUB_SETTING_PAGES` writes the permission down per page. `ClubPayPalCredentialsForm` is
  deliberately unspoken.
- `club_setup` answers in `settings` / `tool` / `page` fields, not prose.
- **A membership card is a credential.** Only the caller's own is ever built
  (`_my_memberships`, matched on `ClubMember.user`). Sending someone else's returns no number and no
  barcode. `MembershipCardPrivacyTests` walks every route.
- `list_club_members` does not say whether a row has a site account.
- `deny` leaves `bap_auto_reason` alone; undoing an undecided lot is a no-op, not a refusal.

- `my_bidder_number` is the one read about a participant that is **not** admin-gated, because it is
  about the caller and nobody else (`_own_tos`, matched on user or email). `describe_person` answers
  the same fact about anybody and is auction-admin only, which left the person holding the paddle
  with no way to ask. `uses_check_in` rides along so "not checked in" isn't reported at an auction
  that has no check-in.

## Account, history, help, source

- `change_email` changes nothing until the link is followed (`nothing_was_changed_yet`).
- A geocoded marker comes back as a question, never a silent save.
- `recent_changes` / `club_history` build `about` from each model's `applies_to` choices plus
  synonyms; the two tables disagree ("settings" is a club's `SETTINGS`, an auction's `RULES`).
- `FAQ.agent_only` hides an entry from `/faq/`, not from anyone. It is not privacy.
- `read_source` reads a downloaded archive in memory; no path it takes touches the filesystem.
- `set_lot_species` never picks among several matches. Only an auction admin's choice teaches
  `SpeciesSearchCache`.

## `mcp_only` writes

Most writes are on `/mcp/` and not in the palette. `palette_actions.MCP_ONLY_SKILLS` is the list and
the reason for each; `test_palette_assist.DriftTests` pins the sixteen the palette keeps, which is
the list that gets quietly shorter.

The palette keeps a write only when you can say it in one sentence, you say it with your hands full,
and you say it more than once in a while — and not even then if the page shows you something you have
to see before deciding. That leaves the auction floor, the checkout table and the door. Everything
else is a page `go_to_page` reaches.

`add_lot`/`add_lots` are the one pair where both surfaces have the skill under different names: the
palette's `add_a_lot` is navigate-only and opens the lot form pre-filled (`LotCreateView.get_initial`
reads the fields off the query string), because the species matching, the auction's field rules and
the seller's own eyes are all on that page. No caller is ever offered both.

- `remove_person` refuses anyone with an invoice or lots; deleting cascades their money away.
- Queue **reordering** is absent: it rewrites every row.
- **Banning** is not a skill: `CreateUserBan` also deletes the user's bids across every auction the
  admin runs. The `_BAN` reason in `NOT_A_SKILL` records it.
- `record_club_money` moves no money, and refuses the categories a reconcile would overwrite.

## No lot travels as a primary key

A lot is named by `lot_number_display`, the number on its label. `_INTERNAL_RESULT_KEYS` strips
`lot_id` at any depth; `lot_id` survives only as an alias. `image_id` is the exception, because a
photo has no label. `test_no_tool_advertises_a_lots_primary_key` and
`test_no_result_hands_out_a_lots_primary_key` hold it.
