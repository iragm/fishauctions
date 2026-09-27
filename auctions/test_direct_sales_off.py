"""Selling outside an auction, switched off by ``ALLOW_USERS_TO_CREATE_LOTS``: nothing user facing offers it,
whatever an old account's own flag says. The code behind it stays for the day it comes back."""

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings

from auctions.filters import LotFilter
from auctions.forms import ChangeUserNotificationsForm, UserLocation
from auctions.models import Lot


class DirectSalesOffTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="old_seller", password="x", email="old@example.com")
        # An account from before the switch was turned off.
        self.user.userdata.can_submit_standalone_lots = True
        self.user.userdata.save()

    def lot_filter(self):
        request = RequestFactory().get("/lots/")
        request.user = self.user
        return LotFilter(request.GET, queryset=Lot.objects.all(), request=request)

    @override_settings(ALLOW_USERS_TO_CREATE_LOTS=False)
    def test_the_account_flag_is_overridden(self):
        self.assertFalse(self.user.userdata.can_sell_standalone_lots)

    @override_settings(ALLOW_USERS_TO_CREATE_LOTS=True)
    def test_the_account_flag_counts_when_it_is_on(self):
        self.assertTrue(self.user.userdata.can_sell_standalone_lots)

    @override_settings(ALLOW_USERS_TO_CREATE_LOTS=False)
    def test_lot_filters_have_no_standalone_options(self):
        form = self.lot_filter().form
        self.assertNotIn("distance", form.fields)
        self.assertNotIn("ships", form.fields)
        self.assertNotIn("no_auction", str(form["auction"]))

    @override_settings(ALLOW_USERS_TO_CREATE_LOTS=True)
    def test_lot_filters_offer_them_when_it_is_on(self):
        form = self.lot_filter().form
        self.assertIn("ships", form.fields)
        self.assertIn("no_auction", str(form["auction"]))

    @override_settings(ALLOW_USERS_TO_CREATE_LOTS=False)
    def test_contact_info_has_no_ship_to_region(self):
        self.assertNotIn("location", UserLocation(instance=self.user.userdata).fields)

    @override_settings(ALLOW_USERS_TO_CREATE_LOTS=True)
    def test_contact_info_has_the_region_when_it_is_on(self):
        self.assertIn("location", UserLocation(instance=self.user.userdata).fields)

    def test_notifications_page_has_no_standalone_lot_emails(self):
        """Nothing sends them since the weekly email went, so they aren't offered either way."""
        fields = ChangeUserNotificationsForm(self.user, instance=self.user.userdata).fields
        self.assertNotIn("email_me_about_new_local_lots", fields)
        self.assertNotIn("email_me_about_new_lots_ship_to_location", fields)
