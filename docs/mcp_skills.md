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
- `lot_queue` is deliberately **not** admin-only, unlike `/auctions/<slug>/queue/`
  (`LotQueueMixin`). Position is worked out over the whole queue before filtering or slicing.
- `add_lot_image` / `remove_lot_image` set `LotImage.url` (`forms.validate_image_url` checks a
  scheme and an extension and nothing else); `image_source` defaults to `RANDOM`; removing promotes
  another picture to thumbnail. `list_lots` has `without_images` and every row a `has_picture`.
  `Lot.image_permission_check` calls `Auction.permission_check`.
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
