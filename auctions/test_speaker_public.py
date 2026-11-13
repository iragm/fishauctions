"""The public speaker directory: adding yourself, "This is me" claims, what search engines see, and
finding and merging duplicate speakers (``auctions.speaker_duplicates`` and the admin connector's tools).
"""

import json

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.test.client import Client
from django.urls import reverse

from auctions.models import Club, ClubMember, Speaker, SpeakerClaim, SpeakerComment, SpeakerTag, SpeakerTopic
from auctions.speaker_duplicates import (
    duplicate_groups,
    link_speaker_to_account,
    merge_problem,
    merge_speakers,
    name_key,
    possible_matches,
)


class SelfAddTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "talker", "talker@example.com", "pw", first_name="Terry", last_name="Talker"
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("speaker_add_self")

    def payload(self, **extra):
        payload = {"name": "Terry Talker", "location": "Providence, RI", "visibility": Speaker.VISIBILITY_PUBLIC}
        payload.update(extra)
        return payload

    def test_anyone_signed_in_can_add_themselves(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 302)
        speaker = Speaker.objects.get(name="Terry Talker")
        self.assertEqual(speaker.user, self.user)
        self.assertEqual(speaker.created_by, self.user)
        self.assertIsNone(speaker.club)

    def test_the_form_starts_from_the_account(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'value="Terry Talker"')
        self.assertContains(response, 'value="talker@example.com"')

    def test_every_visibility_can_be_chosen(self):
        for value, _label in Speaker.VISIBILITY_CHOICES:
            with self.subTest(value=value):
                Speaker.objects.filter(user=self.user).delete()
                self.client.post(self.url, self.payload(visibility=value))
                self.assertEqual(Speaker.objects.get(user=self.user).visibility, value)

    def test_the_form_speaks_to_you(self):
        body = self.client.get(self.url).content.decode()
        for wording in ("about yourself", "The talks you offer.", "everything you talk about", "where you travel from"):
            self.assertIn(wording, body)
        for wording in ("they talk about", "where they travel from", "The talks they offer.", "about the speaker"):
            self.assertNotIn(wording, body)

    def test_a_signed_in_only_page_sends_a_visitor_to_sign_in(self):
        speaker = Speaker.objects.create(name="Talker, Terry", visibility=Speaker.VISIBILITY_SIGNED_IN)
        url = speaker.get_absolute_url()
        response = Client().get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response["Location"])
        self.assertNotIn(speaker.slug, Client().get(reverse("speaker_list")).content.decode())
        panel = reverse("speaker_panel", kwargs={"slug": speaker.slug})
        self.assertEqual(Client().get(panel).status_code, 404)
        old = Speaker.objects.create(name="Old, Terry", is_deleted=True, merged_into=speaker)
        moved = Client().get(old.get_absolute_url())
        self.assertEqual(moved.status_code, 302)
        self.assertIn(reverse("account_login"), moved["Location"])
        self.assertIn(speaker.slug, moved["Location"])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")

    def test_somebody_with_a_profile_is_sent_to_edit_it(self):
        speaker = Speaker.objects.create(name="Terry Talker", user=self.user)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("speaker_edit", kwargs={"slug": speaker.slug}))
        self.client.post(self.url, self.payload(name="Second Me"))
        self.assertFalse(Speaker.objects.filter(name="Second Me").exists())

    def test_the_person_can_edit_and_delete_their_own_profile_without_an_nec_club(self):
        speaker = Speaker.objects.create(name="Terry Talker", user=self.user)
        edit = reverse("speaker_edit", kwargs={"slug": speaker.slug})
        self.assertEqual(self.client.get(edit).status_code, 200)
        self.client.post(edit, self.payload(bio="Fish talks."))
        speaker.refresh_from_db()
        self.assertEqual(speaker.bio, "Fish talks.")
        self.client.post(reverse("speaker_delete", kwargs={"slug": speaker.slug}))
        speaker.refresh_from_db()
        self.assertTrue(speaker.is_deleted)

    def test_a_matching_name_offers_to_claim_it(self):
        listed = Speaker.objects.create(name="Talker, Terry", imported_from_nec=True)
        response = self.client.get(self.url)
        self.assertContains(response, "You may already be in here")
        self.assertContains(response, reverse("speaker_claim", kwargs={"slug": listed.slug}))

    def test_a_page_somebody_already_owns_is_not_offered(self):
        other = User.objects.create_user("other", "other@example.com", "pw")
        Speaker.objects.create(name="Talker, Terry", user=other)
        self.assertNotContains(self.client.get(self.url), "You may already be in here")

    def test_a_hidden_match_is_not_revealed(self):
        Speaker.objects.create(name="Talker, Terry", visibility=Speaker.VISIBILITY_HIDDEN)
        self.assertNotContains(self.client.get(self.url), "You may already be in here")

    def test_the_list_offers_add_yourself_and_then_your_profile(self):
        list_url = reverse("speaker_list")
        self.assertContains(self.client.get(list_url), self.url)
        self.assertNotContains(self.client.get(list_url), reverse("speaker_add") + '"')
        speaker = Speaker.objects.create(name="Terry Talker", user=self.user)
        self.assertContains(self.client.get(list_url), "Your speaker profile")
        self.assertContains(self.client.get(list_url), speaker.get_absolute_url())


class ClaimTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("claimer", "claimer@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)
        self.speaker = Speaker.objects.create(name="Talker, Terry", imported_from_nec=True, bio="Gives talks.")
        self.url = reverse("speaker_claim", kwargs={"slug": self.speaker.slug})
        self.page = reverse("speaker_detail", kwargs={"slug": self.speaker.slug})

    def test_the_button_shows_on_an_unowned_page(self):
        self.assertContains(self.client.get(self.page), "This is me")

    def test_signed_out_it_goes_to_sign_in_and_back(self):
        response = Client().get(self.page)
        self.assertContains(response, "This is me")
        self.assertContains(response, reverse("account_login") + "?next=")

    def test_pressing_it_only_records_a_request(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, self.page)
        claim = SpeakerClaim.objects.get()
        self.assertEqual((claim.speaker, claim.user, claim.status), (self.speaker, self.user, SpeakerClaim.STATUS_OPEN))
        self.speaker.refresh_from_db()
        self.assertIsNone(self.speaker.user)
        self.assertContains(self.client.get(self.page), "checking your request")

    def test_pressing_it_twice_is_one_request(self):
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertEqual(SpeakerClaim.objects.count(), 1)

    def test_a_page_with_an_owner_cannot_be_claimed(self):
        owner = User.objects.create_user("owner", "owner@example.com", "pw")
        Speaker.objects.filter(pk=self.speaker.pk).update(user=owner)
        self.assertNotContains(self.client.get(self.page), "This is me")
        self.client.post(self.url)
        self.assertFalse(SpeakerClaim.objects.exists())

    def test_a_page_you_cannot_see_cannot_be_claimed(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_HIDDEN)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertFalse(SpeakerClaim.objects.exists())

    def test_a_get_writes_nothing(self):
        self.client.get(self.url)
        self.assertFalse(SpeakerClaim.objects.exists())

    def test_approving_links_the_page_and_folds_in_their_own_profile(self):
        own = Speaker.objects.create(name="Terry Talker", user=self.user, bio="My own words.", phone="555")
        SpeakerClaim.objects.create(speaker=self.speaker, user=self.user)
        link_speaker_to_account(self.speaker, self.user)
        self.speaker.refresh_from_db()
        own.refresh_from_db()
        self.assertEqual(self.speaker.user, self.user)
        self.assertEqual(self.speaker.bio, "My own words.")
        self.assertEqual(self.speaker.phone, "555")
        self.assertTrue(own.is_deleted)
        self.assertEqual(own.merged_into, self.speaker)
        self.assertEqual(SpeakerClaim.objects.get().status, SpeakerClaim.STATUS_APPROVED)
        self.assertTrue(self.speaker.can_be_deleted_by(self.user))


class SearchEngineTests(TestCase):
    def setUp(self):
        self.speaker = Speaker.objects.create(
            name="Talker, Terry",
            bio="Terry keeps <killifish> & talks about them.",
            location="Providence, RI",
            email="terry@example.com",
            phone="401-555-0100",
        )
        self.page = reverse("speaker_detail", kwargs={"slug": self.speaker.slug})

    def test_a_public_page_has_the_seo_treatment(self):
        response = Client().get(self.page)
        content = response.content.decode()
        self.assertIn('<link rel="canonical" href="http://testserver' + self.page + '">', content)
        self.assertIn('property="og:title" content="Terry Talker"', content)
        self.assertIn("<h1", content)
        self.assertIn("Terry Talker - aquarium club speaker", content)
        self.assertNotIn("X-Robots-Tag", response.headers)
        start = content.index('<script type="application/ld+json">') + len('<script type="application/ld+json">')
        data = json.loads(content[start : content.index("</script>", start)])
        self.assertEqual(data["@type"], "Person")
        self.assertEqual(data["name"], "Terry Talker")
        self.assertIn("<killifish>", data["description"])
        # Escaped, so a bio can't close the script tag.
        self.assertNotIn("<killifish>", content[start : content.index("</script>", start)])

    def test_contact_details_are_for_people_signed_in(self):
        self.assertNotContains(Client().get(self.page), "terry@example.com")
        self.assertNotContains(Client().get(self.page), "401-555-0100")
        user = User.objects.create_user("reader", "reader@example.com", "pw")
        # A page the person listed themselves (one a club typed in stays with NEC clubs).
        Speaker.objects.filter(pk=self.speaker.pk).update(user=User.objects.create_user("terry", "t@example.com", "pw"))
        client = Client()
        client.force_login(user)
        self.assertContains(client.get(self.page), "terry@example.com")

    def test_who_added_a_speaker_is_not_public(self):
        adder = User.objects.create_user("adder", "adder@example.com", "pw", first_name="Ada", last_name="Adder")
        Speaker.objects.filter(pk=self.speaker.pk).update(created_by=adder)
        self.assertNotContains(Client().get(self.page), "Ada Adder")

    def test_every_other_visibility_is_kept_out_of_search_engines(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_LINK)
        self.speaker.refresh_from_db()
        response = Client().get(self.speaker.share_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertNotContains(response, 'rel="canonical"')
        self.assertNotContains(response, "application/ld+json")

    def test_the_panel_fragment_is_never_indexed(self):
        response = Client().get(reverse("speaker_panel", kwargs={"slug": self.speaker.slug}))
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex")

    @override_settings(ALLOW_SEARCH_INDEXING=True)
    def test_the_sitemap_lists_public_speakers_only(self):
        hidden = Speaker.objects.create(name="Hidden, Hal", visibility=Speaker.VISIBILITY_NEC)
        body = Client().get("/sitemap.xml").content.decode()
        self.assertIn(self.page, body)
        self.assertIn(reverse("speaker_list"), body)
        self.assertNotIn(hidden.get_absolute_url(), body)

    def test_a_merged_duplicates_address_redirects_permanently(self):
        duplicate = Speaker.objects.create(name="Terry Talker")
        merge_speakers(duplicate, self.speaker)
        response = Client().get(reverse("speaker_detail", kwargs={"slug": duplicate.slug}))
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.url, self.page)

    def test_a_merge_does_not_redirect_into_a_page_you_cannot_see(self):
        duplicate = Speaker.objects.create(name="Terry Talker")
        merge_speakers(duplicate, self.speaker)
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_HIDDEN)
        response = Client().get(reverse("speaker_detail", kwargs={"slug": duplicate.slug}))
        self.assertEqual(response.status_code, 404)

    def test_tags_are_not_a_public_search(self):
        tagger = User.objects.create_user("tagger", "tagger@example.com", "pw")
        SpeakerTag.objects.create(speaker=self.speaker, user=tagger, tag="book_again")
        Speaker.objects.create(name="Other, Olive")
        response = Client().get(reverse("speaker_list") + "?query=recommended")
        self.assertNotContains(response, "Terry Talker")


