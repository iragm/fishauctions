"""Getting started, sellers and bidders: the create bounce, the join form, the email settings mailing address.

These are the review fixes that send a new person to the right page instead of leaving them stuck.
"""

from django.contrib.messages import get_messages
from django.forms import HiddenInput
from django.urls import reverse

from auctions.forms import AuctionJoin, ClubEmailSettingsForm
from auctions.models import Club
from auctions.tests import StandardTestCase


class OnboardingTests(StandardTestCase):
    def test_create_auction_without_permission_goes_to_support(self):
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("create_auction"))
        self.assertRedirects(response, reverse("support"), fetch_redirect_response=False)
        self.assertTrue(any("can't create auctions yet" in str(m) for m in get_messages(response.wsgi_request)))

    def test_single_location_join_needs_no_tick(self):
        form = AuctionJoin(user=self.user_with_no_lots, auction=self.in_person_auction)
        self.assertTrue(form.fields["i_agree"].initial)
        self.assertIsInstance(form.fields["i_agree"].widget, HiddenInput)

    def test_join_page_shows_add_phone_link(self):
        self.online_auction.require_phone_number = True
        self.online_auction.save()
        userdata = self.user_with_no_lots.userdata
        userdata.phone_number = ""
        userdata.address = "123 Test Street"
        userdata.save()
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Add your phone number to join")
        self.assertContains(response, reverse("contact_info"))

    def test_join_page_shows_add_address_link_for_mail(self):
        self.location.pickup_by_mail = True
        self.location.save()
        userdata = self.user_with_no_lots.userdata
        userdata.phone_number = "555-0100"
        userdata.address = ""
        userdata.save()
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Add your address to join")
        self.assertNotContains(response, "Add your phone number to join")

    def test_join_button_when_nothing_missing(self):
        userdata = self.user_with_no_lots.userdata
        userdata.phone_number = "555-0100"
        userdata.address = "123 Test Street"
        userdata.save()
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Join auction")
        self.assertNotContains(response, "Add your phone number to join")
        self.assertNotContains(response, "Add your address to join")

    def test_email_settings_need_address_when_emails_on(self):
        club = Club.objects.create(name="Test club")
        form = ClubEmailSettingsForm(
            data={"send_membership_renewal_confirmation": "on", "mailing_address": ""},
            instance=club,
            show_email_routing=False,
        )
        form.is_valid()
        self.assertIn("mailing_address", form.errors)
        self.assertIn("Add a mailing address to send these emails.", form.errors["mailing_address"])

    def test_email_settings_allow_blank_address_when_emails_off(self):
        club = Club.objects.create(name="Test club")
        form = ClubEmailSettingsForm(
            data={"mailing_address": ""},
            instance=club,
            show_email_routing=False,
        )
        form.is_valid()
        self.assertNotIn("mailing_address", form.errors)
