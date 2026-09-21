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
`go_to_page` reaches its page. **`palette_actions.MCP_ONLY_SKILLS` is the list and the argument for
each entry**, in one table rather than a flag per registration, because it is one editorial decision
about one surface and reading it has to be possible in one sitting. A reason there is about the
client, never about the capability: **who reads the answer** (`read_source`/`club_api` return pages
of text, wrong for a one-line box paid for out of this site's own model budget), and **who does the
acting** — the palette keeps a write only when you can say it in one sentence, you say it with your
hands full, and you say it more than once in a while, and not even then if the page shows you
something you have to see before deciding. Sixteen writes survive that: the auction floor, the
checkout table and the door. `test_palette_assist.DriftTests` pins those sixteen — the list that gets
quietly shorter — rather than the fifty-odd that don't. What each skill goes through is catalogued in
`docs/mcp_skills.md`.

`add_lot`/`add_lots` are the one pair where both surfaces have the skill under different names: the
palette's is `add_a_lot`, navigate-only, which opens the lot form with what was said already in it.
No caller is offered both, and the name matters — the same tool called `sell_a_lot` lost every
"add lots to my next auction" to `add_person`, which has the word add in its name.

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

`palette_assist.tools_for` is `mcp.tools.tool_descriptors(user)` plus three tools of its own —
`ask_the_user` and `cannot_do_this` — there is no tool that takes a sentence. `llm.complete` sends them as OpenAI function
definitions (`llm.as_openai_tool`). `read_reply` maps "which tool" to
lookup/action/question/answer/refusal. `complete_json` stays for the four callers that want data,
not a call (species matching, donations, the two speaker commands).

**Every turn ends in something the user can touch**: a link, a countdown card, a question with
clickable options, or a read's own summary with the things it names linked underneath. A paragraph is
not one of those — in a one-line box, "would you like A or B?" as prose is a dead end, especially by
voice. So `tool_choice="required"`, and **the model has no tool that takes a sentence**: an answer is
the resolver's own `summary`, and the model's contribution is choosing which read holds it. It once
said "I've updated the email on your account" about a write that never ran. `read_reply` still
rescues a question written as prose into a clarify card, and `echoes_the_query` refuses a call that
put the whole sentence into one of its own fields.

`answers_on_its_own` ends the turn on the read itself rather than asking again. Measured: given a
`describe_auction` result that plainly answered the question, the model called `describe_auction` a
second time — it has the answer and no way to say so. A read that produced a summary has answered,
unless it is one of `STEP_LOOKUPS` (the `find_*` pair, `find_page`, `my_context`, which exist to feed
another tool) or the query names something to do.

`asks_for_something_removed` takes the writes away for a turn that names a skill the palette gave up:
told to refund a lot with no tool for it, the model reached for `no_sale`, and "give bob 10 points"
became a $10 charge on his invoice. The vocabulary is built from `MCP_ONLY_SKILLS` minus every word
the surviving writes are named by — **nothing is listed in the prompt**, because what the box can't do
is an endless list and it would cost tokens every round.

`navigation_shortcut` answers "take me to my invoices" from the route catalog with no model call at
all, when one route is clearly ahead of the next. `navigate_only` — a preference, and
`ASSISTANT_NAVIGATE_ONLY` for everybody — keeps the writes off permanently; the site-wide one also
refuses a countdown card that was already on screen.

The links come from `palette_actions.KEY_ABOUT`, the same block `/mcp/` turns into `resource_link`s:
`assist_stream` collects it off every lookup and `about_groups` turns it into rows. It used to be
stripped and thrown away, so "the next auction is on the 19th" arrived with no way to open it.

`tools_for(user, query)` drops the writes for a question. The writes come **last**, so the short list
is a byte-exact prefix of the long one and both share one cached prompt. For the same reason nothing
user-specific is in the system prompt: the facts about the user ride in the first message
(`context_message`), leaving a prompt that is identical for everyone in a permission tier, and a
cache that stays warm between people instead of going cold between one person's sessions. The
`asks_for_something_removed` tier is the exception: it is a *subset* of the reads rather than a
shorter suffix, so it shares no cached prefix and costs full price. Watch the cached percentage on
the analytics page if those turns stop being rare.

**A question is settled before any word bag is consulted.** `asks_a_question` — a question mark, a
question word, or a yes/no opener — runs first in `wants_the_writes` and guards `asks_for_a_write` in
`answers_on_its_own`. It used to run second, and the writes are named after the things people ask
about: ten of fifteen plainly-phrased questions contain a write word, so "what time is check in?" was
handed `check_in` *and* could not be answered by the read that answered it. Both vocabularies also
grow through `_SYNONYMS`, because they are built out of the registry's wording and nobody speaks the
registry's wording — `remove_person` says "somebody" and `add_person` says "someone", which took every
write away from "add somebody to the auction". `SurvivingWritesStayReachableTests` pins that the
sixteen are still reachable by the words people use; `DriftTests` only pins that they exist.

Palette-only, not in the MCP layer: `obvious_match`/`shortcut_match`, the confirm countdown and its
trust window, `humanize`, the `_give_up` fallback ladder, `sanitize_context`/`_carry_over` memory,
throttles, cancel/report analytics.

## When it gets busy

The per-user limits (`check_request_budget`, counted per command; `check_call_budget` as a backstop
on rounds) do nothing about ten people each inside their own. The ceiling that binds first is the
provider's: about 8.6k tokens a call against a 200k-per-minute account is roughly 23 calls a minute
for the whole site, and reaching it answers every user at once with an error.

So everybody gets slower before anybody gets refused, and the waiting is on screen. `site_load()` is
what the **last** minute cost against `LLM_TOKENS_PER_MINUTE` — two buckets, the older weighted by
how much of it is still in the window, because one bucket dropping to zero on the minute released the
whole held-back queue at once every sixty seconds. `reserve_tokens`/`settle_tokens` charge
`ESTIMATED_CALL_TOKENS` **before** the call and correct it after: spending only on the way back left
a call invisible while in flight, so ten people typing at once all read a load of zero and all went.
Past `BUSY_THRESHOLD` `wait_for_the_queue` holds each request a little longer the busier it is, to
`MAX_WAIT_SECONDS` — after which ordinary search is the better answer and they can have it now.

**The wait is the caller's to do.** `assist_stream` yields it as `wait_seconds` on a progress event
and `CommandPaletteAssistView` awaits it; sleeping in the generator pinned that request's
thread-sensitive executor thread *and* its database connection (`CONN_MAX_AGE` is 0, so a stream holds
one to the end) for the whole wait, at exactly the load where connections are what runs out.
`_let_go_of_the_database` drops it first. `assist()` sleeps for itself, so `?stream=false` can't walk
past the queue.

A `429` is `llm.RateLimited`, which waits out the provider's own `Retry-After` and tries once more,
because it means "in a moment", not "no" — but only if `TOTAL_BUDGET_SECONDS` has room for the wait
*and* the call it pays for. That budget is checked with `llm.DEFAULT_TIMEOUT_SECONDS` of headroom
rather than merely "not yet spent", which had let one round run to 36 seconds before anything looked
at the clock. `BREAKER_FAILURES` consecutive failures rest the model for a minute, timed from the
moment it trips: `cache.add` sets a timeout only on creation, so the rest used to start at failure
one. Per user, `WINDOW_MAX_REQUESTS` is set where a working admin won't meet it, and going over hands
back search results rather than a refusal.

## Watching it work

`/admin-dashboard/palette-analytics/`. `LLMUsage.request_id` is one id per thing somebody typed, so
a lookup and the answer it fed are one story — rounds-per-request was counted over the *text* of the
query before, which made two people asking the same thing one query. `variant` fingerprints the
prompt, the skill list and the model together, so a deploy that changes any of them starts a new row
and a before and an after can sit next to each other without anybody remembering the date. With a
handful of users a rate is one person's afternoon, so the page prints every exchange in order.

Three columns exist because the page could say a command went wrong and never say what it went wrong
*on*: **`subject`** is the auction, club or lot it landed on — the same line its confirmation card
showed, so "it answered about the wrong auction" is checkable; **`read_the_query`** marks the ones
where the server read that auction out of the sentence because the model left the parameter out, a
guess made on somebody's behalf; **`tools_offered`** names the tier (`all`/`reads`/`pages`/`locked`),
so a turn that quietly lost its write tools isn't just another navigation. The page also shows
whether the breaker is open right now, which was previously visible only as slow answers.

`palette_assist.shortcut_proposals` offers phrases the assistant has answered the same way every
single time, one button each. The mining was always there and nothing ever ran it. An accepted
phrase stops reaching the model at all: no call, no wait, and no way for it to come back wrong.

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
- **OIDC is one switch, `OIDC_RSA_KEYFILE`** (`auctions/mcp/oidc.py`). `/mcp/` never needs identity;
  this exists so a plugin directory can read a *verified* email address and keep a work account out
  of a personal workspace. No key means the `openid`/`email` scopes are not advertised at all —
  a scope in `SCOPES` is a promise in three documents, including the protected-resource one. With a
  key, every `Application` is given RS256 on the way into the database, because DCR and CIMD both
  leave `algorithm` blank and the first `openid` request would be signed with nothing.
- `/.well-known/openai-apps-challenge` (`auctions/mcp/verification.py`) serves
  `OPENAI_APPS_CHALLENGE_TOKEN` and nothing else — one token, no JSON, no redirect, no sign-in.
  Blank 404s: two hosts answering with different tokens is how a shared host name fails to verify.

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
- **`_named_or_resolved` is the one place the sentence is read**, so the action, the label on its
  confirmation card (`action_context`) and the run after that card all decide the same way.
  `auction_named_in` is whole words only and needs the title said straight through, every
  distinguishing word of it said somewhere, or — for a one-word title — that word said beside an
  auction noun; it is scoped to `command_palette._own_auctions`, since `_joined_auctions` hands a
  superuser every club's auction. A loosened hint is never matched *loosely* when what's left is
  generic (`_GENERIC_HINTS`): stripping the article off "the auction" left a word every title
  contains, and `title__icontains` then returned whichever came first.
- **`pin_the_subject` writes the resolved auction into a countdown card's own params.** The card is
  built in one request and confirmed in another, and `execute` never sees the sentence; without it a
  card naming one auction ran against another.
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
  empty — no widget calls a tool. `openai/outputTemplate` rides beside `ui/resourceUri` and is the
  **only** thing duplicated for ChatGPT: it documents that key as a compatibility alias, the mime
  type and CSP are already the shared spelling, and a widget that silently doesn't draw is worth
  more than one key on nine tools.
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
- Two gaps in that guarantee. `palette_actions.postable_views()` requires `hasattr(view, "post")`, so
  `CreateUserIgnoreCategory`/`DeleteUserIgnoreCategory` (which write in `get()`, no URL name) are in
  none of `postable_views()`, `NOT_A_SKILL` or `palette_routes.EXCLUDED`. And it reads only
  `palette_actions.AUDITED_VIEW_MODULES` — `auctions.views` and `auctions.donation_views`; the latter
  was added when the donation skills arrived, having held five user-facing writes in none of the three
  tables. `app_links`, `apple_notifications` and `passkit_views` are still outside it.
- `request_a_skill` records what an agent couldn't do; `/admin-dashboard/assistant-requests/` is the
  queue, ordered by distinct askers. Row content is model-written: displayed, escaped, never executed.
- `docs/mcp_next.md` is the standing list of unused spec features, including what's already rejected.

```bash
docker exec -it django python3 manage.py test auctions.test_mcp auctions.test_mcp_widgets auctions.test_mcp_resources auctions.test_mcp_permissions auctions.test_source_code auctions.test_palette_account
curl -s -X POST http://127.0.0.1/mcp/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}'
# expect 401 + WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"
```
