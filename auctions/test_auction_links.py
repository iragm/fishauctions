"""Auction join links, the lot list's behaviour, and the Cloudflare image pipeline."""

import datetime
import io
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Category,
    Club,
    Lot,
    LotImage,
    PickupLocation,
)
from auctions.tests import StandardTestCase, WritableMediaRoot


class AuctionJoinLinksUserTests(StandardTestCase):
    """Regression + guard tests for the AuctionTOS.user=None bug.

    Joining an auction through the UI must link the AuctionTOS to the joining user so downstream
    user-FK lookups keep working: the join-state check on the auction page, /bids/ and /lots/won/
    (both restrict lots to auctions the user has a TOS in, via LotFilter.possibleAuctions).
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.open_auction = Auction.objects.create(
            created_by=self.user,
            title="Open online auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        self.open_location = PickupLocation.objects.create(
            name="open location", auction=self.open_auction, pickup_time=now + datetime.timedelta(days=2)
        )
        self.fresh_user = User.objects.create_user(
            username="fresh_joiner", password="testpassword", email="fresh@example.com"
        )

    def _join(self):
        self.client.force_login(self.fresh_user)
        return self.client.post(
            reverse("auction_main", kwargs={"slug": self.open_auction.slug}),
            {
                "i_agree": "on",
                "pickup_location": str(self.open_location.pk),
                "time_spent_reading_rules": "5",
            },
        )

    def test_join_links_auctiontos_to_user(self):
        """The core fix: a first-time UI join leaves AuctionTOS.user set, not None."""
        self._join()
        tos = AuctionTOS.objects.get(auction=self.open_auction, email="fresh@example.com")
        self.assertEqual(tos.user, self.fresh_user)

    def test_join_marks_location_chosen_so_form_is_not_reshown(self):
        """With the user linked, the auction page recognizes the join and hides the join form."""
        self._join()
        self.client.force_login(self.fresh_user)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.open_auction.slug}))
        self.assertTrue(response.context["hasChosenLocation"])

    def test_won_lot_visible_on_won_lots_page_after_join(self):
        self._join()
        tos = AuctionTOS.objects.get(auction=self.open_auction, user=self.fresh_user)
        seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.open_auction, pickup_location=self.open_location
        )
        won = Lot.objects.create(
            lot_name="Fresh user won this",
            auction=self.open_auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=tos,
            active=False,
        )
        # date_posted is auto_now_add; push it out of the 20-minute new-lot hiding window.
        Lot.objects.filter(pk=won.pk).update(date_posted=timezone.now() - datetime.timedelta(days=1))
        self.client.force_login(self.fresh_user)
        response = self.client.get(reverse("won_lots"))
        self.assertContains(response, "Fresh user won this")

    def test_bid_lot_visible_on_bids_page_after_join(self):
        self._join()
        seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.open_auction, pickup_location=self.open_location
        )
        lot = Lot.objects.create(
            lot_name="Fresh user bid on this",
            auction=self.open_auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            active=True,
        )
        Lot.objects.filter(pk=lot.pk).update(date_posted=timezone.now() - datetime.timedelta(days=1))
        Bid.objects.create(user=self.fresh_user, lot_number=lot, amount=5)
        self.client.force_login(self.fresh_user)
        response = self.client.get(reverse("my_bids"))
        self.assertContains(response, "Fresh user bid on this")

    def test_next_param_is_carried_into_join_form_action(self):
        """Visiting the auction with ?next= renders a join form that POSTs back with ?next=,
        so get_success_url can return the user to where they came from."""
        self.client.force_login(self.fresh_user)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.open_auction.slug}) + "?next=/lots/")
        self.assertEqual(response.context["form"].helper.form_action.split("?next=")[-1], "%2Flots%2F")

    def test_join_redirects_to_next(self):
        self.client.force_login(self.fresh_user)
        response = self.client.post(
            reverse("auction_main", kwargs={"slug": self.open_auction.slug}) + "?next=/lots/",
            {
                "i_agree": "on",
                "pickup_location": str(self.open_location.pk),
                "time_spent_reading_rules": "5",
            },
        )
        self.assertRedirects(response, "/lots/", fetch_redirect_response=False)

    def test_recommended_lots_can_exclude_a_lot(self):
        from auctions.filters import get_recommended_lots

        self._join()
        seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.open_auction, pickup_location=self.open_location
        )
        category = Category.objects.create(name="Recommend test category")
        lot_a = Lot.objects.create(
            lot_name="Recommend A",
            auction=self.open_auction,
            auctiontos_seller=seller_tos,
            species_category=category,
            quantity=1,
            active=True,
        )
        lot_b = Lot.objects.create(
            lot_name="Recommend B",
            auction=self.open_auction,
            auctiontos_seller=seller_tos,
            species_category=category,
            quantity=1,
            active=True,
        )
        Lot.objects.filter(pk__in=[lot_a.pk, lot_b.pk]).update(date_posted=timezone.now() - datetime.timedelta(days=1))
        results = list(get_recommended_lots(user=self.fresh_user, auction=self.open_auction.slug, exclude_pk=lot_a.pk))
        self.assertIn(lot_b, results)
        self.assertNotIn(lot_a, results)


class AuctionTOSEmailChangeGuardTests(StandardTestCase):
    """The email-change guard in AuctionTOS.save() should only unlink the account on a *real*
    email change to an address that isn't the linked user's own."""

    def test_real_email_change_unlinks_user_and_resets_status(self):
        guard_user = User.objects.create_user(username="guard1", password="x", email="guard-a@example.com")
        tos = AuctionTOS.objects.create(
            user=guard_user,
            auction=self.online_auction,
            pickup_location=self.location,
            email="guard-a@example.com",
            email_address_status="VALID",
            manually_added=False,
        )
        tos.email = "guard-b@example.com"
        tos.save()
        tos.refresh_from_db()
        self.assertIsNone(tos.user)
        self.assertEqual(tos.email_address_status, "UNKNOWN")

    def test_filling_blank_email_keeps_user(self):
        guard_user = User.objects.create_user(username="guard2", password="x", email="guard2@example.com")
        # No matching user exists for this address at creation, so user stays as we set it.
        tos = AuctionTOS.objects.create(
            user=guard_user,
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=False,
        )
        self.assertIsNone(tos.email)
        tos.email = "guard2@example.com"
        tos.save()
        tos.refresh_from_db()
        self.assertEqual(tos.user, guard_user)

    def test_change_to_linked_users_own_email_keeps_user(self):
        guard_user = User.objects.create_user(username="guard3", password="x", email="guard3-own@example.com")
        tos = AuctionTOS.objects.create(
            user=guard_user,
            auction=self.online_auction,
            pickup_location=self.location,
            email="guard3-other@example.com",
            manually_added=False,
        )
        tos.email = "guard3-own@example.com"
        tos.save()
        tos.refresh_from_db()
        self.assertEqual(tos.user, guard_user)


class RelinkAuctiontosUsersCommandTests(StandardTestCase):
    """Tests for the relink_auctiontos_users repair command."""

    def _make_orphan(self, email):
        """Create an AuctionTOS with no user (no matching user exists yet, so save() can't auto-link)."""
        tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email=email,
            manually_added=False,
        )
        self.assertIsNone(tos.user)
        return tos

    def test_relinks_orphaned_tos(self):
        orphan = self._make_orphan("orphan@example.com")
        orphan_user = User.objects.create_user(username="orphanu", password="x", email="orphan@example.com")
        call_command("relink_auctiontos_users")
        orphan.refresh_from_db()
        self.assertEqual(orphan.user, orphan_user)

    def test_dry_run_makes_no_changes(self):
        orphan = self._make_orphan("orphan2@example.com")
        User.objects.create_user(username="orphanu2", password="x", email="orphan2@example.com")
        call_command("relink_auctiontos_users", "--dry-run")
        orphan.refresh_from_db()
        self.assertIsNone(orphan.user)

    def test_merges_duplicate_keeping_oldest(self):
        orphan = self._make_orphan("dup@example.com")
        dup_user = User.objects.create_user(username="dupu", password="x", email="dup@example.com")
        # A newer TOS already linked to the user in the same auction.
        own = AuctionTOS.objects.create(auction=self.online_auction, pickup_location=self.location, user=dup_user)
        call_command("relink_auctiontos_users")
        # Oldest record (the orphan) is kept as canonical and gets the user; the newer one is merged away.
        orphan.refresh_from_db()
        self.assertEqual(orphan.user, dup_user)
        self.assertFalse(AuctionTOS.objects.filter(pk=own.pk).exists())


