"""Importing lots and users from a CSV or a club's Google Drive sheet."""

import csv
import datetime
import io

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubMember,
    Lot,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase


class AuctionHistoryTests(StandardTestCase):
    """Test that auction history is properly tracked for lot operations and user joins"""

    def test_lot_edit_creates_history(self):
        """Test that editing a lot creates an audit history entry"""
        self.client.login(username="my_lot", password="testpassword")

        # Set up user data required by LotValidation
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        # Create an auction with lot submission still open
        theFuture = timezone.now() + datetime.timedelta(days=3)
        test_auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for editing",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now(),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
        )
        test_location = PickupLocation.objects.create(name="test location", auction=test_auction, pickup_time=theFuture)
        test_tos = AuctionTOS.objects.create(user=self.user, auction=test_auction, pickup_location=test_location)

        # Create a lot that can be edited (no winner, no bids)
        editable_lot = Lot.objects.create(
            lot_name="Editable test lot",
            auction=test_auction,
            auctiontos_seller=test_tos,
            quantity=1,
            user=self.user,
        )

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()

        # Edit a lot - provide all required fields
        url = reverse("edit_lot", kwargs={"pk": editable_lot.pk})

        response = self.client.post(
            url,
            {
                "part_of_auction": True,
                "auction": test_auction.pk,
                "lot_name": "Updated Lot Name",
                "quantity": 2,
                "reserve_price": 2,
                "summernote_description": "test",
                "donation": False,
                "i_bred_this_fish": False,
                "buy_now_price": "",
                "custom_checkbox": False,
                "custom_field_1": "text",
            },
            follow=True,  # follow to the selling redirect
        )
        assert response.status_code == 200
        # Check that history was created
        new_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()
        assert new_count == initial_count + 1

        # Verify the history entry
        history = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").latest("timestamp")
        assert "Edited lot" in history.action
        assert history.user == self.user

    def test_lot_delete_creates_history(self):
        """Test that deleting a lot creates an audit history entry"""
        self.client.login(username="my_lot", password="testpassword")

        # Set up user data required by LotValidation
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        # Create an auction with lot submission still open
        theFuture = timezone.now() + datetime.timedelta(days=3)
        test_auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for deleting",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now(),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
        )
        test_location = PickupLocation.objects.create(name="test location", auction=test_auction, pickup_time=theFuture)
        test_tos = AuctionTOS.objects.create(user=self.user, auction=test_auction, pickup_location=test_location)

        deletable_lot = Lot.objects.create(
            lot_name="Deletable test lot",
            auction=test_auction,
            auctiontos_seller=test_tos,
            quantity=1,
            user=self.user,
        )

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()

        # Delete the lot
        self.client.post(reverse("delete_lot", kwargs={"pk": deletable_lot.pk}), follow=True)

        # Check that history was created
        new_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()
        assert new_count == initial_count + 1

        # Verify the history entry
        history = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").latest("timestamp")
        assert "Deleted lot" in history.action
        assert history.user == self.user

    def test_user_join_creates_history_only_once(self):
        """Test that joining an auction creates history only on first join"""
        # Create a new user who hasn't joined yet
        User.objects.create_user(username="new_user", password="testpassword", email="new@example.com")
        self.client.login(username="new_user", password="testpassword")

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()

        # Join the auction for the first time
        self.client.post(
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}),
            {
                "pickup_location": self.location.pk,
                "i_agree": True,
                "time_spent_reading_rules": 10,
            },
        )

        # Check that history was created
        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        assert new_count == initial_count + 1

        # Verify the history entry
        history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").latest("timestamp")
        assert "has joined this auction" in history.action

        # Join again (re-submit the same form)
        self.client.post(
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}),
            {
                "pickup_location": self.location.pk,
                "i_agree": True,
                "time_spent_reading_rules": 20,
            },
        )

        # Check that NO new history was created
        final_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        assert final_count == new_count  # Should be the same as after first join


