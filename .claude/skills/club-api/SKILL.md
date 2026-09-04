---
name: club-api
description: The club REST API at /api/v1/clubs/<slug>/ -- key permissions, the private block, lot filtering and ordering rules. Use when touching auctions/views/club_api.py, auctions/serializers.py, ClubAPIKey, or the API documentation include.
---

# The club API

`/api/v1/clubs/<slug>/…`, authenticated with a `ClubAPIKey` (`X-API-Key`, prefix `ck_`) or by a
signed-in club admin. One checkbox per capability on the key; `ClubAPIViewMixin.require_club_permission`
takes the key flag *and* the equivalent `ClubMember` permission, so both callers go through one gate.
`/clubs/<slug>/api-keys/<pk>/` is the documentation — every endpoint is written up there, behind the
`{% if %}` for its own permission, and nowhere else. It has **two readers and one copy**: the page
draws `auctions/templates/auctions/_club_api_endpoints.html`, and the `club_api` MCP tool renders
that same include as text for an agent writing an integration, with an unsaved key holding exactly
the permissions being documented. Both fill it in from `views.club_api_documentation_context`, so a
number in an example is the number the code enforces.

Members, BAP points/lots and species lookup came first. The read-only auction and lot feed is three
more checkboxes:

- `can_read_auction_info` → `auctions/` (list, with the `current` and `latest` slugs named in it)
  and `auctions/<identifier>/`.
- `can_read_public_lots` → `auctions/<identifier>/lots/` and `.../lots/<lot number>/`.
- `can_read_private_lots` → **the one privacy flag.**

`<identifier>` is an auction slug or the word `current` or `latest`; a real slug wins, so the words
are only ever a fallback. `current` is the pinned `Club.current_auction` if it hasn't wound down,
else the soonest one that hasn't — deliberately looser than `views._club_current_auction`, which
serves the public website embed and will only offer a *promoted* auction. `latest` is the last one
created, promoted or not.

- **Everything that names somebody is in a `private` object that is absent, not null, without the
  flag** (`serializers.PrivateBlockMixin`): buyer and seller names, emails and bidder numbers.
  Removed lots are in the same bargain — excluded entirely from a public key's answer, returned
  with `private.removed` to a key that can read private info. Deleted lots never come back at all.
- **`google_drive_link` is on no tier.** That sheet is shared "anyone with the link can view", so
  the link *is* the credential, and no checkbox on a key should hand out the club's spreadsheet.
- Every reference carries both halves: `{"id": 7, "name": "Cichlids"}`.
- Lot filtering has one rule: **a parameter named after a column matches that column, and
  `?filter=` is the one that looks everywhere.** The narrow ones are `lot_name`, `description`,
  `custom_field_1`, `custom_dropdown` (the whole value — it is a controlled vocabulary the auction
  publishes as `lot_fields.custom_dropdown_options`), `lot_number` (both spellings), `category` /
  `category_id` (through the same `views._resolve_category` the species lookup uses), `species_id`,
  `sold`, `donation`, `i_bred_this_fish`, `custom_checkbox`. `count` is the filtered total. `sold`
  is spelled out as winner-**and**-price because `Lot.sold` is a property. A value it cannot parse
  is a 400, never a shrug — a filter that silently does nothing shows up as a club's front page
  listing the whole auction.
- **A `?filter=` that is all digits is a lot number**, and skips the text columns entirely —
  otherwise `1` matches `10 gallon` and half the descriptions in the auction, and buries the one
  lot the person was after. `?description=1` is still there for digits in the prose.
- **`?filter=` searches public columns only, for every caller** (`views.LOT_GENERIC_FILTER_COLUMNS`).
  `filters.LotAdminFilter` — the admin page's version of the same box — also searches seller name,
  username and bidder number, and copying that list would let a public key confirm a name one
  character at a time. `?seller=` / `?winner=` (name, bidder number or email) carry that instead and
  **refuse** a key without the privacy flag rather than matching nothing, so one `?filter=` means
  one thing whoever sends it.
- **`?ordering=` is an allowlist** (`views.LOT_ORDERING`), not a pass-through to `order_by`: a
  caller who can name any column can order by `auctiontos_winner__email` and binary-search the
  auction's email list out of the sort order without ever holding the private permission.
- `?fields=` narrows each lot (`serializers.SparseFieldsMixin`, applied in `__init__` so an omitted
  field costs no queries). It cannot conjure `private` — the mixin pops that afterwards.
- Each lot's `url` ends in `?src=<key name>`, which is the parameter `PageView` tracking already
  reads — a club that publishes this feed sees its own website in the auction's stats.
- `thumbnail` is one link for a lot tile; `images` is every picture with a full-size and a thumbnail
  URL. Both are absolute (`serializers._absolute`) — Cloudflare hands back absolute URLs and local
  media does not. `views._lot_images_by_owner` and `_auto_images_by_lot_name` are `Lot.images` and
  `Lot.auto_image` batched for a whole page; the second is `models.find_image`'s rule minus its
  per-user preference, and it needs an `AuctionTOS` admin row, not just `created_by`.
- Money is always a string. `serializers.DecimalField` renders one; a raw `Decimal` in a hand-built
  dict comes out of DRF's encoder as a float, which is why `fees.minimum_bid` is formatted by hand.
