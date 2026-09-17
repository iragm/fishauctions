---
name: club-api
description: The club REST API at /api/v1/clubs/<slug>/ -- key permissions, the private block, lot filtering and ordering rules. Use when touching auctions/views/club_api.py, auctions/serializers.py, ClubAPIKey, or the API documentation include.
---

# The club API

`/api/v1/clubs/<slug>/…`, authenticated by a `ClubAPIKey` (`X-API-Key`, `ck_`) or a signed-in club
admin. `require_club_permission` checks the key flag and the matching `ClubMember` permission.

**The documentation is `_club_api_endpoints.html`, once.** The key page draws it and the `club_api`
MCP tool renders it, both from `views.club_api_documentation_context`.

- `can_read_auction_info`: `auctions/` and `auctions/<identifier>/`.
- `can_read_public_lots`: `…/lots/` and `…/lots/<lot number>/`.
- `can_read_private_lots`: the privacy flag.

`<identifier>` is a slug, or `current` / `latest` (a real slug wins). `current` is looser than
`views._club_current_auction`: it doesn't require a promoted auction.

- **Anything naming a person is in `private`, absent (not null) without the flag.** Removed lots are
  excluded from public answers. Deleted lots never appear.
- **`google_drive_link` is on no tier.** The link is the credential.
- References carry `{"id", "name"}`.
- A parameter named after a column filters that column; `?filter=` searches all public columns.
  Unparseable values are 400s.
- An all-digit `?filter=` is a lot number only.
- **`?filter=` never searches private columns**, for anyone, or a public key could confirm a name
  letter by letter. `?seller=` / `?winner=` refuse without the flag.
- **`?ordering=` is an allowlist** (`LOT_ORDERING`); sorting by email would leak it.
- `?fields=` can't bring `private` back.
- Lot `url` ends in `?src=<key name>` for `PageView` tracking.
- Image URLs are absolute. `_auto_images_by_lot_name` needs an `AuctionTOS` admin row.
- Money is always a string; a raw `Decimal` in a dict comes out as a float.
