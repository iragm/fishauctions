"""AR lot-location solver — bearing-dominant 2D bundle adjustment.

The mobile app is a dumb sensor: for each camera frame it turns QR sightings into
``(bearing, depression)`` angle measurements (size-independent, so nothing depends on the printed
QR size) and reports the phone's integrated gyro heading (``yaw_deg``), then POSTs them. All fusion
lives here. This module solves the rolling observation buffer from scratch each pass into a relative
2D map of lot positions.

Formulation (see the backend spec): one camera pose ``(x, y, θ)`` per distinct
``(session_id, frame_id)``, one landmark ``(x, y)`` per observed lot, and one phone-height nuisance
``h`` per session.

* **Bearing residual** (strong, ~0.1° accurate): ``wrap(atan2(Ly−Cy, Lx−Cx) − θ − bearing_ccw)/σ_b``
  where ``bearing_ccw = −radians(bearing_deg)`` and ``σ_b`` is 0.01 rad for FOV-calibrated bearings,
  0.02 otherwise. This is what pins the layout — triangulation, never ranging.
* **Depression pseudo-range** (weak, fixes scale): for ``depression_deg > 8°`` the label-plane model
  gives ``r̃ = h / tan(depression)``; residual ``(‖L − C‖ − r̃) / (0.5·r̃)``. Level views carry no
  range information and are skipped.
* **Heading odometry** (fixes cross-table direction): between consecutive frames of one session that
  both carry gyro yaw, ``wrap((θ_b − θ_a) − radians(yaw_b − yaw_a)) / σ_gyro(Δt)`` ties the poses'
  rotations to the measured turn. Because yaw is cumulative from session start, this survives a long
  walk between tables — so a user scanning one label per frame while walking A → B produces a
  session-rigid frame and the A-to-B direction becomes *measured* rather than an arbitrary guess.
* **Weights**: every observation residual is scaled by ``w = quality · exp(−age_hours/3)``.
  Observations older than 24 h or with ``w < 0.05`` are dropped — the "recent scans win" knob (a
  moved lot's stale sightings fade on a ~3 h half-life).
* **Frame chaining**: consecutive frames of one session within ~60 s get a weak motion prior (a soft
  residual on camera displacement beyond a pace-scaled cap) so single-detection sweep frames chain.
* **Components (islands)**: a union-find over the factor graph (frames + landmarks, edges from
  observations and session chain links) splits each solve into connected components. A component
  with ≥2 lots that already have stored positions is rigidly tied to the existing map frame; a
  component with fewer gets a cold-start gauge at an offset origin so islands never render
  overlapping. One scanning walk between two areas links their components into one.
* **GPS island anchoring**: bearings/gyro fix each island's *internal* layout and orientation, but
  say nothing about where one disconnected island sits relative to another — cold-start islands are
  otherwise marched along +x in an arbitrary order. When observations carry a phone GPS fix, each
  cold-start island's base is instead placed at its GPS centroid, converted to a local east/north
  (ENU) metre frame (east→+x, north→+y) relative to the solve's mean fix. GPS gives position but no
  heading, so this only *translates* islands (relative arrangement becomes roughly geographic,
  north-up); within-island orientation stays bearing-derived. The GPS group is shifted to sit past
  any prior-anchored island, so the established map never moves. Islands with no GPS fall back to the
  marched layout. See :func:`_gps_cold_bases`.
* **Gauge**: lots that already have a position get a weak prior pulling toward it (map doesn't
  rotate/flip between solves). A cold-start component pins its first landmark at its offset origin
  and its second on the +x axis.
* **Robustness**: ``least_squares(loss="soft_l1")`` with a sparse Jacobian; after convergence drop
  observations whose residual exceeds 3× the median and re-solve once.

Absolute scale comes only from the soft height prior (0.65 ± 0.3 m), so positions are ±30% metric —
fine for a relative admin map and an "about N m" readout, and the layout itself is bearing-accurate.
"""

import logging
import math
from collections import defaultdict, namedtuple

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

logger = logging.getLogger(__name__)

