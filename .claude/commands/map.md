---
description: Regenerate docs/module_map.md, the which-file-do-I-open index
allowed-tools: Bash(python3 auctions/module_map.py*), Read, Edit
---

```
python3 auctions/module_map.py --write
```

`docs/module_map.md` is generated from every module's own docstring and top-level names, so it
cannot drift -- but the checked-in copy can go stale, and both `--ci` and a test fail when it has.
Regenerate and commit it in the same change as whatever moved.

If the run reports a module over 300 lines with no docstring, **write the docstring** rather than
reaching for the threshold. That docstring is the entire anti-drift mechanism here: it sits in the
diff of the change that would invalidate it, which a separate document never does. Say what the
module is *for* and what is non-obvious about it, not what its classes are called.
