# The views package

34 modules split by area out of one old `views.py`. `docs/module_map.md` lists them.

- **`base.py` is the only bulk import, and the graph is acyclic.** A cycle is an `ImportError` at
  startup with no culprit named. A helper two areas need moves to `base.py`.
- **`__init__.py` re-exports with `import *`** for `urls.py`. Private `_helpers` aren't exported:
  import and patch them where they are defined.
- **Absolute imports** (`from auctions.models import …`), enforced by ruff TID252.
- **Split by area, never by line count.**

Where things live:

- `check_club_permission` (`base.py`) is the one club permission gate, shared with the API and palette.
- Membership renewal helpers are in `base.py`: four areas use them.
- `bulk_actions.py` holds the many-row writes, which is why none is a skill.
