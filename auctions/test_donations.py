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
from django.utils.html import escape

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

    def __init__(self, payload=None, error=None, configured=True):
        super().__init__(model="fake-model", api_key="key")
        self.payload = payload if payload is not None else {}
        self.error = error
        self.configured = configured
        self.calls = []

    def is_configured(self):
        return self.configured

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
            club=self.club, name="Treasurer", email="treasurer@example.com", permission_manage_donations=True
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

    @override_settings(**ROUTING_SETTINGS)
    def test_the_resolve_endpoint_passes_the_donation_kind_through(self):
        """The seam the Lambda actually reads.

        ``resolve_routing_info`` marking an address as a donation is worth nothing if the view in
        front of it drops the flag: the Lambda decides *both* whether to post the body back here
        and whether an empty recipient means "forward to nobody" from this one field.  Without it
        no vendor reply is ever recorded, and a club with no donation contact has its vendors'
        replies forwarded to the site's fallback inbox instead.
        """
        url = reverse("inbound_email_routing")
        address = f"{self.club.slug}-donations-{self.vendor.routing_key}"
        response = self.client.get(url, {"address": address}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["kind"], "donation")
        self.assertEqual(payload["vendor_key"], self.vendor.routing_key)
        self.assertEqual(payload["recipient"], "")

    @override_settings(**ROUTING_SETTINGS)
    def test_the_resolve_endpoint_leaves_kind_off_ordinary_aliases(self):
        url = reverse("inbound_email_routing")
        response = self.client.get(url, {"address": "info"}, HTTP_X_ROUTING_SECRET="test-secret")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kind", response.json())


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
        for _ in range(donations.MAX_DONATION_EMAILS_PER_DAY):
            donations.draft_request(self.vendor)
        with self.assertRaises(LLMError):
            donations.draft_request(self.vendor)

    def test_the_draft_and_summary_budgets_are_separate(self):
        for _ in range(donations.MAX_DONATION_EMAILS_PER_DAY):
            donations.draft_request(self.vendor)
        # The incoming budget is untouched, so a reply arriving now is still summarized.
        self.assertTrue(donations.check_rate_limit(self.club, "incoming"))

    def test_a_draft_spends_the_clubs_daily_allowance(self):
        """Nothing was sent, but the call was made and paid for."""
        donations.draft_request(self.vendor)
        self.assertEqual(donations.donation_email_quota(self.club).used, 1)

    def test_a_draft_that_is_then_sent_is_only_counted_once(self):
        donations.draft_request(self.vendor)
        donations.send_request(self.vendor, subject="Hello", body="Please donate", user=self.admin)
        self.assertEqual(donations.donation_email_quota(self.club).used, 1)

    def test_a_failed_draft_still_counts(self):
        """The tokens are spent by the time the model falls over."""
        self.use_provider(FakeProvider(error=LLMError("model is down")))
        with self.assertRaises(LLMError):
            donations.draft_request(self.vendor)
        self.assertEqual(donations.donation_email_quota(self.club).used, 1)

    def test_a_site_with_no_model_configured_charges_nothing(self):
        self.use_provider(FakeProvider(payload={"subject": "s", "body": "b"}, configured=False))
        with self.assertRaises(LLMError):
            donations.draft_request(self.vendor)
        self.assertEqual(donations.donation_email_quota(self.club).used, 0)

    def test_drafting_stops_once_the_day_is_spent_on_sending(self):
        for index in range(donations.MAX_DONATION_EMAILS_PER_DAY):
            DonationEmail.objects.create(
                vendor=self.vendor,
                direction=DonationEmail.DIRECTION_OUTGOING,
                subject=f"Request {index}",
                body="Please donate",
            )
        with self.assertRaises(LLMError) as caught:
            donations.draft_request(self.vendor)
        self.assertIn("limit", str(caught.exception))

    def test_a_follow_up_to_our_own_email_is_prompted_as_a_nudge(self):
        donations.draft_request(self.vendor, last_email="Our first request", last_email_is_outgoing=True)
        prompt = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn("The last email the club sent this vendor", prompt)
        self.assertIn("Our first request", prompt)

    def test_a_reply_from_the_vendor_is_prompted_as_a_reply(self):
        donations.draft_request(self.vendor, last_email="What did you have in mind?")
        prompt = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn("Their last message to the club", prompt)

    def test_the_three_kinds_of_email_are_told_apart(self):
        self.assertEqual(donations.draft_mode(), donations.DRAFT_MODE_FIRST)
        self.assertEqual(donations.draft_mode("   "), donations.DRAFT_MODE_FIRST)
        self.assertEqual(donations.draft_mode("ours", last_email_is_outgoing=True), donations.DRAFT_MODE_FOLLOWUP)
        self.assertEqual(donations.draft_mode("theirs"), donations.DRAFT_MODE_REPLY)

    def test_a_reply_is_not_briefed_as_a_donation_request(self):
        """The bug this guards: one first-approach system prompt for all three emails.

        A heading in the user turn asking for a reply loses to a system prompt that says "you write
        donation request emails, say who the club is, make the ask, say what the business gets" --
        so the answer to "sure, what's the next step?" came back reading like a fresh solicitation.
        """
        donations.draft_request(self.vendor, last_email="Sure, what's the next step?")
        system = self.provider.calls[0]["system"]
        self.assertIn("NOT a donation request", system)
        self.assertIn("Answer what they actually asked", system)
        self.assertNotIn("Make one clear, modest ask", system)
        self.assertNotIn("what the business gets: their name in front of", system)

    def test_a_first_approach_still_gets_the_pitch(self):
        donations.draft_request(self.vendor)
        system = self.provider.calls[0]["system"]
        self.assertIn("Make one clear, modest ask", system)
        self.assertIn("first approach", system)

    def test_a_nudge_is_briefed_as_a_nudge(self):
        donations.draft_request(self.vendor, last_email="Our first request", last_email_is_outgoing=True)
        system = self.provider.calls[0]["system"]
        self.assertIn("nudge, not a second pitch", system)
        self.assertNotIn("Make one clear, modest ask", system)

    def test_every_kind_keeps_the_rules_that_are_not_negotiable(self):
        for mode in (donations.DRAFT_MODE_FIRST, donations.DRAFT_MODE_FOLLOWUP, donations.DRAFT_MODE_REPLY):
            system = donations.draft_system_prompt(mode)
            with self.subTest(mode=mode):
                self.assertIn("tax deductible", system)
                self.assertIn('"subject"', system)
                self.assertIn("unsubscribe line in the body", system)

    def test_a_reply_may_put_the_address_in_the_body_and_a_first_approach_may_not(self):
        """A vendor asking what happens next is answered in the body, not in the fine print."""
        donations.draft_request(self.vendor, last_email="Sure, what's the next step?")
        reply_prompt = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn("Put it in the body", reply_prompt)
        self.assertIn("1 Main St", reply_prompt)

        donations.draft_request(self.vendor)
        first_prompt = self.provider.calls[1]["messages"][0]["content"]
        self.assertIn("Do not repeat it in the body", first_prompt)


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
        self.assertIn("This message is a donation request from:", email.body)
        self.assertIn("Test Aquarium Society", email.body)

    def test_a_club_named_in_its_own_address_is_not_named_twice(self):
        """Most clubs type their name at the top of the address, and it read as a stutter."""
        self.club.donation_mailing_address = "Test Aquarium Society\n1 Main St\nSpringfield, IL 62701"
        self.club.save()
        self.vendor.refresh_from_db()
        footer = donations.unsubscribe_footer(self.vendor)
        self.assertEqual(footer.count("Test Aquarium Society"), 1)
        self.assertIn("Test Aquarium Society\n1 Main St", footer)

    def test_a_club_missing_from_its_own_address_is_still_named(self):
        self.club.donation_mailing_address = "1 Main St\nSpringfield, IL 62701"
        self.club.save()
        self.vendor.refresh_from_db()
        footer = donations.unsubscribe_footer(self.vendor)
        self.assertIn("Test Aquarium Society\n1 Main St", footer)

    def test_the_draft_prompt_keeps_the_address_out_of_the_body(self):
        """It is in the footer of every email already; repeating it is what made them long."""
        self.assertIn("Never put the club's mailing address in the body", donations._DRAFT_SYSTEM_PROMPT)
        prompt = donations.build_draft_prompt(self.vendor)
        self.assertIn("only to be used if they have asked where to send a donation", prompt)

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

    def test_a_member_with_the_donation_permission_sees_the_vendor_table(self):
        staffer = User.objects.create_user(username="don_staff", password="pw", email="s@example.com")
        ClubMember.objects.create(club=self.club, user=staffer, permission_manage_donations=True)
        self.client.force_login(staffer)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fishy Business")

    def test_the_donation_permission_alone_still_gets_a_sidebar(self):
        """The sidebar link is the only way in, so it has to render for a donations-only member."""
        staffer = User.objects.create_user(username="don_only", password="pw", email="o@example.com")
        ClubMember.objects.create(club=self.club, user=staffer, permission_manage_donations=True)
        self.client.force_login(staffer)
        response = self.client.get(self.list_url)
        # The offcanvas id only exists when club_sidebar_can_view let the sidebar render at all.
        self.assertContains(response, 'id="clubSidebar"')
        self.assertContains(response, "Donation Tracking")

    def test_managing_the_member_list_no_longer_grants_donations(self):
        """Membership managers used to get these pages for free; donations is its own job now."""
        manager = User.objects.create_user(username="don_mgr", password="pw", email="m@example.com")
        ClubMember.objects.create(club=self.club, user=manager, permission_add_edit=True)
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        vendor_url = reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk})
        self.assertEqual(self.client.get(vendor_url).status_code, 403)

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

    def test_a_model_failure_keeps_the_context_that_was_typed(self):
        """Making them retype what they told the model, because the model went down, is the worst of both."""
        self.use_provider(FakeProvider(error=LLMError("model is down")))
        response = self.client.post(
            self.url,
            {"step": "generate", "context": "They sold us tanks last year", "last_email": ""},
        )
        self.assertContains(response, "model is down")
        self.assertContains(response, "They sold us tanks last year")

    def test_an_unsubscribed_vendor_cannot_be_contacted(self):
        donations.unsubscribe_vendor(self.vendor)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_outsider_cannot_open_the_dialog(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_opening_the_dialog_costs_nothing(self):
        """Only asking for a draft spends the allowance; looking at the form doesn't."""
        self.client.get(self.url)
        self.assertEqual(donations.donation_email_quota(self.club).used, 0)

    def test_a_draft_the_admin_walks_away_from_is_still_counted(self):
        """There is no Cancel button to press in a test: closing the modal posts nothing at all."""
        self.client.post(self.url, {"step": "generate", "context": "", "last_email": ""})
        self.assertEqual(donations.donation_email_quota(self.club).used, 1)
        self.assertFalse(DonationEmail.objects.filter(vendor=self.vendor).exists())


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class FollowUpEmailTests(DonationTestMixin, TestCase):
    """The second email to a vendor: what it is written from, and what it is called."""

    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_contact", kwargs={"pk": self.vendor.pk})
        self.provider = self.use_provider(
            FakeProvider({"subject": "A brand new subject line", "body": "Dear Pat,\n\nAny thoughts?"})
        )
        self.client.force_login(self.admin)

    def send_first_request(self):
        return donations.send_request(
            self.vendor, subject="A donation for our spring auction", body="Please donate", user=self.admin
        )

    def receive_reply(self, subject="Re: A donation for our spring auction", message_id="<vendor-1@example.com>"):
        return donations.record_incoming(
            self.vendor,
            sender=self.vendor.email,
            recipients="club@example.com",
            subject=subject,
            body="What did you have in mind?",
            message_id=message_id,
        )[0]

    def open_dialog(self):
        return self.client.get(self.url)

    def test_the_email_we_already_sent_prefills_the_last_email_box(self):
        """The commonest follow-up of all: they never wrote back, so our own request is the context."""
        self.send_first_request()
        self.assertContains(self.open_dialog(), "Please donate")

    def test_our_own_footer_is_not_fed_back_in(self):
        self.send_first_request()
        response = self.open_dialog()
        self.assertNotContains(response, donations.FOOTER_MARKER)

    def test_a_reply_from_the_vendor_wins_over_our_own_email(self):
        self.send_first_request()
        self.receive_reply()
        response = self.open_dialog()
        self.assertContains(response, "What did you have in mind?")
        self.assertContains(response, "Their last message")

    def test_the_box_says_which_way_the_last_email_went(self):
        self.send_first_request()
        response = self.open_dialog()
        self.assertContains(response, "The last email you sent them")
        self.assertContains(response, f'value="{DonationEmail.DIRECTION_OUTGOING}"')

    def test_our_own_email_is_prompted_as_a_nudge_not_a_first_approach(self):
        self.send_first_request()
        self.client.post(
            self.url,
            {
                "step": "generate",
                "context": "",
                "last_email": "Please donate",
                "last_email_direction": DonationEmail.DIRECTION_OUTGOING,
            },
        )
        prompt = self.provider.calls[0]["messages"][0]["content"]
        self.assertIn("The last email the club sent this vendor", prompt)

    def test_a_second_email_keeps_the_subject_of_the_first(self):
        self.send_first_request()
        response = self.client.post(self.url, {"step": "generate", "context": "", "last_email": "Please donate"})
        self.assertContains(response, "RE: A donation for our spring auction")
        self.assertNotContains(response, "A brand new subject line")

    def test_a_reply_does_not_stack_up_re_prefixes(self):
        self.send_first_request()
        self.receive_reply()
        response = self.client.post(self.url, {"step": "generate", "context": "", "last_email": "Anything?"})
        self.assertContains(response, "RE: A donation for our spring auction")
        self.assertNotContains(response, "RE: Re:")

    def test_a_first_email_keeps_the_subject_the_model_wrote(self):
        response = self.client.post(self.url, {"step": "generate", "context": "", "last_email": ""})
        self.assertContains(response, "A brand new subject line")

    def test_a_follow_up_is_threaded_onto_the_vendors_own_message(self):
        from post_office.models import Email

        self.receive_reply(message_id="<vendor-42@example.com>")
        donations.send_request(self.vendor, subject="RE: Donations", body="Thanks for getting back", user=self.admin)
        queued = Email.objects.order_by("-id").first()
        self.assertEqual(queued.headers.get("In-Reply-To"), "<vendor-42@example.com>")
        self.assertEqual(queued.headers.get("References"), "<vendor-42@example.com>")

    def test_a_message_id_that_arrived_without_brackets_is_still_a_valid_header(self):
        from post_office.models import Email

        self.receive_reply(message_id="vendor-43@example.com")
        donations.send_request(self.vendor, subject="RE: Donations", body="Thanks", user=self.admin)
        queued = Email.objects.order_by("-id").first()
        self.assertEqual(queued.headers.get("In-Reply-To"), "<vendor-43@example.com>")

    def test_a_first_email_is_not_threaded_onto_anything(self):
        from post_office.models import Email

        donations.send_request(self.vendor, subject="Donations", body="Please donate", user=self.admin)
        queued = Email.objects.order_by("-id").first()
        self.assertNotIn("In-Reply-To", queued.headers)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class VendorListOrderTests(DonationTestMixin, TestCase):
    """What order the vendor table comes back in."""

    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_vendors", kwargs={"slug": self.club.slug})
        self.client.force_login(self.admin)
        now = timezone.now()
        self.vendor.followup_due = now + datetime.timedelta(days=2)
        self.vendor.save()
        self.soonest = DonationVendor.objects.create(
            club=self.club, name="Aaa Overdue", email="a@example.com", followup_due=now - datetime.timedelta(days=5)
        )
        self.latest = DonationVendor.objects.create(
            club=self.club, name="Zzz Later", email="z@example.com", followup_due=now + datetime.timedelta(days=30)
        )
        self.no_date = DonationVendor.objects.create(club=self.club, name="Aaa No Date", email="n@example.com")

    def listed(self):
        return list(self.client.get(self.url).context["table"].data)

    def test_the_most_overdue_vendor_is_first(self):
        self.assertEqual(self.listed()[:3], [self.soonest, self.vendor, self.latest])

    def test_vendors_with_no_follow_up_date_come_last(self):
        """Nobody is waiting on them -- they unsubscribed, or the date was cleared by hand."""
        self.assertEqual(self.listed()[-1], self.no_date)

    def test_an_unsubscribed_vendor_drops_to_the_bottom(self):
        donations.unsubscribe_vendor(self.soonest)
        listed = self.listed()
        self.assertIn(self.soonest, listed[-2:])
        self.assertEqual(listed[0], self.vendor)


class LatestReplyColumnTests(DonationTestMixin, TestCase):
    """The vendor table's one-line summary of what each vendor last said.

    The summary itself is written when the reply arrives (see :class:`IncomingStatusRulesTests`);
    this is about getting it in front of somebody without opening every vendor in turn.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_vendors", kwargs={"slug": self.club.slug})
        self.client.force_login(self.admin)

    def reply(self, summary, *, days_ago=0, direction=DonationEmail.DIRECTION_INCOMING):
        return DonationEmail.objects.create(
            vendor=self.vendor,
            direction=direction,
            subject="Re: Donation request",
            body="...",
            summary=summary,
            date=timezone.now() - datetime.timedelta(days=days_ago),
        )

    def test_the_summary_of_the_latest_reply_is_in_the_table(self):
        self.reply("They will donate a 20 gallon tank.")
        self.assertContains(self.client.get(self.url), "They will donate a 20 gallon tank.")

    def test_only_the_newest_reply_is_shown(self):
        self.reply("An older answer.", days_ago=5)
        self.reply("What they say now.")
        response = self.client.get(self.url)
        self.assertContains(response, "What they say now.")
        self.assertNotContains(response, "An older answer.")

    def test_our_own_message_is_not_mistaken_for_their_reply(self):
        self.reply("Never written for outgoing mail.", direction=DonationEmail.DIRECTION_OUTGOING)
        self.assertNotContains(self.client.get(self.url), "Never written for outgoing mail.")

    def test_a_vendor_who_has_not_replied_shows_a_dash(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Latest reply")

    def test_a_long_summary_is_trimmed_but_kept_in_full_on_hover(self):
        summary = "They will donate " + ("a very large tank " * 20)
        summary = summary[: DonationEmail._meta.get_field("summary").max_length].strip()
        self.reply(summary)
        content = self.client.get(self.url).content.decode()
        self.assertIn("\u2026", content)
        self.assertIn(escape(summary), content)


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
                "donation_mailing_address": "PO Box 1",
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
        """The recommendation stays on the field; the reasoning for it moved to a note above.

        Both used to say the whole thing, on one screen, which made the recommendation easier to
        skim past rather than harder. The note is on the email settings page (where the field is)
        rather than the donation settings page (where the field isn't), and only when the club
        actually runs donation tracking.
        """
        self.assertIn("Leave blank (recommended)", self.form().fields["donation_email_member"].help_text)

    @override_settings(**ROUTING_SETTINGS)
    def test_the_reasoning_is_on_the_email_page_when_donation_tracking_is_on(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("club_email_settings", kwargs={"slug": self.club.slug}))
        self.assertContains(page, "Leave the donation contact blank")
        self.assertContains(page, "never reaches")

    @override_settings(**ROUTING_SETTINGS)
    def test_a_club_that_does_not_track_donations_is_not_warned_about_it(self):
        self.club.enable_donation_tracking = False
        self.club.save()
        self.client.force_login(self.admin)
        page = self.client.get(reverse("club_email_settings", kwargs={"slug": self.club.slug}))
        self.assertNotContains(page, "Leave the donation contact blank")

    @override_settings(**ROUTING_SETTINGS)
    def test_the_donation_page_says_where_replies_go_and_where_to_change_it(self):
        self.club.donation_email_member = ClubMember.objects.create(
            club=self.club, name="Donations", email="don@example.com", permission_manage_donations=True
        )
        self.club.save()
        self.client.force_login(self.admin)
        page = self.client.get(reverse("club_donation_settings", kwargs={"slug": self.club.slug}))
        self.assertContains(page, "currently also forwarded to")
        self.assertContains(page, reverse("club_email_settings", kwargs={"slug": self.club.slug}))

    def test_only_members_who_manage_donations_can_be_the_contact(self):
        """Offering anyone else would name a recipient donation_email_recipient then refuses."""
        staffer = ClubMember.objects.create(
            club=self.club, name="Donations", email="don@example.com", permission_manage_donations=True
        )
        manager = ClubMember.objects.create(
            club=self.club, name="Membership", email="mem@example.com", permission_add_edit=True
        )
        choices = list(self.form().fields["donation_email_member"].queryset)
        self.assertIn(staffer, choices)
        self.assertNotIn(manager, choices)

    def test_a_membership_manager_is_no_longer_a_valid_recipient(self):
        manager = ClubMember.objects.create(
            club=self.club, name="Membership", email="mem@example.com", permission_add_edit=True
        )
        self.club.donation_email_member = manager
        self.club.save()
        self.club.refresh_from_db()
        self.assertIsNone(self.club.donation_email_recipient)


@isolated_cache("donations")
class DonationPermissionHelpTextTests(DonationTestMixin, TestCase):
    """The Manage donations checkbox in the member permissions dialog."""

    def field(self):
        from auctions.forms import ClubMemberPermissionsForm

        self.club.refresh_from_db()
        member = ClubMember.objects.create(club=self.club, name="Somebody")
        return ClubMemberPermissionsForm(instance=member).fields["permission_manage_donations"]

    def test_it_describes_the_permission_when_tracking_is_on(self):
        self.assertEqual(self.field().help_text, "Allow the user to add and email vendors and manage club donations")

    def test_it_says_where_to_switch_tracking_on_when_it_is_off(self):
        self.club.enable_donation_tracking = False
        self.club.save()
        self.assertEqual(self.field().help_text, "Donation tracking is off right now, enable it in setup")

    @override_settings(SES_ROUTE_EMAILS_ENABLED=False)
    def test_it_is_absent_when_the_site_has_no_email_routing(self):
        from auctions.forms import ClubEmailSettingsForm

        form = ClubEmailSettingsForm(instance=self.club, show_email_routing=False)
        self.assertNotIn("donation_email_member", form.fields)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class DailyEmailLimitTests(DonationTestMixin, TestCase):
    """The cap on donation emails a club may send in one day."""

    def fill_the_day(self, count=None):
        """Record *count* outgoing emails today without going through the sending path."""
        count = donations.MAX_DONATION_EMAILS_PER_DAY if count is None else count
        for index in range(count):
            DonationEmail.objects.create(
                vendor=self.vendor,
                direction=DonationEmail.DIRECTION_OUTGOING,
                subject=f"Request {index}",
                body="Please donate",
            )

    def test_the_quota_counts_what_went_out_today(self):
        self.fill_the_day(3)
        quota = donations.donation_email_quota(self.club)
        self.assertEqual(quota.used, 3)
        self.assertEqual(quota.remaining, donations.MAX_DONATION_EMAILS_PER_DAY - 3)
        self.assertFalse(quota.exhausted)

    def test_yesterdays_emails_do_not_count(self):
        self.fill_the_day()
        DonationEmail.objects.update(date=timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(donations.donation_email_quota(self.club).used, 0)

    def test_incoming_replies_do_not_count(self):
        DonationEmail.objects.create(
            vendor=self.vendor, direction=DonationEmail.DIRECTION_INCOMING, subject="Re:", body="Sure"
        )
        self.assertEqual(donations.donation_email_quota(self.club).used, 0)

    def test_another_clubs_emails_do_not_count(self):
        other_club = Club.objects.create(name="Other Club", enable_donation_tracking=True)
        other_vendor = DonationVendor.objects.create(club=other_club, name="Elsewhere", email="e@example.com")
        DonationEmail.objects.create(
            vendor=other_vendor, direction=DonationEmail.DIRECTION_OUTGOING, subject="Hi", body="Please"
        )
        self.assertEqual(donations.donation_email_quota(self.club).used, 0)

    def test_sending_past_the_limit_is_refused(self):
        self.fill_the_day()
        with self.assertRaises(donations.DonationSendError) as caught:
            donations.send_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        self.assertIn("limit", str(caught.exception))
        self.assertEqual(
            DonationEmail.objects.filter(direction=DonationEmail.DIRECTION_OUTGOING).count(),
            donations.MAX_DONATION_EMAILS_PER_DAY,
        )

    def test_copy_paste_mode_is_held_to_the_same_limit(self):
        """Copying a request out is still asking a vendor for something, so it counts."""
        self.club.donation_email_mode = Club.DONATION_EMAIL_MODE_COPY
        self.club.save()
        self.vendor.refresh_from_db()
        self.fill_the_day()
        with self.assertRaises(donations.DonationSendError):
            donations.record_copied_request(self.vendor, subject="Hi", body="Please donate", user=self.admin)

    def test_the_contact_dialog_closes_itself_instead_of_offering_a_send(self):
        self.fill_the_day()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("club_donation_contact", kwargs={"pk": self.vendor.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "limit")
        self.assertNotContains(response, 'hx-vals=\'{"step": "send"}\'')

    def test_the_dialog_will_not_send_even_when_posted_to_directly(self):
        self.fill_the_day()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club_donation_contact", kwargs={"pk": self.vendor.pk}),
            {"step": "send", "subject": "Hello", "body": "Please donate", "context": "", "last_email": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            DonationEmail.objects.filter(direction=DonationEmail.DIRECTION_OUTGOING).count(),
            donations.MAX_DONATION_EMAILS_PER_DAY,
        )

    def test_the_table_button_says_when_the_limit_lifts(self):
        self.fill_the_day()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("club_donation_vendors", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "donation-contact-blocked")
        self.assertContains(response, "Try again")

    def test_the_vendor_page_shows_how_much_is_left(self):
        self.fill_the_day(4)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("club_donation_vendors", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Donation emails today")
        self.assertContains(response, f"4 of {donations.MAX_DONATION_EMAILS_PER_DAY}")

    def test_a_blocked_vendor_still_says_what_is_wrong_with_the_vendor(self):
        """Both kinds of "no" exist at once; the one about this vendor is the more useful."""
        self.fill_the_day()
        self.vendor.email = ""
        self.vendor.save()
        self.assertEqual(donations.contact_blocked_reason(self.vendor), "Add an email address for this vendor first")

    def test_the_bar_never_runs_past_its_track(self):
        self.fill_the_day(donations.MAX_DONATION_EMAILS_PER_DAY + 10)
        quota = donations.donation_email_quota(self.club)
        self.assertEqual(quota.percent_used, 100)
        self.assertEqual(quota.remaining, 0)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class ReviewStepButtonsTests(DonationTestMixin, TestCase):
    """The buttons on the drafted-email step, which is where an email is actually committed."""

    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_contact", kwargs={"pk": self.vendor.pk})
        self.client.force_login(self.admin)
        self.use_provider(FakeProvider({"subject": "A donation for our auction", "body": "Dear Pat,"}))

    def review(self):
        return self.client.post(self.url, {"step": "generate", "context": "", "last_email": ""})

    def test_a_club_that_sends_from_here_gets_a_send_button(self):
        response = self.review()
        self.assertContains(response, 'hx-vals=\'{"step": "send"}\'')
        self.assertContains(response, "Send")

    def test_a_copy_paste_club_gets_a_record_button_instead(self):
        self.club.donation_email_mode = Club.DONATION_EMAIL_MODE_COPY
        self.club.save()
        response = self.review()
        self.assertContains(response, 'hx-vals=\'{"step": "send"}\'')
        self.assertContains(response, "Copy &amp; record")

    def test_there_is_no_separate_copy_button_or_rewrite(self):
        """One way to commit an email, in either mode: send it, or copy-and-record it."""
        for mode in (Club.DONATION_EMAIL_MODE_ROUTED, Club.DONATION_EMAIL_MODE_COPY):
            self.club.donation_email_mode = mode
            self.club.save()
            response = self.review()
            self.assertNotContains(response, "Copy to clipboard", msg_prefix=mode)
            self.assertNotContains(response, "Rewrite", msg_prefix=mode)
            self.assertNotContains(response, 'hx-vals=\'{"step": "generate"}\'', msg_prefix=mode)

    def test_the_footer_is_not_clipped_out_of_a_scrollable_modal(self):
        """The buttons live in a <form> inside .modal-content, which Bootstrap alone would hide.

        See the .modal-dialog-scrollable rule in auction_site.css: without it a long email pushes
        the footer past the content box and it is silently cropped away.
        """
        response = self.review()
        self.assertContains(response, "modal-dialog-scrollable")
        self.assertContains(response, '<div class="modal-footer flex-wrap">')

    def test_a_step_that_makes_no_sense_keeps_what_was_typed(self):
        response = self.client.post(self.url, {"context": "They sell tanks", "last_email": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "They sell tanks")

    def test_an_edited_email_is_what_gets_recorded(self):
        self.client.post(
            self.url,
            {"step": "send", "subject": "Hello", "body": "Please donate a tank", "context": "", "last_email": ""},
        )
        email = DonationEmail.objects.get(vendor=self.vendor)
        self.assertIn("Please donate a tank", email.body)


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class VendorFormTests(DonationTestMixin, TestCase):
    """Adding and editing a vendor through the modal."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.create_url = reverse("club_donation_vendor_create", kwargs={"slug": self.club.slug})

    def form(self, instance=None):
        from auctions.forms import DonationVendorForm

        return DonationVendorForm(instance=instance, club=self.club)

    def test_a_new_vendor_is_not_asked_when_to_follow_up(self):
        self.assertNotIn("followup_due", self.form().fields)
        self.assertIn("followup_due", self.form(instance=self.vendor).fields)

    def test_a_new_vendor_is_due_for_a_follow_up_straight_away(self):
        before = timezone.now()
        self.client.post(
            self.create_url,
            {"name": "New Vendor", "contact_name": "", "email": "new@example.com", "status": "new", "context": ""},
        )
        vendor = DonationVendor.objects.get(name="New Vendor")
        self.assertIsNotNone(vendor.followup_due)
        self.assertGreaterEqual(vendor.followup_due, before)
        self.assertTrue(vendor.is_followup_due)

    def test_the_follow_up_date_uses_a_native_calendar(self):
        """The site's datepicker widget can't start itself inside a modal, so this one is plain."""
        response = self.client.get(reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk}) + "?edit=1")
        self.assertContains(response, 'type="date"')
        self.assertNotContains(response, "data-dbdp-config")

    def test_a_picked_date_is_stored_as_a_datetime(self):
        self.client.post(
            reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk}),
            {
                "name": self.vendor.name,
                "contact_name": "",
                "email": self.vendor.email,
                "status": DonationVendor.STATUS_NEW,
                "followup_due": "2026-09-15",
                "context": "",
            },
        )
        self.vendor.refresh_from_db()
        self.assertEqual(timezone.localtime(self.vendor.followup_due).date(), datetime.date(2026, 9, 15))

    def test_an_existing_date_comes_back_as_the_day_it_falls_on(self):
        self.vendor.followup_due = timezone.now()
        self.vendor.save()
        expected = timezone.localtime(self.vendor.followup_due).date()
        self.assertEqual(self.form(instance=self.vendor).initial["followup_due"], expected)

    def test_the_edit_dialog_has_one_set_of_buttons(self):
        """Crispy draws Cancel/Save; a second footer holding another Close is just clutter."""
        response = self.client.get(reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk}) + "?edit=1")
        self.assertNotContains(response, 'data-modal-close-action="none">Close')


