# Style reference

Read before any frontend/template change. Self-hosted Bootswatch Darkly 5.3.3
(`auctions/static/css/vendor/bootstrap.min.css`), dark mode site-wide (`<html data-bs-theme="dark">`).

## Where things live

- **Never edit vendor CSS.** Site-wide overrides go in **`auctions/static/css/auction_site.css`**
  (loaded after Bootstrap in `base.html`), per-component (`.btn-danger`, `.alert-danger`), not just
  `:root` variables — extend the existing block for a color rather than adding one elsewhere.
- After editing CSS: `docker exec django python3 manage.py collectstatic --no-input` (`-u root` if
  permissions complain).
- HTML emitted from Python (`auctions/views/`, `auctions/tables.py`, `auctions/forms.py`) follows the
  same rules as templates. `docs/style_migration.md` has three greps that catch forbidden classes —
  run before committing.

## Palette

| Token | Value | Notes |
|---|---|---|
| Body / panel | `#222` / `#303030` | base surface / cards |
| Primary | `#375a7f` (link `#2fa4e7`) | |
| Success | `#00bc8c` | fills need dark text (white is ~2.5:1) |
| Danger fill / text | `#a93226` / `#e2756a` | fill = white text; `.text-danger` = the lighter tint |
| Warning fill / text | `#b9770e` / `#d99f3f` | fill = dark text (5.7:1 vs 3.7:1 white); `.text-warning` = lighter tint |
| Info (alert only) | `#3498db` | white text, 3.1:1 — large/bold only |
| Gray | `#444`, `#6c757d`, `#adb5bd`, `#dee2e6` | Cancel/Close, pagination |

Derived: danger hover `#902b20`, active `#87281e`/border `#7f261d`; warning hover `#9d650c`, active
`#945f0b`/border `#8b590b`.

## Text on colored backgrounds

`bg-success`/`btn-success` and `bg-warning`/`btn-warning` → **`text-dark`** (never `text-black`).
`bg-danger`/`btn-danger` → white text. Canonical classes get this from `auction_site.css`
automatically; the legacy pattern (`badge bg-success`, `btn bg-success`) does not, so add `text-dark`
by hand. Toast helper (`base.html`): `info`/`danger` white, `success`/`warning` dark.

## Alerts

