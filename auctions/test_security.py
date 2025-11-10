"""
Security tests to ensure AuctionTOS and user data is properly protected.

These tests verify that:
1. Unauthenticated users cannot access user/AuctionTOS data
2. Non-admin authenticated users cannot access user/AuctionTOS data
3. Auction admins CAN access user/AuctionTOS data for their auctions only
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Auction, AuctionTOS, PickupLocation

User = get_user_model()


class AuctionTOSSecurityTestCase(TestCase):
    """Test security for AuctionTOS data access"""

    def setUp(self):
        """Set up test data"""
        # Create users
        self.auction_creator = User.objects.create_user(
            username="creator", password="testpassword", email="creator@example.com"
        )
        self.auction_admin = User.objects.create_user(
            username="admin", password="testpassword", email="admin@example.com"
        )
        self.regular_user = User.objects.create_user(
            username="regular", password="testpassword", email="regular@example.com"
        )
        self.other_user = User.objects.create_user(username="other", password="testpassword", email="other@example.com")

        # Create auctions
        future_time = timezone.now() + timezone.timedelta(days=3)
        self.auction1 = Auction.objects.create(
            created_by=self.auction_creator,
            title="Test Auction 1",
            is_online=True,
            date_end=future_time,
            date_start=timezone.now(),
        )
        self.auction2 = Auction.objects.create(
            created_by=self.other_user,
            title="Test Auction 2",
            is_online=True,
            date_end=future_time,
            date_start=timezone.now(),
        )

        # Create pickup locations
        self.location1 = PickupLocation.objects.create(
            name="Location 1",
            auction=self.auction1,
            pickup_time=future_time,
        )
        self.location2 = PickupLocation.objects.create(
            name="Location 2",
            auction=self.auction2,
            pickup_time=future_time,
        )

        # Create AuctionTOS entries
        self.admin_tos = AuctionTOS.objects.create(
            user=self.auction_admin,
            auction=self.auction1,
            pickup_location=self.location1,
            is_admin=True,
            name="Admin User",
            email="admin@example.com",
            bidder_number="001",
        )
        self.regular_tos = AuctionTOS.objects.create(
            user=self.regular_user,
            auction=self.auction1,
            pickup_location=self.location1,
            is_admin=False,
            name="Regular User",
            email="regular@example.com",
            bidder_number="002",
        )
        self.other_tos = AuctionTOS.objects.create(
            user=self.other_user,
            auction=self.auction2,
            pickup_location=self.location2,
            is_admin=False,
            name="Other User",
            email="other@example.com",
            bidder_number="003",
        )

    def test_auctiontos_autocomplete_unauthenticated(self):
        """Unauthenticated users should not access AuctionTOS autocomplete"""
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "test"})
        # Should redirect to login or return empty results
        self.assertIn(response.status_code, [302, 200])
        if response.status_code == 200:
            # If it returns 200, it should be empty results
            self.assertNotContains(response, "Regular User")
            self.assertNotContains(response, "Admin User")

    def test_auctiontos_autocomplete_non_admin(self):
        """Non-admin users should not access AuctionTOS autocomplete"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "test"})
        # Should return empty results as user is not an admin
        self.assertEqual(response.status_code, 200)
        # Check that sensitive data is not exposed
        if hasattr(response, "json"):
            data = response.json()
            # Ensure no results are returned for non-admin
            self.assertEqual(len(data.get("results", [])), 0)

    def test_auctiontos_autocomplete_admin_access(self):
        """Auction admins should access AuctionTOS autocomplete for their auction"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "Regular"})
        # Admin should get results
        self.assertEqual(response.status_code, 200)

    def test_auctiontos_autocomplete_creator_access(self):
        """Auction creators should access AuctionTOS autocomplete for their auction"""
        self.client.login(username="creator", password="testpassword")
        url = reverse("auctiontos-autocomplete")
        response = self.client.get(url, {"auction": self.auction1.pk, "q": "Regular"})
        # Creator should get results
        self.assertEqual(response.status_code, 200)

    def test_auctiontos_admin_view_unauthenticated(self):
        """Unauthenticated users should not access AuctionTOS admin view"""
        url = reverse("auctiontosadmin", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auctiontos_admin_view_non_admin(self):
        """Non-admin users should not access AuctionTOS admin view"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auctiontos_admin_view_admin_access(self):
        """Auction admins should access AuctionTOS admin view"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_auctiontos_delete_unauthenticated(self):
        """Unauthenticated users should not access AuctionTOS delete"""
        url = reverse("auctiontosdelete", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auctiontos_delete_non_admin(self):
        """Non-admin users should not access AuctionTOS delete"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontosdelete", kwargs={"pk": self.regular_tos.pk})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auctiontos_memo_unauthenticated(self):
        """Unauthenticated users should not access memo endpoint"""
        url = reverse("auctiontosmemo", kwargs={"pk": self.regular_tos.pk})
        response = self.client.post(url, {"memo": "test memo"})
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auctiontos_memo_non_admin(self):
        """Non-admin users should not access memo endpoint"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontosmemo", kwargs={"pk": self.regular_tos.pk})
        response = self.client.post(url, {"memo": "test memo"})
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auction_users_list_unauthenticated(self):
        """Unauthenticated users should not access auction users list"""
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auction_users_list_non_admin(self):
        """Non-admin users should not access auction users list"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auction_users_list_admin_access(self):
        """Auction admins should access auction users list"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)
        # Should see user data
        self.assertContains(response, "Regular User")

    def test_auction_users_list_creator_access(self):
        """Auction creators should access auction users list"""
        self.client.login(username="creator", password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_auction_report_csv_unauthenticated(self):
        """Unauthenticated users should not access auction report CSV"""
        url = reverse("user_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_auction_report_csv_non_admin(self):
        """Non-admin users should not access auction report CSV"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auction_report_csv_admin_access(self):
        """Auction admins should access auction report CSV"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)
        # Should be CSV
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_compose_email_users_unauthenticated(self):
        """Unauthenticated users should not access compose email page"""
        url = reverse("compose_email_to_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_compose_email_users_non_admin(self):
        """Non-admin users should not access compose email page"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_compose_email_users_admin_access(self):
        """Auction admins should access compose email page"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_bulk_add_users_unauthenticated(self):
        """Unauthenticated users should not access bulk add users"""
        url = reverse("bulk_add_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should redirect to login or be denied
        self.assertIn(response.status_code, [302, 403])

    def test_bulk_add_users_non_admin(self):
        """Non-admin users should not access bulk add users"""
        self.client.login(username="regular", password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_bulk_add_users_admin_access(self):
        """Auction admins should access bulk add users"""
        self.client.login(username="admin", password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.auction1.slug})
        response = self.client.get(url)
        # Should be allowed
        self.assertEqual(response.status_code, 200)

    def test_admin_cannot_access_other_auction_data(self):
        """Auction admins should not access data from other auctions"""
        self.client.login(username="admin", password="testpassword")
        # Try to access auction2 data (admin is not admin of auction2)
        url = reverse("auction_tos_list", kwargs={"slug": self.auction2.slug})
        response = self.client.get(url)
        # Should be denied
        self.assertEqual(response.status_code, 403)

    def test_auctiontos_validation_requires_admin(self):
        """AuctionTOS validation endpoint requires admin access"""
        # Non-admin
        self.client.login(username="regular", password="testpassword")
        url = reverse("auctiontos_validation", kwargs={"slug": self.auction1.slug})
        response = self.client.post(url, {"name": "Test"})
        # Should be denied
        self.assertEqual(response.status_code, 403)

        # Admin should work
        self.client.login(username="admin", password="testpassword")
        response = self.client.post(url, {"name": "Test"})
        # Should be allowed
        self.assertEqual(response.status_code, 200)