class LotListUXTests(StandardTestCase):
    """Part 3 UX: the persistent 'Outbid' chip on /bids/, the 20-minute new-lot message on the
    auction lot list, and gating the 'Add Lots' button by the submission window."""

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.ux_auction = Auction.objects.create(
            created_by=self.user,
            title="UX online auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
            lot_submission_end_date=now + datetime.timedelta(days=2),
            winning_bid_percent_to_club=25,
        )
        self.ux_location = PickupLocation.objects.create(
            name="ux location", auction=self.ux_auction, pickup_time=now + datetime.timedelta(days=2)
        )
        self.bidder = User.objects.create_user(username="ux_bidder", password="x", email="uxbidder@example.com")
        AuctionTOS.objects.create(user=self.bidder, auction=self.ux_auction, pickup_location=self.ux_location)
        self.other = User.objects.create_user(username="ux_other", password="x", email="uxother@example.com")
        self.seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.ux_auction, pickup_location=self.ux_location
        )

    def _make_lot(self, name, recent=False):
        lot = Lot.objects.create(
            lot_name=name,
            auction=self.ux_auction,
            auctiontos_seller=self.seller_tos,
            user=self.user,
            quantity=1,
            active=True,
        )
        if not recent:
            Lot.objects.filter(pk=lot.pk).update(date_posted=timezone.now() - datetime.timedelta(days=1))
        return lot

    def test_outbid_chip_shown_when_not_high_bidder(self):
        lot = self._make_lot("Lot I got outbid on")
        Bid.objects.create(user=self.bidder, lot_number=lot, amount=50)
        Bid.objects.create(user=self.other, lot_number=lot, amount=100)
        self.client.force_login(self.bidder)
        response = self.client.get(reverse("my_bids"))
        self.assertContains(response, "Lot I got outbid on")
        self.assertContains(response, "Outbid")

    def test_no_outbid_chip_when_high_bidder(self):
        lot = self._make_lot("Lot I am winning")
        Bid.objects.create(user=self.bidder, lot_number=lot, amount=100)
        Bid.objects.create(user=self.other, lot_number=lot, amount=50)
        self.client.force_login(self.bidder)
        response = self.client.get(reverse("my_bids"))
        self.assertContains(response, "Lot I am winning")
        self.assertNotContains(response, "Outbid")

    def test_recently_added_lots_message(self):
        # The only lot was posted moments ago, so it's hidden from non-owners by the 20-minute window.
        self._make_lot("Brand new lot", recent=True)
        self.client.force_login(self.bidder)
        response = self.client.get(self.ux_auction.view_lot_link)
        self.assertContains(response, "Recently added lots will appear here shortly")
        self.assertNotContains(response, "Brand new lot")

    def test_add_lots_button_shown_while_submission_open(self):
        self.client.force_login(self.bidder)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.ux_auction.slug}))
        self.assertContains(response, "bi-calendar-plus")

    def test_add_lots_button_shown_after_submission_closes_and_redirects(self):
        """The Add Lot(s) button stays visible even after lot submission closes. Clicking it
        returns the user to the auction rules page with a 'Lot submission has ended' error,
        rather than the button being hidden."""
        self.ux_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(hours=1)
        self.ux_auction.save()
        self.assertFalse(self.ux_auction.can_submit_lots)
        self.client.force_login(self.bidder)
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.ux_auction.slug}))
        # The button is still rendered
        self.assertContains(response, "bi-calendar-plus")
        # Clicking it redirects back to the auction with an error instead of adding a lot
        response = self.client.get(self.ux_auction.add_lot_link, follow=True)
        self.assertRedirects(response, self.ux_auction.get_absolute_url())
        self.assertContains(response, "Lot submission has ended")


