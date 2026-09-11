"""What the DMCA safe harbour needs to be true, checked.

Section 512 is a list of conditions rather than a standard, which makes it unusually testable, and
every test here is one of the conditions:

* the designated agent is published, and only when one is actually configured (512(c)(2));
* a notice can be sent and reaches the agent, and the address that address resolves to is not a
  hole in the ground (512(c)(2) again -- AOL lost the safe harbour in *Ellison v. Robertson* for
  letting notices bounce);
* a notice has all six of its parts or it is not a notice (512(c)(3)(A));
* **removing something removes it** -- the file, the thumbnails, and the copy at the edge
  (512(c)(1)(C)), which is the one this whole area of the code was missing;
* strikes are recorded, counted, and stop counting when withdrawn (512(i)(1)(A)).

``AgentConfigurationTests`` is the one to read first: everything else assumes an agent, and the
question of what a deployment with no agent does is the one a fork gets wrong.
"""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from auctions import dmca
from auctions.email_routing import resolve_routed_recipient
from auctions.models import ContentReport, CopyrightNotice, CopyrightStrike, Lot, LotImage
from auctions.tests import StandardTestCase, WritableMediaRoot

#: A complete set of agent details, for tests that need /dmca/ to exist.
AGENT = {
    "DMCA_SERVICE_PROVIDER_NAME": "Test Fish Club, Inc.",
    "DMCA_AGENT_NAME": "Copyright Agent",
    "DMCA_AGENT_PHONE": "+1 802 555 0100",
    "DMCA_AGENT_EMAIL": "dmca@example.com",
    "DMCA_AGENT_ADDRESS": "123 Test Street, Anytown VT",
}

#: A notice with all six parts of 512(c)(3)(A) in it.
COMPLETE_NOTICE = {
    "name": "Ansel Adams",
    "email": "ansel@example.com",
    "phone": "+1 555 0100",
    "address": "1 Yosemite Road",
    "work": "My photograph of a bristlenose pleco, first published on my website",
    "material": "The first image on the lot page",
    "good_faith": True,
    "accurate": True,
    "signature": "Ansel Adams",
}

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class AgentConfigurationTests(SimpleTestCase):
    """Who gets published, and what an unconfigured deployment does instead."""

    @override_settings(**AGENT)
    def test_a_fully_configured_agent_is_published(self):
        agent = dmca.agent()
        self.assertEqual(agent["name"], "Copyright Agent")
        self.assertEqual(agent["service_provider"], "Test Fish Club, Inc.")
        self.assertEqual(agent["email"], "dmca@example.com")
        self.assertTrue(dmca.is_configured())

    @override_settings(
        DMCA_SERVICE_PROVIDER_NAME="",
        DMCA_AGENT_NAME="",
        DMCA_AGENT_PHONE="",
        DMCA_AGENT_EMAIL="",
        DMCA_AGENT_ADDRESS="",
    )
    def test_nothing_configured_publishes_nothing(self):
        """A fork that has not registered must not publish somebody else's agent."""
        self.assertIsNone(dmca.agent())
        self.assertFalse(dmca.is_configured())

    @override_settings(**{**AGENT, "DMCA_AGENT_PHONE": ""})
    def test_a_partial_agent_is_no_agent(self):
        """512(c)(2) names four things; three of them tells a rightsholder they can't reach anyone."""
        self.assertIsNone(dmca.agent())

    @override_settings(
        **{**AGENT, "DMCA_AGENT_EMAIL": "", "DMCA_AGENT_ADDRESS": ""},
        ADMINS=[("Admin", "admin@example.com")],
        MAILING_ADDRESS="9 Fallback Lane",
    )
    def test_email_and_address_fall_back_to_the_settings_most_sites_already_have(self):
        agent = dmca.agent()
        self.assertEqual(agent["email"], "admin@example.com")
        self.assertEqual(agent["address"], "9 Fallback Lane")

    @override_settings(
        **{**AGENT, "DMCA_AGENT_ADDRESS": ""},
        MAILING_ADDRESS="No address configured",
    )
    def test_the_unset_mailing_address_placeholder_is_not_an_address(self):
        """Publishing the words "No address configured" is worse than publishing no page."""
        self.assertIsNone(dmca.agent())

    @override_settings(
        DMCA_SERVICE_PROVIDER_NAME="",
        DMCA_AGENT_NAME="",
        DMCA_AGENT_PHONE="",
        DMCA_AGENT_EMAIL="",
        DMCA_AGENT_ADDRESS="",
        ADMINS=[("Admin", "admin@example.com")],
    )
    def test_notices_still_reach_somebody_with_no_agent_registered(self):
        """Not being in the directory is a reason to lose the safe harbour, not to drop the mail."""
        self.assertEqual(dmca.agent_email(), "admin@example.com")


