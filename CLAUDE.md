# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Django 5.x auction platform with Python 3.11.9, Bootstrap 5, jQuery, HTMx. MariaDB, Redis, Nginx, Uvicorn/Gunicorn, Celery, Docker Compose. Main app: `auctions/` (~5k lines models.py, ~8k lines views.py, ~3k lines forms.py).

## Development Setup

```bash
cp .env.example .env && sed -i '1,4d' .env  # Remove first 4 lines (production config)
mkdir -p logs && chmod -R 777 logs
docker compose --profile "*" build          # 5-10 min first time
docker compose up -d
```

Access at `http://127.0.0.1` (port **80**, not 8000).

Create superuser:
```bash
docker exec -it django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
```

## Testing & Linting

```bash
docker compose run --rm test --ci --verbose   # Run before every commit (format + lint + tests)
docker compose run --rm test --format         # Auto-fix formatting
docker compose run --rm test --lint           # Auto-fix linting
docker exec -it django python3 manage.py test # Run Django tests (requires compose up)
```

Ruff config: `ruff.toml` (line-length: 120). Replicate CI locally: `./.github/scripts/prepare-ci.sh && docker compose run --rm test --ci --verbose`

## Django Commands

Always run inside the container:
```bash
docker exec -it django python3 manage.py makemigrations
docker exec -it django python3 manage.py migrate
docker exec -it django python3 manage.py shell
```

Migration permission error? Use `docker exec -u root -it django ...`

## Species list

`Lot.species` is picked from `Species`, which comes from two places:

* a **pinned** FishBase snapshot (`FISHBASE_VERSION` in `auctions/fishbase.py`) — ~36k fish, with
  family/order and FishBase's aquarium-trade rating
* `auctions/data/aquarium_species.csv`, a curated list of plants, freshwater invertebrates, live
  foods and **cultivars** — everything FishBase has never heard of. Edit the CSV, re-run the
  import. Its header states the rule for adding a row.

SeaLifeBase is **deliberately not imported** (102k mostly-marine species, and no cherry shrimp);
the code is kept and `--databases fb,slb` still works. See the comment in `auctions/fishbase.py`.

```bash
docker exec django python3 manage.py import_fishbase --check-version   # is the pin stale?
docker exec django python3 manage.py import_fishbase                    # ~36k species, ~1 min
docker exec django python3 manage.py import_fishbase --only-curated     # just the CSV, no download
docker exec django python3 manage.py import_fishbase --only-categories  # re-map family -> Category
docker exec django python3 manage.py import_fishbase --only-legacy --dry-run  # pre-import leftovers
docker exec django python3 manage.py import_fishbase --purge slb        # drop an unused source
```

A **cultivar** ("Blue Dream", "Halfmoon") is a `Species` row with `variety` set and `parent`
pointing at the nominal species, carrying the parent's genus and epithet — so breeder points,
genus BAP rules and the category all still see the plain species. Show `full_scientific_name`,
never `scientific_name`, wherever a human reads it.

Adding a species is an **admin workflow, not a database edit**: `/admin-dashboard/species-gaps/`
lists the lot names that keep showing up with no species (the sibling of the command palette's
bounce list), and each row links to `/species/new/` with the name prefilled — fill in two boxes
and every lot with that name gets the species, plus the matcher learns the name. Rows added there
are `source="admin"`, which `import_fishbase --only-legacy` deliberately never touches.

`Species.trade_rank` (0 = in the hobby, 1 = its genus is, 2 = neither) is what suggestions are
ordered by. FishBase's own `Aquarium` column is not enough on its own — it files *Chindongo
saulosi* under "never/rarely" — so the genus gets a say and `in_trade_override` lets a person
overrule it. `Species.save()` maintains the species tier; the genus tier needs
`Species.recompute_trade_ranks()`, which the importer runs.

`Species.category` is derived from family/order by `auctions/species_categories.py`, which maps
onto whatever categories a site's admins have actually created (it never creates one). A lot with
a species takes that category, and the lot forms hide their category picker while a species is
chosen.

Matching a typed lot name to a species lives in `auctions/species_matching.py` (exact, then
token/phrase search, then the LLM, with every answer cached in `SpeciesSearchCache`). Its rules are
deliberately strict — a wrong species ends up on a printed label and in breeder points, so "no
match" is a better answer than a plausible one. See `auctions/test_species.py`.

## Dependencies

Never edit `requirements.txt` directly. Edit `requirements.in` or `requirements-test.in`, then:
```bash
./.github/scripts/update-packages.sh           # Add new packages
./.github/scripts/update-packages.sh --upgrade # Upgrade all
```

## Architecture

```
auctions/            # Main app: models, views, forms, templates, static, migrations (180+)
  management/commands/  # Cron jobs: endauctions, sendnotifications, email_invoice, etc.
  tests.py             # Extend StandardTestCase for test setup (users, auctions, lots)
fishauctions/        # Project settings (reads .env), ASGI, URLs, Celery config
docker-compose.yaml  # Services: web (Django), db (MariaDB), redis, nginx, celery-worker, celery-beat, test
Dockerfile           # Multi-stage: builder → test → dev → final
entrypoint.sh        # Auto-runs migrate, collectstatic on start; uvicorn (dev) / gunicorn (prod)
ruff.toml            # Linting/format config
```

**Key models:** User/UserData, Auction, Lot, Bid, Invoice, AuctionTOS, PickupLocation, Category, Species, ChatMessage, PageView

**URLs:** `auctions/urls.py` and `fishauctions/urls.py`

## Frontend / Templates

- **Template tags must open and close on one line.** Django's lexer has no `re.DOTALL`, so a
  `{# … #}`, `{% … %}`, or `{{ … }}` split across two lines is not an error — Django doesn't
  recognize it and renders it onto the page as text for users to read. Use
  `{% comment %} … {% endcomment %}` for any note longer than one line. Enforced by
  `auctions/template_lint.py` (a pre-commit hook, part of `--ci`/`--lint`, and
  `auctions/test_template_hygiene.py`).

- Read `style_reference.md` before making any frontend/template/CSS change. It
  documents the palette, text-on-color rules, outline-button and pagination
  fixes, the unavailable-action ("stay clickable") standard, and the message-type
  taxonomy. Never edit vendor CSS; site-wide overrides go in `auctions/static/css/auction_site.css`.

## Model Changes

- Always create migrations after model changes (`makemigrations` then `migrate`).
- When adding fields to `Auction`, check if they belong in `fields_to_clone` in `AuctionCreateView`.

## Common Issues

| Problem | Fix |
|---|---|
| Won't start | First 4 lines of `.env` not removed |
| Port 80 in use | Add `HTTP_PORT=81` to `.env` |
| Migration permission error | Use `docker exec -u root -it django ...` |
| Static files missing | `docker exec -it django python3 manage.py collectstatic --no-input` |
| DB out of sync | `docker exec -it django python3 manage.py migrate` |
| Build fails | `docker compose down && docker system prune -a -f && docker compose --profile "*" build --no-cache` |