class DuplicateTests(TestCase):
    def test_name_key_folds_the_two_spellings(self):
        self.assertEqual(name_key("O'Leary, Rachel"), name_key("Rachel O Leary"))
        self.assertEqual(name_key("Dr. José  Núñez"), "jose nunez")
        self.assertEqual(name_key("Smith, John Q."), "john smith")

    def test_possible_matches(self):
        imported = Speaker.objects.create(name="Smith, John")
        Speaker.objects.create(name="Smith, Jane")
        self.assertEqual(possible_matches("John Smith"), [imported])
        self.assertEqual(possible_matches(""), [])

    def test_pairs_and_why(self):
        a = Speaker.objects.create(name="Smith, John")
        b = Speaker.objects.create(name="John Smith")
        c = Speaker.objects.create(name="Jon Smith", website="https://www.fish.example/")
        d = Speaker.objects.create(name="Somebody Else", website="http://fish.example")
        Speaker.objects.create(name="Gone Smith", is_deleted=True)
        pairs = {(x.pk, y.pk): reasons for x, y, reasons in duplicate_groups()}
        self.assertIn("same name", pairs[(a.pk, b.pk)])
        self.assertIn("similar name", pairs[(a.pk, c.pk)])
        self.assertIn("same website", pairs[(c.pk, d.pk)])
        self.assertNotIn((a.pk, d.pk), pairs)

    def test_a_merge_fills_blanks_and_moves_everything(self):
        club = Club.objects.create(name="NEC Club", is_nec_club=True)
        tagger = User.objects.create_user("tagger", "tagger@example.com", "pw")
        topic = SpeakerTopic.objects.create(name="Killifish test")
        kept = Speaker.objects.create(name="Smith, John", bio="NEC bio", imported_from_nec=True)
        closed = Speaker.objects.create(name="John Smith", bio="Other bio", phone="555", club=club)
        closed.topics.add(topic)
        SpeakerTag.objects.create(speaker=closed, user=tagger, tag="funny")
        SpeakerTag.objects.create(speaker=kept, user=tagger, tag="funny")
        SpeakerTag.objects.create(speaker=closed, user=tagger, tag="visuals")
        SpeakerComment.objects.create(speaker=closed, user=tagger, body="Great talk")
        merge_speakers(closed, kept)
        kept.refresh_from_db()
        closed.refresh_from_db()
        self.assertEqual(kept.bio, "NEC bio")
        self.assertEqual(kept.phone, "555")
        self.assertEqual(kept.club, club)
        self.assertEqual(list(kept.topics.all()), [topic])
        self.assertEqual(set(kept.tags.values_list("tag", flat=True)), {"funny", "visuals"})
        self.assertEqual(kept.comments.count(), 1)
        self.assertTrue(closed.is_deleted)
        self.assertEqual(closed.merged_into, kept)

    def test_two_accounts_pages_are_never_merged(self):
        one = User.objects.create_user("one", "one@example.com", "pw")
        two = User.objects.create_user("two", "two@example.com", "pw")
        a = Speaker.objects.create(name="John Smith", user=one)
        b = Speaker.objects.create(name="John Smith", user=two)
        self.assertIn("different site accounts", merge_problem(a, b))
        self.assertIn("same speaker", merge_problem(a, a))

    def test_a_chain_of_merges_ends_at_the_live_page(self):
        a = Speaker.objects.create(name="John Smith")
        b = Speaker.objects.create(name="Smith, John")
        c = Speaker.objects.create(name="J Smith")
        merge_speakers(a, b)
        merge_speakers(b, c)
        a.refresh_from_db()
        self.assertEqual(a.merged_into, c)
        response = Client().get(reverse("speaker_detail", kwargs={"slug": a.slug}))
        self.assertEqual(response.url, c.get_absolute_url())


