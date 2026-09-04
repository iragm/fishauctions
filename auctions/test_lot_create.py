"""Creating a lot, and the invoice lists a seller and buyer see afterwards."""

import datetime

from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    Category,
    Invoice,
    Lot,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase


class LotCreateViewTests(StandardTestCase):
    """Test lot creation with different user types"""

    def test_lot_create_anonymous(self):
        """Anonymous users cannot create lots"""
        response = self.client.get("/lots/new/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_lot_create_logged_in_not_joined(self):
        """User not joined to auction should not be able to create lot in that auction"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        # Try to create a lot in the auction they haven't joined
        response = self.client.get(f"/lots/new/?auction={self.online_auction.slug}")
        # They can access the form, but posting should fail or redirect
        assert response.status_code == 302

    def test_lot_create_logged_in_joined(self):
        """User joined to auction can create lots"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={self.online_auction.slug}")
        assert response.status_code == 302

    def test_anonymous_get_params_preserved_in_login_redirect(self):
        """Anonymous users should have GET params preserved in the login redirect"""
        response = self.client.get("/lots/new/?lot_name=TestFish&quantity=3")
        assert response.status_code == 302
        # The next parameter should include the full path with query params
        redirect_url = response["Location"]
        assert "lot_name=TestFish" in redirect_url or "%3Flot_name%3DTestFish" in redirect_url

    def test_contact_info_redirect_preserves_get_params(self):
        """If a user needs to fill out contact info, GET params should be preserved"""
        # user_who_does_not_join has no contact info set
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get("/lots/new/?lot_name=TestFish&quantity=3")
        assert response.status_code == 302
        redirect_url = response["Location"]
        # Should redirect to contact_info and preserve the full path with GET params
        assert "/contact_info" in redirect_url
        # Should preserve the full path including GET params
        assert "lots%2Fnew" in redirect_url or "lots/new" in redirect_url
        assert "lot_name" in redirect_url
        assert "quantity" in redirect_url

    def test_get_params_set_form_initial_values(self):
        """GET params matching form fields should set form initial values on the create form"""
        theFuture = timezone.now() + datetime.timedelta(days=3)
        # Set up user with contact info
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()
        # Create an open auction
        open_auction = Auction.objects.create(
            created_by=self.user,
            title="Open auction",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
        )
        open_location = PickupLocation.objects.create(name="open location", auction=open_auction, pickup_time=theFuture)
        AuctionTOS.objects.create(user=self.user, auction=open_auction, pickup_location=open_location)
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={open_auction.slug}&lot_name=TestFish&quantity=5&donation=true")
        assert response.status_code == 200
        form = response.context["form"]
        assert form.initial.get("lot_name") == "TestFish"
        assert form.initial.get("quantity") == "5"

    def test_lot_create_form_shows_custom_dropdown_when_enabled(self):
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()
        open_auction = Auction.objects.create(
            created_by=self.user,
            title="Open auction with dropdown",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="allow",
            custom_dropdown_name="Habitat",
        )
        open_location = PickupLocation.objects.create(name="open location", auction=open_auction, pickup_time=theFuture)
        AuctionTOS.objects.create(user=self.user, auction=open_auction, pickup_location=open_location)
        AuctionDropdown.objects.create(auction=open_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=open_auction, user=self.user, value="Pond")
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={open_auction.slug}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Habitat")
        self.assertContains(response, 'id="id_custom_dropdown"')

    def test_lot_create_form_shows_custom_dropdown_for_last_auction_used(self):
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        open_auction = Auction.objects.create(
            created_by=self.user,
            title="Last auction with dropdown",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="allow",
            custom_dropdown_name="Habitat",
        )
        open_location = PickupLocation.objects.create(name="open location", auction=open_auction, pickup_time=theFuture)
        AuctionTOS.objects.create(user=self.user, auction=open_auction, pickup_location=open_location)
        AuctionDropdown.objects.create(auction=open_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=open_auction, user=self.user, value="Pond")
        user_data.last_auction_used = open_auction
        user_data.save()
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get("/lots/new/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Habitat")
        self.assertContains(response, 'id="id_custom_dropdown"')

    def test_lot_create_switch_from_required_dropdown_to_disabled_auction_saves(self):
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        required_auction = Auction.objects.create(
            created_by=self.user,
            title="Required dropdown auction",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="required",
            custom_dropdown_name="Habitat",
        )
        required_location = PickupLocation.objects.create(
            name="required location", auction=required_auction, pickup_time=the_future
        )
        AuctionTOS.objects.create(user=self.user, auction=required_auction, pickup_location=required_location)
        AuctionDropdown.objects.create(auction=required_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=required_auction, user=self.user, value="Pond")

        disabled_auction = Auction.objects.create(
            created_by=self.user,
            title="Disabled dropdown auction",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="disable",
        )
        disabled_location = PickupLocation.objects.create(
            name="disabled location", auction=disabled_auction, pickup_time=the_future
        )
        AuctionTOS.objects.create(user=self.user, auction=disabled_auction, pickup_location=disabled_location)
        user_data.last_auction_used = required_auction
        user_data.save()

        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.post(
            "/lots/new/",
            data={
                "part_of_auction": "True",
                "auction": str(disabled_auction.pk),
                "lot_name": "Switch auction lot",
                "species_category": str(Category.objects.filter(name="Uncategorized").first().pk),
                "quantity": "1",
                "reserve_price": "5",
                "custom_dropdown": "River",
            },
        )
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else None)
        lot = Lot.objects.filter(lot_name="Switch auction lot").latest("date_posted")
        self.assertEqual(lot.auction, disabled_auction)
        self.assertEqual(lot.custom_dropdown, "")

    def test_lot_create_copy_prefills_custom_dropdown_when_target_options_match(self):
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        source_auction = Auction.objects.create(
            created_by=self.user,
            title="Source auction",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="allow",
            custom_dropdown_name="Habitat",
        )
        source_location = PickupLocation.objects.create(
            name="source location", auction=source_auction, pickup_time=the_future
        )
        source_tos = AuctionTOS.objects.create(user=self.user, auction=source_auction, pickup_location=source_location)
        AuctionDropdown.objects.create(auction=source_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=source_auction, user=self.user, value="Pond")
        source_lot = Lot.objects.create(
            lot_name="Copied lot",
            auction=source_auction,
            auctiontos_seller=source_tos,
            user=self.user,
            reserve_price=5,
            custom_dropdown="River",
        )

        target_auction = Auction.objects.create(
            created_by=self.user,
            title="Target auction",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="allow",
            custom_dropdown_name="Habitat",
        )
        target_location = PickupLocation.objects.create(
            name="target location", auction=target_auction, pickup_time=the_future
        )
        AuctionTOS.objects.create(user=self.user, auction=target_auction, pickup_location=target_location)
        AuctionDropdown.objects.create(auction=target_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=target_auction, user=self.user, value="Lake")

        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={target_auction.slug}&copy={source_lot.pk}")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["custom_dropdown"].value(), "River")

    def test_lot_create_copy_does_not_prefill_custom_dropdown_when_target_options_do_not_match(self):
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        source_auction = Auction.objects.create(
            created_by=self.user,
            title="Source auction mismatch",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="allow",
            custom_dropdown_name="Habitat",
        )
        source_location = PickupLocation.objects.create(
            name="source mismatch location", auction=source_auction, pickup_time=the_future
        )
        source_tos = AuctionTOS.objects.create(user=self.user, auction=source_auction, pickup_location=source_location)
        AuctionDropdown.objects.create(auction=source_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=source_auction, user=self.user, value="Pond")
        source_lot = Lot.objects.create(
            lot_name="Copied mismatch lot",
            auction=source_auction,
            auctiontos_seller=source_tos,
            user=self.user,
            reserve_price=5,
            custom_dropdown="River",
        )

        target_auction = Auction.objects.create(
            created_by=self.user,
            title="Target auction mismatch",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="allow",
            custom_dropdown_name="Habitat",
        )
        target_location = PickupLocation.objects.create(
            name="target mismatch location", auction=target_auction, pickup_time=the_future
        )
        AuctionTOS.objects.create(user=self.user, auction=target_auction, pickup_location=target_location)
        AuctionDropdown.objects.create(auction=target_auction, user=self.user, value="Lake")
        AuctionDropdown.objects.create(auction=target_auction, user=self.user, value="Ocean")

        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={target_auction.slug}&copy={source_lot.pk}")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn(form["custom_dropdown"].value(), ("", None))

    def test_lot_create_switch_to_required_dropdown_auction_requires_selection(self):
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        disabled_auction = Auction.objects.create(
            created_by=self.user,
            title="Disabled start auction",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="disable",
        )
        disabled_location = PickupLocation.objects.create(
            name="disabled start location", auction=disabled_auction, pickup_time=the_future
        )
        AuctionTOS.objects.create(user=self.user, auction=disabled_auction, pickup_location=disabled_location)

        required_auction = Auction.objects.create(
            created_by=self.user,
            title="Required target auction",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
            use_custom_dropdown_field="required",
            custom_dropdown_name="Habitat",
        )
        required_location = PickupLocation.objects.create(
            name="required target location", auction=required_auction, pickup_time=the_future
        )
        AuctionTOS.objects.create(user=self.user, auction=required_auction, pickup_location=required_location)
        AuctionDropdown.objects.create(auction=required_auction, user=self.user, value="River")
        AuctionDropdown.objects.create(auction=required_auction, user=self.user, value="Pond")

        user_data.last_auction_used = disabled_auction
        user_data.save()
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.post(
            "/lots/new/",
            data={
                "part_of_auction": "True",
                "auction": str(required_auction.pk),
                "lot_name": "Required switch lot",
                "species_category": str(Category.objects.filter(name="Uncategorized").first().pk),
                "quantity": "1",
                "reserve_price": "5",
                "custom_dropdown": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("custom_dropdown", response.context["form"].errors)

    def test_get_params_not_applied_to_edit_form(self):
        """GET params should not be applied to the lot edit form"""
        the_future = timezone.now() + datetime.timedelta(days=3)
        # Set up user with contact info
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()
        # Create an open auction with an editable lot
        open_auction = Auction.objects.create(
            created_by=self.user,
            title="Open auction edit test",
            is_online=True,
            date_end=the_future,
            date_start=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
        )
        open_location = PickupLocation.objects.create(
            name="edit location", auction=open_auction, pickup_time=the_future
        )
        test_tos = AuctionTOS.objects.create(user=self.user, auction=open_auction, pickup_location=open_location)
        editable_lot = Lot.objects.create(
            lot_name="Original Name",
            auction=open_auction,
            auctiontos_seller=test_tos,
            quantity=1,
            user=self.user,
        )
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("edit_lot", kwargs={"pk": editable_lot.pk})
        response = self.client.get(f"{url}?lot_name=HackedName")
        assert response.status_code == 200
        form = response.context["form"]
        # The form should show the existing lot name, not be overridden by GET params
        assert form["lot_name"].value() == "Original Name"
        assert form.instance.lot_name == "Original Name"


class InvoiceViewTests(StandardTestCase):
    """Test invoice views with different user types"""

    def test_invoice_view_anonymous(self):
        """Anonymous users should not view invoices"""
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_invoice_view_owner(self):
        """Invoice owner can view their invoice"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_invoice_view_other_user(self):
        """Other users should not view someone else's invoice"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        # Should be denied
        assert response.status_code in [302, 403]

    def test_invoice_view_admin(self):
        """Admin can view any invoice"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_invoice_view_admin_new_adjustment_amount_starts_blank(self):
        """New adjustment rows should not prefill amount with 0."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200
        amount_value = response.context["formset"].forms[0]["amount"].value()
        assert amount_value in ["", None]


class MyInvoicesListTests(StandardTestCase):
    """/invoices/ -- the user's own invoice list, an htmx table sorted newest first"""

    def setUp(self):
        super().setUp()
        # A second invoice for the same user, deliberately created out of date order so that
        # "newest first" can't pass by accident on insertion order.
        self.older_invoice = self.invoice
        self.newer_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        Invoice.objects.filter(pk=self.older_invoice.pk).update(
            date=timezone.now() - datetime.timedelta(days=10),
        )
        Invoice.objects.filter(pk=self.newer_invoice.pk).update(date=timezone.now())

    def invoice_pks(self, response):
        return [row.record.pk for row in response.context["table"].rows]

    def test_anonymous_user_is_sent_to_login(self):
        response = self.client.get(reverse("my_invoices"))
        assert response.status_code in [301, 302, 403]

    def test_default_sort_is_newest_first(self):
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"))
        assert response.status_code == 200
        self.assertEqual(response.context["table"].order_by, ("-date",))
        self.assertEqual(self.invoice_pks(response), [self.newer_invoice.pk, self.older_invoice.pk])

    def test_date_column_can_be_sorted_oldest_first(self):
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"), {"sort": "date"})
        self.assertEqual(self.invoice_pks(response), [self.older_invoice.pk, self.newer_invoice.pk])

    def test_other_users_invoices_are_not_listed(self):
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"))
        assert self.invoiceB.pk not in self.invoice_pks(response)

    def test_query_filters_by_auction_name(self):
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"), {"query": "in-person"})
        self.assertEqual(self.invoice_pks(response), [self.newer_invoice.pk])

    def test_query_filters_by_status_word(self):
        Invoice.objects.filter(pk=self.newer_invoice.pk).update(status="PAID")
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"), {"query": "paid"})
        self.assertEqual(self.invoice_pks(response), [self.newer_invoice.pk])

    def test_htmx_request_returns_only_the_table(self):
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"), HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        self.assertNotContains(response, "<html")
        self.assertContains(response, f"/invoices/{self.newer_invoice.pk}/")

    def test_user_with_no_invoices_sees_the_empty_state(self):
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"))
        assert response.status_code == 200
        self.assertContains(response, "any invoices yet")

    def test_no_search_results_says_so(self):
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(reverse("my_invoices"), {"query": "nothing matches this"})
        self.assertContains(response, "No invoices match")
