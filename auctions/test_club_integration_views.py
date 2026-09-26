"""The views a club uses to plug in outside services, and two of its HTMx admin dialogs.

Covers the Discord interactions endpoint (signature and timestamp checks, PING, the join button and join
modal) and the Discord role and join-message settings pages; the Mailchimp OAuth callback, Mailchimp
audience and Brevo list pickers, and the gaps in the webhook tests in test_marketing.py; the BAP award
create/edit/delete dialog; and the club member edit dialog. Every call to Discord, Mailchimp or Brevo is
mocked.
"""

import datetime
import json
from time import time
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import mailchimp as mc
from auctions.models import (
    Auction,
    BapAward,
    Club,
    ClubDiscordRole,
    ClubHistory,
    ClubMember,
    Lot,
    UserData,
)
from auctions.views.discord import (
    _DISCORD_TYPE_CHANNEL_MESSAGE,
    _DISCORD_TYPE_COMPONENT,
    _DISCORD_TYPE_MODAL,
    _DISCORD_TYPE_MODAL_SUBMIT,
    _DISCORD_TYPE_PING,
)


def make_user(username, **kwargs):
    user = User.objects.create_user(username=username, password="pw", email=f"{username}@example.com", **kwargs)
    UserData.objects.get_or_create(user=user)
    return user


class ClubFixtureMixin:
    """A club with an admin, a member holding one named permission, a plain member and a stranger, plus a
    second club with its own admin.
    """

    permission = "permission_edit_club"

    @classmethod
    def setUpTestData(cls):
        cls.club = Club.objects.create(name="Integration Club")
        cls.other_club = Club.objects.create(name="Other Club")
        cls.admin = make_user("club_admin")
        ClubMember.objects.create(club=cls.club, user=cls.admin, name="Club Admin", permission_admin=True)
        cls.permitted = make_user("permitted")
        ClubMember.objects.create(club=cls.club, user=cls.permitted, name="Permitted", **{cls.permission: True})
        cls.plain = make_user("plain")
        ClubMember.objects.create(club=cls.club, user=cls.plain, name="Plain Member")
        cls.stranger = make_user("stranger")
        cls.other_admin = make_user("other_admin")
        ClubMember.objects.create(club=cls.other_club, user=cls.other_admin, name="Other", permission_admin=True)


# --- Discord interactions ------------------------------------------------------------------------


