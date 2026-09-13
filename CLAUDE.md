# CLAUDE.md

Django 5.x auction platform. Python 3.11.9, Bootstrap 5, jQuery, HTMx, MariaDB, Redis, Nginx,
Uvicorn/Gunicorn, Celery, Docker Compose. Main app: `auctions/`.

## Where things are

This file is loaded on **every** request, so it holds only what is true everywhere.

| Where | What is in it |
|---|---|
| `docs/module_map.md` | **Start here.** One line per module, generated from the code. |
| A module's own docstring | What that module is for. Anything over 300 lines has one, enforced. |
| `auctions/views/CLAUDE.md` | The views package and its acyclic-import rule. |
| `auctions/mcp/CLAUDE.md` | The MCP endpoint, the palette, and the one registry behind both. |
| `auctions/templates/CLAUDE.md` | Templates, styles, navigation surfaces. |
| `.claude/skills/` | Species list, club API, announcements, Celery, voice, mobile app. |
| `style_reference.md` | Read before **any** visual change. |

Slash commands: `/test`, `/ci`, `/map`, `/migrate`, `/fishbase`, `/mcp`.

**No hand-written prose that a script could derive, and no prose about code that a test can't
check.** Write a docstring or a test instead of a document.

```
auctions/
  views/                34 modules        mcp/          the MCP server
  mobile/               the app's API     migrations/   290+
  management/commands/  cron jobs         models.py     80 models in one file, deliberately
  palette_actions.py    the one catalogue behind the command palette and /mcp/
fishauctions/           settings (reads .env), ASGI, URLs, Celery
```

**Key models:** User/UserData, Auction, Lot, Bid, Invoice, AuctionTOS, PickupLocation, Category,
Species, ChatMessage, PageView. **URLs:** `auctions/urls.py`, `fishauctions/urls.py`.

## Setup

```bash
cp .env.example .env && sed -i '1,4d' .env  # first 4 lines are production config
mkdir -p logs && chmod -R 777 logs
docker compose --profile "*" build          # 5-10 min first time
docker compose up -d
```

Site is on port **80**, not 8000. Superuser:

```bash
docker exec django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
```

## Testing and linting

```bash
docker compose run --rm test --ci --verbose   # format + lint + template + module map. NO TESTS.
docker compose run --rm test --format         # auto-fix formatting
docker compose run --rm test --lint           # auto-fix linting
docker exec django python3 manage.py test     # the tests (needs compose up)
```

- **`--ci` runs no tests.** Run `manage.py test` separately, and background it: ~2.5 min with
  `--parallel`, half of that building the test DB from ~290 migrations.
- **Never run two suites at once.** They share one `test_auctions` database and corrupt each other
  into hundreds of unrelated errors.
- `--parallel` needs **`tblib`**, or the first failure kills the run with no traceback. In a parallel
  log the **first** `failed:` block is the real one; anything after `Destroying test database` is
  collateral.
- Two things keep the suite fast and are easy to undo: `fishauctions/test_runner.py` swaps PBKDF2
  for MD5, and `StandardTestCase` builds its fixture in `setUpTestData`. A subclass adding per-test
  setup still calls `super().setUp()`; one adding fixture rows extends `setUpTestData`, and needs
  `WritableMediaRoot` if those rows save a file.
- Replicate CI with `./.github/scripts/prepare-ci.sh && docker compose run --rm test --ci --verbose`
  -- `prepare-ci.sh` **overwrites `.env`**.

## Commands and dependencies

Always inside the container, and **no `-t`** unless the command wants a terminal (`docker exec -t`
fails with "the input device is not a TTY" for an agent, a hook, a script or CI).

```bash
docker exec -i django python3 manage.py makemigrations   # -i: it asks about renames
docker exec django python3 manage.py migrate
docker exec -it django python3 manage.py shell           # -it: a REPL, so only from a terminal
```

Never edit `requirements.txt`. Edit `requirements.in` / `requirements-test.in`, then
`./.github/scripts/update-packages.sh` (`--upgrade` to upgrade all).

## Rules that apply everywhere

- **Adding a URL costs two entries.** A new named URL or POST view is an `Action` in
  `auctions/palette_actions.py` -- which is what puts it in the palette *and* on `/mcp/` -- or is
  excused in `NOT_A_SKILL` with a reason about the capability. `test_palette_skills` fails otherwise.
- **A field taken off a model comes off every form in the same commit.** Otherwise `urls.py` fails
  to import and the container crash-loops behind an entrypoint that refuses a half-migrated
  database, at which point `makemigrations` cannot run.
- **Always migrate after model changes.** A new `Auction` field may belong in
  `AUCTION_FIELDS_TO_CLONE`.
- **Never rebuild a foreign key.** Every FK is stored under a mangled `table?constraint` name
  MariaDB will not `DROP`, so the migration passes on a fresh test DB and fails on the real one.
- **Template tags open and close on one line.** Django's lexer has no `re.DOTALL`, so a split
  `{# #}`, `{% %}` or `{{ }}` renders onto the page as text. Use `{% comment %}` for anything longer.
- **`.delay()` goes inside `transaction.on_commit`.** A `post_delete` fires inside Django's delete
  transaction; enqueuing directly once left a task pointing at an image already gone from Cloudflare.
- **Anything over 300 lines says what it is for** in a module docstring. **There is no line limit.**
- **Never edit vendor CSS or JS.** Site-wide overrides go in `auctions/static/css/auction_site.css`.
  `.ignore` hides vendored files from search; `rg --no-ignore` when you need one.

## Common issues

| Problem | Fix |
|---|---|
| Won't start | First 4 lines of `.env` not removed |
| Port 80 in use | `HTTP_PORT=81` in `.env` |
| Migration permission error | `docker exec -u root django ...` |
| Static files missing | `manage.py collectstatic --no-input` |
| DB out of sync | `manage.py migrate` |
| `IntegrityError (1364, "Field 'x' doesn't have a default value")` | A `NOT NULL` column left by an abandoned branch: in no model and no migration, so every insert 500s. `migrate` -- `0418_drop_orphan_columns` handles it. Test DBs are built from migrations, so the suite can never catch this. |
| Build fails | `docker compose down && docker system prune -a -f && docker compose --profile "*" build --no-cache` |
