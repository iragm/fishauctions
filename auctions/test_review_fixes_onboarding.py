"""Getting started, sellers and bidders: the create bounce, the join form, the email settings mailing address.

These are the review fixes that send a new person to the right page instead of leaving them stuck.
"""

import datetime
import re

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.forms import HiddenInput
from django.urls import reverse
from django.utils import timezone

from auctions.forms import AuctionJoin, ClubEmailSettingsForm
from auctions.models import AuctionTOS, Club
from auctions.tests import StandardTestCase


class OnboardingTests(StandardTestCase):
    def _open_online_auction_for_joining(self, user):
        """An online auction still taking entries, which ``user`` has not joined.

        The fixture's online auction ended two days ago, so its join card is hidden; ``no_lots`` already
        holds a ticket on it, so the page shows "View lots" rather than the join button.
        """
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=3)
        self.online_auction.save()
        self.client.login(username=user.username, password="testpassword")
        return user.userdata

    def test_create_auction_without_permission_goes_to_support(self):
        userdata = self.user_with_no_lots.userdata
        userdata.can_create_club_auctions = False
        userdata.save()
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("create_auction"))
        self.assertRedirects(response, reverse("support"), fetch_redirect_response=False)
        self.assertTrue(any("can't create auctions yet" in str(m) for m in get_messages(response.wsgi_request)))

    def test_single_location_join_needs_no_tick(self):
        form = AuctionJoin(user=self.user_with_no_lots, auction=self.in_person_auction)
        self.assertNotIn("i_agree", form.fields)
        self.assertIsInstance(form.fields["pickup_location"].widget, HiddenInput)

    def test_single_location_join_posts_back_what_the_page_renders(self):
        """A hidden agree field once rendered "False", so pressing Join on a single-location auction did nothing."""
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=3)
        self.in_person_auction.save()
        newcomer = get_user_model().objects.create_user(
            username="newcomer",
            password="testpassword",
            email="newcomer@example.com",
            first_name="New",
            last_name="Comer",
        )
        newcomer.emailaddress_set.create(email=newcomer.email, verified=True, primary=True)
        self.client.login(username="newcomer", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.in_person_auction.slug})
        page = self.client.get(url).content.decode()
        form = re.search(r'<form[^>]*id="rule-form".*?</form>', page, re.DOTALL).group(0)
        data = {}
        for tag in re.findall(r"<input[^>]*>", form):
            name = re.search(r'name="([^"]*)"', tag)
            if name and 'type="checkbox"' not in tag:
                value = re.search(r'value="([^"]*)"', tag)
                data[name.group(1)] = value.group(1) if value else ""
        self.assertIn("pickup_location", data)
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuctionTOS.objects.filter(user=newcomer, auction=self.in_person_auction).exists())

    def test_join_page_shows_add_phone_link(self):
        self.online_auction.require_phone_number = True
        self.online_auction.save()
        userdata = self._open_online_auction_for_joining(self.user_who_does_not_join)
        userdata.phone_number = ""
        userdata.address = "123 Test Street"
        userdata.save()
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Add your phone number to join")
        self.assertContains(response, reverse("contact_info"))

    def test_join_page_shows_add_address_link_for_mail(self):
        self.location.pickup_by_mail = True
        self.location.save()
        userdata = self._open_online_auction_for_joining(self.user_who_does_not_join)
        userdata.phone_number = "555-0100"
        userdata.address = ""
        userdata.save()
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Add your address to join")
        self.assertNotContains(response, "Add your phone number to join")

    def test_join_button_when_nothing_missing(self):
        userdata = self._open_online_auction_for_joining(self.user_who_does_not_join)
        userdata.phone_number = "555-0100"
        userdata.address = "123 Test Street"
        userdata.save()
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
