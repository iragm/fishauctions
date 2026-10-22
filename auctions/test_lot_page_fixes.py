"""The lot page and label fixes: the SOLD stamp, online labels only once sold, pasted images, the upload
size page, what a copied lot starts with, and where watched lots are.
"""

import datetime
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.forms import CreateLotForm
from auctions.models import Auction, AuctionTOS, Lot, LotImage, PickupLocation
from auctions.services import clone_lot_values

from .tests import StandardTestCase, WritableMediaRoot, give_contact_info


def image_bytes():
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (10, 10), "blue").save(buffer, format="JPEG")
    return buffer.getvalue()


class SoldStampTests(StandardTestCase):
    def test_lot_page_stamps_a_sold_lot(self):
        LotImage.objects.create(lot_number=self.lot, url="https://example.com/fish.jpg", is_primary=True)
        response = self.client.get(self.lot.lot_link)
        self.assertContains(response, 'class="sold-stamp"')

    def test_lot_page_leaves_an_unsold_lot_alone(self):
        LotImage.objects.create(lot_number=self.unsoldLot, url="https://example.com/fish.jpg", is_primary=True)
        response = self.client.get(self.unsoldLot.lot_link)
        self.assertNotContains(response, 'class="sold-stamp"')


class OnlineLabelsOnlyOnceSoldTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_property(self):
        self.assertTrue(self.lot.label_can_be_printed)
        self.assertFalse(self.unsoldLot.label_can_be_printed)
        self.assertTrue(self.in_person_lot.label_can_be_printed)

    def test_unsold_online_lot_label_is_refused(self):
        response = self.client.get(reverse("single_lot_label", kwargs={"pk": self.unsoldLot.pk}))
        self.assertRedirects(response, self.unsoldLot.lot_link, fetch_redirect_response=False)

    def test_unsold_online_lot_label_is_refused_for_an_admin_too(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("single_lot_label", kwargs={"pk": self.unsoldLot.pk}))
        self.assertEqual(response.status_code, 302)

    def test_sold_online_lot_label_prints(self):
        response = self.client.get(reverse("single_lot_label", kwargs={"pk": self.lot.pk}), {"pdf": 1})
        self.assertEqual(response.status_code, 200)

    def test_lot_page_offers_no_label_until_sold(self):
        self.unsoldLot.user = self.user
        self.unsoldLot.save()
        label_url = reverse("single_lot_label", kwargs={"pk": self.unsoldLot.pk})
        self.assertNotContains(self.client.get(self.unsoldLot.lot_link), label_url)
        self.lot.user = self.user
        self.lot.save()
        self.assertContains(self.client.get(self.lot.lot_link), reverse("single_lot_label", kwargs={"pk": self.lot.pk}))

    def test_mobile_label_is_refused(self):
        from .test_mobile_features import _bearer

        response = self.client.get(
            reverse("mobile-label-lot", kwargs={"pk": self.unsoldLot.pk}), {"fmt": "png"}, **_bearer(self.user)
        )
        self.assertEqual(response.status_code, 400)
        sold = self.client.get(
            reverse("mobile-label-lot", kwargs={"pk": self.lot.pk}), {"fmt": "png"}, **_bearer(self.user)
        )
        self.assertEqual(sold.status_code, 200)