@override_settings(DISCORD_PUBLIC_KEY="aa" * 32, DISCORD_BOT_TOKEN="")
class DiscordInteractionsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.club = Club.objects.create(name="Discord Club", discord_server_id="guild-1")

    def _post(self, payload, timestamp=None, signature="00"):
        return self.client.post(
            reverse("discord_interactions"),
            data=json.dumps(payload).encode(),
            content_type="application/json",
            HTTP_X_SIGNATURE_ED25519=signature,
            HTTP_X_SIGNATURE_TIMESTAMP=str(int(time()) if timestamp is None else timestamp),
        )

    def _modal(self, email, name="New Person", discord_id="555", username="newbie", guild_id="guild-1"):
        return {
            "type": _DISCORD_TYPE_MODAL_SUBMIT,
            "guild_id": guild_id,
            "member": {"user": {"id": discord_id, "username": username}},
            "data": {
                "custom_id": "join_modal",
                "components": [
                    {"components": [{"custom_id": "name", "value": name}]},
                    {"components": [{"custom_id": "email", "value": email}]},
                ],
            },
        }

    def _content(self, response):
        return json.loads(response.content)["data"]["content"]

    def test_a_real_signature_over_a_tampered_body_is_refused(self):
        """Uses a real Ed25519 key rather than the mock, so the check itself is exercised."""
        key = Ed25519PrivateKey.generate()
        public_hex = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        timestamp = str(int(time()))
        signed = json.dumps({"type": _DISCORD_TYPE_PING}).encode()
        signature = key.sign(timestamp.encode() + signed).hex()
        url = reverse("discord_interactions")
        with self.settings(DISCORD_PUBLIC_KEY=public_hex):
            good = self.client.post(
                url,
                data=signed,
                content_type="application/json",
                HTTP_X_SIGNATURE_ED25519=signature,
                HTTP_X_SIGNATURE_TIMESTAMP=timestamp,
            )
            tampered = self.client.post(
                url,
                data=json.dumps({"type": _DISCORD_TYPE_PING, "x": 1}).encode(),
                content_type="application/json",
                HTTP_X_SIGNATURE_ED25519=signature,
                HTTP_X_SIGNATURE_TIMESTAMP=timestamp,
            )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(tampered.status_code, 403)

    @patch("auctions.views.discord.verify_discord_signature", return_value=False)
    def test_bad_signature_is_refused(self, verify):
        self.assertEqual(self._post({"type": _DISCORD_TYPE_PING}).status_code, 403)

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_stale_timestamp_is_refused_before_the_signature_is_checked(self, verify):
        response = self._post({"type": _DISCORD_TYPE_PING}, timestamp=int(time()) - 3600)
        self.assertEqual(response.status_code, 403)
        verify.assert_not_called()

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_missing_headers_and_garbage_timestamp_are_bad_requests(self, verify):
        url = reverse("discord_interactions")
        self.assertEqual(self.client.post(url, data=b"{}", content_type="application/json").status_code, 400)
        self.assertEqual(self._post({"type": _DISCORD_TYPE_PING}, timestamp="soon").status_code, 400)

    @override_settings(DISCORD_PUBLIC_KEY="")
    def test_unconfigured_site_refuses_everything(self):
        self.assertEqual(self._post({"type": _DISCORD_TYPE_PING}).status_code, 403)

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_ping_is_answered_with_pong(self, verify):
        response = self._post({"type": _DISCORD_TYPE_PING})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"type": _DISCORD_TYPE_PING})

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_join_button_opens_the_modal_for_a_stranger_but_not_a_member(self, verify):
        button = {
            "type": _DISCORD_TYPE_COMPONENT,
            "guild_id": "guild-1",
            "data": {"custom_id": "join_button"},
            "member": {"user": {"id": "777", "username": "clicker"}},
        }
        self.assertEqual(json.loads(self._post(button).content)["type"], _DISCORD_TYPE_MODAL)
        ClubMember.objects.create(club=self.club, name="Clicker", discord_id="777")
        self.assertEqual(json.loads(self._post(button).content)["type"], _DISCORD_TYPE_CHANNEL_MESSAGE)

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_join_modal_creates_a_member(self, verify):
        response = self._post(self._modal("New@Example.com"))
        self.assertEqual(response.status_code, 200)
        member = ClubMember.objects.get(club=self.club, discord_id="555")
        self.assertEqual(member.name, "New Person")
        self.assertEqual(member.email, "new@example.com")
        self.assertEqual(member.discord_username, "newbie")
        self.assertEqual(member.source, "discord")
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="added via Discord").exists())

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_join_modal_from_an_existing_discord_id_changes_nothing(self, verify):
        existing = ClubMember.objects.create(
            club=self.club, name="Already Here", email="here@example.com", discord_id="555"
        )
        response = self._post(self._modal("someone-else@example.com", name="Renamed"))
        self.assertIn("already registered", self._content(response))
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Already Here")
        self.assertEqual(existing.email, "here@example.com")
        self.assertEqual(ClubMember.objects.filter(club=self.club).count(), 1)

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_join_modal_refuses_an_email_linked_to_another_discord_account(self, verify):
        owner = ClubMember.objects.create(club=self.club, name="Owner", email="owner@example.com", discord_id="111")
        response = self._post(self._modal("owner@example.com", discord_id="999"))
        self.assertIn("linked to another Discord account", self._content(response))
        owner.refresh_from_db()
        self.assertEqual(owner.discord_id, "111")
        self.assertFalse(ClubMember.objects.filter(discord_id="999").exists())

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_join_modal_rejects_an_invalid_email_without_creating_anyone(self, verify):
        response = self._post(self._modal("not an email"))
        self.assertIn("valid email", self._content(response))
        self.assertFalse(ClubMember.objects.filter(club=self.club).exists())

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_join_modal_for_an_unconnected_server_creates_nobody(self, verify):
        response = self._post(self._modal("x@example.com", guild_id="unknown-guild"))
        self.assertIn("No club", self._content(response))
        self.assertFalse(ClubMember.objects.filter(discord_id="555").exists())

    # Accepted: the typed email isn't verified (the Discord settings page says so), so it links whichever
    # member has that address. A Discord account links once, and the reply doesn't say which case happened.
    @override_settings(DISCORD_BOT_TOKEN="bot-token")
    @patch("requests.delete")
    @patch("requests.put")
    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    def test_an_email_claim_links_the_existing_member_and_says_nothing_about_them(self, verify, put, delete):
        put.return_value = MagicMock(status_code=204)
        delete.return_value = MagicMock(status_code=204)
        member = ClubMember.objects.create(club=self.club, name="Paid Member", email="paid@example.com")
        response = self._post(self._modal("paid@example.com", name="Someone", discord_id="666", username="someone"))
        member.refresh_from_db()
        self.assertEqual(member.discord_id, "666")
        self.assertNotIn("Paid Member", self._content(response))


