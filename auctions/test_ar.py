"""Tests for Part 3 — AR lot scanning & location mapping, plus the two Part 1/2 follow-up fixes.

* Solver geometry (``auctions.ar_mapping``): synthetic scenes recovered up to a similarity
  transform, outlier rejection, moved-lot convergence, prior-continuity (no flip), metric scale.
* Mobile API (``ar/lots``, ``ar/observations``, ``ar/positions``): auth, caps, per-user flags,
  label fields, clamping/dropping, dirty flag, payload shapes.
* Web: admin-only lot map + data + clear, and the widened QR scan counter.
* Follow-ups: ``/lots/my-last-auction/`` redirect and the ``escpos-raster`` seed width.
"""

import json
import math
import random
import uuid
from datetime import timedelta
from itertools import combinations
from unittest.mock import patch

import numpy as np
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.ar_mapping import (
    ISLAND_GAP_M,
    M_PER_DEG_LAT,
    M_PER_DEG_LON,
    Observation,
    _compass_targets,
    _declination_deg,
    solve_positions,
    update_positions_for_auction,
)
from auctions.mobile.services import ar as ar_service
from auctions.models import Lot, LotObservation, LotPosition, PageView, ThermalPrinterProfile, Watch
from auctions.tests import StandardTestCase


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def _gen_observations(
    lots,
    cams,
    session_id,
    *,
    h=0.65,
    now=None,
    age_hours=0.0,
    quality=1.0,
    fov=True,
    gps=None,
    heading=False,
    declination=0.0,
):
    """Exact synthetic sightings of ``lots`` (id -> (x, y)) from camera poses ``cams``
    ((x, y, theta_rad)). ``h`` is the phone height above the label plane. ``gps`` is an optional
    ``(lat, lon)`` fix stamped on every frame of the session (for island-anchoring tests). When
    ``heading`` is truthy each frame is stamped with the ground-truth *magnetic* compass heading that
    matches its camera θ — the inverse of the solver's conversion, ``H = (90 − deg(θ) − declination)
    % 360`` — so a compass-fed solve should recover the scene's absolute ENU orientation. Default off,
    so every existing call is byte-for-byte unchanged."""
    now = now or timezone.now()
    captured = now - timedelta(hours=age_hours)
    obs = []
    for ci, (cx, cy, theta) in enumerate(cams):
        frame_id = f"{str(session_id)[:6]}f{ci:03d}"
        frame_heading = ((90.0 - math.degrees(theta) - declination) % 360.0) if heading else None
        for lot_id, (lx, ly) in lots.items():
            world_bearing = math.atan2(ly - cy, lx - cx)
            bearing_ccw = world_bearing - theta
            r = math.hypot(lx - cx, ly - cy)
            obs.append(
                Observation(
                    lot_id=lot_id,
                    session_id=str(session_id),
                    frame_id=frame_id,
                    captured_at=captured,
                    bearing_deg=-math.degrees(bearing_ccw),
                    depression_deg=math.degrees(math.atan(h / r)),
                    quality=quality,
                    fov_calibrated=fov,
                    latitude=gps[0] if gps else None,
                    longitude=gps[1] if gps else None,
                    heading_deg=frame_heading,
                )
            )
    return obs


def _similarity_rmse(true_pts, est_pts):
    """RMSE after best-fit similarity (rotation+scale+reflection+translation) alignment."""
    A = np.asarray(true_pts, float)
    B = np.asarray(est_pts, float)
    A0, B0 = A - A.mean(0), B - B.mean(0)
    U, _s, Vt = np.linalg.svd(B0.T @ A0)
    R = U @ Vt
    scale = (B0 @ R * A0).sum() / (A0**2).sum()
    return float(np.sqrt(((B0 - scale * (A0 @ R.T)) ** 2).sum() / len(A))), float(scale)


def _mean_pairwise(pts):
    pts = np.asarray(pts, float)
    return float(np.mean([np.linalg.norm(pts[a] - pts[b]) for a, b in combinations(range(len(pts)), 2)]))


# Square of 4 lots + 4 camera poses looking in at it (a well-conditioned scene).
SQUARE = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (2.0, 2.0), 13: (0.0, 2.0)}
CAMS = [
    (1.0, -2.0, math.radians(80)),
    (4.0, 1.0, math.radians(170)),
    (1.0, 4.0, math.radians(260)),
    (-2.0, 1.0, math.radians(-10)),
]


class ArSolverTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_square_recovered_up_to_similarity(self):
        obs = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now)
        sol = solve_positions(obs, {}, now=self.now)
        self.assertEqual(set(sol), set(SQUARE))
        est = [(sol[k].x, sol[k].y) for k in sorted(SQUARE)]
        true = [SQUARE[k] for k in sorted(SQUARE)]
        rmse, _scale = _similarity_rmse(true, est)
        self.assertLess(rmse, 0.2, f"shape not recovered (RMSE {rmse:.3f} m)")

    def test_outlier_observation_rejected(self):
        obs = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now)
        obs[5] = obs[5]._replace(bearing_deg=obs[5].bearing_deg + 40)  # one badly-wrong detection
        sol = solve_positions(obs, {}, now=self.now)
        est = [(sol[k].x, sol[k].y) for k in sorted(SQUARE)]
        true = [SQUARE[k] for k in sorted(SQUARE)]
        rmse, _ = _similarity_rmse(true, est)
        self.assertLess(rmse, 0.25, f"outlier not rejected (RMSE {rmse:.3f} m)")

    def test_moved_lot_converges_to_new_spot(self):
        session = uuid.uuid4()
        moved_old = dict(SQUARE)
        moved_new = dict(SQUARE)
        moved_new[12] = (4.0, 2.0)  # lot 12 physically relocated
        stale = _gen_observations(moved_old, CAMS, session, now=self.now, age_hours=8.0)  # weight ~0.07
        fresh = _gen_observations(moved_new, CAMS, session, now=self.now, age_hours=0.0)  # weight 1.0
        sol = solve_positions(stale + fresh, {}, now=self.now)
        # Align est → new truth, then lot 12 should sit nearer its NEW spot than its OLD one.
        true_new = [moved_new[k] for k in sorted(moved_new)]
        est = [(sol[k].x, sol[k].y) for k in sorted(moved_new)]
        A = np.asarray(true_new, float)
        B = np.asarray(est, float)
        A0, B0 = A - A.mean(0), B - B.mean(0)
        U, _s, Vt = np.linalg.svd(B0.T @ A0)
        R = U @ Vt
        scale = (B0 @ R * A0).sum() / (A0**2).sum()
        est_aligned = (B - B.mean(0)) @ R / scale + A.mean(0)
        idx = sorted(moved_new).index(12)
        d_new = np.linalg.norm(est_aligned[idx] - np.array(moved_new[12]))
        d_old = np.linalg.norm(est_aligned[idx] - np.array(moved_old[12]))
        self.assertLess(d_new, d_old, f"moved lot didn't follow fresh scans (new {d_new:.2f} vs old {d_old:.2f})")

    def test_priors_keep_frame_no_flip(self):
        obs = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now)
        first = solve_positions(obs, {}, now=self.now)
        priors = {k: (first[k].x, first[k].y) for k in first}
        obs2 = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now)
        second = solve_positions(obs2, priors, now=self.now)
        max_move = max(math.hypot(second[k].x - priors[k][0], second[k].y - priors[k][1]) for k in priors)
        self.assertLess(max_move, 0.3, f"prior solve rotated/flipped the frame (max move {max_move:.3f} m)")

    def test_depression_recovers_roughly_metric_scale(self):
        obs = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now, h=0.65)
        sol = solve_positions(obs, {}, now=self.now)
        est = [(sol[k].x, sol[k].y) for k in sorted(SQUARE)]
        true = [SQUARE[k] for k in sorted(SQUARE)]
        scale = _mean_pairwise(est) / _mean_pairwise(true)
        self.assertGreater(scale, 0.6, f"scale collapsed ({scale:.2f})")
        self.assertLess(scale, 1.4, f"scale blew up ({scale:.2f})")

    def test_empty_returns_nothing(self):
        self.assertEqual(solve_positions([], {}, now=self.now), {})

    def test_stale_observations_excluded(self):
        obs = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now, age_hours=48.0)  # > 24h window
        self.assertEqual(solve_positions(obs, {}, now=self.now), {})


