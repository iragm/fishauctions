# Style reference

UI/color conventions for the auction site frontend. **Read this before making any
frontend/template change.** The site runs a self-hosted **Bootswatch Darkly
5.3.3** build (`auctions/static/css/vendor/bootstrap.min.css`) in dark mode
site-wide (`<html data-bs-theme="dark">`).

> **If templates or views are found that don't conform to this spec, either
> change them to conform or document a reason why they cannot.**

## Where things live

- **Never edit the vendor CSS** (`auctions/static/css/vendor/bootstrap.min.css`).
- Site-wide overrides go in **`auctions/static/css/auction_site.css`** (loaded
  after Bootstrap in `auctions/templates/base.html`).
- After editing CSS: `docker exec django python3 manage.py collectstatic --no-input`
  (use `-u root` if permissions complain).
- Some HTML is emitted from Python (`auctions/views.py`, `auctions/tables.py`,
  `auctions/forms.py`) — the same rules apply there.

### Why overrides are per-component, not just `:root`

Darkly is a *compiled* theme: component classes bake literal colors into their
own CSS variables (e.g. `.btn-danger{--bs-btn-bg:#e74c3c}`). Overriding
`:root{--bs-danger}` alone does **not** recolor `.btn-danger`, `.alert-danger`,
`.text-bg-danger`, etc. You must override the component variables too. All of
these overrides already exist in `auction_site.css`; extend that block if you
add new colored components.

## Palette

Dark theme; the base surface is near-black (`#222`), panels around `#303030`.

| Token | Value | Notes |
|---|---|---|
| Primary | `#375a7f` (link accent `#2fa4e7`) | unchanged |
| Success | `#00bc8c` | unchanged — **fills need dark text** (white is only ~2.5:1) |
| **Danger (fills)** | **`#a93226`** | darkened from `#e74c3c`, same hue; white text OK (~6.6:1) |
| **Danger (text)** | **`#e2756a`** | lighter tint used by `.text-danger` so red text stays legible on dark |
| **Warning (fills)** | **`#b9770e`** | darkened from `#f39c12`, same hue; **dark text** (~5.7:1, vs 3.7:1 white) |
| **Warning (text)** | **`#d99f3f`** | lighter tint used by `.text-warning` on dark |
| Secondary / gray | `#444`, `#6c757d`, `#adb5bd`, `#dee2e6` | pagination + outline buttons |

Danger/warning were darkened so they "don't pop as much." Fills (`.bg-*`,
`.btn-*`, `.text-bg-*`, badges) use the darker base; **text utilities**
(`.text-danger`, `.text-warning`) are decoupled to a lighter tint because the
darker fill color is too dark to read as text on the dark body. This mirrors
Bootstrap's own `-text-emphasis` pattern.

Derived hover/active shades (Bootstrap-style ~15/20/25% darker):

- Danger: hover `#902b20`, active `#87281e`, active-border `#7f261d`.
- Warning: hover `#9d650c`, active `#945f0b`, active-border `#8b590b`.

## Text on colored backgrounds (dark theme)

- **`bg-success` / `btn-success` → dark text.** White on `#00bc8c` fails AA.
- **`bg-warning` / `btn-warning` → dark text.** White on the darker `#b9770e`
  fails AA; black is ~5.7:1.
- **`bg-danger` / `btn-danger` → white text** (dark red, ~6.6:1).
- Standardize on **`text-dark`** (not `text-black`) for the dark-text class.

How this is enforced:

- `.btn-success`, `.btn-warning`, `.text-bg-success`, `.text-bg-warning` set the
  correct text color in `auction_site.css`, so the canonical component classes
  are always correct.
- The legacy utility pattern (`badge bg-success`, `btn bg-success`, …) sets a
  fill but no text color and would inherit the light body color. A **scoped**
  rule — `.badge.bg-success, .badge.bg-warning, .btn.bg-success, .btn.bg-warning
  { color:#000 }` — fixes those leaf components without the blunt
  `.bg-success{color}` rule that would cascade into nested content.
- In markup, still add `text-dark` when you write a new success/warning badge or
  banner, for clarity and so it's correct even outside a badge/btn.

The global toast helper (`base.html`) uses these types: `info`/`danger` →
white text, `success`/`warning` → dark text.

## Outline buttons

Darkly renders outline buttons with the *fill* color as their resting text
color. On the dark background `btn-outline-secondary` (`#444`) and
`btn-outline-dark` (`#303030`) are effectively invisible, and after darkening,
`btn-outline-danger` is hard to read. `auction_site.css` gives each a light
resting text color; hover/active still fill with the accent color:

- `btn-outline-secondary`, `btn-outline-dark`: light-gray text, gray border.
- `btn-outline-danger`: light-red text `#ec8b80`, fills `#a93226` on hover.
- `btn-outline-warning`: light-amber text `#d9a441`, fills `#b9770e` on hover.

Do **not** paper over this by adding `text-light` to individual templates — the
CSS handles it once. Only touch a template if it has a conflicting explicit text
class fighting the fix.

## Pagination

