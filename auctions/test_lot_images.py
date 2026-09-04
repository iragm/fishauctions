"""Lot images: uploading, ordering, rotating and deleting them; plus signup forms."""

import datetime
import json

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    ChangeUsernameForm,
    CreateLotForm,
    CustomResetPasswordForm,
    CustomSignupForm,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    Lot,
    LotImage,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase


class LotImageManagementTests(StandardTestCase):
    """Tests for the image management features added in the image management update"""

    def setUp(self):
        super().setUp()
        # Create a lot that can have images added (not sold)
        self.image_lot = Lot.objects.create(
            lot_name="Image lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        # Create a LotImage with only a URL (no uploaded file)
        self.url_image = LotImage.objects.create(
            lot_number=self.image_lot,
            url="https://example.com/fish.jpg",
            image_source="RANDOM",
            is_primary=True,
        )

    def test_lotimage_display_url_with_url_field(self):
        """display_url should return the url field when no image file is uploaded"""
        self.assertEqual(self.url_image.display_url, "https://example.com/fish.jpg")

    def test_lotimage_display_url_empty(self):
        """display_url should return None when neither image nor url is set"""
        empty_image = LotImage.objects.create(
            lot_number=self.image_lot,
            image_source="RANDOM",
        )
        self.assertIsNone(empty_image.display_url)

    def test_lot_use_images_from_field(self):
        """use_images_from should link one lot to another for image management"""
        source_lot = Lot.objects.create(
            lot_name="Source lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        self.image_lot.use_images_from = source_lot
        self.image_lot.save()
        self.image_lot.refresh_from_db()
        self.assertEqual(self.image_lot.use_images_from, source_lot)

    def test_image_permission_check_blocks_when_dependent_online_lot_sold(self):
        """image_permission_check should return False if a dependent online auction lot is sold"""
        source_lot = Lot.objects.create(
            lot_name="Source lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        # Create a dependent sold lot that uses source_lot's images
        dependent_lot = Lot.objects.create(
            lot_name="Dependent lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            use_images_from=source_lot,
        )
        # can_add_images is False for sold lots (winning_price is set)
        self.assertFalse(dependent_lot.can_add_images)
        # source_lot should now fail image_permission_check because dependent is sold online auction lot
        self.assertFalse(source_lot.image_permission_check(self.user))

    def test_image_permission_check_allows_when_no_dependent_lots(self):
        """image_permission_check should work normally when no dependent lots exist"""
        source_lot = Lot.objects.create(
            lot_name="Source lot no dep",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        # Normal permission check should pass for lot owner
        self.assertTrue(source_lot.image_permission_check(self.user))

    def test_lot_image_url_field_cleared_after_processing(self):
        """image_url field on a Lot should be cleared after an image is created from it"""
        # Directly test the model field behavior
        test_lot = Lot.objects.create(
            lot_name="URL image lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            image_url="https://example.com/new_fish.jpg",
        )
        # Simulate the processing: create a LotImage from image_url and clear the field
        if test_lot.image_url:
            LotImage.objects.create(
                lot_number=test_lot,
                url=test_lot.image_url,
                is_primary=not test_lot.image_count,
                image_source="RANDOM",
            )
            test_lot.image_url = None
            test_lot.save(update_fields=["image_url"])
        test_lot.refresh_from_db()
        self.assertIsNone(test_lot.image_url)
        self.assertEqual(test_lot.image_count, 1)
        self.assertEqual(test_lot.images[0].url, "https://example.com/new_fish.jpg")

    def test_create_image_form_url_validation_rejects_non_image(self):
        """CreateImageForm should reject URLs that don't have image extensions"""
        from auctions.forms import CreateImageForm

        form = CreateImageForm(data={"url": "https://example.com/page.html", "image_source": "RANDOM"})
        self.assertFalse(form.is_valid())
        self.assertIn("url", form.errors)

    def test_create_image_form_url_validation_accepts_image_url(self):
        """CreateImageForm should accept URLs with image extensions"""
        from auctions.forms import CreateImageForm

        form = CreateImageForm(data={"url": "https://example.com/photo.jpg", "image_source": "RANDOM"})
        self.assertTrue(form.is_valid())

    def test_create_image_form_url_rejects_non_http_scheme(self):
        """CreateImageForm should reject URLs with non-http/https schemes"""
        from auctions.forms import CreateImageForm

        form = CreateImageForm(data={"url": "ftp://example.com/photo.jpg", "image_source": "RANDOM"})
        self.assertFalse(form.is_valid())
        self.assertIn("url", form.errors)

    def test_lot_image_url_field_invalid_extension_shows_error(self):
        """Submitting a lot form with an image_url that lacks an image extension should show an error and not create a LotImage"""
        self.client.login(username="my_lot", password="testpassword")
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        self.user.userdata.address = "123 Test St"
        self.user.userdata.can_submit_standalone_lots = True
        self.user.userdata.save()
        test_lot = Lot.objects.create(
            lot_name="Bad-extension URL test lot",
            user=self.user,
            quantity=1,
            local_pickup=True,
            payment_cash=True,
        )
        initial_image_count = LotImage.objects.filter(lot_number=test_lot).count()
        form_data = {
            "lot_name": test_lot.lot_name,
            "quantity": 1,
            "reserve_price": 2,
            "image_url": "https://example.com/not-an-image.html",
            "cloned_from": "",
            "run_duration": 10,
            "part_of_auction": "False",
            "local_pickup": "on",
            "payment_cash": "on",
        }
        response = self.client.post(f"/lots/edit/{test_lot.pk}/", data=form_data, follow=True)
        # No LotImage should be created for the invalid URL
        self.assertEqual(LotImage.objects.filter(lot_number=test_lot).count(), initial_image_count)
        # An error message should be shown
        messages_list = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("not valid" in m for m in messages_list))

    def test_lot_image_url_field_invalid_scheme_shows_error(self):
        """Submitting a lot form with an image_url that uses a non-http/https scheme should show an error and not create a LotImage"""
        self.client.login(username="my_lot", password="testpassword")
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        self.user.userdata.address = "123 Test St"
        self.user.userdata.can_submit_standalone_lots = True
        self.user.userdata.save()
        test_lot = Lot.objects.create(
            lot_name="Bad-scheme URL test lot",
            user=self.user,
            quantity=1,
            local_pickup=True,
            payment_cash=True,
        )
        initial_image_count = LotImage.objects.filter(lot_number=test_lot).count()
        form_data = {
            "lot_name": test_lot.lot_name,
            "quantity": 1,
            "reserve_price": 2,
            "image_url": "ftp://example.com/photo.jpg",
            "cloned_from": "",
            "run_duration": 10,
            "part_of_auction": "False",
            "local_pickup": "on",
            "payment_cash": "on",
        }
        response = self.client.post(f"/lots/edit/{test_lot.pk}/", data=form_data, follow=True)
        self.assertEqual(LotImage.objects.filter(lot_number=test_lot).count(), initial_image_count)
        messages_list = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("not valid" in m for m in messages_list))

    def test_lot_image_url_field_accepts_valid_url(self):
        """The lot image_url hidden field should accept valid http image URLs without showing errors"""

        # Ensure user can submit standalone lots
        self.user.userdata.can_submit_standalone_lots = True
        self.user.userdata.save()
        form = CreateLotForm(
            data={
                "lot_name": "Test",
                "quantity": 1,
                "reserve_price": 2,
                "image_url": "https://example.com/photo.jpg",
                "cloned_from": "",
                "run_duration": 10,
                "part_of_auction": "False",
                "local_pickup": "on",
                "payment_cash": "on",
            },
            user=self.user,
            cloned_from=None,
            auction=None,
        )
        self.assertNotIn("image_url", form.errors)

    def test_images_managed_from_only_shown_to_lot_creator(self):
        """images_managed_from_lot context should only be set for the lot creator, not auction admins"""
        source_lot = Lot.objects.create(
            lot_name="Source lot for creator check",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            use_images_from=self.image_lot,
        )
        # lot creator (self.user) should see the note
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(source_lot.lot_link)
        self.assertIn("images_managed_from_lot", response.context)

        # auction admin (self.admin_user) should NOT see the note
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(source_lot.lot_link)
        self.assertNotIn("images_managed_from_lot", response.context)
        # when use_images_from is set, nobody (not even admin) should be able to add images to this lot
        # — images are managed from the source lot instead
        self.assertFalse(source_lot.image_permission_check(self.admin_user))

    def test_images_and_thumbnail_delegate_via_use_images_from(self):
        """images and thumbnail should return images from the source lot when use_images_from is set"""
        delegating_lot = Lot.objects.create(
            lot_name="Delegating lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            use_images_from=self.image_lot,
        )
        # delegating_lot has no direct images, but should show image_lot's images
        self.assertEqual(list(delegating_lot.images), [self.url_image])
        self.assertEqual(delegating_lot.thumbnail, self.url_image)

    def test_lot_detail_renders_auto_image_from_url(self):
        """Lot detail should render URL-only auto images without trying to access an uploaded file"""
        self.user.userdata.auto_add_images = True
        self.user.userdata.save(update_fields=["auto_add_images"])
        self.online_auction.auto_add_images = True
        self.online_auction.save(update_fields=["auto_add_images"])

        source_lot = Lot.objects.create(
            lot_name="Auto image lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        LotImage.objects.create(
            lot_number=source_lot,
            url="https://example.com/auto-image.jpg",
            image_source="RANDOM",
            is_primary=True,
        )
        target_lot = Lot.objects.create(
            lot_name="Auto image lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            user=self.user,
            quantity=1,
        )

        response = self.client.get(target_lot.lot_link)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://example.com/auto-image.jpg")

    def test_htmx_lot_renders_auto_image_from_url(self):
        """HTMX lot view should render URL-only auto images without trying to access an uploaded file"""
        self.client.force_login(self.admin_user)
        self.user.userdata.auto_add_images = True
        self.user.userdata.save(update_fields=["auto_add_images"])
        self.online_auction.auto_add_images = True
        self.online_auction.save(update_fields=["auto_add_images"])

        source_lot = Lot.objects.create(
            lot_name="Auto image lot simple",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        LotImage.objects.create(
            lot_number=source_lot,
            url="https://example.com/auto-image-simple.jpg",
            image_source="RANDOM",
            is_primary=True,
        )
        target_lot = Lot.objects.create(
            lot_name="Auto image lot simple",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            user=self.user,
            quantity=1,
        )

        response = self.client.get(
            reverse(
                "htmx_lot",
                kwargs={"slug": self.online_auction.slug, "custom_lot_number": target_lot.lot_number_display},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://example.com/auto-image-simple.jpg")

    def test_image_url_form_integration(self):
        """Submitting a lot edit form with image_url set should create a LotImage"""
        self.client.login(username="my_lot", password="testpassword")
        # Ensure the user has the required contact info for LotValidation
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        self.user.userdata.address = "123 Test St"
        self.user.userdata.can_submit_standalone_lots = True
        self.user.userdata.save()
        # Use a standalone lot owned by self.user
        test_lot = Lot.objects.create(
            lot_name="Edit URL test lot",
            user=self.user,
            quantity=1,
            local_pickup=True,
            payment_cash=True,
        )
        initial_image_count = LotImage.objects.filter(lot_number=test_lot).count()
        form_data = {
            "lot_name": test_lot.lot_name,
            "quantity": 1,
            "reserve_price": 2,
            "image_url": "https://example.com/new_image.jpg",
            "cloned_from": "",
            "run_duration": 10,
            "part_of_auction": "False",
            "local_pickup": "on",
            "payment_cash": "on",
        }
        self.client.post(f"/lots/edit/{test_lot.pk}/", data=form_data)
        # After form submission the image should be created and image_url cleared
        test_lot.refresh_from_db()
        new_images = LotImage.objects.filter(lot_number=test_lot)
        self.assertEqual(new_images.count(), initial_image_count + 1)
        self.assertEqual(new_images.latest("createdon").url, "https://example.com/new_image.jpg")
        self.assertIsNone(test_lot.image_url)

    def test_lot_clone_copies_images(self):
        """Cloning a lot should deep-copy URL images to the new lot; original keeps its own images"""
        original_lot = Lot.objects.create(
            lot_name="Original lot to clone",
            user=self.user,
            quantity=1,
        )
        original_image = LotImage.objects.create(
            lot_number=original_lot,
            url="https://example.com/original.jpg",
            image_source="ACTUAL",
            is_primary=True,
        )
        # Simulate the lot-copy logic (same path as LotValidation.form_valid)
        new_lot = Lot.objects.create(lot_name="Cloned lot", user=self.user, quantity=1)
        originalImages = LotImage.objects.filter(lot_number=original_lot.lot_number)
        for img in originalImages:
            new_img = LotImage.objects.create(
                createdon=img.createdon,
                lot_number=new_lot,
                image_source=img.image_source,
                is_primary=img.is_primary,
                url=img.url,
            )
            if img.image:
                from easy_thumbnails.files import get_thumbnailer

                new_img.image = get_thumbnailer(img.image)
            new_img.save()
        # New lot has its own copy of the image
        cloned_images = LotImage.objects.filter(lot_number=new_lot)
        self.assertEqual(cloned_images.count(), 1)
        cloned_img = cloned_images.first()
        self.assertEqual(cloned_img.url, original_image.url)
        self.assertEqual(cloned_img.image_source, "ACTUAL")
        self.assertTrue(cloned_img.is_primary)
        # Original lot still has its images (they were not moved)
        self.assertEqual(LotImage.objects.filter(lot_number=original_lot).count(), 1)

    def test_image_permission_check_blocks_when_dependent_any_auction_lot_sold(self):
        """image_permission_check should block for any auction lot sold, not just online auctions"""
        source_lot = Lot.objects.create(
            lot_name="Source for in-person check",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            quantity=1,
        )
        # dependent sold lot in an in-person auction
        dependent_lot = Lot.objects.create(
            lot_name="Dependent in-person lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            quantity=1,
            winning_price=10,
            use_images_from=source_lot,
        )
        self.assertFalse(dependent_lot.can_add_images)
        # source_lot should be blocked regardless of auction type
        self.assertFalse(source_lot.image_permission_check(self.user))


class ChangeUsernameFormTest(TestCase):
    """Tests for ChangeUsernameForm to ensure @ symbol is disallowed in usernames"""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpassword", email="test@example.com")

    def test_username_with_at_symbol_is_invalid(self):
        form = ChangeUsernameForm(data={"username": "user@name"}, instance=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    def test_username_without_at_symbol_is_valid(self):
        form = ChangeUsernameForm(data={"username": "validusername"}, instance=self.user)
        self.assertTrue(form.is_valid())

    def test_username_with_only_at_symbol_is_invalid(self):
        form = ChangeUsernameForm(data={"username": "@"}, instance=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)


class CustomSignupFormTest(TestCase):
    """Tests that the allauth adapter rejects usernames with @ via ACCOUNT_USERNAME_VALIDATORS"""

    def test_username_with_at_symbol_rejected_by_adapter(self):
        from allauth.account.adapter import get_adapter

        with self.assertRaises(ValidationError):
            get_adapter().clean_username("user@name")

    def test_username_without_at_symbol_accepted_by_adapter(self):
        from allauth.account.adapter import get_adapter

        result = get_adapter().clean_username("validuser", shallow=True)
        self.assertEqual(result, "validuser")

    @override_settings(RECAPTCHA_ENABLED=False)
    def test_signup_form_removes_captcha_when_recaptcha_is_disabled(self):
        form = CustomSignupForm()
        self.assertNotIn("captcha", form.fields)

    @override_settings(RECAPTCHA_ENABLED=False)
    def test_reset_password_form_removes_captcha_when_recaptcha_is_disabled(self):
        form = CustomResetPasswordForm()
        self.assertNotIn("captcha", form.fields)


class AdminUserSignupsJSONTests(TestCase):
    """Tests for the AdminUserSignupsJSON view with extended data series"""

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="superuser_signups", password="testpassword", email="super@example.com"
        )
        self.location_auction = Auction.objects.create(
            created_by=self.superuser,
            title="Test auction",
            is_online=True,
            date_end=timezone.now() - datetime.timedelta(days=1),
            date_start=timezone.now() - datetime.timedelta(days=5),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=0,
        )
        self.second_auction = Auction.objects.create(
            created_by=self.superuser,
            title="Second auction",
            is_online=True,
            date_end=timezone.now() - datetime.timedelta(days=1),
            date_start=timezone.now() - datetime.timedelta(days=5),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=0,
        )
        self.pickup = PickupLocation.objects.create(
            name="pickup",
            auction=self.location_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=1),
        )
        self.second_pickup = PickupLocation.objects.create(
            name="second pickup",
            auction=self.second_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=1),
        )
        # User who has joined an auction (no lots)
        self.user_with_tos = User.objects.create_user(
            username="user_with_tos", password="testpassword", email="u1@example.com"
        )
        AuctionTOS.objects.create(user=self.user_with_tos, auction=self.location_auction, pickup_location=self.pickup)
        # User who has won a lot (winner=self, winning_price set)
        self.user_winner = User.objects.create_user(
            username="user_winner", password="testpassword", email="u2@example.com"
        )
        self.winner_tos = AuctionTOS.objects.create(
            user=self.user_winner, auction=self.location_auction, pickup_location=self.pickup
        )
        self.won_lot = Lot.objects.create(
            lot_name="Won lot",
            auction=self.location_auction,
            auctiontos_seller=self.winner_tos,
            quantity=1,
            winner=self.user_winner,
            winning_price=10,
        )
        # User who has sold a lot (Lot.user=seller, winning_price set)
        self.user_seller = User.objects.create_user(
            username="user_seller", password="testpassword", email="u3@example.com"
        )
        self.sold_lot = Lot.objects.create(
            lot_name="Sold lot",
            auction=self.location_auction,
            user=self.user_seller,
            quantity=1,
            winning_price=5,
        )
        # Stale user: last_activity older than 400 days
        self.stale_user = User.objects.create_user(
            username="stale_user", password="testpassword", email="u4@example.com"
        )
        stale_data = UserData.objects.get(user=self.stale_user)
        stale_data.last_activity = timezone.now() - datetime.timedelta(days=401)
        stale_data.save()
        # Non-stale user (no special state)
        self.fresh_user = User.objects.create_user(
            username="fresh_user", password="testpassword", email="u5@example.com"
        )
        # User with multiple AuctionTOS entries and multiple sold lots (to test distinct counting)
        self.multi_user = User.objects.create_user(
            username="multi_user", password="testpassword", email="u6@example.com"
        )
        self.multi_tos1 = AuctionTOS.objects.create(
            user=self.multi_user, auction=self.location_auction, pickup_location=self.pickup
        )
        self.multi_tos2 = AuctionTOS.objects.create(
            user=self.multi_user, auction=self.second_auction, pickup_location=self.second_pickup
        )
        Lot.objects.create(
            lot_name="Multi lot 1",
            auction=self.location_auction,
            user=self.multi_user,
            quantity=1,
            winning_price=8,
        )
        Lot.objects.create(
            lot_name="Multi lot 2",
            auction=self.location_auction,
            user=self.multi_user,
            quantity=1,
            winning_price=9,
        )
        # Total users in test DB: superuser + user_with_tos + user_winner + user_seller
        #                         + stale_user + fresh_user + multi_user = 7

    def _get_json(self, days=None):
        self.client.force_login(self.superuser)
        url = reverse("admin_user_signups_json")
        params = f"?days={days}" if days else ""
        response = self.client.get(url + params)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_returns_four_datasets(self):
        """Response should contain four datasets"""
        data = self._get_json(days=90)
        self.assertEqual(len(data["datasets"]), 4)

    def test_dataset_labels(self):
        """Datasets should have the correct labels"""
        data = self._get_json(days=90)
        labels = [ds["label"] for ds in data["datasets"]]
        self.assertIn("Total users", labels)
        self.assertIn("Joined an auction", labels)
        self.assertIn("Won or sold a lot", labels)
        self.assertIn("Stale (400+ days inactive)", labels)

    def test_total_users_exact_count(self):
        """The total users series final value must equal the exact number of users"""
        data = self._get_json(days=90)
        total_ds = next(ds for ds in data["datasets"] if ds["label"] == "Total users")
        expected = User.objects.count()
        self.assertEqual(total_ds["data"][-1], expected)

    def test_joined_auction_exact_count(self):
        """Joined an auction series must count distinct users with an AuctionTOS, not join rows"""
        data = self._get_json(days=90)
        tos_ds = next(ds for ds in data["datasets"] if ds["label"] == "Joined an auction")
        # user_with_tos, user_winner, multi_user (2 TOS) = 3 distinct users
        # multi_user has 2 AuctionTOS rows but must be counted once
        expected = User.objects.filter(auctiontos__isnull=False).distinct().count()
        self.assertEqual(tos_ds["data"][-1], expected)

    def test_won_or_sold_exact_count(self):
        """Won or sold series must count distinct users with a won lot or a sold lot (winning_price set)"""
        data = self._get_json(days=90)
        won_sold_ds = next(ds for ds in data["datasets"] if ds["label"] == "Won or sold a lot")
        # user_winner (winner field set), user_seller (lot with winning_price), multi_user (lots with winning_price) = 3
        # multi_user has 2 sold lots but must be counted once
        winners = set(User.objects.filter(winner__isnull=False).values_list("pk", flat=True))
        sellers = set(User.objects.filter(lot__winning_price__isnull=False).values_list("pk", flat=True))
        expected = len(winners | sellers)
        self.assertEqual(won_sold_ds["data"][-1], expected)

    def test_unsold_lot_not_counted_as_sold(self):
        """A user who submitted a lot without a winning_price must not appear in the 'won or sold' series"""
        unsold_user = User.objects.create_user(
            username="unsold_user", password="testpassword", email="unsold@example.com"
        )
        Lot.objects.create(
            lot_name="Unsold lot",
            auction=self.location_auction,
            user=unsold_user,
            quantity=1,
            # no winning_price
        )
        data = self._get_json(days=90)
        won_sold_ds = next(ds for ds in data["datasets"] if ds["label"] == "Won or sold a lot")
        winners = set(User.objects.filter(winner__isnull=False).values_list("pk", flat=True))
        sellers = set(User.objects.filter(lot__winning_price__isnull=False).values_list("pk", flat=True))
        expected = len(winners | sellers)
        self.assertEqual(won_sold_ds["data"][-1], expected)
        self.assertNotIn(unsold_user.pk, winners | sellers)

    def test_stale_users_exact_count(self):
        """Stale users series must equal the exact count of users inactive for 400+ days"""
        data = self._get_json(days=90)
        stale_ds = next(ds for ds in data["datasets"] if ds["label"] == "Stale (400+ days inactive)")
        cutoff = timezone.now() - datetime.timedelta(days=400)
        expected = User.objects.filter(userdata__last_activity__lt=cutoff).count()
        self.assertEqual(stale_ds["data"][-1], expected)

    def test_non_admin_is_redirected(self):
        """Non-superuser should be redirected away from the JSON endpoint"""
        regular_user = User.objects.create_user(
            username="regular_user_signups", password="testpassword", email="reg@example.com"
        )
        self.client.force_login(regular_user)
        response = self.client.get(reverse("admin_user_signups_json"))
        self.assertNotEqual(response.status_code, 200)