class ClubDiscordEditRoleViewTests(ClubFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.club.discord_server_id = "guild-2"
        cls.club.save()
        cls.old_paid = ClubDiscordRole.objects.create(
            club=cls.club, role_id="r1", role_name="Old Paid", is_paid_role=True
        )
        cls.role = ClubDiscordRole.objects.create(club=cls.club, role_id="r2", role_name="Members")
        cls.too_high = ClubDiscordRole.objects.create(
            club=cls.club, role_id="r3", role_name="Mods", bot_can_manage=False
        )
        cls.foreign_role = ClubDiscordRole.objects.create(club=cls.other_club, role_id="r4", role_name="Theirs")

    def _url(self, role, club=None):
        return reverse("club_discord_edit_role", kwargs={"slug": (club or self.club).slug, "pk": role.pk})

    def test_permission_gate(self):
        self.assertEqual(self.client.get(self._url(self.role)).status_code, 302)
        self.client.force_login(self.plain)
        self.assertEqual(self.client.post(self._url(self.role), {"is_paid_role": "on"}).status_code, 403)
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.post(self._url(self.role), {"is_paid_role": "on"}).status_code, 403)
        self.role.refresh_from_db()
        self.assertFalse(self.role.is_paid_role)

    def test_marking_a_paid_role_unmarks_the_previous_one(self):
        self.client.force_login(self.permitted)
        response = self.client.post(
            self._url(self.role), {"is_paid_role": "on", "bap_points_for_role": "25", "hap_points_for_role": "-4"}
        )
        self.assertRedirects(
            response, reverse("club_discord_config", kwargs={"slug": self.club.slug}), fetch_redirect_response=False
        )
        self.role.refresh_from_db()
        self.old_paid.refresh_from_db()
        self.assertTrue(self.role.is_paid_role)
        self.assertEqual(self.role.bap_points_for_role, 25)
        self.assertEqual(self.role.hap_points_for_role, 0)
        self.assertFalse(self.old_paid.is_paid_role)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="Members").exists())

    def test_garbage_points_are_zero(self):
        self.client.force_login(self.permitted)
        self.client.post(self._url(self.role), {"bap_points_for_role": "lots"})
        self.role.refresh_from_db()
        self.assertEqual(self.role.bap_points_for_role, 0)

    def test_a_role_above_the_bot_cannot_be_edited(self):
        self.client.force_login(self.permitted)
        self.assertEqual(self.client.get(self._url(self.too_high)).status_code, 302)
        self.client.post(self._url(self.too_high), {"is_paid_role": "on"})
        self.too_high.refresh_from_db()
        self.assertFalse(self.too_high.is_paid_role)

    def test_another_clubs_role_is_not_found(self):
        self.client.force_login(self.permitted)
        self.assertEqual(self.client.post(self._url(self.foreign_role), {"is_paid_role": "on"}).status_code, 404)
        self.foreign_role.refresh_from_db()
        self.assertFalse(self.foreign_role.is_paid_role)