Darkly hardcodes success-green into `.pagination` vars, which leaks into the
django-tables2 htmx next/previous/page controls (they render `.page-link`
divs — see `auctions/templates/tables/bootstrap_htmx*.html`). `auction_site.css`
overrides `.pagination` to neutral grays: bg `#444`, hover `#5a5a5a`, active
`#6c757d`, disabled `#2b2b2b`/muted `#888`, white text. Fixed site-wide; no
per-table markup needed.

## Truncating text inside a flex column

`text-truncate` only clips when the element has a boundary narrower than its text.
Inside a **wrapping** flex container it doesn't get one: `.nav` sets
`flex-wrap: wrap`, and a multi-line column flex container sizes its line to the
widest item's *max-content* width, then stretches every item to that line — so a
`white-space: nowrap` child (which is what `text-truncate` makes it) grows to the
full text width and spills over the neighbouring content.

Fix the container, not the child: add **`flex-nowrap`** to the `.nav.flex-column`
(see `auctions/templates/club_sidebar_nav.html`). Then the line's cross size is the
container's width and every child, truncating or not, is clipped to it.

Things that look like they should fix this but don't: `min-width: 0` on the
column or on the truncating child (it isn't failing to shrink, it's stretching),
and `overflow-x: hidden` on an ancestor (hides the symptom, still cuts the text
mid-word with no ellipsis). `w-100` / `max-width: 100%` on the child does work,
but only for that one child.

## Filter controls: search box, then dropdowns

A row of filter controls that reads fine at 1200px falls apart at 390px — inline
`w-auto` selects size to their content, and the row wraps into a ragged stack of
mismatched heights. The fix is not a separate mobile layout. It's using controls
whose size doesn't depend on how much is inside them:

1. **The search box gets a full-width line of its own**, above everything else.
2. **Every other filter is a dropdown** on the line below it, in the same shape as
   `partials/htmx_table_filters.html` (`btn-sm dropdown-toggle` + `dropdown-menu`
   of `<label class="dropdown-item">` rows). A dropdown button is the same size
   whether it holds four options or forty, so nothing has to be rearranged for a
   phone. Cap tall menus with `dropdown-menu-scroll`.
3. **No apply button, no result count, no "clear all".** These tables filter as
   you type; a filter is cleared by its own "Any …" option or by unticking it.

Reference implementation: `partials/speaker_table_header.html`. Its topic menu is
radios (single-select) beside the shared keyword checkboxes (multi-select); the
htmx attributes sit on the `dropdown-menu` rather than on each of the ~30 radios,
since `change` bubbles.

Two things that follow from it:

- **Don't add a control for something rare.** The speaker list takes a radius as
  words in the search box (`within 50 miles`, parsed by `SpeakerFilter`) and hints
  at it in the placeholder, instead of spending a permanent dropdown on it.
- **Not every keyword needs a menu row.** `photo`, `mapped` and `myclub` still
  work typed into the box; they're just not worth a row everyone has to read past.

Put the view-switching control (list/map, table/tiles) **below** the filters and
directly above the results, not up beside the heading where it gets missed.

## Autocomplete pickers (select2)

Every select2 box on the site — django-autocomplete-light's and the three pages
that start select2 themselves — is sized and painted by one block in
`auction_site.css`, so a picker is the same 38px box as the `.form-control` above
it. Two things to know before touching one:

- **dal copies the field's classes onto `.select2-selection`**, the visible box,
  because it starts select2 with `containerCssClass: ':all:'` (the option's name
  is a select2 3.x leftover; it decorates the *selection* adapter). Crispy's
  bootstrap5 field template puts `form-select` on any `Select` widget, so the box
  arrives wearing Bootstrap's padding and caret on top of select2's — two sets of
  padding, two carets, text 25px in where every other field's is 13px. The block
  strips Bootstrap's padding and caret image back off; don't re-add either.
- **Don't load a select2 skin on a page.** The Bootstrap 3 skin
  (`select2-bootstrap.min.css`) sizes to a 5.3 theme, and any per-page stylesheet
  loads after `auction_site.css`. The site rules are prefixed with `body` so they
  still win, but the page ends up fighting itself for no gain.

## Unavailable actions stay clickable

**Don't hide or disable a button when its action is currently unavailable.** Keep
it clickable and, on click, show a clear message (toast/alert) explaining why and
what to do next. A `disabled` button — especially one whose only explanation is a
tooltip/popover — leaves users stuck with no feedback (Bootstrap 5 does not even
auto-initialize `data-bs-toggle="popover"`, so those explanations often never
render).

**Exception:** features that are exclusive to one auction type (in-person vs
online) may stay hidden in the other type.

**Exception: app-only actions** (`fishauctions://` deep links — native printing,
lot scanning, Tap to Pay). The scheme has no handler in a browser, so the button is
dead rather than merely unavailable. Judge it by the page:

- A page people use *on desktop* keeps the button clickable and toasts "this
  lives in the app" — hiding it there reads as "the feature doesn't exist". See
  the "Find this lot" button in `auction_lot_map.html`.
