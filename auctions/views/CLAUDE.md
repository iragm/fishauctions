# The views package

`views.py` used to be one 27,621-line module. It is now 34 modules in this directory, split by area
along the seams the file already had: it was written in thematic runs with each block of constants
sitting just above the views that use them, so almost every module here is one contiguous stretch of
the original file.

Four rules hold it together.

- **`base.py` is the only module the others import from in bulk**, and the import graph is
  **acyclic**. That property is what made the split possible and it is not decorative: a cycle here
  is an `ImportError` at startup, and Django will not tell you which module to blame. If a helper is
  wanted in two areas, it moves to `base.py` -- it does not get imported sideways.
- **`__init__.py` re-exports everything with `import *`**, because `urls.py` refers to 347 of these
  names as `views.SomeView`. That works for public names only. A **private** helper (`_foo`) is
  imported from the module that defines it -- `from auctions.views.base import _foo` -- and so is
  patched there in tests, since patching `auctions.views._foo` no longer reaches anything.
- **Imports of the rest of the app are absolute** (`from auctions.models import ...`), not `..`
  relative. Ruff's TID252 enforces it, and the two older packages here (`mcp/`, `mobile/`) already
  did it that way.
- **No module over 1500 lines.** `auctions/module_map.py` fails the build on a new one. When a
  module gets close, split it along an area boundary and give both halves a real docstring rather
  than adding it to the `OVERSIZED` list.

Every module's docstring says what area it covers; `docs/module_map.md` lists them all in one place.
Start there rather than grepping blind.

## Where the awkward ones live

- **Permissions**: `check_club_permission` in `base.py` is the single gate for "may this user do
  this to this club", shared with the club API and the command palette so it cannot be checked two
  ways.
- **Membership renewal**: `_process_invoice_membership_renewal` and friends are in `base.py`, not
  with the invoices, because invoices, payments, webhooks and the club member pages all four reach
  for them.
- **The bulk buttons** (`bulk_actions.py`) are the only writes on the site that touch many rows at
  once, which is exactly why none of them is an assistant skill.
- **`club_api.py`** carries the API's three standing rules: everything naming a person is inside a
  `private` block that is *absent* without the privacy flag, `?filter=` searches public columns only
  whoever sends it, and `?ordering=` is an allowlist rather than a pass-through to `order_by`.