class ClubDiscordSendJoinMessageViewTests(ClubFixtureMixin, TestCase):
    def _url(self):
        return reverse("club_discord_send_join_message", kwargs={"slug": self.club.slug})

    @override_settings(DISCORD_BOT_TOKEN="bot-token")
    @patch("auctions.views.discord.requests.post")
    def test_permission_gate(self, post):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.post(self._url(), {"channel_id": "chan"}).status_code, 403)
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.post(self._url(), {"channel_id": "chan"}).status_code, 403)
        post.assert_not_called()

    @override_settings(DISCORD_BOT_TOKEN="bot-token")
    @patch("auctions.views.discord.requests.post")
    @patch("auctions.views.discord.requests.get")
    def test_posts_a_join_button_to_the_channel(self, get, post):
        # The channel must be a snowflake in this club's own server: see test_site_account_fixes.
        Club.objects.filter(pk=self.club.pk).update(discord_server_id="777")
        get.return_value = MagicMock(status_code=200)
        get.return_value.json.return_value = {"id": "42", "guild_id": "777"}
        post.return_value = MagicMock(status_code=200)
        self.client.force_login(self.permitted)
        response = self.client.post(self._url(), {"channel_id": " 42 "})
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://discord.com/api/v10/channels/42/messages")
        button = post.call_args.kwargs["json"]["components"][0]["components"][0]
        self.assertEqual(button["custom_id"], "join_button")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bot bot-token")

    @override_settings(DISCORD_BOT_TOKEN="bot-token")
    @patch("auctions.views.discord.requests.post")
    def test_blank_channel_sends_nothing(self, post):
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"channel_id": "  "})
        post.assert_not_called()

    @override_settings(DISCORD_BOT_TOKEN="")
    @patch("auctions.views.discord.requests.post")
    def test_no_bot_token_sends_nothing(self, post):
        self.client.force_login(self.permitted)
        self.assertEqual(self.client.post(self._url(), {"channel_id": "chan"}).status_code, 302)
        post.assert_not_called()


# --- Mailchimp and Brevo -------------------------------------------------------------------------


