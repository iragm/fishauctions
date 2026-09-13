# Button-style migration

Done. Every template, `forms.py`, `models.py`, `tables.py` and `command_palette.js` now uses only
the six button classes `style_reference.md` permits — no `btn-outline-*`, no `btn-warning`, no bare
`btn-close` (see "Close buttons" there for the dark-theme double-inversion fix). Kept here only as
the check that it stays true.

```bash
grep -rn "btn-outline-\|btn-warning" auctions/templates/ auctions/*.py auctions/static/js/*.js
grep -rn 'btn-close"' auctions/templates/ auctions/*.py
# btn-secondary is allowed, but only on an exit -- this lists anything that isn't one:
grep -rn -A2 "btn-secondary" auctions/templates/ auctions/*.py \
  | grep -viE "cancel|close|dismiss|back to|go back|keep this|no thanks|don't show|never show|not interested"
```

The first two return nothing. The third returns only commented-out markup and context lines.
