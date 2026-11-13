from django.contrib.auth.models import User
from django.urls import reverse
from django.utils.html import escape

from auctions import auction_nav
from auctions.models import Auction, Club, ClubMember
from auctions.tests import StandardTestCase

IN_PERSON_ONLY = {
    "Print labels",
    "Print paddles",
    "Recruit volunteers",
    "Lot queue",
    "Set lot winners",
    "Checkout",
    "Lot map",
}
ONLINE_ONLY = {"Logistics"}
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
        self.assertEqual(_labels(self.online_auction), EVERYWHERE | ONLINE_ONLY)

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

    def test_online_club_auction_has_no_card_scanning(self):
        club = Club.objects.create(name="Online Club")
        Auction.objects.filter(pk=self.online_auction.pk).update(club=club, manage_users_through_club="all")
        self.online_auction.refresh_from_db()
        self.assertNotIn("Scan club cards", _labels(self.online_auction))
        self.client.force_login(self.user)
        url = reverse("auction_quick_check_in", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, url)
        response = self.client.get(reverse("auction_pages", kwargs={"slug": self.online_auction.slug}))
        self.assertNotContains(response, url)
        self.assertNotContains(response, "Lot map")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_online_check_in_mode_auction_keeps_check_in(self):
        club = Club.objects.create(name="Single Club")
        Auction.objects.filter(pk=self.online_auction.pk).update(club=club, manage_users_through_club="checkin")
        self.online_auction.refresh_from_db()
        self.assertIn("Check in", _labels(self.online_auction))
        self.client.force_login(self.user)
        url = reverse("auction_quick_check_in", kwargs={"slug": self.online_auction.slug})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_in_person_club_auction_keeps_card_scanning(self):
        club = Club.objects.create(name="In Person Club")
        Auction.objects.filter(pk=self.in_person_auction.pk).update(club=club, manage_users_through_club="all")
        self.in_person_auction.refresh_from_db()
        self.assertIn("Scan club cards", _labels(self.in_person_auction))
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.in_person_auction.slug}))
        self.assertContains(response, "Scan club cards")


class ClubSidebarMembersLinkTests(StandardTestCase):
    """The sidebar's Members link reaches whoever the member list lets in, and nobody else."""

    def _member(self, username, **permissions):
        user = User.objects.create_user(username=username, password="testpassword")
        ClubMember.objects.create(club=self.club, user=user, name=username, **permissions)
        return user

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Sidebar Club")
        Auction.objects.filter(pk=self.in_person_auction.pk).update(club=self.club, manage_users_through_club="")
        self.in_person_auction.refresh_from_db()
        self.members_url = reverse("club_admin", kwargs={"slug": self.club.slug})

    def test_auction_runner_gets_members_link_when_people_are_not_managed_through_the_club(self):
        self.client.force_login(self._member("runner", permission_manage_auctions=True))
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.in_person_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self.members_url}"')
        self.assertEqual(self.client.get(self.members_url).status_code, 200)

    def test_managed_auction_points_members_at_its_users(self):
        Auction.objects.filter(pk=self.in_person_auction.pk).update(manage_users_through_club="all")
        self.client.force_login(self._member("managed_runner", permission_manage_auctions=True))
        response = self.client.get(reverse("auction_lot_list", kwargs={"slug": self.in_person_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{self.members_url}"')
        self.assertContains(response, reverse("auction_tos_list", kwargs={"slug": self.in_person_auction.slug}))

    def test_treasurer_without_member_access_gets_no_members_link(self):
        self.client.force_login(self._member("treasurer", permission_money=True))
        response = self.client.get(reverse("club_treasurer_report", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{self.members_url}"')
        self.assertEqual(self.client.get(self.members_url).status_code, 403)

    def test_bap_manager_on_club_page_gets_members_link(self):
        Club.objects.filter(pk=self.club.pk).update(enable_breeder_award_program=True)
        self.client.force_login(self._member("bap", permission_manage_bap=True))
        response = self.client.get(reverse("club_bap_lots", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self.members_url}"')
