-- Grant permissions for the test database
-- Django's test runner creates test databases with a 'test_' prefix
-- This grants the database user permission to create and use test databases

-- Note: CREATE privilege must be granted globally (on *.*) in MariaDB as it doesn't
-- support database name patterns for CREATE grants. This allows Django to create
-- test databases with any name (e.g., test_auctions, test_auctions_2 for parallel tests)
GRANT CREATE ON *.* TO 'mysqluser'@'%';

-- Grant full privileges on any database starting with 'test_'
-- This pattern matching is supported for table-level privileges in MariaDB
GRANT ALL PRIVILEGES ON `test_%`.* TO 'mysqluser'@'%';

FLUSH PRIVILEGES;
