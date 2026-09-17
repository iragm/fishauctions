# MCP spec features we don't use

Checked against MCP **2025-11-25**. This transport answers one POST with one JSON body and holds no
session, so anything where the server speaks first is out by construction. The API-level MCP
connector does tools only; prompts and resources reach Claude through a custom connector.

## Worth doing

1. **`_meta["anthropic/requiresUserInteraction"]`** on `send_club_announcement`,
   `retract_announcement`, `update_auction_setting`, `undo_sale` and `place_bid`: a permission prompt
   on every call. Never on `check_in` (eighty a night). Reconcile with `destructive` so the two lists
   don't drift.
2. **Incremental scope consent (SEP-835).** Connect read-only, return a `401` naming `write` on the
   first write. `mcp/auth.py` must tell "no token" from "token without this scope".

## Decided against

- **Source as a resource** (`source://{path}`): the path has to be searched for anyway.
- **GitHub code search** for `read_source`: needs a credential on every deployment and fork. One
  anonymous archive download doesn't.
- **Slugs in `resources/list`**: a list of auctions is enumeration. `help://faq` is listed because it
  is the same for everyone. Same reason `completion/complete` refuses `ref/resource`.
- **Inline `data:` icons**: `tools/list` is paid for every session. URLs, five of them, derived.
- **`defer_loading`**: the API caller's setting, not ours. `?tools=` is our lever.
- **Elicitation, sampling, tasks, subscriptions, `notifications/message`**: all need a session.
- **`outputSchema`**: a schema loose enough for every result validates nothing.
- **OIDC discovery**: we are the authorization server.
