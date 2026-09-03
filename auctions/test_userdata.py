"""``UserData`` and ``AuctionTOS`` properties, and merging one user into another."""

import datetime
import io
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionCampaign,
    AuctionIgnore,
    AuctionTOS,
    BapAward,
    Bid,
    Category,
    ChatSubscription,
    Club,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    PageView,
    PayPalSeller,
    PickupLocation,
    SearchHistory,
    SquareSeller,
    UserIgnoreCategory,
    UserInterestCategory,
    Watch,
)
from auctions.tests import StandardTestCase


class AuctionTOSPropertyTests(StandardTestCase):
    """Test AuctionTOS model properties"""

    def test_auction_tos_invoice_relationship(self):
        """Test the invoice relationship"""
        # Invoice should exist from StandardTestCase setup
        assert self.online_tos.invoice is not None
        assert self.online_tos.invoice.auctiontos_user == self.online_tos


class UserDataPropertyTests(StandardTestCase):
    """Test UserData model properties"""

    def test_user_data_exists(self):
        """Test that UserData is created for users"""
        # UserData should be automatically created
        assert hasattr(self.user, "userdata")
        assert self.user.userdata is not None

    def test_user_data_unnotified_subscriptions_count(self):
        """Test the unnotified_subscriptions_count property"""
        # This is tested in ChatSubscriptionTests but we can add basic checks
        user_data = self.user.userdata
        # Initially should be 0
        assert user_data.unnotified_subscriptions_count == 0


