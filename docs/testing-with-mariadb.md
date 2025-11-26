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

If you're upgrading from a previous version and have an existing MariaDB volume, you may need to manually grant test database permissions:

```bash
# Option 1: Run the helper script
./db-init/grant-permissions-existing-db.sh

# Option 2: Grant permissions manually
docker exec -it db mariadb -uroot -p${DATABASE_ROOT_PASSWORD} -e "
GRANT CREATE, DROP ON *.* TO 'mysqluser'@'%';
GRANT ALL PRIVILEGES ON \`test_%\`.* TO 'mysqluser'@'%';
FLUSH PRIVILEGES;
"
```

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
- The database user must have CREATE and DROP privileges (automatically configured via `db-init/01-grant-test-permissions.sql`)

## Continuous Integration

The GitHub Actions workflow in `.github/workflows/image-builds.yml` automatically runs tests against MariaDB as part of the CI pipeline. The `docker compose up --detach --wait --wait-timeout 60` command ensures the database is fully ready before running tests.
