"""Tests for the mobile offline-mode backend (in-person sale).

Covers GET /api/mobile/offline/snapshot/ and POST /api/mobile/offline/sync/:
snapshot scoping/ordering/status, idempotent replay, requested-number honoring + remap,
cross-batch ``op:<id>`` references, the four conflict rules, per-op independence, and auth/limits.
"""

import json
from decimal import Decimal

from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.models import AuctionTOS, Invoice, Lot, MobileOfflineOp
from auctions.tests import StandardTestCase


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class MobileOfflineSnapshotTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-offline-snapshot")
        # The in-person auction is the natural offline target: admin_user administers it.
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()

    def _get(self, user):
        return self.client.get(self.url, **_bearer(user))

    def test_requires_jwt(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_non_admin_gets_null_auction(self):
        # user_who_does_not_join administers nothing.
        resp = self._get(self.user_who_does_not_join)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["auction"])

    def test_admin_gets_last_auction_used(self):
        resp = self._get(self.admin_user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["auction"]["slug"], self.in_person_auction.slug)

    def test_falls_back_to_most_recent_admin_auction(self):
        # No last_auction_used set for the creator, but they administer both auctions.
        self.user.userdata.last_auction_used = None
        self.user.userdata.save()
        resp = self._get(self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.json()["auction"])

    def test_users_ordered_by_name_with_invoice_status(self):
        AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, name="Zed", bidder_number="900"
        )
        AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, name="Amy", bidder_number="901"
        )
        resp = self._get(self.admin_user)
        names = [u["name"] for u in resp.json()["users"]]
        self.assertEqual(names, sorted(names))
        # A user with no invoice reports NONE.
        amy = next(u for u in resp.json()["users"] if u["name"] == "Amy")
        self.assertEqual(amy["invoice_status"], "NONE")

    def test_invoice_status_reported(self):
        tos = AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Paid Person",
            bidder_number="800",
        )
        Invoice.objects.create(auctiontos_user=tos, auction=self.in_person_auction, status="PAID")
        resp = self._get(self.admin_user)
        row = next(u for u in resp.json()["users"] if u["pk"] == tos.pk)
        self.assertEqual(row["invoice_status"], "PAID")

    def test_lots_exclude_banned_and_deleted(self):
        seller = self.admin_in_person_tos
        good = Lot.objects.create(lot_name="Good", auction=self.in_person_auction, auctiontos_seller=seller, quantity=1)
        Lot.objects.create(
            lot_name="Banned", auction=self.in_person_auction, auctiontos_seller=seller, quantity=1, banned=True
        )
        Lot.objects.create(
            lot_name="Deleted", auction=self.in_person_auction, auctiontos_seller=seller, quantity=1, is_deleted=True
        )
        resp = self._get(self.admin_user)
        pks = {lot["pk"] for lot in resp.json()["lots"]}
        self.assertIn(good.pk, pks)
        names = {lot["lot_name"] for lot in resp.json()["lots"]}
        self.assertNotIn("Banned", names)
        self.assertNotIn("Deleted", names)

    def test_lot_number_display_dash_mode(self):
        # in_person_auction uses use_seller_dash_lot_numbering=True → custom_lot_number strings.
        seller = self.admin_in_person_tos
        lot = Lot.objects.create(
            lot_name="Dash lot", auction=self.in_person_auction, auctiontos_seller=seller, quantity=1
        )
        resp = self._get(self.admin_user)
        row = next(x for x in resp.json()["lots"] if x["pk"] == lot.pk)
        self.assertEqual(row["lot_number"], lot.custom_lot_number)
        self.assertIsInstance(row["lot_number"], str)

    def test_lot_number_display_int_mode(self):
        seller = self.online_tos
        lot = Lot.objects.create(lot_name="Int lot", auction=self.online_auction, auctiontos_seller=seller, quantity=1)
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        resp = self._get(self.user)
        row = next(x for x in resp.json()["lots"] if x["pk"] == lot.pk)
        self.assertEqual(row["lot_number"], str(lot.lot_number_int))


class MobileOfflineSyncTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-offline-sync")
        self.auction = self.in_person_auction
        self.auction.use_seller_dash_lot_numbering = False  # int lot numbers are simpler to assert
        self.auction.save()

    def _post(self, user, ops, auction=None):
        payload = {"auction": (auction or self.auction).slug, "ops": ops}
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json", **_bearer(user))

    @staticmethod
    def _results_by_id(resp):
        return {r["op_id"]: r for r in resp.json()["results"]}

    # -- auth / limits --------------------------------------------------------

    def test_requires_jwt(self):
        resp = self.client.post(
            self.url, data=json.dumps({"auction": self.auction.slug, "ops": []}), content_type="application/json"
        )
        self.assertIn(resp.status_code, (401, 403))

    def test_non_admin_forbidden(self):
        resp = self._post(self.user_who_does_not_join, [])
        self.assertEqual(resp.status_code, 403)

    def test_too_many_ops_is_400(self):
        ops = [{"op_id": str(i), "type": "add_user", "name": f"n{i}", "bidder_number": ""} for i in range(501)]
        resp = self._post(self.admin_user, ops)
        self.assertEqual(resp.status_code, 400)

    def test_response_includes_snapshot(self):
        resp = self._post(self.admin_user, [])
        self.assertEqual(resp.status_code, 200)
        self.assertIn("snapshot", resp.json())
        self.assertEqual(resp.json()["snapshot"]["auction"]["slug"], self.auction.slug)

    # -- add_user -------------------------------------------------------------

    def test_add_user_honors_free_number(self):
        op = {
            "op_id": "u1",
            "type": "add_user",
            "bidder_number": "58",
            "name": "New Guy",
            "email": "",
            "phone_number": "",
        }
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["u1"]
        self.assertEqual(res["status"], "applied")
        self.assertEqual(res["bidder_number"], "58")
        self.assertTrue(AuctionTOS.objects.filter(auction=self.auction, bidder_number="58", name="New Guy").exists())

    def test_add_user_same_number_same_name_already_applied(self):
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Dup Person", bidder_number="70"
        )
        op = {
            "op_id": "u2",
            "type": "add_user",
            "bidder_number": "70",
            "name": "dup person",
            "email": "",
            "phone_number": "",
        }
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["u2"]
        self.assertEqual(res["status"], "already_applied")
        self.assertEqual(res["bidder_number"], "70")
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="70").count(), 1)

    def test_add_user_same_number_different_name_conflict(self):
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Server Person", bidder_number="71"
        )
        op = {
            "op_id": "u3",
            "type": "add_user",
            "bidder_number": "71",
            "name": "Phone Person",
            "email": "",
            "phone_number": "",
        }
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["u3"]
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(res["conflict"], "user_conflict")
        # Server row untouched.
        self.assertEqual(AuctionTOS.objects.get(auction=self.auction, bidder_number="71").name, "Server Person")

    # -- add_lot --------------------------------------------------------------

    def test_add_lot_for_seller(self):
        seller = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Seller", bidder_number="60"
        )
        op = {
            "op_id": "l1",
            "type": "add_lot",
            "seller": "60",
            "lot_number": "301",
            "lot_name": "Bag of plants",
            "quantity": 2,
            "donation": False,
        }
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["l1"]
        self.assertEqual(res["status"], "applied")
        self.assertEqual(res["lot_number"], "301")
        lot = Lot.objects.get(auction=self.auction, auctiontos_seller=seller, lot_name="Bag of plants")
        self.assertEqual(lot.lot_number_int, 301)
        self.assertEqual(lot.quantity, 2)

    def test_add_lot_remaps_taken_number(self):
        seller = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Seller2", bidder_number="61"
        )
        Lot.objects.create(
            lot_name="Existing", auction=self.auction, auctiontos_seller=seller, quantity=1, lot_number_int=400
        )
        op = {
            "op_id": "l2",
            "type": "add_lot",
            "seller": "61",
            "lot_number": "400",
            "lot_name": "Remapped",
            "quantity": 1,
            "donation": False,
        }
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["l2"]
        self.assertEqual(res["status"], "applied")
        self.assertNotEqual(res["lot_number"], "400")  # remapped
        remapped = Lot.objects.get(auction=self.auction, lot_name="Remapped")
        self.assertEqual(str(remapped.lot_number_int), res["lot_number"])

    def test_add_lot_seller_not_found(self):
        op = {"op_id": "l3", "type": "add_lot", "seller": "99999", "lot_number": "500", "lot_name": "x", "quantity": 1}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["l3"]
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(res["conflict"], "not_found")

    def test_add_lot_invoice_not_open(self):
        seller = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="PaidSeller", bidder_number="62"
        )
        Invoice.objects.create(auctiontos_user=seller, auction=self.auction, status="PAID")
        op = {"op_id": "l4", "type": "add_lot", "seller": "62", "lot_number": "600", "lot_name": "x", "quantity": 1}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["l4"]
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(res["conflict"], "invoice_not_open")
        self.assertFalse(Lot.objects.filter(auction=self.auction, lot_name="x").exists())

    # -- set_winner -----------------------------------------------------------

    def _make_lot(self, seller=None, number=700, **kwargs):
        seller = seller or AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name=f"S{number}", bidder_number=str(number)
        )
        return Lot.objects.create(
            lot_name=f"Lot{number}",
            auction=self.auction,
            auctiontos_seller=seller,
            quantity=1,
            lot_number_int=number,
            **kwargs,
        )

    def test_set_winner_applies(self):
        lot = self._make_lot(number=701)
        winner = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Winner", bidder_number="14"
        )
        op = {"op_id": "w1", "type": "set_winner", "lot": "701", "winner": "14", "winning_price": "12.00"}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w1"]
        self.assertEqual(res["status"], "applied")
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_winner_id, winner.pk)
        self.assertEqual(lot.winning_price, Decimal("12.00"))

    def test_set_winner_unsold(self):
        lot = self._make_lot(number=702)
        op = {"op_id": "w2", "type": "set_winner", "lot": "702", "unsold": True}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w2"]
        self.assertEqual(res["status"], "applied")
        lot.refresh_from_db()
        self.assertIsNone(lot.auctiontos_winner_id)
        self.assertFalse(lot.active)

    def test_set_winner_same_winner_price_already_applied(self):
        existing_winner = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="W9", bidder_number="9"
        )
        self._make_lot(number=703, auctiontos_winner=existing_winner, winning_price=Decimal("10.00"))
        op = {"op_id": "w3", "type": "set_winner", "lot": "703", "winner": "9", "winning_price": "10.00"}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w3"]
        self.assertEqual(res["status"], "already_applied")

    def test_set_winner_conflict_names_server_winner(self):
        server_winner = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="ServerW", bidder_number="9"
        )
        lot = self._make_lot(number=704, auctiontos_winner=server_winner, winning_price=Decimal("10.00"))
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="PhoneW", bidder_number="14"
        )
        op = {"op_id": "w4", "type": "set_winner", "lot": "704", "winner": "14", "winning_price": "12.00"}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w4"]
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(res["conflict"], "winner_conflict")
        self.assertIn("9", res["message"])
        self.assertIn("10.00", res["message"])
        # Server row not mutated.
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_winner_id, server_winner.pk)
        self.assertEqual(lot.winning_price, Decimal("10.00"))

    def test_set_winner_unsold_on_sold_lot_conflicts(self):
        server_winner = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="SW", bidder_number="9"
        )
        lot = self._make_lot(number=705, auctiontos_winner=server_winner, winning_price=Decimal("10.00"))
        op = {"op_id": "w5", "type": "set_winner", "lot": "705", "unsold": True}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w5"]
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(res["conflict"], "winner_conflict")
        lot.refresh_from_db()
        self.assertEqual(lot.winning_price, Decimal("10.00"))

    def test_set_winner_lot_not_found(self):
        op = {"op_id": "w6", "type": "set_winner", "lot": "99999", "winner": "14", "winning_price": "12.00"}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w6"]
        self.assertEqual(res["conflict"], "not_found")

    def test_set_winner_winner_invoice_not_open(self):
        lot = self._make_lot(number=706)
        winner = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="PaidWinner", bidder_number="14"
        )
        Invoice.objects.create(auctiontos_user=winner, auction=self.auction, status="UNPAID")
        op = {"op_id": "w7", "type": "set_winner", "lot": "706", "winner": "14", "winning_price": "12.00"}
        resp = self._post(self.admin_user, [op])
        res = self._results_by_id(resp)["w7"]
        self.assertEqual(res["conflict"], "invoice_not_open")
        lot.refresh_from_db()
        self.assertIsNone(lot.auctiontos_winner_id)

    # -- idempotency / references / independence ------------------------------

    def test_idempotent_replay(self):
        op = {
            "op_id": "idem1",
            "type": "add_user",
            "bidder_number": "80",
            "name": "Once",
            "email": "",
            "phone_number": "",
        }
        first = self._results_by_id(self._post(self.admin_user, [op]))["idem1"]
        self.assertEqual(first["status"], "applied")
        self.assertEqual(first["bidder_number"], "80")
        # Resend the same queue after a "dropped" response.
        second = self._results_by_id(self._post(self.admin_user, [op]))["idem1"]
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(second["bidder_number"], "80")
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="80").count(), 1)

    def test_op_reference_within_batch(self):
        ops = [
            {
                "op_id": "ref_u",
                "type": "add_user",
                "bidder_number": "81",
                "name": "RefUser",
                "email": "",
                "phone_number": "",
            },
            {
                "op_id": "ref_l",
                "type": "add_lot",
                "seller": "op:ref_u",
                "lot_number": "301",
                "lot_name": "RefLot",
                "quantity": 1,
                "donation": False,
            },
            {"op_id": "ref_w", "type": "set_winner", "lot": "op:ref_l", "winner": "op:ref_u", "winning_price": "5.00"},
        ]
        results = self._results_by_id(self._post(self.admin_user, ops))
        self.assertEqual(results["ref_u"]["status"], "applied")
        self.assertEqual(results["ref_l"]["status"], "applied")
        self.assertEqual(results["ref_w"]["status"], "applied")
        lot = Lot.objects.get(auction=self.auction, lot_name="RefLot")
        self.assertEqual(lot.winning_price, Decimal("5.00"))

    def test_op_reference_across_batches(self):
        self._post(
            self.admin_user,
            [
                {
                    "op_id": "b1_u",
                    "type": "add_user",
                    "bidder_number": "82",
                    "name": "CrossUser",
                    "email": "",
                    "phone_number": "",
                }
            ],
        )
        resp2 = self._post(
            self.admin_user,
            [
                {
                    "op_id": "b2_l",
                    "type": "add_lot",
                    "seller": "op:b1_u",
                    "lot_number": "310",
                    "lot_name": "CrossLot",
                    "quantity": 1,
                }
            ],
        )
        res = self._results_by_id(resp2)["b2_l"]
        self.assertEqual(res["status"], "applied")
        seller = AuctionTOS.objects.get(auction=self.auction, bidder_number="82")
        self.assertTrue(Lot.objects.filter(auctiontos_seller=seller, lot_name="CrossLot").exists())

    def test_reference_to_conflicted_op_not_found(self):
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Occupant", bidder_number="83"
        )
        ops = [
            # This add_user conflicts (number taken, different name) → never applied.
            {
                "op_id": "bad_u",
                "type": "add_user",
                "bidder_number": "83",
                "name": "Newcomer",
                "email": "",
                "phone_number": "",
            },
            # References the conflicted op → not_found.
            {
                "op_id": "dep_l",
                "type": "add_lot",
                "seller": "op:bad_u",
                "lot_number": "320",
                "lot_name": "Orphan",
                "quantity": 1,
            },
        ]
        results = self._results_by_id(self._post(self.admin_user, ops))
        self.assertEqual(results["bad_u"]["conflict"], "user_conflict")
        self.assertEqual(results["dep_l"]["conflict"], "not_found")

    def test_per_op_independence(self):
        # op 2 conflicts; op 3 still applies.
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Taken", bidder_number="84"
        )
        ops = [
            {"op_id": "i1", "type": "add_user", "bidder_number": "85", "name": "Fine", "email": "", "phone_number": ""},
            {
                "op_id": "i2",
                "type": "add_user",
                "bidder_number": "84",
                "name": "Clash",
                "email": "",
                "phone_number": "",
            },
            {
                "op_id": "i3",
                "type": "add_user",
                "bidder_number": "86",
                "name": "AlsoFine",
                "email": "",
                "phone_number": "",
            },
        ]
        results = self._results_by_id(self._post(self.admin_user, ops))
        self.assertEqual(results["i1"]["status"], "applied")
        self.assertEqual(results["i2"]["status"], "conflict")
        self.assertEqual(results["i3"]["status"], "applied")

    def test_conflict_not_recorded_in_ledger(self):
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Held", bidder_number="87"
        )
        op = {
            "op_id": "nc1",
            "type": "add_user",
            "bidder_number": "87",
            "name": "Other",
            "email": "",
            "phone_number": "",
        }
        self._post(self.admin_user, [op])
        self.assertFalse(MobileOfflineOp.objects.filter(op_id="nc1").exists())
