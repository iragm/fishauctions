"""Gaps left by test_marketing.py and test_celery_tasks.py: the per-member Mailchimp and Brevo sync
decisions (opt-outs, forced resubscribes, archiving, rejected addresses and club-level errors), contact
deletion and backfill, and the Celery tasks around them -- the marketing sync tasks, the nightly
welcome and expiration-reminder emails (idempotent, one bad member can't stop the rest, a soft time
limit is never swallowed), Discord role refresh, and the daily wallet-pass refreshes.
"""

import datetime
import json
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock, call, patch

import httpx
import requests
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from mailchimp_marketing.api_client import ApiClientError

from auctions import brevo, tasks
from auctions import mailchimp as mc
from auctions.models import AppleDeviceRegistration, Club, ClubHistory, ClubMember, PayPalSeller


def _mailchimp_error(status_code, detail="nope"):
    return ApiClientError(json.dumps({"title": "Error", "status": status_code, "detail": detail}), status_code)


def _brevo_resp(status_code=201, body=None, content=True):
    resp = MagicMock(status_code=status_code, content=json.dumps(body or {}).encode() if content else b"")
    resp.json.return_value = body or {}
    return resp


class MailchimpSyncDecisionTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="MC Gap Club",
            mailchimp_access_token="token",
            mailchimp_server_prefix="us1",
            mailchimp_audience_id="list123",
        )
        self.member = ClubMember.objects.create(club=self.club, name="Joe Member", email="joe@example.com")
        self.client_mock = MagicMock()
        self.client_mock.lists.set_list_member.return_value = {"web_id": 5, "status": "subscribed"}
        patcher = patch("auctions.mailchimp.get_client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set(self, **fields):
        ClubMember.objects.filter(pk=self.member.pk).update(**fields)
        self.member.refresh_from_db()

    def test_non_essential_member_is_sent_as_unsubscribed(self):
        self._set(contact_status="non_essential")
        self.client_mock.lists.set_list_member.return_value = {"web_id": 5, "status": "unsubscribed"}
        self.assertTrue(mc.sync_member(self.member))
        body = self.client_mock.lists.set_list_member.call_args.args[2]
        self.assertEqual(body["status"], "unsubscribed")
        self.assertEqual(body["status_if_new"], "unsubscribed")

    def test_remote_cleaned_is_not_resubscribed(self):
        self._set(mailchimp_status="cleaned")
        mc.sync_member(self.member)
        self.assertNotIn("status", self.client_mock.lists.set_list_member.call_args.args[2])

    def test_force_status_resubscribes_a_remote_unsubscribe(self):
        """The self-service resubscribe link is the one path allowed to override Mailchimp's opt-out."""
        self._set(mailchimp_status="unsubscribed")
        mc.sync_member(self.member, force_status=True)
        self.assertEqual(self.client_mock.lists.set_list_member.call_args.args[2]["status"], "subscribed")

    def test_deleted_member_is_archived_not_upserted(self):
        self._set(is_deleted=True)
        self.assertTrue(mc.sync_member(self.member))
        self.client_mock.lists.delete_list_member.assert_called_once_with(
            "list123", mc.subscriber_hash("joe@example.com")
        )
        self.client_mock.lists.set_list_member.assert_not_called()
        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "archived")

    def test_bad_address_is_archived(self):
        self._set(email_address_status="BAD")
        mc.sync_member(self.member)
        self.client_mock.lists.set_list_member.assert_not_called()
        self.client_mock.lists.delete_list_member.assert_called_once()

    def test_member_without_email_is_never_upserted(self):
        self._set(email="")
        mc.sync_member(self.member)
        self.client_mock.lists.set_list_member.assert_not_called()

    def test_archiving_a_never_synced_member_ignores_404(self):
        self._set(contact_status="do_not_contact")
        self.client_mock.lists.delete_list_member.side_effect = _mailchimp_error(404)
        self.assertTrue(mc.sync_member(self.member))
        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "archived")

    def test_archive_server_error_is_recorded_on_the_club(self):
        self._set(contact_status="do_not_contact", mailchimp_status="subscribed")
        self.client_mock.lists.delete_list_member.side_effect = _mailchimp_error(500, "Server down")
        with self.assertLogs("auctions.mailchimp", level="ERROR"):
            self.assertFalse(mc.sync_member(self.member))
        self.club.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(self.club.mailchimp_last_error, "Server down")
        self.assertEqual(self.member.mailchimp_status, "subscribed")

    def test_rejected_address_marks_member_cleaned_not_club(self):
        self.client_mock.lists.set_list_member.side_effect = _mailchimp_error(400, "looks fake")
        with self.assertLogs("auctions.mailchimp", level="WARNING"):
            self.assertFalse(mc.sync_member(self.member))
        self.member.refresh_from_db()
        self.club.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "cleaned")
        self.assertEqual(self.club.mailchimp_last_error, "")

    def test_success_clears_a_previous_club_error(self):
        Club.objects.filter(pk=self.club.pk).update(mailchimp_last_error="old failure")
        self.assertTrue(mc.sync_member(self.member))
        self.club.refresh_from_db()
        self.assertEqual(self.club.mailchimp_last_error, "")
        self.assertIsNotNone(self.club.mailchimp_last_sync)

    def test_tag_failure_still_records_the_sync(self):
        self.client_mock.lists.update_list_member_tags.side_effect = _mailchimp_error(400)
        with self.assertLogs("auctions.mailchimp", level="ERROR"):
            self.assertTrue(mc.sync_member(self.member))
        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "subscribed")

    def test_change_email_falls_back_to_sync_when_old_contact_missing(self):
        self.client_mock.lists.update_list_member.side_effect = _mailchimp_error(404)
        mc.change_member_email(self.member, "old@example.com")
        self.client_mock.lists.set_list_member.assert_called_once()
        self.assertEqual(
            self.client_mock.lists.set_list_member.call_args.args[1], mc.subscriber_hash("joe@example.com")
        )

    def test_change_email_to_same_address_does_nothing(self):
        mc.change_member_email(self.member, "joe@example.com")
        self.client_mock.lists.update_list_member.assert_not_called()


class MailchimpDeleteAndBackfillTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="MC Delete Club",
            mailchimp_access_token="token",
            mailchimp_server_prefix="us1",
            mailchimp_audience_id="list123",
        )

    @patch("auctions.mailchimp.get_client")
    def test_delete_contact_permanently(self, get_client):
        self.assertTrue(mc.delete_contact_by_email(self.club, "Gone@Example.com"))
        get_client.return_value.lists.delete_list_member_permanent.assert_called_once_with(
            "list123", mc.subscriber_hash("gone@example.com")
        )

    @patch("auctions.mailchimp.get_client")
    def test_delete_contact_ignores_404(self, get_client):
        get_client.return_value.lists.delete_list_member_permanent.side_effect = _mailchimp_error(404)
        self.assertTrue(mc.delete_contact_by_email(self.club, "gone@example.com"))

    @patch("auctions.mailchimp.get_client")
    def test_delete_contact_raises_other_errors_so_the_task_retries(self, get_client):
        get_client.return_value.lists.delete_list_member_permanent.side_effect = _mailchimp_error(500)
        with self.assertRaises(ApiClientError):
            mc.delete_contact_by_email(self.club, "gone@example.com")

    @patch("auctions.mailchimp.get_client")
    def test_delete_contact_noop_without_email_or_connection(self, get_client):
        self.assertFalse(mc.delete_contact_by_email(self.club, ""))
        other = Club.objects.create(name="Unconnected")
        self.assertFalse(mc.delete_contact_by_email(other, "gone@example.com"))
        get_client.assert_not_called()

    @patch("auctions.tasks.sync_club_member_to_mailchimp.delay")
    def test_backfill_queues_in_scope_members_only(self, delay):
        keep = ClubMember.objects.create(club=self.club, name="Keep", email="keep@example.com")
        opted_out = ClubMember.objects.create(
            club=self.club, name="Opted", email="opted@example.com", contact_status="do_not_contact"
        )
        ClubMember.objects.create(club=self.club, name="No Email", email="")
        gone = ClubMember.objects.create(club=self.club, name="Gone", email="gone@example.com")
        ClubMember.objects.filter(pk=gone.pk).update(is_deleted=True)
        ClubMember.objects.create(club=Club.objects.create(name="Other"), name="X", email="x@example.com")

        self.assertEqual(mc.backfill(self.club), 2)
        self.assertEqual({c.args[0] for c in delay.call_args_list}, {keep.pk, opted_out.pk})


class BrevoSyncDecisionTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Brevo Gap Club", brevo_api_key="xkeysib-test", brevo_list_id="7")
        self.member = ClubMember.objects.create(club=self.club, name="Joe Member", email="joe@example.com")
        self.client_mock = MagicMock()
        self.client_mock.request.return_value = _brevo_resp(201, {"id": 99})
        patcher = patch("auctions.brevo.get_client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set(self, **fields):
        ClubMember.objects.filter(pk=self.member.pk).update(**fields)
        self.member.refresh_from_db()

    def _body(self):
        return self.client_mock.request.call_args_list[0].kwargs["json_body"]

    def test_non_essential_member_is_blacklisted(self):
        self._set(contact_status="non_essential")
        self.assertTrue(brevo.sync_member(self.member))
        self.assertTrue(self._body()["emailBlacklisted"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "unsubscribed")

    def test_remote_cleaned_stays_blacklisted_and_cleaned(self):
        self._set(brevo_status="cleaned")
        brevo.sync_member(self.member)
        self.assertTrue(self._body()["emailBlacklisted"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "cleaned")

    def test_force_status_resubscribes_a_remote_unsubscribe(self):
        self._set(brevo_status="unsubscribed")
        brevo.sync_member(self.member, force_status=True)
        self.assertFalse(self._body()["emailBlacklisted"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "subscribed")

    def test_update_without_body_keeps_the_known_contact_id(self):
        self._set(brevo_contact_id="55")
        self.client_mock.request.return_value = _brevo_resp(204, content=False)
        brevo.sync_member(self.member)
        self.assertEqual(self.client_mock.request.call_count, 1)
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_contact_id, "55")

    def test_update_without_body_looks_up_an_unknown_contact_id(self):
        self.client_mock.request.side_effect = [_brevo_resp(204, content=False), _brevo_resp(200, {"id": 77})]
        brevo.sync_member(self.member)
        self.assertEqual(self.client_mock.request.call_args.args, ("GET", "/contacts/joe%40example.com"))
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_contact_id, "77")

    def test_contact_id_lookup_404_leaves_it_blank(self):
        self.client_mock.request.side_effect = [_brevo_resp(204, content=False), brevo.BrevoApiError(404, "missing")]
        self.assertTrue(brevo.sync_member(self.member))
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_contact_id, "")
        self.assertEqual(self.member.brevo_status, "subscribed")

    def test_deleted_member_contact_is_deleted(self):
        self._set(is_deleted=True, brevo_contact_id="55")
        self.assertTrue(brevo.sync_member(self.member))
        self.assertEqual(self.client_mock.request.call_args.args, ("DELETE", "/contacts/joe%40example.com"))
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "archived")
        self.assertEqual(self.member.brevo_contact_id, "")

    def test_archiving_a_never_synced_member_ignores_404(self):
        self._set(contact_status="do_not_contact")
        self.client_mock.request.side_effect = brevo.BrevoApiError(404, "not found")
        self.assertTrue(brevo.sync_member(self.member))

    def test_member_without_email_makes_no_api_call(self):
        # brevo.py:_delete_contact: an emailless member is "archived" by sending DELETE /contacts/ (no address).
        self._set(email="")
        brevo.sync_member(self.member)
        self.client_mock.request.assert_not_called()

    def test_unprocessable_address_marks_member_cleaned(self):
        self.client_mock.request.side_effect = brevo.BrevoApiError(422, "invalid email")
        with self.assertLogs("auctions.brevo", level="WARNING"):
            self.assertFalse(brevo.sync_member(self.member))
        self.member.refresh_from_db()
        self.club.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "cleaned")
        self.assertEqual(self.club.brevo_last_error, "")

    def test_auth_failure_recorded_on_club_then_cleared_by_success(self):
        self.client_mock.request.side_effect = brevo.BrevoApiError(401, "Key not found")
        with self.assertLogs("auctions.brevo", level="ERROR"):
            self.assertFalse(brevo.sync_member(self.member))
        self.club.refresh_from_db()
        self.assertEqual(self.club.brevo_last_error, "Key not found")

        self.client_mock.request.side_effect = None
        self.assertTrue(brevo.sync_member(self.member))
        self.club.refresh_from_db()
        self.assertEqual(self.club.brevo_last_error, "")

    def test_brevo_error_is_recorded_on_club(self):
        self.client_mock.request.side_effect = brevo.BrevoError("unusable key")
        self.assertFalse(brevo.sync_member(self.member))
        self.club.refresh_from_db()
        self.assertEqual(self.club.brevo_last_error, "unusable key")

    def test_network_error_propagates_for_celery_retry(self):
        self.client_mock.request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(requests.ConnectionError):
            brevo.sync_member(self.member)


class BrevoClientAndDeleteTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Brevo Client Club", brevo_api_key="xkeysib-test", brevo_list_id="7")

    def test_get_client_none_without_key(self):
        self.assertIsNone(brevo.get_client(Club.objects.create(name="No key")))

    @patch("auctions.brevo.requests.request")
    def test_request_raises_api_error_with_readable_detail(self, request):
        resp = MagicMock(status_code=401, text="raw")
        resp.json.return_value = {"message": "Key not found"}
        request.return_value = resp
        with self.assertRaises(brevo.BrevoApiError) as ctx:
            brevo.get_client(self.club).request("GET", "/account")
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (401, "Key not found"))
        self.assertEqual(request.call_args.kwargs["headers"]["api-key"], "xkeysib-test")

    @patch("auctions.brevo.get_client")
    def test_delete_contact_ignores_404_and_raises_others(self, get_client):
        get_client.return_value.request.side_effect = brevo.BrevoApiError(404, "gone")
        self.assertTrue(brevo.delete_contact_by_email(self.club, "gone@example.com"))
        get_client.return_value.request.side_effect = brevo.BrevoApiError(500, "down")
        with self.assertRaises(brevo.BrevoApiError):
            brevo.delete_contact_by_email(self.club, "gone@example.com")

    @patch("auctions.brevo.get_client")
    def test_delete_contact_noop_without_email_or_connection(self, get_client):
        self.assertFalse(brevo.delete_contact_by_email(self.club, ""))
        self.assertFalse(brevo.delete_contact_by_email(Club.objects.create(name="Unlinked"), "a@example.com"))
        get_client.assert_not_called()

    @patch("auctions.tasks.sync_club_member_to_brevo.delay")
    def test_backfill_queues_in_scope_members_only(self, delay):
        keep = ClubMember.objects.create(club=self.club, name="Keep", email="keep@example.com")
        ClubMember.objects.create(club=self.club, name="No Email", email="")
        gone = ClubMember.objects.create(club=self.club, name="Gone", email="gone@example.com")
        ClubMember.objects.filter(pk=gone.pk).update(is_deleted=True)
        self.assertEqual(brevo.backfill(self.club), 1)
        delay.assert_called_once_with(keep.pk)


class MarketingSyncTaskTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="Task Sync Club",
            mailchimp_access_token="token",
            mailchimp_server_prefix="us1",
            mailchimp_audience_id="list123",
            brevo_api_key="xkeysib-test",
            brevo_list_id="7",
        )
        self.member = ClubMember.objects.create(club=self.club, name="Joe Member", email="joe@example.com")

    @patch("auctions.mailchimp.sync_member")
    def test_mailchimp_task_syncs_member(self, sync_member):
        tasks.sync_club_member_to_mailchimp(self.member.pk)
        self.assertEqual(sync_member.call_args.args[0].pk, self.member.pk)

    @patch("auctions.mailchimp.sync_member")
    def test_mailchimp_task_skips_missing_member_and_disconnected_club(self, sync_member):
        tasks.sync_club_member_to_mailchimp(10**9)
        Club.objects.filter(pk=self.club.pk).update(mailchimp_audience_id="")
        tasks.sync_club_member_to_mailchimp(self.member.pk)
        sync_member.assert_not_called()

    def test_mailchimp_email_change_moves_contact_then_syncs(self):
        manager = MagicMock()
        with (
            patch("auctions.mailchimp.change_member_email", manager.change),
            patch("auctions.mailchimp.sync_member", manager.sync),
        ):
            tasks.sync_club_member_email_change(self.member.pk, "old@example.com")
        self.assertEqual([c[0] for c in manager.mock_calls], ["change", "sync"])
        self.assertEqual(manager.change.call_args.args[1], "old@example.com")

    @patch("auctions.brevo.sync_member")
    def test_brevo_task_syncs_member_and_skips_disconnected(self, sync_member):
        tasks.sync_club_member_to_brevo(self.member.pk)
        sync_member.assert_called_once()
        Club.objects.filter(pk=self.club.pk).update(brevo_list_id="")
        tasks.sync_club_member_to_brevo(self.member.pk)
        sync_member.assert_called_once()

    def test_brevo_email_change_deletes_old_then_syncs(self):
        manager = MagicMock()
        with (
            patch("auctions.brevo.change_member_email", manager.change),
            patch("auctions.brevo.sync_member", manager.sync),
        ):
            tasks.sync_club_member_email_change_brevo(self.member.pk, "old@example.com")
        self.assertEqual([c[0] for c in manager.mock_calls], ["change", "sync"])