class AdminToolTests(TestCase):
    """The resolvers behind find_duplicate_speakers and the three approval-only steps."""

    def setUp(self):
        from auctions.mcp import admin

        self.admin = admin
        self.owner = User.objects.create_superuser("owner", "owner@example.com", "pw")
        self.claimer = User.objects.create_user("claimer", "claimer@example.com", "pw")
        self.request = RequestFactory().get("/")
        self.request.user = self.owner

    def test_finds_claims_and_pairs_with_the_step_to_propose(self):
        a = Speaker.objects.create(name="Smith, John", imported_from_nec=True, email="john@example.com")
        b = Speaker.objects.create(name="John Smith")
        SpeakerClaim.objects.create(speaker=a, user=self.claimer)
        result = self.admin.find_duplicate_speakers(self.request, {})
        text = json.dumps(result)
        self.assertNotIn("john@example.com", text)
        self.assertNotIn("claimer@example.com", text)
        self.assertIn('"tool": "link_speaker_to_account"', text)
        self.assertIn('"keep": "' + a.slug + '", "close": "' + b.slug + '"', text)

    def test_it_is_for_superusers_only(self):
        from django.core.exceptions import PermissionDenied

        self.request.user = self.claimer
        with self.assertRaises(PermissionDenied):
            self.admin.find_duplicate_speakers(self.request, {})
        with self.assertRaises(PermissionDenied):
            self.admin.merge_speakers(self.request, {"keep": "a", "close": "b"})

    def test_the_steps_are_approval_only_and_checked(self):
        for name in ("merge_speakers", "link_speaker_to_account", "decline_speaker_claim"):
            self.assertIn(name, self.admin.APPROVAL_ONLY)
            self.assertIn(name, self.admin.PROPOSAL_CHECKS)
        self.assertIn("find_duplicate_speakers", self.admin.ADMIN_TOOLS)
        speaker = Speaker.objects.create(name="Smith, John")
        check = self.admin.PROPOSAL_CHECKS["link_speaker_to_account"]
        self.assertIn("hasn't asked", check({"speaker": speaker.slug, "account": self.claimer.pk}))
        SpeakerClaim.objects.create(speaker=speaker, user=self.claimer)
        self.assertEqual(check({"speaker": speaker.slug, "account": self.claimer.pk}), "")
        self.assertIn("no live speaker", self.admin.PROPOSAL_CHECKS["merge_speakers"]({"keep": "x", "close": "y"}))

    def test_approved_steps_do_their_job(self):
        speaker = Speaker.objects.create(name="Smith, John")
        SpeakerClaim.objects.create(speaker=speaker, user=self.claimer)
        self.admin.link_speaker_to_account(self.request, {"speaker": speaker.slug, "account": self.claimer.pk})
        speaker.refresh_from_db()
        self.assertEqual(speaker.user, self.claimer)
        other = Speaker.objects.create(name="Jones, Jo")
        SpeakerClaim.objects.create(speaker=other, user=self.claimer)
        self.admin.decline_speaker_claim(self.request, {"speaker": other.slug, "account": self.claimer.username})
        self.assertEqual(SpeakerClaim.objects.get(speaker=other).status, SpeakerClaim.STATUS_DECLINED)
        other.refresh_from_db()
        self.assertIsNone(other.user)

    def test_a_staff_account_is_not_linked_by_proposal(self):
        speaker = Speaker.objects.create(name="Smith, John")
        SpeakerClaim.objects.create(speaker=speaker, user=self.owner)
        problem = self.admin.PROPOSAL_CHECKS["link_speaker_to_account"](
            {"speaker": speaker.slug, "account": self.owner.pk}
        )
        self.assertIn("Staff", problem)


