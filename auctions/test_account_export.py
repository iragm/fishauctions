"""Download my data: what the file contains, what it must never contain, and who may ask for it.

The counterpart to :mod:`auctions.test_account_deletion`. Colorado's and Oregon's privacy acts cover
non-profits where California's CCPA does not, and both pair deletion with a right to a copy, so this
is held to the same standard as the deletion it sits next to in the menu.
"""

import json

from django.test import TestCase
from django.urls import reverse

from auctions.account_export import export, filename
from auctions.models import Bid, Lot, SearchHistory, Watch
from auctions.tests import StandardTestCase


class ExportContentsTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="my_lot", password="testpassword")

    def data(self):
        response = self.client.get(reverse("account_data_export") + "?download=1")
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_the_download_is_a_json_attachment(self):
        response = self.client.get(reverse("account_data_export") + "?download=1")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("my-data", response["Content-Disposition"])

    def test_it_holds_the_account_and_every_preference(self):
        data = self.data()
        self.assertEqual(data["account"]["username"], "my_lot")
        # The profile block walks UserData's fields, so this is a floor, not a fixed list.
        self.assertIn("phone_number", data["profile"])
        self.assertIn("email_visible", data["profile"])

    def test_it_holds_the_auctions_joined_with_the_bidder_number(self):
        numbers = [row["bidder_number"] for row in self.data()["auctions_joined"]]
        self.assertIn("503", numbers)
        self.assertIn("504", numbers)

    def test_a_lot_sold_under_a_bidder_number_is_in_it(self):
        """The fixture's lots are linked by ``auctiontos_seller``, which is how an in-person lot
        arrives. Filtering on ``Lot.user`` alone left them out of their own seller's data."""
        names = [row["name"] for row in self.data()["lots_sold"]]
        self.assertIn("A test lot", names)

    def test_a_lot_won_under_a_bidder_number_is_in_it(self):
        self.client.logout()
        self.client.login(username="no_tos", password="testpassword")
        names = [row["name"] for row in self.data()["lots_won"]]
        self.assertIn("A test lot", names)

    def test_bids_searches_and_watched_lots_are_in_it(self):
        lot = Lot.objects.filter(auction=self.online_auction).first()
        Bid.objects.create(user=self.user, lot_number=lot, amount=7)
        Watch.objects.create(user=self.user, lot_number=lot)
        SearchHistory.objects.create(user=self.user, search="pea puffer")
        data = self.data()
        self.assertEqual([row["amount"] for row in data["bids"]], ["7.00"])
        self.assertEqual(len(data["watched_lots"]), 1)
        self.assertEqual([row["searched_for"] for row in data["searches"]], ["pea puffer"])

    def test_the_browsing_log_is_a_summary_not_every_row(self):
        """PageView is never purged, so a busy organizer's log would be the whole file."""
        browsing = self.data()["browsing"]
        self.assertIn("pages_viewed", browsing)
        self.assertNotIn("url", json.dumps(browsing))

    def test_it_says_what_it_leaves_out(self):
        self.assertTrue(self.data()["not_included"])

    def test_nobody_elses_records_are_in_it(self):
        """The fixture's other users sell and bid in the same auction."""
        body = json.dumps(self.data())
        self.assertNotIn("no_lots", body)
        self.assertNotIn("asdf@example.com", body)


class ExportExcludesCredentialsTests(StandardTestCase):
    """A credential in a downloaded file is a way to lose an account, and is not information about
    the person anyway."""

    def setUp(self):
        super().setUp()
        self.client.login(username="my_lot", password="testpassword")

    def test_no_password_token_or_third_party_key(self):
        from auctions.models import MobileDevice

        MobileDevice.objects.create(
            user=self.user,
            device_uuid="11111111-2222-4333-8444-555555555555",
            device_name="Ira's phone",
            platform="ios",
            fcm_token="SECRET-PUSH-TOKEN",
        )
        body = self.client.get(reverse("account_data_export") + "?download=1").content.decode()
        self.assertIn("Ira's phone", body)
        for secret in ("SECRET-PUSH-TOKEN", "fcm_token", "password", "unsubscribe_link"):
            self.assertNotIn(secret, body)

    def test_the_unsubscribe_token_is_not_in_the_profile_block(self):
        self.assertNotIn("unsubscribe_link", export(self.user)["profile"])


class ExportAccessTests(StandardTestCase):
    def test_signed_out_visitors_are_sent_to_the_login_page(self):
        response = self.client.get(reverse("account_data_export"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_the_download_needs_a_session_too(self):
        response = self.client.get(reverse("account_data_export") + "?download=1")
        self.assertEqual(response.status_code, 302)

    def test_the_page_explains_before_it_offers(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("account_data_export"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "?download=1")
        self.assertContains(response, "credentials, not information about you")


class FilenameTests(TestCase):
    def test_a_username_that_is_not_filename_safe_is_made_safe(self):
        class Fake:
            username = "ira/../etc"

        self.assertNotIn("/", filename(Fake()))


class MenuTests(TestCase):
    def test_the_download_sits_next_to_delete_in_the_account_menu(self):
        from auctions.account_nav import GROUPS

        rows = [row.url_name for group in GROUPS for row in group.rows]
        self.assertIn("account_data_export", rows)
        self.assertEqual(rows.index("account_data_export") + 1, rows.index("account_delete"))