@override_settings(
    CLOUDFLARE_IMAGES_ENABLED=True,
    CLOUDFLARE_IMAGES_ACCOUNT_ID="test-account",
    CLOUDFLARE_IMAGES_API_TOKEN="test-token",
    CLOUDFLARE_IMAGES_ACCOUNT_HASH="test-hash",
    CLOUDFLARE_IMAGES_DOMAIN="",
)
class CloudflareImagesTests(WritableMediaRoot, StandardTestCase):
    """Cloudflare Images serving, fallback, and the migrate_to_cloudflare_images command"""

    def _image_file(self, name="test.jpg"):
        from PIL import Image as PILImage

        buffer = io.BytesIO()
        PILImage.new("RGB", (20, 20), "blue").save(buffer, format="JPEG")
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")

    def _lot(self, name="Cloudflare test lot"):
        return Lot.objects.create(lot_name=name, user=self.user, quantity=1, active=False, lot_run_duration=10)

    def _mock_upload_response(self, image_id="cf-image-id"):
        response = MagicMock()
        response.json.return_value = {"success": True, "result": {"id": image_id}}
        return response

    def test_delivery_url(self):
        from auctions import cloudflare_images

        self.assertEqual(cloudflare_images.delivery_url("abc123"), "https://imagedelivery.net/test-hash/abc123/public")
        self.assertEqual(
            cloudflare_images.delivery_url("abc123", "lot_list"),
            "https://imagedelivery.net/test-hash/abc123/lot_list",
        )
        # unknown variants fall back to full size rather than 404ing
        self.assertEqual(
            cloudflare_images.delivery_url("abc123", "no_such_variant"),
            "https://imagedelivery.net/test-hash/abc123/public",
        )
        with override_settings(CLOUDFLARE_IMAGES_DOMAIN="auction.example.com"):
            self.assertEqual(
                cloudflare_images.delivery_url("abc123", "lot_list"),
                "https://auction.example.com/cdn-cgi/imagedelivery/test-hash/abc123/lot_list",
            )

    def test_lot_image_urls_prefer_cloudflare(self):
        image = LotImage.objects.create(
            lot_number=self._lot(), cloudflare_image_id="abc123", url="http://example.com/pic.jpg"
        )
        self.assertEqual(image.display_url, "https://imagedelivery.net/test-hash/abc123/public")
        self.assertEqual(image.thumbnail_url, "https://imagedelivery.net/test-hash/abc123/lot_list")
        with override_settings(CLOUDFLARE_IMAGES_ENABLED=False):
            # not enabled and no local file: fall back to the url field
            self.assertEqual(image.display_url, "http://example.com/pic.jpg")
            self.assertEqual(image.thumbnail_url, "http://example.com/pic.jpg")

    def test_club_icon_urls_prefer_cloudflare(self):
        club = Club.objects.create(name="Icon club", icon=self._image_file("icon.jpg"), cloudflare_image_id="icon1")
        self.assertEqual(club.icon_display_url, "https://imagedelivery.net/test-hash/icon1/public")
        self.assertEqual(club.icon_thumbnail_url, "https://imagedelivery.net/test-hash/icon1/club_icon")
        with override_settings(CLOUDFLARE_IMAGES_ENABLED=False):
            # falls back to the locally generated easy-thumbnails alias (named by size, not alias)
            self.assertEqual(club.icon_display_url, club.icon.url)
            self.assertIn("128x128", club.icon_thumbnail_url)

    def test_management_command_migrates_all_images_in_one_run(self):
        lot = self._lot()
        first = LotImage.objects.create(lot_number=lot, image=self._image_file("first.jpg"))
        second = LotImage.objects.create(lot_number=lot, image=self._image_file("second.jpg"))
        with patch("auctions.cloudflare_images.requests.post", return_value=self._mock_upload_response()) as mock_post:
            call_command("migrate_to_cloudflare_images")
        # a single run migrates every pending original (only the originals, never the thumbnails)
        self.assertEqual(mock_post.call_count, 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.cloudflare_image_id, "cf-image-id")
        self.assertEqual(second.cloudflare_image_id, "cf-image-id")
        # everything is migrated; another run makes no API calls
        with patch("auctions.cloudflare_images.requests.post", return_value=self._mock_upload_response()) as mock_post:
            call_command("migrate_to_cloudflare_images")
        self.assertEqual(mock_post.call_count, 0)

    def test_management_command_count_limits_images_per_run(self):
        lot = self._lot()
        first = LotImage.objects.create(lot_number=lot, image=self._image_file("first.jpg"))
        second = LotImage.objects.create(lot_number=lot, image=self._image_file("second.jpg"))
        with patch("auctions.cloudflare_images.requests.post", return_value=self._mock_upload_response()) as mock_post:
            call_command("migrate_to_cloudflare_images", "--count", "1")
        self.assertEqual(mock_post.call_count, 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.cloudflare_image_id, "cf-image-id")
        self.assertEqual(second.cloudflare_image_id, "")
        # the next run picks up where the last one left off
        with patch(
            "auctions.cloudflare_images.requests.post", return_value=self._mock_upload_response("cf-image-2")
        ) as mock_post:
            call_command("migrate_to_cloudflare_images", "--count", "1")
        self.assertEqual(mock_post.call_count, 1)
        second.refresh_from_db()
        self.assertEqual(second.cloudflare_image_id, "cf-image-2")

    def test_management_command_skips_when_lock_held(self):

        from auctions.management.commands.migrate_to_cloudflare_images import LOCK_KEY, LOCK_TIMEOUT_SECONDS

        LotImage.objects.create(lot_number=self._lot(), image=self._image_file())
        cache.add(LOCK_KEY, 1, LOCK_TIMEOUT_SECONDS)
        try:
            with patch("auctions.cloudflare_images.requests.post") as mock_post:
                call_command("migrate_to_cloudflare_images")
            self.assertEqual(mock_post.call_count, 0)
        finally:
            cache.delete(LOCK_KEY)

    def test_management_command_skips_deleted_lots(self):
        lot = self._lot()
        lot.is_deleted = True
        lot.save()
        LotImage.objects.create(lot_number=lot, image=self._image_file("deleted.jpg"))
        with patch("auctions.cloudflare_images.requests.post", return_value=self._mock_upload_response()) as mock_post:
            call_command("migrate_to_cloudflare_images")
        self.assertEqual(mock_post.call_count, 0)

    @override_settings(CLOUDFLARE_IMAGES_ENABLED=False)
    def test_management_command_noop_when_disabled(self):
        LotImage.objects.create(lot_number=self._lot(), image=self._image_file())
        with patch("auctions.cloudflare_images.requests.post") as mock_post:
            call_command("migrate_to_cloudflare_images")
        self.assertEqual(mock_post.call_count, 0)

    def test_management_command_aborts_on_api_error(self):
        from django.core.management.base import CommandError

        image = LotImage.objects.create(lot_number=self._lot(), image=self._image_file())
        response = MagicMock()
        response.status_code = 403
        response.json.return_value = {"success": False, "errors": [{"code": 10000, "message": "bad token"}]}
        with (
            patch("auctions.cloudflare_images.requests.post", return_value=response),
            self.assertRaises(CommandError),
        ):
            call_command("migrate_to_cloudflare_images")
        image.refresh_from_db()
        self.assertEqual(image.cloudflare_image_id, "")

    def test_management_command_marks_rejected_files_and_continues(self):
        from auctions.cloudflare_images import UPLOAD_FAILED

        lot = self._lot()
        bad = LotImage.objects.create(lot_number=lot, image=self._image_file("bad.jpg"))
        good = LotImage.objects.create(lot_number=lot, image=self._image_file("good.jpg"))
        rejected = MagicMock()
        rejected.status_code = 415
        rejected.json.return_value = {"success": False, "errors": [{"code": 5455, "message": "unsupported format"}]}
        with patch("auctions.cloudflare_images.requests.post", side_effect=[rejected, self._mock_upload_response()]):
            call_command("migrate_to_cloudflare_images")
        bad.refresh_from_db()
        good.refresh_from_db()
        # the rejected file is marked so it isn't retried every run, and keeps serving locally
        self.assertEqual(bad.cloudflare_image_id, UPLOAD_FAILED)
        self.assertNotIn("imagedelivery", bad.display_url)
        self.assertEqual(good.cloudflare_image_id, "cf-image-id")

    def test_replacing_image_clears_stale_cloudflare_id(self):
        image = LotImage.objects.create(lot_number=self._lot(), image=self._image_file(), cloudflare_image_id="stale")
        image.image = self._image_file("replacement.jpg")
        image.save()
        self.assertEqual(image.cloudflare_image_id, "")

    def test_setting_new_id_with_new_image_is_kept(self):
        # the lot copy flows set a new file and its matching id in the same save
        image = LotImage.objects.create(lot_number=self._lot())
        image.image = self._image_file()
        image.cloudflare_image_id = "copied-id"
        image.save()
        image.refresh_from_db()
        self.assertEqual(image.cloudflare_image_id, "copied-id")

    def test_relist_lot_copies_cloudflare_id(self):
        lot = Lot.objects.create(
            lot_name="Relist cloudflare",
            user=self.user,
            quantity=1,
            winner=self.user_with_no_lots,
            winning_price=10,
            active=False,
            lot_run_duration=10,
        )
        LotImage.objects.create(
            lot_number=lot, image=self._image_file(), cloudflare_image_id="shared-id", is_primary=True
        )
        new_lot = lot.relist_lot()
        new_image = LotImage.objects.filter(lot_number=new_lot).first()
        self.assertEqual(new_image.cloudflare_image_id, "shared-id")

    ROLLBACK = "rolled back on purpose"

    def test_deleting_row_queues_cloudflare_delete(self):
        """Queued on commit, not from inside the delete.

        post_delete fires inside the transaction Django wraps every delete in, so enqueuing there
        directly meant a rollback could leave the row alive pointing at an image that had already
        been deleted from Cloudflare. captureOnCommitCallbacks is what runs the callback in a
        TestCase, where nothing ever really commits.
        """
        image = LotImage.objects.create(lot_number=self._lot(), cloudflare_image_id="gone1")
        with patch("auctions.tasks.delete_cloudflare_image.delay") as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                image.delete()
        mock_delay.assert_called_once_with("gone1")

    def test_a_rolled_back_delete_does_not_delete_the_image(self):
        """The reason for the on_commit. A Cloudflare delete cannot be undone."""
        image = LotImage.objects.create(lot_number=self._lot(), cloudflare_image_id="gone2")
        from django.db import transaction

        with patch("auctions.tasks.delete_cloudflare_image.delay") as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        image.delete()
                        raise ValueError(self.ROLLBACK)
                except ValueError:
                    pass
        mock_delay.assert_not_called()
        self.assertTrue(LotImage.objects.filter(cloudflare_image_id="gone2").exists())

    def test_delete_task_skips_shared_images(self):
        from auctions import tasks

        lot = self._lot()
        LotImage.objects.create(lot_number=lot, cloudflare_image_id="shared-id")
        with patch("auctions.cloudflare_images.delete") as mock_delete:
            tasks.delete_cloudflare_image("shared-id")
            self.assertEqual(mock_delete.call_count, 0)
            tasks.delete_cloudflare_image("unreferenced-id")
            mock_delete.assert_called_once_with("unreferenced-id")

    def test_setup_syncs_variants(self):
        from auctions import cloudflare_images

        response = MagicMock()
        response.json.return_value = {"success": True, "result": {}}
        with patch("auctions.cloudflare_images.requests.post", return_value=response) as mock_post:
            call_command("migrate_to_cloudflare_images", "--setup")
        self.assertEqual(mock_post.call_count, len(cloudflare_images.VARIANTS))