class PastedImageTests(WritableMediaRoot, StandardTestCase):
    def setUp(self):
        super().setUp()
        give_contact_info(self.user)
        self.client.force_login(self.user)
        self.open_auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Open for lots",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=5),
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=2),
        )
        location = PickupLocation.objects.create(
            name="here", auction=self.open_auction, pickup_time=timezone.now() + datetime.timedelta(days=6)
        )
        AuctionTOS.objects.create(user=self.user, auction=self.open_auction, pickup_location=location)

    def test_pasted_image_on_the_lot_page_is_added(self):
        lot = Lot.objects.create(
            lot_name="unsold", auction=self.open_auction, auctiontos_seller=self.online_tos, quantity=1, user=self.user
        )
        response = self.client.post(
            reverse("add_image", kwargs={"lot": lot.pk}),
            {"image": SimpleUploadedFile("p.jpg", image_bytes(), content_type="image/jpeg"), "image_source": "RANDOM"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.json(), {"ok": True})
        self.assertTrue(LotImage.objects.get(lot_number=lot).is_primary)

    def test_a_bad_paste_is_an_error_not_a_page(self):
        lot = Lot.objects.create(
            lot_name="unsold", auction=self.open_auction, auctiontos_seller=self.online_tos, quantity=1, user=self.user
        )
        response = self.client.post(
            reverse("add_image", kwargs={"lot": lot.pk}),
            {"image": SimpleUploadedFile("p.jpg", b"not an image", content_type="image/jpeg")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_pasting_on_someone_elses_lot_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(
            reverse("add_image", kwargs={"lot": self.unsoldLot.pk}),
            {"image": SimpleUploadedFile("p.jpg", image_bytes(), content_type="image/jpeg")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LotImage.objects.filter(lot_number=self.unsoldLot).exists())

    def test_pasted_image_on_the_new_lot_form_is_added(self):
        response = self.client.post(
            f"{reverse('new_lot')}?auction={self.open_auction.slug}",
            {
                "lot_name": "Pasted lot",
                "auction": self.open_auction.pk,
                "quantity": 1,
                "reserve_price": "5",
                "part_of_auction": "True",
                "run_duration": "10",
                "pasted_image": SimpleUploadedFile("p.jpg", image_bytes(), content_type="image/jpeg"),
            },
        )
        self.assertEqual(response.status_code, 302)
        lot = Lot.objects.get(lot_name="Pasted lot")
        self.assertEqual(LotImage.objects.filter(lot_number=lot, is_primary=True).count(), 1)

    def test_the_lot_page_listens_for_pastes_only_for_who_can_add_images(self):
        lot = Lot.objects.create(
            lot_name="unsold", auction=self.open_auction, auctiontos_seller=self.online_tos, quantity=1, user=self.user
        )
        self.assertContains(self.client.get(lot.lot_link), "lotImages.fromPaste")
        self.client.force_login(self.user_with_no_lots)
        self.assertNotContains(self.client.get(lot.lot_link), "lotImages.fromPaste")


class UploadTooLargeTests(TestCase):
    def test_page_keeps_the_413(self):
        response = self.client.get(reverse("upload_too_large"))
        self.assertEqual(response.status_code, 413)
        self.assertContains(response, "too large", status_code=413)


class CopiedLotTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.online_auction.minimum_bid = 2
        self.online_auction.custom_field_1 = "allow"
        self.online_auction.custom_field_1_name = "Tank size"
        self.online_auction.save()
        self.new_auction = Auction.objects.create(
            created_by=self.user,
            title="Next one",
            is_online=True,
            minimum_bid=5,
            custom_field_1="allow",
            custom_field_1_name="tank size ",
            custom_checkbox_name="Something else",
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=5),
        )
        self.old = Lot.objects.get(pk=self.unsoldLot.pk)
        self.old.reserve_price = 2
        self.old.custom_field_1 = "20 gallon"
        self.old.custom_checkbox = True
        self.old.save()

    def test_the_old_auctions_minimum_becomes_the_new_one(self):
        self.assertEqual(clone_lot_values(self.old, self.new_auction)["reserve_price"], 5)

    def test_a_chosen_minimum_is_kept(self):
        self.old.reserve_price = 12
        self.old.save()
        self.assertEqual(clone_lot_values(self.old, self.new_auction)["reserve_price"], 12)

    def test_custom_fields_follow_their_names(self):
        values = clone_lot_values(self.old, self.new_auction)
        self.assertEqual(values["custom_field_1"], "20 gallon")
        self.assertFalse(values["custom_checkbox"])

    def test_the_copy_form_starts_from_the_auction_it_goes_into(self):
        AuctionTOS.objects.create(user=self.user, auction=self.new_auction, pickup_location=self.location)
        self.old.user = self.user
        self.old.save()
        form = CreateLotForm(user=self.user, cloned_from=self.old.pk, auction=self.new_auction)
        self.assertEqual(form.fields["reserve_price"].initial, 5)
        self.assertFalse(form.fields["custom_checkbox"].initial)
        self.assertTrue(form.clone_hints["minimum_follows_auction"])


class WatchNoteTests(StandardTestCase):
    def test_lot_page_points_at_the_buying_page(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(self.unsoldLot.lot_link)
        self.assertContains(response, "You can view your watched lots from the")
        self.assertContains(response, reverse("buying"))
