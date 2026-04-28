-- Grant permissions for the test database.
-- Django's test runner creates databases with a 'test_' prefix
-- (test_<dbname> for serial runs; test_<dbname>_1..N for --parallel).
-- This file is executed only on first-time MariaDB volume initialization.
--
-- ALL PRIVILEGES at database scope already includes CREATE and DROP, so a
-- single pattern-scoped grant covers everything Django needs while keeping
-- the test user out of every other database on the server. (Verified
-- against MariaDB 12.2 -- earlier comments here claimed CREATE/DROP could
-- only be granted globally on `*.*`; that is not the case on the version
-- we ship.)

GRANT ALL PRIVILEGES ON `test\_%`.* TO 'mysqluser'@'%';
FLUSH PRIVILEGES;