# ── Tunables (all server-side; adjust freely) ──────────────────────────────────────────────────
WINDOW_HOURS = 24.0  # observations older than this never enter the solve
HALF_LIFE_HOURS = 3.0  # recency weight half-life: w includes exp(-age/HALF_LIFE_HOURS)
MIN_WEIGHT = 0.05  # observations lighter than this are excluded
DEPRESSION_MIN_DEG = 8.0  # below this the depression carries no usable range
SIGMA_BEARING_CAL = 0.01  # rad, FOV-calibrated bearings
SIGMA_BEARING_UNCAL = 0.02  # rad, assumed-FOV bearings
RANGE_SIGMA_FRAC = 0.5  # pseudo-range σ as a fraction of the range itself
HEIGHT_PRIOR_M = 0.65  # phone height above the label plane (standing ≈1.4 m, table labels ≈0.75 m)
HEIGHT_SIGMA_M = 0.30
MOTION_WINDOW_S = 60.0  # consecutive-frame chaining window (widened from 10 s: yaw carries heading)
MOTION_CAP_BASE_M = 3.0  # base displacement cap before a motion pair is penalised
MOTION_CAP_PER_S = 1.5  # cap grows with walking pace: cap = max(base, per_s · Δt_seconds)
MOTION_WEIGHT = 0.5
HEADING_MAX_PAIR_S = 600.0  # heading-odometry pairs cap (~10 min): beyond that gyro drift is too big
SIGMA_GYRO_BASE = 0.01  # rad, σ_gyro(Δt) = base + per_s · Δt (drift grows with the gap)
SIGMA_GYRO_PER_S = 0.001
PRIOR_WEIGHT = 0.1  # weak pull of a landmark toward its previous LotPosition
CAMERA_REG_WEIGHT = 0.01  # tiny tether of each camera to its init pose (regularises null directions)
GAUGE_WEIGHT = 1.0e3  # strong cold-start anchor (origin / +x axis)
OUTLIER_FACTOR = 3.0  # drop observations with residual > factor × median, then re-solve
DEFAULT_INIT_RANGE_M = 2.0  # init-only guess when depression gives no range
ISLAND_GAP_M = 20.0  # gap between cold-start islands' bounding boxes so they never render overlapping
M_PER_DEG_LAT = 110540.0  # metres per degree latitude (WGS84 mean); good enough for a hall-scale ENU
M_PER_DEG_LON = 111320.0  # metres per degree longitude at the equator; scaled by cos(lat) in use

# Normalised observation the solver consumes (DB-agnostic, so the solver is unit-testable).
# ``yaw_deg`` is the phone's cumulative gyro heading at capture (ccw-positive about gravity, zero at
# session start, same sign as θ); None ⇒ the device gave no gyro data ("unknown", never "no turn").
# ``latitude``/``longitude`` are the phone's GPS fix at capture (WGS84 deg) or None ⇒ no fix; used
# only to anchor disconnected islands' base locations (see ``_gps_cold_bases``).
Observation = namedtuple(
    "Observation",
    ["lot_id", "session_id", "frame_id", "captured_at", "bearing_deg", "depression_deg", "quality", "fov_calibrated"]
    + ["yaw_deg", "latitude", "longitude"],
    defaults=(None, None, None),
)

# One solved landmark. ``component`` is the solve-local island index (0-based, stable by lowest lot).
Solved = namedtuple("Solved", ["x", "y", "confidence", "observation_count", "component"])


def _wrap(angle):
    """Wrap radians to (−π, π]."""
    return (angle + np.pi) % (2 * np.pi) - np.pi


def _prepare(observations, now):
    """Filter to the live window and pack observations into flat numpy arrays + index maps.

    Returns None when there is nothing solvable, else a dict of everything the solver needs.
    """
    live = []
    for obs in observations:
        age_hours = (now - obs.captured_at).total_seconds() / 3600.0
        if age_hours < 0:
            age_hours = 0.0  # client clock ahead of us; treat as fresh (captured_at is clamped on ingest)
        if age_hours > WINDOW_HOURS:
            continue
        quality = min(max(obs.quality, 0.0), 1.0)
        weight = quality * math.exp(-age_hours / HALF_LIFE_HOURS)
        if weight < MIN_WEIGHT:
            continue
        live.append((obs, weight))
    if not live:
        return None

    # Stable ordering makes the solve deterministic (and the gauge anchors reproducible).
    frame_keys = sorted({(str(o.session_id), o.frame_id) for o, _ in live})
    lot_ids = sorted({o.lot_id for o, _ in live})
    session_ids = sorted({str(o.session_id) for o, _ in live})
    cam_index = {key: i for i, key in enumerate(frame_keys)}
    lm_index = {lot_id: i for i, lot_id in enumerate(lot_ids)}
    h_index = {sid: i for i, sid in enumerate(session_ids)}

    n = len(live)
    cam_idx = np.empty(n, dtype=np.intp)
    lm_idx = np.empty(n, dtype=np.intp)
    h_idx = np.empty(n, dtype=np.intp)
    bearing_ccw = np.empty(n)
    sigma_b = np.empty(n)
    weight = np.empty(n)
    depr_rad = np.empty(n)
    has_range = np.zeros(n, dtype=bool)

    for k, (obs, w) in enumerate(live):
        sid = str(obs.session_id)
        cam_idx[k] = cam_index[(sid, obs.frame_id)]
        lm_idx[k] = lm_index[obs.lot_id]
        h_idx[k] = h_index[sid]
        bearing_ccw[k] = -math.radians(obs.bearing_deg)
        sigma_b[k] = SIGMA_BEARING_CAL if obs.fov_calibrated else SIGMA_BEARING_UNCAL
        weight[k] = w
        depr_rad[k] = math.radians(obs.depression_deg)
        has_range[k] = obs.depression_deg > DEPRESSION_MIN_DEG

    # captured_at, session, yaw and GPS per frame (for motion/heading chaining + island anchoring).
    frame_time = {}
    frame_session = {}
    frame_yaw = {}
    frame_gps = {}
    for obs, _ in live:
        key = (str(obs.session_id), obs.frame_id)
        frame_time[key] = obs.captured_at
        frame_session[key] = str(obs.session_id)
        # All detections of one frame share the frame's yaw/GPS; keep the first non-None seen.
        if frame_yaw.get(key) is None:
            frame_yaw[key] = obs.yaw_deg
        if frame_gps.get(key) is None and obs.latitude is not None and obs.longitude is not None:
            frame_gps[key] = (obs.latitude, obs.longitude)

    return {
        "live": live,
        "frame_keys": frame_keys,
        "lot_ids": lot_ids,
        "session_ids": session_ids,
        "cam_index": cam_index,
        "lm_index": lm_index,
        "h_index": h_index,
        "cam_idx": cam_idx,
        "lm_idx": lm_idx,
        "h_idx": h_idx,
        "bearing_ccw": bearing_ccw,
        "sigma_b": sigma_b,
        "weight": weight,
        "depr_rad": depr_rad,
        "has_range": has_range,
        "frame_time": frame_time,
        "frame_session": frame_session,
        "frame_yaw": frame_yaw,
        "frame_gps": frame_gps,
    }


