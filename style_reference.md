# Style reference

UI/color conventions for the auction site frontend. **Read this before making any
frontend/template change.** The site runs a self-hosted **Bootswatch Darkly
5.3.3** build (`auctions/static/css/vendor/bootstrap.min.css`) in dark mode
site-wide (`<html data-bs-theme="dark">`).

> **If templates or views are found that don't conform to this spec, either
> change them to conform or document a reason why they cannot.**

The button rules below were tightened after most of the site was written; the templates have now
been brought into line with them. `docs/style_migration.md` records what each forbidden class
became and carries the three greps that keep it that way — run them before you commit a template.

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
| Secondary / gray | `#444`, `#6c757d`, `#adb5bd`, `#dee2e6` | Cancel/Close buttons + pagination |

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

### Alerts are solid fills, and a variable override does not reach them

An alert on this site is a **solid brand-coloured block**, not Bootstrap 5.3's
subtle tinted one. Darkly writes `.alert{color:#fff;border:none}` and one
`.alert-warning{background-color:#f39c12}` per variant — plain declarations, far
below the rule that reads `--bs-alert-bg`. So overriding `--bs-alert-bg` /
`--bs-alert-color` in `auction_site.css`, which is what it used to do, changed
nothing at all: every alert stayed Darkly's bright fill under white text, at
2.2:1 for warning and 2.5:1 for success. **Set `background-color` and `color`
outright on `.alert-*`.**

| Variant | Fill | Text |
|---|---|---|
| `alert-primary` | `#375a7f` | white (7.2:1) |
| `alert-secondary` | `#444` | white (9.7:1) |
| `alert-danger` | `#a93226` (the darkened fill) | white (6.6:1) |
| `alert-warning` | `#b9770e` (the darkened fill) | **black** (5.7:1) |
| `alert-success` | `#00bc8c` | **black** (8.6:1) |
| `alert-info` | `#3498db` | white (3.1:1 — passes for large/bold only) |

Two consequences, both handled in `auction_site.css`:

- Darkly also makes every link inside an alert white
  (`.alert .alert-link,.alert a`), which is the same problem one level down and
  usually lands on the sentence that says what to do about the alert. The
  dark-text variants get dark links.
- `.text-dark` is `#303030`, not black, and on the darkened warning fill that is
  3.6:1 where black is 5.7:1 — so writing the documented `alert-warning
  text-dark` would come out *worse* than the alert's own colour. Inside an alert
  the utility is resolved to black (`.alert.text-dark`), so the markup the
  message-type standard asks for is the markup that is right.
- A dismiss button on a light fill needs the dark glyph, the same exception the
  success/warning toast headers take. See "Close buttons".

## Outline buttons are not used

**Do not write `btn-outline-*`.** On the near-black body an outline button is a
rectangle of thin border with low-contrast text in it, and next to a filled
button it reads as disabled rather than as secondary. Every outline button is
replaced by the filled button of the intent it was expressing — usually
`btn-primary`, `btn-danger` for a delete.

There are none left in the templates or in the HTML emitted from Python. The
`auction_site.css` block that gives `btn-outline-secondary`, `btn-outline-dark`,
`btn-outline-danger` and `btn-outline-warning` a readable resting text color
stays as a legibility floor in case one is pasted in from somewhere — it is
**not** a licence to write new ones. Don't paper over the color in a template
either; the CSS handles it once.

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
- **The default theme sets no text colour on a multi-select**, and that is the
  one thing dropping the skin cost. select2 paints the box white and the picked
  chips `#e4e4e4` but colours neither, so both inherited Darkly's `#dee2e6` —
  1.1:1, and the categories on `/ignore/` were invisible rather than missing.
  The same is true of both boxes you can type into (`--inline` inside a
  multi-select and `--dropdown` at the top of an open picker). Those colours are
  in the same `auction_site.css` block as the sizing now; don't put them back in
  a page.

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
`auctions/templates/invoice.html` — a plain `btn btn-primary` (no `disabled`)
that fires an `info` toast with `invoice.reason_for_payment_not_available` on
click.

