# What else the MCP spec has that we aren't using

Written August 2026, against MCP **2025-11-25** (the version `protocol.SUPPORTED_PROTOCOL_VERSIONS`
leads with) and the Claude connector docs of the same date. `CLAUDE.md` describes what the endpoint
*does*; this is the list of what it could do next, with the ones not worth doing said out loud so
nobody re-investigates them.

Two facts frame the whole list, because between them they decide most of it:

* **The API-level MCP connector supports tool calls and nothing else.** Prompts, resources and
  elicitation reach Claude only through a *custom connector* on claude.ai, Desktop, mobile or Claude
  Code — which is how every real user of this endpoint connects, so it is not much of a limit. It
  does mean "does Claude support X" has two answers and the connector docs give the narrower one.
* **This transport answers one POST with one JSON body and holds no session.** Anything that needs
  the server to speak first — elicitation, sampling, progress notifications, resource subscriptions
  — is unavailable here by construction, not by omission. Fixing that means an SSE stream and a
  session table, which is a much larger decision than any single feature on this list.

---

## Built since this was written

**Prompts, with argument completion** (`auctions/mcp/prompts.py`). Four recipes — `run_check_in`,
`chase_unpaid`, `set_up_next_year`, `write_announcement` — plus `completion/complete` for their
`auction` and `club` arguments, scoped to what the caller is actually in. The reason to want
prompts turned out to be the one predicted here and it is worth restating: a tool is chosen by a
model reading a description, a prompt is chosen by a **person** picking it off a menu, and that is
what makes a prompt the only safe place on this server for a multi-step recipe. Nothing in a prompt
body is interpolated except its own arguments, and a test enforces that — a prompt that could carry
a lot description would be a prompt-injection surface with a menu entry.

**Resource templates** (`auctions/mcp/resources.py`). `auction://{auction}`,
`auction://{auction}/lots`, `auction://{auction}/people`, `lot://{auction}/{lot}`,
`club://{club}`, `club://{club}/events`, plus the fixed `me://context` and `me://activity`. Each
one names a registered **read-only** action and is served by calling it with the caller's own
request, so there is no second permission path — the audit in `test_mcp_resources` fails the build
if a template ever names a write.

Two things learned doing it. The token argument is narrower than it looks: attaching a resource
does not shrink `tools/list`, it saves the *turn* — the model choosing a tool, guessing the auction
slug, and being told it guessed wrong. And nothing concrete may ever appear in `resources/list`,
because a list of `auction://spring-2027` is a list of which auctions exist handed to whoever
asked; enumeration stays in the tools, behind a check that knows whose auctions they are. The same
reasoning is why `completion/complete` answers `ref/prompt` and refuses `ref/resource`.

---

## Worth doing

### 1. `resource_link` blocks in tool results (2025-06-18)

A tool result may contain `resource_link` content blocks alongside text. `print_labels`,
`go_to_page`, `renew_membership` and every `followups` entry are URLs stuffed into JSON today; as
resource links they become something the host can render as a real link or offer to open. Small
change (`mcp/tools.py::_result`), immediate polish, no new concepts.

### 2. `_meta["anthropic/requiresUserInteraction"]` on the tools that deserve it

Claude-specific, read straight off `tools/list`: it forces the full permission prompt on **every**
call, in every permission mode, with no "don't ask again" and no one-tap approval from Remote
Control. Verified for Claude Code v2.1.199 and later; older versions and other surfaces ignore it,
which makes it a free improvement rather than something to rely on.

Our `DANGER_CONFIRM` tier already knows which tools these are, but the tier is coarse — `check_in`
is a confirm-tier write somebody does eighty times in an evening, and `send_club_announcement` is a
confirm-tier write that reaches every member of the club at once and cannot be fully taken back.

Candidates, and only these: `send_club_announcement`, `retract_announcement`, `update_auction_setting`
(it is the way in and out of `promote_this_auction`), `undo_sale`. Everything else stays as it is;
a per-call prompt on `check_in` would get the whole connector switched off.

This is the closest thing on the list to free safety, and it is one derived boolean in `descriptor()`.

### 3. Tool icons (SEP-973, 2025-11-25)

Servers may attach icons to tools, resources and prompts — and there are prompts and resources to attach them to now. Fifty-odd tools in a host's list all
share one generic icon today. A handful of inline SVG data URIs — a fish for the lot tools, a
person for the check-in ones, a receipt for invoices, a calendar for club events — keyed off
`tools.area_of`, which already classifies every tool. Cosmetic, cheap, and it makes a fifty-tool
list scannable.

### 4. Incremental scope consent (SEP-835)

The spec now allows a server to answer a call with a `401` naming the **scope it needed**, so a
client can go and get consent for just that. Our design already says `allow_writes` and the `write`
scope are a *ceiling, not a grant* — this is the missing half: connect read-only by default, and
ask for `write` the first time somebody actually tries to check a bidder in. That is a better
default than the current "tick write at connect time and hope", especially for the `static_headers`
case where one credential covers a whole org.

Medium cost: `mcp/auth.py` would need to distinguish "no token" from "token without this scope", and
`WWW-Authenticate` would need the `scope` parameter. Worth doing before this is offered to clubs
that are not already running it.

---

## Not worth doing, and why

* **`defer_loading` / tool search.** Not ours to set — it is a property of the API caller's request
  (`mcp_toolset.default_config`), and Claude Code already defers every MCP tool by itself. The
  server-side lever that *does* exist is `?tools=`, which stays in `parse_areas`. See `CLAUDE.md`.
* **Elicitation, including URL-mode elicitation (SEP-1036).** Would be a beautiful fit for
  `join_auction` ("read the rules, then agree") and for the whole `navigate` tier. It is a
  server-to-client *request* raised mid-call, so it needs the call to stay open across a round trip.
  One POST, one body, no session. Same reason `more_info_needed` exists as a successful result.
* **Sampling, with or without tools (SEP-1577).** Tempting — the host has a model, so species
  matching could run without this site holding an `OPENAI_API_KEY`. Same blocker: server speaks
  first.
* **Tasks (SEP-1686, experimental).** Would suit `send_club_announcement`, which is already a Celery
  job behind a 30-second grace window, and the invoice mailer. Blocked on the same session problem,
  and it is marked experimental. Revisit if we ever add a stream.
* **`outputSchema`.** Declined on purpose and the reasoning is in `CLAUDE.md`: a schema loose enough
  to describe all fifty-odd results validates nothing, and costs seven kilobytes a session to do it.
* **OpenID Connect Discovery (2025-11-25).** We are our own authorization server. Nothing to
  discover elsewhere.
* **`logging/setLevel` and `notifications/message`.** Log level is a deployment decision, and there
  is no stream to send notifications down.
* **Resource subscriptions.** Advertised as `false` and correct: no session, nowhere to send the
  update.