def _session_chains(data):
    """Consecutive-frame links within each session, in time order.

    Returns ``(motion_pairs, heading_pairs)`` where each ``motion_pairs`` entry is
    ``(cam_a, cam_b, cap_m)`` (soft displacement cap, pace-scaled) and each ``heading_pairs`` entry
    is ``(cam_a, cam_b, dyaw_rad, sigma_rad)`` (only when both frames carry gyro yaw).
    """
    motion_pairs = []
    heading_pairs = []
    for sid in data["session_ids"]:
        keys = sorted(
            (k for k in data["frame_keys"] if data["frame_session"][k] == sid),
            key=lambda k: data["frame_time"][k],
        )
        for a, b in zip(keys, keys[1:]):
            dt = abs((data["frame_time"][b] - data["frame_time"][a]).total_seconds())
            ia, ib = data["cam_index"][a], data["cam_index"][b]
            if dt <= MOTION_WINDOW_S:
                cap = max(MOTION_CAP_BASE_M, MOTION_CAP_PER_S * dt)
                motion_pairs.append((ia, ib, cap))
            ya, yb = data["frame_yaw"][a], data["frame_yaw"][b]
            if ya is not None and yb is not None and dt <= HEADING_MAX_PAIR_S:
                heading_pairs.append((ia, ib, math.radians(yb - ya), SIGMA_GYRO_BASE + SIGMA_GYRO_PER_S * dt))
    return motion_pairs, heading_pairs


def _components(data, motion_pairs, heading_pairs):
    """Union-find over the factor graph → connected components ("islands").

    Nodes are frames (``0..ncam-1``) and landmarks (``ncam..ncam+nlm-1``); edges are observations
    (frame↔lot) plus session chain links (frame↔frame motion/heading pairs). Returns a list of
    component dicts (``{"lms": [...], "frames": [...]}``, ordered by their lowest lot id) and an
    ``lm_component`` array mapping each landmark index to its component id.
    """
    ncam = len(data["frame_keys"])
    nlm = len(data["lot_ids"])
    lot_ids = data["lot_ids"]
    parent = list(range(ncam + nlm))

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for k in range(len(data["live"])):
        union(int(data["cam_idx"][k]), ncam + int(data["lm_idx"][k]))
    for a, b, *_ in motion_pairs:
        union(int(a), int(b))
    for a, b, *_ in heading_pairs:
        union(int(a), int(b))

    comp_lms = defaultdict(list)
    for j in range(nlm):
        comp_lms[find(ncam + j)].append(j)
    ordered_roots = sorted(comp_lms, key=lambda r: min(lot_ids[j] for j in comp_lms[r]))

    lm_component = np.full(nlm, -1, dtype=np.intp)
    components = []
    for cid, root in enumerate(ordered_roots):
        lms_in = sorted(comp_lms[root])
        for j in lms_in:
            lm_component[j] = cid
        frames_in = [ci for ci in range(ncam) if find(ci) == root]
        components.append({"lms": lms_in, "frames": frames_in})
    return components, lm_component


def _rigid_align(local, world):
    """Least-squares proper-rigid transform (R, t) with world ≈ R·local + t (Umeyama, no scale, no
    reflection). ``local``/``world`` are (N, 2). Returns (theta, tx, ty)."""
    local = np.asarray(local, dtype=float)
    world = np.asarray(world, dtype=float)
    mu_l = local.mean(0)
    mu_w = world.mean(0)
    cov = (world - mu_w).T @ (local - mu_l)
    U, _s, Vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(U @ Vt))
    R = U @ np.diag([1.0, d]) @ Vt  # forbid reflection (camera rotation is proper)
    t = mu_w - R @ mu_l
    return math.atan2(R[1, 0], R[0, 0]), t[0], t[1]