# Two clusters ~6 m apart in +x; a single session walks A -> B seeing one lot per frame.
CLUSTER_A = {10: (0.0, 0.0), 11: (1.0, 0.0), 12: (0.5, 0.8)}
CLUSTER_B = {20: (6.0, 0.0), 21: (7.0, 0.0), 22: (6.5, 0.8)}
_ALL_CLUSTER_LOTS = {**CLUSTER_A, **CLUSTER_B}


def _face(cam, lot):
    return math.atan2(lot[1] - cam[1], lot[0] - cam[0])


def _walk_observations(session, *, with_yaw=True, drift_deg_per_min=0.0, now=None, h=0.65):
    """One-detection-per-frame walk across CLUSTER_A then CLUSTER_B: each lot is seen from two camera
    stations (a baseline), and the camera turns to face each label, so the reported cumulative yaw
    tracks the true heading (plus optional gyro drift). ``with_yaw=False`` reproduces the old app."""
    now = now or timezone.now()
    stations = [
        ((0.0, -2.0), [10, 11, 12]),
        ((1.0, -2.0), [10, 11, 12]),
        ((5.5, -2.0), [20, 21, 22]),
        ((6.5, -2.0), [20, 21, 22]),
    ]
    frames = [(cam, lid) for cam, lids in stations for lid in lids]
    theta0 = _face(frames[0][0], _ALL_CLUSTER_LOTS[frames[0][1]])
    obs = []
    drift = 0.0
    dt_s = 4.0
    n = len(frames)
    for i, (cam, lid) in enumerate(frames):
        theta = _face(cam, _ALL_CLUSTER_LOTS[lid])
        drift += random.uniform(-1, 1) * drift_deg_per_min * (dt_s / 60.0)
        yaw = (math.degrees(theta - theta0) + drift) if with_yaw else None
        lx, ly = _ALL_CLUSTER_LOTS[lid]
        r = math.hypot(lx - cam[0], ly - cam[1])
        obs.append(
            Observation(
                lot_id=lid,
                session_id=str(session),
                frame_id=f"{str(session)[:6]}f{i:03d}",
                captured_at=now - timedelta(seconds=(n - 1 - i) * dt_s),
                bearing_deg=0.0,
                depression_deg=math.degrees(math.atan(h / r)),
                quality=1.0,
                fov_calibrated=True,
                yaw_deg=yaw,
            )
        )
    return obs


def _ab_direction_error_deg(sol):
    """Angle error of the recovered A->B direction after removing the free global rotation.

    Best-fit-rotate the estimate onto truth (whole map, no per-cluster freedom), then compare the
    A-centroid -> B-centroid bearing. With a session-rigid frame (yaw) the two islands share one
    rotation so this is small; without yaw each island floats and it is large."""
    ids = sorted(_ALL_CLUSTER_LOTS)
    true = np.array([_ALL_CLUSTER_LOTS[k] for k in ids], float)
    est = np.array([(sol[k].x, sol[k].y) for k in ids], float)
    t0 = true - true.mean(0)
    e0 = est - est.mean(0)
    U, _s, Vt = np.linalg.svd(e0.T @ t0)
    if np.linalg.det(U @ Vt) < 0:
        U[:, -1] *= -1  # forbid reflection
    est_aligned = e0 @ (U @ Vt)
    a_idx = [ids.index(k) for k in CLUSTER_A]
    b_idx = [ids.index(k) for k in CLUSTER_B]
    v_est = est_aligned[b_idx].mean(0) - est_aligned[a_idx].mean(0)
    v_true = t0[b_idx].mean(0) - t0[a_idx].mean(0)
    diff = math.atan2(v_est[1], v_est[0]) - math.atan2(v_true[1], v_true[0])
    return abs(math.degrees((diff + math.pi) % (2 * math.pi) - math.pi))


class ArHeadingOdometryTests(TestCase):
    """Gyro yaw as heading odometry: a one-label-per-frame walk between two tables now recovers the
    relative direction between them — the exact case that is unconstrained without yaw."""

    def setUp(self):
        self.now = timezone.now()
        random.seed(1234)

    def test_walk_recovers_cross_table_direction_with_yaw(self):
        sol = solve_positions(_walk_observations(uuid.uuid4(), now=self.now), {}, now=self.now)
        self.assertGreaterEqual(set(sol), set(_ALL_CLUSTER_LOTS))
        self.assertLess(_ab_direction_error_deg(sol), 6.0)

    def test_without_yaw_direction_is_unconstrained(self):
        """Pin the value of the feature: the same scan, minus yaw, does NOT recover the direction."""
        sol = solve_positions(_walk_observations(uuid.uuid4(), with_yaw=False, now=self.now), {}, now=self.now)
        self.assertGreaterEqual(set(sol), set(_ALL_CLUSTER_LOTS))
        self.assertGreater(_ab_direction_error_deg(sol), 15.0)

    def test_yaw_drift_still_converges(self):
        sol = solve_positions(_walk_observations(uuid.uuid4(), drift_deg_per_min=2.0, now=self.now), {}, now=self.now)
        self.assertGreaterEqual(set(sol), set(_ALL_CLUSTER_LOTS))
        self.assertLess(_ab_direction_error_deg(sol), 8.0)

    def test_yawless_session_no_regression(self):
        """Old-app data (no yaw) still solves the well-conditioned co-visible scene as before."""
        obs = _gen_observations(SQUARE, CAMS, uuid.uuid4(), now=self.now)  # yaw_deg defaults to None
        sol = solve_positions(obs, {}, now=self.now)
        self.assertEqual(set(sol), set(SQUARE))
        est = [(sol[k].x, sol[k].y) for k in sorted(SQUARE)]
        rmse, _scale = _similarity_rmse([SQUARE[k] for k in sorted(SQUARE)], est)
        self.assertLess(rmse, 0.2)


