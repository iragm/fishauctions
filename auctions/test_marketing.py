"""Mailchimp and Brevo: syncing members, webhooks, self-service and what gets redacted."""

import datetime
import hashlib
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions import brevo
from auctions import mailchimp as mc
from auctions.models import (
    Auction,
    AuctionTOS,
    Category,
    Club,
    ClubHistory,
    ClubMember,
    Lot,
    PickupLocation,
    UserData,
    UserInterestCategory,
)


class MailchimpHelperTests(TestCase):
    """Pure-logic tests for tag computation, merge fields, status mapping and hashing."""

    def setUp(self):
        self.club = Club.objects.create(name="Test Club")
        self.member = ClubMember.objects.create(club=self.club, name="Jane Q Public", email="jane@example.com")

    def test_subscriber_hash_lowercases_and_trims(self):
        self.assertEqual(mc.subscriber_hash(" Jane@Example.COM "), hashlib.md5(b"jane@example.com").hexdigest())

    def test_name_split(self):
        self.assertEqual(self.member.first_name, "Jane")
        self.assertEqual(self.member.last_name, "Q Public")
        solo = ClubMember.objects.create(club=self.club, name="Cher", email="cher@example.com")
        self.assertEqual(solo.first_name, "Cher")
        self.assertEqual(solo.last_name, "")

    def test_desired_status_mapping(self):
        self.member.contact_status = "contact"
        self.assertEqual(mc._desired_status(self.member), "subscribed")
        self.member.contact_status = "non_essential"
        self.assertEqual(mc._desired_status(self.member), "unsubscribed")
        self.member.contact_status = "do_not_contact"
        self.assertEqual(mc._desired_status(self.member), "archived")
        self.member.contact_status = "contact"
        self.member.email_address_status = "BAD"
        self.assertEqual(mc._desired_status(self.member), "archived")

    def test_lifecycle_tags(self):
        today = timezone.now().date()
        self.member.membership_expiration_date = today + datetime.timedelta(days=10)
        tags = self.member.compute_mailchimp_tags()
        self.assertTrue(tags["expiring-soon"])
        self.assertFalse(tags["expired"])
        self.assertTrue(tags["new-member"])
        self.assertFalse(tags["long-term-member"])

        self.member.membership_expiration_date = today - datetime.timedelta(days=1)
        tags = self.member.compute_mailchimp_tags()
        self.assertTrue(tags["expired"])
        self.assertFalse(tags["expiring-soon"])

    def test_value_and_connection_tags(self):
        self.member.cached_total_sold = Decimal(1500)
        self.member.cached_total_bought = Decimal(50)
        self.member.discord_id = "discord-123"
        self.member.permission_add_edit = True
        tags = self.member.compute_mailchimp_tags()
        self.assertTrue(tags["power-seller"])
        self.assertFalse(tags["power-buyer"])
        self.assertTrue(tags["discord-connected"])
        self.assertTrue(tags["admin"])

    def test_compute_tags_covers_full_vocabulary(self):
        tags = self.member.compute_mailchimp_tags()
        self.assertEqual(set(tags.keys()), set(ClubMember.MAILCHIMP_TAGS))

    def test_merge_fields_include_links_and_split_name(self):
        self.member.membership_expiration_date = datetime.date(2030, 1, 2)
        fields = mc.member_merge_fields(self.member)
        self.assertEqual(fields["FNAME"], "Jane")
        self.assertEqual(fields["LNAME"], "Q Public")
        self.assertEqual(fields["EXPIRES"], "2030-01-02")
        self.assertIn("/member/", fields["RENEW"])
        self.assertTrue(fields["CLUBUNSUB"].endswith("/unsubscribe/"))
        self.assertTrue(fields["RESUB"].endswith("/resubscribe/"))
        self.assertTrue(fields["NOCOMM"].endswith("/no-contact/"))


