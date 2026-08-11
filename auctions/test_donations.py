"""Tests for donation tracking: routing, the inbound webhook, the LLM seams, and the UI gates.

Every language-model call goes through a fake provider installed with
``llm.set_provider_override``, so nothing here touches the network.
"""

import datetime
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import donations
from auctions.email_routing import resolve_donation_alias, resolve_routing_info
from auctions.llm import LLMError, LLMProvider, LLMResult, set_provider_override
from auctions.models import (
    Club,
    ClubHistory,
    ClubMember,
    DonationEmail,
    DonationUnsubscribe,
    DonationVendor,
)
from auctions.test_support import isolated_cache

ROUTING_SETTINGS = {
    "SES_ROUTE_EMAILS_ENABLED": True,
    "EMAIL_ROUTING_DOMAIN": "example.com",
    "INBOUND_ROUTING_SECRET": "test-secret",
}


class FakeProvider(LLMProvider):
    """Returns canned JSON, and remembers what it was asked."""

    name = "fake"

    def __init__(self, payload=None, error=None):
        super().__init__(model="fake-model", api_key="key")
        self.payload = payload if payload is not None else {}
        self.error = error
        self.calls = []

    def is_configured(self):
        return True

    def complete_json(self, system, messages, max_tokens=2000):
        self.calls.append({"system": system, "messages": messages})
        if self.error:
            raise self.error
        return LLMResult(data=self.payload, model=self.model, prompt_tokens=10, completion_tokens=5)