class MailchimpCallbackViewTests(ClubFixtureMixin, TestCase):
    def _start(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["mailchimp_oauth_club_slug"] = self.club.slug
        session["mailchimp_oauth_state"] = "state-for-this-connect"
        session.save()
        return "state-for-this-connect"

    @patch("auctions.mailchimp.exchange_oauth_code", return_value=("tok-123", "us9"))
    def test_the_unsubscribe_uuid_is_not_the_state(self, exchange):
        """It is in every email footer, so anyone the user forwarded an email to could finish the flow."""
        self._start(self.permitted)
        self.client.get(
            reverse("mailchimp_callback"), {"code": "abc", "state": self.permitted.userdata.unsubscribe_link}
        )
        exchange.assert_not_called()

    @override_settings(MAILCHIMP_CLIENT_ID="client-1")
    def test_connecting_puts_a_fresh_state_in_the_session(self):
        self.client.force_login(self.permitted)
        response = self.client.get(reverse("mailchimp_connect", kwargs={"slug": self.club.slug}))
        state = self.client.session["mailchimp_oauth_state"]
        self.assertIn(f"state={state}", response["Location"])
        self.assertNotEqual(state, str(self.permitted.userdata.unsubscribe_link))

    @patch("auctions.mailchimp.exchange_oauth_code", return_value=("tok-123", "us9"))
    def test_stores_the_token_and_makes_a_webhook_secret(self, exchange):
        state = self._start(self.permitted)
        response = self.client.get(reverse("mailchimp_callback"), {"code": "abc", "state": state})
        self.assertRedirects(
            response,
            reverse("club_mailchimp_config", kwargs={"slug": self.club.slug}),
            fetch_redirect_response=False,
        )
        self.assertEqual(exchange.call_args.args[0], "abc")
        self.club.refresh_from_db()
        self.assertEqual(self.club.mailchimp_access_token, "tok-123")
        self.assertEqual(self.club.mailchimp_server_prefix, "us9")
        self.assertEqual(self.club.mailchimp_connected_by, self.permitted)
        self.assertTrue(self.club.mailchimp_webhook_secret)
        self.assertNotIn("mailchimp_oauth_club_slug", self.client.session)

    @patch("auctions.mailchimp.exchange_oauth_code", return_value=("tok-123", "us9"))
    def test_a_wrong_state_is_refused(self, exchange):
        self._start(self.permitted)
        self.client.get(reverse("mailchimp_callback"), {"code": "abc", "state": "someone-elses"})
        exchange.assert_not_called()
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_access_token)

    @patch("auctions.mailchimp.exchange_oauth_code", return_value=("tok-123", "us9"))
    def test_a_user_without_permission_cannot_finish_the_flow(self, exchange):
        state = self._start(self.plain)
        response = self.client.get(reverse("mailchimp_callback"), {"code": "abc", "state": state})
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        exchange.assert_not_called()
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_access_token)

    @patch("auctions.mailchimp.exchange_oauth_code", return_value=("tok-123", "us9"))
    def test_no_session_means_no_club(self, exchange):
        self.client.force_login(self.permitted)
        response = self.client.get(
            reverse("mailchimp_callback"), {"code": "abc", "state": self.permitted.userdata.unsubscribe_link}
        )
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        exchange.assert_not_called()

    @patch("auctions.mailchimp.exchange_oauth_code", side_effect=mc.MailchimpError("nope"))
    def test_a_failed_exchange_stores_nothing(self, exchange):
        state = self._start(self.permitted)
        self.client.get(reverse("mailchimp_callback"), {"code": "abc", "state": state})
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_access_token)
        self.assertFalse(self.club.mailchimp_webhook_secret)


@patch("auctions.mailchimp.account_defaults", return_value=None)
@patch("auctions.mailchimp.backfill", return_value=3)
@patch("auctions.mailchimp.ensure_webhook")
@patch("auctions.mailchimp.ensure_segments")
@patch("auctions.mailchimp.ensure_merge_fields")
@patch("auctions.mailchimp.list_audiences", return_value=[{"id": "aud-1", "name": "Fish People"}])
@patch("auctions.mailchimp.get_client", return_value=MagicMock())
class MailchimpAudienceSelectViewTests(ClubFixtureMixin, TestCase):
    def _url(self):
        return reverse("mailchimp_select_audience", kwargs={"slug": self.club.slug})

    def test_permission_gate(self, *mocks):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.post(self._url(), {"audience_id": "aud-1"}).status_code, 403)
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.post(self._url(), {"audience_id": "aud-1"}).status_code, 403)
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_audience_id)

    def test_picking_an_audience_saves_it_and_provisions(
        self, get_client, list_audiences, merge, seg, hook, backfill, defaults
    ):
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"audience_id": "aud-1"})
        self.club.refresh_from_db()
        self.assertEqual(self.club.mailchimp_audience_id, "aud-1")
        self.assertEqual(self.club.mailchimp_audience_name, "Fish People")
        merge.assert_called_once()
        seg.assert_called_once()
        hook.assert_called_once()
        backfill.assert_called_once()
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="Fish People").exists())

    @patch("auctions.mailchimp.create_audience", side_effect=mc.MailchimpError("no sender"))
    def test_a_failed_new_audience_saves_nothing(
        self, create, get_client, list_audiences, merge, seg, hook, backfill, defaults
    ):
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"audience_id": "__new__"})
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_audience_id)
        hook.assert_not_called()

    def test_blank_choice_saves_nothing(self, get_client, list_audiences, merge, seg, hook, backfill, defaults):
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"audience_id": ""})
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_audience_id)
        backfill.assert_not_called()

    def test_not_connected_saves_nothing(self, get_client, list_audiences, merge, seg, hook, backfill, defaults):
        get_client.return_value = None
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"audience_id": "aud-1"})
        self.club.refresh_from_db()
        self.assertFalse(self.club.mailchimp_audience_id)


