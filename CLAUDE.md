# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Finding your way around

This file is loaded on **every** request, so it holds only what is true everywhere. Everything else
lives next to the thing it describes, and is loaded when you go there:

| Where | What is in it |
|---|---|
| `docs/module_map.md` | **Start here.** One line per module -- its docstring's first sentence and its top-level names. Generated from the code, so it cannot be wrong. |
| A module's own docstring | What that module is for and what is non-obvious about it. Anything over 300 lines has one, enforced. |
| `auctions/views/CLAUDE.md` | The views package: the acyclic-import rule, where the awkward helpers live. |
| `auctions/mcp/CLAUDE.md` | The MCP endpoint, the command palette and the one registry behind both. |
| `auctions/templates/CLAUDE.md` | Templates, styles, and the three navigation surfaces. |
| `.claude/skills/` | Species list, club API, announcements, Celery, voice, the mobile app. Loaded on demand. |
| `docs/` | `mcp_skills.md`, `mcp_next.md`, `club_announcements.md`, `club_event_details.md`, `app_oauth_connect_flows.md`, `style_migration.md` |
| `style_reference.md` | Read before **any** visual change. Palette, the six permitted button classes, message taxonomy. |

Slash commands: `/test`, `/ci`, `/map`, `/migrate`, `/fishbase`, `/mcp`.

**The rule that keeps all of this true: no hand-written prose that a script could derive, and no
prose about code that a test can't check.** `auctions/module_map.py` generates the map and enforces
the docstring and file-size rules; `auctions/template_lint.py` enforces the template-tag rule; the
palette audits in `test_palette_skills.py` enforce that every URL is catalogued. When you are
tempted to write a document, write a docstring or a test instead.

## Stack

Django 5.x auction platform with Python 3.11.9, Bootstrap 5, jQuery, HTMx. MariaDB, Redis, Nginx,
Uvicorn/Gunicorn, Celery, Docker Compose. Main app: `auctions/`.

## Development Setup

```bash
cp .env.example .env && sed -i '1,4d' .env  # Remove first 4 lines (production config)
mkdir -p logs && chmod -R 777 logs
docker compose --profile "*" build          # 5-10 min first time
docker compose up -d
```

Access at `http://127.0.0.1` (port **80**, not 8000).

```bash
docker exec django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
```

## Testing & Linting

```bash
docker compose run --rm test --ci --verbose   # format + lint + template + module map. NO TESTS.
docker compose run --rm test --format         # auto-fix formatting
docker compose run --rm test --lint           # auto-fix linting
docker exec django python3 manage.py test # the tests (needs compose up)
```

Ruff config: `ruff.toml` (line-length 120). Replicate CI with
`./.github/scripts/prepare-ci.sh && docker compose run --rm test --ci --verbose` -- note that
`prepare-ci.sh` **overwrites `.env`**.

- **`--ci` does not run tests.** Run `manage.py test` separately.
- The suite is ~2.5 minutes with `--parallel`, about half of it building the test database from ~290
  migrations. Background it.
- **Never run two at once.** Both share one `test_auctions` database and corrupt each other into
  hundreds of unrelated errors.
- `--parallel` needs **`tblib`** installed or the first failure kills the run with no traceback. In a
  parallel log the **first** `failed:` block is the real failure; anything after
  `Destroying test database for alias 'default'...` is collateral.
- Two things keep the suite fast and are easy to undo by accident: `fishauctions/test_runner.py`
  swaps PBKDF2 for MD5 for the duration of a run, and `StandardTestCase` builds its fixture in
  **`setUpTestData`**. Both are explained in `auctions/tests.py`. A subclass adding per-test setup
  still calls `super().setUp()`; one adding fixture rows extends `setUpTestData`, and if those rows
  save a file it needs `WritableMediaRoot` too.

## Django Commands

Always inside the container. **No `-t`** unless a command genuinely wants a terminal: `docker exec
-t` fails with "the input device is not a TTY" for every caller that is not one -- an agent, a
hook, a script, CI.