class DonationTestMixin:
    """A club with donation tracking on, an admin who can use it, and one vendor."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.club = Club.objects.create(
            name="Test Aquarium Society",
            enable_donation_tracking=True,
            donation_mailing_address="TAS\n1 Main St\nSpringfield, IL 62701",
            donation_context="501(c)(3) #12-3456789",
        )
        self.admin = User.objects.create_user(username="don_admin", password="pw", email="admin@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, permission_admin=True)
        self.outsider = User.objects.create_user(username="don_out", password="pw", email="out@example.com")
        self.vendor = DonationVendor.objects.create(
            club=self.club,
            name="Fishy Business",
            contact_name="Pat Smith",
            email="pat@fishybusiness.example",
        )

    def use_provider(self, provider):
        set_provider_override(provider)
        self.addCleanup(set_provider_override, None)
        return provider


@isolated_cache("donations")
class DonationRoutingTests(DonationTestMixin, TestCase):
    """The <club-slug>-donations-<key> alias."""

    @override_settings(**ROUTING_SETTINGS)
    def test_a_donation_alias_resolves_to_its_vendor(self):
        local_part = f"{self.club.slug}-donations-{self.vendor.routing_key}"
        match = resolve_donation_alias(local_part)
        self.assertIsNotNone(match)
        self.assertEqual(match["vendor"], self.vendor)

    @override_settings(**ROUTING_SETTINGS)
    def test_routing_info_marks_donation_addresses(self):
        info = resolve_routing_info(f"{self.club.slug}-donations-{self.vendor.routing_key}")
        self.assertEqual(info["kind"], "donation")
        self.assertEqual(info["vendor_key"], self.vendor.routing_key)
        # No donation contact set, so there is nobody to forward to -- but the alias still resolves
        # so the message reaches the webhook and is recorded.
        self.assertEqual(info["recipient"], "")

    @override_settings(**ROUTING_SETTINGS)
    def test_a_donation_contact_is_forwarded_to(self):
        member = ClubMember.objects.create(
            club=self.club, name="Treasurer", email="treasurer@example.com", permission_add_edit=True
        )
        self.club.donation_email_member = member
        self.club.save()
        info = resolve_routing_info(f"{self.club.slug}-donations-{self.vendor.routing_key}")
        self.assertEqual(info["recipient"], "treasurer@example.com")

    @override_settings(**ROUTING_SETTINGS)
    def test_an_unknown_key_does_not_resolve(self):
        self.assertIsNone(resolve_donation_alias(f"{self.club.slug}-donations-0000000000"))

    @override_settings(**ROUTING_SETTINGS)
    def test_a_deleted_vendor_does_not_resolve(self):
        self.vendor.is_deleted = True
        self.vendor.save()
        local_part = f"{self.club.slug}-donations-{self.vendor.routing_key}"
        self.assertIsNone(resolve_donation_alias(local_part))

    @override_settings(**ROUTING_SETTINGS)
    def test_copy_paste_mode_does_not_accept_replies(self):
        """A club that isn't sending from this site has no tracked address to receive on."""
        self.club.donation_email_mode = Club.DONATION_EMAIL_MODE_COPY
        self.club.save()
        local_part = f"{self.club.slug}-donations-{self.vendor.routing_key}"
        self.assertIsNone(resolve_donation_alias(local_part))

    @override_settings(**ROUTING_SETTINGS)
    def test_a_malformed_alias_is_not_mistaken_for_a_donation(self):
        for bad in ("club-donations-123", "club-donations-abcdefghij", "club-donations", "club-contact"):
            self.assertIsNone(resolve_donation_alias(bad), bad)

    @override_settings(**ROUTING_SETTINGS)
    def test_the_reply_to_address_round_trips(self):
        address = self.vendor.reply_to_address
        self.assertTrue(address.endswith("@example.com"))
        match = resolve_donation_alias(address.split("@")[0])
        self.assertEqual(match["vendor"], self.vendor)

    def test_routing_keys_are_ten_digits_and_unique(self):
        other = DonationVendor.objects.create(club=self.club, name="Second", email="b@example.com")
        self.assertEqual(len(self.vendor.routing_key), 10)
        self.assertTrue(self.vendor.routing_key.isdigit())
        self.assertNotEqual(self.vendor.routing_key, other.routing_key)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class InboundDonationWebhookTests(DonationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("inbound_donation_email")
        self.provider = self.use_provider(
            FakeProvider({"summary": "They will donate a 20 gallon tank.", "status": "promised"})
        )

    def post(self, payload, secret="test-secret"):
        headers = {"HTTP_X_ROUTING_SECRET": secret} if secret is not None else {}
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def reply_payload(self, **overrides):
        payload = {
            "address": f"{self.club.slug}-donations-{self.vendor.routing_key}@example.com",
            "from": "Pat Smith <pat@fishybusiness.example>",
            "subject": "Re: Donation request",
            "body": "Happy to help! We'll send a 20 gallon tank.",
            "message_id": "<abc123@mail.example>",
        }
        payload.update(overrides)
        return payload

    def test_a_reply_is_recorded_against_its_vendor(self):
        response = self.post(self.reply_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "recorded")
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertEqual(email.direction, DonationEmail.DIRECTION_INCOMING)
        self.assertEqual(email.sender, "pat@fishybusiness.example")
        self.assertIn("20 gallon tank", email.body)

    def test_the_summary_and_status_come_from_the_model(self):
        self.post(self.reply_payload())
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.vendor.refresh_from_db()
        self.assertEqual(email.summary, "They will donate a 20 gallon tank.")
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_PROMISED)

    def test_an_incoming_reply_makes_the_followup_due_now(self):
        self.vendor.followup_due = timezone.now() + datetime.timedelta(days=30)
        self.vendor.save()
        before = timezone.now()
        self.post(self.reply_payload())
        self.vendor.refresh_from_db()
        self.assertGreaterEqual(self.vendor.followup_due, before)
        self.assertLessEqual(self.vendor.followup_due, timezone.now())

    def test_the_body_is_stripped_of_images(self):
        html = '<p>Sure!</p><img src="data:image/png;base64,' + ("A" * 500) + '"><p>Bye</p>'
        self.post(self.reply_payload(body=html))
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertNotIn("<img", email.body)
        self.assertNotIn("base64", email.body)
        self.assertIn("Sure!", email.body)

    def test_a_duplicate_delivery_is_ignored(self):
        self.post(self.reply_payload())
        response = self.post(self.reply_payload())
        self.assertEqual(response.json()["status"], "duplicate")
        self.assertEqual(DonationEmail.objects.filter(vendor=self.vendor).count(), 1)

    def test_an_unmatched_address_is_dropped_quietly(self):
        response = self.post(self.reply_payload(address="nobody-donations-0000000000@example.com"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "dropped")
        self.assertEqual(DonationEmail.objects.count(), 0)

    def test_a_bad_secret_is_rejected(self):
        self.assertEqual(self.post(self.reply_payload(), secret="wrong").status_code, 401)
        self.assertEqual(DonationEmail.objects.count(), 0)

    def test_a_missing_secret_is_rejected(self):
        self.assertEqual(self.post(self.reply_payload(), secret=None).status_code, 401)

    def test_the_daily_summary_budget_is_capped_but_mail_is_still_stored(self):
        for index in range(donations.MAX_INCOMING_LLM_CALLS_PER_DAY + 3):
            self.post(self.reply_payload(message_id=f"<msg{index}@mail.example>"))
        self.assertEqual(
            DonationEmail.objects.filter(vendor=self.vendor).count(),
            donations.MAX_INCOMING_LLM_CALLS_PER_DAY + 3,
        )
        self.assertEqual(len(self.provider.calls), donations.MAX_INCOMING_LLM_CALLS_PER_DAY)
        unsummarized = DonationEmail.objects.filter(vendor=self.vendor, summary="").count()
        self.assertEqual(unsummarized, 3)

    def test_a_model_failure_does_not_lose_the_reply(self):
        self.use_provider(FakeProvider(error=LLMError("boom")))
        response = self.post(self.reply_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DonationEmail.objects.filter(vendor=self.vendor).count(), 1)


@isolated_cache("donations")
class IncomingStatusRulesTests(DonationTestMixin, TestCase):
    """What the model is and isn't allowed to do to a vendor's status."""

    def test_an_invented_status_is_discarded(self):
        self.assertFalse(donations.apply_incoming_status(self.vendor, "totally_made_up"))
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_NEW)

    def test_the_model_cannot_assign_donation_received(self):
        self.assertFalse(donations.apply_incoming_status(self.vendor, DonationVendor.STATUS_RECEIVED))

    def test_the_model_cannot_assign_do_not_contact(self):
        self.assertFalse(donations.apply_incoming_status(self.vendor, DonationVendor.STATUS_DO_NOT_CONTACT))

    def test_do_not_contact_is_never_overridden(self):
        self.vendor.status = DonationVendor.STATUS_DO_NOT_CONTACT
        self.vendor.save()
        self.assertFalse(donations.apply_incoming_status(self.vendor, DonationVendor.STATUS_INTERESTED))
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_DO_NOT_CONTACT)

    def test_an_unsubscribed_vendor_is_never_moved(self):
        self.vendor.unsubscribed = True
        self.vendor.save()
        self.assertFalse(donations.apply_incoming_status(self.vendor, DonationVendor.STATUS_INTERESTED))

    def test_a_received_donation_outranks_a_later_email(self):
        self.vendor.status = DonationVendor.STATUS_RECEIVED
        self.vendor.save()
        self.assertFalse(donations.apply_incoming_status(self.vendor, DonationVendor.STATUS_NOT_INTERESTED))

    def test_a_real_change_is_written_to_club_history(self):
        self.assertTrue(donations.apply_incoming_status(self.vendor, DonationVendor.STATUS_INTERESTED))
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, applies_to="DONATIONS", action__contains="Interested").exists()
        )

    def test_a_summary_longer_than_the_column_is_truncated(self):
        summary, status = donations._validate_incoming_reply({"summary": "x" * 400, "status": "interested"})
        self.assertEqual(len(summary), donations.SUMMARY_LENGTH)
        self.assertEqual(status, DonationVendor.STATUS_INTERESTED)

    def test_an_unclear_reply_leaves_the_status_alone(self):
        summary, status = donations._validate_incoming_reply({"summary": "Out of office", "status": "unclear"})
        self.assertIsNone(status)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class DraftingTests(DonationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.provider = self.use_provider(
            FakeProvider({"subject": "Donation request", "body": "Dear Pat,\n\nWould you donate?"})
        )

    def test_a_draft_comes_back_as_subject_and_body(self):
        subject, body = donations.draft_request(self.vendor, context="They sell tanks")
        self.assertEqual(subject, "Donation request")
        self.assertIn("Dear Pat", body)

    def test_the_prompt_carries_the_club_and_vendor_context(self):
        donations.draft_request(self.vendor, context="They sold us tanks last year")
        prompt = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn("Fishy Business", prompt)
        self.assertIn("Pat Smith", prompt)
        self.assertIn("501(c)(3) #12-3456789", prompt)
        self.assertIn("1 Main St", prompt)
        self.assertIn("They sold us tanks last year", prompt)

    def test_a_long_last_email_is_truncated_before_it_is_sent(self):
        donations.draft_request(self.vendor, last_email="word " * 5000)
        prompt = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn("[truncated]", prompt)
        self.assertLess(len(prompt), donations.LAST_EMAIL_LIMIT + donations.CONTEXT_LIMIT + 2000)

    def test_an_off_contract_reply_is_rejected(self):
        self.use_provider(FakeProvider({"subject": "Only a subject"}))
        with self.assertRaises(LLMError):
            donations.draft_request(self.vendor)

    def test_the_daily_draft_budget_is_capped(self):
        for _ in range(donations.MAX_DRAFT_LLM_CALLS_PER_DAY):
            donations.draft_request(self.vendor)
        with self.assertRaises(LLMError):
            donations.draft_request(self.vendor)

    def test_the_draft_and_summary_budgets_are_separate(self):
        for _ in range(donations.MAX_DRAFT_LLM_CALLS_PER_DAY):
            donations.draft_request(self.vendor)
        # The incoming budget is untouched, so a reply arriving now is still summarized.
        self.assertTrue(donations.check_rate_limit(self.club, "incoming"))


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class SendingTests(DonationTestMixin, TestCase):
    def test_sending_records_an_outgoing_email_and_schedules_a_followup(self):
        donations.send_request(self.vendor, subject="Hello", body="Please donate", user=self.admin)
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.vendor.refresh_from_db()
        self.assertEqual(email.direction, DonationEmail.DIRECTION_OUTGOING)
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_EMAIL_SENT)
        self.assertIsNotNone(self.vendor.followup_due)
        expected = self.vendor.last_contact + datetime.timedelta(days=self.club.donation_followup_days)
        self.assertAlmostEqual(self.vendor.followup_due, expected, delta=datetime.timedelta(seconds=5))

    def test_the_followup_interval_follows_the_club_setting(self):
        self.club.donation_followup_days = 1
        self.club.save()
        donations.send_request(self.vendor, subject="Hello", body="Please donate", user=self.admin)
        self.vendor.refresh_from_db()
        expected = self.vendor.last_contact + datetime.timedelta(days=1)
        self.assertAlmostEqual(self.vendor.followup_due, expected, delta=datetime.timedelta(seconds=5))

    def test_the_sent_message_carries_an_address_and_an_opt_out(self):
        donations.send_request(self.vendor, subject="Hello", body="Please donate", user=self.admin)
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertIn("1 Main St", email.body)
        self.assertIn(self.vendor.unsubscribe_url, email.body)
        self.assertIn("Test Aquarium Society", email.body)

    def test_it_is_sent_from_the_vendors_own_tracked_address(self):
        donations.send_request(self.vendor, subject="Hello", body="Please donate", user=self.admin)
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertEqual(email.sender, self.vendor.reply_to_address)

    def test_an_already_promised_vendor_is_not_demoted_to_email_sent(self):
        self.vendor.status = DonationVendor.STATUS_PROMISED
        self.vendor.save()
        donations.send_request(self.vendor, subject="Hi", body="Thanks", user=self.admin)
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_PROMISED)

    def test_a_do_not_contact_vendor_cannot_be_sent_to(self):
        self.vendor.status = DonationVendor.STATUS_DO_NOT_CONTACT
        self.vendor.save()
        with self.assertRaises(donations.DonationSendError):
            donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        self.assertEqual(DonationEmail.objects.count(), 0)

    def test_a_vendor_with_no_email_cannot_be_sent_to(self):
        self.vendor.email = ""
        self.vendor.save()
        with self.assertRaises(donations.DonationSendError):
            donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)

    @override_settings(SES_ROUTE_EMAILS_ENABLED=False)
    def test_a_site_without_routing_falls_back_to_copy_paste(self):
        """A stored mode of "routed" on a site that can't route must not offer a Send button."""
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_email_mode, Club.DONATION_EMAIL_MODE_ROUTED)
        self.assertFalse(self.club.sends_donation_email)

    def test_copy_paste_mode_refuses_to_send(self):
        self.club.donation_email_mode = Club.DONATION_EMAIL_MODE_COPY
        self.club.save()
        self.vendor.refresh_from_db()
        with self.assertRaises(donations.DonationSendError):
            donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)

    def test_a_copied_request_is_recorded_the_same_way(self):
        donations.record_copied_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.vendor.refresh_from_db()
        self.assertEqual(email.direction, DonationEmail.DIRECTION_OUTGOING)
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_EMAIL_SENT)
        self.assertIsNotNone(self.vendor.followup_due)

    def test_sending_is_refused_without_a_postal_address(self):
        """CAN-SPAM requires a physical address on a bulk solicitation; refuse rather than omit."""
        self.club.donation_mailing_address = ""
        self.club.save()
        self.vendor.refresh_from_db()
        with self.assertRaises(donations.DonationSendError):
            donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        self.assertEqual(DonationEmail.objects.count(), 0)

    def test_the_from_line_names_the_club(self):
        from post_office.models import Email as QueuedEmail

        donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        queued = QueuedEmail.objects.latest("id")
        self.assertIn("Test Aquarium Society", queued.from_email)
        self.assertIn(self.vendor.reply_to_address, queued.from_email)

    def test_the_message_says_it_is_a_donation_request(self):
        donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertIn("donation request from Test Aquarium Society", email.body)

    def test_a_copied_request_still_carries_the_address_and_opt_out(self):
        """Copy/paste mode has no postal-address gate, but the text it hands over still has both."""
        donations.record_copied_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertIn("1 Main St", email.body)
        self.assertIn(self.vendor.unsubscribe_url, email.body)

    def test_the_draft_prompt_forbids_claiming_tax_deductibility(self):
        self.assertIn("tax deductible", donations._DRAFT_SYSTEM_PROMPT)

    def test_sending_is_written_to_club_history(self):
        donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, applies_to="DONATIONS", action__contains="Sent").exists()
        )


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class UnsubscribeTests(DonationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("donation_unsubscribe", kwargs={"uuid": self.vendor.uuid})

    def test_a_get_does_not_unsubscribe_anyone(self):
        """Mail clients prefetch links; only the POST may act."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.unsubscribed)

    def test_posting_unsubscribes_the_vendor_permanently(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.unsubscribed)
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_DO_NOT_CONTACT)
        self.assertTrue(DonationUnsubscribe.objects.filter(email=self.vendor.email).exists())

    def test_unsubscribing_reaches_every_club(self):
        other_club = Club.objects.create(name="Other Club", enable_donation_tracking=True)
        twin = DonationVendor.objects.create(
            club=other_club, name="Fishy Business", email=self.vendor.email, status=DonationVendor.STATUS_INTERESTED
        )
        self.client.post(self.url)
        twin.refresh_from_db()
        self.assertTrue(twin.unsubscribed)
        self.assertEqual(twin.status, DonationVendor.STATUS_DO_NOT_CONTACT)

    def test_a_vendor_added_later_elsewhere_is_born_unsubscribed(self):
        self.client.post(self.url)
        other_club = Club.objects.create(name="Later Club", enable_donation_tracking=True)
        added_later = DonationVendor.objects.create(club=other_club, name="Fishy Business", email=self.vendor.email)
        self.assertTrue(added_later.unsubscribed)
        self.assertEqual(added_later.status, DonationVendor.STATUS_DO_NOT_CONTACT)
        self.assertFalse(added_later.can_be_contacted)

    def test_an_unsubscribed_vendor_cannot_be_sent_to(self):
        self.client.post(self.url)
        self.vendor.refresh_from_db()
        with self.assertRaises(donations.DonationSendError):
            donations.send_request(self.vendor, subject="Hi", body="Please", user=self.admin)

    def test_an_admin_cannot_clear_an_unsubscribe_through_the_form(self):
        self.client.post(self.url)
        self.vendor.refresh_from_db()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk}),
            {
                "name": "Fishy Business",
                "contact_name": "",
                "email": "pat@fishybusiness.example",
                "status": DonationVendor.STATUS_INTERESTED,
                "followup_due": "",
                "context": "",
            },
        )
        self.assertIn(response.status_code, (200, 302))
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.unsubscribed)
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_DO_NOT_CONTACT)

    def test_unsubscribing_twice_is_harmless(self):
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DonationUnsubscribe.objects.filter(email=self.vendor.email).count(), 1)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class DonationViewAccessTests(DonationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.list_url = reverse("club_donation_vendors", kwargs={"slug": self.club.slug})

    def test_an_admin_sees_the_vendor_table(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fishy Business")

    def test_an_outsider_is_refused(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_a_logged_out_visitor_is_sent_to_log_in(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_a_view_only_member_is_refused(self):
        viewer = User.objects.create_user(username="don_view", password="pw", email="v@example.com")
        ClubMember.objects.create(club=self.club, user=viewer, permission_view=True)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_the_page_is_gone_when_the_feature_is_off(self):
        self.club.enable_donation_tracking = False
        self.club.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.list_url).status_code, 404)

    def test_the_status_filter_narrows_the_table(self):
        DonationVendor.objects.create(
            club=self.club, name="Promised Pets", email="p@example.com", status=DonationVendor.STATUS_PROMISED
        )
        self.client.force_login(self.admin)
        response = self.client.get(self.list_url, {"status": DonationVendor.STATUS_PROMISED})
        self.assertContains(response, "Promised Pets")
        self.assertNotContains(response, "Fishy Business")

    def test_deleted_vendors_are_hidden(self):
        self.vendor.is_deleted = True
        self.vendor.save()
        self.client.force_login(self.admin)
        response = self.client.get(self.list_url)
        self.assertNotContains(response, "Fishy Business")

    def test_the_vendor_panel_lists_their_emails(self):
        DonationEmail.objects.create(
            vendor=self.vendor,
            direction=DonationEmail.DIRECTION_INCOMING,
            subject="Re: Donation request",
            body="Sure",
            summary="They said yes",
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Re: Donation request")
        self.assertContains(response, "They said yes")

    def test_an_outsider_cannot_open_a_vendor_panel(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk}))
        self.assertEqual(response.status_code, 403)

    def test_a_vendor_can_be_added(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club_donation_vendor_create", kwargs={"slug": self.club.slug}),
            {
                "name": "New Vendor",
                "contact_name": "Alex",
                "email": "alex@new.example",
                "status": DonationVendor.STATUS_NEW,
                "followup_due": "",
                "context": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DonationVendor.objects.filter(club=self.club, name="New Vendor").exists())

    def test_a_duplicate_email_in_the_same_club_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club_donation_vendor_create", kwargs={"slug": self.club.slug}),
            {
                "name": "Copycat",
                "contact_name": "",
                "email": self.vendor.email,
                "status": DonationVendor.STATUS_NEW,
                "followup_due": "",
                "context": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already uses this email")
        self.assertFalse(DonationVendor.objects.filter(name="Copycat").exists())


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class ContactDialogTests(DonationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_contact", kwargs={"pk": self.vendor.pk})
        self.provider = self.use_provider(
            FakeProvider({"subject": "A donation for our spring auction", "body": "Dear Pat,\n\nWould you help?"})
        )
        self.client.force_login(self.admin)

    def test_step_one_asks_for_context(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "better context, better results", status_code=200)

    def test_generating_returns_an_editable_draft(self):
        response = self.client.post(self.url, {"step": "generate", "context": "They sell tanks", "last_email": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A donation for our spring auction")
        self.assertContains(response, "Would you help?")

    def test_the_typed_context_is_saved_on_the_vendor(self):
        self.client.post(self.url, {"step": "generate", "context": "They sell tanks", "last_email": ""})
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.context, "They sell tanks")

    def test_sending_records_the_email_and_closes_the_dialog(self):
        response = self.client.post(
            self.url,
            {"step": "send", "subject": "Hello", "body": "Please donate", "context": "", "last_email": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DonationEmail.objects.filter(vendor=self.vendor).exists())
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, DonationVendor.STATUS_EMAIL_SENT)

    def test_a_model_failure_is_shown_rather_than_crashing(self):
        self.use_provider(FakeProvider(error=LLMError("model is down")))
        response = self.client.post(self.url, {"step": "generate", "context": "", "last_email": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "model is down")

    def test_an_unsubscribed_vendor_cannot_be_contacted(self):
        donations.unsubscribe_vendor(self.vendor)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_outsider_cannot_open_the_dialog(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)


@isolated_cache("donations")
class DonationSettingsTests(DonationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_settings", kwargs={"slug": self.club.slug})

    @override_settings(**ROUTING_SETTINGS)
    def test_an_admin_can_change_the_settings(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            {
                "enable_donation_tracking": "on",
                "donation_email_mode": Club.DONATION_EMAIL_MODE_COPY,
                "donation_followup_days": 14,
                "donation_context": "501(c)(3) #99",
                "donation_mailing_address": "PO Box 1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_email_mode, Club.DONATION_EMAIL_MODE_COPY)
        self.assertEqual(self.club.donation_followup_days, 14)
        self.assertEqual(self.club.donation_context, "501(c)(3) #99")

    @override_settings(SES_ROUTE_EMAILS_ENABLED=False)
    def test_without_routing_the_mode_is_forced_to_copy_paste(self):
        self.client.force_login(self.admin)
        self.client.post(
            self.url,
            {
                "enable_donation_tracking": "on",
                "donation_email_mode": Club.DONATION_EMAIL_MODE_ROUTED,
                "donation_followup_days": 7,
                "donation_context": "",
                "donation_mailing_address": "",
            },
        )
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_email_mode, Club.DONATION_EMAIL_MODE_COPY)

    def test_a_member_without_edit_club_is_refused(self):
        member = User.objects.create_user(username="don_plain", password="pw", email="p@example.com")
        ClubMember.objects.create(club=self.club, user=member, permission_add_edit=True)
        self.client.force_login(member)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_toggling_the_feature_is_written_to_club_history(self):
        self.client.force_login(self.admin)
        self.client.post(
            self.url,
            {
                "donation_email_mode": Club.DONATION_EMAIL_MODE_COPY,
                "donation_followup_days": 7,
                "donation_context": "",
                "donation_mailing_address": "",
            },
        )
        self.club.refresh_from_db()
        self.assertFalse(self.club.enable_donation_tracking)
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, applies_to="DONATIONS", action__contains="off").exists()
        )


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class DonationContactOnEmailSettingsTests(DonationTestMixin, TestCase):
    """The Donation contact row on the email settings page."""

    def form(self):
        from auctions.forms import ClubEmailSettingsForm

        self.club.refresh_from_db()
        return ClubEmailSettingsForm(instance=self.club, show_email_routing=True)

    def test_it_is_editable_when_donation_tracking_is_on(self):
        field = self.form().fields["donation_email_member"]
        self.assertFalse(field.disabled)
        self.assertEqual(field.label, "Donation replies")

    def test_it_is_greyed_out_when_donation_tracking_is_off(self):
        self.club.enable_donation_tracking = False
        self.club.save()
        form = self.form()
        self.assertTrue(form.fields["donation_email_member"].disabled)
        self.assertIn("disabled", str(form["donation_email_member"]))
        self.assertIn("Turn on donation tracking", form.fields["donation_email_member"].help_text)

    def test_it_recommends_leaving_the_contact_unset(self):
        self.assertIn("Leave this blank", self.form().fields["donation_email_member"].help_text)

    @override_settings(SES_ROUTE_EMAILS_ENABLED=False)
    def test_it_is_absent_when_the_site_has_no_email_routing(self):
        from auctions.forms import ClubEmailSettingsForm

        form = ClubEmailSettingsForm(instance=self.club, show_email_routing=False)
        self.assertNotIn("donation_email_member", form.fields)


@isolated_cache("donations")
class TextHandlingTests(TestCase):
    def test_html_is_reduced_to_text(self):
        self.assertEqual(donations.strip_email_html("<p>Hello</p><p>World</p>"), "Hello\n\nWorld")

    def test_scripts_and_styles_are_dropped(self):
        cleaned = donations.strip_email_html("<style>p{color:red}</style><script>evil()</script><p>Hi</p>")
        self.assertNotIn("evil", cleaned)
        self.assertNotIn("color:red", cleaned)
        self.assertIn("Hi", cleaned)

    def test_entities_are_unescaped(self):
        self.assertEqual(donations.strip_email_html("<p>Tom &amp; Jerry</p>"), "Tom & Jerry")

    def test_a_quoted_reply_chain_is_cut(self):
        body = "Yes, happy to help.\n\nOn Mon, Jan 1, 2026 at 9:00 AM Club wrote:\n> Please donate"
        self.assertEqual(donations.strip_quoted_reply(body), "Yes, happy to help.")

    def test_a_body_that_is_only_quoted_text_is_kept(self):
        body = "-----Original Message-----\n> Please donate"
        self.assertEqual(donations.strip_quoted_reply(body), body.strip())

    def test_truncation_marks_where_it_cut(self):
        self.assertTrue(donations.truncate_for_model("word " * 1000, 100).endswith("[truncated]"))

    def test_short_text_is_left_alone(self):
        self.assertEqual(donations.truncate_for_model("Hello", 100), "Hello")

    def test_a_sender_header_is_reduced_to_the_address(self):
        self.assertEqual(donations.sender_address("Pat Smith <pat@example.com>"), "pat@example.com")
