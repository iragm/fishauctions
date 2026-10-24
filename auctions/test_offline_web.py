"""Tests for the emergency page: /offline/, its service worker, and the session-signed sync behind it.

The sync rules themselves are the app's and are tested in ``test_mobile_offline``; these cover what
the website adds: the sign-in, the worker, which pages keep a copy, and the links to the page.
"""

import datetime
import json
from decimal import Decimal

from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from auctions.context_processors import offline_auction
from auctions.models import AuctionTOS, Lot, MobileOfflineOp
from auctions.tests import StandardTestCase
from auctions.views.offline import OFFLINE_STATIC_FILES, worker_version


class OfflinePageTests(StandardTestCase):
    def test_page_is_the_same_for_everyone_and_needs_no_network(self):
        anonymous = self.client.get(reverse("offline"))
        self.client.force_login(self.admin_user)
        signed_in = self.client.get(reverse("offline"))
        self.assertEqual(anonymous.status_code, 200)
        # The worker caches one copy per browser, whoever loaded it.
        self.assertEqual(anonymous.content, signed_in.content)
        html = anonymous.content.decode()
        self.assertNotIn("csrfmiddlewaretoken", html)
        self.assertNotIn("https://", html)
        for name in OFFLINE_STATIC_FILES:
            if name.endswith((".css", ".js")):
                self.assertIn(name.rsplit(".", 1)[0].rsplit("/", 1)[-1], html)

    def test_the_cloudflare_copy_loads_nothing_from_the_site(self):
        response = self.client.get(reverse("offline_cloudflare"))
        html = response.content.decode()
        # Cloudflare requires its token, and shows this page only when it can't reach us.
        self.assertIn("::CLOUDFLARE_ERROR_500S_BOX::", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn('rel="stylesheet"', html)
        self.assertNotIn('href="/static/', html)
        self.assertNotIn("@import", html)
        self.assertNotIn("sourceMappingURL", html)
        self.assertIn(f'<meta name="offline-version" content="{worker_version()}">', html)
        self.assertIn("window.OfflineStore", html)
        self.assertIn('id="winner-form"', html)
        # One <script> and one <style>: nothing in the files written in ends either early.
        self.assertEqual(html.count("</script>"), 1)
        self.assertEqual(html.count("</style>"), 2)

    def test_worker_is_served_from_the_root_and_caches_the_page(self):
        response = self.client.get(reverse("offline_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertEqual(reverse("offline_worker").count("/"), 1)
        body = response.content.decode()
        self.assertIn("const OFFLINE_URL = '/offline/'", body)
        for name in OFFLINE_STATIC_FILES:
            self.assertIn(name.rsplit(".", 1)[0], body)

    def test_worker_falls_back_on_outages_but_not_on_a_plain_500(self):
        body = self.client.get(reverse("offline_worker")).content.decode()
        outage = body.split("new Set([", 1)[1].split("])", 1)[0]
        codes = {int(code) for code in outage.split(",")}
        self.assertTrue({502, 503, 504, 522, 524}.issubset(codes))
        self.assertNotIn(500, codes)

    def test_500_page_links_here(self):
        from django.template.loader import render_to_string

        html = render_to_string("500.html", {})
        self.assertIn(reverse("offline"), html)

    def test_the_500_page_never_runs_the_offline_script(self):
        # Django renders it with no request, so it can't tell an admin from a stranger, or the app from a
        # browser: it would forget the admin's copy, or put the worker in the app.
        from django.template.loader import render_to_string

        self.assertNotIn("OfflineStore.onPage", render_to_string("500.html", {}))

    def test_a_database_outage_is_a_503_so_the_worker_steps_in(self):
        from django.db import OperationalError

        from auctions.error_views import error_500

        request = RequestFactory().get("/")
        for error, status in ((OperationalError("gone away"), 503), (ValueError("a bug"), 500)):
            try:
                raise error
            except Exception:
                response = error_500(request)
            self.assertEqual(response.status_code, status, error)

    def test_the_app_unregisters_a_worker_it_picked_up(self):
        response = self.client.get(reverse("help"), HTTP_USER_AGENT="Mozilla/5.0 FishAuctionsApp/1.0 (Flutter; iOS)")
        self.assertContains(response, "r.unregister()")
        self.assertNotContains(response, "OfflineStore.onPage")

    def test_the_help_has_a_section_to_link_to(self):
        url = reverse("help_guide", kwargs={"slug": "run-an-in-person-auction"})
        self.client.force_login(self.admin_user)
        response = self.client.get(url)
        self.assertContains(response, 'id="working-offline"')
        self.assertContains(response, 'href="#working-offline"')
        # The browser fills these in from its own copy; the server never knows what it holds.
        self.assertContains(response, 'id="offline-copy" hidden')
        self.assertContains(response, 'id="offline-not-kept" hidden')
        self.assertContains(response, f"copyStatus({self.admin_user.pk})")
        self.client.logout()
        signed_out = self.client.get(url)
        self.assertContains(signed_out, "copyStatus(null)")
        self.assertNotContains(signed_out, 'id="offline-not-kept"')
        self.client.force_login(self.admin_user)
        app = self.client.get(url, HTTP_USER_AGENT="Mozilla/5.0 FishAuctionsApp/1.0 (Flutter; iOS)")
        self.assertContains(app, 'id="working-offline"')
        self.assertNotContains(app, "offline-copy")

    def test_set_lot_winners_links_here_on_a_timeout(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "timeout: 15000")
        self.assertContains(response, reverse("offline"))


class OfflineSnapshotWebTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("offline_snapshot")
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()

    def test_signed_out_is_refused(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_ping_is_cheap_and_sets_a_csrf_cookie(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url, {"ping": "1"})
        self.assertEqual(response.json(), {"user_id": self.admin_user.pk})
        self.assertIn("csrftoken", response.cookies)
        self.assertIn("no-cache", response["Cache-Control"])

    def test_admin_gets_their_auction_and_who_it_is_for(self):
        self.client.force_login(self.admin_user)
        data = self.client.get(self.url).json()
        self.assertEqual(data["auction"]["slug"], self.in_person_auction.slug)
        self.assertEqual(data["user_id"], self.admin_user.pk)

    def test_an_auction_they_do_not_run_is_null(self):
        self.client.force_login(self.user_who_does_not_join)
        data = self.client.get(self.url, {"auction": self.in_person_auction.slug}).json()
        self.assertIsNone(data["auction"])

    def test_a_jwt_alone_does_not_sign_in(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.admin_user).access_token
        response = self.client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertIn(response.status_code, (401, 403))


class OfflineSyncWebTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("offline_sync")
        self.auction = self.in_person_auction
        self.auction.use_seller_dash_lot_numbering = False
        self.auction.save()
        self.seller = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Seller", bidder_number="801"
        )
        self.buyer = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.in_person_location, name="Buyer", bidder_number="802"
        )
        self.lot = Lot.objects.create(
            lot_name="Guppies", auction=self.auction, auctiontos_seller=self.seller, quantity=1, lot_number_int=801
        )

    def _post(self, ops, client=None, auction=None):
        payload = {"auction": (auction or self.auction).slug, "ops": ops}
        return (client or self.client).post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_signed_out_is_refused(self):
        self.assertIn(self._post([]).status_code, (401, 403))

    def test_csrf_is_enforced(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin_user)
        self.assertEqual(self._post([], client=client).status_code, 403)
        client.get(reverse("offline_snapshot"), {"ping": "1"})
        token = client.cookies["csrftoken"].value
        response = client.post(
            self.url,
            data=json.dumps({"auction": self.auction.slug, "ops": []}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)

    def test_someone_who_does_not_run_it_is_refused(self):
        self.client.force_login(self.user_who_does_not_join)
        self.assertEqual(self._post([]).status_code, 403)

    def test_a_sale_goes_in_once_however_often_it_is_sent(self):
        self.client.force_login(self.admin_user)
        op = {"op_id": "web-w1", "type": "set_winner", "lot": "801", "winner": "802", "winning_price": "15.00"}
        first = self._post([op]).json()
        self.assertEqual(first["results"][0]["status"], "applied")
        self.assertEqual(first["snapshot"]["user_id"], self.admin_user.pk)
        second = self._post([op]).json()
        self.assertEqual(second["results"][0]["status"], "already_applied")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.auctiontos_winner, self.buyer)
        self.assertEqual(self.lot.winning_price, Decimal("15.00"))
        self.assertEqual(MobileOfflineOp.objects.filter(op_id="web-w1").count(), 1)

    def test_a_late_sale_keeps_the_time_it_was_typed(self):
        self.client.force_login(self.admin_user)
        typed = timezone.now() - datetime.timedelta(minutes=40)
        op = {
            "op_id": "web-w2",
            "type": "set_winner",
            "lot": "801",
            "winner": "802",
            "winning_price": "5",
            "recorded_at": typed.isoformat(),
        }
        self._post([op])
        self.lot.refresh_from_db()
        self.assertAlmostEqual(self.lot.date_end.timestamp(), typed.timestamp(), delta=1)

    def test_a_time_in_the_future_or_long_before_the_auction_is_ignored(self):
        self.client.force_login(self.admin_user)
        for number, when in (
            (901, timezone.now() + datetime.timedelta(days=3)),
            (902, self.auction.date_start - datetime.timedelta(days=30)),
        ):
            lot = Lot.objects.create(
                lot_name="Late", auction=self.auction, auctiontos_seller=self.seller, quantity=1, lot_number_int=number
            )
            before = timezone.now()
            op = {
                "op_id": f"web-late-{number}",
                "type": "set_winner",
                "lot": str(number),
                "winner": "802",
                "winning_price": "5",
                "recorded_at": when.isoformat(),
            }
            result = self._post([op]).json()["results"][0]
            self.assertEqual(result["status"], "applied", result)
            lot.refresh_from_db()
            self.assertGreaterEqual(lot.date_end, before)

    def test_an_op_another_sync_already_recorded_is_a_replay_not_a_conflict(self):
        # Two tabs send the same entry at once. Both miss the ledger, the other one commits first, and
        # ours hits the unique op_id: that is a replay, not an entry for the person to fix.
        from unittest import mock

        from django.db import IntegrityError

        from auctions.mobile.services import offline

        MobileOfflineOp.objects.create(op_id="web-w5", auction=self.auction, user=self.admin_user, op_type="set_winner")
        real_filter = MobileOfflineOp.objects.filter
        lookups = []

        def first_lookup_misses(*args, **kwargs):
            lookups.append(kwargs)
            return MobileOfflineOp.objects.none() if len(lookups) == 1 else real_filter(*args, **kwargs)

        op = {"op_id": "web-w5", "type": "set_winner", "lot": "801", "winner": "802", "winning_price": "7"}
        with (
            mock.patch.object(offline.MobileOfflineOp.objects, "filter", side_effect=first_lookup_misses),
            mock.patch.object(offline._OpApplier, "_apply_set_winner", side_effect=IntegrityError("duplicate")),
        ):
            result = offline._OpApplier(self.auction, self.admin_user).apply_one(op)
        self.assertEqual(result["status"], "already_applied")


class OfflineCopyContextTests(StandardTestCase):
    """Which pages keep a copy of an auction for the emergency page."""

    def _slug(self, user, app=False):
        request = RequestFactory().get("/")
        request.user = user
        request.is_mobile_app = app
        return offline_auction(request)["offline_auction"]()

    def setUp(self):
        super().setUp()
        self.in_person_auction.date_start = timezone.now() + datetime.timedelta(hours=3)
        self.in_person_auction.save()
        self.admin_user.userdata.last_auction_used = self.in_person_auction
        self.admin_user.userdata.save()

    def test_the_admin_of_an_in_person_auction_starting_soon(self):
        self.assertEqual(self._slug(self.admin_user), self.in_person_auction.slug)

    def test_not_in_the_app(self):
        self.assertEqual(self._slug(self.admin_user, app=True), "")

    def test_not_for_someone_who_does_not_run_it(self):
        self.user_who_does_not_join.userdata.last_auction_used = self.in_person_auction
        self.user_who_does_not_join.userdata.save()
        self.assertEqual(self._slug(self.user_who_does_not_join), "")

    def test_not_weeks_away(self):
        self.in_person_auction.date_start = timezone.now() + datetime.timedelta(days=20)
        self.in_person_auction.save()
        self.assertEqual(self._slug(self.admin_user), "")

    def test_not_for_an_online_auction(self):
        self.admin_user.userdata.last_auction_used = self.online_auction
        self.admin_user.userdata.save()
        self.assertEqual(self._slug(self.admin_user), "")

    def test_the_page_passes_it_to_the_script(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("help"))
        self.assertContains(response, "OfflineStore.onPage")
        from django.utils.html import escapejs

        self.assertContains(response, f"auction: '{escapejs(self.in_person_auction.slug)}'")

    def test_the_app_never_loads_the_worker(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("help"), HTTP_USER_AGENT="Mozilla/5.0 FishAuctionsApp/1.0 (Flutter; iOS)")
        self.assertNotContains(response, "offline_store.js")
