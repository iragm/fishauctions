There's a lot of excessively long text scattered throughout this project.  Docstrings, md files, comments, even user facing stuff.

This is unacceptable design - they burn tokens when claude works on this project, they drift from code, hallucinations become law, and users can't wade through massive amounts of text.

If it's not 100% essential, remove it.

If it is 100% essential, say it with fewer words.

## Rules for cutting

- **Comments and docstrings only.** Never a string literal: `help_text`, `Action(description=...)`
  in `palette_actions.py`, MCP tool descriptions and assertion messages reach a user or the model.
- A module over 300 lines keeps a docstring (`auctions/module_map.py` enforces it) and its **first
  line** is published into `docs/module_map.md`. Regenerate with `python3 auctions/module_map.py
  --write` **on the host** after a pass (the django container can't write `docs/`).
- `test_palette_skills.py` greps resolver docstrings for ``ViewName`` + "'s own body". That phrasing
  survives verbatim.
- `.env.example` keeps its comments: env vars are documented there on purpose.
- A `.py` anywhere but `swag/ .git/ .github/ logs/ mediafiles/ staticfiles/` enters the module map
  and the linter. That is why `docs/ses_inbound_router.py.txt` has that extension.
- After any batch of edits: compile-check every file, `--format`, `--lint`, regenerate the map,
  then `manage.py test --parallel`. Never two suites at once.

## Still to do

### Python

Prose = comment + docstring lines, measured 2026-09-15 after the passes below. Total 26,681
(17,013 in code, 9,668 in tests).

| Prose | % | File |
|---|---|---|
| 1405 | 11% | `auctions/models.py` -- one pass done; the rest is mostly short and load-bearing |
| 1164 | 9% | `auctions/palette_actions.py` -- one pass done |
| 303 | 18% | `auctions/mobile/views.py` |
| 222 | 35% | `auctions/mobile/serializers.py` |
| 202 | 13% | `auctions/tables.py` |
| 185 | 37% | `auctions/club_health.py` |
| 181 | 31% | `auctions/views/palette.py` |
| 181 | 24% | `auctions/views/embeds.py` |
| 180 | 40% | `auctions/mobile/services/social_auth.py` |
| 177 | 14% | `auctions/views/auction_stats.py` |
| 168 | 22% | `auctions/printer_programs.py` |
| 167 | 13% | `auctions/views/club_integrations.py` |
| 165 | 19% | `auctions/views/ajax.py` |
| 161 | 17% | `auctions/views/bulk_add.py` |
| 154 | 21% | `auctions/mailchimp.py` |
| 150 | 33% | `auctions/llm.py` |
| 147 | 29% | `auctions/species_categories.py` |
| 143 | 24% | `auctions/views/printing.py` |
| 140 | 22% | `auctions/brevo.py` |
| 139 | 41% | `auctions/mcp/auth.py` |
| 135 | 37% | `auctions/usability_report.py` |
| 126 | 34% | `auctions/views/usability.py` |
| 123 | 34% | `auctions/context_processors.py` |
| 116 | 29% | `auctions/mcp/resources.py` |
| 115 | 32% | `auctions/notifications.py` |
| 114 | 30% | `auctions/apple_wallet.py` |

Also untouched: the rest of `auctions/views/`, `auctions/management/commands/`,
`auctions/static/js/*.js` (excluding vendor), `auctions/urls.py`, and the infra scripts
(`entrypoint.sh`, `update.sh`, `docker-compose.yaml`, `.github/workflows/`).

Tests, all untouched this round: `test_species.py` (601), `test_palette_assist.py` (441),
`tests_selenium.py` (322), `test_mobile_features.py` (294), `test_stats.py` (277),
`test_tap_to_pay.py` (244), `test_ar.py` (219), `test_palette_skills.py` (213), and ~60 more.

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

- `help_text=` -- 533 across `models.py`, `forms.py` and `mobile/serializers.py`; 103 over 120
  characters, 38 over 200. About a third of the long ones are on forms a user sees; the rest are
  Django-admin only.
- `Action(description=...)` in `palette_actions.py` and the MCP tool descriptions -- read by the
  model, so shortening changes behaviour.
- Admin setup checklist: wordy, but no user sees it. Leave.

## Done

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
| various tests | | ~600 lines off, partial |

Second pass, 2026-09-15 (suite green, `--ci` passes, map regenerated):

| File | Was | Now |
|---|---|---|
| `docs/mcp_skills.md` | 350 | 76 |
| `docs/club_announcements.md` | 253 | deleted (folded into the club-announcements skill) |
| `docs/club_event_details.md` | 135 | deleted (folded into the club-announcements skill) |
| `docs/app_oauth_connect_flows.md` | 192 | 51 |
| `docs/mcp_next.md` | 167 | 27 |
| `docs/app_printing_contract.md` | 138 | 56 |
| `.claude/skills/*/SKILL.md` (six) | 475 | 247 |
| `auctions/templates/CLAUDE.md` | 75 | 30 |
| `auctions/views/CLAUDE.md` | 41 | 16 |
| `auctions/palette_actions.py` | 15451 | 13167 |
| `auctions/models.py` | 14907 | 13304 |
| `auctions/forms.py` | 6865 | 6117 |
| `auctions/species_matching.py` | 1608 | 1035 |
| `auctions/tasks.py` | 2146 | 1730 |
| `auctions/palette_assist.py` | 1518 | 1147 |
| `auctions/services.py` | 1433 | 1152 |
| `fishauctions/settings.py` | 1388 | 1117 |
| `auctions/views/club_api.py` | 1343 | 1135 |
| plus `ar_mapping`, `signals`, `lifecycle`, `donations`, `mobile/services/payments`, `mcp/tools`, `filters`, `views/{lot_pages,species,selling,payments,webhooks,base}`, `apple_notifications`, `google_calendar`, `command_palette`, `admin`, `palette_routes`, `serializers` | | |

Judgement calls made while cutting:

- `SES.md` lost the 401-line Lambda handler to "it's in git history". That source is the only copy
  of live infrastructure, so it is back as `docs/ses_inbound_router.py.txt`.
- `style_reference.md` had two paths that were already wrong (`partials/htmx_table_filters.html` is
  really under `auctions/partials/`). Fixed rather than carried forward.
- `USABILITY.md`'s pass log is gone -- that is what `git log` is for. Every decision marked "do not
  reopen" was kept, because those exist to stop somebody re-proposing them.
- Decision records ("deliberately not done, and why") were kept in short form everywhere; the
  narrative of how each was discovered was cut.
- Commented-out code blocks in `models.py` and `forms.py` were deleted outright.
- `plain_text`'s docstring in `palette_actions.py` carried a stray line ("Human: ah, to be an llm
  and write that last line") -- removed.

Process note: nine parallel agents were killed mid-edit by a rate limit in the first pass and left
12 test files with unterminated docstrings. The second pass ran inline with a dump/replace helper
that verifies each block's hash before writing and parses the result; nothing broke.
