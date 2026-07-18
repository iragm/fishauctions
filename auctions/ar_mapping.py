"""AR lot-location solver — bearing-dominant 2D bundle adjustment.

The mobile app is a dumb sensor: for each camera frame it turns QR sightings into
``(bearing, depression)`` angle measurements (size-independent, so nothing depends on the printed
QR size) and POSTs them. All fusion lives here. This module solves the rolling observation buffer
from scratch each pass into a relative 2D map of lot positions.

Formulation (see the backend spec): one camera pose ``(x, y, θ)`` per distinct
``(session_id, frame_id)``, one landmark ``(x, y)`` per observed lot, and one phone-height nuisance
``h`` per session.

* **Bearing residual** (strong, ~0.1° accurate): ``wrap(atan2(Ly−Cy, Lx−Cx) − θ − bearing_ccw)/σ_b``
  where ``bearing_ccw = −radians(bearing_deg)`` and ``σ_b`` is 0.01 rad for FOV-calibrated bearings,
  0.02 otherwise. This is what pins the layout — triangulation, never ranging.
* **Depression pseudo-range** (weak, fixes scale): for ``depression_deg > 8°`` the label-plane model
  gives ``r̃ = h / tan(depression)``; residual ``(‖L − C‖ − r̃) / (0.5·r̃)``. Level views carry no
  range information and are skipped.
* **Weights**: every observation residual is scaled by ``w = quality · exp(−age_hours/3)``.
  Observations older than 24 h or with ``w < 0.05`` are dropped — the "recent scans win" knob (a
  moved lot's stale sightings fade on a ~3 h half-life).
* **Frame chaining**: consecutive frames of one session within 10 s get a weak motion prior (a soft
  residual on camera displacement beyond ~3 m) so single-detection sweep frames still chain.
* **Gauge**: lots that already have a position get a weak prior pulling toward it (map doesn't
  rotate/flip between solves). Cold start pins the first landmark at the origin and the second to the
  +x axis.
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
MOTION_WINDOW_S = 10.0  # consecutive-frame chaining window
MOTION_MAX_M = 3.0  # only displacement beyond this is penalised
MOTION_WEIGHT = 0.5
PRIOR_WEIGHT = 0.1  # weak pull of a landmark toward its previous LotPosition
CAMERA_REG_WEIGHT = 0.01  # tiny tether of each camera to its init pose (regularises null directions)
GAUGE_WEIGHT = 1.0e3  # strong cold-start anchor (origin / +x axis)
OUTLIER_FACTOR = 3.0  # drop observations with residual > factor × median, then re-solve
DEFAULT_INIT_RANGE_M = 2.0  # init-only guess when depression gives no range

# Normalised observation the solver consumes (DB-agnostic, so the solver is unit-testable).
Observation = namedtuple(
    "Observation",
    ["lot_id", "session_id", "frame_id", "captured_at", "bearing_deg", "depression_deg", "quality", "fov_calibrated"],
)

# One solved landmark.
Solved = namedtuple("Solved", ["x", "y", "confidence", "observation_count"])


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

    # captured_at per frame (for motion-prior chaining) and the frame's session.
    frame_time = {}
    frame_session = {}
    for obs, _ in live:
        key = (str(obs.session_id), obs.frame_id)
        frame_time[key] = obs.captured_at
        frame_session[key] = str(obs.session_id)

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
    }


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


def _initial_guess(data, priors):
    """Bootstrap init by incremental rigid resection: place the first frame's landmarks from their
    range+bearing, then for each later frame recover its pose by aligning its local ray points to the
    landmarks already placed, and drop any new landmarks it sees into the world. A good start keeps
    the local optimiser out of the reflection/rotation minima a bearing-only problem is riddled with.
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

    last_theta = 0.0
    for ci in frame_order:
        dets = by_frame[ci]
        # Local ray points in the camera frame (camera looks along +x of its own frame).
        local = {
            k: _range_guess(data, k) * np.array([math.cos(data["bearing_ccw"][k]), math.sin(data["bearing_ccw"][k])])
            for k in dets
        }
        shared = [(k, data["lm_idx"][k]) for k in dets if placed[data["lm_idx"][k]]]

        if len(shared) >= 2:
            theta, tx, ty = _rigid_align([local[k] for k, _ in shared], [lms[j] for _, j in shared])
        elif len(shared) == 1:
            k, j = shared[0]
            theta = last_theta
            rot = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
            tx, ty = lms[j] - rot @ local[k]
        else:
            theta, tx, ty = (last_theta, 0.0, 0.0)  # disconnected/first frame → anchor at origin
        cams[ci] = (tx, ty, theta)
        last_theta = theta

        rot = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
        for k in dets:
            j = data["lm_idx"][k]
            if not placed[j]:
                lms[j] = np.array([tx, ty]) + rot @ local[k]
                placed[j] = True

    heights = np.full(nsess, HEIGHT_PRIOR_M)
    return np.concatenate([cams.ravel(), lms.ravel(), heights])