def _range_guess(data, k):
    if data["has_range"][k]:
        return HEIGHT_PRIOR_M / max(math.tan(data["depr_rad"][k]), 1e-3)
    return DEFAULT_INIT_RANGE_M


def _rot(theta):
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


def _initial_guess(data, priors, components):
    """Bootstrap init by incremental rigid resection, then lay out islands so they don't overlap.

    Place the first frame's landmarks from their range+bearing; for each later frame recover its pose
    by aligning its local ray points to the landmarks already placed (or, when only one/zero are
    shared, seed its heading from gyro yaw relative to the session anchor instead of the old
    assume-no-rotation guess), and drop new landmarks into the world. Then translate/rotate each
    cold-start component (one with <2 stored-prior lots) to its own offset origin so disconnected
    islands render side by side rather than piled at the origin.

    Returns ``(x0, gauges)`` where ``gauges`` is the per-component gauge spec the residual consumes.
    """
    frame_keys = data["frame_keys"]
    lot_ids = data["lot_ids"]
    nsess = len(data["session_ids"])
    ncam, nlm = len(frame_keys), len(lot_ids)

    cams = np.zeros((ncam, 3))
    lms = np.zeros((nlm, 2))
    placed = [False] * nlm

    # Seed placed landmarks from priors (keeps continuity + ties later frames to the old frame).
    for lot_id in lot_ids:
        j = data["lm_index"][lot_id]
        if lot_id in priors:
            lms[j] = priors[lot_id]
            placed[j] = True

    # Detections grouped by frame, in global time order.
    by_frame = defaultdict(list)
    for k in range(len(data["live"])):
        by_frame[data["cam_idx"][k]].append(k)
    frame_order = sorted(range(ncam), key=lambda ci: data["frame_time"][frame_keys[ci]])

    # Session heading anchor: seed θ from θ_anchor + Δyaw when a frame carries gyro yaw.
    anchor_theta = {}  # session_id -> θ at the anchor frame
    anchor_yaw = {}  # session_id -> yaw_deg at the anchor frame
    last_pos = {}  # session_id -> (x, y) of the previous frame (spatial continuity along a walk)
    last_theta = 0.0
    for ci in frame_order:
        key = frame_keys[ci]
        sid = data["frame_session"][key]
        fyaw = data["frame_yaw"][key]
        dets = by_frame[ci]
        # Local ray points in the camera frame (camera looks along +x of its own frame).
        local = {
            k: _range_guess(data, k) * np.array([math.cos(data["bearing_ccw"][k]), math.sin(data["bearing_ccw"][k])])
            for k in dets
        }
        shared = [(k, data["lm_idx"][k]) for k in dets if placed[data["lm_idx"][k]]]

        if len(shared) >= 2:
            theta, tx, ty = _rigid_align([local[k] for k, _ in shared], [lms[j] for _, j in shared])
        else:
            if fyaw is not None and sid in anchor_yaw:
                theta = anchor_theta[sid] + math.radians(fyaw - anchor_yaw[sid])
            else:
                theta = last_theta
            if len(shared) == 1:
                k, j = shared[0]
                tx, ty = lms[j] - _rot(theta) @ local[k]
            else:
                # No shared landmark: thread the walk from the previous same-session frame's position
                # (spatial continuity) instead of jumping to the origin — that origin jump is what let
                # a single-detection sweep across tables collapse the two islands together.
                tx, ty = last_pos.get(sid, (0.0, 0.0))
        cams[ci] = (tx, ty, theta)
        last_theta = theta
        last_pos[sid] = (tx, ty)
        if fyaw is not None and sid not in anchor_yaw:
            anchor_theta[sid], anchor_yaw[sid] = theta, fyaw

        rot = _rot(theta)
        for k in dets:
            j = data["lm_idx"][k]
            if not placed[j]:
                lms[j] = np.array([tx, ty]) + rot @ local[k]
                placed[j] = True

    gauges = _layout_components(data, components, priors, lms, cams)
    heights = np.full(nsess, HEIGHT_PRIOR_M)
    return np.concatenate([cams.ravel(), lms.ravel(), heights]), gauges