@patch("auctions.brevo.account_info", return_value={"company": "", "address": {}})
@patch("auctions.brevo.backfill", return_value=2)
@patch("auctions.brevo.ensure_webhook")
@patch("auctions.brevo.ensure_attributes")
@patch("auctions.brevo.list_contact_lists", return_value=[{"id": 7, "name": "Club List"}])
@patch("auctions.brevo.get_client", return_value=MagicMock())
class BrevoListSelectViewTests(ClubFixtureMixin, TestCase):
    def _url(self):
        return reverse("brevo_select_list", kwargs={"slug": self.club.slug})

    def test_permission_gate(self, *mocks):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.post(self._url(), {"list_id": "7"}).status_code, 403)
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.post(self._url(), {"list_id": "7"}).status_code, 403)
        self.club.refresh_from_db()
        self.assertFalse(self.club.brevo_list_id)

    def test_picking_a_list_saves_it_and_provisions(self, get_client, lists, attrs, hook, backfill, info):
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"list_id": "7"})
        self.club.refresh_from_db()
        self.assertEqual(self.club.brevo_list_id, "7")
        self.assertEqual(self.club.brevo_list_name, "Club List")
        attrs.assert_called_once()
        hook.assert_called_once()
        backfill.assert_called_once()

    def test_a_list_not_in_the_account_is_refused(self, get_client, lists, attrs, hook, backfill, info):
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"list_id": "99"})
        self.club.refresh_from_db()
        self.assertFalse(self.club.brevo_list_id)
        hook.assert_not_called()

    def test_prefills_a_blank_donation_address_only(self, get_client, lists, attrs, hook, backfill, info):
        info.return_value = {"company": "", "address": {"street": "1 Reef Rd", "city": "Tampa", "zipCode": "33601"}}
        self.client.force_login(self.permitted)
        self.client.post(self._url(), {"list_id": "7"})
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_mailing_address, "1 Reef Rd\nTampa 33601")

        Club.objects.filter(pk=self.club.pk).update(donation_mailing_address="Typed by the club", brevo_list_id="")
        self.client.post(self._url(), {"list_id": "7"})
        self.club.refresh_from_db()
        self.assertEqual(self.club.donation_mailing_address, "Typed by the club")


class WebhookEdgeCaseTests(TestCase):
    """What test_marketing.py's webhook tests don't: a club with no secret, and events with no email."""

    @classmethod
    def setUpTestData(cls):
        cls.club = Club.objects.create(
            name="Hook Edge Club", mailchimp_webhook_secret="mc-secret", brevo_webhook_secret="br-secret"
        )
        cls.unhooked = Club.objects.create(name="No Hooks Club")
        cls.member = ClubMember.objects.create(club=cls.club, name="Edge Member", email="edge@example.com")
        cls.other = ClubMember.objects.create(club=cls.club, name="Other Member", email="other@example.com")

    def test_a_club_without_a_secret_refuses_any_secret(self):
        for name in ("mailchimp_webhook", "brevo_webhook"):
            url = reverse(name, kwargs={"slug": self.unhooked.slug, "secret": "anything"})
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_another_clubs_secret_is_refused(self):
        url = reverse("mailchimp_webhook", kwargs={"slug": self.unhooked.slug, "secret": "mc-secret"})
        self.assertEqual(
            self.client.post(url, {"type": "unsubscribe", "data[email]": "edge@example.com"}).status_code, 403
        )

    def test_mailchimp_unsubscribe_without_an_email_touches_nobody(self):
        url = reverse("mailchimp_webhook", kwargs={"slug": self.club.slug, "secret": "mc-secret"})
        self.assertEqual(self.client.post(url, {"type": "unsubscribe"}).status_code, 200)
        self.assertFalse(ClubMember.objects.filter(club=self.club).exclude(mailchimp_status="").exists())

    def test_brevo_unsubscribe_updates_only_that_member_case_insensitively(self):
        url = reverse("brevo_webhook", kwargs={"slug": self.club.slug, "secret": "br-secret"})
        self.client.post(
            url, data=json.dumps({"event": "unsubscribe", "email": "EDGE@example.com"}), content_type="application/json"
        )
        self.member.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "unsubscribed")
        self.assertFalse(self.other.brevo_status)

    def test_brevo_contact_deleted_archives_and_bad_json_is_ignored(self):
        url = reverse("brevo_webhook", kwargs={"slug": self.club.slug, "secret": "br-secret"})
        self.assertEqual(self.client.post(url, data=b"{not json", content_type="application/json").status_code, 200)
        self.client.post(
            url,
            data=json.dumps({"event": "contact_deleted", "email": "edge@example.com"}),
            content_type="application/json",
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.brevo_status, "archived")


