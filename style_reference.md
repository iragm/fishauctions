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

## Unavailable actions stay clickable

**Don't hide or disable a button when its action is currently unavailable.** Keep
it clickable and, on click, show a clear message (toast/alert) explaining why and
what to do next. A `disabled` button — especially one whose only explanation is a
tooltip/popover — leaves users stuck with no feedback (Bootstrap 5 does not even
auto-initialize `data-bs-toggle="popover"`, so those explanations often never
render).

**Exception:** features that are exclusive to one auction type (in-person vs
online) may stay hidden in the other type.

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
