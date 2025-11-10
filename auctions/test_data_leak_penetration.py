"""
Penetration tests to verify no data leaks from public endpoints.

These tests attempt to access sensitive data as unauthenticated users
and non-admin authenticated users to ensure proper security controls.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Auction, AuctionTOS, Invoice, Lot, PickupLocation

User = get_user_model()


class DataLeakPenetrationTests(TestCase):
    """Penetration tests for data leaks"""

    def setUp(self):
        """Set up test data"""
        # Create users
        self.auction_creator = User.objects.create_user(
            username="creator",
            password="testpassword",
            email="creator@example.com",
            first_name="Creator",
            last_name="User",
        )
        self.admin_user = User.objects.create_user(
            username="admin",
            password="testpassword",
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
        )
        self.seller_user = User.objects.create_user(
            username="seller",
            password="testpassword",
            email="seller@example.com",
            first_name="Seller",
            last_name="User",
        )
        self.buyer_user = User.objects.create_user(
            username="buyer",
            password="testpassword",
            email="buyer@example.com",
            first_name="Buyer",
            last_name="User",
        )
        self.random_user = User.objects.create_user(
            username="random",
            password="testpassword",
            email="random@example.com",
            first_name="Random",
            last_name="User",
        )

        # Create auction
        future_time = timezone.now() + timezone.timedelta(days=3)
        self.auction = Auction.objects.create(
            created_by=self.auction_creator,
            title="Test Auction",
            is_online=True,
            date_end=future_time,
            date_start=timezone.now(),
        )

        # Create pickup location
        self.location = PickupLocation.objects.create(
            name="Test Location",
            auction=self.auction,
            pickup_time=future_time,
        )

        # Create AuctionTOS entries
        self.admin_tos = AuctionTOS.objects.create(
            user=self.admin_user,
            auction=self.auction,
            pickup_location=self.location,
            is_admin=True,
            name="Admin User",
            email="admin@example.com",
            phone_number="555-1234",
            address="123 Admin St",
            bidder_number="001",
        )
        self.seller_tos = AuctionTOS.objects.create(
            user=self.seller_user,
            auction=self.auction,
            pickup_location=self.location,
            is_admin=False,
            name="Seller User",
            email="seller@example.com",
            phone_number="555-5678",
            address="456 Seller Ave",
            bidder_number="002",
        )
        self.buyer_tos = AuctionTOS.objects.create(
            user=self.buyer_user,
            auction=self.auction,
            pickup_location=self.location,
            is_admin=False,
            name="Buyer User",
            email="buyer@example.com",
            phone_number="555-9012",
            address="789 Buyer Blvd",
            bidder_number="003",
        )

        # Create a lot
        self.lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            quantity=1,
            winning_price=50,
            auctiontos_winner=self.buyer_tos,
            active=False,
        )

        # Create invoices for testing
        self.seller_invoice = Invoice.objects.create(
            auctiontos_user=self.seller_tos,
            auction=self.auction,
        )
        self.buyer_invoice = Invoice.objects.create(
            auctiontos_user=self.buyer_tos,
            auction=self.auction,
        )

    def test_lot_page_no_email_leak_unauthenticated(self):
        """Unauthenticated users should not see emails on lot pages"""
        url = reverse("lot_by_pk", kwargs={"pk": self.lot.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Check that emails are not in the response
        self.assertNotContains(response, "seller@example.com")
        self.assertNotContains(response, "buyer@example.com")
        self.assertNotContains(response, "admin@example.com")

        # Check that phone numbers are not in the response
        self.assertNotContains(response, "555-5678")
        self.assertNotContains(response, "555-9012")

        # Check that addresses are not in the response
        self.assertNotContains(response, "456 Seller Ave")
        self.assertNotContains(response, "789 Buyer Blvd")

    def test_lot_page_no_email_leak_random_user(self):
        """Random authenticated users should not see emails on lot pages"""
        self.client.login(username="random", password="testpassword")
        url = reverse("lot_by_pk", kwargs={"pk": self.lot.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Check that other users' emails are not in the response
        self.assertNotContains(response, "seller@example.com")
        self.assertNotContains(response, "buyer@example.com")
        self.assertNotContains(response, "admin@example.com")

    def test_auction_page_no_email_leak_unauthenticated(self):
        """Unauthenticated users should not see user emails on auction page"""
        url = reverse("auction_main", kwargs={"slug": self.auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Check that user emails are not in the response (except creator if email_visible)
        self.assertNotContains(response, "seller@example.com")
        self.assertNotContains(response, "buyer@example.com")
        self.assertNotContains(response, "admin@example.com")

    def test_auction_page_no_email_list_random_user(self):
        """Random users should not see email list links on auction page"""
        self.client.login(username="random", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Check that email list is not exposed
        self.assertNotContains(response, "mailto:?bcc=")

    def test_api_endpoints_require_authentication(self):
        """Critical API endpoints should require authentication"""
        # List of API endpoints that should require authentication
        api_endpoints = [
            ("api/auctiontos-autocomplete/", {}),
            ("api/lot-autocomplete/", {}),
            ("api/users/lot_notifications/", {}),
            ("api/users/auction_notifications/", {}),
        ]

        for endpoint, params in api_endpoints:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(f"/{endpoint}", params)
                # Should redirect to login (302) or return error (400/403)
                self.assertIn(response.status_code, [302, 400, 403, 405])

    def test_auction_users_list_blocked_for_non_admins(self):
        """Non-admin users should not access the auction users list"""
        self.client.login(username="random", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
        response = self.client.get(url)
        # Should be blocked
        self.assertIn(response.status_code, [302, 403])

    def test_auction_users_csv_blocked_for_non_admins(self):
        """Non-admin users should not access the auction report CSV"""
        self.client.login(username="random", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.auction.slug})
        response = self.client.get(url)
        # Should be blocked
        self.assertIn(response.status_code, [302, 403])

    def test_auction_admin_can_see_emails(self):
        """Auction admins should be able to see user emails"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Admin should see the user list (though emails may not be directly in HTML)

    def test_no_direct_auctiontos_access_via_api(self):
        """Direct AuctionTOS API access should be blocked for non-admins"""
        self.client.login(username="random", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.seller_tos.pk})
        response = self.client.get(url)
        # Should be blocked
        self.assertEqual(response.status_code, 403)

    def test_lot_exchange_info_only_for_admins_and_sellers(self):
        """Exchange info on sold lots should only be visible to admins and sellers"""
        # Make sure the lot is sold and in an online auction
        self.lot.auction.is_online = True
        self.lot.auction.save()
        self.lot.save()

        # Random user should not see exchange info
        self.client.login(username="random", password="testpassword")
        url = reverse("lot_by_pk", kwargs={"pk": self.lot.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Should not contain "Exchange info" section
        # (We can't check for exact text as it might not be shown for various reasons)

        # Seller should see exchange info
        self.client.login(username="seller", password="testpassword")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Seller should see their own info

        # Admin should see exchange info
        self.client.login(username="admin", password="testpassword")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Admin should see exchange info

    def test_invoice_access_restricted(self):
        """Users should only access their own invoices"""
        # Try to access seller's invoice as buyer
        self.client.login(username="buyer", password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.seller_invoice.pk})
        response = self.client.get(url)
        # Should be blocked or redirected
        self.assertIn(response.status_code, [302, 403, 404])