# --- BAP awards ----------------------------------------------------------------------------------


class BapAwardAdminViewTests(ClubFixtureMixin, TestCase):
    permission = "permission_manage_bap"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.breeder = ClubMember.objects.create(club=cls.club, name="Breeder", email="breeder@example.com")
        cls.outsider = ClubMember.objects.create(club=cls.other_club, name="Outsider", email="out@example.com")
        cls.viewer = make_user("viewer")
        ClubMember.objects.create(club=cls.club, user=cls.viewer, name="Viewer", permission_view=True)

    def _create_url(self, club=None):
        return reverse("bapaward_create", kwargs={"slug": (club or self.club).slug})

    def _data(self, member=None, **overrides):
        data = {"club_member": (member or self.breeder).pk, "date": "2026-09-01", "points": "10", "notes": "fry"}
        data.update(overrides)
        return data

    def test_permission_gate(self):
        for user in (self.viewer, self.stranger, self.other_admin):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self._create_url(), self._data()).status_code, 403, user)
        self.assertFalse(BapAward.objects.exists())

    def test_create_awards_points_and_totals_them(self):
        self.client.force_login(self.permitted)
        response = self.client.post(self._create_url(), self._data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bapAwardListChanged")
        award = BapAward.objects.get()
        self.assertEqual(award.points, 10)
        self.assertEqual(award.awarded_by, self.permitted)
        self.breeder.refresh_from_db()
        self.assertEqual(self.breeder.bap_points, 10)
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, applies_to="BAP", action__startswith="Added").exists()
        )

    def test_a_member_of_another_club_cannot_be_awarded(self):
        self.client.force_login(self.permitted)
        response = self.client.post(self._create_url(), self._data(member=self.outsider))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BapAward.objects.exists())

    def test_edit_changes_points_and_recalculates(self):
        award = BapAward.objects.create(club_member=self.breeder, date=datetime.date(2026, 9, 1), points=10)
        self.client.force_login(self.permitted)
        url = reverse("bapaward_admin", kwargs={"pk": award.pk})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, self._data(points="4"))
        award.refresh_from_db()
        self.breeder.refresh_from_db()
        self.assertEqual(award.points, 4)
        self.assertEqual(self.breeder.bap_points, 4)

    def test_another_clubs_admin_cannot_edit_or_delete(self):
        award = BapAward.objects.create(club_member=self.breeder, date=datetime.date(2026, 9, 1), points=10)
        self.client.force_login(self.other_admin)
        self.assertEqual(
            self.client.post(reverse("bapaward_admin", kwargs={"pk": award.pk}), self._data(points="99")).status_code,
            403,
        )
        self.assertEqual(self.client.post(reverse("bapaward_delete", kwargs={"pk": award.pk})).status_code, 403)
        award.refresh_from_db()
        self.assertEqual(award.points, 10)

    def _club_lot(self, club):
        auction = Auction.objects.create(
            title=f"{club.name} spring auction", club=club, is_online=False, date_start=timezone.now()
        )
        return Lot.objects.create(lot_name="Endler fry", quantity=1, auction=auction)

    def test_another_clubs_lot_is_not_marked(self):
        """``?lot_pk=`` is scoped to this club's auctions; another club's lot left their points queue."""
        lot = self._club_lot(self.other_club)
        self.client.force_login(self.permitted)
        self.client.post(self._create_url() + f"?lot_pk={lot.pk}", self._data(points="15"))
        lot.refresh_from_db()
        self.assertIsNone(BapAward.objects.get().lot)
        self.assertFalse(lot.manually_approved)

    def test_award_for_a_lot_marks_it_and_delete_undoes_it(self):
        lot = self._club_lot(self.club)
        self.client.force_login(self.permitted)
        self.client.post(self._create_url() + f"?lot_pk={lot.pk}", self._data(points="15"))
        award = BapAward.objects.get()
        lot.refresh_from_db()
        self.assertEqual(award.lot, lot)
        self.assertEqual(lot.bap_points_awarded, 15)
        self.assertTrue(lot.manually_approved)

        self.assertEqual(self.client.post(reverse("bapaward_delete", kwargs={"pk": award.pk})).status_code, 200)
        self.assertFalse(BapAward.objects.exists())
        lot.refresh_from_db()
        self.breeder.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, 0)
        self.assertFalse(lot.manually_approved)
        self.assertEqual(self.breeder.bap_points, 0)

    def test_missing_award_is_not_found(self):
        self.client.force_login(self.permitted)
        self.assertEqual(self.client.get(reverse("bapaward_admin", kwargs={"pk": 999999})).status_code, 404)