@isolated_cache("donations")
@override_settings(**ROUTING_SETTINGS)
class UnsubscribeIsFinalTests(DonationTestMixin, TestCase):
    """A vendor who opts out is never written to again, by any route this site offers."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        donations.unsubscribe_vendor(self.vendor)
        self.vendor.refresh_from_db()

    def test_neither_way_of_sending_will_touch_them(self):
        for send in (donations.send_request, donations.record_copied_request):
            with self.assertRaises(donations.DonationSendError):
                send(self.vendor, subject="Hi", body="Please donate", user=self.admin)
        self.assertEqual(DonationEmail.objects.count(), 0)

    def test_the_form_will_not_let_an_admin_edit_their_way_back(self):
        from auctions.forms import DonationVendorForm

        form = DonationVendorForm(instance=self.vendor, club=self.club)
        self.assertTrue(form.fields["status"].disabled)
        self.assertTrue(form.fields["email"].disabled)

    def test_removing_and_re_adding_them_does_not_reset_it(self):
        self.vendor.is_deleted = True
        self.vendor.save()
        self.client.post(
            reverse("club_donation_vendor_create", kwargs={"slug": self.club.slug}),
            {
                "name": "Fishy Business",
                "contact_name": "",
                "email": "pat@fishybusiness.example",
                "status": DonationVendor.STATUS_NEW,
                "context": "",
            },
        )
        added = DonationVendor.objects.filter(name="Fishy Business", is_deleted=False).first()
        self.assertIsNotNone(added)
        self.assertTrue(added.unsubscribed)
        self.assertFalse(added.can_be_contacted)

    def test_the_opt_out_line_speaks_to_the_vendor(self):
        footer = donations.unsubscribe_footer(self.vendor)
        self.assertIn("If you don't want to be contacted again", footer)
        self.assertIn(self.vendor.unsubscribe_url, footer)


@isolated_cache("donations")
class DonationSettingsFormTests(DonationTestMixin, TestCase):
    """What the settings page insists on before a club can start asking for donations."""

    def setUp(self):
        super().setUp()
        self.url = reverse("club_donation_settings", kwargs={"slug": self.club.slug})
        self.client.force_login(self.admin)

    def post(self, **overrides):
        data = {
            "enable_donation_tracking": "on",
            "donation_email_mode": Club.DONATION_EMAIL_MODE_COPY,
            "donation_followup_days": 7,
            "donation_context": "",
            "donation_mailing_address": "PO Box 1\nSpringfield, IL 62701",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_a_mailing_address_is_required_to_turn_it_on(self):
        response = self.post(donation_mailing_address="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "postal address")
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_mailing_address, "TAS\n1 Main St\nSpringfield, IL 62701")

    def test_turning_it_off_does_not_need_an_address(self):
        response = self.post(enable_donation_tracking="", donation_mailing_address="")
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertFalse(self.club.enable_donation_tracking)

    def test_saving_lands_on_the_page_the_settings_were_for(self):
        response = self.post()
        self.assertRedirects(
            response,
            reverse("club_donation_vendors", kwargs={"slug": self.club.slug}),
            fetch_redirect_response=False,
        )

    def test_turning_it_off_stays_on_the_settings_page(self):
        """The vendor page 404s once tracking is off, so there is nowhere else to go."""
        response = self.post(enable_donation_tracking="")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

    def test_the_terms_are_readable_from_the_page(self):
        response = self.client.get(self.url)
        self.assertContains(response, "terms and conditions")
        self.assertContains(response, "donation-terms-modal")
        self.assertContains(response, "hold harmless")

    def test_the_page_warns_about_state_nonprofit_rules(self):
        response = self.client.get(self.url)
        self.assertContains(response, "registered non-profit")


@isolated_cache("donations")
class TextHandlingTests(TestCase):
    def test_html_is_reduced_to_text(self):
        self.assertEqual(donations.strip_email_html("<p>Hello</p><p>World</p>"), "Hello\n\nWorld")

    def test_scripts_and_styles_are_dropped(self):
        cleaned = donations.strip_email_html("<style>p{color:red}</style><script>evil()</script><p>Hi</p>")
        self.assertNotIn("evil", cleaned)
        self.assertNotIn("color:red", cleaned)
        self.assertIn("Hi", cleaned)

    def test_an_unclosed_script_tag_does_not_eat_the_reply(self):
        """A stray "<script>" in a vendor's message must not hide what they wrote after it."""
        cleaned = donations.strip_email_html("<script>oops<p>Yes, we can donate a filter.</p>")
        self.assertIn("Yes, we can donate a filter.", cleaned)

    def test_a_body_built_to_be_slow_is_still_fast(self):
        """Bodies arrive from strangers, so the stripping has to stay linear in their length.

        Each of these used to backtrack from every opening tag to the end of the string looking for
        a closer that never comes: at this size the script/style pattern alone took ~13 seconds, and
        a real multi-megabyte email would have taken hours of CPU. Anything near the old cost fails
        this even on a slow machine.
        """
        import time

        for payload in ("<img" * 20000, "<" * 20000, "<script>" * 20000, "<style " * 20000):
            started = time.monotonic()
            donations.strip_email_html(payload)
            self.assertLess(time.monotonic() - started, 2.0, f"stripping {payload[:8]!r}... was too slow")

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

    def test_reply_prefixes_are_stripped_however_many_there_are(self):
        self.assertEqual(donations.strip_reply_prefix("Re: Fwd: RE: Donations"), "Donations")
        self.assertEqual(donations.strip_reply_prefix("Re[2]: Donations"), "Donations")
        self.assertEqual(donations.strip_reply_prefix("Donations"), "Donations")

    def test_a_subject_that_is_only_prefixes_does_not_loop_forever(self):
        self.assertEqual(donations.strip_reply_prefix("Re: " * 500), "")

    def test_a_follow_up_subject_replies_to_the_thread(self):
        self.assertEqual(donations.followup_subject("Re: Donations"), "RE: Donations")

    def test_a_follow_up_subject_falls_back_when_the_thread_has_none(self):
        self.assertEqual(donations.followup_subject("", "A donation for our auction"), "RE: A donation for our auction")
        self.assertEqual(donations.followup_subject("", ""), "")

    def test_a_follow_up_subject_fits_the_form_that_holds_it(self):
        self.assertLessEqual(len(donations.followup_subject("x" * 400)), 200)

    def test_our_own_footer_is_cut_off_a_stored_message(self):
        body = "Please donate.\n\n---\nThis message is a donation request from:\nTAS\n1 Main St\n\nUnsubscribe: ..."
        self.assertEqual(donations.strip_donation_footer(body), "Please donate.")

    def test_a_message_with_no_footer_is_left_alone(self):
        self.assertEqual(donations.strip_donation_footer("  Please donate.  "), "Please donate.")