```html
<button type="button" class="btn btn-primary" id="payment-not-available"
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
  <button type="submit" class="btn btn-danger btn-sm">Disconnect</button>
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
| **What this page is for** (standing explanation) | `help-note` — see below | `bi-lightbulb-fill` |
| **Information** (neutral fact, one-off) | `alert-info` or `text-muted` | `bi-info-circle` |
| **Error** (failed / blocking) | `alert-danger`, toast type `danger` | `bi-exclamation-triangle-fill` |
| **Warning** (caution, non-blocking) | `alert-warning text-dark`, toast type `warning` | `bi-exclamation-triangle` |
| **"Do this first" / setup guidance** | `bg-primary bg-gradient bg-opacity-50` banner | (heading + steps) |

The first two rows are the pair most often confused. A **help note** is the
sentence that is true every time the page loads ("this is a list of the people in
your auction"); an **alert-info** is something that is true *right now* ("bidding
is ending soon"). Standing text in an alert box trains people to skip alert boxes.

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

Six classes, and nothing else. The point is that a colour means the same thing
on every page: green is not "this button matters to me", it is "this is the one
that completes the thing you came here to do".

| Intent | Class |
|---|---|
| Almost every button | `btn-primary` |
| Auction-admin or club-admin only, invisible to ordinary users | `btn-info` |
| Deleting or destroying something | `btn-danger` |
| Saving a form, and a handful of pivotal actions | `btn-success text-dark` |
| **Backing out — Cancel, Close** | **`btn-secondary`** |
| A link that must not look like a button | `btn-link`, or plain text |

Rules that follow from the table, all of them enforceable by grep:

- **No `btn-outline-*`.** See "Outline buttons are not used" above.
- **`btn-secondary` is for backing out, and only for that.** A **Cancel** or a
  **Close** is `btn-secondary`, every time — it is `#444`, the one grey on the
  page, and that is exactly right for the button somebody presses when they
  decided *not* to do the thing. The dismissal must not compete with the action
  it sits beside: a Cancel in `btn-primary` next to a Save is two buttons of
  equal weight and a coin toss.
  A **"Back to X"** link at the top or bottom of a sub-page is the same gesture
  as Cancel — leaving without doing the thing — and is `btn-secondary` too.
  It is **not** the old "a button I don't want to think about" default. If it
  isn't leaving the dialog, abandoning the form or backing out of the page, it
  isn't `btn-secondary` — whatever else it was saying, say it with `btn-primary`.
- **"Cancel" the verb is not "Cancel" the exit.** A button that cancels a
  *thing* — a volunteer job, a pending email change, an integration — is that
  action and takes that action's colour (usually `btn-danger`), even though the
  word on it is Cancel. The test is what happens if the user walks away instead:
  if nothing happens, it's an exit; if the thing stays cancelled, it's a verb.
- **No `btn-warning`.** Amber on a control means "careful" and almost nothing
  ever used it that way; edit buttons wore it because they felt risky. Editing is
  a primary action. Warnings belong in `alert-warning`, not on a button.
- **`btn-success` is rationed.** Form-save buttons, and the handful of actions a
  page exists for: *join this auction*, *place bid*, *buy now*, *pay / renew
  membership*. Two green buttons on one screen means one of them is wrong. It
  always carries `text-dark` (white on `#00bc8c` fails AA).
- **`btn-danger` is for destruction, not for danger.** Delete, remove, refund,
  ban, revoke. Not for "this is important".
- **A selected/unselected pair is grey and blue.** In a `btn-group` that shows
  which of several options is currently chosen — the feedback ratings, the
  speaker List/Map switch, the invoice Open/Ready/Paid group, the speaker tags —
  the unselected options are `btn-secondary` and the selected one is
  `btn-primary active`. This is the one place grey does not mean "back out": an
  option you have not picked is not something the page is asking you to do, and
  the contrast between grey and blue is what makes the current state readable at
  a glance. All-blue-with-`active` is not enough — the difference between
  `btn-primary` and `btn-primary.active` is a few percent of lightness.
  A group whose *current* value is hidden rather than marked (the club contact
  preference page drops the option you are already on) is not a pair at all:
  every button there is an action, so every button is `btn-primary`.
- **`btn-info` marks the admin half of a page.** On a page members and admins
  both see, it is what separates "the thing you came for" from "the thing you can
  do because you run this". A page only admins can reach doesn't need it —
  everything there is admin, so `btn-primary` is right.

`btn-sm` is orthogonal to all of this: a row of controls above a table or beside
a heading is `btn-sm`, a page's main action is not.

### Close buttons

Write **`btn-close btn-close-white`** on every modal, offcanvas, alert and toast
on the site. Since the whole site is `data-bs-theme="dark"`, there is no case
where the plain one is correct — and never the `btn-close text-reset bg-light`
workaround, which is a light square with a dark X in it that reads as a
rendering bug.

**The class alone was not enough, and this is the part worth knowing.** Darkly
compiles `.btn-close` with an already-*white* glyph (`fill='%23fff'`), and
Bootstrap 5.3 then adds

```css
[data-bs-theme=dark] .btn-close { filter: invert(1) grayscale(100%) brightness(200%) }
```

