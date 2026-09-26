"""An account with no email address matches nobody by email.

Rows are matched to accounts by address all over the site, and participants added by hand often have
none. A bare ``Q(email=user.email)`` for an account whose address is blank matched every one of them:
signing in claimed them all, and their invoices listed as the account's own.
"""

from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.urls import reverse

from auctions.models import AuctionTOS, Club, ClubMember, Invoice, email_q
from auctions.tests import StandardTestCase


class BlankEmailTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.nobody = User.objects.create_user("no_address", "", "pw")
        self.hand_added = AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, name="Walk-in", email=""
        )

    def test_email_q_matches_nothing_for_a_blank_address(self):
        self.assertFalse(AuctionTOS.objects.filter(email_q("email", "")).exists())
        self.assertFalse(AuctionTOS.objects.filter(email_q("email", None)).exists())

    def test_signing_in_claims_nobody(self):
        club = Club.objects.create(name="Blank email club")
        member = ClubMember.objects.create(club=club, name="No email member", email="")
        user_logged_in.send(sender=User, user=self.nobody, request=None)
        self.hand_added.refresh_from_db()
        member.refresh_from_db()
        self.assertIsNone(self.hand_added.user_id)
        self.assertIsNone(member.user_id)

    def test_invoices_of_hand_added_people_are_not_listed(self):
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.hand_added)
        self.client.force_login(self.nobody)
        response = self.client.get(f"/invoices/{invoice.pk}/")
        self.assertNotEqual(response.status_code, 200)

    def test_my_invoices_lists_none_of_them(self):
        Invoice.objects.get_or_create(auctiontos_user=self.hand_added)
        self.client.force_login(self.nobody)
        response = self.client.get(reverse("my_invoices"))
        self.assertNotContains(response, "Walk-in")