class ArComponentTests(TestCase):
    """Disconnected scans become distinct islands (non-overlapping); a linking walk merges them."""

    def setUp(self):
        self.now = timezone.now()

    def test_two_disjoint_sessions_form_distinct_nonoverlapping_islands(self):
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (1.0, 1.5)}
        b = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (1.0, 1.5)}
        obs = _gen_observations(a, CAMS, uuid.uuid4(), now=self.now) + _gen_observations(
            b, CAMS, uuid.uuid4(), now=self.now
        )
        sol = solve_positions(obs, {}, now=self.now)
        comp_a = {sol[k].component for k in a}
        comp_b = {sol[k].component for k in b}
        self.assertEqual(len(comp_a), 1)
        self.assertEqual(len(comp_b), 1)
        self.assertNotEqual(comp_a, comp_b)
        ax = [sol[k].x for k in a]
        bx = [sol[k].x for k in b]
        # Bounding boxes are disjoint in x (islands laid out side by side, never interleaved).
        self.assertTrue(max(ax) < min(bx) or max(bx) < min(ax))
        self.assertGreater(abs(min(bx) - max(ax)) if max(ax) < min(bx) else abs(min(ax) - max(bx)), ISLAND_GAP_M - 5)

    def test_linking_walk_merges_islands_into_one(self):
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (1.0, 1.5)}
        b = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (1.0, 1.5)}
        link = {10: (0.0, 0.0), 20: (2.0, 0.0)}  # one lot from each cluster, co-visible in one session
        obs = (
            _gen_observations(a, CAMS, uuid.uuid4(), now=self.now)
            + _gen_observations(b, CAMS, uuid.uuid4(), now=self.now)
            + _gen_observations(link, CAMS, uuid.uuid4(), now=self.now)
        )
        sol = solve_positions(obs, {}, now=self.now)
        self.assertEqual(len({sol[k].component for k in list(a) + list(b)}), 1)

    def test_gps_anchors_disconnected_islands_by_location(self):
        # Two disjoint scans ~100 m apart north-south. Without GPS they'd be marched ~20 m apart in x;
        # with GPS their bases reflect the real separation (distance and direction).
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (1.0, 1.5)}
        b = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (1.0, 1.5)}
        lat0, lon0 = 40.0, -75.0
        obs = _gen_observations(a, CAMS, uuid.uuid4(), now=self.now, gps=(lat0, lon0)) + _gen_observations(
            b, CAMS, uuid.uuid4(), now=self.now, gps=(lat0 + 100.0 / M_PER_DEG_LAT, lon0)
        )
        sol = solve_positions(obs, {}, now=self.now)
        ca = np.array([(sol[k].x, sol[k].y) for k in a]).mean(0)
        cb = np.array([(sol[k].x, sol[k].y) for k in b]).mean(0)
        d = cb - ca
        self.assertAlmostEqual(float(np.hypot(*d)), 100.0, delta=15.0)
        self.assertGreater(abs(d[1]), abs(d[0]) * 3)  # separation is mostly north (y), not east (x)

    def test_gps_east_west_separation(self):
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (1.0, 1.5)}
        b = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (1.0, 1.5)}
        lat0, lon0 = 40.0, -75.0
        dlon = 100.0 / (M_PER_DEG_LON * math.cos(math.radians(lat0)))  # 100 m east
        obs = _gen_observations(a, CAMS, uuid.uuid4(), now=self.now, gps=(lat0, lon0)) + _gen_observations(
            b, CAMS, uuid.uuid4(), now=self.now, gps=(lat0, lon0 + dlon)
        )
        sol = solve_positions(obs, {}, now=self.now)
        ca = np.array([(sol[k].x, sol[k].y) for k in a]).mean(0)
        cb = np.array([(sol[k].x, sol[k].y) for k in b]).mean(0)
        d = cb - ca
        self.assertAlmostEqual(float(np.hypot(*d)), 100.0, delta=15.0)
        self.assertGreater(abs(d[0]), abs(d[1]) * 3)  # separation is mostly east (x)

    def test_gps_island_sits_past_prior_map(self):
        # A prior-anchored island fixes the map frame; a disconnected GPS island lands past it, never
        # on top of it (GPS never disturbs the established map).
        priors = {10: (0.0, 0.0), 11: (2.0, 0.0)}
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (1.0, 1.5)}  # 2 prior lots → prior island
        b = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (1.0, 1.5)}  # cold GPS island
        obs = _gen_observations(a, CAMS, uuid.uuid4(), now=self.now) + _gen_observations(
            b, CAMS, uuid.uuid4(), now=self.now, gps=(40.0, -75.0)
        )
        sol = solve_positions(obs, priors, now=self.now)
        self.assertGreater(min(sol[k].x for k in b), max(sol[k].x for k in a))

    def test_no_gps_keeps_marched_layout(self):
        # Without GPS the disconnected islands still march side by side (unchanged behaviour).
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (1.0, 1.5)}
        b = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (1.0, 1.5)}
        obs = _gen_observations(a, CAMS, uuid.uuid4(), now=self.now) + _gen_observations(
            b, CAMS, uuid.uuid4(), now=self.now
        )
        sol = solve_positions(obs, {}, now=self.now)
        ax = [sol[k].x for k in a]
        bx = [sol[k].x for k in b]
        self.assertTrue(max(ax) < min(bx) or max(bx) < min(ax))


def _wrap_deg(x):
    """Wrap degrees to (−180, 180]."""
    return (x + 180.0) % 360.0 - 180.0


def _rot_pts(pts, ang):
    c, s = math.cos(ang), math.sin(ang)
    return {k: (c * x - s * y, s * x + c * y) for k, (x, y) in pts.items()}


def _rot_cams(cams, ang):
    c, s = math.cos(ang), math.sin(ang)
    return [(c * x - s * y, s * x + c * y, th + ang) for (x, y, th) in cams]


