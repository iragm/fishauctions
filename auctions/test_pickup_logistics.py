"""Online auction pickups: holding lots for the second pickup, the day-before email, the logistics page,
the admin's post-auction banner, and the help chart of how far ahead auctions are created.
"""

import datetime
import io
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from post_office.models import Email

from auctions import logistics
from auctions.models import Auction, AuctionHistory, AuctionTOS, Invoice, Lot, PickupLocation, User
from auctions.services import join_auction, lot_add_block
from auctions.tests import StandardTestCase


class PickupTestCase(StandardTestCase):
    """A two-location online auction that has ended: MA has one pickup, VT has a second."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        cls.organizer = User.objects.create_user(username="organizer", password="testpassword", email="o@example.com")
        cls.multi = Auction.objects.create(
            created_by=cls.organizer,
            title="Two state auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=7),
            date_end=now - datetime.timedelta(hours=3),
        )
        cls.ma = PickupLocation.objects.create(
            name="MA", auction=cls.multi, pickup_time=now + datetime.timedelta(hours=20)
        )
        cls.vt = PickupLocation.objects.create(
            name="VT",
            auction=cls.multi,
            pickup_time=now + datetime.timedelta(hours=20),
            second_pickup_time=now + datetime.timedelta(days=3),
        )
        cls.ma_seller = AuctionTOS.objects.create(
            auction=cls.multi, pickup_location=cls.ma, name="Ma Seller", bidder_number="1"
        )
        cls.vt_seller = AuctionTOS.objects.create(
            auction=cls.multi, pickup_location=cls.vt, name="Vt Seller", bidder_number="2"
        )
        cls.buyer_user = User.objects.create_user(username="vtbuyer", password="testpassword", email="b@example.com")
        cls.vt_buyer = AuctionTOS.objects.create(
            user=cls.buyer_user,
            auction=cls.multi,
            pickup_location=cls.vt,
            name="Vt Buyer",
            bidder_number="3",
            email="b@example.com",
        )

    def sell(self, seller, winner, price=10):
        return Lot.objects.create(
            lot_name="lot",
            auction=self.multi,
            auctiontos_seller=seller,
            auctiontos_winner=winner,
            winning_price=price,
            active=False,
            date_end=timezone.now() - datetime.timedelta(hours=3),
        )

    def invoice_for(self, tos):
        return Invoice.objects.get_or_create(auctiontos_user=tos, auction=self.multi)[0]

    def pay(self, tos):
        Invoice.objects.filter(pk=self.invoice_for(tos).pk).update(status="PAID")


class HoldForSecondPickupTests(PickupTestCase):
    def test_only_offered_where_the_location_has_a_second_pickup(self):
        self.assertTrue(self.vt_buyer.can_hold_for_second_pickup)
        self.assertFalse(self.ma_seller.can_hold_for_second_pickup)
        self.assertFalse(self.online_tos.can_hold_for_second_pickup)

    def test_holding_stops_selling_and_selling_stops_holding(self):
        tos, _, _ = join_auction(self.buyer_user, self.multi, self.vt, hold_for_second_pickup=True)
        self.assertTrue(tos.hold_for_second_pickup)
        self.assertFalse(tos.can_sell)
        code, _message = lot_add_block(self.multi, tos, is_admin=False, bulk=False)
        self.assertEqual(code, "selling_not_allowed")
        # Unchecking is always allowed.
        tos, _, _ = join_auction(self.buyer_user, self.multi, self.vt, hold_for_second_pickup=False)
        self.assertFalse(tos.hold_for_second_pickup)
        self.assertTrue(tos.can_sell)
        # Once they have lots, it can't be turned on.
        self.sell(tos, self.ma_seller)
        tos, _, _ = join_auction(self.buyer_user, self.multi, self.vt, hold_for_second_pickup=True)
        self.assertFalse(tos.hold_for_second_pickup)

    def test_the_lot_form_leaves_out_an_auction_they_hold_in(self):
        Auction.objects.filter(pk=self.multi.pk).update(
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=1),
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=1),
        )
        join_auction(self.buyer_user, self.multi, self.vt)
        self.assertIn(self.multi, self.buyer_user.userdata.available_auctions_to_submit_lots)
        join_auction(self.buyer_user, self.multi, self.vt, hold_for_second_pickup=True)
        self.assertNotIn(self.multi, User.objects.get(pk=self.buyer_user.pk).userdata.available_auctions_to_submit_lots)

    def test_moving_to_a_location_without_a_second_pickup_drops_the_hold(self):
        join_auction(self.buyer_user, self.multi, self.vt, hold_for_second_pickup=True)
        tos, _, _ = join_auction(self.buyer_user, self.multi, self.ma)
        self.assertFalse(tos.hold_for_second_pickup)

    def test_the_rules_page_offers_the_box(self):
        self.multi.date_end = timezone.now() + datetime.timedelta(days=1)
        self.multi.save()
        self.client.force_login(self.buyer_user)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.multi.slug}))
        self.assertContains(response, "Hold lots purchased at my location for the second pickup time")
        self.assertContains(response, "Checking this will prevent you from selling in this auction")
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertNotContains(response, "Hold lots purchased")

    def test_the_label_and_invoice_say_hold(self):
        AuctionTOS.objects.filter(pk=self.vt_buyer.pk).update(hold_for_second_pickup=True)
        lot = self.sell(self.vt_seller, AuctionTOS.objects.get(pk=self.vt_buyer.pk))
        self.assertIn("hold for second pickup", Lot.objects.get(pk=lot.pk).winner_location)
        invoice = self.invoice_for(self.vt_buyer)
        self.client.force_login(self.organizer)
        response = self.client.get(invoice.get_absolute_url())
        self.assertContains(response, "Hold for second pickup")


class PickupPlanTests(PickupTestCase):
    def plan(self, tos):
        return AuctionTOS.objects.get(pk=tos.pk).pickup_plan

    def test_plans(self):
        self.assertEqual(self.plan(self.vt_buyer), "")
        # Only from their own location.
        self.sell(self.vt_seller, self.vt_buyer)
        self.assertEqual(self.plan(self.vt_buyer), AuctionTOS.PICKUP_FIRST)
        # And from elsewhere, unpaid: both.
        self.sell(self.ma_seller, self.vt_buyer)
        self.assertEqual(self.plan(self.vt_buyer), AuctionTOS.PICKUP_BOTH)
        # Holding: only the second, paid or not.
        AuctionTOS.objects.filter(pk=self.vt_buyer.pk).update(hold_for_second_pickup=True)
        self.assertEqual(self.plan(self.vt_buyer), AuctionTOS.PICKUP_SECOND)
        self.pay(self.vt_buyer)
        self.assertEqual(self.plan(self.vt_buyer), AuctionTOS.PICKUP_SECOND)
        # A seller always comes to the first.
        self.sell(self.vt_buyer, self.ma_seller)
        self.assertEqual(self.plan(self.vt_buyer), AuctionTOS.PICKUP_BOTH)
        # No second pickup at their location: the first.
        self.assertEqual(self.plan(self.ma_seller), AuctionTOS.PICKUP_FIRST)

    def test_paid_with_only_lots_from_elsewhere_skips_the_first(self):
        self.sell(self.ma_seller, self.vt_buyer)
        self.pay(self.vt_buyer)
        self.assertEqual(self.plan(self.vt_buyer), AuctionTOS.PICKUP_SECOND)


class PickupTomorrowEmailTests(PickupTestCase):
    def run_command(self):
        call_command("auctiontos_notifications", stdout=io.StringIO())

    def test_sent_once_the_day_before_to_people_with_lots(self):
        AuctionTOS.objects.filter(auction=self.multi).update(confirm_email_sent=True, print_reminder_email_sent=True)
        self.sell(self.ma_seller, self.vt_buyer)
        self.pay(self.vt_buyer)
        self.run_command()
        mail = Email.objects.filter(to="b@example.com", template__name="online_pickup_tomorrow")
        self.assertEqual(mail.count(), 1)
        self.assertIn("Pickup tomorrow", mail.first().subject)
        self.assertIn("don't need to come", mail.first().message)
        self.run_command()
        self.assertEqual(mail.count(), 1)

    def test_waits_for_overtime(self):
        AuctionTOS.objects.filter(auction=self.multi).update(confirm_email_sent=True, print_reminder_email_sent=True)
        self.sell(self.ma_seller, self.vt_buyer)
        running = self.sell(self.ma_seller, None, price=None)
        Lot.objects.filter(pk=running.pk).update(date_end=timezone.now() + datetime.timedelta(minutes=10))
        self.run_command()
        self.assertFalse(AuctionTOS.objects.get(pk=self.vt_buyer.pk).second_confirm_email_sent)
        Lot.objects.filter(pk=running.pk).update(date_end=timezone.now() - datetime.timedelta(minutes=1))
        self.run_command()
        self.assertTrue(AuctionTOS.objects.get(pk=self.vt_buyer.pk).second_confirm_email_sent)

    def test_nothing_won_or_sold_gets_nothing(self):
        AuctionTOS.objects.filter(auction=self.multi).update(confirm_email_sent=True, print_reminder_email_sent=True)
        self.run_command()
        self.assertFalse(Email.objects.filter(template__name="online_pickup_tomorrow").exists())


class LogisticsPageTests(PickupTestCase):
    def names(self, key):
        tab = next(tab for tab in logistics.tabs(self.multi) if tab.key == key)
        return {tos.name: tos.pickup_now for tos in logistics.people(self.multi, tab)}

    def test_tabs(self):
        labels = [tab.label for tab in logistics.tabs(self.multi)]
        self.assertEqual(sorted(labels), ["MA", "VT - First", "VT - Second"])

    def test_sellers_come_first_and_held_lots_wait(self):
        self.sell(self.vt_seller, self.vt_buyer)
        self.sell(self.ma_seller, self.vt_buyer)
        vt = self.vt.pk
        self.assertEqual(self.names(f"{vt}-1"), {"Vt Buyer": 1, "Vt Seller": 0})
        self.assertEqual(self.names(f"{vt}-2"), {"Vt Buyer": 1})
        AuctionTOS.objects.filter(pk=self.vt_buyer.pk).update(hold_for_second_pickup=True)
        self.assertEqual(self.names(f"{vt}-1"), {"Vt Seller": 0})
        self.assertEqual(self.names(f"{vt}-2"), {"Vt Buyer": 2})
        self.assertEqual(self.names(f"{self.ma.pk}-1"), {"Ma Seller": 0})

    def test_unpaid_buyers_come_to_the_first_to_pay(self):
        self.sell(self.ma_seller, self.vt_buyer)
        vt = self.vt.pk
        self.assertEqual(self.names(f"{vt}-1"), {"Vt Buyer": 0})
        self.pay(self.vt_buyer)
        self.assertEqual(self.names(f"{vt}-1"), {})
        self.assertEqual(self.names(f"{vt}-2"), {"Vt Buyer": 1})

    def test_page_and_marking_off(self):
        self.sell(self.vt_seller, self.vt_buyer)
        url = reverse("auction_logistics", kwargs={"slug": self.multi.slug})
        self.client.force_login(self.buyer_user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.organizer)
        response = self.client.get(f"{url}?tab={self.vt.pk}-1")
        self.assertContains(response, "VT - Second")
        self.assertContains(response, "Vt Buyer")
        self.assertContains(response, "Sold lots")
        response = self.client.get(f"{url}?tab={self.vt.pk}-2")
        self.assertNotContains(response, "Sold lots")
        mark = reverse("auction_logistics_mark", kwargs={"slug": self.multi.slug, "pk": self.vt_buyer.pk, "which": 1})
        response = self.client.post(mark, {"marked": "1"})
        self.assertContains(response, "text-decoration-line-through")
        # A second volunteer's stale tap doesn't undo it.
        self.client.post(mark, {"marked": "1"})
        tos = AuctionTOS.objects.get(pk=self.vt_buyer.pk)
        self.assertIsNotNone(tos.marked_off_first_pickup)
        self.assertIsNone(tos.marked_off_second_pickup)
        self.assertIsNotNone(AuctionTOS.objects.get(pk=self.vt_buyer.pk).marked_off_first_pickup)
        self.client.post(mark, {"marked": ""})
        self.assertIsNone(AuctionTOS.objects.get(pk=self.vt_buyer.pk).marked_off_first_pickup)
        self.assertEqual(AuctionHistory.objects.filter(auction=self.multi, action__contains="Vt Buyer").count(), 2)
        actions = list(AuctionHistory.objects.filter(auction=self.multi).values_list("action", flat=True))
        self.assertTrue(any("Marked off Vt Buyer" in action for action in actions))
        self.assertTrue(any("Cleared Vt Buyer" in action for action in actions))
        # Someone in another auction can't be marked from this one.
        other = reverse(
            "auction_logistics_mark", kwargs={"slug": self.multi.slug, "pk": self.online_tos.pk, "which": 1}
        )
        self.assertEqual(self.client.post(other, {"marked": "1"}).status_code, 404)

    def test_in_person_auctions_have_none(self):
        self.client.force_login(self.user)
        url = reverse("auction_logistics", kwargs={"slug": self.in_person_auction.slug})
        self.assertEqual(self.client.get(url).status_code, 404)


class PostAuctionBannerTests(PickupTestCase):
    def test_overtime_then_the_rest(self):
        lot = self.sell(self.ma_seller, self.vt_buyer)
        running = self.sell(self.ma_seller, None, price=None)
        Lot.objects.filter(pk=running.pk).update(date_end=timezone.now() + datetime.timedelta(minutes=20))
        steps = logistics.progress(self.multi)
        self.assertEqual(steps["lots"], {"done": 1, "total": 2, "percent": 50})
        self.assertEqual(steps["running"], 1)
        Lot.objects.filter(pk=running.pk).update(date_end=timezone.now() - datetime.timedelta(minutes=1))
        Invoice.objects.filter(pk=self.invoice_for(self.vt_buyer).pk).update(calculated_total=-10)
        steps = logistics.progress(self.multi)
        self.assertNotIn("lots", steps)
        self.assertEqual(steps["buyers_paid"]["total"], 1)
        self.assertEqual(steps["labels"], {"done": 0, "total": 1, "percent": 0})
        Lot.objects.filter(pk=lot.pk).update(label_printed=True)
        self.assertNotIn("labels", logistics.progress(self.multi))

    def test_only_admins_see_it(self):
        url = reverse("auction_post_end_banner", kwargs={"slug": self.multi.slug})
        self.sell(self.ma_seller, self.vt_buyer)
        self.client.force_login(self.buyer_user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.organizer)
        self.assertContains(self.client.get(url), "sellers have printed their labels")

    def test_nothing_before_the_end(self):
        Auction.objects.filter(pk=self.multi.pk).update(date_end=timezone.now() + datetime.timedelta(hours=1))
        self.assertIsNone(logistics.progress(Auction.objects.get(pk=self.multi.pk)))


class UsersFilterTests(PickupTestCase):
    def test_outstanding_and_unprinted(self):
        from auctions.filters import AuctionTOSFilter

        self.sell(self.ma_seller, self.vt_buyer)
        self.invoice_for(self.vt_buyer)
        qs = AuctionTOS.objects.filter(auction=self.multi)
        self.assertEqual({tos.name for tos in AuctionTOSFilter.generic(None, qs, "unprinted")}, {"Ma Seller"})
        outstanding = {tos.name for tos in AuctionTOSFilter.generic(None, qs, "outstanding")}
        self.assertIn("Vt Buyer", outstanding)
        self.pay(self.vt_buyer)
        outstanding = {tos.name for tos in AuctionTOSFilter.generic(None, qs, "outstanding")}
        self.assertNotIn("Vt Buyer", outstanding)


class LocationFilterTests(PickupTestCase):
    def test_location_stacks_with_other_filters(self):
        from auctions.filters import AuctionTOSFilter

        self.sell(self.ma_seller, self.vt_buyer)
        self.invoice_for(self.vt_buyer)
        qs = AuctionTOS.objects.filter(auction=self.multi)

        def names(query):
            return {tos.name for tos in AuctionTOSFilter.generic(None, qs, query)}

        self.assertEqual(names('location:"vt"'), {"Vt Seller", "Vt Buyer"})
        self.assertEqual(names("location:VT exists"), {"Vt Buyer"})
        self.assertEqual(names('location:"MA" buyer'), set())

    def test_the_location_button_links_the_filter(self):
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.multi.slug}))
        self.assertContains(response, "?query=location%3A%22VT%22")


class CreatedAheadChartTests(StandardTestCase):
    def test_points_by_kind(self):
        from auctions.help_stats import _created_ahead

        for days in range(10):
            auction = Auction.objects.create(
                created_by=self.user,
                title=f"Chart auction {days}",
                is_online=True,
                date_start=timezone.now() - datetime.timedelta(days=30),
                date_end=timezone.now() - datetime.timedelta(days=28),
            )
            Auction.objects.filter(pk=auction.pk).update(
                date_posted=auction.date_start - datetime.timedelta(days=days + 1)
            )
            location = PickupLocation.objects.create(name="here", auction=auction, pickup_time=timezone.now())
            tos = AuctionTOS.objects.create(auction=auction, pickup_location=location, name="Seller")
            Lot.objects.create(
                lot_name="sold", auction=auction, auctiontos_seller=tos, auctiontos_winner=tos, winning_price=20
            )
        with patch.object(Auction, "club_profit", 100):
            chart = _created_ahead(min_lots=1, min_auctions=5)
        self.assertEqual(chart["online"]["auctions"], 10)
        self.assertNotIn("in_person", chart)
        self.assertNotIn("Chart auction", str(chart))