```bash
docker exec -i django python3 manage.py makemigrations   # -i: it asks about renames
docker exec django python3 manage.py migrate
docker exec -it django python3 manage.py shell           # -it: a REPL, so only from a terminal
```

Migration permission error? `docker exec -u root django ...`

## Dependencies

Never edit `requirements.txt` directly. Edit `requirements.in` or `requirements-test.in`, then:

```bash
./.github/scripts/update-packages.sh           # add new packages
./.github/scripts/update-packages.sh --upgrade # upgrade all
```

## Architecture

```
auctions/               # the app
  views/                  34 modules -- see auctions/views/CLAUDE.md
  mcp/                    the MCP server -- see auctions/mcp/CLAUDE.md
  mobile/                 the app's own API
  management/commands/    cron jobs: endauctions, sendnotifications, email_invoice, ...
  migrations/             290+
  models.py               80 models in one file, deliberately -- its docstring says why
  palette_actions.py      the one catalogue behind the command palette and /mcp/
fishauctions/           settings (reads .env), ASGI, URLs, Celery config
docker-compose.yaml     web, db, redis, nginx, celery-worker, celery-beat, test
```

**Key models:** User/UserData, Auction, Lot, Bid, Invoice, AuctionTOS, PickupLocation, Category,
Species, ChatMessage, PageView. **URLs:** `auctions/urls.py`, `fishauctions/urls.py`.

## Rules that apply everywhere

- **Adding a URL costs you two entries.** A new named URL or POST view must be catalogued as an
  `Action` in `auctions/palette_actions.py` -- which is what puts it in the command palette *and* on
  `/mcp/`, since both read one registry -- or excused in `NOT_A_SKILL` with a reason about the
  **capability**, not about the palette. `test_palette_skills` fails the build otherwise.
- **A field taken off a model comes off every form in the same commit.** A form naming a dropped
  field raises `FieldError` at import, `urls.py` fails to load, and the container crash-loops behind
  an entrypoint that refuses to serve a half-migrated database -- at which point `makemigrations`
  cannot run. Fix the forms first.
- **Always create migrations after model changes.** Adding a field to `Auction`? Check whether it
  belongs in `AUCTION_FIELDS_TO_CLONE`.
- **Never rebuild a foreign key.** Every FK here is stored under a mangled `table?constraint` name
  MariaDB will not `DROP`, so a migration that recreates one passes against a fresh test database and
  fails against the real one.
- **Template tags open and close on one line.** Django's lexer has no `re.DOTALL`, so a split
  `{# … #}`, `{% … %}` or `{{ … }}` renders onto the page as text with no error anywhere. Use
  `{% comment %}` for anything longer.
- **`.delay()` goes inside `transaction.on_commit`.** A `post_delete` fires *inside* Django's delete
  transaction; enqueuing directly once left a task pointing at an image already gone from Cloudflare.
- **No module over 1500 lines, and anything over 300 says what it is for.** `auctions/module_map.py`
  enforces both. Its `OVERSIZED` list is a ratchet that only shrinks.
- **Never edit vendor CSS or JS.** Site-wide overrides go in `auctions/static/css/auction_site.css`.
  `.ignore` keeps vendored and minified files out of search results; `rg --no-ignore` when you really
  need one.

## Common Issues

| Problem | Fix |
|---|---|
| Won't start | First 4 lines of `.env` not removed |
| Port 80 in use | Add `HTTP_PORT=81` to `.env` |
| Migration permission error | `docker exec -u root django ...` |
| Static files missing | `docker exec django python3 manage.py collectstatic --no-input` |
| DB out of sync | `docker exec django python3 manage.py migrate` |
| `IntegrityError (1364, "Field 'x' doesn't have a default value")` | A `NOT NULL` column left behind by an abandoned branch. In no model and no migration, so every insert 500s. `migrate` -- `0418_drop_orphan_columns` drops any such column and leaves inert ones alone. Test databases are built from migrations, so the suite can never catch this. |
| Build fails | `docker compose down && docker system prune -a -f && docker compose --profile "*" build --no-cache` |
