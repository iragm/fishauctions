"""Club management, access requests and the AI agent banner: the review fixes that save an admin a trip.

Covers the one-click access request that replaced the mailto buttons, the club sidebar's collapsible auction
block, the setup list's connected marks, the import prompt on a club's members page, and the banner pointing
admins with no AI agent at ChatGPT and Claude.
"""

import datetime
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from auctions.models import Club, ClubMember, UserAPIKey, UserData
from auctions.tests import StandardTestCase


class AccessRequestTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="my_lot", password="testpassword")
        self.url = reverse("access_request")

    def test_access_request_emails_owner_once(self):
        with mock.patch("post_office.mail.send") as send:
            response = self.client.post(
                f"{self.url}?next=/",
                {"kind": "promotion", "auction": self.online_auction.slug},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(send.call_count, 1)
        args, kwargs = send.call_args
        self.assertEqual(kwargs["subject"], "Request access to create promoted auctions")
        self.assertIn("Username: my_lot", kwargs["message"])
        self.assertIn(self.online_auction.get_absolute_url(), kwargs["message"])
        self.assertEqual(kwargs["headers"], {"Reply-To": self.user.email})

    def test_access_request_rate_limited(self):
        with mock.patch("post_office.mail.send") as send:
            for _ in range(4):
                self.client.post(self.url, {"kind": "square"})
        self.assertEqual(send.call_count, 3)

    def test_access_request_rejects_unknown_kind(self):
        with mock.patch("post_office.mail.send") as send:
            response = self.client.post(self.url, {"kind": "something-else"})
        self.assertEqual(response.status_code, 400)
        send.assert_not_called()


class ClubSidebarAuctionBlockTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="my_lot", password="testpassword")
        self.club = Club.objects.create(name="Harbor Aquarium Society", active=True)
        ClubMember.objects.create(club=self.club, user=self.user, permission_admin=True)
        self.online_auction.club = self.club
        self.online_auction.save()
        self.club.current_auction = self.online_auction
        self.club.save()

    def test_sidebar_auction_block_open_on_auction_page(self):
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, 'id="clubSidebarAuction" class="collapse show"')

    def test_sidebar_auction_block_collapsed_on_club_page(self):
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertContains(response, 'id="clubSidebarAuction" class="collapse">')
        self.assertNotContains(response, 'id="clubSidebarAuction" class="collapse show"')


class ClubSetupListTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="my_lot", password="testpassword")
        self.club = Club.objects.create(name="Harbor Aquarium Society", active=True)
        ClubMember.objects.create(club=self.club, user=self.user, permission_admin=True)
        self.url = reverse("club_setup", kwargs={"slug": self.club.slug})
        self.mailchimp_url = reverse("club_mailchimp_config", kwargs={"slug": self.club.slug})
        self.brevo_url = reverse("club_brevo_config", kwargs={"slug": self.club.slug})

    def test_setup_marks_connected_and_hides_other_mail_provider(self):
        self.club.brevo_connected_on = timezone.now()
        self.club.discord_server_id = "1234567890"
        self.club.save()
        response = self.client.get(self.url)
        self.assertContains(response, f'href="{self.brevo_url}"')
        self.assertNotContains(response, f'href="{self.mailchimp_url}"')
        self.assertContains(response, "bi-check-circle-fill text-success")

    def test_setup_lists_import_members(self):
        response = self.client.get(self.url)
        import_url = reverse("club_member_import", kwargs={"slug": self.club.slug})
        self.assertContains(response, f'href="{import_url}"')
        self.assertContains(response, "Import members")


class NewClubMembersPageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="my_lot", password="testpassword")
        self.club = Club.objects.create(name="Harbor Aquarium Society", active=True)
        ClubMember.objects.create(club=self.club, user=self.user, permission_admin=True)
        self.url = reverse("club_admin", kwargs={"slug": self.club.slug})
        self.import_href = f'href="{reverse("club_member_import", kwargs={"slug": self.club.slug})}"'

    def test_new_club_lands_on_members(self):
        response = self.client.get(self.url)
        self.assertContains(response, self.import_href)
        self.assertContains(response, "Import members")

    def test_import_prompt_hides_once_someone_else_is_a_member(self):
        ClubMember.objects.create(club=self.club, name="Jane Doe", email="jane@example.com")
        response = self.client.get(self.url)
        self.assertNotContains(response, self.import_href)


class AIAgentBannerTests(StandardTestCase):
    """The banner that stands where the AI box was, while the box is off."""

    def setUp(self):
        super().setUp()
        self.auction_url = reverse("auction_main", kwargs={"slug": self.online_auction.slug})

    def test_an_admin_with_no_agent_sees_the_banner_and_no_box(self):
        UserData.objects.filter(user=self.user).update(use_llm_search=True)
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(self.auction_url)
        self.assertContains(response, "data-ai-agent-banner")
        self.assertContains(response, reverse("help_guide", kwargs={"slug": "ai-agents"}))
        self.assertNotContains(response, "data-palette-box")

    def test_an_admin_with_an_agent_connected_sees_neither(self):
        UserAPIKey.objects.create(user=self.user, name="mine", prefix="ak_banner", key_hash="x")
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(self.auction_url)
        self.assertNotContains(response, "data-ai-agent-banner")
        self.assertNotContains(response, "data-palette-box")

    def test_an_expired_key_is_no_agent(self):
        UserAPIKey.objects.create(
            user=self.user,
            name="old",
            prefix="ak_old",
            key_hash="x",
            expires_at=timezone.now() - datetime.timedelta(days=1),
        )
        self.client.login(username="my_lot", password="testpassword")
        self.assertContains(self.client.get(self.auction_url), "data-ai-agent-banner")

    def test_not_shown_to_a_non_admin(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertNotContains(self.client.get(self.auction_url), "data-ai-agent-banner")

    def test_on_a_club_page_it_is_for_the_people_who_run_the_club(self):
        club = Club.objects.create(name="Harbor Aquarium Society", active=True)
        ClubMember.objects.create(club=club, user=self.user, permission_admin=True)
        ClubMember.objects.create(club=club, user=self.user_with_no_lots)
        url = reverse("club_detail", kwargs={"slug": club.slug})
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(url)
        self.assertContains(response, "data-ai-agent-banner")
        self.assertContains(response, "run your club")
        self.assertNotContains(response, "data-palette-box")
        self.client.login(username="no_lots", password="testpassword")
        self.assertNotContains(self.client.get(url), "data-ai-agent-banner")
