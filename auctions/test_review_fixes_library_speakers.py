"""Review fixes for the library list and the speaker directory: the library's default list, its Mine,
topic and year filters and search box; speaker tagging, which anyone signed in may do; the remote chip,
contactable-first order and the claimed mark.
"""

from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from auctions.documents.models import Document, Visibility
from auctions.models import Club, ClubMember, Speaker, SpeakerTag
from auctions.tests import StandardTestCase

HTMX = {"HTTP_HX_REQUEST": "true"}


def make_document(title, owner, **kwargs):
    kwargs.setdefault("visibility", Visibility.PUBLIC)
    return Document.objects.create(
        owner=owner, title=title, original_name=f"{title}.md", status=Document.READY, **kwargs
    )


class LibraryListFixesTests(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.member = User.objects.create_user("wp9_member", "wp9_member@example.com", "pw")
        cls.other = User.objects.create_user("wp9_other", "wp9_other@example.com", "pw")
        cls.club = Club.objects.create(name="WP9 Fish Club", abbreviation="WP9")
        ClubMember.objects.create(club=cls.club, user=cls.member, name="Bob Smith")
        make_document("Member notes", cls.member, club=None, visibility=Visibility.PRIVATE)
        make_document("Club newsletter", cls.other, club=cls.club, visibility=Visibility.CLUB)
        make_document("Public article", cls.other, club=None, visibility=Visibility.PUBLIC)
        make_document("Someone else's notes", cls.other, club=None, visibility=Visibility.PRIVATE)
        make_document("Breeding notes", cls.other, club=None, topics=["breeding"], year=1977)
        make_document("Food notes", cls.other, club=None, topics=["food"], year=1990)

    def test_library_lists_public_documents_by_default(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("library"))
        self.assertContains(response, "Public article")
        self.assertContains(response, "Club newsletter")
        self.assertNotContains(response, "Someone else&#x27;s notes")

    def test_library_mine_filter(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("library"), {"mine": "1"}, **HTMX)
        self.assertContains(response, "Member notes")
        self.assertContains(response, "Club newsletter")
        self.assertNotContains(response, "Public article")

    def test_library_topic_and_year_filters(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("library"), {"topic": "breeding"}, **HTMX)
        self.assertContains(response, "Breeding notes")
        self.assertNotContains(response, "Food notes")
        response = self.client.get(reverse("library"), {"year": "1990"}, **HTMX)
        self.assertContains(response, "Food notes")
        self.assertNotContains(response, "Breeding notes")
        page = self.client.get(reverse("library"))
        self.assertContains(page, "?topic=breeding")
        self.assertContains(page, "?year=1977")

    def test_library_box_submits_to_search(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("library"))
        self.assertContains(page, f'action="{reverse("library_search")}"')
        self.assertContains(page, 'name="query"')
        with mock.patch("auctions.views.documents.search", return_value=([], 0)):
            response = self.client.get(reverse("library_search"), {"query": "breeding"})
        self.assertEqual(response.context["query"], "breeding")


class SpeakerDirectoryFixesTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user("wp9_outsider", "wp9_outsider@example.com", "pw")
        self.speaker = Speaker.objects.create(name="Talker, Terry")
        self.url = reverse("speaker_tag", kwargs={"slug": self.speaker.slug})

    def test_any_signed_in_user_can_tag(self):
        client = Client()
        client.force_login(self.outsider)
        response = client.post(self.url, {"tag": "funny"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SpeakerTag.objects.filter(speaker=self.speaker, user=self.outsider, tag="funny").exists())

    def test_speaker_can_tag_own_profile(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(user=self.outsider)
        client = Client()
        client.force_login(self.outsider)
        response = client.post(self.url, {"tag": "funny"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SpeakerTag.objects.filter(speaker=self.speaker, user=self.outsider, tag="funny").exists())

    def test_signed_out_cannot_tag(self):
        response = Client().post(self.url, {"tag": "funny"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)
        self.assertFalse(SpeakerTag.objects.exists())

    def test_tagging_hidden_speaker_404s(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_NEC)
        client = Client()
        client.force_login(self.outsider)
        self.assertEqual(client.post(self.url, {"tag": "funny"}).status_code, 404)
        self.assertFalse(SpeakerTag.objects.exists())

    def test_remote_chip_rendered_for_signed_in_only(self):
        self.assertNotContains(Client().get(reverse("speaker_list")), 'id="speaker-remote"')
        client = Client()
        client.force_login(self.outsider)
        self.assertContains(client.get(reverse("speaker_list")), 'id="speaker-remote"')

    def test_contactable_speakers_sort_first(self):
        # The claimed speaker is the older record, so only the contactable sort puts it first.
        claimed = Speaker.objects.create(name="Claimed, Clara", user=self.outsider)
        plain = Speaker.objects.create(name="Plain, Pete")
        client = Client()
        client.force_login(self.outsider)
        content = client.get(reverse("speaker_list")).content.decode()
        self.assertLess(
            content.index(reverse("speaker_detail", kwargs={"slug": claimed.slug})),
            content.index(reverse("speaker_detail", kwargs={"slug": plain.slug})),
        )

    def test_claimed_mark_shown(self):
        self.assertNotContains(Client().get(reverse("speaker_list")), 'title="Claimed"')
        Speaker.objects.filter(pk=self.speaker.pk).update(user=self.outsider)
        client = Client()
        client.force_login(self.outsider)
        self.assertContains(client.get(reverse("speaker_list")), 'title="Claimed"')
