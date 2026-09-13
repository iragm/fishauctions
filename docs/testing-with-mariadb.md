# Test database (MariaDB)

Tests run against MariaDB, not SQLite, to match production. `manage.py test` creates `test_auctions`,
migrates it, runs, then drops it — entirely separate from the production `auctions` database.

## Permissions

`db-init/01-grant-test-permissions.sql` grants the app's MariaDB user CREATE/DROP scoped to the
`test_%` pattern (`test_auctions`, and `test_auctions_1`..`N` under `--parallel`) — nothing else on
the server. Init scripts run only on first volume init, so an existing volume needs a manual grant:

```bash
./grant-test-permissions.sh   # revokes any prior over-broad CREATE/DROP on *.*, applies the scoped grant; safe to re-run
```

or by hand:

```bash
docker exec -it db mariadb -uroot -p"${DATABASE_ROOT_PASSWORD}" -e "
REVOKE CREATE, DROP ON *.* FROM 'mysqluser'@'%';
GRANT ALL PRIVILEGES ON \`test\_%\`.* TO 'mysqluser'@'%';
"
```

## Running tests

```bash
docker exec -it django python3 manage.py test
docker exec -it django python3 manage.py test auctions
docker exec -it django python3 manage.py test auctions.tests.ViewLotTest
docker exec -it django python3 manage.py test --verbosity=2
```

Needs the db container healthy (`docker compose up -d`). CI's
`docker compose up --detach --wait --wait-timeout 60` does the same wait before running tests.
