# AR Lot Mapping — App ↔ Backend Observation Payload Contract

This is the contract of record for the AR lot-scanning feature: what the mobile app sends and what the
backend does with it. It is written for the mobile-app developer — the app-side rules below are
normative.

## What it is

AR lot mapping builds a live 2D map of where each lot's printed QR label physically sits in the room.
The app points the camera at labels; for every camera frame it turns each QR sighting into an
angle measurement and POSTs a batch to the server. The server fuses every session's measurements into a
relative metric map of lot positions (used by the admin lot map and the in-app "locate this lot"
readout).

- **Endpoint:** `POST /api/mobile/ar/observations/` (JWT auth). Returns `202 {"accepted": <int>}`.
- **Design stance:** the app is a **dumb sensor**; **all** fusion lives server-side in
  `auctions/ar_mapping.py`. The app never solves geometry — it reports raw angles and whatever optional
  per-frame sensor channels it can (gyro, GPS, compass, odometry) and the server does the rest.
- **Compatibility rule:** every per-frame field beyond `detections` is **optional**. Older app builds
  (that omit a channel) and older backend builds (that ignore one) interoperate freely. Junk in any
  optional channel is **dropped to null server-side, never a 400** — one bad reading must never lose a
  good batch. Add channels freely; don't break this pattern.

## Payload schema

A batch is `{auction, session_id, fov_hdeg?, frames[]}`. Each frame is one camera exposure and carries
the optional sensor channels plus its `detections[]` (the QR sightings in that frame). All detections
of one frame share the frame's pose, which is what makes their bearing differences mutually
constraining.

```json
{
  "auction": "spring-fry-swap-2026",
  "session_id": "8f2c1e7a-3b6d-4a90-b1c2-0d5e9f4a7c31",
  "fov_hdeg": 68.0,
  "frames": [
    {
      "frame_id": "f000",
      "captured_at": "2026-07-21T18:04:11.482Z",
      "yaw_deg": 0.0,
      "latitude": 40.4406,
      "longitude": -79.9959,
      "heading_deg": 137.5,
      "odo_x_m": 0.0,
      "odo_y_m": 0.0,
      "detections": [
        { "lot": 48213, "bearing_deg": -12.5, "depression_deg": 28.9, "quality": 0.82 },
        { "lot": 48219, "bearing_deg": 7.1, "depression_deg": 24.0, "quality": 0.61 }
      ]
    }
  ]
}
```

Field notes:

- `auction` — auction slug (string). `session_id` — opaque client token, one per AR screen mount, max 36
  chars. It is stored/compared as an opaque string (a plain varchar), never parsed as a UUID, so any
  token is fine; do **not** assume it must be RFC-4122.
- `fov_hdeg` — the horizontal camera FOV (degrees) the bearings were computed against. Present ⇒ the
  batch's rows are marked FOV-calibrated (the solver trusts the bearings tighter); absent ⇒ assumed-FOV
  fallback.
- `frame_id` — unique per camera frame within a session (max 32 chars). `captured_at` — ISO-8601; a
  client clock ahead of the server is clamped to server-now on ingest.