class NecMemberStillSeesTheirPartsTests(TestCase):
    def test_an_nec_officer_sees_tags_attribution_and_add_a_speaker(self):
        club = Club.objects.create(name="NEC Club", is_nec_club=True)
        officer = User.objects.create_user(
            "officer", "officer@example.com", "pw", first_name="Ada", last_name="Officer"
        )
        ClubMember.objects.create(club=club, user=officer, name="Ada Officer", permission_admin=True)
        speaker = Speaker.objects.create(name="Talker, Terry", created_by=officer, club=club)
        client = Client()
        client.force_login(officer)
        page = client.get(speaker.get_absolute_url())
        self.assertContains(page, "Added by Ada Officer (NEC Club)")
        self.assertContains(page, "Tags")
        self.assertContains(client.get(reverse("speaker_list")), reverse("speaker_add"))


class ReviewFindingTests(TestCase):
    """Holes the adversarial review found."""

    def setUp(self):
        self.officer = User.objects.create_user("officer", "officer@example.com", "pw")
        self.person = User.objects.create_user("person", "person@example.com", "pw")

    def test_a_merge_does_not_hand_an_imported_page_to_whoever_typed_the_duplicate(self):
        kept = Speaker.objects.create(name="Smith, John", imported_from_nec=True)
        closed = Speaker.objects.create(name="John Smith", created_by=self.officer)
        merge_speakers(closed, kept)
        kept.refresh_from_db()
        self.assertIsNone(kept.created_by)
        self.assertFalse(kept.can_be_deleted_by(self.officer))

    def test_whoever_typed_somebody_in_loses_the_page_once_it_is_theirs(self):
        speaker = Speaker.objects.create(name="Jane Doe", created_by=self.officer)
        self.assertTrue(speaker.can_be_deleted_by(self.officer))
        Speaker.objects.filter(pk=speaker.pk).update(user=self.person)
        speaker.refresh_from_db()
        self.assertFalse(speaker.can_be_deleted_by(self.officer))
        self.assertTrue(speaker.can_be_deleted_by(self.person))

    def test_deleting_the_account_takes_down_the_self_listing(self):
        from auctions.account_deletion import delete_account

        own = Speaker.objects.create(name="Jane Doe", user=self.person, created_by=self.person, email="j@example.com")
        claimed = Speaker.objects.create(name="Doe, Janet", imported_from_nec=True, user=self.person, phone="555")
        delete_account(self.person)
        own.refresh_from_db()
        claimed.refresh_from_db()
        self.assertTrue(own.is_deleted)
        self.assertEqual(own.email, "")
        self.assertFalse(claimed.is_deleted)
        self.assertIsNone(claimed.user)
        self.assertEqual(claimed.phone, "")

    def test_contact_details_typed_in_by_a_club_stay_with_nec_clubs(self):
        speaker = Speaker.objects.create(name="Jane Doe", created_by=self.officer, email="jane@example.com")
        client = Client()
        client.force_login(self.person)
        self.assertNotContains(client.get(speaker.get_absolute_url()), "jane@example.com")
        Speaker.objects.filter(pk=speaker.pk).update(user=self.person)
        stranger = User.objects.create_user("stranger", "stranger@example.com", "pw")
        client.force_login(stranger)
        self.assertContains(client.get(speaker.get_absolute_url()), "jane@example.com")

    def test_the_sign_in_link_never_carries_a_share_key(self):
        speaker = Speaker.objects.create(name="Jane Doe", visibility=Speaker.VISIBILITY_LINK, email="j@example.com")
        response = Client().get(speaker.share_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "key%3D")

    def test_a_merged_panel_redirects_to_the_kept_panel(self):
        kept = Speaker.objects.create(name="Smith, John")
        closed = Speaker.objects.create(name="John Smith")
        merge_speakers(closed, kept)
        response = Client().get(reverse("speaker_panel", kwargs={"slug": closed.slug}))
        self.assertEqual(response.url, reverse("speaker_panel", kwargs={"slug": kept.slug}))

    def test_a_declined_claim_is_not_reopened(self):
        speaker = Speaker.objects.create(name="Jane Doe")
        SpeakerClaim.objects.create(speaker=speaker, user=self.person, status=SpeakerClaim.STATUS_DECLINED)
        client = Client()
        client.force_login(self.person)
        client.post(reverse("speaker_claim", kwargs={"slug": speaker.slug}))
        self.assertEqual(SpeakerClaim.objects.get().status, SpeakerClaim.STATUS_DECLINED)

    def test_a_merge_carries_the_nec_credit_and_the_persons_photo(self):
        kept = Speaker.objects.create(name="John Smith", user=self.person, url="https://example.com/me.jpg")
        closed = Speaker.objects.create(
            name="Smith, John",
            imported_from_nec=True,
            source_url="https://northeastcouncil.org/x/",
            wordpress_post_id=99,
        )
        merge_speakers(closed, kept)
        kept.refresh_from_db()
        self.assertTrue(kept.imported_from_nec)
        self.assertEqual(kept.wordpress_post_id, 99)
        self.assertEqual(kept.url, "https://example.com/me.jpg")


