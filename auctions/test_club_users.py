"""Managing people through a club rather than through an auction, and the bid API."""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    AuctionEditForm,
)
from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Bid,
    Club,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    Lot,
    PickupLocation,
)


class ManageUsersThroughClubTests(TestCase):
    """The per-auction 'manage_users_through_club' setting, which moves user management to ClubMember."""

    def setUp(self):
        now = timezone.now()
        self.creator = User.objects.create_user(username="auction_creator", password="testpw", email="c@example.com")
        self.joiner = User.objects.create_user(username="joiner", password="testpw", email="j@example.com")
        self.club_admin_user = User.objects.create_user(
            username="club_admin", password="testpw", email="ca@example.com"
        )
        self.club_add_edit_user = User.objects.create_user(
            username="club_add_edit", password="testpw", email="cae@example.com"
        )
        self.club_manage_auctions_user = User.objects.create_user(
            username="club_manage_auctions", password="testpw", email="cma@example.com"
        )
        self.outsider = User.objects.create_user(username="outsider", password="testpw", email="o@example.com")
        self.club = Club.objects.create(name="Test Club")
        # Permission rows on the club
        ClubMember.objects.create(club=self.club, user=self.club_admin_user, name="Admin", permission_admin=True)
        ClubMember.objects.create(
            club=self.club, user=self.club_add_edit_user, name="AddEdit", permission_add_edit=True
        )
        ClubMember.objects.create(
            club=self.club,
            user=self.club_manage_auctions_user,
            name="ManageAuctions",
            permission_manage_auctions=True,
        )
        self.auction = Auction.objects.create(
            created_by=self.creator,
            title="Empty Auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=10),
            club=self.club,
        )
        self.location = PickupLocation.objects.create(
            name="loc", auction=self.auction, pickup_time=now + datetime.timedelta(days=5)
        )

    def _enable_club_managed(self):
        self.auction.manage_users_through_club = "all"
        self.auction.save()

    def _enable_checkin_mode(self):
        self.auction.manage_users_through_club = "checkin"
        self.auction.save()

    def _auction_form_data(self):
        return {
            "title": self.auction.title,
            "slug": self.auction.slug,
            "date_start": self.auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": self.auction.date_end.strftime("%Y-%m-%d %H:%M:%S"),
            "date_online_bidding_starts": "",
            "date_online_bidding_ends": "",
            "lot_submission_start_date": "",
            "lot_submission_end_date": "",
            "summernote_description": self.auction.summernote_description or "",
            "online_bidding": self.auction.online_bidding,
            "buy_now": self.auction.buy_now,
            "reserve_price": self.auction.reserve_price,
            "lot_entry_fee": self.auction.lot_entry_fee,
            "unsold_lot_fee": self.auction.unsold_lot_fee,
            "winning_bid_percent_to_club": self.auction.winning_bid_percent_to_club,
            "pre_register_lot_discount_percent": self.auction.pre_register_lot_discount_percent,
            "winning_bid_percent_to_club_for_club_members": self.auction.winning_bid_percent_to_club_for_club_members,
            "lot_entry_fee_for_club_members": self.auction.lot_entry_fee_for_club_members,
            "alternate_split_mode": self.auction.alternate_split_mode,
            "alternative_split_label": self.auction.alternative_split_label,
            "minimum_bid": self.auction.minimum_bid,
            "tax": self.auction.tax,
            "club": str(self.club.pk),
        }

    def test_is_club_managed_requires_club(self):
        a = Auction.objects.create(
            created_by=self.creator,
            title="No club",
            is_online=False,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        a.manage_users_through_club = "all"
        a.save()
        self.assertFalse(a.is_club_managed)
        self.auction.manage_users_through_club = "all"
        self.auction.save()
        self.assertTrue(self.auction.is_club_managed)

    def test_cannot_enable_when_lots_exist(self):
        Lot.objects.create(lot_name="x", auction=self.auction, quantity=1)
        form = AuctionEditForm(
            data={"manage_users_through_club": "all", "club": str(self.club.pk)},
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        # Field-level cleaner triggers before full clean
        form.is_valid()
        self.assertIn("manage_users_through_club", form.errors)

    def test_cannot_enable_when_invoices_exist(self):
        tos = AuctionTOS.objects.create(user=self.creator, auction=self.auction, pickup_location=self.location)
        Invoice.objects.create(auctiontos_user=tos, auction=self.auction)
        form = AuctionEditForm(
            data={"manage_users_through_club": "all", "club": str(self.club.pk)},
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        form.is_valid()
        self.assertIn("manage_users_through_club", form.errors)

    def test_can_disable_without_activity(self):
        """Disabling club-managed mode is allowed when there are no lots or invoices."""
        self._enable_club_managed()
        form = AuctionEditForm(
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        self.assertFalse(form.fields["manage_users_through_club"].disabled)

    def test_cannot_disable_once_lots_exist(self):
        """Disabling is blocked once lots have been added."""
        self._enable_club_managed()
        Lot.objects.create(lot_name="x", auction=self.auction, quantity=1)
        form = AuctionEditForm(
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        # UI-level: field is disabled so users cannot post an empty value.
        self.assertTrue(form.fields["manage_users_through_club"].disabled)
        # The validator also rejects turning it off.
        form2 = AuctionEditForm(
            data={"manage_users_through_club": "", "club": str(self.club.pk)},
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        form2.fields["manage_users_through_club"].disabled = False
        form2.is_valid()
        self.assertIn("manage_users_through_club", form2.errors)

    def test_enabling_club_management_syncs_existing_club_members(self):
        self.joiner.userdata.preferred_bidder_number = "246"
        self.joiner.userdata.save(update_fields=["preferred_bidder_number"])
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", email=self.joiner.email)
        # Give the other members fixed numbers so random assignment can't take 246.
        for idx, m in enumerate(ClubMember.objects.filter(club=self.club).exclude(pk=member.pk), start=1):
            ClubMember.objects.filter(pk=m.pk).update(bidder_number=str(idx))
        form = AuctionEditForm(
            data={**self._auction_form_data(), "manage_users_through_club": "all"},
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        self.assertTrue(form.is_valid(), msg=form.errors)
        form.save()
        member.refresh_from_db()
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        self.assertEqual(member.bidder_number, "246")
        self.assertEqual(shadow.bidder_number, "246")

    def test_membership_fee_can_be_enabled_with_club_managed_mode(self):
        self.club.membership_annual_fee = Decimal("25.00")
        self.club.save(update_fields=["membership_annual_fee"])
        form = AuctionEditForm(
            data={
                **self._auction_form_data(),
                "manage_users_through_club": "all",
                "add_membership_fee_to_invoices_for_expired_members": True,
            },
            instance=self.auction,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        form.is_valid()
        self.assertNotIn("add_membership_fee_to_invoices_for_expired_members", form.errors)

    def _online_club_auction(self):
        online = Auction.objects.create(
            created_by=self.creator,
            title="Online Club Auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=5),
            club=self.club,
        )
        PickupLocation.objects.create(
            name="online loc", auction=online, pickup_time=timezone.now() + datetime.timedelta(days=2)
        )
        return online

    def test_checkin_choice_hidden_for_online_auction(self):
        form = AuctionEditForm(
            instance=self._online_club_auction(), user=self.creator, cloned_from=None, user_timezone="UTC"
        )
        choice_values = [c[0] for c in form.fields["manage_users_through_club"].choices]
        self.assertNotIn("checkin", choice_values)
        self.assertIn("all", choice_values)

    def test_checkin_mode_rejected_for_online_auction(self):
        """The validator rejects check-in mode for online auctions even past the UI."""
        online = self._online_club_auction()
        form = AuctionEditForm(
            data={"manage_users_through_club": "checkin", "club": str(self.club.pk)},
            instance=online,
            user=self.creator,
            cloned_from=None,
            user_timezone="UTC",
        )
        # Restore the choices so the custom validator runs.
        form.fields["manage_users_through_club"].choices = Auction.MANAGE_USERS_CHOICES
        form.is_valid()
        self.assertIn("manage_users_through_club", form.errors)
        self.assertIn("in-person", " ".join(form.errors["manage_users_through_club"]).lower())

    def test_permission_check_grants_club_admin_and_manage_auctions(self):
        self._enable_club_managed()
        self.assertTrue(self.auction.permission_check(self.club_admin_user))
        self.assertTrue(self.auction.permission_check(self.club_manage_auctions_user))
        # add_edit alone doesn't grant permission_check; the view gates it via can_add_edit_people.
        self.assertFalse(self.auction.permission_check(self.club_add_edit_user))
        self.assertFalse(self.auction.permission_check(self.outsider))

    def test_join_creates_clubmember_and_shadow_auctiontos(self):
        self._enable_club_managed()

        from auctions.forms import AuctionJoin
        from auctions.views import AuctionInfo

        form = AuctionJoin(
            data={
                "i_agree": True,
                "pickup_location": str(self.location.pk),
                "time_spent_reading_rules": "5",
            },
            auction=self.auction,
            user=self.joiner,
        )
        self.assertTrue(form.is_valid(), msg=f"Form errors: {form.errors}")
        view = AuctionInfo()
        view.auction = self.auction
        view.kwargs = {}
        view.object = self.auction
        request = MagicMock()
        request.user = self.joiner
        view.request = request
        view.get_form = lambda: form
        view.form_valid = lambda f: None
        view.form_invalid = lambda f: None
        view.post(request)
        cm = ClubMember.objects.get(club=self.club, user=self.joiner)
        self.assertEqual(cm.source, "Empty Auction")
        self.assertTrue(cm.bidder_number)
        self.assertNotEqual(cm.bidder_number, "")
        # The join links the AuctionTOS to the joining user.
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=cm)
        self.assertEqual(tos.user, self.joiner)
        self.assertEqual(tos.bidder_number, cm.bidder_number)

    def test_checkin_mode_self_join_does_not_grant_bidding(self):
        """Self-joining a check-in auction doesn't grant bidding; the member must check in."""
        self._enable_checkin_mode()

        from auctions.forms import AuctionJoin
        from auctions.views import AuctionInfo

        form = AuctionJoin(
            data={
                "i_agree": True,
                "pickup_location": str(self.location.pk),
                "time_spent_reading_rules": "5",
            },
            auction=self.auction,
            user=self.joiner,
        )
        self.assertTrue(form.is_valid(), msg=f"Form errors: {form.errors}")
        view = AuctionInfo()
        view.auction = self.auction
        view.kwargs = {}
        view.object = self.auction
        request = MagicMock()
        request.user = self.joiner
        view.request = request
        view.get_form = lambda: form
        view.form_valid = lambda f: None
        view.form_invalid = lambda f: None
        view.post(request)
        tos = AuctionTOS.objects.get(auction=self.auction, user=self.joiner)
        self.assertIsNone(tos.checked_in)
        self.assertFalse(tos.bidding_allowed)
        self.assertTrue(tos.requires_check_in_before_bidding)
        self.assertFalse(tos.can_bid_in_auction)

    def test_check_bidding_permissions_blocks_unchecked_in_member(self):
        """check_bidding_permissions refuses a member who hasn't checked in, even with bidding_allowed."""
        from auctions.bidding import check_bidding_permissions

        self._enable_checkin_mode()
        cm = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="123")
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=cm)
        tos.bidding_allowed = True  # simulate a stray grant that skipped check-in
        tos.checked_in = None
        tos.save()
        lot = Lot.objects.create(lot_name="a lot", auction=self.auction, quantity=1, user=self.creator)
        self.assertEqual(
            check_bidding_permissions(lot, self.joiner),
            "You must check in at the event before you can bid",
        )
        # Once checked in, the gate no longer blocks on check-in grounds.
        tos.checked_in = timezone.now()
        tos.save()
        self.assertNotEqual(
            check_bidding_permissions(lot, self.joiner),
            "You must check in at the event before you can bid",
        )

    def test_edit_form_warns_when_checkin_mode_and_pre_event_online_bidding(self):
        """Warn when check-in mode is combined with online bidding before the start date."""
        self.client.force_login(self.creator)
        data = {
            **self._auction_form_data(),
            "manage_users_through_club": "checkin",
            "online_bidding": "allow",
            "date_online_bidding_starts": (self.auction.date_start - datetime.timedelta(days=3)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "date_online_bidding_ends": self.auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
        }
        response = self.client.post(reverse("edit_auction", kwargs={"slug": self.auction.slug}), data=data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "uses check-in mode")
        # no warning when online bidding opens at/after the start date
        data["date_online_bidding_starts"] = self.auction.date_start.strftime("%Y-%m-%d %H:%M:%S")
        data["date_online_bidding_ends"] = (self.auction.date_start + datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        response = self.client.post(reverse("edit_auction", kwargs={"slug": self.auction.slug}), data=data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "uses check-in mode")

    def test_join_page_hides_club_add_message_for_existing_member(self):
        self._enable_club_managed()
        ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", email=self.joiner.email)
        self.client.force_login(self.joiner)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Joining this auction will also add you to")

    def test_club_managed_auction_users_page_hides_import_and_add_to_club_actions(self):
        self._enable_club_managed()
        self.client.force_login(self.creator)
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Import members CSV")
        self.assertNotContains(response, f"Add all users to {self.club}")

    def test_signal_propagates_clubmember_changes_to_shadow(self):
        self._enable_club_managed()
        cm = ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="Joiner",
            bidder_number="42",
        )
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=cm)
        cm.bidding_allowed = False
        cm.selling_allowed = False
        cm.save()
        tos.refresh_from_db()
        self.assertFalse(tos.bidding_allowed)
        self.assertFalse(tos.selling_allowed)
        cm.bidder_number = "77"
        cm.save()
        tos.refresh_from_db()
        self.assertEqual(tos.bidder_number, "77")

    def test_a_new_member_keeps_their_number_and_the_auction_moves_whoever_had_it(self):
        """A new member keeps their number; the row with no club record behind it moves."""
        self._enable_club_managed()
        stranger = AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="314",
        )
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="314")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        stranger.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(shadow.bidder_number, "314")
        self.assertEqual(member.bidder_number, "314")
        self.assertNotEqual(stranger.bidder_number, "314")
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="314").count(), 1)

    def test_an_invoiced_auction_is_kept_in_step_too(self):
        """Invoiced auctions are renumbered too, even though issued invoices print the old number."""
        self._enable_club_managed()
        cm = ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="Joiner",
            bidder_number="55",
        )
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=cm)
        self.auction.invoiced = True
        self.auction.save()
        cm.bidder_number = "999"
        cm.save()
        tos.refresh_from_db()
        self.assertEqual(tos.bidder_number, "999")

    def test_renumbering_a_member_moves_a_row_that_had_drifted_onto_the_number(self):
        """Renumbering a member moves a row that had drifted onto the number back to its own club number."""
        self._enable_checkin_mode()
        borrower = ClubMember.objects.create(club=self.club, name="Borrower", bidder_number="10")
        borrowing_row = AuctionTOS.objects.get(auction=self.auction, clubmember=borrower)
        AuctionTOS.objects.filter(pk=borrowing_row.pk).update(bidder_number="20")
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="30")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)

        member.bidder_number = "20"
        member.save()

        shadow.refresh_from_db()
        borrowing_row.refresh_from_db()
        self.assertEqual(shadow.bidder_number, "20", "the number the admin typed has to reach the auction")
        self.assertEqual(borrowing_row.bidder_number, "10")
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="20").count(), 1)

    def _second_club_managed_auction(self):
        """A second club-managed auction of the same club, to observe propagation across auctions."""
        now = timezone.now()
        other = Auction.objects.create(
            created_by=self.creator,
            title="Second Auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=10),
            club=self.club,
        )
        PickupLocation.objects.create(name="loc2", auction=other, pickup_time=now + datetime.timedelta(days=5))
        other.manage_users_through_club = self.auction.manage_users_through_club
        other.save()
        return other

    def test_editing_a_member_from_inside_an_auction_is_not_undone_by_the_row_save(self):
        """The member form's row save doesn't revert the member edit it just made (update_fields respected)."""
        self._enable_club_managed()
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Old Name", email="old@example.com")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        stale = AuctionTOS.objects.get(pk=shadow.pk)  # loaded before the edit, as the view does

        member.name = "New Name"
        member.email = "new@example.com"
        member.save()

        stale.is_club_member = True
        stale.save(update_fields=["is_club_member"])

        member.refresh_from_db()
        shadow.refresh_from_db()
        self.assertEqual(member.name, "New Name")
        self.assertEqual(member.email, "new@example.com")
        self.assertEqual(shadow.name, "New Name")
        self.assertEqual(shadow.email, "new@example.com")

    def test_a_full_save_of_a_participant_row_still_reaches_the_member(self):
        """The other half: an edit made in the auction is still an edit to the person."""
        self._enable_club_managed()
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)

        shadow.phone_number = "555-0123"
        shadow.save()

        member.refresh_from_db()
        self.assertEqual(member.phone_number, "555-0123")

    def test_check_in_with_a_number_renumbers_the_member_everywhere(self):
        """Check-in with a number renumbers the member everywhere, not just one row."""
        from auctions.services import check_in_auctiontos

        self._enable_checkin_mode()
        other_auction = self._second_club_managed_auction()
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="41")
        here = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        there = AuctionTOS.objects.get(auction=other_auction, clubmember=member)

        check_in_auctiontos(here, bidder_number="86", acting_user=self.club_admin_user)

        here.refresh_from_db()
        there.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(here.bidder_number, "86")
        self.assertEqual(member.bidder_number, "86", "the club is where the number lives in this mode")
        self.assertEqual(there.bidder_number, "86", "every other auction they are in follows")

    def test_a_number_assigned_at_check_in_still_moves_whoever_had_it(self):
        """The displacement the club-scoped writer does, reached through check-in."""
        from auctions.services import check_in_auctiontos

        self._enable_checkin_mode()
        stranger = AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="64",
        )
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="15")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)

        check_in_auctiontos(shadow, bidder_number="64", acting_user=self.club_admin_user)

        shadow.refresh_from_db()
        stranger.refresh_from_db()
        self.assertEqual(shadow.bidder_number, "64")
        self.assertNotEqual(stranger.bidder_number, "64")
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="64").count(), 1)

    def test_a_name_too_long_for_the_participant_row_is_cut_rather_than_raising(self):
        """A name longer than AuctionTOS.name (181) is truncated rather than raising DataError 1406."""
        self._enable_club_managed()
        long_name = "Bartholomew " * 20
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name=long_name[:200])
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        limit = AuctionTOS._meta.get_field("name").max_length
        self.assertEqual(shadow.name, long_name[:limit])

        member.phone_number = "555-0100"
        member.save()  # the save that used to raise

        shadow.refresh_from_db()
        self.assertEqual(shadow.phone_number, "555-0100")
        self.assertEqual(shadow.name, long_name[:limit])

    def test_renumbering_a_member_takes_the_number_off_whoever_had_it(self):
        """Renumbering a member takes the number off whoever had it."""
        self._enable_checkin_mode()
        stranger = AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="77",
        )
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="12")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)

        member.bidder_number = "77"
        member.save()

        shadow.refresh_from_db()
        stranger.refresh_from_db()
        self.assertEqual(shadow.bidder_number, "77")
        self.assertNotEqual(stranger.bidder_number, "77")
        self.assertTrue(stranger.bidder_number)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="77").count(), 1)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.auction, action__contains="Joined directly").exists(),
            "the auction has to record whose card stopped matching",
        )

    def test_the_member_form_allows_a_number_somebody_else_is_using(self):
        """The member form applies a number someone else uses; live validation names who moves."""
        from auctions.forms import ClubMemberAdminForm

        self._enable_checkin_mode()
        AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="77",
        )
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="12")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)

        form = ClubMemberAdminForm(
            data={
                "name": member.name,
                "email": "",
                "phone_number": "",
                "address": "",
                "memo": "",
                "contact_status": member.contact_status,
                "bidder_number": "77",
                "bidding_allowed": True,
                "selling_allowed": True,
            },
            instance=member,
            club=self.club,
            auctiontos=shadow,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_the_live_validation_names_whoever_is_about_to_be_renumbered(self):
        self._enable_checkin_mode()
        AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="77",
        )
        member = ClubMember.objects.create(club=self.club, name="Joiner", bidder_number="12")
        ClubMember.objects.filter(user=self.club_admin_user, club=self.club).update(permission_add_edit=True)
        self.client.force_login(self.club_admin_user)
        response = self.client.post(
            reverse("clubmember_validation", kwargs={"slug": self.club.slug}),
            {"pk": member.pk, "name": member.name, "bidder_number": "77"},
        )
        self.assertEqual(response.status_code, 200)
        note = response.json()["bidder_number_note"]
        self.assertIn("Joined directly", note)
        self.assertIn("new one", note)
        self.assertEqual(response.json()["bidder_number_tooltip"], "", "not an error -- it will be applied")

    def test_the_live_validation_warns_when_creating_a_member_too(self):
        """Live validation warns when creating a member too."""
        self._enable_checkin_mode()
        AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="77",
        )
        ClubMember.objects.filter(user=self.club_admin_user, club=self.club).update(permission_add_edit=True)
        self.client.force_login(self.club_admin_user)
        response = self.client.post(
            reverse("clubmember_validation", kwargs={"slug": self.club.slug}),
            {"name": "Brand New", "bidder_number": "77"},
        )
        self.assertEqual(response.status_code, 200)
        note = response.json()["bidder_number_note"]
        self.assertIn("Joined directly", note)
        self.assertIn("new one", note)

    def test_setting_a_lot_winner_follows_a_renumbered_member(self):
        """The symptom that made this worth chasing: the lot went to the wrong person."""
        self._enable_checkin_mode()
        borrower = ClubMember.objects.create(club=self.club, name="Borrower", bidder_number="10")
        borrowing_row = AuctionTOS.objects.get(auction=self.auction, clubmember=borrower)
        AuctionTOS.objects.filter(pk=borrowing_row.pk).update(bidder_number="20")
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="30")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        member.bidder_number = "20"
        member.save()
        # Check-in mode refuses winners who haven't checked in.
        AuctionTOS.objects.filter(pk=shadow.pk).update(checked_in=timezone.now(), bidding_allowed=True)

        from auctions.views import DynamicSetLotWinner

        view = DynamicSetLotWinner()
        view.request = type("R", (), {"user": self.creator})()
        view.auction = self.auction
        tos, error = view.validate_winner("20", "save")
        self.assertIsNone(error)
        self.assertEqual(tos.pk, shadow.pk)

    def test_a_corrected_name_and_email_reach_the_auction(self):
        """A corrected name and email on the member reach the auction's rows."""
        self._enable_checkin_mode()
        member = ClubMember.objects.create(
            club=self.club,
            name="Jane Smith",
            email="jane@example.com",
            phone_number="555-0100",
            address="1 Old Street",
            bidder_number="41",
        )
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        self.assertEqual(shadow.name, "Jane Smith")

        member.name = "Jane Smythe"
        member.email = "jane.smythe@example.com"
        member.phone_number = "555-0199"
        member.address = "2 New Street"
        member.save()

        shadow.refresh_from_db()
        self.assertEqual(shadow.name, "Jane Smythe")
        self.assertEqual(shadow.email, "jane.smythe@example.com")
        self.assertEqual(shadow.email_address_status, "UNKNOWN", "a bounce against the old address must not follow")
        self.assertEqual(shadow.phone_number, "555-0199")
        self.assertEqual(shadow.address, "2 New Street")

    def test_a_detail_fixed_in_the_auction_reaches_the_club_and_every_other_auction(self):
        """A detail fixed in an auction reaches the club and every other auction."""
        self._enable_checkin_mode()
        other_auction = Auction.objects.create(
            created_by=self.creator,
            title="Last year",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=400),
            date_end=timezone.now() - datetime.timedelta(days=390),
            club=self.club,
        )
        PickupLocation.objects.create(
            name="old loc", auction=other_auction, pickup_time=timezone.now() - datetime.timedelta(days=390)
        )
        other_auction.manage_users_through_club = "all"
        other_auction.invoiced = True
        other_auction.save()
        member = ClubMember.objects.create(club=self.club, name="Jane", address="1 Old Street")
        here = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        there = AuctionTOS.objects.get(auction=other_auction, clubmember=member)

        here.address = "2 New Street"
        here.save()

        member.refresh_from_db()
        there.refresh_from_db()
        self.assertEqual(member.address, "2 New Street", "the club record is the same person")
        self.assertEqual(there.address, "2 New Street", "and so is last year's auction")

    def test_a_corrected_email_reaches_the_auction_even_when_it_looks_like_a_duplicate(self):
        """A corrected email reaches the auction even when another row has it (flagged as duplicate)."""
        self._enable_checkin_mode()
        AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Somebody else",
            email="shared@example.com",
        )
        member = ClubMember.objects.create(club=self.club, name="Jane", email="jane@example.com")
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)

        member.email = "shared@example.com"
        member.save()

        shadow.refresh_from_db()
        self.assertEqual(shadow.email, "shared@example.com")
        self.assertEqual(shadow.email_address_status, "UNKNOWN")

    def test_check_in_mode_does_not_hand_out_bidding_from_the_club_page(self):
        """Enabling a member at club level doesn't grant bidding in check-in mode."""
        self._enable_checkin_mode()
        member = ClubMember.objects.create(club=self.club, name="Not here yet", bidding_allowed=False)
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        self.assertIsNone(shadow.checked_in)
        self.assertFalse(shadow.bidding_allowed)

        member.bidding_allowed = True
        member.save()

        shadow.refresh_from_db()
        self.assertFalse(shadow.bidding_allowed, "not checked in, so not bidding")

    def test_check_in_mode_still_takes_bidding_away_from_the_club_page(self):
        self._enable_checkin_mode()
        member = ClubMember.objects.create(club=self.club, name="Arrived", bidding_allowed=True)
        shadow = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        AuctionTOS.objects.filter(pk=shadow.pk).update(checked_in=timezone.now(), bidding_allowed=True)

        member.bidding_allowed = False
        member.save()

        shadow.refresh_from_db()
        self.assertFalse(shadow.bidding_allowed)

    def test_checking_somebody_in_gives_them_their_club_number_and_moves_whoever_had_it(self):
        """``_upsert_clubmember_shadow_tos`` gives the member their club number and moves whoever had it."""
        from auctions.views.base import _upsert_clubmember_shadow_tos

        self._enable_checkin_mode()
        stranger = AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.location,
            name="Joined directly",
            bidder_number="88",
        )
        member = ClubMember.objects.create(club=self.club, name="At the door", bidder_number="88")
        AuctionTOS.objects.filter(auction=self.auction, clubmember=member).delete()

        tos = _upsert_clubmember_shadow_tos(self.auction, member)

        stranger.refresh_from_db()
        self.assertIsNotNone(tos)
        self.assertEqual(tos.bidder_number, "88")
        self.assertNotEqual(stranger.bidder_number, "88")
        self.assertEqual(AuctionTOS.objects.filter(auction=self.auction, bidder_number="88").count(), 1)

    def test_validate_winner_resolves_via_clubmember(self):
        self._enable_club_managed()
        cm = ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="Joiner",
            bidder_number="123",
        )
        from auctions.views import DynamicSetLotWinner

        view = DynamicSetLotWinner()
        view.request = type("R", (), {"user": self.creator})()
        view.auction = self.auction
        tos, error = view.validate_winner("123", "save")
        self.assertIsNone(error)
        self.assertIsNotNone(tos)
        self.assertEqual(tos.clubmember_id, cm.pk)
        self.assertEqual(tos.bidder_number, "123")

    def test_checkin_mode_property_and_users_page_actions(self):
        self._enable_checkin_mode()
        self.client.force_login(self.creator)
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.use_check_in_mode)
        self.assertContains(response, "Door prizes")
        self.assertContains(response, "Check in")

    def test_checkin_mode_manual_member_creation_sets_checked_in_and_bidding_allowed(self):
        self._enable_checkin_mode()
        self.client.force_login(self.club_add_edit_user)
        response = self.client.post(
            reverse("clubmember_create", kwargs={"slug": self.club.slug}) + f"?auction={self.auction.slug}",
            {
                "name": "Checked In Member",
                "email": "checkedin@example.com",
                "phone_number": "",
                "address": "",
                "contact_status": "contact",
                "bidder_number": "",
                "memo": "",
                "discord_role_auto_managed": "on",
                "discord_role_override": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        member = ClubMember.objects.get(club=self.club, email="checkedin@example.com")
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        self.assertIsNotNone(tos.checked_in)
        self.assertTrue(tos.bidding_allowed)

    def test_validate_winner_requires_checked_in_in_checkin_mode(self):
        self._enable_checkin_mode()
        ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="Joiner",
            bidder_number="123",
        )
        from auctions.views import DynamicSetLotWinner

        view = DynamicSetLotWinner()
        view.request = type("R", (), {"user": self.creator})()
        view.auction = self.auction
        tos, error = view.validate_winner("123", "save")
        self.assertEqual(error, "This bidder has not been checked in yet")
        self.assertIsNotNone(tos)
        tos, error = view.validate_winner("123", "force_save")
        self.assertIsNone(error)
        self.assertIsNotNone(tos)

    def test_check_in_endpoint_marks_user_checked_in(self):
        self._enable_checkin_mode()
        cm = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="123")
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=cm)
        self.client.force_login(self.creator)
        response = self.client.post(reverse("auction_check_in", kwargs={"pk": tos.pk}))
        self.assertEqual(response.status_code, 200)
        tos.refresh_from_db()
        self.assertIsNotNone(tos.checked_in)
        self.assertTrue(tos.bidding_allowed)

    def test_turn_bidding_off_for_all_users(self):
        self._enable_checkin_mode()
        cm = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner", bidder_number="123")
        AuctionTOS.objects.create(
            user=self.joiner,
            auction=self.auction,
            pickup_location=self.location,
            clubmember=cm,
            bidder_number="123",
            bidding_allowed=True,
            checked_in=timezone.now(),
        )
        self.client.force_login(self.creator)
        response = self.client.post(reverse("auction_disable_bidding", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuctionTOS.objects.filter(auction=self.auction, bidding_allowed=True).exists())

    def test_door_prize_picker_only_uses_checked_in_users(self):
        self._enable_checkin_mode()
        checked_in_member = ClubMember.objects.create(
            club=self.club, user=self.joiner, name="Checked In Winner", bidder_number="123"
        )
        unchecked_member = ClubMember.objects.create(
            club=self.club, user=self.outsider, name="Unchecked User", bidder_number="456"
        )
        checked_in_tos = AuctionTOS.objects.get(auction=self.auction, clubmember=checked_in_member)
        checked_in_tos.checked_in = timezone.now()
        checked_in_tos.save()
        unchecked_tos = AuctionTOS.objects.get(auction=self.auction, clubmember=unchecked_member)
        self.client.force_login(self.creator)
        response = self.client.post(reverse("auction_door_prizes", kwargs={"slug": self.auction.slug}))
        self.assertRedirects(response, reverse("auction_door_prizes", kwargs={"slug": self.auction.slug}))
        checked_in_tos.refresh_from_db()
        unchecked_tos.refresh_from_db()
        self.assertIsNotNone(checked_in_tos.door_prize_called)
        self.assertIsNone(unchecked_tos.door_prize_called)

    def test_quick_check_in_scan_assigns_bidder_number(self):
        self._enable_checkin_mode()
        member = ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="Joiner",
            bidder_number="",
        )
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {"barcode": str(member.membership_number), "assign_bidder_number": "456"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        member.refresh_from_db()
        self.assertEqual(member.bidder_number, "456")
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        self.assertEqual(tos.bidder_number, "456")
        self.assertIsNotNone(tos.checked_in)
        self.assertTrue(tos.bidding_allowed)

    def test_barcode_scan_check_in_only_ignores_side_effects(self):
        """The self check-in kiosk's check_in_only ignores bidder number and adjustments."""
        self._enable_checkin_mode()
        member = ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="Joiner",
            bidder_number="",
            # The phone seeds the bidder number, so it can't randomly land on 456.
            phone_number="555-555-0123",
        )
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "barcode": str(member.membership_number),
                "check_in_only": "1",
                "assign_bidder_number": "456",
                "adjustment_type": "ADD",
                "adjustment_amount": "10",
                "adjustment_label": "sneaky",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        member.refresh_from_db()
        self.assertNotEqual(member.bidder_number, "456")
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        self.assertNotEqual(tos.bidder_number, "456")
        self.assertIsNotNone(tos.checked_in)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice__auctiontos_user=tos).exists())

    def test_barcode_scan_check_in_only_unrecognized_barcode(self):
        self._enable_checkin_mode()
        self.client.force_login(self.creator)
        for bad_barcode in ["111112345", "0105raffle", "not-a-number"]:
            response = self.client.post(
                reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
                {"barcode": bad_barcode, "check_in_only": "1"},
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["message"], "Unrecognized barcode")

    def test_barcode_scan_denied_for_outsider(self):
        self._enable_checkin_mode()
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner")
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {"barcode": str(member.membership_number)},
        )
        self.assertEqual(response.status_code, 403)

    def test_barcode_scan_adjustment_on_checked_in_card_keeps_checkin_time(self):
        """An adjustment scan then an already-checked-in card keeps the check-in time."""
        self._enable_checkin_mode()
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner")
        self.client.force_login(self.creator)
        # First scan checks them in
        self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {"barcode": str(member.membership_number)},
        )
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        original_checked_in = tos.checked_in
        self.assertIsNotNone(original_checked_in)
        # Second scan carries an adjustment
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "barcode": str(member.membership_number),
                "adjustment_type": "ADD",
                "adjustment_amount": "7",
                "adjustment_label": "raffle",
            },
        )
        self.assertEqual(response.status_code, 200)
        tos.refresh_from_db()
        self.assertEqual(tos.checked_in, original_checked_in)
        self.assertTrue(InvoiceAdjustment.objects.filter(invoice__auctiontos_user=tos, amount=7).exists())

    def test_barcode_scan_adjustment_applied_to_bidder_number(self):
        """An adjustment scan then a paddle applies to that bidder number."""
        self._enable_checkin_mode()
        tos = AuctionTOS.objects.create(
            user=self.joiner,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="222",
            checked_in=timezone.now(),
            name="Bidder Person",
        )
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "apply_to_bidder_number": "222",
                "adjustment_type": "DISCOUNT",
                "adjustment_amount": "5",
                "adjustment_label": "coupon",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verb"], "Adjusted")
        adj = InvoiceAdjustment.objects.get(invoice__auctiontos_user=tos)
        self.assertEqual(adj.amount, 5)
        self.assertEqual(adj.adjustment_type, "DISCOUNT")

    def test_barcode_scan_adjustment_to_bidder_requires_checked_in(self):
        """In check-in mode, the bidder must be checked in."""
        self._enable_checkin_mode()
        AuctionTOS.objects.create(
            user=self.joiner,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="333",
            name="Not Yet Here",
        )
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "apply_to_bidder_number": "333",
                "adjustment_type": "ADD",
                "adjustment_amount": "5",
                "adjustment_label": "late fee",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not checked in", response.json()["message"])
        self.assertFalse(InvoiceAdjustment.objects.exists())

    def test_barcode_scan_adjustment_to_bidder_no_checkin_mode(self):
        """Outside check-in mode, no check-in is needed."""
        self._enable_club_managed()  # "all" mode, not check-in
        tos = AuctionTOS.objects.create(
            user=self.joiner,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="444",
            name="Regular Bidder",
        )
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "apply_to_bidder_number": "444",
                "adjustment_type": "ADD",
                "adjustment_amount": "8",
                "adjustment_label": "extra",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(InvoiceAdjustment.objects.filter(invoice__auctiontos_user=tos, amount=8).exists())

    def test_barcode_scan_adjustment_to_unknown_bidder_number(self):
        self._enable_checkin_mode()
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "apply_to_bidder_number": "999",
                "adjustment_type": "ADD",
                "adjustment_amount": "5",
                "adjustment_label": "x",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("999", response.json()["message"])

    def test_barcode_scan_adjustment_to_bidder_with_closed_invoice_errors(self):
        """A closed invoice on the bidder number errors."""
        self._enable_checkin_mode()
        tos = AuctionTOS.objects.create(
            user=self.joiner,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="222",
            checked_in=timezone.now(),
            name="Paid Bidder",
        )
        Invoice.objects.create(auctiontos_user=tos, auction=self.auction, status="UNPAID")
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "apply_to_bidder_number": "222",
                "adjustment_type": "ADD",
                "adjustment_amount": "5",
                "adjustment_label": "late fee",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not open", response.json()["message"])
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice__auctiontos_user=tos).exists())

    def test_barcode_scan_adjustment_on_member_card_with_closed_invoice_errors(self):
        """A closed invoice on the member card errors without creating another."""
        self._enable_checkin_mode()
        member = ClubMember.objects.create(club=self.club, user=self.joiner, name="Joiner")
        tos = AuctionTOS.objects.get(auction=self.auction, clubmember=member)
        Invoice.objects.create(auctiontos_user=tos, auction=self.auction, status="PAID")
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("auction_barcode_scan", kwargs={"slug": self.auction.slug}),
            {
                "barcode": str(member.membership_number),
                "adjustment_type": "ADD",
                "adjustment_amount": "9",
                "adjustment_label": "raffle",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not open", response.json()["message"])
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice__auctiontos_user=tos).exists())

    def test_self_check_in_page(self):
        self._enable_checkin_mode()
        self.client.force_login(self.creator)
        response = self.client.get(reverse("auction_self_check_in", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Self-checkin")
        self.assertContains(response, "checkInOnly: true")

    def test_self_check_in_page_requires_checkin_mode(self):
        self.client.force_login(self.creator)
        response = self.client.get(reverse("auction_self_check_in", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 404)

    def test_self_check_in_page_denied_for_outsider(self):
        self._enable_checkin_mode()
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("auction_self_check_in", kwargs={"slug": self.auction.slug}))
        self.assertEqual(response.status_code, 403)

    def test_clubmember_generate_bidder_number_unique_per_club(self):
        cm1 = ClubMember.objects.create(
            club=self.club,
            user=self.joiner,
            name="A",
            phone_number="555-111-2222",
        )
        cm1.generate_bidder_number()
        self.assertTrue(cm1.bidder_number)
        cm2 = ClubMember.objects.create(
            club=self.club,
            user=self.club_add_edit_user,
            name="B",
            phone_number="555-111-2222",
        )
        cm2.generate_bidder_number()
        self.assertNotEqual(cm1.bidder_number, cm2.bidder_number)


class PlaceBidApiTests(TestCase):
    """/api/lots/<pk>/bid/ saves bids over HTTP, even when the websocket broadcast fails."""

    def setUp(self):
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.seller = User.objects.create_user(username="bid_seller", password="x", email="seller@example.com")
        self.bidder = User.objects.create_user(username="bid_bidder", password="x", email="bidder@example.com")
        self.outsider = User.objects.create_user(username="bid_outsider", password="x", email="out@example.com")
        self.auction = Auction.objects.create(
            created_by=self.seller,
            title="Active bidding auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=the_future,
        )
        self.location = PickupLocation.objects.create(name="bid location", auction=self.auction, pickup_time=the_future)
        self.seller_tos = AuctionTOS.objects.create(
            user=self.seller, auction=self.auction, pickup_location=self.location
        )
        self.bidder_tos = AuctionTOS.objects.create(
            user=self.bidder, auction=self.auction, pickup_location=self.location
        )
        self.lot = Lot.objects.create(
            lot_name="A biddable lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            quantity=1,
            reserve_price=10,
            date_end=the_future,
        )
        # Backdate past the 20-minute new-lot bidding hold.
        Lot.objects.filter(pk=self.lot.pk).update(date_posted=timezone.now() - datetime.timedelta(hours=2))
        self.lot.refresh_from_db()
        self.url = reverse("lot_bid", kwargs={"pk": self.lot.pk})

    def _bids(self, user=None):
        qs = Bid.objects.exclude(is_deleted=True).filter(lot_number=self.lot)
        if user:
            qs = qs.filter(user=user)
        return qs

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_persists_bid(self, mock_broadcast):
        """A valid bid is saved and the result is broadcast to other viewers."""
        self.client.force_login(self.bidder)
        response = self.client.post(self.url, {"bid": "15"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "NEW_HIGH_BIDDER")
        bid = self._bids(self.bidder).first()
        self.assertIsNotNone(bid)
        self.assertEqual(bid.amount, 15)
        self.assertTrue(mock_broadcast.called)

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_requires_login(self, mock_broadcast):
        """Anonymous users can't bid and nothing is saved."""
        response = self.client.post(self.url, {"bid": "15"})
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(self._bids().exists())

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_rejects_user_not_in_auction(self, mock_broadcast):
        """A user who hasn't joined the auction gets an error and no bid is saved."""
        self.client.force_login(self.outsider)
        response = self.client.post(self.url, {"bid": "15"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "ERROR")
        self.assertFalse(self._bids(self.outsider).exists())

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_missing_lot_returns_404(self, mock_broadcast):
        self.client.force_login(self.bidder)
        response = self.client.post(reverse("lot_bid", kwargs={"pk": 99999999}), {"bid": "15"})
        self.assertEqual(response.status_code, 404)

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_on_lot_without_category(self, mock_broadcast):
        """A lot with no species_category doesn't break bidding."""
        Lot.objects.filter(pk=self.lot.pk).update(species_category=None)
        self.lot.refresh_from_db()
        self.client.force_login(self.bidder)
        response = self.client.post(self.url, {"bid": "15"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "NEW_HIGH_BIDDER")
        self.assertTrue(self._bids(self.bidder).exists())

    def test_bid_saved_even_when_broadcast_fails(self):
        """A broadcast failure still saves the bid."""
        from auctions.bidding import place_bid_and_broadcast

        with patch("auctions.bidding.broadcast_bid_result", side_effect=Exception("redis down")):
            result = place_bid_and_broadcast(self.lot, self.bidder, "15")
        self.assertEqual(result["type"], "NEW_HIGH_BIDDER")
        self.assertEqual(self._bids(self.bidder).count(), 1)

    def test_broadcast_targets_lot_group(self):
        """A normal bid is broadcast to the whole-lot group so every viewer updates."""
        from auctions.bidding import place_bid_and_broadcast

        sent = {}

        async def fake_group_send(group, message):
            sent["group"] = group
            sent["message"] = message

        with patch("auctions.consumers.get_channel_layer") as mock_get_layer:
            mock_get_layer.return_value.group_send = fake_group_send
            place_bid_and_broadcast(self.lot, self.bidder, "15")

        self.assertEqual(sent["group"], f"lot_{self.lot.pk}")
        self.assertEqual(sent["message"]["info"], "NEW_HIGH_BIDDER")

    def _in_person_lot(self, **auction_kwargs):
        """An in-person auction and lot, with the bidder joined."""
        the_future = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(
            created_by=self.seller,
            title="In-person auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=the_future,
            online_bidding="allow",
            **auction_kwargs,
        )
        location = PickupLocation.objects.create(name="ip loc", auction=auction, pickup_time=the_future)
        seller_tos = AuctionTOS.objects.create(user=self.seller, auction=auction, pickup_location=location)
        AuctionTOS.objects.create(user=self.bidder, auction=auction, pickup_location=location)
        lot = Lot.objects.create(
            lot_name="in person lot",
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            reserve_price=10,
            date_end=the_future,
        )
        Lot.objects.filter(pk=lot.pk).update(date_posted=timezone.now() - datetime.timedelta(hours=2))
        return lot

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_before_online_bidding_starts(self, mock_broadcast):
        lot = self._in_person_lot(
            date_online_bidding_starts=timezone.now() + datetime.timedelta(hours=2),
            date_online_bidding_ends=timezone.now() + datetime.timedelta(days=2),
        )
        self.client.force_login(self.bidder)
        response = self.client.post(reverse("lot_bid", kwargs={"pk": lot.pk}), {"bid": "15"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "ERROR")
        self.assertIn("hasn't started", response.json()["message"].lower())
        self.assertFalse(Bid.objects.exclude(is_deleted=True).filter(lot_number=lot).exists())

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_after_online_bidding_ends(self, mock_broadcast):
        lot = self._in_person_lot(
            date_online_bidding_starts=timezone.now() - datetime.timedelta(days=2),
            date_online_bidding_ends=timezone.now() - datetime.timedelta(hours=1),
        )
        self.client.force_login(self.bidder)
        response = self.client.post(reverse("lot_bid", kwargs={"pk": lot.pk}), {"bid": "15"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "ERROR")
        self.assertIn("ended", response.json()["message"].lower())
        self.assertFalse(Bid.objects.exclude(is_deleted=True).filter(lot_number=lot).exists())

    @patch("auctions.bidding.broadcast_bid_result")
    def test_api_bid_on_sold_lot(self, mock_broadcast):
        """A lot with a winner can't be bid on (no winning_price, so this tests the winner check)."""
        self.lot.winner = self.seller
        self.lot.auctiontos_winner = self.bidder_tos
        self.lot.save()
        self.client.force_login(self.bidder)
        response = self.client.post(self.url, {"bid": "25"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "ERROR")
        self.assertIn("sold", response.json()["message"].lower())
        self.assertFalse(self._bids(self.bidder).exists())
