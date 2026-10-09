# MCP spec features we don't use

Checked against MCP **2025-11-25**. This transport answers one POST with one JSON body and holds no
session, so anything where the server speaks first is out by construction. The API-level MCP
connector does tools only; prompts and resources reach Claude through a custom connector.

## Worth doing

1. **The scans themselves over MCP.** `add_document` takes the agent's transcription, so the library
   has no picture to check it against. Once a client can hand over a file it is holding (tool
   arguments are text today), keep the original beside the text.

## Decided against

- **`_meta["anthropic/requiresUserInteraction"]`** on the riskier writes (announcements, auction
  settings and dates, undo_sale, place_bid): a prompt on every call, with no "always allow", is too
  heavy for writes that aren't bad, and a routine can't run them at all.
- **Incremental scope consent** (connect read-only, step up to `write` on a `403 insufficient_scope`):
  a read-only connection confuses people, and whether claude.ai and ChatGPT step up is unproven. The
  admin endpoint is where read-only lives.
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
