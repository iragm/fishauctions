There's a lot of excessively long text scattered throughout this project.  Docstrings, md files, comments, even user facing stuff.

This is unacceptable design - they burn tokens when claude works on this project, they drift from code, hallucinations become law, and users can't wade through massive amounts of text.

If it's not 100% essential, remove it.

If it is 100% essential, say it with fewer words.

## Rules for cutting

- **Comments and docstrings only.** Never a string literal: `help_text`, `Action(description=...)`
  in `palette_actions.py`, MCP tool descriptions and assertion messages reach a user or the model.
- A module over 300 lines keeps a docstring (`auctions/module_map.py` enforces it) and its **first
  line** is published into `docs/module_map.md`. Regenerate with `python3 auctions/module_map.py
  --write` after a pass -- reformatting changes the line counts the map records.
- `test_palette_skills.py` greps resolver docstrings for ``ViewName`` + "'s own body". That phrasing
  survives verbatim.
- `.env.example` keeps its comments: env vars are documented there on purpose.
- A `.py` anywhere but `swag/ .git/ .github/ logs/ mediafiles/ staticfiles/` enters the module map
  and the linter. That is why `docs/ses_inbound_router.py.txt` has that extension.
- After any batch of edits: compile-check every file, `--format`, regenerate the map, `--ci`, then
  `manage.py test`. Never two suites at once.

## Files to work through

### Markdown (not user facing -- cut freely)

| Lines | File | Why it is on the list |
|---|---|---|
| 350 | `docs/mcp_skills.md` | Catalogue written as prose; most of it is derivable |
| 253 | `docs/club_announcements.md` | Overlaps `.claude/skills/club-announcements/SKILL.md` |
| 192 | `docs/app_oauth_connect_flows.md` | |
| 192 | `.claude/skills/species-list/SKILL.md` | |
| 167 | `docs/mcp_next.md` | Ideas list; no code depends on it |
| 138 | `docs/app_printing_contract.md` | |
| 135 | `docs/club_event_details.md` | |
| 77 | `.claude/skills/club-announcements/SKILL.md` | |
| 75 | `auctions/templates/CLAUDE.md` | |
| 70 | `.claude/skills/club-api/SKILL.md` | |
| 65 | `.claude/skills/celery-tasks/SKILL.md` | |
| 42 | `.claude/skills/mobile-app/SKILL.md` | |
| 41 | `auctions/views/CLAUDE.md` | |
| 29 | `.claude/skills/voice-set-winners/SKILL.md` | |

`docs/module_map.md` is generated -- it shrinks on its own. `readme.md` is done.

### Python -- untouched so far

Prose = comment + docstring lines, measured before any of this started.

| Prose | % | File |
|---|---|---|
| 3449 | 22% | `auctions/palette_actions.py` -- the single biggest target left |
| 1155 | 17% | `auctions/forms.py` |
| 862 | 54% | `auctions/species_matching.py` |
| 616 | 29% | `auctions/tasks.py` |
| 567 | 37% | `auctions/palette_assist.py` |
| 473 | 33% | `auctions/services.py` |
| 445 | 32% | `fishauctions/settings.py` |
| 344 | 26% | `auctions/views/club_api.py` |
| 322 | 25% | `auctions/tests_selenium.py` |
| 316 | 42% | `auctions/mobile/services/payments.py` |
| 279 | 26% | `auctions/ar_mapping.py` |
| 277 | 20% | `auctions/test_stats.py` |
| 276 | 21% | `auctions/test_tap_to_pay.py` |
| 275 | 15% | `auctions/filters.py` |
| 273 | 24% | `auctions/signals.py` |
| 270 | 18% | `auctions/views/lot_pages.py` |
| 251 | 47% | `auctions/mcp/tools.py` |
| 251 | 35% | `auctions/lifecycle.py` |
| 246 | 26% | `auctions/donations.py` |
| 231 | 13% | `auctions/test_ar.py` |
| 222 | 19% | `auctions/test_celery_tasks.py` |
| 217 | 14% | `auctions/command_palette.py` |
| 216 | 34% | `auctions/views/species.py` |
| 213 | 20% | `auctions/views/selling.py` |
| 210 | 16% | `auctions/test_auction_props.py` |
| 209 | 18% | `auctions/views/payments.py` |
| 204 | 40% | `auctions/apple_notifications.py` |
| 204 | 26% | `auctions/google_calendar.py` |
| 203 | 7% | `auctions/test_club_events.py` |
| 202 | 16% | `auctions/test_bulk_add_lots.py` |
| 198 | 16% | `auctions/test_models_misc.py` |
| 197 | 12% | `auctions/admin.py` |
| 195 | 10% | `auctions/palette_routes.py` |
| 193 | 20% | `auctions/serializers.py` |
| 191 | 19% | `auctions/views/webhooks.py` |
| 186 | 17% | `auctions/views/base.py` |

