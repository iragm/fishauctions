# Fish Auctions - Copilot Coding Agent Instructions

## Project Overview

**Fish Auctions** is a free, full-featured auction platform built with Python 3.11.9, Django 5.x, Bootstrap, jQuery, and HTMx. It enables clubs to run online or in-person auctions with features like automatic invoicing, projector support, breeder award programs, multi-location auctions, recommendation systems, and comprehensive statistics. The platform is production-ready and used by dozens of clubs.

### Repository Stats
- **Size**: ~22MB (excluding dependencies)
- **Primary Language**: Python (~20,785 lines across core files)
- **Templates**: 100+ HTML templates
- **Main Application**: Django app called `auctions` (~86 Python files)
- **Database**: MariaDB in production, SQLite in tests
- **Web Server**: Uvicorn (dev) / Gunicorn with Uvicorn workers (prod)
- **Reverse Proxy**: Nginx
- **Cache/Channels**: Redis
- **Containerization**: Docker Compose

## Environment Setup

### Prerequisites
- Docker and Docker Compose installed
- Git

### Initial Setup (Development Environment)

**ALWAYS** follow these steps in order when setting up the development environment:

1. Clone the repository (if not already cloned):
   ```bash
   git clone https://github.com/iragm/fishauctions
   cd fishauctions
   ```

2. Create environment file:
   ```bash
   cp .env.example .env
   ```

3. **CRITICAL**: Edit `.env` and remove the first 4 lines (lines 1-4). These lines configure production settings and must be removed for development:
   ```bash
   sed -i '1,4d' .env
   ```
   Without this step, the development environment will fail to start properly.

4. Create required directories:
   ```bash
   mkdir -p logs
   chmod -R 777 logs
   ```

5. Build containers:
   ```bash
   docker compose --profile "*" build
   ```
   **Expected time**: 5-10 minutes on first build. Subsequent builds use cache and are faster.

6. Start containers:
   ```bash
   docker compose up -d
   ```

7. Access the site at `http://127.0.0.1` (port 80, not 8000 like typical Django projects)

8. Create a superuser (optional but recommended):
   ```bash
   docker exec -it django python3 manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); u=User.objects.create_superuser('admin', 'admin@example.com', 'example'); u.emailaddress_set.create(email=u.email, verified=True, primary=True)"
   ```

## Building, Testing, and Linting

### Running Tests and Lints (CI Mode)

**ALWAYS** run this command before submitting code to ensure it will pass CI:
```bash
docker compose run --rm test --ci --verbose
```
This runs both formatting checks and linting without modifying files. It will fail if changes are required.

**Expected time**: 2-3 minutes

### Code Formatting

**Auto-format code** (modifies files):
```bash
docker compose run --rm test --format
```

**Check formatting without changes** (CI mode):
```bash
docker compose run --rm test --format-check
```

The project uses **Ruff** for formatting with a line length of 120 characters. Configuration is in `ruff.toml`.

### Linting

**Lint and auto-fix issues**:
```bash
docker compose run --rm test --lint
```

**Check linting without changes** (CI mode):
```bash
docker compose run --rm test --lint-check
```

Ruff is configured to check for:
- Import sorting (isort)
- Code style (pycodestyle)
- Pyflakes errors
- flake8-builtins, comprehensions, logging, pytest-style, pathlib usage
- Pyupgrade for modern Python patterns

### Running Django Tests

The project uses Django's built-in test framework. Tests are located in `auctions/tests.py`.

**Run tests inside the Django container**:
```bash
docker exec -it django python3 manage.py test
```

**Note**: Tests require the containers to be running (`docker compose up -d`).

### Pre-commit Hooks (Optional)

