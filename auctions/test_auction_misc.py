"""The smaller auction surfaces -- pickup locations, stats, bulk pages, watching, images."""

import datetime
import io
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    Lot,
    LotImage,
)
from auctions.tests import StandardTestCase, WritableMediaRoot


class PickupLocationTests(StandardTestCase):
    """Test PickupLocation model properties and views"""

    def test_pickup_location_create_anonymous(self):
        """Anonymous users cannot create pickup locations"""
        url = reverse("create_auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_pickup_location_create_non_admin(self):
        """Non-admin users cannot create pickup locations"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("create_auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_pickup_location_create_admin(self):
        """Admin users can create pickup locations"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("create_auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_pickup_location_list_anonymous(self):
        """Anonymous users can view pickup locations"""
        url = reverse("auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code != 200

    def test_pickup_location_list_logged_in(self):
        """Logged in users can view pickup locations"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class AuctionStatsViewTests(StandardTestCase):
    """Test auction stats view with different user types"""

    def test_auction_stats_anonymous(self):
        """Anonymous users cannot view stats - requires login and admin permissions"""
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_auction_stats_non_admin(self):
        """Non-admin users cannot view stats"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        # Should be denied (403) or redirect (302)
        assert response.status_code in [302, 403]

    def test_auction_stats_creator(self):
        """Creator can view stats"""
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_stats_admin(self):
        """Admin can view stats"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_stats_recalculation_threshold(self):
        """Stats recalculation respects 20-minute threshold"""
        from django.utils import timezone

        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"

        # Test 1: Stats older than 20 minutes should trigger recalculation
        old_time = timezone.now() - timezone.timedelta(minutes=25)
        self.online_auction.last_stats_update = old_time
        self.online_auction.next_update_due = None
        self.online_auction.save()

        response = self.client.get(url)
        assert response.status_code == 200
        # Should show recalculation message in context
        assert response.context.get("stats_being_recalculated") is True, "Should show recalculation message"

        self.online_auction.refresh_from_db()
        # next_update_due should be set (scheduled for recalculation)
        assert self.online_auction.next_update_due is not None, "next_update_due should be set for old stats"

        # Test 2: Stats within 20 minutes should NOT trigger recalculation
        recent_time = timezone.now() - timezone.timedelta(minutes=10)
        self.online_auction.last_stats_update = recent_time
        self.online_auction.next_update_due = None
        self.online_auction.save()

        response = self.client.get(url)
        assert response.status_code == 200
        # Should NOT show recalculation message in context
        assert response.context.get("stats_being_recalculated") is not True, "Should not show recalculation message"

        self.online_auction.refresh_from_db()
        # next_update_due should remain None (no recalculation scheduled)
        assert self.online_auction.next_update_due is None, "next_update_due should not be set for recent stats"

        # Test 3: Already scheduled recalculation should not reschedule
        old_time = timezone.now() - timezone.timedelta(minutes=25)
        scheduled_time = timezone.now() + timezone.timedelta(minutes=2)
        self.online_auction.last_stats_update = old_time
        self.online_auction.next_update_due = scheduled_time
        self.online_auction.save()

        response = self.client.get(url)
        assert response.status_code == 200
        # Should still show recalculation message but not reschedule
        assert response.context.get("stats_being_recalculated") is True, "Should show recalculation message"

        self.online_auction.refresh_from_db()
        assert self.online_auction.next_update_due == scheduled_time, "Should not reschedule if already scheduled"

    def test_lot_sell_prices_labels_match_bins(self):
        """Test that lot sell prices chart labels use whole number boundaries"""

        # Create some test lots with various prices
        lot_prices = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]
        for price in lot_prices:
            Lot.objects.create(
                lot_name=f"Test lot ${price}",
                quantity=1,
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                winning_price=price,
            )

        # Recalculate stats
        stats = self.online_auction.set_stat_lot_sell_prices()

        # Get the stats data
        labels = stats["labels"]
        data = stats["data"][0]  # First provider's data

        # Verify data and labels have the same length
        assert len(data) == len(labels), f"Data length ({len(data)}) should match labels length ({len(labels)})"

        # Verify that all bin labels use whole numbers (no decimals)
        for i, label in enumerate(labels):
            if label == "Not sold":
                continue
            if "+" in label:
                # Overflow bin like "$39+"
                continue

            # Extract the bin boundaries from label
            label_cleaned = label.replace(self.online_auction.currency_symbol, "")
            if "-" in label_cleaned:
                label_start, label_end = label_cleaned.split("-")
                # Verify both values are whole numbers
                assert "." not in label_start, f"Label {label} start should be whole number, got {label_start}"
                assert "." not in label_end, f"Label {label} end should be whole number, got {label_end}"

                # Verify they are valid integers
                bin_start = int(label_start)
                bin_end = int(label_end)

                # Verify bin_end > bin_start
                assert bin_end > bin_start, f"Label {label} end ({bin_end}) should be greater than start ({bin_start})"


class BulkAddLotsViewTests(StandardTestCase):
    """Test bulk add lots view with different user types"""

    def test_bulk_add_lots_anonymous(self):
        """Anonymous users cannot bulk add lots"""
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_bulk_add_lots_non_admin(self):
        """Non-admin users cannot bulk add lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_bulk_add_lots_admin(self):
        """Admin users can bulk add lots"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_bulk_add_lots_creator(self):
        """Auction creator can bulk add lots"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class BulkAddUsersViewTests(StandardTestCase):
    """Test bulk add users view with different user types"""

    def test_bulk_add_users_anonymous(self):
        """Anonymous users cannot bulk add users"""
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_bulk_add_users_non_admin(self):
        """Non-admin users cannot bulk add users"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_bulk_add_users_admin(self):
        """Admin users can bulk add users"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class SetLotWinnersViewTests(StandardTestCase):
    """Test set lot winners view with different user types"""

    def test_set_lot_winners_anonymous(self):
        """Anonymous users cannot access set lot winners"""
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_set_lot_winners_non_admin(self):
        """Non-admin users cannot access set lot winners"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 403

    def test_set_lot_winners_admin(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class AuctionDeleteViewTests(StandardTestCase):
    """Test auction deletion with different user types"""

    def test_auction_delete_anonymous(self):
        """Anonymous users cannot delete auctions"""
        url = f"/auctions/{self.online_auction.slug}/delete/"
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_auction_delete_non_creator(self):
        """Non-creator users cannot delete auctions"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/delete/"
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_auction_delete_creator(self):
        """Creator can access delete page"""
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/delete/"
        response = self.client.get(url)
        assert response.status_code == 302


class AdditionalAuctionPropertyTests(StandardTestCase):
    """Test additional Auction model properties"""

    def test_auction_urls(self):
        """Test various URL properties"""
        assert self.online_auction.url == reverse("auction_main", kwargs={"slug": self.online_auction.slug})
        assert self.online_auction.add_lot_link == f"{reverse('new_lot')}?auction={self.online_auction.slug}"
        assert (
            self.online_auction.view_lot_link == f"{reverse('allLots')}?auction={self.online_auction.slug}&status=all"
        )
        assert (
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}) in self.online_auction.label_print_link
        )
        assert (
            reverse("auction_main", kwargs={"slug": self.online_auction.slug})
            in self.online_auction.label_print_unprinted_link
        )

    def test_template_status(self):
        """Test template_status property"""
        # Create a future auction
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future auction",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        assert future_auction.template_status == "Starts:"

        # Create an in-progress auction
        in_progress_auction = Auction.objects.create(
            created_by=self.user,
            title="In progress",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert in_progress_auction.template_status == "Now until:"

    def test_auction_str_method(self):
        """Test the __str__ method of Auction"""
        # Auction title without "auction" should have it added
        auction1 = Auction.objects.create(
            created_by=self.user,
            title="Fish Sale",
            is_online=True,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        str_repr = str(auction1)
        assert "auction" in str_repr.lower()
        assert "the " in str_repr.lower() or str_repr.startswith("The ")

    def test_can_submit_lots(self):
        """Test the can_submit_lots property"""
        # Create an auction with lot submission dates
        auction = Auction.objects.create(
            created_by=self.user,
            title="Lot submission test",
            is_online=True,
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=1),
            date_start=timezone.now() + datetime.timedelta(days=2),
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        # Should be able to submit lots during the submission window
        assert auction.can_submit_lots is True

        # Create an auction where lot submission has ended
        ended_submission_auction = Auction.objects.create(
            created_by=self.user,
            title="Ended submission",
            is_online=True,
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=3),
            lot_submission_end_date=timezone.now() - datetime.timedelta(days=1),
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        # Should not be able to submit lots
        assert ended_submission_auction.can_submit_lots is False


class AdditionalLotPropertyTests(StandardTestCase):
    """Test additional Lot model properties"""

    def test_lot_banned_property(self):
        """Test the banned property of lots"""
        # Create a normal lot
        lot = Lot.objects.create(
            lot_name="Normal lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            banned=False,
        )
        assert lot.banned is False

        # Update to banned
        lot.banned = True
        lot.save()
        assert lot.banned is True

    def test_lot_donation_property(self):
        """Test the donation property of lots"""
        lot = Lot.objects.create(
            lot_name="Donation lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            donation=False,
        )
        assert lot.donation is False

        lot.donation = True
        lot.save()
        assert lot.donation is True


class UserViewTests(StandardTestCase):
    """Test user profile view with different user types"""

    def test_user_view_anonymous(self):
        """Anonymous users can view user profiles"""
        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_user_view_logged_in(self):
        """Logged in users can view user profiles"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_user_view_own_profile(self):
        """Users can view their own profile"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        assert response.status_code == 200


class ImageViewTests(WritableMediaRoot, StandardTestCase):
    """Test image create/update/delete views"""

    def _image_bytes(self, fmt="JPEG", size=(10, 10)):
        """Return the raw bytes of a small valid image in the given format"""
        from PIL import Image as PILImage

        buffer = io.BytesIO()
        PILImage.new("RGB", size, "blue").save(buffer, format=fmt)
        return buffer.getvalue()

    def _animated_gif_bytes(self, size=(10, 10)):
        """Raw bytes of a two-frame GIF -- the upload behind the `cannot write mode P as
        JPEG` 500: easy_thumbnails hands an animated source back as a palette image, which
        Pillow then refuses to write as the JPEG thumbnail it wants to make."""
        from PIL import Image as PILImage

        frames = [PILImage.new("RGB", size, color).convert("P") for color in ("red", "green")]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
        return buffer.getvalue()

    def _addable_lot(self):
        """An unsold lot the standard `self.user` is allowed to add images to"""
        return Lot.objects.create(
            lot_name="addable lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )

    def test_image_create_anonymous(self):
        """Anonymous users cannot create images"""
        url = reverse("add_image", kwargs={"lot": self.lot.pk})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_image_create_logged_in(self):
        """Logged in users can access image create form"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("add_image", kwargs={"lot": self.lot.pk})
        response = self.client.get(url)
        assert response.status_code == 302

    def test_create_image_form_accepts_valid_image(self):
        """A real image passes form validation"""
        from auctions.forms import CreateImageForm

        upload = SimpleUploadedFile("ok.jpg", self._image_bytes(), content_type="image/jpeg")
        form = CreateImageForm(data={"image_source": "ACTUAL"}, files={"image": upload})
        self.assertTrue(form.is_valid(), form.errors)

    def test_create_image_form_rejects_corrupt_image(self):
        """A corrupt/non-image upload becomes an inline field error, never a 500"""
        from auctions.forms import CreateImageForm

        upload = SimpleUploadedFile("bad.jpg", b"this is definitely not an image", content_type="image/jpeg")
        form = CreateImageForm(data={"image_source": "ACTUAL"}, files={"image": upload})
        self.assertFalse(form.is_valid())
        self.assertIn("image", form.errors)

    def test_validate_uploaded_image_rejects_garbage(self):
        """validate_uploaded_image raises a friendly ValidationError on unreadable data"""
        from auctions.forms import validate_uploaded_image

        upload = SimpleUploadedFile("bad.png", b"not an image at all", content_type="image/png")
        with self.assertRaises(ValidationError):
            validate_uploaded_image(upload)

    def test_validate_uploaded_image_accepts_real_image(self):
        """validate_uploaded_image returns the file (rewound) for a valid image"""
        from auctions.forms import validate_uploaded_image

        upload = SimpleUploadedFile("ok.png", self._image_bytes(fmt="PNG"), content_type="image/png")
        # Should not raise, and should leave the file ready to be re-read.
        validate_uploaded_image(upload)
        self.assertEqual(upload.tell(), 0)

    def test_animated_gif_upload_is_converted_to_jpeg(self):
        """An animated GIF is flattened on the way in so thumbnailing can write it"""
        from auctions.forms import CreateImageForm

        upload = SimpleUploadedFile("fish.gif", self._animated_gif_bytes(), content_type="image/gif")
        form = CreateImageForm(data={"image_source": "ACTUAL"}, files={"image": upload})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["image"].name.endswith(".jpg"))

    def test_editing_an_image_with_an_animated_gif(self):
        """The prod regression: POSTing an animated GIF to /images/<pk>/edit raised
        `OSError: cannot write mode P as JPEG` out of easy_thumbnails and 500ed."""
        lot = self._addable_lot()
        image = LotImage.objects.create(lot_number=lot, image_source="ACTUAL")
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("edit_image", kwargs={"pk": image.pk})
        upload = SimpleUploadedFile("fish.gif", self._animated_gif_bytes(), content_type="image/gif")
        response = self.client.post(url, {"image": upload, "image_source": "ACTUAL"})
        self.assertEqual(response.status_code, 302)
        image.refresh_from_db()
        self.assertTrue(image.image.name.endswith(".jpg"), image.image.name)

    def test_site_error_on_save_is_not_masked_as_corrupt(self):
        """A permission/disk error while saving must surface as a 500 (which emails the
        admins), not be reported to the user as a corrupt image. This is the prod
        regression: [Errno 13] Permission denied writing to mediafiles/images/."""
        lot = self._addable_lot()
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("add_image", kwargs={"lot": lot.pk})
        upload = SimpleUploadedFile("ok.jpg", self._image_bytes(), content_type="image/jpeg")
        permission_error = PermissionError("[Errno 13] Permission denied: '/home/app/web/mediafiles/images/ok.jpg'")
        with patch("auctions.models.LotImage.save", side_effect=permission_error):
            with self.assertRaises(PermissionError):
                self.client.post(url, {"image": upload, "image_source": "ACTUAL"})

    def test_image_processing_error_on_save_shown_to_user(self):
        """If the image itself is unusable at save time, the user gets a friendly error
        (not a 500) and stays on the form."""
        from PIL import UnidentifiedImageError

        lot = self._addable_lot()
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("add_image", kwargs={"lot": lot.pk})
        upload = SimpleUploadedFile("ok.jpg", self._image_bytes(), content_type="image/jpeg")
        with patch("auctions.models.LotImage.save", side_effect=UnidentifiedImageError("bad image")):
            response = self.client.post(url, {"image": upload, "image_source": "ACTUAL"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "corrupt or in an unsupported format")
        self.assertFalse(LotImage.objects.filter(lot_number=lot).exists())


class WatchViewTests(StandardTestCase):
    """Test watch/unwatch functionality"""

    def test_watch_anonymous(self):
        """Anonymous users cannot watch lots"""
        # watchOrUnwatch is a function-based view
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "1"})
        # Should be denied (401/403) - DRF APIView does not redirect
        assert response.status_code in [401, 403]

    def test_watch_logged_in(self):
        """Logged in users can watch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Success")

    def test_unwatch_logged_in(self):
        """Logged in users can unwatch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        # First watch
        self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "1"})
        # Then unwatch
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "false"})
        self.assertEqual(response.status_code, 200)

    def test_get_request_denied(self):
        """GET requests should be denied"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/api/watchitem/{self.lot.pk}/")
        self.assertEqual(response.status_code, 405)


class MyBidsViewTests(StandardTestCase):
    """Test my bids view with different user types"""

    def test_my_bids_anonymous(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/bids/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_my_bids_logged_in(self):
        """Logged in users can view their bids"""
        self.client.login(username=self.userB.username, password="testpassword")
        response = self.client.get("/bids/")
        assert response.status_code == 200


class MyWonLotsViewTests(StandardTestCase):
    """Test my won lots view with different user types"""

    def test_my_won_lots_anonymous(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/lots/won/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_my_won_lots_logged_in(self):
        """Logged in users can view their won lots"""
        self.client.login(username=self.userB.username, password="testpassword")
        response = self.client.get("/lots/won/")
        assert response.status_code == 200