class CSVImportTests(StandardTestCase):
    """Test CSV import functionality for bulk adding users"""

    def test_csv_import_with_memo_field(self):
        """Test that memo field is correctly imported from CSV"""
        import csv
        from io import StringIO

        # Create CSV content with memo field
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "memo"])
        writer.writerow(["test1@example.com", "Test User 1", "This is a test memo"])
        writer.writerow(["test2@example.com", "Test User 2", "Another memo"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that users were created with memo
        tos1 = AuctionTOS.objects.filter(auction=self.online_auction, email="test1@example.com").first()
        tos2 = AuctionTOS.objects.filter(auction=self.online_auction, email="test2@example.com").first()

        self.assertIsNotNone(tos1)
        self.assertIsNotNone(tos2)
        self.assertEqual(tos1.memo, "This is a test memo")
        self.assertEqual(tos2.memo, "Another memo")

    def test_csv_import_with_admin_field(self):
        import csv
        from io import StringIO

        # Create CSV content with proper formatting
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "admin"])
        writer.writerow(["admin1@example.com", "Admin 1", "yes"])
        writer.writerow(["admin2@example.com", "Admin 2", "true"])
        writer.writerow(["admin3@example.com", "Admin 3", "1"])
        writer.writerow(["regular@example.com", "Regular User", "no"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that admin users were created correctly
        admin1 = AuctionTOS.objects.filter(auction=self.online_auction, email="admin1@example.com").first()
        admin2 = AuctionTOS.objects.filter(auction=self.online_auction, email="admin2@example.com").first()
        admin3 = AuctionTOS.objects.filter(auction=self.online_auction, email="admin3@example.com").first()
        regular = AuctionTOS.objects.filter(auction=self.online_auction, email="regular@example.com").first()

        self.assertIsNotNone(admin1)
        self.assertIsNotNone(admin2)
        self.assertIsNotNone(admin3)
        self.assertIsNotNone(regular)

        self.assertTrue(admin1.is_admin)
        self.assertTrue(admin2.is_admin)
        self.assertTrue(admin3.is_admin)
        self.assertFalse(regular.is_admin)

    def test_csv_import_with_staff_field(self):
        """Test that 'staff' column name also works for admin field"""
        import csv
        from io import StringIO

        # Create CSV content with proper formatting
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "staff"])
        writer.writerow(["staff1@example.com", "Staff 1", "yes"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that admin user was created
        staff1 = AuctionTOS.objects.filter(auction=self.online_auction, email="staff1@example.com").first()
        self.assertIsNotNone(staff1)
        self.assertTrue(staff1.is_admin)

    def test_csv_import_bidder_number_not_in_use(self):
        """Test that bidder number from CSV is used if not already in use"""
        import csv
        from io import StringIO

        # Create CSV content with proper formatting
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["bidder1@example.com", "Bidder 1", "9999"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # 9999 is outside the 1-999 auto-generation range.
        bidder1 = AuctionTOS.objects.filter(auction=self.online_auction, email="bidder1@example.com").first()
        self.assertIsNotNone(bidder1)
        self.assertEqual(bidder1.bidder_number, "9999")

    def test_csv_import_bidder_number_in_use_new_user(self):
        """Test that bidder number is not assigned if already in use for a new user"""
        import csv
        from io import StringIO

        # Create an existing user with bidder number 777
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            bidder_number="777",
        )

        # Create CSV content with same bidder number
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["newuser@example.com", "New User", "777"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        new_user = AuctionTOS.objects.filter(auction=self.online_auction, email="newuser@example.com").first()
        self.assertIsNotNone(new_user)
        self.assertNotEqual(new_user.bidder_number, "777")

    def test_csv_import_bidder_number_update_existing_user(self):
        import csv
        from io import StringIO

        # Create an existing user without bidder number
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            bidder_number="",
        )
        # save() auto-assigns a random bidder_number; clear it for the test
        AuctionTOS.objects.filter(pk=existing_tos.pk).update(bidder_number="")
        # Auto-generated numbers may collide with the target.
        for other in AuctionTOS.objects.filter(auction=self.online_auction, bidder_number="888").exclude(
            pk=existing_tos.pk
        ):
            AuctionTOS.objects.filter(pk=other.pk).update(bidder_number=f"x{other.pk}")

        # Create CSV content to update with bidder number
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["existing@example.com", "Existing User", "888"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that bidder number was updated
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.bidder_number, "888")

    def test_csv_import_bidder_number_exclude_self(self):
        """Test that bidder number check excludes the user being updated"""
        import csv
        from io import StringIO

        # Create an existing user with bidder number
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            bidder_number="666",
        )

        # Create CSV content with same bidder number (re-importing same user)
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["existing@example.com", "Existing User Updated", "666"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that bidder number was kept (not cleared)
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.bidder_number, "666")
        self.assertEqual(existing_tos.name, "Existing User Updated")

    def test_csv_import_update_existing_user_memo_and_admin(self):
        """Test that existing user's memo and admin status are updated from CSV"""
        import csv
        from io import StringIO

        # Create an existing user
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            memo="",
            is_admin=False,
        )

        # Create CSV content to update memo and admin status
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "memo", "admin"])
        writer.writerow(["existing@example.com", "Existing User", "Updated memo", "yes"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that memo and admin were updated
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.memo, "Updated memo")
        self.assertTrue(existing_tos.is_admin)

    def test_csv_import_update_creates_history(self):
        """Test that updating a user via CSV creates a summary history entry"""
        import csv
        from io import StringIO

        # Create an existing user
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="update@example.com",
            name="User to Update",
            memo="",
        )

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()

        # Create CSV content to update the user
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "memo"])
        writer.writerow(["update@example.com", "User to Update", "New memo from CSV"])

        csv_file = SimpleUploadedFile(
            "update_users.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv"
        )

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that user was updated
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.memo, "New memo from CSV")

        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        self.assertEqual(new_count, initial_count + 1)

        update_history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").latest(
            "timestamp"
        )
        self.assertIn("1 users updated", update_history.action)
        self.assertIn("update_users.csv", update_history.action)

    def test_csv_import_no_change_no_update(self):
        """Test that users are only counted as updated if they actually change"""
        import csv
        from io import StringIO

        # Create an existing user with memo already set
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="nochange@example.com",
            name="No Change User",
            memo="Existing memo",
        )

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()

        # Create CSV content with same data (no actual change)
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "memo"])
        writer.writerow(["nochange@example.com", "No Change User", "Existing memo"])

        csv_file = SimpleUploadedFile("no_change.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        self.assertEqual(new_count, initial_count)

    def test_csv_import_records_filename_in_history(self):
        """Test that CSV import records the filename in auction history"""
        import csv
        from io import StringIO

        # Create CSV content
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name"])
        writer.writerow(["newuser@example.com", "New User"])

        csv_file = SimpleUploadedFile(
            "users_import.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv"
        )

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()

        # Import CSV
        self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

        # Check that history was created with filename
        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        self.assertEqual(new_count, initial_count + 1)

        history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").latest("timestamp")
        self.assertIn("users_import.csv", history.action)
        self.assertIn("1 users added", history.action)


class CSVImportBiddingPermissionTests(StandardTestCase):
    """The "bidding allowed" column: a blank cell must not revoke bidding, since the user CSV export
    leaves it blank for everyone who can bid.
    """

    def _import(self, rows, header=("email", "name", "bidding allowed")):
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")
        self.client.login(username="admin_user", password="testpassword")
        return self.run_csv_import(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            csv_file,
        )

    def _tos(self, email):
        return AuctionTOS.objects.filter(auction=self.online_auction, email=email).first()

    def test_blank_bidding_cell_does_not_disable_bidding_for_a_new_user(self):
        self._import([["new@example.com", "New User", ""]])
        self.assertTrue(self._tos("new@example.com").bidding_allowed)

    def test_blank_bidding_cell_leaves_an_existing_user_alone(self):
        self.online_tos.email = "existing@example.com"
        self.online_tos.bidding_allowed = True
        self.online_tos.save()
        self._import([["existing@example.com", self.online_tos.name or "Existing User", ""]])
        self.online_tos.refresh_from_db()
        self.assertTrue(self.online_tos.bidding_allowed)

    def test_explicit_no_disables_bidding(self):
        self.online_tos.email = "existing@example.com"
        self.online_tos.bidding_allowed = True
        self.online_tos.save()
        self._import([["existing@example.com", self.online_tos.name or "Existing User", "No"]])
        self.online_tos.refresh_from_db()
        self.assertFalse(self.online_tos.bidding_allowed)

    def test_explicit_yes_restores_bidding(self):
        self.online_tos.email = "existing@example.com"
        self.online_tos.bidding_allowed = False
        self.online_tos.save()
        self._import([["existing@example.com", self.online_tos.name or "Existing User", "Yes"]])
        self.online_tos.refresh_from_db()
        self.assertTrue(self.online_tos.bidding_allowed)

    def test_common_yes_and_no_spellings_are_understood(self):
        self._import(
            [
                ["y@example.com", "Y User", "Y"],
                ["one@example.com", "One User", "1"],
                ["padded@example.com", "Padded User", "  TRUE  "],
                ["x@example.com", "X User", "x"],
                ["n@example.com", "N User", "N"],
                ["zero@example.com", "Zero User", "0"],
                ["false@example.com", "False User", " False "],
            ]
        )
        for email in ("y@example.com", "one@example.com", "padded@example.com", "x@example.com"):
            self.assertTrue(self._tos(email).bidding_allowed, f"{email} should be allowed to bid")
        for email in ("n@example.com", "zero@example.com", "false@example.com"):
            self.assertFalse(self._tos(email).bidding_allowed, f"{email} should not be allowed to bid")

    def test_unreadable_value_leaves_bidding_at_the_default(self):
        self._import([["huh@example.com", "Huh User", "maybe?"]])
        self.assertTrue(self._tos("huh@example.com").bidding_allowed)

    def test_no_bidding_column_at_all_allows_bidding(self):
        self._import([["nocolumn@example.com", "No Column User"]], header=("email", "name"))
        self.assertTrue(self._tos("nocolumn@example.com").bidding_allowed)

    def test_explicit_no_wins_over_the_manually_added_default(self):
        """AuctionTOS.save() force-allows bidding for manually added users; an explicit "no" still wins."""
        self.online_auction.only_approved_bidders = True
        self.online_auction.save()
        self._import(
            [
                ["denied@example.com", "Denied User", "no"],
                ["allowed@example.com", "Allowed User", ""],
            ]
        )
        self.assertFalse(self._tos("denied@example.com").bidding_allowed)
        self.assertTrue(self._tos("allowed@example.com").bidding_allowed)

    def test_blank_admin_cell_does_not_strip_an_existing_admin(self):
        """A blank admin cell doesn't strip an existing admin."""
        self.admin_online_tos.email = "theadmin@example.com"
        self.admin_online_tos.save()
        self._import(
            [["theadmin@example.com", self.admin_online_tos.name, ""]],
            header=("email", "name", "admin"),
        )
        self.assertEqual(
            AuctionTOS.objects.filter(auction=self.online_auction, email="theadmin@example.com").count(), 1
        )
        # ...and the blank cell left their admin flag alone.
        self.admin_online_tos.refresh_from_db()
        self.assertTrue(self.admin_online_tos.is_admin)

    def test_user_csv_export_spells_out_bidding_allowed(self):
        """The user CSV export spells out bidding allowed, so it round-trips."""
        self.online_tos.bidding_allowed = True
        self.online_tos.save()
        self.tosB.bidding_allowed = False
        self.tosB.save()
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("user_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(response.content.decode("utf-8").splitlines()))
        column = rows[0].index("Bidding allowed")
        values = {row[column] for row in rows[1:] if row}
        self.assertIn("Yes", values)
        self.assertIn("No", values)
        self.assertNotIn("", values)


class EnableBiddingForAllUsersTests(StandardTestCase):
    """The bulk "enable bidding for all users" repair on the users page."""

    def _url(self):
        return reverse("auction_enable_bidding_for_all", kwargs={"slug": self.online_auction.slug})

    def _disable_bidding_for_everyone(self):
        AuctionTOS.objects.filter(auction=self.online_auction).update(bidding_allowed=False)

    def test_button_is_hidden_when_everyone_can_bid(self):
        self.assertEqual(self.online_auction.users_with_bidding_disabled, 0)
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertNotContains(response, "Enable bidding for all users")

    def test_button_appears_once_someone_cannot_bid(self):
        self.tosB.bidding_allowed = False
        self.tosB.save()
        self.assertEqual(self.online_auction.users_with_bidding_disabled, 1)
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Enable bidding for all users")

    def test_check_in_auctions_never_offer_it(self):
        """Never offered in check-in mode, where bidding off until check-in is intended."""
        club = Club.objects.create(name="Bidding Repair Club")
        self.online_auction.club = club
        self.online_auction.manage_users_through_club = "checkin"
        self.online_auction.save()
        self._disable_bidding_for_everyone()
        self.assertEqual(self.online_auction.users_with_bidding_disabled, 0)
        self.client.login(username="admin_user", password="testpassword")
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_modal_names_the_number_of_affected_users(self):
        self._disable_bidding_for_everyone()
        count = self.online_auction.users_with_bidding_disabled
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Enable bidding for {count} users?")
        self.assertContains(response, "cannot be undone")

    def test_post_enables_bidding_and_records_history(self):
        self._disable_bidding_for_everyone()
        count = self.online_auction.users_with_bidding_disabled
        self.assertTrue(count)
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            AuctionTOS.objects.filter(auction=self.online_auction, bidding_allowed=False).exists(),
        )
        history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").latest("timestamp")
        self.assertIn(f"Enabled bidding for {count} users", history.action)

    def test_the_user_who_could_not_bid_can_bid_afterwards(self):
        """End to end: the repaired user passes check_bidding_permissions."""
        from auctions.bidding import check_bidding_permissions

        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()
        self.unsoldLot.active = True
        self.unsoldLot.date_end = self.online_auction.date_end
        self.unsoldLot.save()
        self.tosB.bidding_allowed = False
        self.tosB.save()
        self.assertEqual(
            check_bidding_permissions(self.unsoldLot, self.userB),
            "This auction requires admin approval before you can bid",
        )
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(self._url())
        self.tosB.refresh_from_db()
        self.assertTrue(self.tosB.bidding_allowed)
        self.assertNotEqual(
            check_bidding_permissions(self.unsoldLot, self.userB),
            "This auction requires admin approval before you can bid",
        )

    def test_non_admin_cannot_use_it(self):
        self._disable_bidding_for_everyone()
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.post(self._url())
        self.assertIn(response.status_code, [302, 403])
        self.assertTrue(AuctionTOS.objects.filter(auction=self.online_auction, bidding_allowed=False).exists())

    def test_club_managed_auction_also_fixes_the_member_records(self):
        """Club-managed auctions fix the member records too."""
        club = Club.objects.create(name="Managed Bidding Club")
        self.online_auction.club = club
        self.online_auction.manage_users_through_club = "all"
        self.online_auction.save()
        member = ClubMember.objects.create(club=club, name="Managed Member", email="managed@example.com")
        tos = AuctionTOS.objects.filter(auction=self.online_auction, clubmember=member).first()
        self.assertIsNotNone(tos, "creating a member should have created its shadow participant record")
        ClubMember.objects.filter(pk=member.pk).update(bidding_allowed=False)
        AuctionTOS.objects.filter(pk=tos.pk).update(bidding_allowed=False)
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(self._url())
        tos.refresh_from_db()
        member.refresh_from_db()
        self.assertTrue(tos.bidding_allowed)
        self.assertTrue(member.bidding_allowed)


class CSVImportPreviewTests(StandardTestCase):
    """The preview/confirm flow and duplicate handling for the AuctionTOS importer."""

    def _bulk_add_url(self):
        return reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})

    def test_upload_without_confirm_creates_nothing(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        csv_file = SimpleUploadedFile(
            "u.csv", b"name,email\nPreview Only,previewonly@example.com\n", content_type="text/csv"
        )
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        # Upload but never POST the confirm token.
        upload = self.client.post(self._bulk_add_url(), {"csv_file": csv_file})
        self.assertIn("preview=", upload.get("Location", ""))
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before)
        self.assertFalse(
            AuctionTOS.objects.filter(auction=self.online_auction, email="previewonly@example.com").exists()
        )

    def test_preview_page_renders_with_duplicate_radios(self):
        """GET ?preview renders the merge/create choice for a possible duplicate."""
        self.online_tos.name = "Bob Smith"
        self.online_tos.email = "bob@example.com"
        self.online_tos.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        csv_file = SimpleUploadedFile(
            "signin.csv",
            b"name,bidder number,email\nBob Smith,9998,\nNew Person,9997,newperson@example.com\n",
            content_type="text/csv",
        )
        upload = self.client.post(self._bulk_add_url(), {"csv_file": csv_file})
        preview = self.client.get(upload["Location"])
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Review import")
        self.assertContains(preview, "Merge into existing")
        self.assertContains(preview, "Bob Smith")

    def test_no_email_walkin_merges_into_existing_online_record(self):
        """A no-email check-in row for someone who joined online is a possible duplicate, and the default
        merge updates the existing record with the check-in bidder number.
        """
        self.online_tos.name = "Bob Smith"
        self.online_tos.email = "bob@example.com"
        self.online_tos.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        csv_file = SimpleUploadedFile("signin.csv", b"name,bidder number\nBob Smith,9998\n", content_type="text/csv")
        # No decisions passed -> default 'merge'.
        self.run_csv_import(self._bulk_add_url(), csv_file)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before)
        self.online_tos.refresh_from_db()
        self.assertEqual(self.online_tos.bidder_number, "9998")
        self.assertEqual(self.online_tos.email, "bob@example.com")

    def test_no_email_walkin_create_choice_makes_flagged_duplicate(self):
        """Choosing 'create' makes a second record flagged as a possible duplicate."""
        self.online_tos.name = "Bob Smith"
        self.online_tos.email = "bob@example.com"
        self.online_tos.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        csv_file = SimpleUploadedFile("signin.csv", b"name,bidder number\nBob Smith,9998\n", content_type="text/csv")
        self.run_csv_import(self._bulk_add_url(), csv_file, decisions={0: "create"})
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before + 1)
        new = AuctionTOS.objects.filter(auction=self.online_auction, bidder_number="9998").first()
        self.assertIsNotNone(new)
        self.assertEqual(new.possible_duplicate, self.online_tos)

    def test_email_match_is_case_and_whitespace_insensitive(self):
        self.online_tos.name = "Carol"
        self.online_tos.email = "carol@example.com"
        self.online_tos.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        csv_file = SimpleUploadedFile(
            "u.csv", b"name,email,memo\nCarol, Carol@Example.COM ,vip\n", content_type="text/csv"
        )
        self.run_csv_import(self._bulk_add_url(), csv_file)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before)
        self.online_tos.refresh_from_db()
        self.assertEqual(self.online_tos.memo, "vip")

    def test_email_is_normalized_on_save(self):
        """AuctionTOS.save normalizes a real email and leaves None as None."""
        tos = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, email="  Mixed@Case.COM "
        )
        self.assertEqual(tos.email, "mixed@case.com")
        no_email = AuctionTOS.objects.create(auction=self.online_auction, pickup_location=self.location)
        self.assertFalse(no_email.email)

    def test_ragged_row_does_not_500(self):
        """A row with more columns than the header imports rather than 500ing."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        # DictReader puts the surplus under a None key.
        csv_file = SimpleUploadedFile(
            "ragged.csv", b"name,email\nBob,bob@example.com,extra,more\n", content_type="text/csv"
        )
        response = self.run_csv_import(self._bulk_add_url(), csv_file)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuctionTOS.objects.filter(auction=self.online_auction, email="bob@example.com").exists())

    def test_duplicate_email_rows_in_file_collapse_to_one_record(self):
        """Rows sharing a normalized email collapse into one record, filling each other's blank fields."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        csv_file = SimpleUploadedFile(
            "dupes.csv",
            b"name,email,phone\nDana,Dupe@Example.com,\nDana, dupe@example.com ,555-1212\n",
            content_type="text/csv",
        )
        self.run_csv_import(self._bulk_add_url(), csv_file)
        matches = AuctionTOS.objects.filter(auction=self.online_auction, email="dupe@example.com")
        self.assertEqual(matches.count(), 1)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before + 1)
        self.assertEqual(matches.first().phone_number, "555-1212")

    def test_duplicate_email_rows_surfaced_in_preview_as_skipped(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        csv_file = SimpleUploadedFile(
            "dupes.csv",
            b"name,email\nEli,combine@example.com\nEli,combine@example.com\n",
            content_type="text/csv",
        )
        upload = self.client.post(self._bulk_add_url(), {"csv_file": csv_file})
        preview = self.client.get(upload["Location"])
        self.assertContains(preview, "Duplicate email in file")

    def test_double_confirm_same_token_does_not_double_import(self):
        """Re-POSTing the same confirm token imports once."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        csv_file = SimpleUploadedFile("once.csv", b"name,email\nOnce Only,once@example.com\n", content_type="text/csv")
        upload = self.client.post(self._bulk_add_url(), {"csv_file": csv_file})
        token = upload["Location"].split("preview=")[1].split("&")[0]
        self.client.post(self._bulk_add_url(), {"_confirm": token}, follow=True)
        self.client.post(self._bulk_add_url(), {"_confirm": token}, follow=True)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before + 1)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction, email="once@example.com").count(), 1)

    def test_cancel_frees_token_and_writes_nothing(self):
        """Cancel clears the token and writes nothing."""

        self.client.login(username=self.admin_user.username, password="testpassword")
        before = AuctionTOS.objects.filter(auction=self.online_auction).count()
        csv_file = SimpleUploadedFile("cancel.csv", b"name,email\nNope,nope@example.com\n", content_type="text/csv")
        upload = self.client.post(self._bulk_add_url(), {"csv_file": csv_file})
        token = upload["Location"].split("preview=")[1].split("&")[0]
        self.client.post(self._bulk_add_url(), {"_cancel": token})
        self.assertIsNone(cache.get(f"csv_import:{token}"))
        # Confirming the cancelled token is a no-op.
        self.client.post(self._bulk_add_url(), {"_confirm": token}, follow=True)
        self.assertEqual(AuctionTOS.objects.filter(auction=self.online_auction).count(), before)


class GoogleDriveImportTests(StandardTestCase):
    """Test Google Drive import functionality"""

    def test_save_google_drive_link(self):
        """Test that we can save a Google Drive link"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for Google Drive link",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        test_link = "https://docs.google.com/spreadsheets/d/test123/edit#gid=0"
        auction.google_drive_link = test_link
        auction.save()

        # Refresh from database
        auction.refresh_from_db()
        self.assertEqual(auction.google_drive_link, test_link)

    def test_google_drive_import_view_requires_login(self):
        """Test that the Google Drive import view requires login"""
        response = self.client.get(reverse("import_from_google_drive", kwargs={"slug": self.online_auction.slug}))
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_google_drive_import_view_accessible_by_admin(self):
        """Test that admin can access the Google Drive import view"""
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("import_from_google_drive", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auctions/import_from_google_drive.html")

    def test_sync_button_visible_when_link_set(self):
        """Test that sync button appears on users page when google_drive_link is set"""
        self.online_auction.google_drive_link = "https://docs.google.com/spreadsheets/d/test123/edit#gid=0"
        self.online_auction.save()

        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync from Google Drive")

    def test_sync_button_not_visible_when_no_link(self):
        """Test that sync button does not appear when no google_drive_link is set"""
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sync from Google Drive")


class WeeklyPromoEmailTrackingTestCase(StandardTestCase):
    """Test that the weekly_promo_emails_sent field is incremented correctly"""

    def test_auction_has_weekly_promo_emails_sent_field(self):
        """Test that the new field exists and defaults to 0"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for weekly promo",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        assert auction.weekly_promo_emails_sent == 0

    def test_weekly_promo_emails_sent_increments(self):
        """Test that we can increment the weekly_promo_emails_sent field"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for weekly promo increment",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        from django.db.models import F

        # Simulate what the management command does
        Auction.objects.filter(pk=auction.pk).update(weekly_promo_emails_sent=F("weekly_promo_emails_sent") + 1)

        # Refresh from database
        auction.refresh_from_db()
        assert auction.weekly_promo_emails_sent == 1

        # Increment again
        Auction.objects.filter(pk=auction.pk).update(weekly_promo_emails_sent=F("weekly_promo_emails_sent") + 1)
        auction.refresh_from_db()
        assert auction.weekly_promo_emails_sent == 2

    def test_weekly_promo_email_click_rate(self):
        """Test that the click rate calculation handles div/0 correctly"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for click rate",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )

        # Test div/0 case - should return 0 when no emails sent
        assert auction.weekly_promo_emails_sent == 0
        assert auction.weekly_promo_email_click_rate == 0

        # Set some emails sent
        Auction.objects.filter(pk=auction.pk).update(weekly_promo_emails_sent=100)
        auction.refresh_from_db()

        # With 0 clicks and 100 emails, rate should be 0%
        assert auction.weekly_promo_email_click_rate == 0.0
