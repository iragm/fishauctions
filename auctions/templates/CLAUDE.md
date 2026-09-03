# Templates, styles and the navigation surfaces

Loaded when you touch anything under `auctions/templates/`. Read `style_reference.md` at the
repository root before any visual change -- it is the palette, the six permitted button classes,
the close-button and help-note patterns, and the message-type taxonomy.
`docs/style_migration.md` is the worklist of files that do not conform yet; take a few off it
when you are in a template anyway.

- **Template tags must open and close on one line.** Django's lexer has no `re.DOTALL`, so a
  `{# … #}`, `{% … %}`, or `{{ … }}` split across two lines is not an error — Django doesn't
  recognize it and renders it onto the page as text for users to read. Use
  `{% comment %} … {% endcomment %}` for any note longer than one line. Enforced by
  `auctions/template_lint.py` (a pre-commit hook, part of `--ci`/`--lint`, and
  `auctions/test_template_hygiene.py`).

- Read `style_reference.md` before making any frontend/template/CSS change. It
  documents the palette, text-on-color rules, the six permitted button classes (no
  `btn-outline-*`, no `btn-warning`, and `btn-secondary` only on a Cancel or a Close), close
  buttons, hamburger menus, help notes, pagination, the unavailable-action ("stay clickable")
  standard, and the message-type taxonomy. Never edit vendor CSS; site-wide overrides go in
  `auctions/static/css/auction_site.css`. `docs/style_migration.md` is the worklist of files that
  don't conform to the button rules yet — take a few off it when you're in a template anyway.

## The three navigation surfaces

`base.html` draws the navbar and, beside the content, at most one sidebar: the **club** one
(`club_sidebar.html`, off `view.club_sidebar_can_view`) or the **Account setup** one
(`account_sidebar.html`, off `account_nav_active`). Both are a column at `>=lg` and one named
button opening an offcanvas below it; the club menu is `btn-info` because it is an admin surface
and the account menu is `btn-primary` because it is the reader's own.

The account menu replaced `preferences_ribbon.html`. Consequences worth knowing:

- **`auctions/account_nav.py` is the whole list**, and it is not just a menu: `active_page()` is
  what decides a page draws the sidebar at all, so a page dropped from `GROUPS` loses its
  navigation, not merely its highlight. In the app, which draws no navbar over these pages, that
  leaves no way out. `test_account_nav.SidebarReachTests` opens every page in the menu and fails if
  the sidebar isn't on it.
- The navbar's user menu is **three rows**: Invoices, Feedback, and **Account** — which is
  `/account/setup/`, a redirect to the account page you were last on (Contact info the first time).
  The visit is recorded by the `account_nav` context processor, in the session, on GETs only;
  `account_delete` is deliberately never remembered.
- The **app's drawer is the same three rows** (`auctions/mobile/menu.py`, section id `account` —
  the app merges Tap to Pay in by that id, so it stays whatever else changes). It used to be a flat
  list of every settings page; that would now be a second menu of the same pages with nothing
  keeping it in step with the first. `test_mobile_menu` requires every navbar link to reach the
  drawer.
- `Row.gate` is the only per-user condition, and only PayPal and Square carry one. Both gates moved
  from the ribbon unchanged — the Square one took a fix (`test_tap_to_pay`) that must not be lost.
- **`account` and `userpage` are the same page**, listed once as *Public user page*. The navbar's
  "Account information" and the ribbon's "My account" were one URL under two names. `userpage` is
  also the one page told apart by its argument — somebody else's profile is not your account page.
- No row is painted `text-danger`, Delete account included: one red row in a nav is the loudest
  thing in the menu, and the page itself is where the red belongs.

## Preferences and Notifications are two pages

`ChangeUserPreferencesForm` (`/preferences/`) is what the site *shows* you;
`ChangeUserNotificationsForm` (`/notifications/`) is when it may *contact* you. They partition the
`UserData` fields between them — no field on both — and that is load-bearing in three places:

- **It is why neither page has any JavaScript.** `distance_unit` stayed on `/preferences/` and the
  three radii went to `/notifications/`, so nothing on either page can change a unit that a field on
  the same page has to be converted against. Distances are stored in **miles** always; the
  notifications form converts once in `__init__` for display and once in `clean()` on the way back,
  reading the unit off the instance. Putting a unit control on the notifications page would bring
  the converter back.
- `palette_actions._preference_form_for` picks the form that owns a named setting, so
  `update_preferences` (one palette/MCP tool) still reaches every setting. Adding a field to
  neither form takes it off that tool.
- `command_palette._user_pref_field_items` searches both and links to the page the field is on.

Both views extend `OwnUserDataUpdate`, which lists **`SuccessMessageMixin` first** — written the
other way round (which is what they were) `UpdateView.form_valid` wins the MRO and the success
message never renders.
