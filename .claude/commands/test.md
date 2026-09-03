---
description: Run the Django test suite (or the modules you name)
argument-hint: "[dotted.test.path ...]"
allowed-tools: Bash(docker exec django python3 manage.py test *), Bash(docker compose ps*), Read
---

Run the site's tests: `$ARGUMENTS` if anything was named, otherwise the whole suite.

```
docker exec django python3 manage.py test --parallel auto $ARGUMENTS
```

Rules that matter here, all of which have already cost somebody an afternoon:

- **Never start a second run while one is going.** Both share the single `test_auctions` database
  and corrupt each other into hundreds of unrelated errors. Check with
  `docker exec db mariadb -e 'SHOW PROCESSLIST' 2>/dev/null | grep -c test_auctions` if unsure.
- **Run it in the background.** The whole suite is ~2.5 minutes parallel, and about half of that is
  building the test database from ~290 migrations rather than running tests.
- **In a parallel log, the first `failed:` block is the real failure.** Anything after
  `Destroying test database for alias 'default'...` is collateral from the run tearing itself down.
- `--ci` does *not* run tests -- it is format and lint only. They are separate steps.

If a run dies half way and leaves a broken database, re-run with `--noinput` to rebuild it.