The project supports pre-commit hooks but they are **optional**. To use:
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files  # First time only
```

Configuration is in `.pre-commit-config.yaml`.

## Django Management Commands

**ALWAYS** run Django management commands inside the Django container:

```bash
docker exec -it django python3 manage.py <command>
```

Common commands:
- `makemigrations` - Create new migrations
- `migrate` - Apply migrations
- `collectstatic` - Collect static files
- `shell` - Django shell
- `createsuperuser` - Create a superuser

**Special Note on Migrations**: If you encounter a permission denied error about `0006_alter_subscriptioninfo_user_agent.py`, run:
```bash
docker exec -u root -it django python3 manage.py makemigrations
```

### Cron Jobs (Management Commands)

The project uses custom management commands (located in `auctions/management/commands/`) that run via cron in production. In development, these must be run manually:

- `endauctions` - End auctions and declare winners
- `sendnotifications` - Send reminder emails
- `auctiontos_notifications` - Welcome and print reminder emails  
- `email_invoice` - Email users about invoices
- `send_queued_mail` - Send queued emails
- `auction_emails` - Send auction-related emails
- `email_unseen_chats` - Send unread chat notifications
- `weekly_promo` - Weekly promotional email (Wednesdays)
- `set_user_location` - Set user locations
- `remove_duplicate_views` - Clean up duplicate page views
- `webpush_notifications_deduplicate` - Deduplicate push notifications

These run automatically in production (DEBUG=False) via the crontab defined in `/home/app/web/crontab`.

## Adding or Updating Dependencies

**NEVER** edit `requirements.txt` or `requirements-test.txt` directly. Instead:

1. Edit `requirements.in` (production deps) or `requirements-test.in` (test deps)
2. Run the update script:
   ```bash
   ./.github/scripts/update-packages.sh
   ```

To upgrade all packages to latest versions:
```bash
./.github/scripts/update-packages.sh --upgrade
```

This uses `pip-compile` inside the Django container to generate pinned `requirements.txt` files.

## Project Structure and Architecture

### Directory Layout

```
fishauctions/
├── .github/
│   ├── scripts/
│   │   ├── prepare-ci.sh         # CI setup script
│   │   ├── test-and-lint.sh      # Test/lint runner script
│   │   └── update-packages.sh    # Dependency update script
│   └── workflows/
│       ├── test.yml              # GitHub Actions CI workflow
│       └── image-builds.yml      # Docker image builds
├── auctions/                     # Main Django application
│   ├── management/commands/      # Custom management commands
│   ├── migrations/               # Database migrations (180+ migration files)
│   ├── static/                   # Static files (CSS, JS, images)
│   ├── templates/                # HTML templates (100+ templates)
│   ├── admin.py                  # Django admin configuration
│   ├── consumers.py              # WebSocket consumers (Channels)
│   ├── context_processors.py    # Template context processors
│   ├── filters.py                # Django filters
│   ├── forms.py                  # Django forms (~2,972 lines)
│   ├── models.py                 # Django models (~4,929 lines)
│   ├── routing.py                # WebSocket routing
│   ├── tables.py                 # Django tables2 configuration
│   ├── tests.py                  # Test cases
│   ├── urls.py                   # URL routing
│   └── views.py                  # Views (~7,782 lines)
├── fishauctions/                 # Django project settings
│   ├── asgi.py                   # ASGI configuration (for Channels/WebSockets)
│   ├── settings.py               # Django settings (reads from .env)
│   ├── urls.py                   # Root URL configuration
│   └── wsgi.py                   # WSGI configuration
├── .devcontainer/                # VS Code dev container config
├── .vscode/                      # VS Code settings (Ruff formatting)
├── docker-compose.yaml           # Docker Compose configuration
├── Dockerfile                    # Multi-stage Dockerfile
├── entrypoint.sh                 # Container entrypoint script
├── manage.py                     # Django management script
├── requirements.in               # Production dependencies
├── requirements.txt              # Pinned production dependencies
├── requirements-test.in          # Test dependencies (ruff, pre-commit)
├── requirements-test.txt         # Pinned test dependencies
├── ruff.toml                     # Ruff configuration
├── .pre-commit-config.yaml       # Pre-commit hooks configuration
├── .gitignore                    # Git ignore rules
├── crontab                       # Cron job definitions
├── task.sh                       # Cron job wrapper script
├── update.sh                     # Production update script
├── nginx.dev.conf                # Nginx config for development
├── nginx.prod.conf               # Nginx config for production
└── readme.md                     # Project README
```

### Key Models (auctions/models.py)

The main models include:
- `User` - Extended Django user model with `UserData`
- `Auction` - Auction configuration and settings
- `Lot` - Items for auction
- `Bid` - Bids on lots
- `Invoice` - Generated invoices
- `AuctionTOS` - User participation in auctions
- `PickupLocation` - Pickup locations for auctions
- `Category` - Lot categories
- `ChatMessage` - User messaging
- `PageView` - Analytics/tracking
- Plus many more supporting models

### Configuration Files

- **ruff.toml** - Linting and formatting configuration (line-length: 120)
- **.pre-commit-config.yaml** - Pre-commit hooks (trailing whitespace, EOF fixer, YAML check, Ruff)
- **docker-compose.yaml** - Defines 5 services: web (Django), db (MariaDB), redis, nginx, test
- **Dockerfile** - Multi-stage build (builder, test, dev, final)
- **.env** - Environment variables (never commit this file)
- **.gitignore** - Excludes: *.pyc, .env, db.sqlite3, media/, staticfiles/, logs/, swag/

### Important Environment Variables (.env file)

The application uses the 12-factor app pattern - all configuration is in `.env`:

- `DEBUG` - Set to 'False' for production, unset/remove line for development
- `SECRET_KEY` - Django secret key
- `DATABASE_*` - Database connection settings (defaults work for Docker)
- `SITE_DOMAIN` - Your domain name
- `REDIS_PASSWORD` - Redis password
- Email settings (Gmail SMTP or AWS SES)
- API keys (Google Maps, reCAPTCHA, OAuth, etc.)
- Feature flags (`ALLOW_USERS_TO_CREATE_AUCTIONS`, `ENABLE_HELP`, etc.)

**Development defaults** are set for most variables in `fishauctions/settings.py`.

## CI/CD and Validation

### GitHub Actions Workflows

The repository uses GitHub Actions for CI. Workflows are in `.github/workflows/`:

1. **test.yml** - Runs on all pushes and PRs to master:
   - Runs `.github/scripts/prepare-ci.sh` (creates .env, logs directory)
   - Runs `docker compose run --rm test --ci --verbose` (format check + lint check)
   - Also runs pre-commit validation

**To replicate CI locally**:
```bash
./.github/scripts/prepare-ci.sh
docker compose run --rm test --ci --verbose
```

### Pre-commit Validation

Pre-commit checks (can run locally or in CI):
- Trailing whitespace removal
- End-of-file fixer
- YAML validation
- Large file check
- Ruff linting and formatting

## Common Pitfalls and Workarounds

### 1. Development Environment Not Starting

**Problem**: Containers fail to start or site doesn't load.
**Solution**: Ensure you removed the first 4 lines from `.env`. They set production-only configurations.

### 2. Permission Denied on Migrations

**Problem**: `webpush` migration file permission error.
**Solution**: Run migrations as root:
```bash
docker exec -u root -it django python3 manage.py makemigrations
```

### 3. Port 80 Already in Use

**Problem**: Port 80 is used by another service.
**Solution**: Add `HTTP_PORT=81` (or another port) to `.env` and access site at `http://127.0.0.1:81`