class ClubAdminsAddSpeakersTests(TestCase):
    def test_an_admin_of_any_club_adds_somebody_and_their_club_is_recorded(self):
        club = Club.objects.create(name="Ordinary Club", is_nec_club=False)
        admin = User.objects.create_user("clubadmin", "clubadmin@example.com", "pw")
        ClubMember.objects.create(club=club, user=admin, name="Club Admin", permission_admin=True)
        client = Client()
        client.force_login(admin)
        self.assertContains(client.get(reverse("speaker_list")), reverse("speaker_add") + '"')
        client.post(reverse("speaker_add"), {"name": "Guest, Gil", "location": "Boston, MA"})
        speaker = Speaker.objects.get(name="Guest, Gil")
        self.assertEqual((speaker.created_by, speaker.club), (admin, club))

    def test_a_deleted_admin_row_does_not_count(self):
        club = Club.objects.create(name="Ordinary Club")
        former = User.objects.create_user("former", "former@example.com", "pw")
        ClubMember.objects.create(club=club, user=former, name="Former", permission_admin=True, is_deleted=True)
        client = Client()
        client.force_login(former)
        self.assertEqual(client.get(reverse("speaker_add")).status_code, 403)

    def test_tags_show_in_the_list_for_anyone_signed_in(self):
        speaker = Speaker.objects.create(name="Talker, Terry")
        tagger = User.objects.create_user("tagger", "tagger@example.com", "pw")
        SpeakerTag.objects.create(speaker=speaker, user=tagger, tag="funny")
        reader = User.objects.create_user("reader", "reader@example.com", "pw")
        client = Client()
        client.force_login(reader)
        self.assertContains(client.get(reverse("speaker_list")), "Funny")
        self.assertNotContains(Client().get(reverse("speaker_list")), "Funny")