- Per detection: `lot` (pk), `bearing_deg` (horizontal angle in that frame's camera coords, +right),
  `depression_deg` (ray angle below horizontal, gravity-referenced, +down), `quality` (0–1 sharpness,
  optional, default 1.0). A detection with an out-of-range angle or a lot not live in the auction is
  dropped silently — never fail the frame.
- Batch caps: ≤ 50 frames/batch, ≤ 10 detections/frame (exceeding either is a clean 400; chunk on the
  app side).

## Per-frame sensor channels

Each channel below is optional and independent. Convention, app-side rules, what the solver does, and a
status line follow for each.

### `yaw_deg` — relative gyro heading (odometry)

- **Convention:** the phone's integrated gyro heading at capture, degrees, **ccw-positive** about
  gravity, **cumulative/unwrapped** and **zero at session start**. It is a *relative* heading, not a
  compass bearing. `null`/absent ⇒ no gyro data ("unknown", never "didn't turn").
- **App rules:** send the running unwrapped integral (e.g. three left turns ⇒ `1080`, valid). Only stop
  sending it if the gyro is unavailable. A runaway integrator value (`|yaw| > 36000`) is dropped
  server-side.
- **Solver:** heading odometry — between consecutive frames of one session that both carry yaw, the
  measured turn ties the two camera rotations together. This makes a one-label-per-frame walk between
  two tables recover the *relative direction* between them (otherwise unconstrained).
- **Status:** done (app + backend).

### `latitude` / `longitude` — GPS island position anchoring

- **Convention:** the phone's GPS fix at capture (WGS84 degrees). **Both or neither.** `null`/absent ⇒
  no fix.
- **App rules:** omit both (or send null) when there is no location permission or no fix. **Never send
  `(0, 0)` as "no fix"** — it is the classic sentinel and is discarded server-side, but the app must not
  rely on that; just omit. A coarse fix is fine (GPS only anchors island *bases*, not individual lots).
  Per-frame (send it on every frame you have it).
- **Solver:** anchors the *base location* of each disconnected island, so separately-scanned areas
  render roughly where they physically are (converted to a local east/north metre frame) instead of at
  arbitrary offsets. GPS gives position but no orientation.
- **Status:** done (app + backend).

### `heading_deg` — absolute compass island orientation

- **Convention:** the phone's **absolute compass heading** for the camera's forward axis, degrees
  **CW from MAGNETIC north** (0 = N, 90 = E), **tilt-compensated**. `null`/absent ⇒ no compass reading
  ("unknown"). Unlike `yaw_deg` (relative), this is an absolute bearing.
- **App rules:** report the tilt-compensated magnetic heading of where the camera points. Do **not**
  apply declination — the server does magnetic→true itself (WMM declination from the frame's GPS +
  date). A value outside `[-360, 360]` or non-finite is dropped; a survivor is normalized to `[0, 360)`.
- **Solver:** a **soft (~20°) prior** on each carrying frame's world orientation — it fixes each
  disconnected island's *absolute* rotation (the one thing bearings + GPS cannot). Soft on purpose:
  indoor magnetic interference is real, so it never overrides good bearings.
- **Status:** done (app + backend, as of 2026-07-21).

### `odo_x_m` / `odo_y_m` — translation dead-reckoning

- **Convention:** the phone's **cumulative planar displacement since session start**, metres, in the
  **same session-fixed frame as `yaw_deg`**: origin at the session's first tracked position; **+x = the
  camera's forward direction (ground-plane projection) at yaw 0** (session start); **+y = 90°
  counter-clockwise from +x** (the camera's left). `null`/absent ⇒ unknown (tracking unsupported or
  lost) — never "didn't move".
- **App rules:**
  - **Both or neither.** Send the cumulative `(x, y)` pair or omit both.
  - **`(0, 0)` is VALID** — it is the session origin, which the first frame legitimately reports. This
    is the deliberate difference from GPS: do **not** treat `(0, 0)` as "no data" and do **not** omit
    the first frame's zero.
  - **Source-agnostic:** ARKit/ARCore world-tracking position projected to the ground plane is
    preferred; pedometer step-integration is acceptable. Never block scanning on tracking availability —
    if there's no tracker, just omit odo and keep scanning.
  - **Tracker-reset rule:** the cumulative values must all share one frame for the whole session. If the
    underlying tracker resets / rebases / relocalizes to a new origin mid-session, **stop sending odo
    for the rest of that session** (do not resume with values in a different frame).
  - A half-supplied pair, any non-finite value, or an implausible magnitude (`|value| > 10000`, i.e. >10
    km of walking) drops both to null server-side — but the app should send clean data.
- **Solver:** true positional odometry. Consecutive frames that carry **both odo and yaw** get a
  *measured* displacement residual (σ = `0.3 m + 5 %·distance + 0.01·Δt`) that **supersedes** the
  generous pace-cap guess for that step. Yaw is required alongside odo because the odo frame's world
  rotation is recovered as `φ = θ − radians(yaw)`. Being metric, odometry also **sharpens the map's
  absolute scale** beyond the height-prior-only ±30 %, and it makes a walk between two tables recover
  the metric *distance* between them (yaw alone recovers only the direction).
- **Status:** **backend done — APP TODO (this is the handoff).** The backend consumes odo whenever it
  arrives and ignores its absence; the app does not yet send it.

## Remaining known gap

None currently planned beyond the app-side implementation of `odo_x_m`/`odo_y_m` above. Every channel
rides the same optional-field pattern, so any future sensor (e.g. a barometric floor hint) can be added
the same way: optional per-frame field, junk dropped to null server-side, older builds unaffected.