- A page that is only useful in the app, or a long public list where the button
  would be pure noise on web, gates on `request.is_mobile_app` (lot page's
  "Find this lot", `auction.html`'s "Scan lots", the lot-list scanning button in
  `lot_tile_page.html` / `lot_list_page.html`, Bluetooth label printing, and the
  command palette's "Lot scanning" / "Tap to Pay" rows in `command_palette.py`).

Reference implementation: the "Payment not available" button in
`auctions/templates/invoice.html` — a plain `btn btn-secondary` (no `disabled`)
that fires an `info` toast with `invoice.reason_for_payment_not_available` on
click.

```html
<button type="button" class="btn btn-secondary" id="payment-not-available"
        data-reason="{{ invoice.reason_for_payment_not_available }}">Pay now</button>
<script>
  document.getElementById('payment-not-available').addEventListener('click', function () {
    window.jQuery.toast({ title: 'Payment not available',
      content: this.getAttribute('data-reason'), type: 'info', delay: 8000 });
  });
</script>
```

This is the documented standard; apply it to obvious cases as you touch pages,
rather than refactoring every page at once.

## Confirmation dialogs

**Never use the browser's `confirm()` (or `alert()` / `prompt()`) to confirm an
action.** Use a styled modal with explicit yes/no buttons.

A native `confirm()` is unstyleable, is prefixed with the bare origin
("127.0.0.1 says…"), carries no product voice, is silently suppressed once the
user ticks *prevent this page from creating additional dialogs* — which
**fires the action as if cancelled, or blocks it entirely, with no feedback** —
and inside the mobile app's webview it reads like a phishing prompt.

Use the declarative helper (`auctions/static/js/confirm_modal.js`, loaded
site-wide from `base.html`). No per-page JavaScript:

```html
<form method="post" action="{% url 'google_calendar_disconnect' club.slug %}"
      data-confirm="Disconnect Google Calendar? The calendar stays in your Google account, but syncing will stop."
      data-confirm-title="Disconnect Google Calendar?"
      data-confirm-ok="Disconnect"
      data-confirm-variant="danger">
  {% csrf_token %}
  <button type="submit" class="btn btn-outline-danger btn-sm">Disconnect</button>
</form>
```

| Attribute | Purpose |
|---|---|
| `data-confirm` | **Required.** The question. Say what will happen, including what *won't* be undone. |
| `data-confirm-title` | Dialog heading. Default "Are you sure?" |
| `data-confirm-ok` | Confirm button label. Default "Yes". |
| `data-confirm-cancel` | Cancel button label. Default "Cancel". |
| `data-confirm-variant` | `danger` / `success` / `primary` — picks the confirm button style per the Actions table below. Default `primary`. |

Works on a `<form>` (intercepts submit, then re-submits via `requestSubmit()` so
HTMX and native validation still run) or on an `<a>`/`<button>`. For flows that
need to confirm from inside your own JavaScript, call
`confirmAction({message, title, okLabel, variant}, onConfirm)` directly.

Rules:

- **Name the verb on the confirm button** — "Delete event", "Disconnect", not
  "OK". The button should read as the thing about to happen.
- **`danger` variant for anything destructive or irreversible**, and say so in
  the message.
- The message is rendered with `textContent`, so it can never inject markup —
  but that also means **no HTML in `data-confirm`**.
- Confirmation is not a substitute for reversibility. Prefer a soft delete plus
  an undo where the data is worth keeping.

## Message-type standard

Use the right channel for the right kind of message, consistently.

| Kind | Style | Icon |
|---|---|---|
| **Information** (neutral fact) | `alert-info` or `text-muted` | `bi-info-circle` |
| **Error** (failed / blocking) | `alert-danger`, toast type `danger` | `bi-exclamation-triangle-fill` |
| **Warning** (caution, non-blocking) | `alert-warning text-dark`, toast type `warning` | `bi-exclamation-triangle` |
| **"Do this first" / setup guidance** | `bg-primary bg-gradient bg-opacity-50` banner | (heading + steps) |

The canonical **"do this first"** banner is the *Finish setting up* checklist in
`auctions/templates/auction_ribbon.html`:

```html
<div class="mt-4 mb-4 p-2 bg-primary text-white rounded bg-gradient bg-opacity-50">
  <h5>Finish setting up</h5>
  <ul class="list-unstyled"> … steps with bi-check-square-fill / bi-exclamation-square-fill … </ul>
</div>
```

All "enable payments / promote this auction / set up X" guidance banners use this
same `bg-primary` pattern (the Square-payments banner was migrated from
`bg-success` to conform).

### Actions (buttons)

| Intent | Class |
|---|---|
| Primary action | `btn-primary` |
| Secondary / neutral | `btn-secondary` |
| Destructive (confirm before irreversible) | `btn-danger` |
| Confirm / complete (save, sold, join) | `btn-success text-dark` |

Tooltips (`data-bs-toggle="tooltip"`, `title=`) are **supplementary only** —
never the sole carrier of critical instructions. If a control's only explanation
is a tooltip, surface it as visible text or an on-click message as well.

## Contrast target

Aim for WCAG AA (~4.5:1 body text, ~3:1 large/bold). The values above were chosen
against the dark surfaces; when picking a new color pairing, keep to that bar.