class SignedInReadersTests(TestCase):
    """What the second adversarial review found about notes readable by anyone signed in."""

    def setUp(self):
        self.nec_club = Club.objects.create(name="NEC Club", is_nec_club=True)
        self.officer = User.objects.create_user("officer", "o@example.com", "pw", first_name="Ada", last_name="Officer")
        self.row = ClubMember.objects.create(club=self.nec_club, user=self.officer, name="Ada", permission_view=True)
        self.reader = User.objects.create_user("reader", "r@example.com", "pw")
        self.speaker = Speaker.objects.create(name="Talker, Terry")
        self.comment = SpeakerComment.objects.create(speaker=self.speaker, user=self.officer, body="Great talk")

    def page_for(self, user):
        client = Client()
        client.force_login(user)
        return client.get(self.speaker.get_absolute_url())

    def test_every_reader_sees_the_writers_username_not_their_name(self):
        for user in (self.reader, self.officer):
            with self.subTest(user=user.username):
                page = self.page_for(user)
                self.assertContains(page, "Great talk")
                self.assertNotContains(page, "Ada Officer")
                self.assertContains(page, ">officer &middot;")

    def test_an_email_shaped_username_is_never_shown(self):
        User.objects.filter(pk=self.officer.pk).update(username="ada@example.com")
        page = self.page_for(self.reader)
        self.assertNotContains(page, "ada@example.com")
        self.assertContains(page, "A member &middot;")

    def test_a_deactivated_writer_reads_as_deleted(self):
        User.objects.filter(pk=self.officer.pk).update(is_active=False)
        self.assertContains(self.page_for(self.reader), "Deleted user &middot;")

    def test_anyone_signed_in_can_comment_but_not_tag(self):
        client = Client()
        client.force_login(self.reader)
        response = client.post(
            reverse("speaker_comment", kwargs={"slug": self.speaker.slug}), {"body": "Saw him in May"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SpeakerComment.objects.filter(user=self.reader, body="Saw him in May").exists())
        response = client.post(reverse("speaker_tag", kwargs={"slug": self.speaker.slug}), {"tag": "funny"})
        self.assertEqual(response.status_code, 403)

    def test_nobody_comments_on_a_page_that_hides_comments(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_LINK)
        client = Client()
        client.force_login(self.reader)
        response = client.post(reverse("speaker_comment", kwargs={"slug": self.speaker.slug}), {"body": "x"})
        self.assertEqual(response.status_code, 404)

    def test_an_nec_only_page_shows_its_notes_only_to_nec_clubs(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_NEC, user=self.reader)
        page = self.page_for(self.reader)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Great talk")

    def test_a_former_officer_can_still_delete_their_own_comment(self):
        self.row.delete()
        client = Client()
        client.force_login(self.officer)
        self.assertContains(client.get(self.speaker.get_absolute_url()), "Delete this comment?")
        client.post(reverse("speaker_comment_delete", kwargs={"slug": self.speaker.slug, "pk": self.comment.pk}))
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)

    def test_nobody_else_can_delete_it(self):
        client = Client()
        client.force_login(self.reader)
        response = client.post(
            reverse("speaker_comment_delete", kwargs={"slug": self.speaker.slug, "pk": self.comment.pk})
        )
        self.assertEqual(response.status_code, 403)
        self.comment.refresh_from_db()
        self.assertFalse(self.comment.is_deleted)

    def test_a_club_named_in_the_link_that_they_dont_admin_records_no_club(self):
        admin_club = Club.objects.create(name="Admin Club")
        ClubMember.objects.create(club=admin_club, user=self.officer, name="Ada", permission_admin=True)
        client = Client()
        client.force_login(self.officer)
        client.post(f"{reverse('speaker_add')}?club={self.nec_club.slug}", {"name": "Guest, Gil", "location": "Boston"})
        self.assertIsNone(Speaker.objects.get(name="Guest, Gil").club)

    def test_whoever_typed_in_contact_details_still_sees_them(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(created_by=self.reader, email="terry@example.com")
        self.assertContains(self.page_for(self.reader), "terry@example.com")

    def test_a_speaker_cant_comment_on_their_own_page(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(user=self.reader)
        client = Client()
        client.force_login(self.reader)
        response = client.post(reverse("speaker_comment", kwargs={"slug": self.speaker.slug}), {"body": "I'm great"})
        self.assertContains(response, "Comments are for other people")
        self.assertFalse(SpeakerComment.objects.filter(user=self.reader).exists())

    def test_comments_are_limited_per_hour(self):
        for number in range(SpeakerComment.HOURLY_LIMIT):
            SpeakerComment.objects.create(speaker=self.speaker, user=self.reader, body=f"note {number}")
        client = Client()
        client.force_login(self.reader)
        response = client.post(reverse("speaker_comment", kwargs={"slug": self.speaker.slug}), {"body": "one more"})
        self.assertContains(response, "try again later")
        self.assertFalse(SpeakerComment.objects.filter(body="one more").exists())

    def test_deleting_an_account_removes_its_own_comments_and_unsigns_club_notes(self):
        from auctions.account_deletion import _anonymize_assistant_records

        club_note = SpeakerComment.objects.create(
            speaker=self.speaker, user=self.officer, club=self.nec_club, body="Club note"
        )
        _anonymize_assistant_records(self.officer)
        self.assertFalse(SpeakerComment.objects.filter(pk=self.comment.pk).exists())
        club_note.refresh_from_db()
        self.assertIsNone(club_note.user)

    def test_deleting_from_a_page_you_cant_open_returns_nothing(self):
        Speaker.objects.filter(pk=self.speaker.pk).update(visibility=Speaker.VISIBILITY_HIDDEN)
        client = Client()
        client.force_login(self.officer)
        response = client.post(
            reverse("speaker_comment_delete", kwargs={"slug": self.speaker.slug, "pk": self.comment.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)
