#!/bin/bash
# Apply test-database permissions to an existing development MariaDB volume.
#
# Fresh installations get the right grants from db-init/01-grant-test-permissions.sql
# on first volume initialization. Run this script if you upgraded an existing
# dev volume that was provisioned by an older version of that init SQL (which
# granted CREATE/DROP on *.*) -- it tightens the grant down to test_%.* and
# revokes the over-broad prior grant if present.
#
# Symptom that means you need to run this:
#   "Access denied for user 'mysqluser'@'%' to database 'test_auctions'"

set -euo pipefail

ROOT_PW="${DATABASE_ROOT_PASSWORD:-secret}"

run_sql() {
    docker exec -i db mariadb -uroot -p"${ROOT_PW}" -e "$1"
}

echo "Inspecting current grants for mysqluser..."
GRANTS="$(docker exec -i db mariadb -uroot -p"${ROOT_PW}" -BNe \
    "SHOW GRANTS FOR 'mysqluser'@'%'" 2>/dev/null || true)"

if echo "${GRANTS}" | grep -qE 'GRANT [A-Z, ]*(CREATE|DROP)[A-Z, ]* ON \*\.\*'; then
    echo "Found over-broad CREATE/DROP grant on *.*; revoking..."
    run_sql "REVOKE CREATE, DROP ON *.* FROM 'mysqluser'@'%';"
fi

echo "Granting pattern-scoped permissions on test_%.* ..."
run_sql "GRANT ALL PRIVILEGES ON \`test\\_%\`.* TO 'mysqluser'@'%'; FLUSH PRIVILEGES;"

echo "Final grants:"
run_sql "SHOW GRANTS FOR 'mysqluser'@'%';"

echo "✓ Permissions updated"