# --- Club member edit dialog ---------------------------------------------------------------------


class ClubMemberAdminViewTests(ClubFixtureMixin, TestCase):
    permission = "permission_add_edit"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.viewer = make_user("viewer")
        ClubMember.objects.create(club=cls.club, user=cls.viewer, name="Viewer", permission_view=True)
        # permission_add_edit alone doesn't open the dialog; it also needs view (or manage_auctions).
        ClubMember.objects.filter(club=cls.club, user=cls.permitted).update(permission_view=True)
        cls.target = ClubMember.objects.create(
            club=cls.club, name="Target Member", email="target@example.com", bidder_number="12"
        )
        cls.holder = ClubMember.objects.create(club=cls.club, name="Holder", email="h@example.com", bidder_number="77")

    def _url(self, member=None):
        return reverse("clubmember_admin", kwargs={"pk": (member or self.target).pk})

    def _data(self, **overrides):
        data = {
            "name": "Renamed Member",
            "memo": "met at swap",
            "email": "renamed@example.com",
            "phone_number": "",
            "address": "",
            "contact_status": "contact",
            "bidder_number": "12",
            "bidding_allowed": "on",
            "selling_allowed": "on",
        }
        data.update(overrides)
        return data

    def test_stranger_and_other_clubs_admin_are_refused(self):
        for user in (self.stranger, self.plain, self.other_admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self._url()).status_code, 403, user)
            self.assertEqual(self.client.post(self._url(), self._data()).status_code, 403, user)
        self.target.refresh_from_db()
        self.assertEqual(self.target.name, "Target Member")

    def test_anonymous_is_refused(self):
        self.assertIn(self.client.post(self._url(), self._data()).status_code, (401, 403))

    def test_edit_saves_the_member(self):
        self.client.force_login(self.permitted)
        response = self.client.post(self._url(), self._data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reload-page")
        self.target.refresh_from_db()
        self.assertEqual(self.target.name, "Renamed Member")
        self.assertEqual(self.target.email, "renamed@example.com")
        self.assertEqual(self.target.memo, "met at swap")
        self.assertTrue(self.target.admin_edited)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="Updated member").exists())

    def test_viewer_gets_a_read_only_form(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "hx-post=")

    def test_a_bidder_number_held_by_another_member_is_refused(self):
        self.client.force_login(self.permitted)
        response = self.client.post(self._url(), self._data(bidder_number="77"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already used")
        self.target.refresh_from_db()
        self.assertEqual(self.target.bidder_number, "12")
        self.assertEqual(self.target.name, "Target Member")

    def test_missing_member_is_not_found(self):
        self.client.force_login(self.permitted)
        self.assertEqual(self.client.get(reverse("clubmember_admin", kwargs={"pk": 999999})).status_code, 404)
