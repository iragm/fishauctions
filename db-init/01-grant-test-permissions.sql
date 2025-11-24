-- Grant permissions for the test database
-- Django's test runner creates test databases with a 'test_' prefix
-- This grants the database user permission to create and use test databases

GRANT CREATE ON *.* TO 'mysqluser'@'%';
GRANT ALL PRIVILEGES ON `test_%`.* TO 'mysqluser'@'%';
FLUSH PRIVILEGES;
