# Printing: what the app must change

Server side is live and tested (`test_remote_print.py`, `test_mobile_features.py`). Written
2026-09-11 after a live auction printed at ~5 s a label and reported a paper jam as success.

## 1. Print runs through the batch endpoint

A label renders in ~110 ms and is cached. The five seconds was forty round trips.

`POST /api/mobile/labels/batch/`

```json
{ "lots": [123, 124, 125], "resolution": "600x400", "dpi": 203 }
```

```json
{
  "labels":    [{"lot": 123, "content_type": "image/png", "png": "<base64>"}],
  "remaining": [124, 125],
  "skipped":   [{"lot": 999, "detail": "Lot not found."}],
  "resolution": "600x400",
  "dpi": 203
}
```

- `lots` is in print order; duplicates are dropped. Defaults match `GET /api/mobile/labels/<pk>/`.
- Each response stops at 25 labels or 3 seconds. **Loop:** print what came back, POST `remaining`,
  until it is empty.
- `skipped` never fails the batch. Log it, don't abort.
- A single reprint still uses `labels/<pk>/`.
- Nothing here marks a lot printed.

## 2. Report what didn't print

`POST /api/mobile/labels/printed/` takes three optional fields:

```json
{
  "lots":       [123, 124],
  "failed":     [125],
  "conditions": ["paper_jam"],
  "message":    "Printer reported a jam after label 3"
}
```

- `failed`: sent but not printed. Put back to unprinted, so "print unprinted labels" reprints them.
- `conditions`: validated against `printer_programs.STATUS_CONDITIONS` (`cover_open`,
  `out_of_paper`, `paper_jam`, `no_ribbon`, `overheated`, `low_battery`, `printing`, `paused`,
  `error`). Unknown → 400.
- `message`: kept verbatim.

Stop reporting success because the bytes reached the socket. If the profile has a status program,
read it after the run. If the printer can't be queried, send only `lots`.

Fixed server-side, no app change: the print-method form error from the web, the silent PDF fallback
with the app closed, and `empty_labels` blanking thermal labels.