class UserDataMergeIntoTests(TestCase):
    def setUp(self):
        self.source_user = User.objects.create_user(
            username="merge_source", password="testpass", email="merge_source@example.com"
        )
        self.target_user = User.objects.create_user(
            username="merge_target", password="testpass", email="merge_target@example.com"
        )

        now = timezone.now()
        self.now = now
        self.club = Club.objects.create(name="Merge Club")
        self.auction = Auction.objects.create(
            created_by=self.source_user,
            club=self.club,
            title="Merge Auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=1),
        )
        self.location = PickupLocation.objects.create(
            name="Merge Location",
            auction=self.auction,
            pickup_time=now + datetime.timedelta(days=2),
        )
        self.source_member = ClubMember.objects.create(
            club=self.club,
            user=self.source_user,
            name="Source Member",
            email="source.member@example.com",
            phone_number="555-1111",
            membership_last_paid=now.date(),
            permission_manage_bap=True,
        )
        self.target_member = ClubMember.objects.create(
            club=self.club,
            user=self.target_user,
            name="Target Member",
            email="",
        )
        self.source_tos = AuctionTOS.objects.create(
            user=self.source_user,
            auction=self.auction,
            pickup_location=self.location,
            clubmember=self.source_member,
            name="Source TOS",
            email="source.tos@example.com",
        )
        self.target_tos = AuctionTOS.objects.create(
            user=self.target_user,
            auction=self.auction,
            pickup_location=self.location,
            clubmember=self.target_member,
            name="Target TOS",
            email="target.tos@example.com",
        )
        self.target_invoice = Invoice.objects.get_or_create(auctiontos_user=self.target_tos, auction=self.auction)[0]
        self.source_invoice = Invoice.objects.get_or_create(auctiontos_user=self.source_tos, auction=self.auction)[0]
        InvoiceAdjustment.objects.create(invoice=self.source_invoice, adjustment_type="ADD", amount=5, notes="move me")
        self.source_payment = InvoicePayment.objects.create(
            club_member=self.source_member,
            payment_target="CLUB_MEMBER",
            amount=Decimal("15.00"),
        )
        self.source_award = BapAward.objects.create(
            club_member=self.source_member,
            date=now.date(),
            points=7,
            notes="move me too",
        )
        self.seller_lot = Lot.objects.create(
            lot_name="Seller Lot",
            auction=self.auction,
            auctiontos_seller=self.source_tos,
            user=self.source_user,
            quantity=1,
        )
        self.winner_lot = Lot.objects.create(
            lot_name="Winner Lot",
            auction=self.auction,
            auctiontos_seller=self.target_tos,
            auctiontos_winner=self.source_tos,
            winner=self.source_user,
            winning_price=Decimal("10.00"),
            active=False,
            quantity=1,
        )
        self.membership_invoice = Invoice.objects.create(
            club=self.club,
            buyer=self.source_user,
            status="UNPAID",
            renewal_needed=True,
        )
        self.bid = Bid.objects.create(user=self.source_user, lot_number=self.seller_lot, amount=Decimal("12.00"))
        Watch.objects.create(user=self.target_user, lot_number=self.seller_lot)
        self.source_watch = Watch.objects.create(user=self.source_user, lot_number=self.seller_lot)
        self.page_view = PageView.objects.create(user=self.source_user, lot_number=self.seller_lot)
        self.auction_ignore = AuctionIgnore.objects.create(user=self.source_user, auction=self.auction)
        self.user_ignore_category = UserIgnoreCategory.objects.create(
            user=self.source_user, category=Category.objects.create(name="Killifish")
        )
        self.user_interest = UserInterestCategory.objects.create(
            user=self.source_user,
            category=Category.objects.create(name="Cichlids"),
            interest=4,
        )
        self.search_history = SearchHistory.objects.create(user=self.source_user, search="killifish")
        self.auction_campaign = AuctionCampaign.objects.create(auction=self.auction, user=self.source_user)
        self.target_chat = ChatSubscription.objects.create(
            user=self.target_user,
            lot=self.seller_lot,
        )
        self.target_chat.last_seen = now - datetime.timedelta(days=2)
        self.target_chat.last_notification_sent = now - datetime.timedelta(days=2)
        self.target_chat.save(update_fields=["last_seen", "last_notification_sent"])
        self.source_chat = ChatSubscription.objects.create(
            user=self.source_user,
            lot=self.seller_lot,
        )
        self.source_chat.last_seen = now - datetime.timedelta(days=1)
        self.source_chat.last_notification_sent = now - datetime.timedelta(days=1)
        self.source_chat.unsubscribed = True
        self.source_chat.save(update_fields=["last_seen", "last_notification_sent", "unsubscribed"])
        PayPalSeller.objects.create(user=self.source_user, paypal_merchant_id="paypal_123", payer_email="paypal@test")
        SquareSeller.objects.create(
            user=self.source_user,
            square_merchant_id="square_123",
            access_token="token",
            refresh_token="refresh",
            payer_email="square@test",
        )

        self.target_user.userdata.credit = Decimal("1.50")
        self.target_user.userdata.save(update_fields=["credit"])
        self.source_user.userdata.phone_number = "555-0000"
        self.source_user.userdata.address = "123 Source St"
        self.source_user.userdata.credit = Decimal("8.50")
        self.source_user.userdata.is_trusted = True
        self.source_user.userdata.paypal_enabled = True
        self.source_user.userdata.square_enabled = True
        self.source_user.userdata.save(
            update_fields=["phone_number", "address", "credit", "is_trusted", "paypal_enabled", "square_enabled"]
        )

    def test_merge_into_moves_domain_data_and_empties_source_userdata(self):
        self.source_user.userdata.merge_into(self.target_user)

        self.auction.refresh_from_db()
        self.club.refresh_from_db()
        self.target_user.userdata.refresh_from_db()
        self.source_user.userdata.refresh_from_db()
        self.seller_lot.refresh_from_db()
        self.winner_lot.refresh_from_db()
        self.membership_invoice.refresh_from_db()
        self.bid.refresh_from_db()
        self.page_view.refresh_from_db()
        self.search_history.refresh_from_db()
        self.auction_campaign.refresh_from_db()
        self.source_payment.refresh_from_db()
        self.source_award.refresh_from_db()
        self.target_member.refresh_from_db()
        self.source_member.refresh_from_db()
        self.target_chat.refresh_from_db()

        self.assertEqual(self.auction.created_by, self.target_user)
        self.assertFalse(AuctionTOS.objects.filter(pk=self.source_tos.pk).exists())
        self.assertTrue(
            InvoiceAdjustment.objects.filter(invoice__auctiontos_user=self.target_tos, notes="move me").exists()
        )
        self.assertFalse(Invoice.objects.filter(pk=self.source_invoice.pk).exists())
        self.assertEqual(self.seller_lot.auctiontos_seller, self.target_tos)
        self.assertEqual(self.seller_lot.user, self.target_user)
        self.assertEqual(self.winner_lot.auctiontos_winner, self.target_tos)
        self.assertEqual(self.winner_lot.winner, self.target_user)
        self.assertEqual(self.membership_invoice.buyer, self.target_user)
        self.assertEqual(self.bid.user, self.target_user)
        self.assertEqual(self.page_view.user, self.target_user)
        self.assertEqual(self.source_payment.club_member, self.target_member)
        self.assertEqual(self.source_award.club_member, self.target_member)
        self.assertEqual(self.target_member.email, "source.member@example.com")
        self.assertEqual(self.target_member.phone_number, "555-1111")
        self.assertTrue(self.target_member.permission_manage_bap)
        self.assertEqual(self.target_member.membership_last_paid, self.now.date())
        self.assertTrue(self.source_member.is_deleted)
        self.assertIsNone(self.source_member.user)
        self.assertEqual(Watch.objects.filter(user=self.target_user, lot_number=self.seller_lot).count(), 1)
        self.assertEqual(ChatSubscription.objects.filter(user=self.target_user, lot=self.seller_lot).count(), 1)
        self.assertGreaterEqual(self.target_chat.last_seen, self.source_chat.last_seen)
        self.assertGreaterEqual(self.target_chat.last_notification_sent, self.source_chat.last_notification_sent)
        self.assertTrue(self.target_chat.unsubscribed)
        self.assertTrue(AuctionIgnore.objects.filter(user=self.target_user, auction=self.auction).exists())
        self.assertTrue(
            UserIgnoreCategory.objects.filter(
                user=self.target_user, category=self.user_ignore_category.category
            ).exists()
        )
        self.assertTrue(
            UserInterestCategory.objects.filter(user=self.target_user, category=self.user_interest.category).exists()
        )
        self.assertEqual(self.search_history.user, self.target_user)
        self.assertEqual(self.auction_campaign.user, self.target_user)
        self.assertEqual(self.target_user.userdata.credit, Decimal("10.00"))
        self.assertEqual(self.target_user.userdata.phone_number, "555-0000")
        self.assertEqual(self.target_user.userdata.address, "123 Source St")
        self.assertTrue(self.target_user.userdata.is_trusted)
        self.assertTrue(self.target_user.userdata.paypal_enabled)
        self.assertTrue(self.target_user.userdata.square_enabled)
        self.assertIsNone(self.source_user.userdata.phone_number)
        self.assertIsNone(self.source_user.userdata.address)
        self.assertEqual(self.source_user.userdata.credit, 0)
        self.assertFalse(PayPalSeller.objects.filter(user=self.source_user).exists())
        self.assertFalse(SquareSeller.objects.filter(user=self.source_user).exists())
        self.assertTrue(PayPalSeller.objects.filter(user=self.target_user, paypal_merchant_id="paypal_123").exists())
        self.assertTrue(SquareSeller.objects.filter(user=self.target_user, square_merchant_id="square_123").exists())

    def test_management_command_calls_merge_into(self):
        out = io.StringIO()

        call_command(
            "empty_account_and_move_data",
            self.source_user.username,
            self.target_user.username,
            stdout=out,
        )

        self.membership_invoice.refresh_from_db()
        self.target_user.userdata.refresh_from_db()
        self.assertIn(
            f"Moved data from {self.source_user.username} to {self.target_user.username}.",
            out.getvalue(),
        )
        self.assertFalse(AuctionTOS.objects.filter(pk=self.source_tos.pk).exists())
        self.assertEqual(self.membership_invoice.buyer, self.target_user)
        self.assertEqual(self.target_user.userdata.phone_number, "555-0000")
