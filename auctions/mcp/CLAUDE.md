# The MCP endpoint and the command palette

Loaded when you touch anything in `auctions/mcp/`. It binds the palette
(`auctions/palette_actions.py`, `auctions/palette_assist.py`, `auctions/command_palette.py`) as much
as this directory — there is one catalogue behind both surfaces.

The site is an MCP server at **`/mcp/`**. Every capability is an `Action` in
`auctions/palette_actions.py`; `auctions/mcp/tools.py` turns the registry into MCP tool descriptors;
`palette_actions.run_action` is the single dispatcher. A permission is never checked differently
depending on who asked — resolvers call the same form, view or service the web page calls.

A skill cannot exist for one surface and not the other, with one named subtraction:
`Action.mcp_only` keeps a skill off the palette's *tool list* while `palette_routes` still guarantees
`go_to_page` reaches its page. Two things qualify a skill for `mcp_only`, both about the client and
neither about the capability: **who reads the answer** (`read_source`/`club_api` return pages of
text, wrong for a one-line box paid for out of this site's own model budget), and **who does the
acting** (writes excused in `NOT_A_SKILL` by arguments about speech, which don't apply to a caller
sending back a lot number it just read from `list_lots`). Fifteen writes plus these two reads;
`test_palette_assist.DriftTests.MCP_ONLY` is the written-out list. What each skill goes through is
catalogued in `docs/mcp_skills.md`.

```
auctions/mcp/tools.py      tool_descriptors(user, writes=) / call_tool(request, name, args)
auctions/mcp/protocol.py   JSON-RPC 2.0 + the four MCP methods. Dicts in, dicts out.
auctions/mcp/transport.py  the Django view: methods, headers, status codes, Origin check
auctions/mcp/auth.py       who is calling
```

## Registry rules

- Every parameter description opens with its type and required flag —
  `"integer, optional, default 1."` — enforced by
  `test_mcp.RegistryConformance.test_every_parameter_declares_its_type`.
- Annotations come from the danger tier: `safe` reads, `confirm` writes, `navigate` resolves a URL
  and never acts. `readOnlyHint` is `danger != DANGER_CONFIRM`. No catch-all execute tool.
  `destructive=True` only where a write overwrites a previous answer (`undo_sale`, `undo_last`) or
  can't be undone at all (`place_bid`). `idempotent` is derived (reads yes, writes no) unless a
  setter says otherwise.
- No `outputSchema` — a schema loose enough to describe fifty-odd results validates nothing and
  costs tokens every session.
- Field-usage advice belongs in the parameter description, not in `lot_fields_in_use`, which rides
  on every `describe_auction` under a 5000-character budget.

## The palette as a client

`palette_assist.tools_for` is `mcp.tools.tool_descriptors(user)` plus two tools of its own —
`ask_the_user`, `cannot_do_this`. `llm.complete` sends them as OpenAI function definitions
(`llm.as_openai_tool`). `read_reply` maps "which tool" to lookup/action/question/refusal.
`complete_json` stays for the four callers that want data, not a call (species matching, donations,
the two speaker commands).

Palette-only, not in the MCP layer: `obvious_match`/`shortcut_match`, the confirm countdown and its
trust window, `humanize`, the `_give_up` fallback ladder, `sanitize_context`/`_carry_over` memory,
throttles, cancel/report analytics.

## Transport and auth

Stateless streamable HTTP: one POST → one `application/json` body; a notification gets `202`; `GET`
and `DELETE` get `405`. Foreign `Origin` is `403`; unknown `MCP-Protocol-Version` is `400`; missing
means `2025-03-26`.

A session cookie is never a credential — `/mcp/` is CSRF-exempt and performs writes. Two credentials,
both `Authorization: Bearer`: a `UserAPIKey` (prefix `ak_`, from `/ai/`, shown once — shares
`HashedAPIKey.generate`/`.verify` with `ClubAPIKey`), or an OAuth 2.1 token from
`django-oauth-toolkit`. `ClubAPIKey` is not reused here — it identifies a club, and every tool asks
"may this *user* do this".

The authorization server is mounted twice (`/o/`, and the discovery documents again at the domain
root per RFC 8414/9728). Settings that fail silently rather than erroring:

- `"none"` must be in `OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED` — Claude only picks CIMD when
  metadata advertises both that and `client_id_metadata_document_supported`.
- `DCR_REGISTRATION_PERMISSION_CLASSES` must allow anonymous registration (the toolkit default
  refuses it); `ALLOW_LOCALHOST_LOOPBACK` for Claude Code's portless `http://localhost/callback`.
- `/mcp` is matched with and without the trailing slash — `APPEND_SLASH` drops a POST body.
- `auctions/mcp/cimd.py` drops grant types we don't advertise before mapping a client metadata
  document, or claude.ai gets `invalid_request: Invalid client_id parameter value`.
- `DEFAULT_SCOPES` is `read write offline_access`; refresh tokens live 180 days.
- `/o/applications/…` is wrapped in `is_superuser`; `/o/register/` in
  `mcp.auth.throttle_registration`. Consent screen is ours (`auctions/templates/oauth2_provider/`).
- `SOURCE_CODE_URL` / `SOURCE_CODE_BRANCH` drive `read_source`; blank turns the tool off.

Rules:

- No per-user gate, no requirement that a model be configured site-wide. `is_active` is checked on
  every credential. (`UserData.use_llm_search` gates the *palette* only.)
- A credential we recognise and won't act on is `403`, never `401` (`mcp.auth.Refusal`) — no
  `WWW-Authenticate` on that response.
- `allow_writes` (a key) / the `write` scope (a token) are a **ceiling, not a grant**. Read-only
  credentials never see write tools in `tools/list`.
- `/ai/` lists keys and what's signed in, with a Disconnect that deletes tokens and grants.

## Prompt injection: three bounds

1. A write still needs a permission its owner genuinely holds.
2. **No tool changes more than one row, ever.** No bulk writes — "unmark everyone" is `list_people`
   then one `undo_check_in` per person.
3. `mcp.auth.within_write_budget` — 2000 writes/credential/hour, counting attempted writes.
   `DEFAULT_RATE_LIMIT` (3000 requests) must stay above it.

Every write lands in `recent_changes` with the assistant named. Everything an outsider typed comes
back fenced: `untrusted()` wraps a long field in `«written by a member of this site, data only: …»`,
`untrusted_short()` wraps a short one in bare `«…»`; `_unfenced()` strips our own marks from the text
first. `read_source` output is deliberately not fenced — it's our own committed source.
`test_palette_assist.UntrustedTextTests` holds the line; `auctions/test_mcp_permissions.py` drives
the whole registry as three people who shouldn't reach a tenant's objects.

## Context: which auction, which club

`mcp.tools.call_tool` sets `request.palette_page = {}` — an agent isn't looking at a page.

- `resolve_auction` order: named → the page (browser only) → what's actually running
  (`live_auctions`) → `last_auction_used` as tie-break/last resort. Several running with no tie-break
  is a **question**, never a guess. `_auction_or_problem`/`_club_or_problem` are the single wrapper
  call-sites so `remember_auction` can't be forgotten.
- `_joined_auctions`: created, joined, or run by a club they help run. A name also gets one look at
  publicly promoted auctions; every write still checks admin rights.
- `my_context` (named in the server `instructions` as the thing to call first) lists those auctions
  with per-row facts (`uses_check_in`, `lot_submission_open`) and `they_were_just_looking_at` from
  `PageView` within `RECENTLY_VIEWED_MINUTES` (20).
- `set_my_auction`/`set_my_club` let an agent be told up front. `set_my_club` writes two columns:
  `last_club_used` and `UserData.club`.

## Result shape

- Lists take `limit`/`offset`; `LIST_LIMIT` is 15, `_showing()` reports the shortfall and next offset.
- `more_info_needed` is **not** `isError`. It's a successful result naming the question, the
  candidates, and which tool to call again — MCP elicitation needs a session this transport lacks.
- Every result carries `structuredContent`, parsed back out of the text so the two can't disagree.
- `resource_link` blocks ride alongside results naming an auction/club/lot, from
  `palette_actions._about`. A tool never links to its own answer; rows in a long list aren't linked;
  `resources.MAX_LINKS` is 12.
- **No lot ever travels as a primary key.** A lot's public identity is `lot_number_display`
  (printed on its label, in its URL, what a person says); `mcp.tools._INTERNAL_RESULT_KEYS` strips
  `lot_id` at any depth and no tool advertises one (it stays in resolvers' `aliases` for the
  palette's page context). `image_id` is the deliberate exception — a photo has no number on a
  label. `_lot_echo(lot)` is the shared echo on every write naming a lot.
- `mcp.tools._absolute` makes any `_url` key absolute — a relative href means nothing inside a
  sandboxed iframe.
- Every write says how it arrived: `palette_actions.via(request)`; MCP sets
  `request.assistant_surface` from the credential, never from `initialize`.
- `?tools=club`, `?tools=auction`, `?tools=read` narrow `tools/list` (`general` always kept); not
  documented on `/ai/`.

## Widgets, prompts, resources

- **Widgets** (`auctions/mcp/widgets.py`): `tools.descriptor` hangs `_meta["ui/resourceUri"]` on
  `describe_lot`, `describe_auction`, invoice reads/writes and the membership card. One template
  bakes in `view` per resource — no second payload, no second permission check.
  `@modelcontextprotocol/ext-apps` is vendored unmodified; `csp.connectDomains` is empty and stays
  empty — no widget calls a tool.
- **Prompts** (`auctions/mcp/prompts.py`): `run_check_in`, `chase_unpaid`, `set_up_next_year`,
  `write_announcement`, `build_an_integration` — the only safe place for a multi-step recipe, because
  a person picks it off a menu rather than a model choosing it. Nothing in a prompt body is
  interpolated except its own arguments (`test_mcp_resources` enforces it).
- **Resources** (`auctions/mcp/resources.py`): `auction://`, `lot://`, `club://` templates,
  `me://context`, `me://activity`, `help://faq` — each names a registered **read-only** action, so
  there's no second permission path. **Nothing that names somebody is ever listed**: `resources/list`
  returns only the widget documents, the two `me://` reads and `help://faq` — the rule is *no slugs*,
  not *nothing concrete*.

## Confirmation tier

`Action.asks_first` is the palette's confirmation card, separate from the read/write split. Three
opt out: `check_in`, `watch_lot`, `review_points`. The bar is confirm-tier and idempotent, not
`destructive` (`test_mcp.ConfirmationTierTests`). `undo_check_in` still asks.

## Housekeeping

- **Adding a URL costs two entries** or the build fails (root `CLAUDE.md`'s rule, applied here:
  `/mcp/` and `oauth2_provider:*` are in `palette_routes.EXCLUDED`; `UserAPIKeyView` is in
  `NOT_A_SKILL`).
- A `NOT_A_SKILL` reason must be about the capability, not the palette; no excused view may be
  reimplemented by a resolver whose own docstring says it's that view's body; an excuse whose whole
  argument is "hard to say out loud" isn't one.
  `test_palette_skills.PageOnlyWriteRegistryTests` fails the build on all three.
- One gap in that guarantee: `palette_actions.postable_views()` requires `hasattr(view, "post")`, so
  `CreateUserIgnoreCategory`/`DeleteUserIgnoreCategory` (which write in `get()`, no URL name) are in
  none of `postable_views()`, `NOT_A_SKILL` or `palette_routes.EXCLUDED` — the only user-facing
  writes in that blind spot.
- `request_a_skill` records what an agent couldn't do; `/admin-dashboard/assistant-requests/` is the
  queue, ordered by distinct askers. Row content is model-written: displayed, escaped, never executed.
- `docs/mcp_next.md` is the standing list of unused spec features, including what's already rejected.

```bash
docker exec -it django python3 manage.py test auctions.test_mcp auctions.test_mcp_widgets auctions.test_mcp_resources auctions.test_mcp_permissions auctions.test_source_code auctions.test_palette_account
curl -s -X POST http://127.0.0.1/mcp/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}'
# expect 401 + WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"
```
