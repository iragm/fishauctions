"""Tests for the speaker directory: the NEC WordPress import, NEC-only scoping, the
list/map view, tagging, and comments."""

import io
import tempfile
from io import StringIO
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse

from auctions.management.commands.import_nec_speakers import canonical_topic_name, clean_text
from auctions.models import Club, ClubMember, Speaker, SpeakerComment, SpeakerTag, SpeakerTopic

# A trimmed WXR export with the shapes that actually matter: an entity inside CDATA, the two
# spellings of cichlids the real file has, a bio with a trailing "Programs:" run-on, a speaker
# whose photo is linked by _thumbnail_id, one linked only by post_parent, and a draft to skip.
SAMPLE_WXR = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
<title>NEC</title>
<item>
    <title><![CDATA[Cumberton, Kevin]]></title>
    <link>https://northeastcouncil.org/speaker/cumberton-kevin/</link>
    <content:encoded><![CDATA[Kevin breeds Central &amp; South American cichlids. Programs: Cichlids of Costa Rica]]></content:encoded>
    <wp:post_id>672</wp:post_id>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_type><![CDATA[speaker]]></wp:post_type>
    <category domain="speaker_topics" nicename="cichlids"><![CDATA[CIchlids]]></category>
    <category domain="speaker_topics" nicename="reef"><![CDATA[Reef &amp; Brackish]]></category>
    <wp:postmeta>
        <wp:meta_key><![CDATA[_thumbnail_id]]></wp:meta_key>
        <wp:meta_value><![CDATA[674]]></wp:meta_value>
    </wp:postmeta>
</item>
<item>
    <title><![CDATA[Mous, Esther]]></title>
    <link>https://northeastcouncil.org/speaker/mous-esther/</link>
    <content:encoded><![CDATA[Esther operates Aquaflora in Amsterdam.]]></content:encoded>
    <wp:post_id>673</wp:post_id>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_type><![CDATA[speaker]]></wp:post_type>
    <category domain="speaker_topics" nicename="cichids"><![CDATA[Cichids]]></category>
</item>
<item>
    <title><![CDATA[Draft, Never Published]]></title>
    <content:encoded><![CDATA[Should not be imported.]]></content:encoded>
    <wp:post_id>675</wp:post_id>
    <wp:status><![CDATA[draft]]></wp:status>
    <wp:post_type><![CDATA[speaker]]></wp:post_type>
