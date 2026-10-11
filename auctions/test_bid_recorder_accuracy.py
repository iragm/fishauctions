from auctions import test_lot_money_fixes as money
from auctions.test_lot_money_fixes import InPersonSaleBase


class SalesRecordedTests(InPersonSaleBase):
    def test_a_sale_recorded_once_is_accurate(self):
        self.sell()
        self.assertEqual(self.sold_lot().sales_recorded, 1)
        self.assertEqual(self.in_person_auction.bid_recorder_accuracy, {"lots": 1, "unchanged": 1, "percent": 100})

    def test_a_corrected_winner_is_not(self):
        self.sell()
        self.sell(action="force_save", price="15", winner="504")
        self.assertEqual(self.sold_lot().sales_recorded, 2)
        self.assertEqual(self.in_person_auction.bid_recorder_accuracy["percent"], 0)

    def test_unsold_and_online_lots_are_not_counted(self):
        self.assertIsNone(self.in_person_auction.bid_recorder_accuracy)
        self.assertIsNone(self.online_auction.bid_recorder_accuracy)


class LotAdminPriceEditTests(InPersonSaleBase):
    url = money.LotAdminTests.url
    data = money.LotAdminTests.data

    def test_changing_only_the_price_counts(self):
        self.sell()
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="12"))
        self.assertEqual(self.sold_lot().winning_price, 12)
        self.assertEqual(self.sold_lot().sales_recorded, 2)

    def test_saving_without_a_change_does_not(self):
        self.sell()
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="10"))
        self.assertEqual(self.sold_lot().sales_recorded, 1)
