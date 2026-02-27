#!/bin/bash
# Script to grant test database permissions on existing development systems
# Run this if you're getting "Access denied for user 'mysqluser'@'%' to database 'test_auctions'"
# For fresh installations, 01-grant-test-permissions.sql handles this automatically.

# When running inside the container as a MariaDB init script, docker is not available.
# In that case, skip this script - 01-grant-test-permissions.sql already grants permissions.
if ! command -v docker &>/dev/null; then
    exit 0
fi

echo "Granting test database permissions to mysqluser..."
docker exec -it db mariadb -uroot -p${DATABASE_ROOT_PASSWORD:-secret} -e "
GRANT CREATE, DROP ON *.* TO 'mysqluser'@'%';
GRANT ALL PRIVILEGES ON \`test_%\`.* TO 'mysqluser'@'%';
FLUSH PRIVILEGES;
"

if [ $? -eq 0 ]; then
    echo "✓ Permissions granted successfully"
else
    echo "✗ Failed to grant permissions. Make sure the db container is running and DATABASE_ROOT_PASSWORD is correct."
    exit 1
fi