def _gps_cold_bases(data, components, running_x):
    """World positions (ENU metres) to anchor each cold-start island whose frames carry a GPS fix.

    Returns ``({component_index: np.array([east, north])}, running_x)``. Cold islands with no GPS
    frame are omitted (the caller marches them along +x as before). The whole GPS group is translated
    as one rigid block so it sits just past any prior-anchored island (its min-x → the incoming
    ``running_x``) and is vertically centred (mean north → 0) — a pure translation that preserves the
    islands' relative geography while never disturbing the established map. GPS gives no heading, so
    only the base translation is anchored; within-island orientation stays bearing-derived. The
    returned ``running_x`` is advanced past the group so non-GPS cold islands march after it.
    """
    frame_gps = data["frame_gps"]
    if not frame_gps:
        return {}, running_x

    # ENU reference = mean of every fix in the solve (self-contained; the map is relative anyway).
    lats = [ll[0] for ll in frame_gps.values()]
    lons = [ll[1] for ll in frame_gps.values()]
    lat0, lon0 = sum(lats) / len(lats), sum(lons) / len(lons)
    cos_lat0 = math.cos(math.radians(lat0))

    def enu(lat, lon):
        return np.array([(lon - lon0) * M_PER_DEG_LON * cos_lat0, (lat - lat0) * M_PER_DEG_LAT])

    frame_keys = data["frame_keys"]
    raw = {}
    for cidx, comp in enumerate(components):
        if comp["mode"] != "cold":
            continue
        pts = [enu(*frame_gps[frame_keys[ci]]) for ci in comp["frames"] if frame_keys[ci] in frame_gps]
        if pts:
            raw[cidx] = np.mean(pts, axis=0)
    if not raw:
        return {}, running_x

    centroids = np.array(list(raw.values()))
    shift = np.array([running_x - float(centroids[:, 0].min()), -float(centroids[:, 1].mean())])
    bases = {cidx: (c + shift) for cidx, c in raw.items()}
    running_x = running_x + float(centroids[:, 0].max() - centroids[:, 0].min()) + ISLAND_GAP_M
    return bases, running_x


def _layout_components(data, components, priors, lms, cams):
    """Classify each component and lay out cold-start ones at non-overlapping origins.

    Mutates ``lms``/``cams`` in place for cold-start components (canonicalises rotation to put the
    first anchor at the origin and the second on +x, then shifts the island to its base — its GPS
    centroid when the island's frames carry a fix, else beyond the running bounding box). Returns
    ``gauges``: a list of ``("prior", [(j, px, py), ...])`` for components rigidly tied to stored
    positions and ``("cold", j1, ox, oy, j2_or_None)`` for cold-start ones.
    """
    lot_ids = data["lot_ids"]
    prior_ids = set(priors)

    placed_max_x = None
    for comp in components:
        prior_in = [(j, priors[lot_ids[j]]) for j in comp["lms"] if lot_ids[j] in prior_ids]
        comp["prior_in"] = prior_in
        comp["mode"] = "prior" if len(prior_in) >= 2 else "cold"
        if comp["mode"] == "prior":
            mx = max(px for _, (px, _py) in prior_in)
            placed_max_x = mx if placed_max_x is None else max(placed_max_x, mx)

    running_x = (placed_max_x + ISLAND_GAP_M) if placed_max_x is not None else 0.0
    # GPS-anchored bases for the cold islands that carry a fix (arranged as a group past the priors).
    gps_bases, running_x = _gps_cold_bases(data, components, running_x)

    gauges = []
    for cidx, comp in enumerate(components):
        lms_in = comp["lms"]
        if comp["mode"] == "prior":
            gauges.append(("prior", [(j, float(px), float(py)) for j, (px, py) in comp["prior_in"]]))
            continue
        gps_base = gps_bases.get(cidx)
        if len(lms_in) == 1:
            j1 = lms_in[0]
            base = gps_base if gps_base is not None else np.array([running_x, 0.0])
            lms[j1] = np.array([float(base[0]), float(base[1])])
            gauges.append(("cold", j1, float(base[0]), float(base[1]), None))
            if gps_base is None:
                running_x += ISLAND_GAP_M
            continue
        # Two anchors: the two lowest lot-ids in the component (lms_in is sorted by landmark index,
        # which is lot-id order). Canonicalise: j1 → origin, j2 → +x axis.
        j1, j2 = lms_in[0], lms_in[1]
        p1 = lms[j1].copy()
        v = lms[j2] - p1
        ang = math.atan2(v[1], v[0])
        R = _rot(-ang)
        pts = np.array([(lms[j] - p1) @ R.T for j in lms_in])
        min_x = float(pts[:, 0].min())
        max_x = float(pts[:, 0].max())
        if gps_base is not None:
            # Land the island's landmark centroid on its GPS centroid (2D translate, no marching).
            base = np.asarray(gps_base, dtype=float) - pts.mean(0)
        else:
            base = np.array([running_x - min_x, 0.0])
        for local_i, j in enumerate(lms_in):
            lms[j] = np.array([pts[local_i, 0] + base[0], pts[local_i, 1] + base[1]])
        for ci in comp["frames"]:
            cp = (np.array(cams[ci, :2]) - p1) @ R.T + base
            cams[ci, 0], cams[ci, 1] = cp[0], cp[1]
            cams[ci, 2] = cams[ci, 2] - ang
        gauges.append(("cold", j1, float(lms[j1, 0]), float(lms[j1, 1]), j2))
        if gps_base is None:
            running_x = base[0] + max_x + ISLAND_GAP_M
    return gauges


def _gauge_count(gauges):
    total = 0
    for g in gauges:
        if g[0] == "prior":
            total += 2 * len(g[1])
        else:  # cold: pin anchor1 (x, y) [+ anchor2 y when present]
            total += 3 if g[4] is not None else 2
    return total


