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

from auctions.ar_mapping import Observation, solve_positions
from auctions.mobile.services import ar as ar_service
from auctions.models import Lot, LotObservation, LotPosition, PageView, ThermalPrinterProfile, Watch
from auctions.tests import StandardTestCase


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def _gen_observations(lots, cams, session_id, *, h=0.65, now=None, age_hours=0.0, quality=1.0, fov=True):
    """Exact synthetic sightings of ``lots`` (id -> (x, y)) from camera poses ``cams``
    ((x, y, theta_rad)). ``h`` is the phone height above the label plane."""
    now = now or timezone.now()
    captured = now - timedelta(hours=age_hours)
    obs = []
    for ci, (cx, cy, theta) in enumerate(cams):
        frame_id = f"{str(session_id)[:6]}f{ci:03d}"
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