class MailchimpCategoryTagTests(TestCase):
    """Tests for _top_category_names() and category tag integration in _sync_tags / ensure_segments."""

    def setUp(self):
        self.club = Club.objects.create(name="Cat Club")
        self.user = User.objects.create_user(username="catuser", email="cat@example.com")
        self.member = ClubMember.objects.create(
            club=self.club, name="Cat User", email="cat@example.com", user=self.user
        )
        self.cat_a = Category.objects.create(name="Cichlids")
        self.cat_b = Category.objects.create(name="Tetras")
        self.cat_c = Category.objects.create(name="Corydoras")
        self.cat_uncat = Category.objects.get_or_create(name="Uncategorized")[0]

    # --- _top_category_names: UserInterestCategory path ---

    def test_top_cats_uses_user_interest_when_present(self):
        UserInterestCategory.objects.create(user=self.user, category=self.cat_a, interest=10)
        UserInterestCategory.objects.create(user=self.user, category=self.cat_b, interest=5)
        names = mc._top_category_names(self.member)
        self.assertEqual(names, {"Cichlids", "Tetras"})

    def test_top_cats_excludes_uncategorized_from_interests(self):
        UserInterestCategory.objects.create(user=self.user, category=self.cat_uncat, interest=100)
        UserInterestCategory.objects.create(user=self.user, category=self.cat_a, interest=5)
        names = mc._top_category_names(self.member)
        self.assertNotIn("Uncategorized", names)
        self.assertIn("Cichlids", names)

    def test_top_cats_capped_at_5_from_interests(self):
        cats = [Category.objects.create(name=f"Fish-{i}") for i in range(8)]
        for i, cat in enumerate(cats):
            UserInterestCategory.objects.create(user=self.user, category=cat, interest=10 - i)
        names = mc._top_category_names(self.member)
        self.assertEqual(len(names), 5)
        self.assertIn("Fish-0", names)
        self.assertNotIn("Fish-7", names)

    # --- _top_category_names: lot-history fallback ---

    def _make_lot_setup(self):
        """Return (auction, location, seller_tos, winner_tos) for the club's auction."""
        the_future = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Club auction",
            club=self.club,
            date_end=timezone.now() - datetime.timedelta(days=1),
            date_start=timezone.now() - datetime.timedelta(days=2),
        )
        location = PickupLocation.objects.create(name="loc", auction=auction, pickup_time=the_future)
        seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=auction, pickup_location=location, email="cat@example.com"
        )
        winner_tos = AuctionTOS.objects.create(auction=auction, pickup_location=location, email="buyer@example.com")
        return auction, location, seller_tos, winner_tos

    def test_top_cats_falls_back_to_lot_history_when_no_interests(self):
        auction, location, seller_tos, winner_tos = self._make_lot_setup()
        Lot.objects.create(lot_name="L1", auctiontos_seller=seller_tos, species_category=self.cat_a, auction=auction)
        Lot.objects.create(lot_name="L2", auctiontos_seller=seller_tos, species_category=self.cat_a, auction=auction)
        Lot.objects.create(lot_name="L3", auctiontos_seller=seller_tos, species_category=self.cat_b, auction=auction)
        names = mc._top_category_names(self.member)
        self.assertIn("Cichlids", names)
        self.assertIn("Tetras", names)

    def test_top_cats_counts_won_lots_in_fallback(self):
        auction, location, seller_tos, winner_tos = self._make_lot_setup()
        # seller_tos already has email="cat@example.com"; reuse it as winner to avoid duplicate-merge
        Lot.objects.create(lot_name="Won1", auctiontos_winner=seller_tos, species_category=self.cat_c, auction=auction)
        names = mc._top_category_names(self.member)
        self.assertIn("Corydoras", names)

    def test_top_cats_fallback_excludes_uncategorized(self):
        auction, location, seller_tos, winner_tos = self._make_lot_setup()
        Lot.objects.create(
            lot_name="U1", auctiontos_seller=seller_tos, species_category=self.cat_uncat, auction=auction
        )
        Lot.objects.create(lot_name="A1", auctiontos_seller=seller_tos, species_category=self.cat_a, auction=auction)
        names = mc._top_category_names(self.member)
        self.assertNotIn("Uncategorized", names)

    def test_top_cats_fallback_capped_at_5(self):
        auction, location, seller_tos, winner_tos = self._make_lot_setup()
        cats = [Category.objects.create(name=f"Species-{i}") for i in range(7)]
        for i, cat in enumerate(cats):
            for _ in range(7 - i):
                Lot.objects.create(
                    lot_name=f"L-{cat.name}", auctiontos_seller=seller_tos, species_category=cat, auction=auction
                )
        names = mc._top_category_names(self.member)
        self.assertEqual(len(names), 5)
        self.assertIn("Species-0", names)
        self.assertNotIn("Species-6", names)

    def test_top_cats_no_email_returns_empty(self):
        member = ClubMember.objects.create(club=self.club, name="No Email")
        self.assertEqual(mc._top_category_names(member), set())

    # --- _sync_tags sends category tags ---

    @patch("auctions.mailchimp.get_client")
    def test_sync_tags_includes_category_tags(self, mock_get_client):
        client = MagicMock()
        client.lists.set_list_member.return_value = {"web_id": 1, "status": "subscribed"}
        mock_get_client.return_value = client
        self.club.mailchimp_access_token = "tok"
        self.club.mailchimp_server_prefix = "us1"
        self.club.mailchimp_audience_id = "listXYZ"
        self.club.mailchimp_webhook_secret = "sec"
        self.club.save()

        UserInterestCategory.objects.create(user=self.user, category=self.cat_a, interest=10)
        mc.sync_member(self.member)

        _, _, payload = client.lists.update_list_member_tags.call_args.args
        sent_tags = {t["name"]: t["status"] for t in payload["tags"]}
        self.assertEqual(sent_tags.get("Cichlids"), "active")
        self.assertEqual(sent_tags.get("Tetras"), "inactive")
        self.assertNotIn("Uncategorized", sent_tags)

    # --- ensure_segments creates category segments ---

    def test_ensure_segments_includes_categories(self):
        self.club.mailchimp_access_token = "tok"
        self.club.mailchimp_server_prefix = "us1"
        self.club.mailchimp_audience_id = "listXYZ"
        self.club.save()
        client = MagicMock()
        client.lists.list_segments.return_value = {"segments": []}
        with patch("auctions.mailchimp.get_client", return_value=client):
            mc.ensure_segments(self.club)
        created = {call.args[1]["name"] for call in client.lists.create_segment.call_args_list}
        self.assertIn("Cichlids", created)
        self.assertIn("Tetras", created)
        self.assertNotIn("Uncategorized", created)