Solid brand fills, not Bootstrap 5.3's tinted style — `background-color`/`color` set outright per
variant in `auction_site.css` (`--bs-alert-bg` overrides do nothing against Darkly's plain rules).

| Variant | Fill | Text |
|---|---|---|
| `alert-primary` | `#375a7f` | white |
| `alert-secondary` | `#444` | white |
| `alert-danger` | `#a93226` | white |
| `alert-warning` | `#b9770e` | black |
| `alert-success` | `#00bc8c` | black |
| `alert-info` | `#3498db` | white |

Links inside `alert-warning`/`alert-success` are forced black already; `.alert.text-dark` resolves to
`#000` (not the usual `#303030`), so `alert-warning text-dark` markup is correct as written. A
`btn-close` on the two dark-text variants needs the inverted-glyph exception below.

## Component gotchas

| Component | Rule |
|---|---|
| `.table` | Darkly ties table bg to body bg (looks like a hole in a card). Fixed site-wide (`--bs-table-bg:transparent`). Don't add a background in markup; don't use `table-dark`. |
| `.pagination` (incl. django-tables2 htmx controls) | Darkly's green is overridden site-wide to neutral grays (bg `#444`, hover `#5a5a5a`, active `#6c757d`, disabled bg `#2b2b2b`/text `#888`). No per-table markup needed. |
| `text-truncate` in a wrapping flex column (`.nav.flex-column`) | Won't clip without **`flex-nowrap`** on the container (see `club_sidebar_nav.html`). `min-width:0` and `overflow-x:hidden` do not fix it. |
| select2 pickers | Sizing/color fixed in one `body`-prefixed block. Don't load a select2 skin/stylesheet on any page; don't re-add Bootstrap padding/caret to `.select2-selection`; don't set picker text colors per-page. |

## Filter controls

Search box full-width on its own line; every other filter is a dropdown below it (`btn-sm
dropdown-toggle` + `dropdown-menu` of `<label class="dropdown-item">` rows, per
`auctions/partials/htmx_table_filters.html`; cap tall menus with `dropdown-menu-scroll`). No apply
button, no result count, no "clear all" — filters as you type. Fold a rare filter into search-box
keyword parsing instead of a dedicated dropdown (see `SpeakerFilter` in `auctions/filters.py`).
View-switch controls (list/map, table/tiles) sit directly above the results, not beside the heading.

## Unavailable actions stay clickable

**Never hide or disable a button for an unavailable action** — keep it clickable and toast/alert why
on click. Exceptions: a feature exclusive to one auction type (in-person/online) may be hidden in the
other. `fishauctions://` deep links have no browser handler — a desktop-usable page keeps the button
and toasts "this lives in the app" (e.g. `auction_lot_map.html`); an app-only or noisy-on-web page
gates on `request.is_mobile_app` instead (e.g. Bluetooth label printing, invoice-page Tap to Pay,
where web already has working Square/PayPal buttons so no toast is needed).

## Confirmation dialogs

**Never use `confirm()`/`alert()`/`prompt()`.** Use `data-*` attributes on a `<form>`/`<a>`/`<button>`
via the site-wide helper (`auctions/static/js/confirm_modal.js`); from custom JS call
`confirmAction({message, title, okLabel, variant}, onConfirm)`.

| Attribute | Purpose |
|---|---|
| `data-confirm` | **Required.** The question — say what won't be undone. Renders as `textContent`; no HTML. |
| `data-confirm-title` | Dialog heading. Default "Are you sure?" |
| `data-confirm-ok` / `data-confirm-cancel` | Button labels. Default "Yes" / "Cancel". |
| `data-confirm-variant` | `danger`/`success`/`primary`. Default `primary`. |

Name the verb on the confirm button ("Delete event", not "OK"); `danger` variant for anything
destructive/irreversible. Confirmation is not a substitute for reversibility — prefer soft delete +
undo.

## Message-type standard

| Kind | Style | Icon |
|---|---|---|
| Standing "what this page is for" | `help-note` (below) | `bi-lightbulb-fill` |
| Neutral fact, one-off | `alert-info` or `text-muted` | `bi-info-circle` |
| Error (failed/blocking) | `alert-danger`, toast `danger` | `bi-exclamation-triangle-fill` |
| Warning (non-blocking) | `alert-warning text-dark`, toast `warning` | `bi-exclamation-triangle` |
| "Do this first" setup guidance | `bg-primary bg-gradient bg-opacity-50` banner | heading + steps |

A help note is true every page load; `alert-info` is true *right now* — don't put standing text in an
alert box.

### Buttons — six classes, nothing else

| Intent | Class |
|---|---|
| Almost every button | `btn-primary` |
| Auction/club-admin only, invisible to ordinary users | `btn-info` |
| Deleting or destroying something | `btn-danger` |
| Saving a form, or a handful of pivotal actions | `btn-success text-dark` |
| Backing out — Cancel, Close | `btn-secondary` |
| A link that must not look like a button | `btn-link`, or plain text |

- **No `btn-outline-*`, ever** — use the filled button of the same intent (a legibility-only CSS fix
  for pasted-in outline classes exists, it's not license to write new ones). No `btn-warning` —
  warnings go in `alert-warning`, not a button.
- `btn-secondary` is *only* for backing out (Cancel, Close, "Back to X"); if the click doesn't leave a
  dialog/form/page, it isn't `btn-secondary`. A button that cancels a *thing* (a job, a pending
  change) keeps that action's color (usually `btn-danger`) even if labeled "Cancel" — test: does
  walking away leave the thing cancelled?
- `btn-success` is rationed to form-save and the page's one pivotal action (join, bid, buy,
  pay/renew), always with `text-dark`. Two green buttons on one screen means one is wrong.
- `btn-danger` = destruction (delete/remove/refund/ban/revoke), not "this is important".
- Selected/unselected toggle pair (feedback ratings, List/Map switch): unselected `btn-secondary`,
  selected `btn-primary active`. If the current value is omitted rather than marked, every option is
  an action — all `btn-primary`.
- `btn-info` marks the admin half of a mixed member/admin page; admin-only pages just use
  `btn-primary`. `btn-sm` is for a control row above a table/heading, not a page's main action.

### Close buttons

Always **`btn-close btn-close-white`** (modal, offcanvas, alert, toast) — never plain `btn-close`,
never `btn-close text-reset bg-light`. Don't add your own `filter` to `.btn-close` (site CSS already
disables Bootstrap's dark-mode double-inversion). If a dismiss needs to be discoverable rather than
merely present, use a `btn-secondary` "Close" button instead of a bare `btn-close`.

### Hamburger menus

**Only one hamburger icon on the site:** the main site menu, top right. Every other collapsed
menu/offcanvas trigger gets a name plus icon (e.g. "`{{ club.name }}` menu") — the name may hide below
a breakpoint only if the icon alone is self-explanatory (a funnel for filters).

### Help notes

Standing explanatory text goes in `.help-note` (`auction_site.css`: light-blue tint, left rule, no
full border): `<div class="help-note"><i class="bi bi-lightbulb-fill"></i><div>Text.</div></div>`. No
quota either way — test each one by whether the reader would get something wrong if it were missing.
A field-specific fact needed while filling that field belongs in the field's own `help_text`, not a
note.

### Page headings

A page whose name is already shown elsewhere (sidebar, ribbon, tab title) doesn't repeat it in an
`<h1>`–`<h3>`; add one only when the page has no nav entry of its own or needs its sections labeled.
The page's one action sits on the same row as its heading, right-aligned
(`d-flex justify-content-between align-items-center`), not on its own line below.

## Contrast target

WCAG AA: ~4.5:1 body text, ~3:1 large/bold. Hold new color pairings to that bar.
