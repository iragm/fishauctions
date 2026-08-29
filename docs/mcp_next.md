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

**Prompts, with argument completion** (`auctions/mcp/prompts.py`). Five recipes — `run_check_in`,
`chase_unpaid`, `set_up_next_year`, `write_announcement`, `build_an_integration` — plus
`completion/complete` for their
`auction` and `club` arguments, scoped to what the caller is actually in. The reason to want
prompts turned out to be the one predicted here and it is worth restating: a tool is chosen by a
model reading a description, a prompt is chosen by a **person** picking it off a menu, and that is
what makes a prompt the only safe place on this server for a multi-step recipe. Nothing in a prompt
body is interpolated except its own arguments, and a test enforces that — a prompt that could carry
a lot description would be a prompt-injection surface with a menu entry.

**`resource_link` blocks in tool results** (`resources.links_for`). A result that named an
auction, a club or a lot now carries a link to the URI that *is* that thing, so a host can fetch
the whole record without the model spending a turn choosing a tool and guessing a slug.

The thing worth writing down is where the slug comes from. The obvious implementation reads it out
of the answer, and the answer cannot be read: `auction` is the auction's **slug** in `_lot_echo`
and its **title** in `list_lots` and `describe_lot` — both right where they are, and a URI built by
guessing between them is a link that 404s. So the resolver says, in `palette_actions.KEY_ABOUT`
(`_about`), which is bookkeeping and is stripped on every surface before anything reaches a person
or a model. A tool never links to its own answer: `describe_lot` returning `lot://spring/14` is a
pointer at the document it just sent, so that one is dropped — and what goes in its place is what
sits *underneath* it, which is how `describe_auction` comes to offer the auction's lots and its
people while `list_lots` offers only the auction. One of them has already answered the top-level
thing and the other has not.

The other design note is what is *not* linked. Rows in a long list are not: linking eight of a
hundred lots is a sample nobody asked for, and `MAX_LINKS` is twelve because that is the shape of
`my_context` — every club somebody belongs to and every auction running at once.

**Icons on everything worth putting one on** (`auctions/mcp/icons.py`). Tools, prompts, the data
resources and resource templates, and the server's own `serverInfo`, which also gained
`websiteUrl`. Not the `ui://` widget documents: a widget is rendered rather than browsed, so a
thumbnail beside its name is a picture of nothing.

Two decisions worth keeping. They are **URLs on this site, not inlined `data:` URIs**: `tools/list`
is paid for in full, in context, by every host every session, and five inlined SVGs at ~400 bytes
each times sixty-odd tools is a real regression for decoration, where a URL is sixty. As shipped
they are 9.7% of `tools/list` and a test fails the build above 15%. And there are **five, derived**
— read off the danger tier and `tools.area_of`, exactly as the annotations are — rather than
sixty-odd chosen by hand, so there is no second table to keep in step. No `sizes` on them either:
they are SVG, every size is the right size, and `["any"]` is twenty-five characters saying so once
per tool. The raster favicons in `serverInfo` are the one place a host has a real choice to make,
and they are the one place `sizes` is sent.

**Resource templates** (`auctions/mcp/resources.py`). `auction://{auction}`,
`auction://{auction}/lots`, `auction://{auction}/people`, `auction://{auction}/history`,
`lot://{auction}/{lot}`, `club://{club}`, `club://{club}/events`, `club://{club}/history`, plus the
fixed `me://context` and `me://activity` and the public `help://faq`. Each one names a registered
**read-only** action and is served by calling it with the caller's own request, so there is no
second permission path — the audit in `test_mcp_resources` fails the build if a template ever names
a write.

Two things learned doing it. The token argument is narrower than it looks: attaching a resource
does not shrink `tools/list`, it saves the *turn* — the model choosing a tool, guessing the auction
slug, and being told it guessed wrong. And **nothing that names somebody** may appear in
`resources/list`, because a list of `auction://spring-2027` is a list of which auctions exist handed
to whoever asked; enumeration stays in the tools, behind a check that knows whose auctions they are.
The rule is *no slugs*, not *nothing concrete* — which is what lets `help://faq` be listed, since it
is the same document for every caller. The same reasoning is why `completion/complete` answers
`ref/prompt` and refuses `ref/resource`.

**The source code is a tool and deliberately not a resource.** `read_source` reads this site's own
published repository (`SOURCE_CODE_URL`) — searching the code, listing directories, reading numbered
pages of files — and a `source://{path}` template would save nothing: the value of a resource
template is skipping the turn where the model guesses a slug, and here it has to find the path by
searching anyway. It is also the only action with `open_world` set — it is the one thing on the
catalogue that talks to anything but this site's own database — and the only one with `mcp_only`,
which keeps a page of Python out of the command palette's one-line answer box.

Worth recording because it will be re-proposed: **GitHub's code search API was not used.** It
refuses anonymous callers, so every deployment and every fork would need a credential to answer
"how does X work". Downloading the repository as one 4.5 MB archive needs none, is a second,
supports exact substring matching that the token-based index does not, and answers listings and
reads out of the same fetch.

---

## Worth doing

### 1. `_meta["anthropic/requiresUserInteraction"]` on the tools that deserve it

Claude-specific, read straight off `tools/list`: it forces the full permission prompt on **every**
call, in every permission mode, with no "don't ask again" and no one-tap approval from Remote
Control. Verified for Claude Code v2.1.199 and later; older versions and other surfaces ignore it,
which makes it a free improvement rather than something to rely on.

Our `DANGER_CONFIRM` tier already knows which tools these are, but the tier is coarse — `check_in`
is a confirm-tier write somebody does eighty times in an evening, and `send_club_announcement` is a
confirm-tier write that reaches every member of the club at once and cannot be fully taken back.
`Action.asks_first` has since drawn half of that line for the palette (`check_in` is the one action
with it set to `False`), but it is the *cheap* half: it says which writes need no ceremony, not
which ones need more. This entry is still the other half.

Candidates, and only these: `send_club_announcement`, `retract_announcement`, `update_auction_setting`
(it is the way in and out of `promote_this_auction`), `undo_sale`, and `place_bid` — which is the
strongest case on the list and arrived after it was written, being the only write here that nothing
can take back. Everything else stays as it is; a per-call prompt on `check_in` would get the whole
connector switched off.

`place_bid` already carries `destructiveHint` for that reason, which is a widening of what that
flag meant here (it was "overwrites a previous answer"; a bid overwrites nothing and is simply
irreversible). Both are the same question from a host's side — must you ask first? — so if this
entry is ever built, `place_bid` and `destructive` should be checked against each other rather than
drifting into two half-overlapping lists.

This is the closest thing on the list to free safety, and it is one derived boolean in `descriptor()`.

### 2. Incremental scope consent (SEP-835)

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
