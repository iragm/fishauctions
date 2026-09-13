# What the mobile app has to change about printing

The app's source is not in this repo, so this is the half of the printing work that cannot be done
here. Everything below is already live on the server side and tested
(`auctions/test_remote_print.py`, `auctions/test_mobile_features.py`); the app is what is missing.

Written 2026-09-11, from four things reported from a real auction: printing was ~5 seconds a label,
enabling "print to my phone" from a computer threw a form error the first time and "worked" the
second, a computer print with the app closed silently produced a PDF download instead, and a paper
jam was reported to the user as a successful print.

Two of the four were entirely server-side and are fixed; they are recorded at the bottom so nobody
looks for an app change that isn't needed. The two here are real app work.

---

## 1. Print a run through the batch endpoint, not one label at a time

**This is the 5-seconds-a-label fix, and it is the big one.**

The cost is not the render. Measured on this codebase a label is about 4 ms of template, 95 ms of
WeasyPrint and 10 ms of pdfium — roughly 110 ms, and now cached, so a reprint is a cache read. The
five seconds is **forty round trips**: TLS, auth, throttling and scheduling, each one crossing an
auction hall's wifi while somebody stands at a table waiting.

### The endpoint

`POST /api/mobile/labels/batch/`

```json
{ "lots": [123, 124, 125], "resolution": "600x400", "dpi": 203 }
```

`lots` is the run **in print order** — the server de-duplicates it and preserves that order.
`resolution` and `dpi` mean exactly what they mean on `GET /api/mobile/labels/<pk>/` and default the
same way (`600x400` at 203 dpi). Auth, permissions and the `mobile_api` throttle scope are unchanged.

Response:

```json
{
  "labels":    [{"lot": 123, "content_type": "image/png", "png": "<base64>"}],
  "remaining": [124, 125],
  "skipped":   [{"lot": 999, "detail": "Lot not found."}],
  "resolution": "600x400",
  "dpi": 203
}
```

### The loop the app must implement

**The response is deliberately not "all of them".** The server renders until it hits either 25
labels or a 3-second budget, whichever comes first, and hands the rest back in `remaining`. So:

> POST what is left → print what comes back → repeat with `remaining` until it is empty.

That is not a limitation to work around, it is the point: a run of three hundred labels starts
printing after the first chunk rather than after the last, and a server having a slow moment returns
a *smaller* chunk on its own instead of making the phone wait on a minute-long response it cannot
begin to use. An app that instead loops one pk at a time through `labels/batch/` gets none of the
benefit and should keep using `labels/<pk>/`, which still exists and is the right call for a single
reprint.

`skipped` is per-lot and never fails the batch — one bad pk in a run of forty must not cost the
other thirty-nine. Show it or log it; do not abort on it.

**Nothing in this endpoint marks anything printed.** That is still `POST /api/mobile/labels/printed/`
and it is still the app's call, because only the app knows what physically came out.

---

## 2. Report what did *not* print

**This is the paper-jam fix.** The server genuinely cannot see the printer — it is a Bluetooth
device paired to the phone, and nothing about its state reaches this codebase except through the
app. So rather than invent a check the server cannot make, the missing **reporting channel** now
exists and the app has to fill it in.

`POST /api/mobile/labels/printed/` takes three new optional fields:

```json
{
  "lots":       [123, 124],
  "failed":     [125],
  "conditions": ["paper_jam"],
  "message":    "Printer reported a jam after label 3"
}
```

- **`failed`** — lots that were *sent* and did not come out. A lot named here is put back to
  unprinted and flagged for reprinting, so clearing the jam and pressing **print unprinted labels**
  prints exactly what is missing and nothing else. This is the behaviour the reporter wanted and
  could not get: they had to reprint by hand and guess where the run stopped.
- **`conditions`** — machine-readable printer state. It is validated against
  `auctions.printer_programs.STATUS_CONDITIONS`, which is the same vocabulary the printer profiles'
  `status_flags` already decode into, so the profile that says "byte 02 means `paper_jam`" and the
  report that says `paper_jam` cannot drift apart. An unknown value is a 400 listing the known ones.
  The full set today: `cover_open`, `out_of_paper`, `paper_jam`, `no_ribbon`, `overheated`,
  `low_battery`, `printing`, `paused`, `error`.
- **`message`** — the app's own words, kept verbatim, the same way a remote-print job's message is.

All three are optional, because a printer with no status program has nothing to say. An app that
sends only `lots` behaves exactly as before.

**What the app has to do beyond sending this:** stop reporting success unconditionally. Read the
printer's status after a run where the profile has a status program, and put whatever it could not
print into `failed`. If the printer cannot be queried at all, send nothing extra — that is honest.
What must not continue is the current behaviour, which is to tell the user it worked because the
bytes were written to the socket.

---

## Fixed on the server; no app change needed

Recorded so nobody goes looking.

**The form error when enabling "print to your phone" from a computer.** The cause was HTML, not
validation. `DisabledOptionSelect` greys out the app-only print methods on the web, and **HTML's
form-submission algorithm skips a `<select>`'s selected option if that option is disabled** — so an
account already set to Bluetooth submitted no `print_method` at all and got "this field is
required" on a dropdown that was plainly showing a value. Worse, the re-render then had nothing
selected, the browser displayed the first option, and the "successful" second save silently
rewrote the user's print method to PDF. The currently-selected option is never disabled now, the
field is optional on the web, and an omitted value means *leave it alone* rather than *blank it*.

**The silent PDF fallback when the app is closed.** `LotLabelView` used to branch on whether the
phone was answering, so a print made with the app shut fell through to a PDF download with nothing
said. It now branches on the **preference** only: the job is created either way, dispatch marks it
unreachable immediately, and the waiting page opens directly on *"Open the app on your phone, then
press Try again"* with **Try again** and **Cancel** beside it and the PDF still available as a
button somebody chooses. The app is not involved.

**A bonus bug found on the way, worth knowing about because it made thermal prints look broken.**
`empty_labels` exists to skip the used corner of a part-used Avery sheet. There is no such thing on
a roll, but it was being applied to `single_label_page` renders too — which is the Bluetooth raster
path, where page one *is* the picture handed to the printer. Anyone who had ever printed onto a
part-used sheet was getting blank labels out of their thermal printer until they changed that
setting back. Single-label pages now force it to zero.
