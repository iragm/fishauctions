---
description: Format, lint, template check and module-map check -- everything CI runs except tests
allowed-tools: Bash(docker compose run --rm test *), Bash(python3 auctions/*), Read, Edit
---

Run the checks that gate a commit:

```
docker compose run --rm test --ci --verbose
```

That is `ruff format --check`, `ruff check`, the Django template-tag lint
(`auctions/template_lint.py`) and the module-map check (`auctions/module_map.py`) -- **no tests**.

To fix rather than report:

```
docker compose run --rm test --format     # apply formatting
docker compose run --rm test --lint       # apply lint fixes
python3 auctions/module_map.py --write    # regenerate docs/module_map.md
```

If the module-map step complains that a module is too big or has no docstring, read
`auctions/module_map.py` -- the rule and the reason for it are in its docstring. Adding a module to
`OVERSIZED` is a last resort and needs a reason; the list is a ratchet that is supposed to shrink.