Also untouched: the rest of `auctions/views/`, `auctions/management/commands/` (2 of ~20 done),
`auctions/static/js/*.js` (excluding vendor), `auctions/tables.py`, `auctions/urls.py`,
`auctions/club_health.py`, `auctions/species_categories.py`, and the infra scripts
(`entrypoint.sh`, `update.sh`, `docker-compose.yaml`, `.github/workflows/`). `auctions/models.py`
had 3279 prose lines and only 280 came off -- it is barely started.

### User facing -- Ira reviews every change, do not edit unasked

Templates by visible word count (90 templates carry over 120 words):

| Words | File |
|---|---|
| 2209 | `auctions/templates/view_lot_images.html` |
| 1626 | `auctions/templates/auctions/_club_api_endpoints.html` |
| 1357 | `auctions/templates/auction_stats.html` |
| 1145 | `auctions/templates/base.html` |
| 874 | `auctions/templates/species_gaps.html` |
| 798 | `auctions/templates/auctions/club_donation_settings.html` |
| 797 | `auctions/templates/auction_ribbon.html` |
| 740 | `auctions/templates/auctions/square_seller.html` |
| 723 | `auctions/templates/invoice.html` |
| 715 | `auctions/templates/auction.html` |
| 695 | `auctions/templates/dmca.html` (legal -- probably leave) |
| 685 | `auctions/templates/user_api_keys.html` |
| 659 | `auctions/templates/auctions/club_google_calendar_settings.html` |
| 609 | `auctions/templates/auctions/quick_checkout_htmx.html` |
| 569 | `auctions/templates/dashboard_usability.html` |
| 566 | `auctions/templates/auctions/embeds/base.html` |
| 561 | `auctions/templates/auctions/partials/bap_lot_api_fields.html` |
| 555 | `auctions/templates/account_delete.html` |
| 521 | `auctions/templates/auctions/_club_events_list.html` |
| 514 | `auctions/templates/auctions/club_brevo_settings.html` |
| 508 | `auctions/templates/dashboard_club_health.html` |
| 484 | `auctions/templates/auctions/paypal_seller.html` |
| 473 | `auctions/templates/dashboard_lifecycle.html` |
| 427 | `auctions/templates/command_palette_analytics.html` |
| 427 | `auctions/templates/auctions/dynamic_set_lot_winner.html` |
| 399 | `auctions/templates/auctions/club_email_settings.html` |
| 386 | `auctions/templates/promo.html` |

Strings in Python that reach a user or the model:

- `help_text=` -- 436 in `auctions/models.py`, 154 in `auctions/forms.py`, 10 in
  `auctions/mobile/serializers.py`. Many run past 200 characters.
- `Action(description=...)` in `palette_actions.py` and the MCP tool descriptions -- read by the
  model, so shortening changes behaviour.
- Admin setup checklist: wordy, but no user sees it. Leave.

## Done

5,645 lines removed. Suite green (6060 tests), `--ci` passes, module map regenerated.

| File | Was | Now |
|---|---|---|
| `USABILITY.md` | 968 | 119 |
| `SES.md` | 701 | 199 |
| `style_reference.md` | 562 | 171 |
| `docs/phase_9.md` | 348 | 66 |
| `auctions/mobile/views.py` | 2579 | 1671 |
| `VENDOR_LIBRARIES.md` | 185 | 29 |
| `CELERY.md` | 146 | 42 |
| `auctions/mcp/CLAUDE.md` | 278 | 190 |
| `CLAUDE.md` | 155 | 119 |
| `docs/style_migration.md` | 47 | 16 |
| `docs/testing-with-mariadb.md` | 63 | 35 |
| `.github/copilot-instructions.md` | 122 | deleted (stale copy of CLAUDE.md) |
| `AR.md` | 0 | deleted (empty) |
| `auctions/mcp/*.py` | | most trimmed, 300+ lines off |
| `auctions/models.py` | 15173 | 14893 (barely started) |
| various tests | | ~600 lines off, partial |

Judgement calls made while cutting:

- `SES.md` lost the 401-line Lambda handler to "it's in git history". That source is the only copy
  of live infrastructure, so it is back as `docs/ses_inbound_router.py.txt`.
- `style_reference.md` had two paths that were already wrong (`partials/htmx_table_filters.html` is
  really under `auctions/partials/`). Fixed rather than carried forward.
- `USABILITY.md`'s pass log is gone -- that is what `git log` is for. Every decision marked "do not
  reopen" was kept, because those exist to stop somebody re-proposing them.

Process note: nine parallel agents were killed mid-edit by a rate limit and left 12 test files with
unterminated docstrings. They were reverted to HEAD, which is why several tests are back on the
list. Compile-check before trusting any batch.
