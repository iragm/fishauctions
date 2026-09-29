"""Participant rows link to their person's account when written, so "mine" is ``user`` alone.

``AuctionTOS.link_user`` and ``ClubMember.save`` link by email (or through the club member) on every
save; sign-in, by any route, and an account email change catch rows written before the account had the
address. Nothing reads by email any more, so a row that should be linked and isn't is invisible.
"""

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.urls import reverse

from auctions.models import AuctionTOS, Club, ClubMember, Lot
from auctions.tests import StandardTestCase


class AccountLinkingTests(StandardTestCase):
    def _walk_in(self, email=""):
        """A bidder an admin added at the door."""
        return AuctionTOS.objects.create(
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            name="Walk-in",
            email=email,
            manually_added=True,
        )

    def _person(self, username="later", email="later@example.com"):
        user = User.objects.create_user(username=username, password="testpassword", email=email)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
        return user

    def test_an_address_added_later_links_the_row(self):
        user = self._person()
        tos = self._walk_in()
        self.assertIsNone(tos.user_id)
        tos.email = "Later@Example.com"
        tos.save()
        self.assertEqual(tos.user, user)

    def test_a_save_of_a_few_columns_still_stores_the_link(self):
        tos = self._walk_in(email="checkin@example.com")
        user = self._person(email="checkin@example.com")
        tos.save(update_fields=["checked_in"])
        tos.refresh_from_db()
        self.assertEqual(tos.user, user)

    def test_linking_claims_the_lots_already_sold_under_the_row(self):
        user = self._person()
        tos = self._walk_in()
        lot = Lot.objects.create(
            lot_name="Brought to the door", auction=self.in_person_auction, auctiontos_seller=tos, quantity=1
        )
        tos.email = user.email
        tos.save()
        lot.refresh_from_db()
        self.assertEqual(lot.user, user)

    def test_a_club_members_account_links_its_auction_rows(self):
        user = self._person()
        club = Club.objects.create(name="Linking club")
        member = ClubMember.objects.create(club=club, name="Member", email=user.email)
        self.assertEqual(member.user, user)
        tos = self._walk_in()
        tos.clubmember = member
        tos.save()
        self.assertEqual(tos.user, user)

    def test_inactive_accounts_are_not_linked(self):
        user = self._person()
        user.is_active = False
        user.save()
        self.assertIsNone(self._walk_in(email=user.email).user_id)
        club = Club.objects.create(name="Inactive club")
        self.assertIsNone(ClubMember.objects.create(club=club, name="Gone", email=user.email).user_id)

    def test_changing_the_account_address_links_rows_with_the_new_one(self):
        tos = self._walk_in(email="new@example.com")
        user = self._person(email="old@example.com")
        user.email = "new@example.com"
        user.save()
        tos.refresh_from_db()
        self.assertEqual(tos.user, user)

    def test_signing_in_to_the_app_links_rows_from_before_the_account(self):
        tos = self._walk_in(email="phone@example.com")
        self._person(username="phone", email="phone@example.com")
        response = self.client.post(
            reverse("mobile-auth-login"),
            data={"credential": "phone", "password": "testpassword"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        tos.refresh_from_db()
        self.assertEqual(tos.user.username, "phone")

    def test_a_linked_row_older_than_the_persons_own_is_the_one_kept(self):
        """The person joined themselves after an admin had added them: linking the admin's row merges the
        two, and the older row survives.
        """
        added = self._walk_in(email="twice@example.com")
        user = self._person(email="twice@example.com")
        joined = AuctionTOS.objects.create(
            auction=self.in_person_auction, pickup_location=self.in_person_location, user=user
        )
        added.save()
        self.assertFalse(AuctionTOS.objects.filter(pk=joined.pk).exists())
        added.refresh_from_db()
        self.assertEqual(added.user, user)

    def test_what_you_won_without_an_account_is_on_your_buying_dashboard(self):
        tos = self._walk_in(email="buyer@example.com")
        Lot.objects.create(
            lot_name="Won at the door",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            auctiontos_winner=tos,
            winning_price=5,
            quantity=1,
        )
        self._person(username="buyer", email="buyer@example.com")
        self.client.login(username="buyer", password="testpassword")
        response = self.client.get(reverse("buying") + f"?auction={self.in_person_auction.slug}")
        self.assertContains(response, "Won at the door")
        self.assertContains(response, ">Won</span>")
        self.assertEqual(response.context["recent_auctions"], [self.in_person_auction])