</item>
<item>
    <title><![CDATA[kevin-photo]]></title>
    <wp:post_id>674</wp:post_id>
    <wp:post_parent>672</wp:post_parent>
    <wp:status><![CDATA[inherit]]></wp:status>
    <wp:post_type><![CDATA[attachment]]></wp:post_type>
    <wp:attachment_url><![CDATA[https://northeastcouncil.org/wp-content/uploads/kevin.jpg]]></wp:attachment_url>
</item>
<item>
    <title><![CDATA[esther-photo]]></title>
    <wp:post_id>676</wp:post_id>
    <wp:post_parent>673</wp:post_parent>
    <wp:status><![CDATA[inherit]]></wp:status>
    <wp:post_type><![CDATA[attachment]]></wp:post_type>
    <wp:attachment_url><![CDATA[https://northeastcouncil.org/wp-content/uploads/esther.jpg]]></wp:attachment_url>
</item>
</channel>
</rss>
"""


def tiny_jpeg():
    """Real JPEG bytes. The image field resizes on save, so fake bytes won't get through."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), (80, 120, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


def write_sample_export():
    """Drop SAMPLE_WXR in a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8")
    handle.write(SAMPLE_WXR)
    handle.close()
    return handle.name


class ImportNecSpeakersTests(TestCase):
    """The WordPress importer."""

    def setUp(self):
        self.path = write_sample_export()

    def run_import(self, *args):
        out = StringIO()
        call_command("import_nec_speakers", self.path, "--skip-images", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_imports_published_speakers_only(self):
        self.run_import()
        self.assertEqual(Speaker.objects.count(), 2)
        self.assertFalse(Speaker.objects.filter(name__startswith="Draft").exists())

    def test_unescapes_entities_inside_cdata(self):
        """WordPress double-escapes, and CDATA means the XML parser leaves `&amp;` alone."""
        self.run_import()
        self.assertTrue(SpeakerTopic.objects.filter(name="Reef & Brackish").exists())
        self.assertIn("Central & South American", Speaker.objects.get(wordpress_post_id=672).bio)

    def test_merges_duplicate_topic_spellings(self):
        """ "CIchlids" and "Cichids" are the same subject and must not become two rows."""
        self.run_import()
        self.assertEqual(SpeakerTopic.objects.filter(name__iexact="cichlids").count(), 1)
        topic = SpeakerTopic.objects.get(name="Cichlids")
        self.assertEqual(topic.speakers.count(), 2)

    def test_splits_the_bio_from_the_programs_list(self):
        self.run_import()
        speaker = Speaker.objects.get(wordpress_post_id=672)
        self.assertEqual(speaker.bio, "Kevin breeds Central & South American cichlids.")
        self.assertEqual(speaker.programs, "Cichlids of Costa Rica")

    def test_speaker_with_no_programs_keeps_the_whole_bio(self):
        self.run_import()
        speaker = Speaker.objects.get(wordpress_post_id=673)
        self.assertEqual(speaker.bio, "Esther operates Aquaflora in Amsterdam.")
        self.assertEqual(speaker.programs, "")

    def test_imported_rows_are_nec_only_and_unattributed(self):
        self.run_import()
        speaker = Speaker.objects.get(wordpress_post_id=672)
        self.assertTrue(speaker.nec_only)
        self.assertTrue(speaker.imported_from_nec)
        self.assertIsNone(speaker.created_by)
        self.assertEqual(speaker.attribution, "Added from the NEC speaker database")

    def test_rerunning_updates_instead_of_duplicating(self):
        self.run_import()
        Speaker.objects.filter(wordpress_post_id=672).update(bio="edited")
        self.run_import()
        self.assertEqual(Speaker.objects.count(), 2)
        self.assertNotEqual(Speaker.objects.get(wordpress_post_id=672).bio, "edited")

    def test_dry_run_writes_nothing(self):
        self.run_import("--dry-run")
        self.assertEqual(Speaker.objects.count(), 0)

    def test_orphan_topics_are_pruned(self):
        orphan = SpeakerTopic.objects.create(name="Nobody Talks About This")
        self.run_import()
        self.assertFalse(SpeakerTopic.objects.filter(pk=orphan.pk).exists())

    @patch("auctions.management.commands.import_nec_speakers.requests.get")
    def test_downloads_photos_by_thumbnail_id_and_by_parent(self, mock_get):
        """Kevin's photo is linked by _thumbnail_id, Esther's only by post_parent."""
        mock_get.return_value.content = tiny_jpeg()
        mock_get.return_value.raise_for_status.return_value = None
        call_command("import_nec_speakers", self.path, stdout=StringIO(), stderr=StringIO())
        fetched = sorted(call.args[0] for call in mock_get.call_args_list)
        self.assertEqual(
            fetched,
            [
                "https://northeastcouncil.org/wp-content/uploads/esther.jpg",
                "https://northeastcouncil.org/wp-content/uploads/kevin.jpg",
            ],
        )
        self.assertTrue(Speaker.objects.get(wordpress_post_id=672).image)
        self.assertTrue(Speaker.objects.get(wordpress_post_id=673).image)

    @patch("auctions.management.commands.import_nec_speakers.requests.get")
    def test_an_unreachable_photo_does_not_abort_the_import(self, mock_get):
        mock_get.side_effect = requests.RequestException("connection reset")
        call_command("import_nec_speakers", self.path, stdout=StringIO(), stderr=StringIO())
        self.assertEqual(Speaker.objects.count(), 2)
        self.assertFalse(Speaker.objects.get(wordpress_post_id=672).image)

    @patch("auctions.management.commands.import_nec_speakers.requests.get")
    def test_a_photo_that_is_not_an_image_does_not_abort_the_import(self, mock_get):
        """A 200 carrying an HTML error page is the realistic version of this."""
        mock_get.return_value.content = b"<html>404 not found</html>"
        mock_get.return_value.raise_for_status.return_value = None
        out = StringIO()
        call_command("import_nec_speakers", self.path, stdout=out, stderr=StringIO())
        self.assertEqual(Speaker.objects.count(), 2)
        self.assertFalse(Speaker.objects.get(wordpress_post_id=672).image)
        self.assertIn("2 failed", out.getvalue())

    def test_clean_text_collapses_nbsp_runs(self):
        self.assertEqual(clean_text("a\xa0\xa0 b\n\nc"), "a b c")

    def test_canonical_topic_name_folds_aliases(self):
        self.assertEqual(canonical_topic_name("CIchlids"), "Cichlids")
        self.assertEqual(canonical_topic_name("Cichids"), "Cichlids")
        self.assertEqual(canonical_topic_name("Africa"), "African")
        self.assertIsNone(canonical_topic_name("   "))


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerAccessTests(TestCase):
    """Who can see the directory at all."""

    def setUp(self):
        self.nec_club = Club.objects.create(name="NEC Club", is_nec_club=True)
        self.other_club = Club.objects.create(name="Ordinary Club", is_nec_club=False)
        self.officer = User.objects.create_user("officer", "officer@example.com", "pw")
        ClubMember.objects.create(club=self.nec_club, user=self.officer, name="Officer", permission_view=True)
        self.plain_member = User.objects.create_user("plain", "plain@example.com", "pw")
        # A member of the NEC club with no permission at all.
        ClubMember.objects.create(club=self.nec_club, user=self.plain_member, name="Plain")
        self.outsider = User.objects.create_user("outsider", "outsider@example.com", "pw")
        ClubMember.objects.create(club=self.other_club, user=self.outsider, name="Outsider", permission_admin=True)
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pw")
        self.speaker = Speaker.objects.create(name="Talker, Terry", nec_only=True)

    def get(self, user, url=None):
        client = Client()
        client.force_login(user)
        return client.get(url or reverse("speaker_list"))

    def test_club_officer_can_see_the_directory(self):
        self.assertEqual(self.get(self.officer).status_code, 200)

    def test_superuser_can_see_the_directory(self):
        self.assertEqual(self.get(self.superuser).status_code, 200)

    def test_member_of_an_nec_club_with_no_permission_is_told_why(self):
        """Membership alone isn't the bar -- "some permission in an NEC club" is."""
        response = self.get(self.plain_member)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Northeast Council", status_code=403)

    def test_admin_of_a_non_nec_club_is_told_why(self):
        response = self.get(self.outsider)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Northeast Council", status_code=403)

    def test_anonymous_users_are_sent_to_log_in(self):
        response = Client().get(reverse("speaker_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_detail_page_is_scoped_too(self):
        url = reverse("speaker_detail", kwargs={"slug": self.speaker.slug})
        self.assertEqual(self.get(self.outsider, url).status_code, 403)
        self.assertEqual(self.get(self.officer, url).status_code, 200)

    def test_any_single_club_permission_is_enough(self):
        """Every permission flag opens the directory, not just permission_view."""
        for field in ("permission_export", "permission_money", "permission_manage_bap", "permission_admin"):
            with self.subTest(field=field):
                user = User.objects.create_user(f"user_{field}", f"{field}@example.com", "pw")
                ClubMember.objects.create(club=self.nec_club, user=user, name=field, **{field: True})
                self.assertEqual(self.get(user).status_code, 200)

    def test_permission_in_someone_elses_row_does_not_count(self):
        """The permission and the user have to be on the same ClubMember row."""
        lurker = User.objects.create_user("lurker", "lurker@example.com", "pw")
        ClubMember.objects.create(club=self.nec_club, user=lurker, name="Lurker")
        ClubMember.objects.create(club=self.nec_club, user=None, name="Someone Else", permission_admin=True)
        self.assertEqual(self.get(lurker).status_code, 403)


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerListTests(TestCase):
    """Filtering, distance, and the map payload."""

    def setUp(self):
        # Providence, RI
        self.club = Club.objects.create(
            name="NEC Club", is_nec_club=True, latitude=41.8240, longitude=-71.4128, location="Providence, RI"
        )
        self.user = User.objects.create_user("officer", "officer@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.user, name="Officer", permission_view=True)
        self.client = Client()
        self.client.force_login(self.user)

        self.cichlids = SpeakerTopic.objects.create(name="Cichlids")
        self.plants = SpeakerTopic.objects.create(name="Plants")
        # ~40 miles from Providence
        self.nearby = Speaker.objects.create(
            name="Boston, Bob", location="Boston, MA", latitude=42.3601, longitude=-71.0589
        )
        self.nearby.topics.add(self.cichlids)
        # ~2500 miles away
        self.faraway = Speaker.objects.create(
            name="Angeles, Los", location="Los Angeles, CA", latitude=34.0522, longitude=-118.2437
        )
        self.faraway.topics.add(self.plants)
        self.unlocated = Speaker.objects.create(name="Nowhere, Nora")
        self.unlocated.topics.add(self.cichlids)

    def list_url(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return reverse("speaker_list") + (f"?{query}" if query else "")

    def names_in(self, response):
        body = response.content.decode()
        return {speaker.display_name for speaker in Speaker.objects.all() if speaker.display_name in body}

    def test_lists_every_speaker_by_default(self):
        response = self.client.get(self.list_url())
        self.assertEqual(self.names_in(response), {"Bob Boston", "Los Angeles", "Nora Nowhere"})

    def test_club_param_sets_the_distance_origin(self):
        response = self.client.get(self.list_url(club=self.club.slug))
        self.assertContains(response, "miles")
        self.assertContains(response, self.club.name)

    def test_speakers_without_coordinates_sort_last(self):
        """A plain ascending sort on a NULL distance would fill page one with them."""
        response = self.client.get(self.list_url(club=self.club.slug))
        body = response.content.decode()
        self.assertLess(body.index("Bob Boston"), body.index("Nora Nowhere"))
        self.assertLess(body.index("Los Angeles"), body.index("Nora Nowhere"))

    def test_distance_filter_excludes_speakers_that_are_too_far(self):
        response = self.client.get(self.list_url(club=self.club.slug, distance=100))
        names = self.names_in(response)
        self.assertIn("Bob Boston", names)
        self.assertNotIn("Los Angeles", names)

    def test_distance_filter_excludes_speakers_with_no_location(self):
        """An unknown location can't be claimed to be nearby."""
        response = self.client.get(self.list_url(club=self.club.slug, distance=5000))
        self.assertNotIn("Nora Nowhere", self.names_in(response))

    def test_distance_filter_is_ignored_without_an_origin(self):
        response = self.client.get(self.list_url(distance=10))
        self.assertEqual(len(self.names_in(response)), 3)

    def test_a_club_the_user_has_no_permission_in_is_not_used_as_the_origin(self):
        stranger_club = Club.objects.create(
            name="Stranger Club", is_nec_club=True, latitude=34.0522, longitude=-118.2437
        )
        response = self.client.get(self.list_url(club=stranger_club.slug))
        self.assertNotContains(response, "Stranger Club")

    def test_falls_back_to_the_users_own_location(self):
        self.user.userdata.latitude = 41.8240
        self.user.userdata.longitude = -71.4128
        self.user.userdata.save()
        response = self.client.get(self.list_url())
        self.assertContains(response, "your location")
        self.assertContains(response, "miles")

    def test_topic_filter(self):
        response = self.client.get(self.list_url(topic=self.cichlids.slug))
        names = self.names_in(response)
        self.assertEqual(names, {"Bob Boston", "Nora Nowhere"})

    def test_text_search_covers_name_bio_and_location(self):
        Speaker.objects.filter(pk=self.faraway.pk).update(bio="An expert on driftwood")
        self.assertIn("Los Angeles", self.names_in(self.client.get(self.list_url(query="driftwood"))))
        self.assertIn("Bob Boston", self.names_in(self.client.get(self.list_url(query="boston"))))

    def test_mapped_keyword_filters_to_speakers_with_coordinates(self):
        names = self.names_in(self.client.get(self.list_url(query="mapped")))
        self.assertNotIn("Nora Nowhere", names)
        self.assertIn("Bob Boston", names)

    def test_tag_keyword_filters_by_tag(self):
        SpeakerTag.objects.create(speaker=self.nearby, user=self.user, tag="remote")
        names = self.names_in(self.client.get(self.list_url(query="remote")))
        self.assertEqual(names, {"Bob Boston"})

    def test_untagged_keyword(self):
        SpeakerTag.objects.create(speaker=self.nearby, user=self.user, tag="remote")
        self.assertNotIn("Bob Boston", self.names_in(self.client.get(self.list_url(query="untagged"))))

    def test_deleted_speakers_never_appear(self):
        Speaker.objects.filter(pk=self.nearby.pk).update(is_deleted=True)
        self.assertNotIn("Bob Boston", self.names_in(self.client.get(self.list_url())))

    def test_map_payload_only_holds_speakers_with_coordinates(self):
        response = self.client.get(self.list_url())
        payload = response.context["speakers_json"]
        self.assertEqual({row["name"] for row in payload}, {"Bob Boston", "Los Angeles"})
        self.assertEqual(response.context["unmapped_count"], 1)

    def test_map_payload_follows_the_filters(self):
        response = self.client.get(self.list_url(topic=self.plants.slug))
        self.assertEqual({row["name"] for row in response.context["speakers_json"]}, {"Los Angeles"})

    def test_a_search_that_finds_nobody_offers_to_add_them(self):
        response = self.client.get(self.list_url(query="Wanda Missing"))
        self.assertContains(response, "Add a speaker")
        self.assertContains(response, "name=Wanda+Missing")

    def test_the_empty_state_does_not_prefill_a_keyword_as_a_name(self):
        response = self.client.get(self.list_url(query="untagged", topic=self.plants.slug))
        self.assertContains(response, "Add a speaker")
        self.assertNotContains(response, "name=untagged")

    def test_suggestions_are_skipped_on_htmx_requests(self):
        """Working them out reads every speaker name; not worth doing per keystroke."""
        ClubMember.objects.create(club=self.club, name="Sally Speaker")
        response = self.client.get(self.list_url(), headers={"hx-request": "true"})
        self.assertEqual(response.context["suggested_members"], [])

    def test_htmx_response_carries_the_map_payload_out_of_band(self):
        """This is what keeps the map in sync when the filter box swaps only the table."""
        response = self.client.get(self.list_url(query="boston"), headers={"hx-request": "true"})
        self.assertContains(response, 'id="speaker-map-payload" hx-swap-oob="true"')
        self.assertNotContains(response, "<html")


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerTaggingTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="NEC Club", is_nec_club=True)
        self.user = User.objects.create_user("officer", "officer@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.user, name="Officer", permission_view=True)
        self.other = User.objects.create_user("other", "other@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.other, name="Other", permission_view=True)
        self.speaker = Speaker.objects.create(name="Talker, Terry")
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("speaker_tag", kwargs={"slug": self.speaker.slug})

    def test_posting_a_tag_adds_it(self):
        response = self.client.post(self.url, {"tag": "engaging"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SpeakerTag.objects.filter(speaker=self.speaker, user=self.user, tag="engaging").exists())

    def test_posting_the_same_tag_again_removes_it(self):
        self.client.post(self.url, {"tag": "engaging"})
        self.client.post(self.url, {"tag": "engaging"})
        self.assertEqual(SpeakerTag.objects.filter(speaker=self.speaker).count(), 0)

    def test_an_unknown_tag_is_rejected(self):
        self.assertEqual(self.client.post(self.url, {"tag": "smells_nice"}).status_code, 404)
        self.assertEqual(SpeakerTag.objects.count(), 0)

    def test_tag_counts_are_per_tag_across_users(self):
        self.client.post(self.url, {"tag": "engaging"})
        other_client = Client()
        other_client.force_login(self.other)
        other_client.post(self.url, {"tag": "engaging"})
        other_client.post(self.url, {"tag": "funny"})
        counts = {value: count for value, _label, _group, count in self.speaker.tag_counts()}
        self.assertEqual(counts, {"engaging": 2, "funny": 1})

    def test_one_user_cannot_stack_the_same_tag(self):
        SpeakerTag.objects.create(speaker=self.speaker, user=self.user, tag="funny")
        # get_or_create in the view, plus a unique constraint underneath it.
        SpeakerTag.objects.get_or_create(speaker=self.speaker, user=self.user, tag="funny")
        self.assertEqual(SpeakerTag.objects.filter(speaker=self.speaker, tag="funny").count(), 1)

    def test_the_panel_shows_tag_counts(self):
        self.client.post(self.url, {"tag": "book_again"})
        response = self.client.get(reverse("speaker_panel", kwargs={"slug": self.speaker.slug}))
        self.assertContains(response, "Would book again")

    def test_every_tag_definition_has_a_group(self):
        groups = dict(SpeakerTag.grouped_definitions())
        self.assertEqual(sum(len(tags) for tags in groups.values()), len(SpeakerTag.TAG_DEFINITIONS))
        self.assertEqual(set(groups), {SpeakerTag.GROUP_TALK, SpeakerTag.GROUP_LOGISTICS})


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerCommentTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="NEC Club", is_nec_club=True)
        self.user = User.objects.create_user("officer", "officer@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.user, name="Officer", permission_view=True)
        self.other = User.objects.create_user("other", "other@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.other, name="Other", permission_view=True)
        self.speaker = Speaker.objects.create(name="Talker, Terry")
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("speaker_comment", kwargs={"slug": self.speaker.slug})

    def test_posting_a_comment(self):
        response = self.client.post(self.url, {"body": "Excellent talk on plecos."})
        self.assertEqual(response.status_code, 200)
        comment = SpeakerComment.objects.get()
        self.assertEqual(comment.body, "Excellent talk on plecos.")
        self.assertEqual(comment.user, self.user)

    def test_a_comment_is_attributed_to_the_users_only_nec_club(self):
        self.client.post(self.url, {"body": "Good stuff"})
        comment = SpeakerComment.objects.get()
        self.assertEqual(comment.club, self.club)
        self.assertIn(self.club.name, comment.author_display)

    def test_an_empty_comment_is_rejected(self):
        self.client.post(self.url, {"body": "   "})
        self.assertEqual(SpeakerComment.objects.count(), 0)

    def test_deleting_your_own_comment(self):
        self.client.post(self.url, {"body": "Mistake"})
        comment = SpeakerComment.objects.get()
        response = self.client.post(
            reverse("speaker_comment_delete", kwargs={"slug": self.speaker.slug, "pk": comment.pk})
        )
        self.assertEqual(response.status_code, 200)
        comment.refresh_from_db()
        self.assertTrue(comment.is_deleted)

    def test_you_cannot_delete_someone_elses_comment(self):
        self.client.post(self.url, {"body": "Mine"})
        comment = SpeakerComment.objects.get()
        other_client = Client()
        other_client.force_login(self.other)
        response = other_client.post(
            reverse("speaker_comment_delete", kwargs={"slug": self.speaker.slug, "pk": comment.pk})
        )
        self.assertEqual(response.status_code, 403)
        comment.refresh_from_db()
        self.assertFalse(comment.is_deleted)

    def test_deleted_comments_are_hidden_from_the_panel(self):
        self.client.post(self.url, {"body": "Gone soon"})
        SpeakerComment.objects.update(is_deleted=True)
        response = self.client.get(reverse("speaker_panel", kwargs={"slug": self.speaker.slug}))
        self.assertNotContains(response, "Gone soon")


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerCreateDeleteTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="NEC Club", is_nec_club=True)
        self.user = User.objects.create_user("officer", "officer@example.com", "pw")
        self.user.first_name = "Ada"
        self.user.last_name = "Officer"
        self.user.save()
        ClubMember.objects.create(club=self.club, user=self.user, name="Ada Officer", permission_view=True)
        self.other = User.objects.create_user("other", "other@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.other, name="Other", permission_view=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_anyone_with_access_can_add_a_speaker_who_has_no_account(self):
        response = self.client.post(
            reverse("speaker_add"),
            {"name": "Nobody, Ned", "bio": "", "programs": "", "new_topics": "", "location": ""},
        )
        self.assertEqual(response.status_code, 302)
        speaker = Speaker.objects.get(name="Nobody, Ned")
        self.assertEqual(speaker.created_by, self.user)

    def test_new_speakers_default_to_not_nec_only(self):
        self.client.post(reverse("speaker_add"), {"name": "Nobody, Ned", "bio": "", "programs": "", "new_topics": ""})
        self.assertFalse(Speaker.objects.get(name="Nobody, Ned").nec_only)

    def test_new_topics_are_created_and_matched_case_insensitively(self):
        SpeakerTopic.objects.create(name="Cichlids")
        self.client.post(
            reverse("speaker_add"),
            {"name": "Nobody, Ned", "bio": "", "programs": "", "new_topics": "cichlids, Biotopes"},
        )
        speaker = Speaker.objects.get(name="Nobody, Ned")
        self.assertEqual(SpeakerTopic.objects.filter(name__iexact="cichlids").count(), 1)
        self.assertEqual(set(speaker.topics.values_list("name", flat=True)), {"Cichlids", "Biotopes"})

    def test_attribution_names_the_user_and_their_club(self):
        self.client.post(
            reverse("speaker_add"),
            {"name": "Nobody, Ned", "bio": "", "programs": "", "new_topics": "", "club": self.club.pk},
        )
        speaker = Speaker.objects.get(name="Nobody, Ned")
        self.assertEqual(speaker.attribution, "Added by Ada Officer (NEC Club)")

    def test_attribution_without_a_club(self):
        speaker = Speaker.objects.create(name="Nobody, Ned", created_by=self.user)
        self.assertEqual(speaker.attribution, "Added by Ada Officer")

    def test_the_creator_can_delete_their_speaker(self):
        speaker = Speaker.objects.create(name="Nobody, Ned", created_by=self.user)
        response = self.client.post(reverse("speaker_delete", kwargs={"slug": speaker.slug}))
        self.assertEqual(response.status_code, 302)
        speaker.refresh_from_db()
        self.assertTrue(speaker.is_deleted)

    def test_someone_else_cannot_delete_it(self):
        speaker = Speaker.objects.create(name="Nobody, Ned", created_by=self.user)
        other_client = Client()
        other_client.force_login(self.other)
        response = other_client.post(reverse("speaker_delete", kwargs={"slug": speaker.slug}))
        self.assertEqual(response.status_code, 403)
        speaker.refresh_from_db()
        self.assertFalse(speaker.is_deleted)

    def test_imported_speakers_cannot_be_deleted_by_ordinary_users(self):
        """Nobody "added" them, so nobody but a superuser owns them."""
        speaker = Speaker.objects.create(name="Imported, Ivy", imported_from_nec=True, created_by=None)
        response = self.client.post(reverse("speaker_delete", kwargs={"slug": speaker.slug}))
        self.assertEqual(response.status_code, 403)

    def test_the_delete_button_only_renders_for_the_creator(self):
        speaker = Speaker.objects.create(name="Nobody, Ned", created_by=self.user)
        url = reverse("speaker_panel", kwargs={"slug": speaker.slug})
        self.assertContains(self.client.get(url), "Delete")
        other_client = Client()
        other_client.force_login(self.other)
        self.assertNotContains(other_client.get(url), "Remove this speaker?")

    def test_editing_is_restricted_to_the_creator(self):
        speaker = Speaker.objects.create(name="Nobody, Ned", created_by=self.user)
        url = reverse("speaker_edit", kwargs={"slug": speaker.slug})
        self.assertEqual(self.client.get(url).status_code, 200)
        other_client = Client()
        other_client.force_login(self.other)
        self.assertEqual(other_client.get(url).status_code, 403)

    def test_add_form_prefills_from_the_club_member_prompt(self):
        response = self.client.get(reverse("speaker_add") + "?name=Jane+Aquarist&email=jane@example.com")
        self.assertEqual(response.context["form"].initial["name"], "Jane Aquarist")
        self.assertEqual(response.context["form"].initial["email"], "jane@example.com")

    def test_club_members_who_are_not_speakers_are_suggested(self):
        ClubMember.objects.create(club=self.club, name="Sally Speaker", email="sally@example.com")
        response = self.client.get(reverse("speaker_list"))
        self.assertIn("Sally Speaker", [member.name for member in response.context["suggested_members"]])
        self.assertContains(response, "Do any of your club members give talks?")

    def test_a_member_already_in_the_directory_is_not_suggested(self):
        """The NEC stores "Speaker, Sally" and the club stores "Sally Speaker" -- same person.

        Checked through the context rather than the page body: the speaker is in the table
        too, so their name appears either way.
        """
        ClubMember.objects.create(club=self.club, name="Sally Speaker")
        Speaker.objects.create(name="Speaker, Sally")
        response = self.client.get(reverse("speaker_list"))
        suggested = [member.name for member in response.context["suggested_members"]]
        self.assertNotIn("Sally Speaker", suggested)
        # The club's other members are still fair game, so this isn't just an empty list.
        self.assertIn("Ada Officer", suggested)


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerModelTests(TestCase):
    def test_display_name_reverses_last_comma_first(self):
        self.assertEqual(Speaker(name="Cumberton, Kevin").display_name, "Kevin Cumberton")

    def test_display_name_leaves_other_shapes_alone(self):
        self.assertEqual(Speaker(name="Jungle Bob").display_name, "Jungle Bob")
        self.assertEqual(Speaker(name="McKeighen, Jr., Ken").display_name, "McKeighen, Jr., Ken")

    def test_nec_only_speakers_are_hidden_from_non_nec_viewers(self):
        """Nothing exercises this path yet, but it's the whole point of the flag."""
        from auctions.views import NECSpeakerAccessMixin

        Speaker.objects.create(name="Private, Pat", nec_only=True)
        Speaker.objects.create(name="Public, Paula", nec_only=False)

        class FakeView(NECSpeakerAccessMixin):
            nec_clubs = []

            class request:
                class user:
                    is_superuser = False

        names = set(FakeView().visible_speakers().values_list("name", flat=True))
        self.assertEqual(names, {"Public, Paula"})


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerPaletteRouteTests(TestCase):
    """The command palette's speaker scope, including its access rule."""

    def setUp(self):
        from django.test import RequestFactory

        from auctions import palette_routes

        self.palette_routes = palette_routes
        self.factory = RequestFactory()
        self.club = Club.objects.create(name="NEC Club", is_nec_club=True)
        self.officer = User.objects.create_user("officer", "officer@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=self.officer, name="Officer", permission_view=True)
        self.outsider = User.objects.create_user("outsider", "outsider@example.com", "pw")
        self.speaker = Speaker.objects.create(name="O'Leary, Rachel", nec_only=True)
        self.route = palette_routes.ROUTES["speaker_detail"]

    def resolve(self, user, target):
        request = self.factory.get("/")
        request.user = user
        request.palette_page = None
        return self.palette_routes.resolve_route(request, self.route, {"target": target})

    def test_finds_a_speaker_by_their_spoken_name(self):
        """The directory stores "O'Leary, Rachel"; people say "Rachel O'Leary"."""
        result = self.resolve(self.officer, "Rachel O'Leary")
        self.assertEqual(result.get("url"), self.speaker.get_absolute_url())

    def test_finds_a_speaker_by_last_name(self):
        self.assertEqual(self.resolve(self.officer, "O'Leary").get("url"), self.speaker.get_absolute_url())

    def test_an_unknown_name_is_an_error_not_a_wrong_page(self):
        self.assertIn("error", self.resolve(self.officer, "Nobody At All"))

    def test_the_nec_rule_holds_in_the_palette_too(self):
        """Otherwise the palette would confirm an NEC speaker exists to someone who can't open them."""
        result = self.resolve(self.outsider, "Rachel O'Leary")
        self.assertIn("error", result)
        self.assertNotIn("url", result)

    def test_a_speaker_who_is_not_nec_only_is_reachable_by_anyone(self):
        public = Speaker.objects.create(name="Public, Paula", nec_only=False)
        self.assertEqual(self.resolve(self.outsider, "Paula Public").get("url"), public.get_absolute_url())

    def test_a_deleted_speaker_is_not_reachable(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(is_deleted=True)
        self.assertIn("error", self.resolve(self.officer, "Rachel O'Leary"))


@override_settings(SINGLE_CLUB_MODE=False)
class SpeakerNavigationTests(TestCase):
    """The club sidebar link only exists for NEC clubs."""

    def setUp(self):
        self.user = User.objects.create_user("officer", "officer@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def sidebar_for(self, is_nec_club):
        club = Club.objects.create(name="A Club", is_nec_club=is_nec_club)
        ClubMember.objects.create(club=club, user=self.user, name="Officer", permission_admin=True)
        return self.client.get(reverse("club_admin", kwargs={"slug": club.slug}))

    def test_nec_clubs_get_a_speakers_link(self):
        response = self.sidebar_for(is_nec_club=True)
        self.assertContains(response, reverse("speaker_list"))

    def test_other_clubs_do_not(self):
        response = self.sidebar_for(is_nec_club=False)
        self.assertNotContains(response, reverse("speaker_list"))
