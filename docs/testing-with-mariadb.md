# Test Database Configuration

## Overview

Tests now run against a MariaDB database instead of SQLite to better match the production environment.

## How It Works

1. **Django Test Runner**: When you run `python3 manage.py test`, Django automatically:
   - Creates a separate test database named `test_auctions`
   - Runs all migrations on the test database
   - Executes the tests
   - Destroys the test database when tests complete

2. **Database Permissions**: The MariaDB container is configured with initialization scripts in `db-init/` that automatically grant the necessary permissions for creating test databases.

3. **Health Checks**: The database service includes a health check that validates the actual Django user credentials work correctly.

4. **No Production Impact**: The test database is completely separate from the production `auctions` database, so running tests never affects production data.

## Upgrading Existing Development Systems

If you're upgrading from a previous version and have an existing MariaDB volume, you may need to manually grant test database permissions. (The init scripts in `db-init/` only run on first volume initialization, so changes to them do not apply to existing volumes.)

```bash
# Option 1: Run the helper script from the repo root. It detects and revokes
#          any prior over-broad CREATE/DROP grant on *.* before applying the
#          tightened pattern-scoped grant on test_%.* — safe to re-run.
./grant-test-permissions.sh

# Option 2: Apply the same change manually
docker exec -it db mariadb -uroot -p${DATABASE_ROOT_PASSWORD} -e "
REVOKE CREATE, DROP ON *.* FROM 'mysqluser'@'%';
GRANT ALL PRIVILEGES ON \`test_%\`.* TO 'mysqluser'@'%';
FLUSH PRIVILEGES;
"
```

The pattern-scoped grant restricts the test user to creating and dropping databases whose names start with `test_` (e.g. `test_auctions`, `test_auctions_1`...`test_auctions_N` for `--parallel`). They cannot create or drop any other database on the server, including the production `auctions` database.

## Running Tests

```bash
# Run all tests
docker exec -it django python3 manage.py test

# Run tests for a specific app
docker exec -it django python3 manage.py test auctions

# Run a specific test class
docker exec -it django python3 manage.py test auctions.tests.ViewLotTest

# Run tests with more verbose output
docker exec -it django python3 manage.py test --verbosity=2
```

## Requirements

- The database container must be running and healthy (`docker compose up -d`)
- The database user must have privileges on the `test_%` database name pattern (automatically configured via `db-init/01-grant-test-permissions.sql` on first volume initialization)

## Continuous Integration

The GitHub Actions workflow in `.github/workflows/image-builds.yml` automatically runs tests against MariaDB as part of the CI pipeline. The `docker compose up --detach --wait --wait-timeout 60` command ensures the database is fully ready before running tests.