def _residual_context(data, motion_pairs, heading_pairs, gauges, mask):
    """Precompute the fixed pieces of the residual/sparsity (offsets, chain pairs, gauges)."""
    ncam = len(data["frame_keys"])
    nlm = len(data["lot_ids"])
    return {
        "cam_off": 0,
        "lm_off": 3 * ncam,
        "h_off": 3 * ncam + 2 * nlm,
        "ncam": ncam,
        "nlm": nlm,
        "motion_pairs": motion_pairs,
        "heading_pairs": heading_pairs,
        "gauges": gauges,
        "mask": mask,
    }


def _build_residual_fn(data, ctx, x0):
    """Return (residual_fn, seg, active, r0, sparsity) for the current active-observation mask."""
    mask = ctx["mask"]
    active = np.nonzero(mask)[0]
    lm_off, h_off = ctx["lm_off"], ctx["h_off"]
    ncam, nlm = ctx["ncam"], ctx["nlm"]

    cam_idx = data["cam_idx"][active]
    lm_idx = data["lm_idx"][active]
    h_idx = data["h_idx"][active]
    bearing_ccw = data["bearing_ccw"][active]
    sigma_b = data["sigma_b"][active]
    weight = data["weight"][active]
    depr_rad = data["depr_rad"][active]
    has_range = data["has_range"][active]
    range_pos = np.nonzero(has_range)[0]
    n_obs = len(active)
    n_range = len(range_pos)
    motion_pairs = ctx["motion_pairs"]
    heading_pairs = ctx["heading_pairs"]
    gauges = ctx["gauges"]

    # Residual layout: [bearing(n_obs)] [range(n_range)] [height(nsess)]
    #                  [motion(len)] [heading(len)] [camreg(3*ncam)] [gauge(...)]
    nsess = len(data["session_ids"])
    seg = {}
    cursor = 0

    def add(name, count):
        nonlocal cursor
        seg[name] = (cursor, cursor + count)
        cursor += count

    add("bearing", n_obs)
    add("range", n_range)
    add("height", nsess)
    add("motion", len(motion_pairs))
    add("heading", len(heading_pairs))
    add("camreg", 3 * ncam)
    add("gauge", _gauge_count(gauges))
    n_res = cursor

    cam_init = x0[: 3 * ncam].reshape(ncam, 3).copy()

    def residual(params):
        cams = params[: 3 * ncam].reshape(ncam, 3)
        lms = params[lm_off : lm_off + 2 * nlm].reshape(nlm, 2)
        heights = params[h_off : h_off + nsess]
        out = np.empty(n_res)

        C = cams[cam_idx]
        theta = C[:, 2]
        L = lms[lm_idx]
        dx = L[:, 0] - C[:, 0]
        dy = L[:, 1] - C[:, 1]
        model_bearing = np.arctan2(dy, dx) - theta
        b0, b1 = seg["bearing"]
        out[b0:b1] = weight * _wrap(model_bearing - bearing_ccw) / sigma_b

        r0, r1 = seg["range"]
        if n_range:
            dist = np.sqrt(dx[range_pos] ** 2 + dy[range_pos] ** 2)
            r_tilde = heights[h_idx[range_pos]] / np.tan(depr_rad[range_pos])
            r_tilde = np.maximum(r_tilde, 1e-3)
            out[r0:r1] = weight[range_pos] * (dist - r_tilde) / (RANGE_SIGMA_FRAC * r_tilde)

        h0, h1 = seg["height"]
        out[h0:h1] = (heights - HEIGHT_PRIOR_M) / HEIGHT_SIGMA_M

        m0, _m1 = seg["motion"]
        for idx, (a, b, cap) in enumerate(motion_pairs):
            d = math.hypot(cams[b, 0] - cams[a, 0], cams[b, 1] - cams[a, 1])
            out[m0 + idx] = MOTION_WEIGHT * max(0.0, d - cap)

        hd0, _hd1 = seg["heading"]
        for idx, (a, b, dyaw, sigma) in enumerate(heading_pairs):
            out[hd0 + idx] = _wrap((cams[b, 2] - cams[a, 2]) - dyaw) / sigma

        cr0, cr1 = seg["camreg"]
        out[cr0:cr1] = CAMERA_REG_WEIGHT * (cams.ravel() - cam_init.ravel())

        g0, _g1 = seg["gauge"]
        cur = g0
        for g in gauges:
            if g[0] == "prior":
                for j, px, py in g[1]:
                    out[cur] = PRIOR_WEIGHT * (lms[j, 0] - px)
                    out[cur + 1] = PRIOR_WEIGHT * (lms[j, 1] - py)
                    cur += 2
            else:
                _, j1, ox, oy, j2 = g
                out[cur] = GAUGE_WEIGHT * (lms[j1, 0] - ox)
                out[cur + 1] = GAUGE_WEIGHT * (lms[j1, 1] - oy)
                cur += 2
                if j2 is not None:
                    out[cur] = GAUGE_WEIGHT * (lms[j2, 1] - oy)  # second anchor on the +x axis (y = oy)
                    cur += 1
        return out

    sparsity = _build_sparsity(seg, n_res, ncam, nlm, lm_off, h_off, nsess, cam_idx, lm_idx, h_idx, range_pos, ctx)
    return residual, seg, active, residual(x0), sparsity