### 4. Static Files Not Loading

**Problem**: CSS/JS/images not loading.
**Solution**: 
```bash
docker exec -it django python3 manage.py collectstatic --no-input
```

### 5. Database Migration Issues

**Problem**: Database out of sync.
**Solution**: Run migrations:
```bash
docker exec -it django python3 manage.py migrate
```

### 6. Tests Failing in Fresh Environment

**Problem**: Test database errors.
**Solution**: Ensure containers are running (`docker compose up -d`) before running tests.

### 7. Container Build Failures

**Problem**: Docker build fails during pip install.
**Solution**: Clear Docker cache and rebuild:
```bash
docker compose down
docker system prune -a -f
docker compose --profile "*" build --no-cache
```

## Making Code Changes

### Typical Workflow

1. **Make your changes** to Python files, templates, or static files
2. **Format code**: `docker compose run --rm test --format`
3. **Lint code**: `docker compose run --rm test --lint`
4. **Run tests**: `docker exec -it django python3 manage.py test`
5. **Verify in browser**: Check changes at `http://127.0.0.1`
6. **Run CI checks**: `docker compose run --rm test --ci --verbose`

### File Modification Guidelines

- Python files: Follow Django conventions, use Ruff formatting
- Templates: Located in `auctions/templates/`, Django template syntax
- Static files: In `auctions/static/`, collected with `collectstatic`
- Models: Always create migrations after model changes
- URLs: Add to `auctions/urls.py` or `fishauctions/urls.py`
- Forms: Use Django forms in `auctions/forms.py`
- Views: Add to `auctions/views.py`

