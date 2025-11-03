# Fish Auctions - Copilot Instructions

## Overview
Django 5.x auction platform with Python 3.11.9, Bootstrap, jQuery, HTMx. Stack: MariaDB, Redis, Nginx, Uvicorn/Gunicorn, Docker Compose. ~22MB repo, ~21k lines Python, 100+ templates. Main app: `auctions/` (~5k lines models.py, ~8k lines views.py, ~3k lines forms.py).

## Setup (Development)
**CRITICAL - Follow in order:**
```bash
cp .env.example .env && sed -i '1,4d' .env  # Remove first 4 lines (production config)
mkdir -p logs && chmod -R 777 logs
docker compose --profile "*" build          # 5-10 min first time
docker compose up -d
```
Access at `http://127.0.0.1` (port 80, NOT 8000). Create superuser: `docker exec -it django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"`

## Testing & Linting
**Before committing:** `docker compose run --rm test --ci --verbose` (2-3 min)
- Format: `docker compose run --rm test --format` (auto-fix) / `--format-check` (CI)
- Lint: `docker compose run --rm test --lint` (auto-fix) / `--lint-check` (CI)
- Tests: `docker exec -it django python3 manage.py test` (requires `docker compose up -d`)
- Ruff config: `ruff.toml` (line-length: 120). Pre-commit optional: `.pre-commit-config.yaml`

## Django Commands
**ALWAYS run inside container:** `docker exec -it django python3 manage.py <cmd>`
Common: `makemigrations`, `migrate`, `collectstatic`, `shell`, `createsuperuser`
**Migration permission issue?** Use: `docker exec -u root -it django python3 manage.py makemigrations`

## Dependencies
**NEVER edit `requirements.txt` directly.** Edit `requirements.in` or `requirements-test.in`, then run:
`./.github/scripts/update-packages.sh` (add new) / `--upgrade` (upgrade all)

## Architecture
```
auctions/           # Main app: models, views, forms, templates, static, migrations (180+), management/commands
fishauctions/       # Settings (reads .env), ASGI/WSGI, URLs
.github/workflows/  # CI: test.yml (prepare-ci.sh + test --ci), pre-commit validation
docker-compose.yaml # Services: web (Django), db (MariaDB), redis, nginx, test
Dockerfile          # Multi-stage: builder, test, dev, final
entrypoint.sh       # Auto-runs migrate, collectstatic; starts uvicorn (dev) or gunicorn (prod)
ruff.toml           # Linting/format config
.env                # Config (NEVER commit)
.gitignore          # Excludes: *.pyc, .env, db.sqlite3, mediafiles/, staticfiles/, logs/
```

**Key models:** User/UserData, Auction, Lot, Bid, Invoice, AuctionTOS, PickupLocation, Category, ChatMessage, PageView
**Config files:** `ruff.toml`, `.pre-commit-config.yaml`, `docker-compose.yaml`, `Dockerfile`, `.env`
**.env critical vars:** DEBUG (unset for dev), DATABASE_*, SITE_DOMAIN, REDIS_PASSWORD, email (SMTP/SES), API keys

## CI/CD
**GitHub Actions** (`.github/workflows/test.yml`): Runs on push/PR to master
1. `.github/scripts/prepare-ci.sh` (creates .env, logs)
2. `docker compose run --rm test --ci --verbose` (format + lint checks)
3. Pre-commit validation
**Replicate locally:** `./.github/scripts/prepare-ci.sh && docker compose run --rm test --ci --verbose`

## Common Issues
1. **Won't start**: First 4 lines of `.env` not removed (production config blocks dev)
2. **Migration permission error**: `docker exec -u root -it django python3 manage.py makemigrations`
3. **Port 80 in use**: Add `HTTP_PORT=81` to `.env`, access at `http://127.0.0.1:81`
4. **Static files missing**: `docker exec -it django python3 manage.py collectstatic --no-input`
5. **DB out of sync**: `docker exec -it django python3 manage.py migrate`
6. **Test failures**: Ensure `docker compose up -d` first
7. **Build fails**: `docker compose down && docker system prune -a -f && docker compose --profile "*" build --no-cache`

## Workflow
1. Make changes (Python/templates/static)
2. Format: `docker compose run --rm test --format`
3. Lint: `docker compose run --rm test --lint`
4. Test: `docker exec -it django python3 manage.py test`
5. Verify browser: `http://127.0.0.1`
6. CI check: `docker compose run --rm test --ci --verbose`

**Testing:** Extend `StandardTestCase` in `auctions/tests.py` (sets up users, auctions, lots). Test admin permissions, auth checks, no data leaks.

**File guidelines:**
- Python: Django conventions, Ruff formatting
- Templates: `auctions/templates/`, Django syntax
- Static: `auctions/static/`, run `collectstatic` after changes
- Models: Create migrations after changes (`makemigrations`)
- URLs: `auctions/urls.py` or `fishauctions/urls.py`

## Cron Jobs (Management Commands)
Location: `auctions/management/commands/`. Auto in prod (DEBUG=False), manual in dev. Key commands: `endauctions`, `sendnotifications`, `email_invoice`, `send_queued_mail`, `auction_emails`. See `crontab` for full list.

## Critical Rules
1. **ALWAYS** remove first 4 lines from `.env` for dev
2. **ALWAYS** run `docker compose run --rm test --ci` before committing
3. **NEVER** edit `requirements.txt` directly - use `.github/scripts/update-packages.sh`
4. **ALWAYS** use `docker exec -it django` for Django commands
5. **NEVER** commit `.env`, `db.sqlite3`, `mediafiles/`, `logs/`, `*.pyc`
6. Dev server on **port 80**, not 8000
7. Migrations auto-run on start (`entrypoint.sh`)
8. Cron jobs manual in dev (DEBUG=True)
9. Static files auto-collected on start

## Quick Reference
```bash
# Setup
cp .env.example .env && sed -i '1,4d' .env && mkdir -p logs && chmod -R 777 logs
docker compose --profile "*" build && docker compose up -d

# Test/Lint
docker compose run --rm test --ci --verbose  # Before commit
docker compose run --rm test --format        # Auto-format
docker compose run --rm test --lint          # Auto-lint
docker exec -it django python3 manage.py test

# Django
docker exec -it django python3 manage.py makemigrations
docker exec -it django python3 manage.py migrate
docker exec -it django python3 manage.py shell

# Debug
docker logs django
docker compose down && docker compose up -d

# Dependencies
./.github/scripts/update-packages.sh [--upgrade]
```

**Trust these instructions** - validated against actual codebase. Search only if incomplete/incorrect.