def _build_sparsity(seg, n_res, ncam, nlm, lm_off, h_off, nsess, cam_idx, lm_idx, h_idx, range_pos, ctx):
    n_vars = 3 * ncam + 2 * nlm + nsess
    S = lil_matrix((n_res, n_vars), dtype=np.int8)

    def cam_cols(i):
        return [3 * i, 3 * i + 1, 3 * i + 2]

    def lm_cols(j):
        return [lm_off + 2 * j, lm_off + 2 * j + 1]

    b0, _ = seg["bearing"]
    for k in range(len(cam_idx)):
        row = b0 + k
        for col in cam_cols(cam_idx[k]) + lm_cols(lm_idx[k]):
            S[row, col] = 1

    r0, _ = seg["range"]
    for rk, k in enumerate(range_pos):
        row = r0 + rk
        for col in [3 * cam_idx[k], 3 * cam_idx[k] + 1] + lm_cols(lm_idx[k]) + [h_off + h_idx[k]]:
            S[row, col] = 1

    h0, _ = seg["height"]
    for s in range(nsess):
        S[h0 + s, h_off + s] = 1

    m0, _ = seg["motion"]
    for idx, (a, b, _cap) in enumerate(ctx["motion_pairs"]):
        for col in [3 * a, 3 * a + 1, 3 * b, 3 * b + 1]:
            S[m0 + idx, col] = 1

    hd0, _ = seg["heading"]
    for idx, (a, b, _dyaw, _sigma) in enumerate(ctx["heading_pairs"]):
        S[hd0 + idx, 3 * a + 2] = 1
        S[hd0 + idx, 3 * b + 2] = 1

    cr0, _ = seg["camreg"]
    for i in range(3 * ncam):
        S[cr0 + i, i] = 1

    g0, _ = seg["gauge"]
    cur = g0
    for g in ctx["gauges"]:
        if g[0] == "prior":
            for j, _px, _py in g[1]:
                S[cur, lm_off + 2 * j] = 1
                S[cur + 1, lm_off + 2 * j + 1] = 1
                cur += 2
        else:
            _, j1, _ox, _oy, j2 = g
            S[cur, lm_off + 2 * j1] = 1
            S[cur + 1, lm_off + 2 * j1 + 1] = 1
            cur += 2
            if j2 is not None:
                S[cur, lm_off + 2 * j2 + 1] = 1
                cur += 1

    return S.tocsr()


def _solve_once(data, ctx, x0):
    residual, seg, active, _r0, sparsity = _build_residual_fn(data, ctx, x0)
    result = least_squares(
        residual,
        x0,
        method="trf",
        loss="soft_l1",
        f_scale=1.0,
        jac_sparsity=sparsity,
        x_scale="jac",
        max_nfev=200,
    )
    return result, seg, active


def _per_observation_norm(residual_vec, seg, active, data):
    """Collapse each active observation's bearing (+range) residual into one norm, keyed by list
    position in ``active``."""
    b0, b1 = seg["bearing"]
    norms = np.abs(residual_vec[b0:b1]).astype(float)
    r0, r1 = seg["range"]
    if r1 > r0:
        # Map range residuals back onto their observation and add in quadrature.
        active_has_range = data["has_range"][active]
        range_local = np.nonzero(active_has_range)[0]
        rres = residual_vec[r0:r1]
        for local_i, res in zip(range_local, rres):
            norms[local_i] = math.hypot(norms[local_i], res)
    return norms


def solve_positions(observations, priors=None, *, now=None):
    """Solve the observation buffer into ``{lot_id: Solved(x, y, confidence, observation_count,
    component)}``.

    Pure/DB-agnostic so the geometry is unit-testable. ``observations`` is an iterable of
    :class:`Observation`; ``priors`` maps lot_id → (x, y) previous positions.
    """
    from django.utils import timezone

    if now is None:
        now = timezone.now()
    priors = priors or {}
    observations = list(observations)

    data = _prepare(observations, now)
    if data is None:
        return {}

    motion_pairs, heading_pairs = _session_chains(data)
    components, lm_component = _components(data, motion_pairs, heading_pairs)
    x0, gauges = _initial_guess(data, priors, components)

    n_obs_total = len(data["live"])
    mask = np.ones(n_obs_total, dtype=bool)
    ctx = _residual_context(data, motion_pairs, heading_pairs, gauges, mask)

    result, seg, active = _solve_once(data, ctx, x0)

    # One robust re-solve after dropping observations that make no sense (> 3× median residual).
    norms = _per_observation_norm(result.fun, seg, active, data)
    if len(norms) > 2:
        median = float(np.median(norms))
        if median > 0:
            keep_local = norms <= OUTLIER_FACTOR * median
            if not keep_local.all():
                new_mask = mask.copy()
                new_mask[active[~keep_local]] = False
                if new_mask.sum() >= 2:
                    mask = new_mask
                    ctx = _residual_context(data, motion_pairs, heading_pairs, gauges, mask)
                    result, seg, active = _solve_once(data, ctx, result.x)
                    norms = _per_observation_norm(result.fun, seg, active, data)

    return _collect(data, ctx, result, seg, active, norms, lm_component)