class BackfillMarketingContactsTaskTests(TestCase):
    def setUp(self):
        mc_fields = {"mailchimp_access_token": "t", "mailchimp_server_prefix": "us1", "mailchimp_audience_id": "l"}
        self.first = Club.objects.create(name="First", **mc_fields)
        self.second = Club.objects.create(name="Second", **mc_fields)
        self.inactive = Club.objects.create(name="Inactive", **mc_fields)
        Club.objects.filter(pk=self.inactive.pk).update(active=False)
        self.no_token = Club.objects.create(name="No token", mailchimp_server_prefix="us1", mailchimp_audience_id="l")
        self.brevo_club = Club.objects.create(name="Brevo", brevo_api_key="k", brevo_list_id="7")

    @patch("auctions.brevo.backfill")
    @patch("auctions.mailchimp.backfill")
    def test_only_active_connected_clubs(self, mc_backfill, brevo_backfill):
        tasks.backfill_marketing_contacts()
        self.assertEqual({c.args[0].pk for c in mc_backfill.call_args_list}, {self.first.pk, self.second.pk})
        self.assertEqual([c.args[0].pk for c in brevo_backfill.call_args_list], [self.brevo_club.pk])

    @patch("auctions.brevo.backfill")
    @patch("auctions.mailchimp.backfill")
    def test_one_failing_club_does_not_stop_the_rest(self, mc_backfill, brevo_backfill):
        mc_backfill.side_effect = [RuntimeError("boom"), 3]
        with self.assertLogs("auctions.tasks", level="ERROR"):
            tasks.backfill_marketing_contacts()
        self.assertEqual(mc_backfill.call_count, 2)
        brevo_backfill.assert_called_once()

    @patch("auctions.brevo.backfill")
    @patch("auctions.mailchimp.backfill", side_effect=SoftTimeLimitExceeded())
    def test_soft_time_limit_is_not_swallowed(self, mc_backfill, brevo_backfill):
        with self.assertRaises(SoftTimeLimitExceeded):
            tasks.backfill_marketing_contacts()
        brevo_backfill.assert_not_called()


class SafelyTests(TestCase):
    def test_ordinary_exception_is_logged_and_swallowed(self):
        def boom():
            msg = "bad row"
            raise ValueError(msg)

        with self.assertLogs("auctions.tasks", level="ERROR") as logs:
            tasks._safely("step x", boom)
        self.assertIn("step x", logs.output[0])

    def test_soft_time_limit_is_reraised(self):
        def slow():
            raise SoftTimeLimitExceeded()

        with self.assertRaises(SoftTimeLimitExceeded):
            tasks._safely("step y", slow)


class WelcomeEmailTaskTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Welcome Club", send_welcome_email_to_new_members=True)
        self.old = timezone.now() - datetime.timedelta(days=2)
        self.alice = self._member("Alice", "alice@example.com")
        self.bob = self._member("Bob", "bob@example.com")

    def _member(self, name, email, **fields):
        member = ClubMember.objects.create(club=self.club, name=name, email=email)
        ClubMember.objects.filter(pk=member.pk).update(createdon=self.old, **fields)
        return member

    def _flags(self, member):
        member.refresh_from_db()
        return member.welcome_email_sent, member.send_welcome_email

    @patch("auctions.tasks.mail.send")
    def test_running_twice_sends_once(self, send):
        tasks.send_club_member_welcome_emails()
        tasks.send_club_member_welcome_emails()
        self.assertEqual(sorted(c.args[0] for c in send.call_args_list), ["alice@example.com", "bob@example.com"])
        self.assertEqual(ClubHistory.objects.filter(club=self.club, action__contains="welcome letter").count(), 2)

    @patch("auctions.tasks.mail.send")
    def test_members_joined_in_the_last_day_wait(self, send):
        fresh = ClubMember.objects.create(club=self.club, name="Fresh", email="fresh@example.com")
        tasks.send_club_member_welcome_emails()
        self.assertNotIn("fresh@example.com", [c.args[0] for c in send.call_args_list])
        self.assertEqual(self._flags(fresh)[0], False)

    def test_member_is_marked_before_the_send_so_a_failure_is_not_retried_nightly(self):
        with (
            patch("auctions.tasks.send_club_member_email", side_effect=RuntimeError("smtp down")) as send,
            self.assertLogs("auctions.tasks", level="ERROR"),
        ):
            tasks.send_club_member_welcome_emails()
        self.assertEqual(send.call_count, 2, "one failure stopped the rest")
        self.assertTrue(self._flags(self.alice)[0])
        self.assertTrue(self._flags(self.bob)[0])

    def test_soft_time_limit_stops_the_run_but_member_is_marked(self):
        with (
            patch("auctions.tasks.send_club_member_email", side_effect=SoftTimeLimitExceeded()) as send,
            self.assertRaises(SoftTimeLimitExceeded),
        ):
            tasks.send_club_member_welcome_emails()
        self.assertEqual(send.call_count, 1)
        marked = [m for m in (self.alice, self.bob) if self._flags(m)[0]]
        self.assertEqual(len(marked), 1)

    @patch("auctions.tasks.mail.send")
    def test_csv_imports_are_marked_without_mail(self, send):
        imported = self._member("Imported", "imp@example.com", source="csv")
        tasks.send_club_member_welcome_emails()
        self.assertNotIn("imp@example.com", [c.args[0] for c in send.call_args_list])
        self.assertEqual(self._flags(imported), (True, False))

    @patch("auctions.tasks.mail.send")
    def test_club_setting_off_marks_without_mail(self, send):
        Club.objects.filter(pk=self.club.pk).update(send_welcome_email_to_new_members=False)
        tasks.send_club_member_welcome_emails()
        send.assert_not_called()
        self.assertTrue(self._flags(self.alice)[0])

    @patch("auctions.tasks.mail.send")
    def test_deleted_members_are_skipped(self, send):
        ClubMember.objects.filter(pk=self.bob.pk).update(is_deleted=True)
        tasks.send_club_member_welcome_emails()
        self.assertEqual([c.args[0] for c in send.call_args_list], ["alice@example.com"])
        self.assertFalse(self._flags(self.bob)[0])


class ExpirationReminderTaskTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="Reminder Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            send_membership_expiration_reminders=True,
            send_membership_expiration_reminders_30_days=True,
        )
        payer = User.objects.create_user(username="reminder_payee", password="x", email="payee@example.com")
        PayPalSeller.objects.create(user=payer, club=self.club, paypal_merchant_id="merchant_reminder")
        self.alice = self._member("Alice", "alice@example.com")
        self.bob = self._member("Bob", "bob@example.com")

    def _member(self, name, email, **fields):
        today = timezone.localdate()
        member = ClubMember.objects.create(club=self.club, name=name, email=email)
        values = {
            "welcome_email_sent": True,
            "membership_last_paid": today - datetime.timedelta(days=335),
            "membership_expiration_date": today + datetime.timedelta(days=30),
            "membership_expiration_reminder_30_days_due": timezone.now() - datetime.timedelta(minutes=1),
            "membership_expiration_reminder_due": None,
        }
        ClubMember.objects.filter(pk=member.pk).update(**{**values, **fields})
        return member

    def _due(self, member):
        member.refresh_from_db()
        return member.membership_expiration_reminder_30_days_due

    @patch("auctions.tasks.mail.send")
    def test_running_twice_sends_once_per_member(self, send):
        tasks.send_membership_expiration_reminders()
        tasks.send_membership_expiration_reminders()
        self.assertEqual(sorted(c.args[0] for c in send.call_args_list), ["alice@example.com", "bob@example.com"])
        self.assertIsNone(self._due(self.alice))

    def test_one_failing_member_does_not_stop_the_rest(self):
        def send(member, **kwargs):
            if member.pk == self.alice.pk:
                msg = "bad member"
                raise RuntimeError(msg)
            return True

        with (
            patch("auctions.tasks.send_club_member_email", side_effect=send) as mocked,
            self.assertLogs("auctions.tasks", level="ERROR"),
        ):
            tasks.send_membership_expiration_reminders()
        self.assertEqual(mocked.call_count, 2)
        self.assertIsNone(self._due(self.bob))

    def test_soft_time_limit_is_reraised(self):
        with (
            patch("auctions.tasks.send_club_member_email", side_effect=SoftTimeLimitExceeded()),
            self.assertRaises(SoftTimeLimitExceeded),
        ):
            tasks.send_membership_expiration_reminders()

    @patch("auctions.tasks.mail.send")
    def test_reminders_off_clears_due_without_mail(self, send):
        Club.objects.filter(pk=self.club.pk).update(send_membership_expiration_reminders_30_days=False)
        tasks.send_membership_expiration_reminders()
        send.assert_not_called()
        self.assertIsNone(self._due(self.alice))

    @patch("auctions.tasks.mail.send")
    def test_paypal_subscribers_and_lapsed_members_are_skipped(self, send):
        subscriber = self._member("Sub", "sub@example.com", paypal_subscription_id="I-123")
        lapsed = self._member(
            "Lapsed", "lapsed@example.com", membership_expiration_date=timezone.localdate() - datetime.timedelta(1)
        )
        tasks.send_membership_expiration_reminders()
        emailed = [c.args[0] for c in send.call_args_list]
        self.assertNotIn("sub@example.com", emailed)
        self.assertNotIn("lapsed@example.com", emailed)
        # The subscriber keeps its due timestamp so reminders resume if the subscription is cancelled.
        self.assertIsNotNone(self._due(subscriber))
        self.assertIsNotNone(self._due(lapsed))


class MembershipCardEmailTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="Card Club", membership_system="rolling", membership_annual_fee=Decimal(20)
        )
        self.user = User.objects.create_user(username="card_user", password="x", email="card@example.com")
        self.member = ClubMember.objects.create(club=self.club, name="Card Holder", email="card@example.com")

    @patch("auctions.notifications.notify_user")
    @patch("auctions.tasks.mail.send")
    def test_card_email_bypasses_push(self, send, notify_user):
        self.assertTrue(tasks.send_membership_card_email(self.member))
        notify_user.assert_not_called()
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["subject"], "Your Card Club membership card")

    @patch("auctions.tasks.mail.send")
    def test_card_email_says_expired_for_lapsed_member(self, send):
        today = timezone.localdate()
        ClubMember.objects.filter(pk=self.member.pk).update(
            membership_last_paid=today - datetime.timedelta(days=400),
            membership_expiration_date=today - datetime.timedelta(days=35),
        )
        self.member.refresh_from_db()
        tasks.send_membership_card_email(self.member)
        self.assertIn("expired on", send.call_args.kwargs["message"])

    @patch("auctions.tasks.mail.send")
    def test_do_not_contact_and_emailless_members_get_nothing(self, send):
        ClubMember.objects.filter(pk=self.member.pk).update(contact_status="do_not_contact")
        self.member.refresh_from_db()
        self.assertFalse(tasks.send_membership_card_email(self.member))
        blank = ClubMember.objects.create(club=self.club, name="Blank", email="")
        self.assertFalse(tasks.send_membership_card_email(blank))
        send.assert_not_called()


class DiscordRoleRefreshTaskTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Discord Club")
        Club.objects.filter(pk=self.club.pk).update(discord_server_id="srv")
        self.members = []
        for i in range(3):
            member = ClubMember.objects.create(club=self.club, name=f"D{i}", email=f"d{i}@example.com")
            ClubMember.objects.filter(pk=member.pk).update(discord_id=f"id{i}", discord_role_auto_managed=True)
            self.members.append(member)

    def test_one_failing_member_does_not_stop_the_rest(self):
        seen = []
        failing_pk = self.members[0].pk

        def assign(member_self):
            seen.append(member_self.pk)
            if member_self.pk == failing_pk:
                msg = "discord 500"
                raise RuntimeError(msg)

        with (
            patch.object(ClubMember, "discord_role", new_callable=PropertyMock, return_value=object()),
            patch.object(ClubMember, "maybe_assign_discord_role", autospec=True, side_effect=assign),
            self.assertLogs("auctions.tasks", level="ERROR"),
        ):
            tasks.update_expired_membership_discord_roles()
        self.assertEqual(sorted(seen), sorted(m.pk for m in self.members))

    def test_members_whose_role_already_matches_are_skipped(self):
        with (
            patch.object(ClubMember, "discord_role", new_callable=PropertyMock, return_value=None),
            patch.object(ClubMember, "maybe_assign_discord_role") as assign,
        ):
            tasks.update_expired_membership_discord_roles()
        assign.assert_not_called()


class WalletDailyRefreshTaskTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Wallet Club", membership_system="rolling")
        Club.objects.filter(pk=self.club.pk).update(google_wallet_class_created=True)
        today = timezone.localdate()
        self.lapsed = self._member("Lapsed", today - datetime.timedelta(days=1))
        self.long_gone = self._member("Long gone", today - datetime.timedelta(days=10))
        self.current = self._member("Current", today + datetime.timedelta(days=10))

    def _member(self, name, expiration):
        member = ClubMember.objects.create(club=self.club, name=name, email=f"{name.split()[0].lower()}@example.com")
        ClubMember.objects.filter(pk=member.pk).update(membership_expiration_date=expiration)
        return member

    @patch("auctions.google_wallet.update_generic_object_for_member")
    @patch("auctions.google_wallet.is_configured", return_value=True)
    def test_google_refresh_only_touches_recently_lapsed(self, configured, update):
        tasks.refresh_google_wallet_membership_status()
        self.assertEqual([m.pk for (m,), _ in update.call_args_list], [self.lapsed.pk])

    @patch("auctions.google_wallet.update_generic_object_for_member")
    @patch("auctions.google_wallet.is_configured", return_value=False)
    def test_google_refresh_noop_when_unconfigured(self, configured, update):
        tasks.refresh_google_wallet_membership_status()
        update.assert_not_called()

    @patch("auctions.apple_wallet.send_pass_update_notification")
    @patch("auctions.apple_wallet.is_configured", return_value=True)
    def test_apple_refresh_bumps_pass_and_one_bad_device_does_not_block_others(self, configured, notify):
        for member in (self.lapsed, self.long_gone):
            for n in range(2):
                AppleDeviceRegistration.objects.create(
                    member=member, device_library_identifier=f"dev-{member.pk}-{n}", push_token="tok"
                )
        before = timezone.now()
        failing = []

        def send(registration):
            if not failing:
                failing.append(registration.pk)
                msg = "apns down"
                raise httpx.ConnectError(msg)

        notify.side_effect = send
        with self.assertRaises(RuntimeError), self.assertLogs("auctions.tasks", level="ERROR"):
            tasks.refresh_apple_wallet_membership_status()
        self.assertEqual(notify.call_count, 2)
        self.assertEqual({r.member_id for (r,), _ in notify.call_args_list}, {self.lapsed.pk})
        self.lapsed.refresh_from_db()
        self.long_gone.refresh_from_db()
        self.assertGreaterEqual(self.lapsed.apple_pass_updated, before)
        self.assertLess(self.long_gone.apple_pass_updated, before)

    @patch("auctions.apple_wallet.send_pass_update_notification")
    @patch("auctions.apple_wallet.is_configured", return_value=True)
    def test_apple_refresh_skips_members_without_devices(self, configured, notify):
        tasks.refresh_apple_wallet_membership_status()
        notify.assert_not_called()


class CleanupOauthTokensTaskTests(TestCase):
    @patch("django.core.management.call_command")
    def test_one_failing_command_does_not_stop_the_other(self, call_command):
        call_command.side_effect = [RuntimeError("boom"), None]
        with self.assertLogs("auctions.tasks", level="ERROR"):
            tasks.cleanup_oauth_tokens()
        self.assertEqual(call_command.call_args_list, [call("cleartokens"), call("clearcimdapplications")])
