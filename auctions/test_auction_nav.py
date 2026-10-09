from django.contrib.auth.models import User
from django.urls import reverse
from django.utils.html import escape

from auctions import auction_nav
from auctions.models import Auction, Club, ClubMember
from auctions.tests import StandardTestCase

IN_PERSON_ONLY = {"Print labels", "Print paddles", "Recruit volunteers", "Lot queue", "Set lot winners", "Checkout"}
EVERYWHERE = {
    "Main",
    "Users",
    "Lots",
    "Rules",
    "Custom fields",
    "Location",
    "Printable lot list",
    "Stats",
    "Chat messages",
    "Lot map",
    "Feedback",
    "Admin history",
    "Help",
    "Copy to new auction",
    "Delete auction",
}


def _labels(auction):
    return {row["label"] for group in auction_nav.groups_for(auction) for row in group["rows"]}


class AuctionNavTests(StandardTestCase):
    def test_online_auction_has_no_in_person_pages(self):
        self.assertEqual(_labels(self.online_auction), EVERYWHERE)

    def test_in_person_auction_has_everything_but_club_pages(self):
        self.assertEqual(_labels(self.in_person_auction), EVERYWHERE | IN_PERSON_ONLY)

    def test_pages_page_lists_every_row_with_its_description(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_pages", kwargs={"slug": self.in_person_auction.slug}))
        self.assertEqual(response.status_code, 200)
        for group in auction_nav.groups_for(self.in_person_auction):
            for row in group["rows"]:
                self.assertContains(response, escape(row["description"]))

    def test_pages_page_is_admin_only(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(reverse("auction_pages", kwargs={"slug": self.in_person_auction.slug}))
        self.assertEqual(response.status_code, 403)

    def test_more_tab_links_to_pages_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_stats", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, reverse("auction_pages", kwargs={"slug": self.online_auction.slug}))

    def test_online_pages_page_hides_in_person_pages(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_pages", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, "Admin history")
        self.assertNotContains(response, "Set lot winners")

    def test_a_club_member_who_manages_people_sees_the_pages_that_let_them_in(self):
        club = Club.objects.create(name="Nav Club")
        Auction.objects.filter(pk=self.in_person_auction.pk).update(club=club, manage_users_through_club="all")
        helper = User.objects.create_user(username="door_helper", password="testpassword")
        ClubMember.objects.create(club=club, user=helper, name="Helper", permission_add_edit=True)
        self.client.force_login(helper)
        response = self.client.get(reverse("auction_pages", kwargs={"slug": self.in_person_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("auction_quick_check_in", kwargs={"slug": self.in_person_auction.slug}))
        self.assertContains(response, reverse("auction_paddles", kwargs={"slug": self.in_person_auction.slug}))
        self.assertNotContains(response, reverse("auction_delete", kwargs={"slug": self.in_person_auction.slug}))
        self.assertNotContains(response, "Admin history")