class ArCompassHeadingTests(TestCase):
    """Absolute compass heading as a soft island-orientation prior. GPS anchors *where* an island
    sits; the compass anchors *which way it faces* — the one thing bearings + GPS cannot fix on a
    disconnected island. Magnetic→true is corrected via WMM declination (patched here for
    determinism)."""

    def setUp(self):
        self.now = timezone.now()

    def _targets(self, headings, *, gps=None):
        """Run the frame-heading → camera-θ conversion (:func:`_compass_targets`) on a minimal data
        stub: one frame per heading, cam indices 0..n-1."""
        keys = [("s", f"f{i}") for i in range(len(headings))]
        data = {
            "frame_heading": dict(zip(keys, headings)),
            "frame_gps": dict.fromkeys(keys, gps) if gps else {},
            "cam_index": {k: i for i, k in enumerate(keys)},
        }
        return _compass_targets(data, self.now)

    def test_heading_to_theta_conversion(self):
        # ENU world (east=+x, north=+y): a compass heading H points along (sin H, cos H), whose
        # ccw-from-+x angle is 90°−H. No GPS ⇒ declination 0.
        targets = self._targets([0.0, 90.0])
        self.assertAlmostEqual(targets[0], math.pi / 2)  # north ⇒ θ = +π/2
        self.assertAlmostEqual(targets[1], 0.0)  # east ⇒ θ = 0

    def test_declination_applied_before_conversion(self):
        # With +10° east declination, a magnetic heading of 80° is true 90° (east) ⇒ θ = 0.
        with patch("auctions.ar_mapping._declination_deg", return_value=10.0):
            targets = self._targets([80.0], gps=(40.0, -75.0))
        self.assertAlmostEqual(targets[0], 0.0)

    def test_disconnected_islands_recover_absolute_orientation(self):
        # Two disjoint sessions (no shared lots) ~100 m apart. Island A's internal axis runs due east
        # in ENU; island B's identical scene is rotated 90° so its axis runs due north. GPS alone
        # leaves each island's rotation free; the compass pins it. Declination patched to 0.
        lat0, lon0 = 40.0, -75.0
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (2.0, 2.0), 13: (0.0, 2.0)}
        b_base = {20: (0.0, 0.0), 21: (2.0, 0.0), 22: (2.0, 2.0), 23: (0.0, 2.0)}
        ang = math.radians(90)
        b = _rot_pts(b_base, ang)
        with patch("auctions.ar_mapping._declination_deg", return_value=0.0):
            obs = _gen_observations(
                a, CAMS, uuid.uuid4(), now=self.now, gps=(lat0, lon0), heading=True
            ) + _gen_observations(
                b,
                _rot_cams(CAMS, ang),
                uuid.uuid4(),
                now=self.now,
                gps=(lat0 + 100.0 / M_PER_DEG_LAT, lon0),
                heading=True,
            )
            sol = solve_positions(obs, {}, now=self.now)
        self.assertGreaterEqual(set(sol), set(a) | set(b))
        # Two separate islands.
        self.assertNotEqual({sol[k].component for k in a}, {sol[k].component for k in b})
        # A's 10→11 axis is due east (0°); B's 20→21 axis is due north (+90°) — absolutely, no
        # per-island rotation freedom removed.
        dir_a = math.degrees(math.atan2(sol[11].y - sol[10].y, sol[11].x - sol[10].x))
        dir_b = math.degrees(math.atan2(sol[21].y - sol[20].y, sol[21].x - sol[20].x))
        self.assertLess(abs(_wrap_deg(dir_a - 0.0)), 5.0, f"island A axis off ({dir_a:.1f}°)")
        self.assertLess(abs(_wrap_deg(dir_b - 90.0)), 5.0, f"island B axis off ({dir_b:.1f}°)")

    def test_declination_rotates_recovered_island(self):
        # Same scene, declination 0 vs +10°. θ_target = wrap(π/2 − rad(H + D)) = θ_true − rad(D), so a
        # +10° declination rotates the recovered island −10°.
        lat0, lon0 = 40.0, -75.0
        a = {10: (0.0, 0.0), 11: (2.0, 0.0), 12: (2.0, 2.0), 13: (0.0, 2.0)}

        def solve_with_decl(decl):
            with patch("auctions.ar_mapping._declination_deg", return_value=decl):
                obs = _gen_observations(a, CAMS, uuid.uuid4(), now=self.now, gps=(lat0, lon0), heading=True)
                return solve_positions(obs, {}, now=self.now)

        sol0 = solve_with_decl(0.0)
        sol10 = solve_with_decl(10.0)
        dir0 = math.degrees(math.atan2(sol0[11].y - sol0[10].y, sol0[11].x - sol0[10].x))
        dir10 = math.degrees(math.atan2(sol10[11].y - sol10[10].y, sol10[11].x - sol10[10].x))
        self.assertAlmostEqual(_wrap_deg(dir10 - dir0), -10.0, delta=3.0)

    def test_declination_smoke_pittsburgh_and_garbage(self):
        # Real WMM2025 lookup: Pittsburgh sits at roughly −9° (west) declination in 2026.
        when = self.now.replace(year=2026, month=7, day=1)
        d = _declination_deg(40.44, -79.99, when)
        self.assertGreater(d, -13.0)
        self.assertLess(d, -5.0)
        # Garbage coordinates must never raise a solve to death — they yield 0.0 (no correction).
        self.assertEqual(_declination_deg(999.0, -79.99, when), 0.0)


