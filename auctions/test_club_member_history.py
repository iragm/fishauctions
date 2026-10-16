from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from auctions.models import Club, ClubHistory, ClubMember
from auctions.views.club_reports import ClubMemberCSVImportView


class ClubMemberHistoryTests(TestCase):
    """The member list's History action: the club history filtered to one member."""

    def setUp(self):
        self.club = Club.objects.create(name="History Club")
        self.admin = User.objects.create_user(username="history_admin", password="testpass", email="ha@example.com")
        self.admin_member = ClubMember.objects.create(
            club=self.club, user=self.admin, name="History Admin", permission_add_edit=True, permission_view=True
        )
        self.member = ClubMember.objects.create(club=self.club, name="Pat Member", email="pat@example.com")
        self.other = ClubMember.objects.create(club=self.club, name="Sam Other", email="sam@example.com")
        self.history_url = reverse("club_history", kwargs={"slug": self.club.slug})

    def test_the_actions_dropdown_links_to_the_members_history(self):
        self.client.login(username="history_admin", password="testpass")
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertContains(response, f"{self.history_url}?member={self.member.pk}")

    def test_no_history_link_without_permission_to_see_the_history(self):
        ClubMember.objects.filter(pk=self.admin_member.pk).update(permission_view=False)
        self.client.login(username="history_admin", password="testpass")
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertNotContains(response, f"{self.history_url}?member=")

    def test_the_history_page_shows_only_that_members_rows(self):
        ClubHistory.objects.create(club=self.club, action="Pat row", applies_to="MEMBERS", member=self.member)
        ClubHistory.objects.create(club=self.club, action="Sam row", applies_to="MEMBERS", member=self.other)
        ClubHistory.objects.create(club=self.club, action="Settings row", applies_to="SETTINGS")
        self.client.login(username="history_admin", password="testpass")
        response = self.client.get(f"{self.history_url}?member={self.member.pk}")
        self.assertContains(response, "Pat row")
        self.assertNotContains(response, "Sam row")
        self.assertNotContains(response, "Settings row")
        self.assertEqual(response.context["history_member"], self.member)

    def test_another_clubs_member_names_nobody(self):
        stranger = ClubMember.objects.create(club=Club.objects.create(name="Elsewhere"), name="Stranger")
        self.client.login(username="history_admin", password="testpass")
        response = self.client.get(f"{self.history_url}?member={stranger.pk}")
        self.assertIsNone(response.context.get("history_member"))

    def test_an_edit_is_linked_to_the_member(self):
        self.client.login(username="history_admin", password="testpass")
        self.client.post(reverse("club_member_delete", kwargs={"pk": self.member.pk}))
        self.assertTrue(self.member.history.filter(action__startswith="Deactivated").exists())

    def test_a_csv_import_is_in_each_imported_members_history(self):
        view = ClubMemberCSVImportView()
        view.club = self.club
        view.request = RequestFactory().post("/")
        view.request.user = self.admin
        create = view.plan_row({"Name": "New Person", "Email": "new@example.com"})
        update = view.plan_row({"Name": "Pat Member", "Email": "pat@example.com", "Phone": "5551234567"})
        results = {view.apply_action(create, "merge"): 1, view.apply_action(update, "merge"): 1}
        view.record_import_history(results, "roster.csv")
        row = ClubHistory.objects.get(club=self.club, action__startswith="CSV import")
        self.assertIn(self.member, row.members.all())
        self.assertIn(ClubMember.objects.get(club=self.club, email="new@example.com"), row.members.all())
        self.assertNotIn(self.other, row.members.all())

    def test_a_merge_carries_the_duplicates_history(self):
        ClubHistory.objects.create(club=self.club, action="Sam's old row", applies_to="MEMBERS", member=self.other)
        self.member.merge_duplicate(self.other)
        self.assertTrue(self.member.history.filter(action="Sam's old row").exists())
