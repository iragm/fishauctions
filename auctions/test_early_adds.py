import datetime

from django.urls import reverse
from django.utils import timezone

from auctions import early_adds
from auctions.models import Auction, AuctionTOS, Lot
from auctions.tests import StandardTestCase


class EarlyAddsTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.start = timezone.now() - datetime.timedelta(days=30)
        Auction.objects.filter(pk=self.in_person_auction.pk).update(promote_this_auction=True, date_start=self.start)
        Lot.objects.filter(auction=self.in_person_auction).delete()
        for number, days_before in enumerate([10] * 3 + [1] * 7):
            lot = Lot.objects.create(
                lot_name=f"lot {number}",
                auction=self.in_person_auction,
                auctiontos_seller=self.admin_in_person_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.in_person_buyer,
            )
            Lot.objects.filter(pk=lot.pk).update(date_posted=self.start - datetime.timedelta(days=days_before))
        AuctionTOS.objects.filter(auction=self.in_person_auction).update(createdon=self.start)
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(createdon=self.start - datetime.timedelta(days=6))

    def point(self, days=5):
        (point,) = [point for point in early_adds.early_adds(days) if point.pk == self.in_person_auction.pk]
        return point

    def test_shares_of_lots_and_people_added_early(self):
        point = self.point()
        self.assertEqual((point.lots, point.early_lots, point.early_lots_pct), (10, 3, 30.0))
        self.assertEqual(point.gross, 100)
        people = AuctionTOS.objects.filter(auction=self.in_person_auction).count()
        self.assertEqual((point.people, point.early_people), (people, 1))
        self.assertEqual(self.point(days=0).early_lots, 10)

    def test_online_unpromoted_and_small_auctions_are_left_out(self):
        Auction.objects.filter(pk=self.in_person_auction.pk).update(promote_this_auction=False)
        self.assertEqual(early_adds.early_adds(), [])
        Auction.objects.filter(pk=self.in_person_auction.pk).update(promote_this_auction=True)
        Lot.objects.filter(auction=self.in_person_auction, lot_name="lot 0").delete()
        self.assertEqual(early_adds.early_adds(), [])

    def test_summary_splits_at_the_median(self):
        def point(pct, gross):
            return early_adds.AuctionPoint(1, "a", "A", self.start, 100, pct, 50, 0, gross)

        summary = early_adds.summarize(
            [point(10, 100), point(20, 200), point(30, 300), point(40, 400)], "early_lots_pct"
        )
        self.assertAlmostEqual(summary["r"], 1)
        self.assertEqual(summary["median"], 25)
        self.assertEqual((summary["above"]["auctions"], summary["above"]["gross"]), (2, 350))
        self.assertIsNone(early_adds.summarize([point(10, 100)], "early_lots_pct"))

    def test_the_page_is_for_site_admins(self):
        self.client.force_login(self.admin_user)
        self.assertNotEqual(self.client.get(reverse("admin_early_adds")).status_code, 200)
        self.admin_user.is_superuser = True
        self.admin_user.save()
        response = self.client.get(reverse("admin_early_adds"), {"days": "7"})
        self.assertContains(response, "Share of lots added early")
        self.assertContains(response, "early-adds-points")
        self.assertEqual(response.context["days"], 7)
