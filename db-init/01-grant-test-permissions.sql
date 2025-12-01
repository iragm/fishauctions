-- Grant permissions for the test database
-- Django's test runner creates test databases with a 'test_' prefix
-- This grants the database user permission to create and use test databases

-- Note: CREATE and DROP privileges must be granted globally (on *.*) in MariaDB as it doesn't
-- support database name patterns for these grants. This allows Django to create and drop
-- test databases with any name (e.g., test_auctions, test_auctions_2 for parallel tests)

-- Check if user already has these privileges, if not grant them
-- This makes the script idempotent for existing dev systems with persistent volumes
GRANT CREATE, DROP ON *.* TO 'mysqluser'@'%';

-- Grant full privileges on any database starting with 'test_'
-- This pattern matching is supported for table-level privileges in MariaDB
GRANT ALL PRIVILEGES ON `test_%`.* TO 'mysqluser'@'%';

FLUSH PRIVILEGES;
