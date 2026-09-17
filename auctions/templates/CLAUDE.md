# Templates, styles and navigation

Read `style_reference.md` before any visual change. `docs/style_migration.md` lists files that don't
conform yet; fix a few while you're there.

- **Template tags open and close on one line** (`auctions/template_lint.py` enforces it).

## Navigation

`base.html` draws the navbar and at most one sidebar: club (`club_sidebar.html`, `btn-info`) or
account setup (`account_sidebar.html`, `btn-primary`). Column at `>=lg`, offcanvas below.

- **`auctions/account_nav.py` is the list**, and `active_page()` decides whether a page draws the
  sidebar at all. Drop a page from `GROUPS` and, in the app, it has no way out.
  `SidebarReachTests` checks every page.
- The navbar user menu and the app drawer (`mobile/menu.py`, section id `account`) are the same three
  rows: Invoices, Feedback, Account (`/account/setup/`, the last account page visited).
- `Row.gate` is the only per-user condition (PayPal, Square).
- `account` and `userpage` are one page.
- No row is `text-danger`, Delete account included.

## Preferences vs notifications

`/preferences/` is what the site shows you; `/notifications/` is when it may contact you. No field is
on both.

- That's why neither page needs JavaScript: `distance_unit` and the radii are on different pages.
  Distances are stored in miles; the notifications form converts in `__init__` and `clean()`.
- `palette_actions._preference_form_for` finds a field's form. A field on neither is off the tool.
- `OwnUserDataUpdate` lists `SuccessMessageMixin` first, or the success message never shows.