class DmcaPageTests(StandardTestCase):
    """The published page, and the links to it."""

    @override_settings(**AGENT)
    def test_the_page_publishes_every_detail_512c2_requires(self):
        response = self.client.get(reverse("dmca"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        for value in AGENT.values():
            self.assertIn(value, page)

    @override_settings(**AGENT)
    def test_the_published_strike_count_matches_the_one_that_runs(self):
        """A published policy that does not match the implemented one is what loses the safe harbour."""
        response = self.client.get(reverse("dmca"))
        self.assertContains(response, f"closed at {dmca.STRIKES_BEFORE_TERMINATION} strikes")

    @override_settings(
        DMCA_SERVICE_PROVIDER_NAME="",
        DMCA_AGENT_NAME="",
        DMCA_AGENT_PHONE="",
        DMCA_AGENT_EMAIL="",
        DMCA_AGENT_ADDRESS="",
        MAILING_ADDRESS="No address configured",
    )
    def test_the_page_is_absent_rather_than_wrong_when_no_agent_is_registered(self):
        self.assertEqual(self.client.get(reverse("dmca")).status_code, 404)

    @override_settings(**AGENT)
    def test_every_page_links_to_it(self):
        response = self.client.get(reverse("allLots"))
        self.assertContains(response, reverse("dmca"))

    @override_settings(
        DMCA_SERVICE_PROVIDER_NAME="",
        DMCA_AGENT_NAME="",
        DMCA_AGENT_PHONE="",
        DMCA_AGENT_EMAIL="",
        DMCA_AGENT_ADDRESS="",
        MAILING_ADDRESS="No address configured",
    )
    def test_the_footer_link_is_hidden_when_the_page_does_not_exist(self):
        response = self.client.get(reverse("allLots"))
        self.assertNotContains(response, reverse("dmca"))


class NoticeIntakeTests(StandardTestCase):
    """Sending a notice, and what makes something a notice at all."""

    @override_settings(**AGENT)
    def test_a_complete_notice_is_stored_and_sent_to_the_agent(self):
        from post_office.models import Email

        response = self.client.post(reverse("dmca_notice"), COMPLETE_NOTICE, follow=True)
        self.assertEqual(response.status_code, 200)
        notice = CopyrightNotice.objects.get()
        self.assertEqual(notice.name, "Ansel Adams")
        self.assertTrue(notice.is_complete)
        self.assertTrue(Email.objects.filter(to=["dmca@example.com"]).exists())

    @override_settings(**AGENT)
    def test_a_notice_without_the_perjury_statement_is_refused(self):
        """512(c)(3)(A)(vi) is not optional, and a form that took it as optional would be
        collecting something that is not a notice while looking like it collects notices."""
        payload = {**COMPLETE_NOTICE, "accurate": False}
        self.client.post(reverse("dmca_notice"), payload)
        self.assertEqual(CopyrightNotice.objects.count(), 0)

    @override_settings(**AGENT)
    def test_a_notice_without_the_good_faith_statement_is_refused(self):
        payload = {**COMPLETE_NOTICE, "good_faith": False}
        self.client.post(reverse("dmca_notice"), payload)
        self.assertEqual(CopyrightNotice.objects.count(), 0)

    @override_settings(**AGENT)
    def test_the_lot_is_picked_out_of_the_urls_so_the_takedown_is_one_click(self):
        payload = {**COMPLETE_NOTICE, "material": f"https://example.com/lots/{self.lot.pk}/whatever"}
        self.client.post(reverse("dmca_notice"), payload)
        self.assertEqual(CopyrightNotice.objects.get().lot, self.lot)

    @override_settings(**AGENT)
    def test_anybody_can_send_one_without_an_account(self):
        """A photographer who finds their picture here is not going to sign up to say so."""
        self.client.logout()
        self.client.post(reverse("dmca_notice"), COMPLETE_NOTICE)
        self.assertEqual(CopyrightNotice.objects.count(), 1)

    def test_an_incomplete_notice_entered_by_hand_knows_it_is_incomplete(self):
        notice = CopyrightNotice.objects.create(
            name="Someone", email="a@example.com", address="x", work="", material="a lot", signature=""
        )
        self.assertFalse(notice.is_complete)


class NoticeRoutingTests(SimpleTestCase):
    """The address in the public directory has to resolve to a person."""

    @override_settings(**AGENT)
    def test_the_dmca_alias_routes_to_the_agent(self):
        self.assertEqual(resolve_routed_recipient("dmca"), "dmca@example.com")

    @override_settings(
        DMCA_SERVICE_PROVIDER_NAME="",
        DMCA_AGENT_NAME="",
        DMCA_AGENT_PHONE="",
        DMCA_AGENT_EMAIL="",
        DMCA_AGENT_ADDRESS="",
        ADMINS=[("Admin", "admin@example.com")],
    )
    def test_it_still_routes_with_no_agent_configured(self):
        """Before this existed the alias fell through every branch and the Lambda dropped the mail."""
        self.assertEqual(resolve_routed_recipient("dmca"), "admin@example.com")

    @override_settings(
        **{**AGENT, "DMCA_AGENT_EMAIL": "DMCA@Auction.Example"},
        EMAIL_ROUTING_DOMAIN="auction.example",
        ADMINS=[("Admin", "admin@example.com")],
    )
    def test_publishing_the_alias_itself_does_not_route_it_to_itself(self):
        """The setup checklist suggests dmca@yourdomain.com, and that address is the alias.

        Forwarding the alias to itself sends the copy back in through SES from the relay address,
        where the Lambda's loop guard drops it: every notice lost, with nothing bouncing.
        """
        self.assertEqual(dmca.agent()["email"], "DMCA@Auction.Example")
        self.assertEqual(resolve_routed_recipient("dmca"), "admin@example.com")


class ReportContentTests(StandardTestCase):
    """The report button. App Store Review Guideline 1.2 asks for one; so does common sense."""

    def test_a_signed_out_visitor_can_report_a_lot(self):
        self.client.logout()
        response = self.client.post(
            reverse("report_lot", kwargs={"pk": self.lot.pk}),
            {"reason": "SPAM", "details": "Asked me to pay by wire transfer", "reporter_email": "x@example.com"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        report = ContentReport.objects.get()
        self.assertEqual(report.lot, self.lot)
        self.assertEqual(report.reason, "SPAM")
        self.assertIsNone(report.reported_by)

    def test_the_report_page_renders_for_somebody_with_no_account(self):
        self.client.logout()
        response = self.client.get(reverse("report_lot", kwargs={"pk": self.lot.pk}))
        self.assertEqual(response.status_code, 200)
        # Copyright complaints are pointed somewhere else on purpose: this form collects none of
        # the six things a notice needs.
        self.assertContains(response, reverse("dmca"))

    def test_the_lot_page_offers_the_report_link_to_a_signed_out_reader(self):
        """App Review browses with no session, and so does a photographer who just found their
        picture on a listing here."""
        self.client.logout()
        response = self.client.get(self.lot.lot_link)
        self.assertContains(response, reverse("report_lot", kwargs={"pk": self.lot.pk}))

    def test_a_signed_in_reporter_is_recorded(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(
            reverse("report_lot", kwargs={"pk": self.lot.pk}),
            {"reason": "OFFENSIVE", "details": "rude"},
        )
        self.assertEqual(ContentReport.objects.get().reported_by, self.user)

    def test_the_report_survives_the_lot_it_was_about(self):
        """A queue that deletes its own record the moment the thing is dealt with is no record."""
        self.client.post(
            reverse("report_lot", kwargs={"pk": self.lot.pk}),
            {"reason": "PROHIBITED", "details": "not legal to ship"},
        )
        report_pk = ContentReport.objects.get().pk
        Lot.objects.filter(pk=self.lot.pk).delete()
        report = ContentReport.objects.get(pk=report_pk)
        self.assertIsNone(report.lot)
        self.assertIn("/lots/", report.material)


class StrikeTests(StandardTestCase):
    """512(i)(1)(A): adopted, published, and reasonably implemented."""

    def test_strikes_are_counted(self):
        dmca.record_strike(self.user, reason="one")
        dmca.record_strike(self.user, reason="two")
        self.assertEqual(dmca.strike_count(self.user), 2)

    def test_a_withdrawn_strike_stays_on_the_record_and_stops_counting(self):
        strike = dmca.record_strike(self.user, reason="mistaken notice")
        strike.withdrawn = True
        strike.withdrawn_reason = "The sender withdrew it"
        strike.save()
        self.assertEqual(dmca.strike_count(self.user), 0)
        self.assertEqual(CopyrightStrike.objects.filter(user=self.user).count(), 1)

    def test_the_user_is_told_every_time(self):
        from post_office.models import Email

        dmca.record_strike(self.user, reason="a photo")
        self.assertTrue(Email.objects.filter(to=[self.user.email]).exists())

    @override_settings(ADMINS=[("Admin", "admin@example.com")])
    def test_the_third_strike_asks_a_person_rather_than_closing_the_account(self):
        """512(f) exists because false notices are sent; an automatic ban wired to a number a
        stranger controls is a way to lose somebody their account over a form."""
        from post_office.models import Email

        for _ in range(dmca.STRIKES_BEFORE_TERMINATION):
            dmca.record_strike(self.user, reason="a photo")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(Email.objects.filter(to=["admin@example.com"]).exists())

    def test_terminating_closes_the_account_without_deleting_anybody_elses_records(self):
        lot_pk = self.lot.pk
        dmca.terminate(self.user, reason="3 strikes")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(Lot.objects.filter(pk=lot_pk).exists())


class TakedownRemovesTheMaterialTests(WritableMediaRoot, StandardTestCase):
    """The one that matters: deleting the row has to delete the picture.

    Before this, ``LotImage.delete()`` left the JPEG under ``mediafiles/`` and nginx went on
    serving it, unauthenticated, at the same URL the notice quoted -- for another thirty days at
    the edge on top of that. "We deleted the database row" is not removal.
    """

    def _image(self, lot, name="takedown.png"):
        return LotImage.objects.create(
            lot_number=lot,
            image=SimpleUploadedFile(name, PNG, content_type="image/png"),
        )

    def test_deleting_an_image_deletes_the_file(self):
        image = self._image(self.lot)
        storage, path = image.image.storage, image.image.name
        self.assertTrue(storage.exists(path))
        image.delete()
        self.assertFalse(storage.exists(path))

    def test_a_file_two_rows_share_is_left_alone(self):
        """Relisting a lot gives the copy the original's file rather than duplicating it, so
        deleting one row must not take the other row's picture with it."""
        first = self._image(self.lot)
        second = LotImage.objects.create(lot_number=self.lotB, image=first.image.name)
        storage, path = first.image.storage, first.image.name
        first.delete()
        self.assertTrue(storage.exists(path))
        second.delete()
        self.assertFalse(storage.exists(path))

    def test_the_edge_cache_is_purged_so_the_cdn_stops_serving_it(self):
        image = self._image(self.lot)
        url = image.image.url
        with patch("auctions.tasks.purge_edge_cache.delay") as purge, self.captureOnCommitCallbacks(execute=True):
            image.delete()
        self.assertTrue(purge.called, "the file was deleted but the edge was never told")
        purged = purge.call_args[0][0]
        self.assertTrue(any(url in candidate for candidate in purged), purged)
        self.assertTrue(all(candidate.startswith("https://") for candidate in purged), purged)

    def test_take_down_removes_every_image_and_records_a_strike(self):
        self.lot.user = self.user
        self.lot.save()
        self._image(self.lot, "one.png")
        self._image(self.lot, "two.png")
        notice = CopyrightNotice.objects.create(lot=self.lot, **COMPLETE_NOTICE)
        removed = dmca.take_down(notice)
        self.assertEqual(removed, 2)
        self.assertEqual(LotImage.objects.filter(lot_number=self.lot).count(), 0)
        notice.refresh_from_db()
        self.assertEqual(notice.status, "REMOVED")
        self.assertIsNotNone(notice.actioned_on)
        self.assertEqual(dmca.strike_count(self.user), 1)

    def test_take_down_leaves_the_listing_itself_alone(self):
        """A notice is about a photograph. Deleting the listing would take bids and an auction
        entry that had nothing to do with the complaint."""
        self.lot.user = self.user
        self.lot.save()
        self._image(self.lot)
        notice = CopyrightNotice.objects.create(lot=self.lot, **COMPLETE_NOTICE)
        dmca.take_down(notice)
        self.assertTrue(Lot.objects.filter(pk=self.lot.pk).exists())


class MobileConfigTests(TestCase):
    """The app draws the copyright link from the same switch the website does."""

    @override_settings(**AGENT)
    def test_the_config_carries_the_dmca_url_when_an_agent_is_registered(self):
        response = self.client.get("/api/mobile/config/")
        self.assertEqual(response.json().get("dmca_url"), reverse("dmca"))

    @override_settings(
        DMCA_SERVICE_PROVIDER_NAME="",
        DMCA_AGENT_NAME="",
        DMCA_AGENT_PHONE="",
        DMCA_AGENT_EMAIL="",
        DMCA_AGENT_ADDRESS="",
        MAILING_ADDRESS="No address configured",
    )
    def test_the_key_is_absent_rather_than_a_dead_link(self):
        response = self.client.get("/api/mobile/config/")
        self.assertNotIn("dmca_url", response.json())


class ImageSourceLabelTests(SimpleTestCase):
    """The catch-all category asks for permission rather than recording an admission."""

    def test_no_category_tells_a_user_to_say_the_picture_is_stolen(self):
        for value, label in LotImage.PIC_CATEGORIES:
            self.assertNotIn("from the internet", label.lower(), value)

    def test_the_catch_all_still_exists(self):
        """It is what a blank field is set to, so removing it would only move the problem."""
        self.assertIn("RANDOM", dict(LotImage.PIC_CATEGORIES))


class AccountDeletionTests(StandardTestCase):
    """What deletion does to the moderation record."""

    def test_a_report_they_filed_keeps_its_place_in_the_queue_and_loses_them(self):
        from auctions.account_deletion import delete_account

        report = ContentReport.objects.create(
            reason="SPAM", reported_by=self.user, reporter_email="me@example.com", material="/lots/1/"
        )
        delete_account(self.user)
        report.refresh_from_db()
        self.assertIsNone(report.reported_by)
        self.assertEqual(report.reporter_email, "")

    def test_strikes_against_them_survive(self):
        """They hang off the User row that survives deletion, and 512(i) is a condition we would
        otherwise be dismantling one deleted account at a time."""
        from auctions.account_deletion import delete_account

        dmca.record_strike(self.user, reason="a photo")
        delete_account(self.user)
        self.assertEqual(CopyrightStrike.objects.filter(user__pk=self.user.pk).count(), 1)
