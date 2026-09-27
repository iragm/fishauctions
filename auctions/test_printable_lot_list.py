"""The printable lot list: every lot on one page, with the columns its labels print."""

import re

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from auctions.models import AuctionRandomOption, Lot
from auctions.tables import natural_sort_key
from auctions.test_label_layout import ALL_LABEL_FIELDS
from auctions.tests import StandardTestCase


class PrintableLotListTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.auction = self.online_auction
        self.url = reverse("auction_printable_lot_list", kwargs={"slug": self.auction.slug})
        self.client.login(username="admin_user", password="testpassword")

    def get(self, **params):
        return self.client.get(self.url, params)

    def headers(self, response):
        return re.findall(r"<th[^>]*>\s*(.*?)\s*</th>", response.content.decode(), re.DOTALL)

    def lot_names(self, response):
        """The lot names in the order the rows print them."""
        names = set(Lot.objects.filter(auction=self.auction).values_list("lot_name", flat=True))
        cells = re.findall(r"<td[^>]*>\s*(.*?)\s*</td>", response.content.decode(), re.DOTALL)
        return [cell for cell in cells if cell in names]

    def test_only_an_admin_can_see_it(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.get().status_code, 403)

    def test_it_is_in_the_more_menu(self):
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        self.assertContains(response, self.url)

    def test_every_lot_prints_on_one_page(self):
        for number in range(40):
            Lot.objects.create(
                lot_name=f"Extra lot {number}", auction=self.auction, auctiontos_seller=self.online_tos, quantity=1
            )
        response = self.get()
        self.assertContains(response, "Extra lot 0")
        self.assertContains(response, "Extra lot 39")
        self.assertContains(response, self.auction.title)

    def test_removed_lots_are_left_off(self):
        Lot.objects.filter(pk=self.lotB.pk).update(banned=True)
        self.assertNotContains(self.get(), "B test lot")

    def test_the_columns_are_the_label_fields(self):
        self.auction.label_print_fields = "lot_name"
        self.auction.save()
        self.assertNotIn("Seller", self.headers(self.get()))
        self.auction.label_print_fields = "lot_name,seller_name"
        self.auction.save()
        self.assertIn("Seller", self.headers(self.get()))

    def test_a_column_nothing_is_in_is_left_off(self):
        self.auction.label_print_fields = "lot_name,buy_now_label,min_bid_label"
        self.auction.save()
        headers = self.headers(self.get())
        # The unsold lot has a minimum bid; nothing has a buy now price.
        self.assertIn("Min bid", headers)
        self.assertNotIn("Buy now", headers)

    def test_a_sold_lot_leaves_off_what_its_label_leaves_off(self):
        Lot.objects.filter(pk=self.lot.pk).update(reserve_price=15)
        self.auction.label_print_fields = "lot_name,min_bid_label"
        self.auction.save()
        response = self.get()
        self.assertContains(response, "$10")  # the unsold lot
        self.assertNotContains(response, "$15")  # sold, so its label has no minimum bid

    def test_sold_lots_show_their_winner(self):
        self.assertIn("Winner", self.headers(self.get()))

    def test_the_custom_field_column_is_named_after_it(self):
        self.auction.custom_field_1 = "allow"
        self.auction.custom_field_1_name = "Origin"
        self.auction.label_print_fields = "lot_name,custom_field_1"
        self.auction.save()
        Lot.objects.filter(pk=self.lot.pk).update(custom_field_1="Lake Malawi")
        response = self.get()
        self.assertIn("Origin", self.headers(response))
        self.assertContains(response, "Lake Malawi")

    def test_a_table_number_sorts_naturally(self):
        for value in ("Table 1", "Table 2", "Table 10"):
            AuctionRandomOption.objects.create(auction=self.auction, user=self.user, value=value)
        self.auction.use_custom_random_field = True
        self.auction.custom_random_name = "Table"
        self.auction.save()
        tables = {self.lot: "Table 10", self.lotB: "Table 2", self.lotC: "Table 1", self.unsoldLot: "Table 2"}
        for lot, table in tables.items():
            Lot.objects.filter(pk=lot.pk).update(custom_random=table)
        response = self.get(sort="custom_random")
        self.assertIn("Table", self.headers(response))
        self.assertEqual(self.lot_names(response), ["C test lot", "B test lot", "Unsold lot", "A test lot"])
        response = self.get(sort="-custom_random")
        self.assertEqual(self.lot_names(response), ["A test lot", "B test lot", "Unsold lot", "C test lot"])

    def test_sorting_over_htmx_returns_just_the_table(self):
        response = self.client.get(self.url, {"sort": "lot_name"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "<h1")
        self.assertContains(response, 'id="table-container"')

    def test_it_does_not_query_per_lot(self):
        self.auction.label_print_fields = ALL_LABEL_FIELDS
        self.auction.save()

        def add_lots():
            for number in range(4):
                Lot.objects.create(
                    lot_name=f"Extra lot {number}",
                    auction=self.auction,
                    auctiontos_seller=self.online_tos,
                    auctiontos_winner=self.tosB,
                    winning_price=5,
                    quantity=1,
                )

        add_lots()
        self.get()
        with CaptureQueriesContext(connection) as before:
            self.get()
        add_lots()
        with CaptureQueriesContext(connection) as after:
            response = self.get()
        self.assertContains(response, "Extra lot 3", count=2)  # one from each batch
        self.assertEqual(len(after.captured_queries), len(before.captured_queries))


class NaturalSortKeyTests(SimpleTestCase):
    def test_numbers_sort_as_numbers(self):
        values = ["Table 10", "table 2", "Table 1", "Table 9", "Back room", "11"]
        self.assertEqual(
            sorted(values, key=natural_sort_key), ["11", "Back room", "Table 1", "table 2", "Table 9", "Table 10"]
        )

    def test_a_seller_dash_lot_number_sorts_by_seller_then_lot(self):
        self.assertEqual(sorted(["12-3", "2-10", "2-9"], key=natural_sort_key), ["2-9", "2-10", "12-3"])