def _residual_context(data, priors, mask):
    """Precompute the fixed pieces of the residual/sparsity (motion pairs, gauge mode)."""
    ncam = len(data["frame_keys"])
    nlm = len(data["lot_ids"])
    cam_off = 0
    lm_off = 3 * ncam
    h_off = lm_off + 2 * nlm

    # Consecutive-frame motion pairs within one session and the time window.
    motion_pairs = []
    for sid in data["session_ids"]:
        keys = sorted(
            (k for k in data["frame_keys"] if data["frame_session"][k] == sid),
            key=lambda k: data["frame_time"][k],
        )
        for a, b in zip(keys, keys[1:]):
            dt = abs((data["frame_time"][b] - data["frame_time"][a]).total_seconds())
            if dt <= MOTION_WINDOW_S:
                motion_pairs.append((data["cam_index"][a], data["cam_index"][b]))

    prior_lots = [(data["lm_index"][lid], priors[lid]) for lid in data["lot_ids"] if lid in priors]
    cold_start = not prior_lots

    return {
        "cam_off": cam_off,
        "lm_off": lm_off,
        "h_off": h_off,
        "ncam": ncam,
        "nlm": nlm,
        "motion_pairs": motion_pairs,
        "prior_lots": prior_lots,
        "cold_start": cold_start,
        "mask": mask,
    }


def _build_residual_fn(data, ctx, x0):
    """Return (residual_fn, n_residuals, sparsity) for the current active-observation mask."""
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
    prior_lots = ctx["prior_lots"]
    cold_start = ctx["cold_start"]

    # Residual layout: [bearing(n_obs)] [range(n_range)] [height(nsess)]
    #                  [motion(len)] [camreg(3*ncam)] [gauge...]
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
    add("camreg", 3 * ncam)
    if cold_start:
        add("gauge", 3 if nlm >= 2 else 2)
    else:
        add("prior", 2 * len(prior_lots))
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

        m0, m1 = seg["motion"]
        for idx, (a, b) in enumerate(motion_pairs):
            d = math.hypot(cams[b, 0] - cams[a, 0], cams[b, 1] - cams[a, 1])
            out[m0 + idx] = MOTION_WEIGHT * max(0.0, d - MOTION_MAX_M)

        cr0, cr1 = seg["camreg"]
        out[cr0:cr1] = CAMERA_REG_WEIGHT * (cams.ravel() - cam_init.ravel())

        if cold_start:
            g0, _ = seg["gauge"]
            out[g0] = GAUGE_WEIGHT * lms[0, 0]
            out[g0 + 1] = GAUGE_WEIGHT * lms[0, 1]
            if nlm >= 2:
                out[g0 + 2] = GAUGE_WEIGHT * lms[1, 1]  # second landmark pinned to the +x axis
        else:
            p0, _ = seg["prior"]
            for idx, (j, (px, py)) in enumerate(prior_lots):
                out[p0 + 2 * idx] = PRIOR_WEIGHT * (lms[j, 0] - px)
                out[p0 + 2 * idx + 1] = PRIOR_WEIGHT * (lms[j, 1] - py)
        return out

    sparsity = _build_sparsity(
        seg,
        n_res,
        ncam,
        nlm,
        lm_off,
        h_off,
        nsess,
        cam_idx,
        lm_idx,
        h_idx,
        range_pos,
        motion_pairs,
        prior_lots,
        cold_start,
    )
    return residual, seg, active, residual(x0), sparsity


