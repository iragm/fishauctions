# Button-style migration — done

`style_reference.md` says the site uses six button classes and no others: `btn-primary` for almost
everything, `btn-info` for the admin-only half of a shared page, `btn-danger` for destruction,
`btn-success text-dark` for saving a form and for the handful of actions a page exists for,
`btn-secondary` for backing out (Cancel, Close, "Back to X"), and `btn-link` for a link that must
not look like a button. It forbids `btn-outline-*`, `btn-warning`, and the dark `btn-close`.

**This worklist is now empty.** Every `btn-outline-*` and `btn-warning` in the templates,
`forms.py`, `models.py`, `tables.py` and `command_palette.js` has been converted, every bare
`btn-close` now carries `btn-close-white`, and the `btn-secondary` that were not on an exit are
gone. What each one became, so the reasoning is repeatable:

* an outline or leftover grey button that was the page's main action → `btn-primary`
* an outline **danger** → `btn-danger` (delete, unpair, revoke, reject)
* an "Edit" or "Trust this user" wearing `btn-warning` → `btn-info` where it is the admin half of a
  shared page, `btn-primary` where the whole page is admin-only (editing is not a warning)
* a **selected/unselected pair** (the feedback ratings, the speaker tags, the List/Map switch, the
  invoice Open/Ready/Paid group) → `btn-secondary` for the unselected options, `btn-primary active`
  for the selected one. Grey *is* right here: an option you have not chosen is not an action the
  page wants you to take, and a row of identical blue buttons with one of them slightly darker is a
  state you have to squint at.
* a banner's **"Don't show again" / "Not this auction" / "Never show this again"** → `btn-secondary`;
  those are dismissals, which is what that grey is for
* `btn-close` → `btn-close btn-close-white`, every time

## The part that was not a class

`btn-close-white` **did not work on this site**, which is why close buttons stayed invisible even
where the markup was already right. Darkly ships a white close glyph and Bootstrap 5.3's
`[data-bs-theme=dark] .btn-close` filter inverted it to black — and that selector outranks
`.btn-close-white`. `auction_site.css` now cancels the inversion. See "Close buttons" in
`style_reference.md`; the same double-recolor trap applies to any Bootswatch component Bootstrap
also restyles for dark.

## Keeping it that way

```bash
grep -rn "btn-outline-\|btn-warning" auctions/templates/ auctions/*.py auctions/static/js/*.js
grep -rn 'btn-close"' auctions/templates/ auctions/*.py
# btn-secondary is allowed, but only on an exit — this lists anything that isn't one:
grep -rn -A2 "btn-secondary" auctions/templates/ auctions/*.py \
  | grep -viE "cancel|close|dismiss|back to|go back|keep this|no thanks|don't show|never show|not interested"
```

The first two return nothing. The third returns only commented-out markup (the disabled Admin
Actions block in `auction.html`, dead layouts in `forms.py` and `tables.py`) and context lines.
