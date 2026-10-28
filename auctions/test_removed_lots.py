"""Removing a lot ends it; putting it back reopens it unless it is finished or its auction is pretty much over."""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from auctions.models import Auction, AuctionTOS, Club, ClubMember, Lot, PickupLocation
from auctions.services import bap_review_lots


class RemovedLotsEndTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="removed_seller", password="x", email="removed@example.com")
        self.auction = self._auction(timezone.now() - datetime.timedelta(hours=1))

    def _auction(self, start):
        auction = Auction.objects.create(
            title=f"Auction {start}", created_by=self.user, is_online=False, date_start=start
        )
        location = PickupLocation.objects.create(name="loc", auction=auction, pickup_time=start)
        auction.tos = AuctionTOS.objects.create(user=self.user, auction=auction, pickup_location=location)
        return auction

    def _lot(self, auction=None, **kwargs):
        auction = auction or self.auction
        return Lot.objects.create(lot_name="Guppies", auction=auction, auctiontos_seller=auction.tos, **kwargs)

    def test_remove_ends_the_lot(self):
        lot = self._lot()
        lot.remove(True, self.user)
        lot.refresh_from_db()
        self.assertTrue(lot.banned)
        self.assertFalse(lot.active)

    def test_setting_banned_directly_ends_the_lot(self):
        lot = self._lot()
        lot.banned = True
        lot.save()
        lot.refresh_from_db()
        self.assertFalse(lot.active)

    def test_unremove_reopens_the_lot(self):
        lot = self._lot()
        lot.remove(True, self.user)
        lot = Lot.objects.get(pk=lot.pk)
        lot.remove(False, self.user)
        lot.refresh_from_db()
        self.assertFalse(lot.banned)
        self.assertTrue(lot.active)

    def test_unremove_on_same_instance_reopens(self):
        lot = self._lot()
        lot.remove(True, self.user)
        lot.remove(False, self.user)
        lot.refresh_from_db()
        self.assertTrue(lot.active)

    def test_unremove_stays_ended_when_auction_pretty_much_over(self):
        over = self._auction(timezone.now() - datetime.timedelta(days=3))
        lot = self._lot(over)
        lot.remove(True, self.user)
        lot = Lot.objects.get(pk=lot.pk)
        self.assertTrue(lot.auction.pretty_much_over)
        lot.remove(False, self.user)
        lot.refresh_from_db()
        self.assertFalse(lot.banned)
        self.assertFalse(lot.active)

    def test_unremove_stays_ended_when_sold_or_ended_unsold(self):
        sold = self._lot(winning_price=10, auctiontos_winner=self.auction.tos)
        unsold = self._lot()
        unsold.end_unsold(self.user)
        for lot in (sold, unsold):
            lot.remove(True, self.user)
            lot = Lot.objects.get(pk=lot.pk)
            lot.remove(False, self.user)
            lot.refresh_from_db()
            self.assertFalse(lot.active)

    def test_saving_an_open_lot_does_not_reopen_an_ended_one(self):
        lot = self._lot()
        lot.active = False
        lot.save()
        lot = Lot.objects.get(pk=lot.pk)
        lot.lot_name = "Renamed"
        lot.save()
        lot.refresh_from_db()
        self.assertFalse(lot.active)

    def test_update_fields_without_banned_left_alone(self):
        lot = self._lot()
        Lot.objects.filter(pk=lot.pk).update(banned=True)
        lot = Lot.objects.get(pk=lot.pk)
        lot.save(update_fields=["lot_name"])
        lot.refresh_from_db()
        self.assertTrue(lot.active)
        lot.save(update_fields=["banned"])
        lot.refresh_from_db()
        self.assertFalse(lot.active)

    def test_removed_lots_stay_out_of_the_bap_queue(self):
        club = Club.objects.create(name="Removed club")
        ClubMember.objects.create(club=club, user=self.user)
        self.auction.club = club
        self.auction.save()
        lot = self._lot()
        ended = self._lot()
        ended.end_unsold(self.user)
        lot.remove(True, self.user)
        self.assertIn(ended, bap_review_lots(club))
        self.assertNotIn(lot, bap_review_lots(club))

    def test_endauctions_ends_removed_lots_left_open(self):
        from django.core.management import call_command

        lot = self._lot()
        Lot.objects.filter(pk=lot.pk).update(banned=True)
        call_command("endauctions")
        lot.refresh_from_db()
        self.assertFalse(lot.active)