def _build_sparsity(
    seg, n_res, ncam, nlm, lm_off, h_off, nsess, cam_idx, lm_idx, h_idx, range_pos, motion_pairs, prior_lots, cold_start
):
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
    for idx, (a, b) in enumerate(motion_pairs):
        for col in [3 * a, 3 * a + 1, 3 * b, 3 * b + 1]:
            S[m0 + idx, col] = 1

    cr0, _ = seg["camreg"]
    for i in range(3 * ncam):
        S[cr0 + i, i] = 1

    if cold_start:
        g0, _ = seg["gauge"]
        S[g0, lm_off] = 1
        S[g0 + 1, lm_off + 1] = 1
        if nlm >= 2:
            S[g0 + 2, lm_off + 3] = 1
    else:
        p0, _ = seg["prior"]
        for idx, (j, _pos) in enumerate(prior_lots):
            S[p0 + 2 * idx, lm_off + 2 * j] = 1
            S[p0 + 2 * idx + 1, lm_off + 2 * j + 1] = 1

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
    """Solve the observation buffer into ``{lot_id: Solved(x, y, confidence, observation_count)}``.

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

    x0 = _initial_guess(data, priors)
    n_obs_total = len(data["live"])
    mask = np.ones(n_obs_total, dtype=bool)
    ctx = _residual_context(data, priors, mask)

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
                    ctx = _residual_context(data, priors, mask)
                    result, seg, active = _solve_once(data, ctx, result.x)
                    norms = _per_observation_norm(result.fun, seg, active, data)

    return _collect(data, ctx, result, seg, active, norms)


def _collect(data, ctx, result, seg, active, norms):
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
            continue  # every observation for this lot was rejected → no position (caller deletes it)
        mean_res = res_sum[j] / n
        confidence = min(1.0, n / 5.0) * math.exp(-mean_res)
        out[lot_id] = Solved(
            x=float(lms[j, 0]),
            y=float(lms[j, 1]),
            confidence=float(max(0.0, min(1.0, confidence))),
            observation_count=int(n),
        )
    return out


# ── Django glue ────────────────────────────────────────────────────────────────────────────────


def update_positions_for_auction(auction):
    """Re-solve one auction from its live observation buffer and rewrite its LotPosition rows.

    Loads the last-24 h observations, solves, upserts a LotPosition per solved lot, and deletes
    positions whose lot no longer has any surviving observation. Returns the number of solved lots.
    """
    from datetime import timedelta

    from django.utils import timezone

    from auctions.models import LotObservation, LotPosition

    now = timezone.now()
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    rows = LotObservation.objects.filter(auction=auction, captured_at__gte=cutoff).values(
        "lot_id", "session_id", "frame_id", "captured_at", "bearing_deg", "depression_deg", "quality", "fov_calibrated"
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
        )
        for r in rows
    ]

    priors = {p.lot_id: (p.x, p.y) for p in LotPosition.objects.filter(auction=auction)}
    solved = solve_positions(observations, priors, now=now)

    solved_lot_ids = set(solved)
    for lot_id, s in solved.items():
        LotPosition.objects.update_or_create(
            lot_id=lot_id,
            defaults={
                "auction": auction,
                "x": s.x,
                "y": s.y,
                "confidence": s.confidence,
                "observation_count": s.observation_count,
            },
        )
    # Drop positions whose lot no longer has any surviving observation.
    LotPosition.objects.filter(auction=auction).exclude(lot_id__in=solved_lot_ids).delete()
    return len(solved)