class ArApiBaseTestCase(StandardTestCase):
    """Shared fixtures: three unsold lots in the online auction + a JWT-authing client helper."""

    def setUp(self):
        super().setUp()
        cache.clear()  # dirty registry / recommended cache / throttles must not bleed across tests
        self.auction = self.online_auction
        self.auction.label_print_fields = "custom_field_1,custom_checkbox_label,custom_dropdown_label"
        self.auction.custom_field_1 = "allow"
        self.auction.custom_field_1_name = "Notes"
        self.auction.use_custom_checkbox_field = True
        self.auction.custom_checkbox_name = "CARES"
        self.auction.use_custom_dropdown_field = "allow"
        self.auction.custom_dropdown_name = "Table"
        self.auction.save()
        self.lot_a = Lot.objects.create(
            lot_name="Apisto pair",
            auction=self.auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            custom_field_1="Wild caught",
            custom_checkbox=True,
            custom_dropdown="3",
        )
        self.lot_b = Lot.objects.create(
            lot_name="Cory school",
            auction=self.auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        self.lot_c = Lot.objects.create(
            lot_name="Java fern",
            auction=self.auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        # A lot in a different auction (self.lot from StandardTestCase is in online_auction; make one
        # in the in-person auction for the cross-auction case).
        self.other_auction_lot = Lot.objects.create(
            lot_name="Stray lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            quantity=1,
        )


class ArLotsEndpointTests(ArApiBaseTestCase):
    def _get(self, user, pks, auction=None):
        url = reverse("mobile-ar-lots")
        lots = ",".join(str(p) for p in pks)
        return self.client.get(f"{url}?auction={(auction or self.auction).slug}&lots={lots}", **_bearer(user))

    def test_requires_jwt(self):
        url = reverse("mobile-ar-lots")
        resp = self.client.get(f"{url}?auction={self.auction.slug}&lots={self.lot_a.pk}")
        self.assertEqual(resp.status_code, 401)

    def test_missing_auction_404(self):
        url = reverse("mobile-ar-lots")
        resp = self.client.get(f"{url}?auction=does-not-exist&lots=1", **_bearer(self.user))
        self.assertEqual(resp.status_code, 404)

    def test_label_fields_honor_print_fields_and_skip_empty(self):
        resp = self._get(self.user, [self.lot_a.pk, self.lot_b.pk])
        rows = {row["pk"]: row for row in resp.json()["lots"]}
        self.assertEqual(
            rows[self.lot_a.pk]["label_fields"],
            [
                {"label": "Notes", "value": "Wild caught"},
                {"label": "CARES", "value": "CARES"},
                {"label": "Table", "value": "3"},
            ],
        )
        # lot_b has no custom values → no label fields.
        self.assertEqual(rows[self.lot_b.pk]["label_fields"], [])

    def test_label_fields_respect_print_fields_membership(self):
        self.auction.label_print_fields = "lot_name,seller_name"  # no custom fields enabled for labels
        self.auction.save()
        resp = self._get(self.user, [self.lot_a.pk])
        self.assertEqual(resp.json()["lots"][0]["label_fields"], [])

    def test_watched_and_recommended_flags(self):
        Watch.objects.create(user=self.user, lot_number=self.lot_a)
        with patch("auctions.filters.get_recommended_lots", return_value=Lot.objects.filter(pk=self.lot_b.pk)):
            resp = self._get(self.user, [self.lot_a.pk, self.lot_b.pk])
        rows = {row["pk"]: row for row in resp.json()["lots"]}
        self.assertTrue(rows[self.lot_a.pk]["watched"])
        self.assertFalse(rows[self.lot_a.pk]["recommended"])
        self.assertFalse(rows[self.lot_b.pk]["watched"])
        self.assertTrue(rows[self.lot_b.pk]["recommended"])

    def test_cross_auction_pk(self):
        resp = self._get(self.user, [self.other_auction_lot.pk])
        row = resp.json()["lots"][0]
        self.assertFalse(row["in_auction"])
        self.assertEqual(row["name"], "Stray lot")

    def test_unknown_pk_removed(self):
        resp = self._get(self.user, [99999999])
        row = resp.json()["lots"][0]
        self.assertFalse(row["in_auction"])
        self.assertTrue(row["removed"])
        self.assertIsNone(row["name"])

    def test_batch_cap(self):
        pks = list(range(1, 80))  # 79 > 50
        resp = self._get(self.user, pks)
        self.assertLessEqual(len(resp.json()["lots"]), ar_service.MAX_LOTS_PER_METADATA_CALL)

    def test_has_position_flag(self):
        LotPosition.objects.create(lot=self.lot_a, auction=self.auction, x=1, y=2, confidence=0.5)
        resp = self._get(self.user, [self.lot_a.pk, self.lot_b.pk])
        rows = {row["pk"]: row for row in resp.json()["lots"]}
        self.assertTrue(rows[self.lot_a.pk]["has_position"])
        self.assertFalse(rows[self.lot_b.pk]["has_position"])

    def test_image_url_full_size_when_image_present(self):
        # The preview card renders the picture fit-to-width, so it needs the full display image, not
        # just the 250x150 thumbnail. A lot with no image reports image_url: None.
        from auctions.models import LotImage

        LotImage.objects.create(
            lot_number=self.lot_a, url="https://example.com/apisto.jpg", image_source="RANDOM", is_primary=True
        )
        rows = {row["pk"]: row for row in self._get(self.user, [self.lot_a.pk, self.lot_b.pk]).json()["lots"]}
        self.assertEqual(rows[self.lot_a.pk]["image_url"], "https://example.com/apisto.jpg")
        self.assertIsNone(rows[self.lot_b.pk]["image_url"])


class MobileLotWatchEndpointTests(ArApiBaseTestCase):
    """POST /api/mobile/lots/<pk>/watch/ — watch/unwatch from the AR preview card (JWT auth)."""

    def _post(self, user, lot, watch):
        return self.client.post(
            reverse("mobile-lot-watch", kwargs={"pk": lot.pk}),
            data=json.dumps({"watch": watch}),
            content_type="application/json",
            **_bearer(user),
        )

    def test_requires_jwt(self):
        resp = self.client.post(
            reverse("mobile-lot-watch", kwargs={"pk": self.lot_a.pk}),
            data=json.dumps({"watch": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_watch_then_unwatch(self):
        resp = self._post(self.user, self.lot_a, True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"watched": True})
        self.assertTrue(Watch.objects.filter(user=self.user, lot_number=self.lot_a).exists())
        resp = self._post(self.user, self.lot_a, False)
        self.assertEqual(resp.json(), {"watched": False})
        self.assertFalse(Watch.objects.filter(user=self.user, lot_number=self.lot_a).exists())

    def test_watch_is_idempotent(self):
        self._post(self.user, self.lot_a, True)
        self._post(self.user, self.lot_a, True)  # a retry must not create a duplicate row
        self.assertEqual(Watch.objects.filter(user=self.user, lot_number=self.lot_a).count(), 1)

    def test_unwatch_when_not_watching_is_noop(self):
        resp = self._post(self.user, self.lot_a, False)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"watched": False})

    def test_missing_lot_404(self):
        resp = self.client.post(
            reverse("mobile-lot-watch", kwargs={"pk": 99999999}),
            data=json.dumps({"watch": True}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(resp.status_code, 404)

    def test_missing_watch_field_400(self):
        resp = self.client.post(
            reverse("mobile-lot-watch", kwargs={"pk": self.lot_a.pk}),
            data=json.dumps({}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(resp.status_code, 400)


class LotPageBackToArBannerTests(ArApiBaseTestCase):
    """The web lot page shows a sticky "Back to AR" bar only when opened from AR mode inside the app
    (``?src=ar`` + FishAuctionsApp UA)."""

    APP_UA = "FishAuctionsApp/1.0 (iOS)"

    def _url(self, lot):
        return reverse("lot_by_pk", kwargs={"pk": lot.pk})

    def test_banner_shown_in_app_from_ar(self):
        self.client.force_login(self.user)
        html = self.client.get(f"{self._url(self.lot_a)}?src=ar", HTTP_USER_AGENT=self.APP_UA).content.decode()
        self.assertIn("Back to AR", html)
        self.assertIn(f"fishauctions://ar/{self.auction.slug}?locate={self.lot_a.pk}", html)

    def test_banner_absent_on_web(self):
        self.client.force_login(self.user)
        html = self.client.get(f"{self._url(self.lot_a)}?src=ar", HTTP_USER_AGENT="Mozilla/5.0").content.decode()
        self.assertNotIn("Back to AR", html)

    def test_banner_absent_in_app_without_src(self):
        self.client.force_login(self.user)
        html = self.client.get(self._url(self.lot_a), HTTP_USER_AGENT=self.APP_UA).content.decode()
        self.assertNotIn("Back to AR", html)


class ArObservationsEndpointTests(ArApiBaseTestCase):
    def _post(self, user, payload):
        return self.client.post(
            reverse("mobile-ar-observations"),
            data=json.dumps(payload),
            content_type="application/json",
            **_bearer(user),
        )

    def _batch(self, detections, *, frame_id="f001", captured_at=None, fov=68.0, session=None):
        captured_at = captured_at or timezone.now()
        payload = {
            "auction": self.auction.slug,
            "session_id": str(session or uuid.uuid4()),
            "frames": [{"frame_id": frame_id, "captured_at": captured_at.isoformat(), "detections": detections}],
        }
        if fov is not None:
            payload["fov_hdeg"] = fov
        return payload

    def test_requires_jwt(self):
        resp = self.client.post(
            reverse("mobile-ar-observations"), data=json.dumps(self._batch([])), content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401)

    def test_creates_rows_and_sets_dirty_flag(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": -12.5, "depression_deg": 28.9, "quality": 0.8}]
        resp = self._post(self.user, self._batch(det))
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 1})
        self.assertEqual(LotObservation.objects.filter(auction=self.auction).count(), 1)
        self.assertTrue(cache.get(ar_service.ar_dirty_key(self.auction.pk)))
        self.assertIn(self.auction.pk, cache.get(ar_service.AR_DIRTY_REGISTRY_KEY))

    def test_non_rfc_variant_session_id_accepted(self):
        # Regression: MariaDB's native `uuid` type rejects any UUID whose variant nibble (17th hex
        # digit) is 0-7 with OperationalError 1292, which silently 500'd ~half of the app's randomly
        # generated session ids. session_id is now a plain varchar, so any opaque token is stored as-is.
        bad_session = "f61b0b5b-78a5-cced-758a-38fc30f3c5a8"  # variant nibble '7' — was rejected
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        resp = self._post(self.user, self._batch(det, session=bad_session))
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 1})
        self.assertEqual(LotObservation.objects.get(auction=self.auction).session_id, bad_session)

    def test_future_captured_at_clamped(self):
        future = timezone.now() + timedelta(hours=5)
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        self._post(self.user, self._batch(det, captured_at=future))
        obs = LotObservation.objects.get(auction=self.auction)
        self.assertLessEqual(obs.captured_at, timezone.now() + timedelta(seconds=1))

    def test_out_of_range_dropped_valid_kept(self):
        det = [
            {"lot": self.lot_a.pk, "bearing_deg": 200.0, "depression_deg": 20.0},  # bearing out of range → drop
            {"lot": self.lot_b.pk, "bearing_deg": 10.0, "depression_deg": 200.0},  # depression out of range → drop
            {"lot": self.lot_c.pk, "bearing_deg": 5.0, "depression_deg": 15.0},  # valid → kept
        ]
        resp = self._post(self.user, self._batch(det))
        self.assertEqual(resp.json()["accepted"], 1)
        self.assertEqual(LotObservation.objects.count(), 1)
        self.assertEqual(LotObservation.objects.get().lot_id, self.lot_c.pk)

    def test_cross_auction_lot_dropped(self):
        det = [{"lot": self.other_auction_lot.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        resp = self._post(self.user, self._batch(det))
        self.assertEqual(resp.json()["accepted"], 0)
        self.assertEqual(LotObservation.objects.count(), 0)

    def test_fov_present_absent_sets_calibrated(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        self._post(self.user, self._batch(det, fov=70.0))
        self.assertTrue(LotObservation.objects.latest("id").fov_calibrated)
        self._post(self.user, self._batch(det, fov=None, frame_id="f002"))
        self.assertFalse(LotObservation.objects.latest("id").fov_calibrated)

    def test_frame_cap_enforced(self):
        frames = [
            {"frame_id": f"f{i:03d}", "captured_at": timezone.now().isoformat(), "detections": []}
            for i in range(ar_service.MAX_FRAMES_PER_BATCH + 1)
        ]
        payload = {"auction": self.auction.slug, "session_id": str(uuid.uuid4()), "frames": frames}
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 400)

    def test_detection_cap_enforced(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}] * (
            ar_service.MAX_DETECTIONS_PER_FRAME + 1
        )
        resp = self._post(self.user, self._batch(det))
        self.assertEqual(resp.status_code, 400)

    # --- yaw_deg (gyro heading) ----------------------------------------------
    def test_yaw_persisted_on_every_detection_row(self):
        det = [
            {"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0},
            {"lot": self.lot_b.pk, "bearing_deg": 5.0, "depression_deg": 22.0},
        ]
        payload = self._batch(det)
        payload["frames"][0]["yaw_deg"] = -93.46
        resp = self._post(self.user, payload)
        self.assertEqual(resp.json()["accepted"], 2)
        # Every detection row of the frame stores the frame's yaw.
        yaws = list(LotObservation.objects.filter(auction=self.auction).values_list("yaw_deg", flat=True))
        self.assertEqual(yaws, [-93.46, -93.46])

    def test_yaw_optional_defaults_null(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        self._post(self.user, self._batch(det))  # no yaw_deg key at all
        self.assertIsNone(LotObservation.objects.get(auction=self.auction).yaw_deg)

    def test_yaw_null_tolerated(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["yaw_deg"] = None
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        self.assertIsNone(LotObservation.objects.get(auction=self.auction).yaw_deg)

    def test_yaw_junk_clamped_to_null(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["yaw_deg"] = 99999.0  # abs() > 36000 -> dropped as "unknown"
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        self.assertIsNone(LotObservation.objects.get(auction=self.auction).yaw_deg)

    def test_large_but_valid_yaw_kept(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["yaw_deg"] = 1080.0  # three left turns; cumulative + unwrapped, valid
        self._post(self.user, payload)
        self.assertEqual(LotObservation.objects.get(auction=self.auction).yaw_deg, 1080.0)

    # --- GPS (island anchoring) ----------------------------------------------
    def test_gps_persisted_on_every_detection_row(self):
        det = [
            {"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0},
            {"lot": self.lot_b.pk, "bearing_deg": 5.0, "depression_deg": 22.0},
        ]
        payload = self._batch(det)
        payload["frames"][0]["latitude"] = 40.12
        payload["frames"][0]["longitude"] = -75.34
        resp = self._post(self.user, payload)
        self.assertEqual(resp.json()["accepted"], 2)
        rows = LotObservation.objects.filter(auction=self.auction)
        self.assertEqual({(r.latitude, r.longitude) for r in rows}, {(40.12, -75.34)})

    def test_gps_optional_defaults_null(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        self._post(self.user, self._batch(det))  # no lat/lon keys at all
        obs = LotObservation.objects.get(auction=self.auction)
        self.assertIsNone(obs.latitude)
        self.assertIsNone(obs.longitude)

    def test_gps_zero_zero_dropped(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["latitude"] = 0.0  # classic "no fix" sentinel
        payload["frames"][0]["longitude"] = 0.0
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        obs = LotObservation.objects.get(auction=self.auction)
        self.assertIsNone(obs.latitude)
        self.assertIsNone(obs.longitude)

    def test_gps_out_of_range_dropped_not_400(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["latitude"] = 200.0  # out of range
        payload["frames"][0]["longitude"] = -75.0
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        obs = LotObservation.objects.get(auction=self.auction)
        self.assertIsNone(obs.latitude)
        self.assertIsNone(obs.longitude)

    def test_gps_half_supplied_dropped(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["latitude"] = 40.0  # longitude missing → drop the whole fix
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        obs = LotObservation.objects.get(auction=self.auction)
        self.assertIsNone(obs.latitude)
        self.assertIsNone(obs.longitude)

    # --- heading_deg (absolute compass heading) ------------------------------
    def test_heading_persisted_on_every_detection_row(self):
        det = [
            {"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0},
            {"lot": self.lot_b.pk, "bearing_deg": 5.0, "depression_deg": 22.0},
        ]
        payload = self._batch(det)
        payload["frames"][0]["heading_deg"] = 137.5
        resp = self._post(self.user, payload)
        self.assertEqual(resp.json()["accepted"], 2)
        # Every detection row of the frame stores the frame's heading.
        headings = list(LotObservation.objects.filter(auction=self.auction).values_list("heading_deg", flat=True))
        self.assertEqual(headings, [137.5, 137.5])

    def test_heading_optional_defaults_null(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        self._post(self.user, self._batch(det))  # no heading_deg key at all
        self.assertIsNone(LotObservation.objects.get(auction=self.auction).heading_deg)

    def test_heading_null_tolerated(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["heading_deg"] = None
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        self.assertIsNone(LotObservation.objects.get(auction=self.auction).heading_deg)

    def test_heading_junk_dropped_to_null(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["heading_deg"] = 9999.0  # outside [-360, 360] → dropped as "unknown"
        resp = self._post(self.user, payload)
        self.assertEqual(resp.status_code, 202)
        self.assertIsNone(LotObservation.objects.get(auction=self.auction).heading_deg)

    def test_heading_negative_normalized_to_range(self):
        det = [{"lot": self.lot_a.pk, "bearing_deg": 0.0, "depression_deg": 20.0}]
        payload = self._batch(det)
        payload["frames"][0]["heading_deg"] = -90.0  # a valid bearing; normalized to [0, 360)
        self._post(self.user, payload)
        self.assertEqual(LotObservation.objects.get(auction=self.auction).heading_deg, 270.0)


class ArPositionsEndpointTests(ArApiBaseTestCase):
    def _get(self, user, auction=None):
        url = reverse("mobile-ar-positions")
        return self.client.get(f"{url}?auction={(auction or self.auction).slug}", **_bearer(user))

    def test_requires_jwt(self):
        url = reverse("mobile-ar-positions")
        self.assertEqual(self.client.get(f"{url}?auction={self.auction.slug}").status_code, 401)

    def test_empty_auction_null_updated_at(self):
        data = self._get(self.user).json()
        self.assertEqual(data["positions"], [])
        self.assertIsNone(data["updated_at"])
        self.assertEqual(data["unsold_with_position"], 0)
        self.assertEqual(data["island_count"], 0)

    def test_island_count_counts_distinct_components(self):
        # Two lots in component 1, one in component 2 → two islands. Sold/removed lots are excluded
        # from positions, so they never inflate the count.
        LotPosition.objects.create(lot=self.lot_a, auction=self.auction, x=1, y=1, confidence=0.6, component=1)
        LotPosition.objects.create(lot=self.lot_b, auction=self.auction, x=2, y=2, confidence=0.6, component=1)
        LotPosition.objects.create(lot=self.lot_c, auction=self.auction, x=9, y=9, confidence=0.6, component=2)
        self.assertEqual(self._get(self.user).json()["island_count"], 2)

    def test_island_count_single_component(self):
        LotPosition.objects.create(lot=self.lot_a, auction=self.auction, x=1, y=1, confidence=0.6, component=4)
        LotPosition.objects.create(lot=self.lot_b, auction=self.auction, x=2, y=2, confidence=0.6, component=4)
        self.assertEqual(self._get(self.user).json()["island_count"], 1)

    def test_sold_and_removed_excluded(self):
        LotPosition.objects.create(lot=self.lot_a, auction=self.auction, x=1, y=1, confidence=0.6)
        LotPosition.objects.create(lot=self.lot_b, auction=self.auction, x=2, y=2, confidence=0.6)
        LotPosition.objects.create(lot=self.lot_c, auction=self.auction, x=3, y=3, confidence=0.6)
        # lot_b sold, lot_c removed → only lot_a remains in the payload.
        self.lot_b.winning_price = 5
        self.lot_b.auctiontos_winner = self.tosB
        self.lot_b.save()
        self.lot_c.banned = True
        self.lot_c.save()
        data = self._get(self.user).json()
        lot_ids = {p["lot"] for p in data["positions"]}
        self.assertEqual(lot_ids, {self.lot_a.pk})
        self.assertEqual(data["unsold_with_position"], 1)
        self.assertIsNotNone(data["updated_at"])


class ArUpdatePositionsCommandTests(ArApiBaseTestCase):
    def test_command_solves_dirty_and_prunes_old(self):
        session = uuid.uuid4()
        lots = {self.lot_a.pk: (0.0, 0.0), self.lot_b.pk: (2.0, 0.0), self.lot_c.pk: (2.0, 2.0)}
        cams = [(1.0, -2.0, math.radians(80)), (3.0, 1.0, math.radians(175)), (1.0, 3.0, math.radians(265))]
        for obs in _gen_observations(lots, cams, session, now=timezone.now()):
            LotObservation.objects.create(
                auction=self.auction,
                lot_id=obs.lot_id,
                session_id=obs.session_id,
                frame_id=obs.frame_id,
                captured_at=obs.captured_at,
                bearing_deg=obs.bearing_deg,
                depression_deg=obs.depression_deg,
                quality=obs.quality,
                fov_calibrated=obs.fov_calibrated,
            )
        ar_service.mark_auction_dirty(self.auction)
        # A stale observation that should be pruned by the command.
        LotObservation.objects.create(
            auction=self.auction,
            lot_id=self.lot_a.pk,
            session_id=uuid.uuid4(),
            frame_id="old",
            captured_at=timezone.now() - timedelta(hours=48),
            bearing_deg=0,
            depression_deg=20,
            quality=1,
        )
        call_command("update_ar_positions")
        self.assertEqual(LotPosition.objects.filter(auction=self.auction).count(), 3)
        self.assertFalse(LotObservation.objects.filter(frame_id="old").exists())  # pruned


class ArPersistenceTests(ArApiBaseTestCase):
    """The map must not dissolve overnight: stale positions are kept (they still serve as merge
    anchors), only sold/removed lots are dropped, and persistent island ids survive + merge."""

    def _extra_lots(self):
        return [
            Lot.objects.create(
                lot_name=f"extra {i}", auction=self.auction, auctiontos_seller=self.online_tos, quantity=1
            )
            for i in range(3)
        ]

    def _store(self, observations):
        LotObservation.objects.bulk_create(
            [
                LotObservation(
                    auction=self.auction,
                    lot_id=o.lot_id,
                    session_id=o.session_id,
                    frame_id=o.frame_id,
                    captured_at=o.captured_at,
                    bearing_deg=o.bearing_deg,
                    depression_deg=o.depression_deg,
                    quality=o.quality,
                    fov_calibrated=o.fov_calibrated,
                    yaw_deg=o.yaw_deg,
                )
                for o in observations
            ]
        )

    def test_positions_survive_observation_expiry(self):
        lots = {self.lot_a.pk: (0.0, 0.0), self.lot_b.pk: (2.0, 0.0), self.lot_c.pk: (2.0, 2.0)}
        self._store(_gen_observations(lots, CAMS, uuid.uuid4(), now=timezone.now()))
        self.assertEqual(update_positions_for_auction(self.auction), 3)
        self.assertEqual(LotPosition.objects.filter(auction=self.auction).count(), 3)
        # All observations expire and get pruned; the next solve sees nothing new but KEEPS positions.
        LotObservation.objects.filter(auction=self.auction).delete()
        update_positions_for_auction(self.auction)
        self.assertEqual(LotPosition.objects.filter(auction=self.auction).count(), 3)

    def test_sold_and_removed_lots_lose_positions(self):
        for lot in (self.lot_a, self.lot_b, self.lot_c):
            LotPosition.objects.create(lot=lot, auction=self.auction, x=1, y=1, confidence=0.5)
        self.lot_b.winning_price = 5
        self.lot_b.auctiontos_winner = self.tosB
        self.lot_b.save()
        self.lot_c.banned = True
        self.lot_c.save()
        update_positions_for_auction(self.auction)  # no live observations
        remaining = set(LotPosition.objects.filter(auction=self.auction).values_list("lot_id", flat=True))
        self.assertEqual(remaining, {self.lot_a.pk})  # sold + removed dropped, unscanned lot_a kept

    def test_moved_lot_relocates_after_fresh_scans(self):
        lots = {self.lot_a.pk: (0.0, 0.0), self.lot_b.pk: (2.0, 0.0), self.lot_c.pk: (2.0, 2.0)}
        self._store(_gen_observations(lots, CAMS, uuid.uuid4(), now=timezone.now()))
        update_positions_for_auction(self.auction)
        before = LotPosition.objects.get(lot=self.lot_c)
        # lot_c physically relocated; fresh scans of the new layout arrive.
        moved = dict(lots)
        moved[self.lot_c.pk] = (4.0, 0.0)
        self._store(_gen_observations(moved, CAMS, uuid.uuid4(), now=timezone.now()))
        update_positions_for_auction(self.auction)
        after = LotPosition.objects.get(lot=self.lot_c)
        self.assertTrue(before.x != after.x or before.y != after.y)  # it did move

    def test_component_ids_persist_and_merge(self):
        d, e, f = self._extra_lots()
        cams = [(1, -2, math.radians(80)), (3, 1, math.radians(175)), (1, 3, math.radians(265))]
        cluster_a = {self.lot_a.pk: (0.0, 0.0), self.lot_b.pk: (2.0, 0.0), self.lot_c.pk: (1.0, 1.5)}
        cluster_b = {d.pk: (0.0, 0.0), e.pk: (2.0, 0.0), f.pk: (1.0, 1.5)}
        self._store(_gen_observations(cluster_a, cams, uuid.uuid4(), now=timezone.now()))
        self._store(_gen_observations(cluster_b, cams, uuid.uuid4(), now=timezone.now()))
        update_positions_for_auction(self.auction)
        comp_a = {LotPosition.objects.get(lot_id=pk).component for pk in cluster_a}
        comp_b = {LotPosition.objects.get(lot_id=pk).component for pk in cluster_b}
        self.assertEqual(len(comp_a), 1)
        self.assertEqual(len(comp_b), 1)
        self.assertNotEqual(comp_a, comp_b)
        # A later linking walk sees one lot from each cluster together -> islands merge to one id.
        link = {self.lot_a.pk: (0.0, 0.0), d.pk: (2.0, 0.0)}
        self._store(_gen_observations(link, cams, uuid.uuid4(), now=timezone.now()))
        update_positions_for_auction(self.auction)
        merged = {LotPosition.objects.get(lot_id=pk).component for pk in list(cluster_a) + list(cluster_b)}
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged, {min(comp_a | comp_b)})  # smaller id survives


class ArWebMapTests(ArApiBaseTestCase):
    def test_map_admin_only(self):
        url = reverse("auction_lot_map", kwargs={"slug": self.auction.slug})
        # admin ok
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(url).status_code, 200)
        # a plain buyer (non-admin TOS) is forbidden
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_data_admin_only_and_shape(self):
        LotPosition.objects.create(lot=self.lot_a, auction=self.auction, x=1, y=2, confidence=0.7)
        url = reverse("auction_lot_map_data", kwargs={"slug": self.auction.slug})
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin_user)
        data = self.client.get(url).json()
        self.assertEqual(data["positions"][0]["lot"], self.lot_a.pk)
        self.assertIn("lot_number", data["positions"][0])
        self.assertTrue(any(row["pk"] == self.lot_a.pk for row in data["lots"]))

    def test_clear_admin_only_and_wipes(self):
        LotObservation.objects.create(
            auction=self.auction,
            lot_id=self.lot_a.pk,
            session_id=uuid.uuid4(),
            frame_id="f",
            captured_at=timezone.now(),
            bearing_deg=0,
            depression_deg=20,
            quality=1,
        )
        LotPosition.objects.create(lot=self.lot_a, auction=self.auction, x=1, y=2, confidence=0.7)
        url = reverse("auction_lot_map_clear", kwargs={"slug": self.auction.slug})
        # buyer forbidden
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertTrue(LotPosition.objects.filter(auction=self.auction).exists())
        # admin clears both tables
        self.client.force_login(self.admin_user)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(LotObservation.objects.filter(auction=self.auction).exists())
        self.assertFalse(LotPosition.objects.filter(auction=self.auction).exists())

    def test_qr_scan_counter_counts_ar(self):
        # A QR-sourced view and an AR-sourced view both count toward the scanned tally.
        PageView.objects.create(lot_number=self.lot_a, source="qr")
        PageView.objects.create(lot_number=self.lot_b, source="ar")
        PageView.objects.create(lot_number=self.lot_c, source="")  # not a scan
        self.assertEqual(self.auction.number_of_lots_with_scanned_qr, 2)


class FollowUpFixTests(StandardTestCase):
    def test_escpos_raster_seed_width_is_384(self):
        # The seed constant and (via the migrations) the DB row are the full 58 mm head, not 96.
        seed = next(
            p
            for p in __import__("auctions.printer_programs", fromlist=["SEED_PROFILES"]).SEED_PROFILES
            if p["slug"] == "escpos-raster"
        )
        self.assertEqual(seed["print_width_px"], 384)
        row = ThermalPrinterProfile.objects.filter(slug="escpos-raster").first()
        if row:  # seeded by migration
            self.assertEqual(row.print_width_px, 384)

    def test_my_last_auction_requires_login(self):
        resp = self.client.get(reverse("my_last_auction_lots"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url + reverse("account_login"))

    def test_my_last_auction_redirects_to_last(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self.client.force_login(self.user)
        resp = self.client.get(reverse("my_last_auction_lots"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"{reverse('allLots')}?auction={self.online_auction.slug}")

    def test_my_last_auction_plain_when_unset(self):
        self.user.userdata.last_auction_used = None
        self.user.userdata.save()
        self.client.force_login(self.user)
        resp = self.client.get(reverse("my_last_auction_lots"))
        self.assertEqual(resp.url, reverse("allLots"))

    def test_my_last_auction_plain_when_deleted(self):
        self.online_auction.is_deleted = True
        self.online_auction.save()
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self.client.force_login(self.user)
        resp = self.client.get(reverse("my_last_auction_lots"))
        self.assertEqual(resp.url, reverse("allLots"))