class MailchimpSyncTests(TestCase):
    """sync_member / change_member_email against a mocked Mailchimp client."""

    def setUp(self):
        self.club = Club.objects.create(name="Sync Club")
        self.member = ClubMember.objects.create(club=self.club, name="Joe Member", email="joe@example.com")
        self._connect_club()

    def _connect_club(self):
        self.club.mailchimp_access_token = "token"
        self.club.mailchimp_server_prefix = "us1"
        self.club.mailchimp_audience_id = "list123"
        self.club.mailchimp_webhook_secret = "secret123"
        self.club.save()

    def test_no_op_when_not_connected(self):
        self.club.mailchimp_audience_id = ""
        self.club.save()
        with patch("auctions.mailchimp.get_client") as gc:
            self.assertFalse(mc.sync_member(self.member))
            gc.assert_not_called()

    @patch("auctions.mailchimp.get_client")
    def test_sync_member_upserts_and_tags(self, mock_get_client):
        client = MagicMock()
        client.lists.set_list_member.return_value = {"web_id": 42, "status": "subscribed"}
        mock_get_client.return_value = client

        self.assertTrue(mc.sync_member(self.member))

        client.lists.set_list_member.assert_called_once()
        list_id, sub_hash, body = client.lists.set_list_member.call_args.args
        self.assertEqual(list_id, "list123")
        self.assertEqual(sub_hash, mc.subscriber_hash("joe@example.com"))
        self.assertEqual(body["status"], "subscribed")
        self.assertEqual(body["merge_fields"]["FNAME"], "Joe")
        client.lists.update_list_member_tags.assert_called_once()

        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "subscribed")
        self.assertEqual(self.member.mailchimp_web_id, "42")
        self.assertIsNotNone(self.member.mailchimp_last_synced)

    @patch("auctions.mailchimp.get_client")
    def test_sync_respects_remote_unsubscribe(self, mock_get_client):
        client = MagicMock()
        client.lists.set_list_member.return_value = {"web_id": 1, "status": "unsubscribed"}
        mock_get_client.return_value = client

        self.member.mailchimp_status = "unsubscribed"
        self.member.save()
        mc.sync_member(self.member)

        body = client.lists.set_list_member.call_args.args[2]
        # We must not force them back to subscribed.
        self.assertNotIn("status", body)
        self.assertEqual(body["status_if_new"], "subscribed")

    @patch("auctions.mailchimp.get_client")
    def test_do_not_contact_archives(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        self.member.contact_status = "do_not_contact"
        self.member.save()

        mc.sync_member(self.member)
        client.lists.delete_list_member.assert_called_once()
        client.lists.set_list_member.assert_not_called()
        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "archived")

    @patch("auctions.mailchimp.get_client")
    def test_change_member_email(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        mc.change_member_email(self.member, "old@example.com")
        client.lists.update_list_member.assert_called_once_with(
            "list123",
            mc.subscriber_hash("old@example.com"),
            {"email_address": "joe@example.com"},
        )

    def test_in_scope_members_excludes_deleted_and_emailless(self):
        ClubMember.objects.create(club=self.club, name="No Email", email="")
        deleted = ClubMember.objects.create(club=self.club, name="Gone", email="gone@example.com")
        deleted.is_deleted = True
        deleted.save()
        emails = set(mc.in_scope_members(self.club).values_list("email", flat=True))
        self.assertIn("joe@example.com", emails)
        self.assertNotIn("", emails)
        self.assertNotIn("gone@example.com", emails)


class MailchimpWebhookTests(TestCase):
    """Inbound webhook only records Mailchimp status; never touches the site account."""

    def setUp(self):
        self.client_http = Client()
        self.club = Club.objects.create(name="Hook Club")
        self.club.mailchimp_access_token = "token"
        self.club.mailchimp_server_prefix = "us1"
        self.club.mailchimp_audience_id = "list123"
        self.club.mailchimp_webhook_secret = "secret123"
        self.club.save()
        self.user = User.objects.create_user(username="hookuser", password="pw", email="hook@example.com")
        UserData.objects.get_or_create(user=self.user)
        self.member = ClubMember.objects.create(
            club=self.club, user=self.user, name="Hook Member", email="hook@example.com", mailchimp_status="subscribed"
        )

    def _url(self, secret="secret123"):
        return reverse("mailchimp_webhook", kwargs={"slug": self.club.slug, "secret": secret})

    def test_get_verification_ok(self):
        self.assertEqual(self.client_http.get(self._url()).status_code, 200)

    def test_bad_secret_forbidden(self):
        self.assertEqual(self.client_http.get(self._url(secret="wrong")).status_code, 403)
        resp = self.client_http.post(
            self._url(secret="wrong"), {"type": "unsubscribe", "data[email]": "hook@example.com"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_unsubscribe_sets_status_without_touching_account(self):
        resp = self.client_http.post(self._url(), {"type": "unsubscribe", "data[email]": "hook@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "unsubscribed")
        # The site-wide account preference must be untouched.
        self.assertFalse(self.user.userdata.has_unsubscribed)

    def test_cleaned_marks_member(self):
        self.client_http.post(self._url(), {"type": "cleaned", "data[email]": "hook@example.com"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.mailchimp_status, "cleaned")

    def test_upemail_updates_local_email(self):
        self.client_http.post(
            self._url(),
            {"type": "upemail", "data[old_email]": "hook@example.com", "data[new_email]": "new@example.com"},
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "new@example.com")

    def test_webhook_events_write_club_history(self):
        # These happen at Mailchimp, so the club's history log is the admins' only view of them.
        self.client_http.post(self._url(), {"type": "unsubscribe", "data[email]": "hook@example.com"})
        self.client_http.post(
            self._url(),
            {"type": "upemail", "data[old_email]": "hook@example.com", "data[new_email]": "new@example.com"},
        )
        actions = list(
            ClubHistory.objects.filter(club=self.club, applies_to="MEMBERS").values_list("action", flat=True)
        )
        self.assertEqual(len(actions), 2)
        self.assertTrue(any("unsubscribed at Mailchimp" in a for a in actions))
        self.assertTrue(any("changed their email to new@example.com via Mailchimp" in a for a in actions))

    def test_event_for_unknown_email_writes_no_history(self):
        self.client_http.post(self._url(), {"type": "unsubscribe", "data[email]": "nobody@example.com"})
        self.assertFalse(ClubHistory.objects.filter(club=self.club).exists())


class MailchimpSelfServiceTests(TestCase):
    """The UUID merge-field links change only the club contact status."""

    def setUp(self):
        self.client_http = Client()
        self.club = Club.objects.create(name="Self Serve Club")
        self.member = ClubMember.objects.create(club=self.club, name="Sam", email="sam@example.com")

    def test_get_shows_confirmation_without_changing_status(self):
        # A GET (e.g. from an email link scanner/prefetcher) must not change anything.
        url = reverse("club_member_unsubscribe", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
        response = self.client_http.get(url)
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "contact")

    def test_unsubscribe_link(self):
        url = reverse("club_member_unsubscribe", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
        self.assertEqual(self.client_http.post(url).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "non_essential")

    def test_resubscribe_clears_status(self):
        self.member.contact_status = "non_essential"
        self.member.mailchimp_status = "unsubscribed"
        self.member.save()
        url = reverse("club_member_resubscribe", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
        self.client_http.post(url)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "contact")
        self.assertEqual(self.member.mailchimp_status, "")

    def test_nocomm_link(self):
        url = reverse("club_member_nocomm", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
        self.client_http.post(url)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "do_not_contact")

    def test_contact_preference_link_writes_club_history(self):
        # The one-click preference links change the same field the unsubscribe view does, so the
        # club's admins need the same record of it.
        url = reverse(
            "club_member_contact_pref",
            kwargs={"slug": self.club.slug, "uuid": self.member.uuid, "level": "essential"},
        )
        self.assertEqual(self.client_http.get(url).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "non_essential")
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERS").get()
        self.assertIn("essential emails only", history.action)
        self.assertIsNone(history.user)


class ClubMemberEmailPropagationTests(TestCase):
    """Changing a site account email rewrites the member's club records, so the club is told."""

    def setUp(self):
        self.club = Club.objects.create(name="Propagation Club")
        self.user = User.objects.create_user(username="propagate", password="pw", email="before@example.com")
        self.member = ClubMember.objects.create(club=self.club, user=self.user, name="Pat", email="before@example.com")

    def test_account_email_change_writes_club_history(self):
        self.user.email = "after@example.com"
        self.user.save()
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "after@example.com")
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERS").get()
        self.assertIn("before@example.com", history.action)
        self.assertIn("after@example.com", history.action)
        self.assertEqual(history.user, self.user)

    def test_unrelated_account_save_writes_no_history(self):
        self.user.first_name = "Pat"
        self.user.save()
        self.assertFalse(ClubHistory.objects.filter(club=self.club).exists())


class BrevoSyncTests(TestCase):
    """sync_member / change_member_email against a mocked Brevo client (mirrors MailchimpSyncTests)."""

    def setUp(self):
        self.club = Club.objects.create(name="Brevo Sync Club")
        self.member = ClubMember.objects.create(club=self.club, name="Joe Member", email="joe@example.com")
        self._connect_club()

    def _connect_club(self):
        self.club.brevo_api_key = "xkeysib-test"
        self.club.brevo_list_id = "7"
        self.club.brevo_webhook_secret = "secret123"
        self.club.save()

    @staticmethod
    def _resp(status_code=201, body=None):
        resp = MagicMock(status_code=status_code, content=json.dumps(body or {}).encode())
        resp.json.return_value = body or {}
        return resp

    def test_no_op_when_not_connected(self):
        self.club.brevo_list_id = ""
        self.club.save()
        with patch("auctions.brevo.get_client") as gc:
            self.assertFalse(brevo.sync_member(self.member))
            gc.assert_not_called()

    @patch("auctions.brevo.get_client")
    def test_sync_member_upserts(self, mock_get_client):
        client = MagicMock()
        client.request.return_value = self._resp(201, {"id": 99})
        mock_get_client.return_value = client

        self.assertTrue(brevo.sync_member(self.member))

        client.request.assert_called_once()
        method, path = client.request.call_args.args
        self.assertEqual((method, path), ("POST", "/contacts"))
        body = client.request.call_args.kwargs["json_body"]
        self.assertEqual(body["email"], "joe@example.com")
        self.assertEqual(body["listIds"], [7])
        self.assertFalse(body["emailBlacklisted"])
        self.assertTrue(body["updateEnabled"])
        self.assertEqual(body["attributes"]["FIRSTNAME"], "Joe")

        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "subscribed")
        self.assertEqual(self.member.brevo_contact_id, "99")
        self.assertIsNotNone(self.member.brevo_last_synced)

    @patch("auctions.brevo.get_client")
    def test_sync_respects_remote_unsubscribe(self, mock_get_client):
        client = MagicMock()
        client.request.return_value = self._resp(201, {"id": 1})
        mock_get_client.return_value = client

        self.member.brevo_status = "unsubscribed"
        self.member.save()
        brevo.sync_member(self.member)

        body = client.request.call_args.kwargs["json_body"]
        # We must not resubscribe someone Brevo told us opted out.
        self.assertTrue(body["emailBlacklisted"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "unsubscribed")

    @patch("auctions.brevo.get_client")
    def test_do_not_contact_deletes(self, mock_get_client):
        client = MagicMock()
        client.request.return_value = self._resp(204)
        mock_get_client.return_value = client
        self.member.contact_status = "do_not_contact"
        self.member.save()

        brevo.sync_member(self.member)
        method, path = client.request.call_args.args
        self.assertEqual(method, "DELETE")
        self.assertTrue(path.startswith("/contacts/"))
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "archived")

    @patch("auctions.brevo.get_client")
    def test_change_member_email_deletes_old_contact(self, mock_get_client):
        client = MagicMock()
        client.request.return_value = self._resp(204)
        mock_get_client.return_value = client
        brevo.change_member_email(self.member, "old@example.com")
        self.assertEqual(client.request.call_args.args, ("DELETE", "/contacts/old%40example.com"))


class MarketingSyncLogRedactionTests(TestCase):
    """Member email addresses must never reach the log files.

    Mailchimp and Brevo both echo the address back in their error bodies, so it isn't enough to
    keep it out of the format string — the third-party detail has to be scrubbed too.
    """

    def setUp(self):
        self.club = Club.objects.create(name="Redaction Club")
        self.member = ClubMember.objects.create(club=self.club, name="Joe Member", email="joe@example.com")
        self.club.mailchimp_access_token = "token"
        self.club.mailchimp_server_prefix = "us1"
        self.club.mailchimp_audience_id = "list123"
        self.club.brevo_api_key = "xkeysib-test"
        self.club.brevo_list_id = "7"
        self.club.save()

    @staticmethod
    def _mailchimp_error(status_code=400):
        from mailchimp_marketing.api_client import ApiClientError

        return ApiClientError(
            json.dumps(
                {
                    "title": "Invalid Resource",
                    "status": status_code,
                    "detail": "joe@example.com looks fake or invalid, please enter a real email address.",
                }
            ),
            status_code,
        )

    def test_scrub_emails_replaces_addresses(self):
        from auctions.helper_functions import scrub_emails

        self.assertEqual(
            scrub_emails("joe@example.com looks fake"),
            "[email redacted] looks fake",
        )
        self.assertEqual(
            scrub_emails("both a@b.co and c.d+tag@e.example.org"),
            "both [email redacted] and [email redacted]",
        )
        # Non-strings (API details are sometimes dicts) and empty values must not blow up.
        self.assertEqual(scrub_emails(""), "")
        self.assertIsNone(scrub_emails(None))
        self.assertNotIn("joe@example.com", scrub_emails({"detail": "joe@example.com"}))

    @patch("auctions.mailchimp.get_client")
    def test_mailchimp_rejection_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.lists.set_list_member.side_effect = self._mailchimp_error(400)
        mock_get_client.return_value = client

        with self.assertLogs("auctions.mailchimp", level="WARNING") as logs:
            self.assertFalse(mc.sync_member(self.member))

        output = "\n".join(logs.output)
        self.assertNotIn("joe@example.com", output)
        self.assertIn("[email redacted]", output)
        self.assertIn(str(self.member.pk), output)

    @patch("auctions.mailchimp.get_client")
    def test_mailchimp_sync_failure_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.lists.set_list_member.side_effect = self._mailchimp_error(500)
        mock_get_client.return_value = client

        with self.assertLogs("auctions.mailchimp", level="ERROR") as logs:
            self.assertFalse(mc.sync_member(self.member))

        self.assertNotIn("joe@example.com", "\n".join(logs.output))

    @patch("auctions.mailchimp.get_client")
    def test_mailchimp_tag_failure_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.lists.set_list_member.return_value = {"web_id": 1, "status": "subscribed"}
        client.lists.update_list_member_tags.side_effect = self._mailchimp_error(400)
        mock_get_client.return_value = client

        with self.assertLogs("auctions.mailchimp", level="ERROR") as logs:
            mc.sync_member(self.member)

        self.assertNotIn("joe@example.com", "\n".join(logs.output))

    @patch("auctions.mailchimp.get_client")
    def test_mailchimp_change_email_failure_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.lists.update_list_member.side_effect = self._mailchimp_error(500)
        mock_get_client.return_value = client

        with self.assertLogs("auctions.mailchimp", level="ERROR") as logs:
            mc.change_member_email(self.member, "old@example.com")

        output = "\n".join(logs.output)
        self.assertNotIn("joe@example.com", output)
        self.assertNotIn("old@example.com", output)

    @patch("auctions.brevo.get_client")
    def test_brevo_rejection_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.request.side_effect = brevo.BrevoApiError(400, "Invalid email address: joe@example.com")
        mock_get_client.return_value = client

        with self.assertLogs("auctions.brevo", level="WARNING") as logs:
            self.assertFalse(brevo.sync_member(self.member))

        output = "\n".join(logs.output)
        self.assertNotIn("joe@example.com", output)
        self.assertIn("[email redacted]", output)
        self.assertIn(str(self.member.pk), output)

    @patch("auctions.brevo.get_client")
    def test_brevo_sync_failure_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.request.side_effect = brevo.BrevoApiError(500, "Server error syncing joe@example.com")
        mock_get_client.return_value = client

        with self.assertLogs("auctions.brevo", level="ERROR") as logs:
            self.assertFalse(brevo.sync_member(self.member))

        self.assertNotIn("joe@example.com", "\n".join(logs.output))

    @patch("auctions.brevo.get_client")
    def test_brevo_change_email_failure_logs_no_email(self, mock_get_client):
        client = MagicMock()
        client.request.side_effect = brevo.BrevoApiError(500, "Could not delete old@example.com")
        mock_get_client.return_value = client

        with self.assertLogs("auctions.brevo", level="ERROR") as logs:
            brevo.change_member_email(self.member, "old@example.com")

        self.assertNotIn("old@example.com", "\n".join(logs.output))


class BrevoWebhookTests(TestCase):
    """Inbound webhook only records Brevo status; never touches the site account."""

    def setUp(self):
        self.client_http = Client()
        self.club = Club.objects.create(name="Brevo Hook Club")
        self.club.brevo_api_key = "xkeysib-test"
        self.club.brevo_list_id = "7"
        self.club.brevo_webhook_secret = "secret123"
        self.club.save()
        self.user = User.objects.create_user(username="brevohook", password="pw", email="bhook@example.com")
        UserData.objects.get_or_create(user=self.user)
        self.member = ClubMember.objects.create(
            club=self.club, user=self.user, name="Hook Member", email="bhook@example.com", brevo_status="subscribed"
        )

    def _url(self, secret="secret123"):
        return reverse("brevo_webhook", kwargs={"slug": self.club.slug, "secret": secret})

    def _post(self, payload, secret="secret123"):
        return self.client_http.post(self._url(secret), data=json.dumps(payload), content_type="application/json")

    def test_get_verification_ok(self):
        self.assertEqual(self.client_http.get(self._url()).status_code, 200)

    def test_bad_secret_forbidden(self):
        self.assertEqual(self.client_http.get(self._url(secret="wrong")).status_code, 403)
        resp = self._post({"event": "unsubscribe", "email": "bhook@example.com"}, secret="wrong")
        self.assertEqual(resp.status_code, 403)

    def test_unsubscribe_sets_status_without_touching_account(self):
        resp = self._post({"event": "unsubscribe", "email": "bhook@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "unsubscribed")
        # The site-wide account preference must be untouched.
        self.assertFalse(self.user.userdata.has_unsubscribed)

    def test_hard_bounce_marks_cleaned(self):
        self._post({"event": "hard_bounce", "email": "bhook@example.com"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "cleaned")

    def test_spam_marks_cleaned(self):
        self._post({"event": "spam", "email": "bhook@example.com"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "cleaned")

    def test_webhook_events_write_club_history(self):
        self._post({"event": "unsubscribe", "email": "bhook@example.com"})
        self._post({"event": "hard_bounce", "email": "bhook@example.com"})
        actions = list(
            ClubHistory.objects.filter(club=self.club, applies_to="MEMBERS").values_list("action", flat=True)
        )
        self.assertEqual(len(actions), 2)
        self.assertTrue(any("unsubscribed at Brevo" in a for a in actions))
        self.assertTrue(any("marked undeliverable (hardbounce) by Brevo" in a for a in actions))

    def test_event_for_unknown_email_writes_no_history(self):
        self._post({"event": "unsubscribe", "email": "nobody@example.com"})
        self.assertFalse(ClubHistory.objects.filter(club=self.club).exists())


class BrevoSelfServiceTests(TestCase):
    """The shared self-service links also clear the remembered Brevo opt-out on resubscribe."""

    def setUp(self):
        self.client_http = Client()
        self.club = Club.objects.create(name="Brevo Self Serve Club")
        self.member = ClubMember.objects.create(club=self.club, name="Sam", email="sam2@example.com")

    def test_resubscribe_clears_brevo_status(self):
        self.member.contact_status = "non_essential"
        self.member.brevo_status = "unsubscribed"
        self.member.save()
        url = reverse("club_member_resubscribe", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
        self.client_http.post(url)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "contact")
        self.assertEqual(self.member.brevo_status, "")


class BrevoConnectViewTests(TestCase):
    """Pasting an API key validates it against Brevo, then stores it encrypted at rest."""

    def setUp(self):
        self.client_http = Client()
        self.club = Club.objects.create(name="Brevo Connect Club")
        self.admin = User.objects.create_superuser("brevoadmin", "ba@example.com", "pw")
        UserData.objects.get_or_create(user=self.admin)
        self.client_http.force_login(self.admin)

    def _url(self):
        return reverse("brevo_connect", kwargs={"slug": self.club.slug})

    def test_valid_key_is_stored(self):
        with patch("auctions.brevo.list_contact_lists", return_value=[]):
            resp = self.client_http.post(self._url(), {"api_key": "xkeysib-good"})
        self.assertEqual(resp.status_code, 302)
        self.club.refresh_from_db()
        self.assertEqual(self.club.brevo_api_key, "xkeysib-good")
        self.assertTrue(self.club.brevo_webhook_secret)
        self.assertIsNotNone(self.club.brevo_connected_on)

    def test_invalid_key_is_rejected(self):
        with patch("auctions.brevo.list_contact_lists", side_effect=brevo.BrevoApiError(401, "unauthorized")):
            resp = self.client_http.post(self._url(), {"api_key": "bad"})
        self.assertEqual(resp.status_code, 302)
        self.club.refresh_from_db()
        self.assertFalse(self.club.brevo_api_key)

    def test_empty_key_rejected(self):
        self.client_http.post(self._url(), {"api_key": ""})
        self.club.refresh_from_db()
        self.assertFalse(self.club.brevo_api_key)

    def test_ip_block_reports_ip_and_does_not_store_key(self):
        from django.contrib.messages import get_messages

        err = brevo.BrevoApiError(401, "API Key used from an IP address (203.0.113.9) that is not authorized")
        with patch("auctions.brevo.list_contact_lists", side_effect=err):
            resp = self.client_http.post(self._url(), {"api_key": "xkeysib-ip-blocked"})
        self.assertEqual(resp.status_code, 302)
        self.club.refresh_from_db()
        self.assertFalse(self.club.brevo_api_key)
        msgs = [m.message for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("203.0.113.9" in m for m in msgs))


class BrevoErrorClassificationTests(TestCase):
    """Telling Brevo's blocked-IP 401 apart from a bad-key 401 (both share code 'unauthorized')."""

    def test_ip_block_returns_ip(self):
        exc = brevo.BrevoApiError(401, "Using this API Key from an IP address (1.2.3.4) that is not authorized")
        self.assertEqual(brevo.blocked_ip_from_error(exc), "1.2.3.4")

    def test_ip_block_without_parseable_ip_returns_empty(self):
        exc = brevo.BrevoApiError(401, "This IP address is not in your authorized IPs list")
        self.assertEqual(brevo.blocked_ip_from_error(exc), "")

    def test_bad_key_returns_none(self):
        self.assertIsNone(brevo.blocked_ip_from_error(brevo.BrevoApiError(401, "Key not found")))
        self.assertIsNone(brevo.blocked_ip_from_error(brevo.BrevoApiError(401, "unauthorized")))

    def test_non_401_returns_none(self):
        self.assertIsNone(brevo.blocked_ip_from_error(brevo.BrevoApiError(400, "bad request from 1.2.3.4")))