def _collect(data, ctx, result, seg, active, norms, lm_component):
    """Read landmark positions out of the solution and score confidence per lot."""
    nlm, lm_off = ctx["nlm"], ctx["lm_off"]
    lms = result.x[lm_off : lm_off + 2 * nlm].reshape(nlm, 2)

    # Surviving observation count + mean residual per landmark (over active observations only).
    counts = defaultdict(int)
    res_sum = defaultdict(float)
    active_lm = data["lm_idx"][active]
    for local_i, j in enumerate(active_lm):
        counts[j] += 1
        res_sum[j] += norms[local_i]

    out = {}
    for lot_id in data["lot_ids"]:
        j = data["lm_index"][lot_id]
        n = counts.get(j, 0)
        if n == 0:
            continue  # every observation for this lot was rejected → no position (caller keeps the stale one)
        mean_res = res_sum[j] / n
        confidence = min(1.0, n / 5.0) * math.exp(-mean_res)
        out[lot_id] = Solved(
            x=float(lms[j, 0]),
            y=float(lms[j, 1]),
            confidence=float(max(0.0, min(1.0, confidence))),
            observation_count=int(n),
            component=int(lm_component[j]),
        )
    return out


# ── Django glue ────────────────────────────────────────────────────────────────────────────────


def update_positions_for_auction(auction):
    """Re-solve one auction from its live observation buffer and upsert its LotPosition rows.

    Loads the last-24 h observations, solves, upserts a LotPosition (with a persistent island
    ``component`` id) per solved lot, and — crucially — *keeps* stale positions whose lot had no
    surviving observation this pass (they remain the best guess and serve as merge anchors for later
    sessions). Positions are removed only for lots that are now sold or removed; the admin
    "clear all locations" button and lot deletion handle the rest. Returns the number of solved lots.
    """
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from auctions.models import Lot, LotObservation, LotPosition

    now = timezone.now()
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    rows = LotObservation.objects.filter(auction=auction, captured_at__gte=cutoff).values(
        "lot_id",
        "session_id",
        "frame_id",
        "captured_at",
        "bearing_deg",
        "depression_deg",
        "quality",
        "fov_calibrated",
        "yaw_deg",
        "latitude",
        "longitude",
    )
    observations = [
        Observation(
            lot_id=r["lot_id"],
            session_id=r["session_id"],
            frame_id=r["frame_id"],
            captured_at=r["captured_at"],
            bearing_deg=r["bearing_deg"],
            depression_deg=r["depression_deg"],
            quality=r["quality"],
            fov_calibrated=r["fov_calibrated"],
            yaw_deg=r["yaw_deg"],
            latitude=r["latitude"],
            longitude=r["longitude"],
        )
        for r in rows
    ]

    existing = list(LotPosition.objects.filter(auction=auction))
    priors = {p.lot_id: (p.x, p.y) for p in existing}
    prior_component = {p.lot_id: p.component for p in existing}
    solved = solve_positions(observations, priors, now=now)

    # Map each solve-local component to a persistent island id: a component with stored lots inherits
    # the smallest of their ids (that's how two islands merge into one when a walk links them); a
    # brand-new island takes the next free id.
    local_members = defaultdict(list)
    for lot_id, s in solved.items():
        local_members[s.component].append(lot_id)
    existing_ids = set(prior_component.values())
    next_fresh = (max(existing_ids) + 1) if existing_ids else 0
    persistent_id = {}
    for cidx, lot_ids_in in local_members.items():
        stored = {prior_component[lid] for lid in lot_ids_in if lid in prior_component}
        if stored:
            survivor = min(stored)
            persistent_id[cidx] = survivor
            absorbed = stored - {survivor}
            if absorbed:
                # A scanning walk joined previously-separate islands: rewrite every row (including the
                # stale ones we keep) of the absorbed islands onto the surviving (smaller) id.
                LotPosition.objects.filter(auction=auction, component__in=absorbed).update(component=survivor)
        else:
            persistent_id[cidx] = next_fresh
            next_fresh += 1

    for lot_id, s in solved.items():
        LotPosition.objects.update_or_create(
            lot_id=lot_id,
            defaults={
                "auction": auction,
                "x": s.x,
                "y": s.y,
                "confidence": s.confidence,
                "observation_count": s.observation_count,
                "component": persistent_id[s.component],
            },
        )

    # Only sold/removed lots lose their position; a merely-unscanned lot keeps its stale one.
    sold_or_removed = Q(winning_price__isnull=False) | Q(banned=True) | Q(deactivated=True) | Q(is_deleted=True)
    gone_lot_ids = set(Lot.objects.filter(auction=auction).filter(sold_or_removed).values_list("pk", flat=True))
    if gone_lot_ids:
        LotPosition.objects.filter(auction=auction, lot_id__in=gone_lot_ids).delete()
    return len(solved)