### Testing Your Changes

The project has a base test class `StandardTestCase` in `auctions/tests.py` that sets up common test data (users, auctions, lots, etc.). When adding tests:

- Extend `StandardTestCase` for auction-related tests
- Test admin permissions (`is_admin=True`)
- Test that non-logged-in users are blocked appropriately
- Test no data leaks to unauthorized users
- Run: `docker exec -it django python3 manage.py test`

## Development Tools

### VS Code Development Container

The project is optimized for VS Code with dev containers:
1. Install "Remote Development" extension pack
2. Open project and select "Reopen in Container"
3. Extensions auto-installed: Ruff, Python, Pylance, GitLens

### Debugging

- Set `DEBUG=True` in `.env` (or remove the DEBUG line)
- Django Debug Toolbar is installed (`django_debug_toolbar` in INSTALLED_APPS)
- Logs: `docker logs django` or check `./logs/` directory
- Database: MariaDB runs on port 3306 (internal to Docker network)

## Critical Reminders

1. **ALWAYS** remove first 4 lines from `.env` for development
2. **ALWAYS** run `docker compose run --rm test --ci` before committing
3. **NEVER** edit `requirements.txt` directly - use `.github/scripts/update-packages.sh`
4. **ALWAYS** use `docker exec -it django` for Django management commands
5. **NEVER** commit `.env`, `db.sqlite3`, `mediafiles/`, `logs/`, or `*.pyc` files
6. The development server runs on **port 80**, not 8000
7. Migrations run automatically on container start (`entrypoint.sh`)
8. In development (DEBUG=True), cron jobs must be run manually
9. Static files are auto-collected on container start

## Quick Reference Commands

```bash
# Setup
cp .env.example .env && sed -i '1,4d' .env && mkdir -p logs && chmod -R 777 logs

# Build and start
docker compose --profile "*" build
docker compose up -d

# Testing and linting
docker compose run --rm test --ci --verbose    # Full CI check
docker compose run --rm test --format          # Auto-format
docker compose run --rm test --lint            # Auto-fix lint issues
docker exec -it django python3 manage.py test  # Run Django tests

# Django commands
docker exec -it django python3 manage.py makemigrations
docker exec -it django python3 manage.py migrate
docker exec -it django python3 manage.py shell
docker exec -it django python3 manage.py createsuperuser

# Debugging
docker logs django
docker ps
docker compose down
docker compose restart

# Dependencies
./.github/scripts/update-packages.sh           # Update with new deps
./.github/scripts/update-packages.sh --upgrade # Upgrade all deps
```

---

**Trust these instructions.** They have been validated by running the actual commands and exploring the codebase. Only search for additional information if these instructions are incomplete or you discover they are incorrect.