on top of it — inverting that white X to **black on a near-black panel**. That
rule also outranks `.btn-close-white` on specificity (`[attr] .class` beats
`.class`), so writing the documented class could not win either: for a long time
*every* dismiss control on the site was black-on-black, the club sidebar's
offcanvas included. `auction_site.css` now turns the inversion back off
(`filter: none`) and restores the resting opacity, which Darkly sets to 0.4.

The lesson generalises: when a Bootswatch theme has already recolored a
component for dark, Bootstrap's own `[data-bs-theme=dark]` rules recolor it a
second time. Check for a double inversion before adding a utility class on top.

If a dismiss control needs to be discoverable rather than merely present (an
offcanvas somebody opened by accident, a panel with no obvious way out), don't
use `btn-close` at all — use a real button that says `Close`, in
`btn-secondary` like every other way out.

### Hamburger menus

**There is exactly one hamburger on the site: the main site menu, top right.**
That icon means "the navigation for this whole site" and nothing else. A second
bare `☰` on the same page is a mystery box — people don't open it, and the
feature behind it may as well not exist.

Every other collapsed menu gets **a name and an icon**: `Filter lots`,
`{{ club.name }} menu`, `Admin actions`, `Export`. Judge it by whether a member
who has never used the page can tell what is inside without clicking.

```html
<!-- no -->
<button class="btn btn-primary" data-bs-toggle="offcanvas" data-bs-target="#x">
  <i class="bi bi-list"></i>
</button>

<!-- yes -->
<button class="btn btn-primary btn-sm" data-bs-toggle="offcanvas" data-bs-target="#x">
  <i class="bi bi-list"></i> {{ club.name }} menu
</button>
```

The name may be hidden below a breakpoint only when the icon is genuinely
self-explanatory on its own (a funnel for filters); the site menu is the only
control allowed to be an icon at every width.

### Help notes

Pages that explain themselves — "This is a list of items for sale in your
auction. Click a lot name to edit it." — put that sentence in a **help note**:
a light-blue tinted block with a lightbulb, small text, a rule down the left
edge and **no full border**. It is guidance, not an alert; a boxed
`alert-info` at the top of every admin page becomes wallpaper and stops being
read at all.

```html
<div class="help-note">
  <i class="bi bi-lightbulb-fill"></i>
  <div>This is a list of people in your auction. Click on a name to edit that user.</div>
</div>
```

`.help-note` lives in `auction_site.css`. It is a flex row, so the icon stays
put while the text wraps; put the text in the `<div>`, not as a bare sibling of
the icon.

When to use which:

| Kind | Use |
|---|---|
| What this page is for, how to use it | `help-note` |
| Something is misconfigured and needs fixing | `alert-warning text-dark` |
| Something failed | `alert-danger` |
| A one-off neutral fact in the middle of a page | `text-muted` |
| "Do this first" setup checklist | `bg-primary bg-gradient bg-opacity-50` banner |

Keep it to a sentence or three. If a page needs more explanation than that, the
explanation belongs in the FAQ or a blog post with a link from the note.

**One help note per page.** Not one per section, and not one per control — a
page with a note above the form, a note under the table and a paragraph beside
each checkbox has said everything and communicated nothing, and the reader skips
all of it. Ask what the reader would get wrong if the sentence were missing; if
the answer is "nothing", cut it. A field's own `help_text` is the right place for
a fact the reader needs *while filling that field in* ("12 of your 143 members
have the app"), and the wrong place for how the feature works.

### Page headings

**A page whose name is already on screen does not repeat it in an `<h1>`–`<h3>`.**
Club pages are the case that keeps drifting: the club sidebar names every page and
marks the current one active, the ribbon carries the club name, and the browser tab
carries the title, so an `<h3>Setup</h3>` under a highlighted "Setup" link is the
word twice.

Do add one when the page is *not* named elsewhere — a Setup sub-page, a form
reached from a button, anything with no sidebar entry of its own — and when a long
page needs its sections labelled.

**The page's one action goes on the same row as its heading, on the right**, not on
a line of its own underneath:

```html
<div class="d-flex flex-wrap gap-2 align-items-center justify-content-between mb-2">
  <h3 class="mb-0">Speakers</h3>
  <a class="btn btn-sm btn-info" href="…"><i class="bi bi-person-plus-fill"></i> Add a speaker</a>
</div>
```

On a page with no heading, the left of that row is whatever the *reader* came for
(subscribe links, filters) and the right is the admin action. Never the other way
round: a member looking at a club's events should not find the subscribe buttons
pushed to the right edge to balance an "Add event" button they cannot see.

## Contrast target

Aim for WCAG AA (~4.5:1 body text, ~3:1 large/bold). The values above were chosen
against the dark surfaces; when picking a new color pairing, keep to that bar.
