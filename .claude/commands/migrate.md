---
description: Make and apply migrations inside the container
argument-hint: "[app] [--name description]"
allowed-tools: Bash(docker exec *django python3 manage.py *), Read
---

```
docker exec -i django python3 manage.py makemigrations $ARGUMENTS
docker exec django python3 manage.py migrate
```

`-i` and not `-it`: `makemigrations` asks questions ("did you rename x to y?") so it needs stdin,
but `docker exec -t` fails outright with "the input device is not a TTY" whenever the caller is
not a terminal -- which is every command run from an agent, a hook or a script. Same for the
permission-error form: `docker exec -u root django ...`.

Before you get here:

- A field taken off a model must come off **every form naming it in the same commit**. A form with
  a dropped field raises `FieldError` at import, `urls.py` fails to load, and the container
  crash-loops behind an entrypoint that refuses to serve a half-migrated database -- at which point
  `makemigrations` cannot run at all. Fix the forms first.
- A new field on `Auction` probably belongs in `AUCTION_FIELDS_TO_CLONE` too. Check.
- Never rebuild a foreign key. Every FK in this database is stored under a mangled
  `table?constraint` name that MariaDB will not `DROP`, so a migration that recreates one fails on
  the real database while passing against a test database built from scratch.
