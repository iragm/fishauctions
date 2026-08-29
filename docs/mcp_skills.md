# The MCP skills, one area at a time

Every capability on `/mcp/` and in the command palette is an `Action` in
`auctions/palette_actions.py`; this file is the catalogue of what each one goes through — the form,
view or service it calls, and the rules that are that skill's own rather than the endpoint's.
`CLAUDE.md` has the part that binds all of them: the registry rules, the transport and its
credentials, the three prompt-injection bounds, how an auction or club is resolved with no page to
read it off, the shape of a result, and what adding a URL costs you. Read that first; a skill cannot
break those.

## Auction-side skills

- `create_auction` **only ever copies** an auction this person already ran; nothing with anything to
  copy is guessed, and somebody with nothing to copy is handed the create page.
  `services.clone_auction` is shared with the copy button, `services.finish_new_auction` is the
  tail (history line, creator's club, `last_auction_used`, club admins), and
  `services.auction_to_copy` orders by `-date_start` (in-person auctions have no `date_end`).
- `services.PER_RUN_TOS_STATE` blanks the columns that are answers to "what happened last time"
  when `copy_users_when_copying_this_auction` duplicates an `AuctionTOS`: `checked_in`,
  `door_prize_called`, the two confirmation-email flags, `time_spent_reading_rules`,
  `possible_duplicate`. A **club-managed** auction ignores that setting outright.
- `Auction.promote_this_auction` defaults to **False**; the fixtures in `tests.py` set it explicitly
  because `models.guess_category` and `command_palette._visible_auctions` are scoped to promoted
  auctions.
- `update_auction_setting` goes through the real `AuctionEditForm` (which validates the *whole*
  auction, so a rule broken by another field refuses the change and the answer says which field) and
  through `AuctionCustomFieldsForm` via `_set_one_lot_field_setting`. It reports when the form
  overruled it. `_AUCTION_SETTINGS_NOT_SPOKEN` excludes the six dates and the rules text.
  `services.promoting_makes_it_the_clubs_current_auction` is the shared side effect.
- Setup skills: `add_pickup_location` / `update_pickup_location` / `list_pickup_locations` (through
  `PickupLocationForm`; the listing is deliberately **not** admin-only), `add_dropdown_option` /
  `remove_dropdown_option` (same rules as `AuctionDropdownOptionsAPI`), `update_label_fields`
  (through `LabelPrintFieldsForm`; called with no field it reports what the labels print now), and
  `request_volunteers` / `cancel_volunteer_request` (through `VolunteerJobForm` and
  `notify_volunteers_of_job`, in-person auctions only).
- `services.join_auction` (extracted from `AuctionInfo.post`) needs an explicit `agree_to_rules` —
  two calls, not one — and asks which location on a multi-location auction. Gated on
  `closed or pretty_much_over`.
- `check_in` falls back to `_club_member_arriving` when a name matches nobody in the auction:
  exactly one match among the **club's** members creates the shadow row through
  `_upsert_clubmember_shadow_tos` (the scanner's own helper), and the reply says
  `added_to_the_auction`.
- `set_lot_winner` and `undo_sale` take `ignore_errors` (the set-winners page's "ignore errors and
  save"). `undo_sale` refuses outright when either side's invoice is settled. The undo window is 30
  minutes and the stack is 20 deep.
- `place_bid` runs `bidding.place_bid_and_broadcast` (`views.PlaceBid`'s own call: row lock,
  `check_all_permissions`, `check_bidding_permissions`, proxy arithmetic, outbid email, websocket
  broadcast). There is no second bidding path. It is `destructive=True`, not idempotent, returns no
  `undo` block, and the result carries `cannot_be_undone`.
- `answer_question` covers **only the seller's own lots**; rules are the lot page's
  (`check_all_permissions` then `check_chat_permissions`) and the row/broadcast go through
  `consumers.post_chat_message`.
- `find_invoice` is permissioned as the invoice is (an admin of that auction, or its owner).
  `add_invoice_adjustment` validates through `InvoiceAdjustmentForm` (whole dollars, 150-character
  note); the **sign of `amount` picks the direction**; a settled invoice refuses. There is
  deliberately no `remove_invoice_adjustment`.
- **`refund_lot` is two different refunds behind one `paid_by`.** `seller` (the default) is
  `views.LotRefundDialog`'s path exactly — `Lot.refund` (which sends the Square card refund itself
  where the sale went through Square), then `Invoice.recalculate` on each side — and it is a
  **split**: `models.add_price_info` reduces `your_cut` by the same percentage it reduces the
  buyer's charge, so the club's commission shrinks with it. `percent: 0` takes an existing refund
  back off, which is also what the `undo` block sends.
  `club` is the goodwill refund and has **no column of its own on purpose**:
  `partial_refund_percent` is a split by construction, and a second refund field would have to be
  understood by every invoice, every payout, the treasurer's report and the CSV export. It is what
  a treasurer does by hand — a `DISCOUNT` line on the **buyer's** invoice through
  `InvoiceAdjustmentForm`, the lot and the seller untouched — so the seller keeps the full payout
  and the club absorbs it. Consequences: whole currency units only (`InvoiceAdjustment.amount` is a
  `PositiveIntegerField`), so a refund with cents in it refuses and names the figure; a settled
  buyer invoice refuses, as it does for `add_invoice_adjustment`; there is no Square refund; and
  because the lot keeps no mark, a `LotHistory` row says what happened where somebody reading the
  lot will find it. A **settled invoice does not stop the `seller` refund** — the dialog does not
  stop it either, because a club that has already handed the money over still has to record the
  refund — it is reported per side in `settled_invoices` instead. Removing (banning) a lot is still
  the other half of that dialog and still a page.
- **What things go for.** `price_history` ("what does daphnia go for?") and
  `suggest_starting_prices` ("what should I start these at?") are the same query — `_comparable_sales`
  — scoped exactly like `find_lot`, to `command_palette._joined_auctions`. That is the club's own
  price history and deliberately not the site's: a wider sample would also be a price oracle over
  every other club. A lot number resolves to a lot and the search is then done on its **species**
  where it has one, which is what catches "Water fleas" and "Daphnia magna" as one thing; a name
  matching exactly one lot goes the same way, and anything else is matched against lot names, which
  is what the older half of any club's history has. Prices are `winning_price` on non-banned,
  non-deleted lots; donations count (a donated lot still went for what somebody paid) and the lot
  being asked about is excluded from its own evidence. `PRICE_HISTORY_YEARS` is 3 by default and
  `years: 0` reads the whole history. The median, not the mean, for `Auction.median_lot_price`'s
  reason.
- `suggest_starting_prices` defaults to the lots **nobody priced** — `reserve_price` at or under
  `Auction.minimum_bid`, because the column is never actually null (the add-lot form and `add_lot`
  both submit the auction's minimum) — and `all_lots` widens it. The number is the **lower quarter**
  of the past prices, rounded **down**, floored at the auction's own minimum: an opening bid is
  meant to be cleared, so every rounding decision is made in the direction of the lot selling.
  Under `MIN_SALES_TO_SUGGEST` (2) past sales it returns **no number** and says why — one sale is
  the fish two people in the room both wanted, which is exactly the sale not to open the next one
  at. It is read-only and there is no bulk write behind it: `edit_lot` sets a minimum bid, one lot
  at a time, which is also why it pages (one comparables query per row).
- `list_lots` takes a **`query`** alongside its status, matched against the lot name *and* the
  species' common and scientific names, so "the unsold daphnia" is one call. `query` had been an
  accepted alias that the resolver dropped, which answered with every unsold lot in the auction.
- `lot_queue` is deliberately **not** admin-only, unlike `/auctions/<slug>/queue/`
  (`LotQueueMixin`). Position is worked out over the whole queue before filtering or slicing.
- `add_lot_image` / `remove_lot_image` set `LotImage.url` (`forms.validate_image_url` checks a
  scheme and an extension and nothing else); `image_source` defaults to `RANDOM`; removing promotes
  another picture to thumbnail. `list_lots` has `without_images` and every row a `has_picture`.
  `Lot.image_permission_check` calls `Auction.permission_check`.
- `add_lots` takes a **`count`**, on the batch or on one entry, and it is the distinction
  `quantity` could not carry: `quantity` is how many fish are in one bag under one lot number, and
  `count` is how many bags there are. `add_lot` accepts it as an alias and hands the whole call to
  `add_lots` rather than spending a correction round. `MAX_LOTS_PER_BATCH` is **40**, over the
  expanded list: it was 12, sized for what somebody says in one sentence, and an agent reading a
  photographed intake sheet is a caller that did not exist then. Nothing else changed — each lot
  still goes one at a time through `QuickAddLot`, one bad lot still does not lose the others, and
  what bounds a runaway agent is `mcp.auth.within_write_budget`, not this number.
- Lot fields reachable from `add_lot` / `edit_lot`: `custom_checkbox`, `reference_link` (validated
  with a `URLField` and set on the instance beside the form; a YouTube link is embedded and plays on
  the lot page), and `summernote_description` capped at `MAX_SPOKEN_DESCRIPTION_CHARS` (600) with
  `use_description` checked. `_lot_field_switched_off` is load-bearing: `QuickAddLot` **hides** a
  disabled field rather than deleting it, so a submitted value would otherwise be saved and printed.
- `update_person` on a club-managed auction goes through `_update_through_the_club` — the club's own
  `ClubMemberAdminForm`, its duplicate checks, its `ClubHistory` line — because
  `CreateEditAuctionTOS` *disables* `bidder_number` and the permission flags in that mode and a
  disabled Django field cleans to its initial value. Which fields go that way is read off
  `form.fields[...].disabled`; the participant row is re-read after `ClubMember`'s `post_save`
  signal and the contact details copied on; changes are reported from the **saved row**. No extra
  club permission is required (wider than `ClubMemberAdminView.post` on purpose). The member row is
  shared, so a corrected name or email is corrected for the club and every other auction.
- `AuctionTOS.actions_dropdown_html` carries Resend membership card and Deactivate/Reactivate club
  member (the same `club_member_confirm` modals the club page opens); `auction_users.html` has the
  club page's `clubMemberListChanged` refresh. Permissions and Discord stay club-page-only.
- `auctions_near_me` has two halves: `your_auctions` (`_my_auctions` — everything this person is in,
  their clubs' auctions included, at any distance, listed or not) and `auctions`
  (`models.nearby_auctions`, promoted only, up to `MAX_SEARCH_MILES` (3000)). No location on the
  account still gets the first half.

## Club-side skills

- `add_club_event` / `update_club_event` (through `ClubEventForm`), `send_club_announcement` /
  `retract_announcement` (through `ClubAnnouncementForm` and `announcements.queue`, so the grace
  window applies), `set_current_auction`, `update_club_setting`, `list_club_events`,
  `list_club_members`, `sync_club_calendar` (`GoogleCalendarSyncNowView`'s body, including the
  forced re-read of whether the calendar is publicly shared), `club_website_snippets` (hands over
  addresses and points at the page; it deliberately does **not** rebuild the iframe HTML).
- Times go through `palette_actions.user_time`, which reads `UserData.timezone`. Auction dates use
  `local_time` and the auction's own timezone.
- `_CLUB_SETTING_PAGES` is the settings table and the **permission is written down per page**:
  membership takes `permission_money` as well as `permission_edit_club`, BAP takes
  `permission_manage_bap`. It reaches five forms plus `_CLUB_INTEGRATION_SWITCHES`, three booleans
  (`add_auctions_to_calendar`, `create_events_for_auctions`,
  `create_discord_events_for_club_events`) that live on no form and get a `modelform_factory` one so
  they are nameable. `ClubPayPalCredentialsForm` is the deliberate exception, in
  `_CLUB_FORMS_NOT_SPOKEN`.
- `club_setup` is an eighteen-row survey of what the site can do for a club, with `show='unused'`.
  How to switch a feature on is **structured, not prose**: `settings` (names `update_club_setting`
  takes), `tool` (a registered action), `page` (for one that genuinely needs a browser — every
  page-only row is an OAuth sign-in with somebody else). It answers with no club at all.
- Guards: `test_palette_skills` fails the build when a view accepting a POST is neither a skill nor
  written down; `EverySettingIsReachableTests` checks that every `ModelForm` over `Club` is in
  `_CLUB_SETTING_PAGES` or `_CLUB_FORMS_NOT_SPOKEN`, that every field on a reachable form can be
  named, that both auction settings forms are covered, and that every `club_setup` row points at a
  real setting or a registered tool.
- Breeder award program: `points_queue` (the Pending BAP page's own `services.bap_review_lots`
  filtered by `filters.ClubBapLotFilter`; statuses **pending**, **approved**, **denied**,
  **missed**), `review_points` (one of three decisions), `award_points` (out-of-band points to a
  member), `my_points` (the member's side).
- `Lot.default_bap_points` is the single precedence: genus rule → category rule → the club's flat
  rate → the category's own default, plus the auction's bonus checkbox. `Lot.bap_placeholder` picks
  BAP/HAP/CAP. `ClubBapLotHTMxTable.render_actions` is the one deliberate copy (it reads the same
  precedence off two prefetched dicts).
- `services.review_lot_points`: undo writes a history line; undoing a lot nobody has decided is a
  quiet no-op, not a refusal. `deny` deliberately leaves `bap_auto_reason` alone. `hap_points` or
  `cap_points` at a club not running that track separately is refused by name.
- `my_points` forecasts through `Lot.unsold_lot_no_bap_reason` (which ignores whether the lot sold),
  four states per lot, capped at `FORECAST_LOT_CAP` (60) and saying so. Totals come from
  `_membership_facts`.
- Scoping: `_bap_club_or_problem` narrows `_club_or_problem` to clubs where the caller holds
  `permission_manage_bap` and the program is on. `_bap_lot_or_problem` is the matching lot
  narrowing — deliberately not `_resolve_lot`, and it matches a plain integer against
  `lot_number_int`.
- **A membership card is a credential.** `_membership_card` is only ever built for the caller's own
  membership; every route (`my_membership`, `renew_membership`, the self half of
  `send_membership_card`) goes through `_my_memberships`, which matches on `ClubMember.user`.
  Sending another member's card takes `ClubMemberResendCardView`'s rules
  (`permission_add_edit`, a club that issues cards, an address on file, not do-not-contact), sends
  to the address already on the membership (there is no parameter for an address), and the reply is
  a sentence with no `membership_number` and no `barcode_url`.
  `test_palette_skills.MembershipCardPrivacyTests` walks every route.
- `list_club_members` filters `is_paid_member` in one query and one Python pass. A row deliberately
  does not say whether that person has an account on this site; the `no_account` *status* stays.

## Account skills

`update_preferences`, `update_contact_info`, `update_username`, `update_printing_preferences` and
`change_email`, each through the page's own form: `UserLocation`, `ChangeUsernameForm`,
`UserLabelPrefsForm`, allauth's `AddEmailForm`. Password, social sign-in and account deletion stay
navigate-only.

- `change_email` records the new address unverified and posts a link to it (`ACCOUNT_CHANGE_EMAIL`);
  the result says `nothing_was_changed_yet`. `_DiscardedMessages` gives allauth's flash message
  somewhere to go in a request with no message storage.
- `services.propagate_contact_info` (extracted from `UserLocationUpdate.form_valid`) corrects the
  `AuctionTOS` and `ClubMember` copies of a person's name, phone and address at once.
  `CONTACT_INFO_RECENT_DAYS` (30) is what "the auctions they are currently in" means; a
  `manually_added` row is skipped.
- `_save_named_contact_fields` cleans each named value through the form's own `forms.Field` and
  saves those alone, so a form failure about fields the caller never mentioned is not a refusal.
- `auctions/geocoding.py` is the server-side geocoder (extracted from `tasks.geocode_club_member` /
  `tasks.geocode_speaker`) and returns Google's formatted address as well as the point.
  `_marker_to_confirm` comes back as a **question**, never a silent save. `PickupLocationForm.clean`
  requires a marker on any non-mail location, so `add_pickup_location` never produces one without
  coordinates. `_coordinate_pair` stays strict and never interprets an address.

## History, help and source

- `recent_changes` (auction `AuctionHistory`) and `club_history` (`ClubHistory`) both take `search`
  (the history page's own `filters.AuctionHistoryFilter` / `ClubHistoryFilter`), `about` (one
  `applies_to`), `days`, `mine` and `assistant`.
- The `about` vocabulary is **read off each model's own `applies_to` choices** plus a per-table
  synonym dict (the two tables disagree: "settings" is a club's `SETTINGS` and an auction's
  `RULES`). `sales`/`sold`/`winners` → **`LOTS`**, where `DynamicSetLotWinner.commit_winner` writes.
  An unknown word is a refusal naming the ones that work.
- An empty answer distinguishes "no changes matching X" from "nothing has been changed yet". `who`
  is the person's name; it and the line are fenced. `club_history` is gated on `permission_view` and
  times go through `user_time`; it deliberately does **not** read a bare `name` as the club.
- `search_help` takes `source` (`faq`, `blog`, or both) and an **optional** `query` — with nothing to
  look for it hands back the FAQ in the page's own order, which is what makes `help://faq`
  attachable. Paging is exact across the two sources. `FAQ.agent_only` keeps an entry off `/faq/`
  while `search_help` still answers out of it; such a row comes back with **no `url`** and
  `on_the_public_faq_page: false`. It is not privacy.
- `read_source` searches, lists, and reads this site's public repository. `auctions/source_code.py`
  downloads the repo as **one archive** from `codeload.github.com` (no credential, ~4.5 MB), caches
  it an hour, with a process-local memo (`MEMO_SECONDS`, 5 min). **Nothing in it touches a
  filesystem path** — the archive is read in memory and every path resolves against a manifest built
  from it, so `.env`, a keyfile and `../../etc/passwd` are all simply not paths. Grep results are
  ranked (definitions, then path matches, then application Python, then design notes, then the rest,
  tests/migrations/vendored last) and capped by `MAX_GREP_PER_FILE` / `MAX_GREP_MATCHES`. It is the
  one tool with `openWorldHint: true` and the one `mcp_only` action: `palette_assist.tools_for`
  drops it and `read_reply` refuses it by name.

## Species over MCP

Three tools for "fix the scientific name on lot 10", depending on what the site knows:
`set_lot_species` (the species is on the list), `name_a_species` (on the list under a name nobody
says — the commonest case), `add_species` (genuinely not there).

`set_lot_species` with no name given re-reads the lot's own name and runs the matcher with the
**language model turned off**. Several matches is a question with the candidates named, never a
pick. Whether the answer is taught to the rest of the site follows `LotAdmin`'s rule: an auction
admin's choice writes `SpeciesSearchCache`, a seller's does not. `record_choice` is reported either
way.

## The page-only writes (`mcp_only`)

Fifteen **writes** exist over `/mcp/` and not in the command palette (`read_source` is the
sixteenth `mcp_only` action and is a read; see the end of this file). The palette still reaches every
one of the pages behind them — `palette_routes` guarantees `go_to_page` does — so this is a
subtraction from one client's *tool list*, not a second catalogue. `Action.mcp_only` has the long
version of why; the short one is that the excuses these views were sitting behind in `NOT_A_SKILL`
were arguments about **speech** ("identifying it out loud is harder than clicking it", "more than
one spoken sentence can carry"), which is true of somebody dictating and empty against a caller
sending a lot number it read out of `list_lots` a moment earlier.

Nothing else about them is different. Each is one row, each re-asks the page's own permission on the
page's own object, and all fifteen are `confirm`-tier and `asks_first`.

- **`remove_lot`** — `LotDelete` and `LotDeactivate`, which are two buttons because there are two
  kinds of lot. A lot **in an auction** is deleted and only while `Lot.can_be_deleted` allows it
  (that property is the whole guard). A **standalone** lot is deactivated, its bids removed, and
  `restore` puts it back; `permanently` deletes one instead.
- **`queue_lot` / `unqueue_lot`** — `LotQueueMixin.add_lot` and the queue page's Remove button, side
  effects included (`Lot.added_to_queue` set once and never unset, `process_queue_notifications`
  run). Keyed on the lot, not on the queue row. **Reordering is deliberately absent**: it writes
  every row in the queue at once.
- **`remove_bid`** — `BidDelete`, and the counterpart `place_bid` never had. Two different gates:
  `Lot.bids_can_be_removed` is about the lot, `Auction.allow_deleting_bids` only decides whether an
  ordinary bidder may take back their own. One `Bid` row per user per lot, so this is one row.
- **`remove_award`** — `BapAwardDeleteView`, keyed on the **lot** and only on the lot; an award for
  a talk has no handle anybody can say and stays on the page. Resets the lot's award fields so it
  goes back on the pending list rather than sitting there decided.
- **`set_member_active`** — `ClubMemberDeleteView` / `ClubMemberReactivateView`. One boolean, each
  its own undo, so one **idempotent** tool. The hard delete and the merge stay pages.
- **`remove_person`** — `AuctionTOSDelete`'s delete path, and only the one-row half of it. Somebody
  with an invoice, lots to sell or lots they won is **refused with the reason** and sent to the
  merge form; deleting a participant cascades their invoice, adjustments and payments away.
- **`remove_invoice_adjustment`** — the delete half of the invoice page's formset, and the undo
  `add_invoice_adjustment` shipped without. The line is named by **what it says**; several matches
  is a question. Refuses on a settled invoice, exactly as adding does.
- **`set_point_rule`** — the two BAP override save views. A club **rule**, not a table row:
  `ClubBapGenusOverrideForm` refuses a genus no species belongs to, and the answer says which of the
  two kinds it wrote, because a genus rule outranks a category rule.
- **`set_invoice_renewal`** — `InvoiceRenewalNeededToggleView`. Also applies the member discount and
  the alternate split, so the answer carries the new total. **The permission is the invoice's own**:
  an auction invoice asks the auction, a club invoice asks the club.
- **`resend_member_card`** — `ClubMemberResendCardView`. The admin twin of `send_membership_card`,
  which only ever sends the caller their own. No email on file and do-not-contact are answers.
- **`leave_feedback`** — `Feedback`, the only one here that is not administration. Which side the
  caller is on is read off the lot, not asked; buyer feedback lands in `Lot.feedback_*` and seller
  feedback in `Lot.winner_feedback_*`.
- **`hide_chat_message`** — `AuctionChatDeleteUndelete`, a toggle and so its own undo. The message is
  named by a phrase out of it, and both the question and the answer keep the guillemets.
- **`record_club_money`** — `ClubMoneyCreateView`. **No money moves**; this is a bookkeeping row
  behind `permission_money`. The invoice-reconciled categories and the balance adjustment are
  refused, because a hand-entered one is undone at the next reconcile.
- **`rotate_lot_image`** — `ImagesRotate` and `ImagesPrimary`. Filed under "needs a file" and it
  takes an angle. The genuinely new thing is the caller: a client that can see the picture can be
  handed its address and say it is sideways.

`GoogleCalendarSyncNowView` moved into `SKILLS` at the same time and is **not** a new skill — it was
a filing error. `sync_club_calendar`'s docstring has always said it is that view's body, and the
view sat in `NOT_A_SKILL` as outside-service setup because the audit only asked whether a view was
in one table *or* the other. `test_palette_skills` now asks the third question too, and a second one
besides: no excused view may be reimplemented by a resolver that says so in its own docstring.

Deliberately **not** done in this pass: **banning and unbanning**. The ban is wanted and is
reversible by its twin, but `CreateUserBan` also deletes that user's live bids across every auction
the admin runs, which is more than one row. The `_BAN` reason in `NOT_A_SKILL` is the record of that
decision rather than an omission.

## No lot ever travels as a primary key

A lot's public identifier is `lot_number_display` — printed on its label, in its URL
(`/auctions/<auction>/lots/<number>/`), and what somebody in the room says. Every result that names
a lot already carries it, through `palette_actions._lot_echo`.

The pk was a second name for the same thing: one an agent could only ever have got from us, that
means nothing to whoever reads the answer, that addresses a lot in *any* auction rather than a lot
in this one, and that disagrees with the label whenever an auction numbers its lots by hand. So:

- `mcp.tools._INTERNAL_RESULT_KEYS` strips `lot_id` **at any depth**, alongside `undo`. The depth
  matters: the leak was mostly in rows — `find_lot` and `points_queue` put one on every line.
- No tool advertises it. `review_points` and `print_labels` each documented a `lot_id`; both now
  document the lot number. It stays in their `aliases`, so the palette's page context still works
  and nothing that already had one breaks.
- The one `more_info_needed` that answered with a pk (`_bap_lot_or_problem`) answers with the lot
  number, which the same resolver already accepts.
- `test_mcp.RegistryConformance.test_no_tool_advertises_a_lots_primary_key` and
  `CallToolTests.test_no_result_hands_out_a_lots_primary_key` keep it that way.

`image_id` is the deliberate exception, and the reason the tests name `lot_id` rather than every key
ending in `_id`: **a photo has no number on a label**. It is a handle an agent can only get from
`describe_lot` in the same conversation, which is the closest thing a picture has to a public name.
