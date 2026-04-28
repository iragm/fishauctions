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
DB_USER="${DATABASE_USER:-mysqluser}"

run_sql() {
    docker exec -i db mariadb -uroot -p"${ROOT_PW}" -e "$1"
}

# MariaDB returns ERROR 1141 only when the user has no grant entry at all for
# the scope; tolerate that case so the script is idempotent on already-
# tightened systems. Any other error propagates.
tolerate_no_such_grant() {
    local stderr_file rc
    stderr_file="$(mktemp)"
    trap 'rm -f "${stderr_file}"' RETURN
    if docker exec -i db mariadb -uroot -p"${ROOT_PW}" -e "$1" 2>"${stderr_file}"; then
        return 0
    fi
    rc=$?
    if grep -q "ERROR 1141" "${stderr_file}"; then
        return 0
    fi
    cat "${stderr_file}" >&2
    return "${rc}"
}

tolerate_no_such_grant "REVOKE CREATE, DROP ON *.* FROM '${DB_USER}'@'%';"
run_sql "GRANT ALL PRIVILEGES ON \`test\\_%\`.* TO '${DB_USER}'@'%';"
echo "✓ Permissions updated"
