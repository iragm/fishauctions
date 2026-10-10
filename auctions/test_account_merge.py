"""Merging two accounts: the two-sided request, what moves, and what a stranger can't do with it."""

import datetime
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions import account_merge
from auctions.account_merge import MergeRefused, accept_merge, merge_accounts, request_merge
from auctions.friction_models import AbandonedBid
from auctions.models import (
    Club,
    ClubMember,
    Lot,
    UserAPIKey,
    UserBan,
    UserData,
    UserLabelPrefs,
)
from auctions.moderation_models import CopyrightStrike


class AccountMergeCoverageTests(TestCase):
    """Every relation to User is in exactly one table, so a new one is a decision, not an orphan."""

    def test_every_relation_to_user_is_accounted_for_once(self):
        user_model = get_user_model()
        relations = {
            f"{model._meta.label}.{field.name}"
            for model in apps.get_models()
            for field in model._meta.get_fields()
            if field.is_relation
            and not field.auto_created
            and field.concrete
            and field.related_model is user_model
            and field.model is model
        }
        tables = [
            account_merge.MOVED_BY_MERGE_INTO,
            account_merge.REPOINTED,
            set(account_merge.REPOINTED_UNLESS_DUPLICATE),
            account_merge.DROPPED,
            account_merge.LEFT_BEHIND,
        ]
        listed = [label for table in tables for label in table]
        self.assertEqual(sorted(relations - set(listed)), [], "Decide what a merge does with these.")
        self.assertEqual(sorted(set(listed) - relations), [], "These aren't relations to User any more.")
        self.assertEqual(len(listed), len(set(listed)), "A relation is in two tables.")


class MergeTestCase(TestCase):
    def setUp(self):
        self.source = User.objects.create_user(username="oldme", password="pw", email="old@example.com")
        self.target = User.objects.create_user(username="newme", password="pw", email="new@example.com")
        self.stranger = User.objects.create_user(username="stranger", password="pw", email="s@example.com")
        EmailAddress.objects.create(user=self.source, email="old@example.com", verified=True, primary=True)

    def sign_in(self, user):
        self.client.force_login(user)

    def ask(self, username="newme", user=None):
        self.sign_in(user or self.source)
        return self.client.post(reverse("account_merge"), {"action": "request", "username": username})


class MergeRequestTests(MergeTestCase):
    def test_page_is_in_the_account_menu(self):
        self.sign_in(self.source)
        response = self.client.get(reverse("account_merge"))
        self.assertContains(response, "keeps its username")
        self.assertContains(response, 'href="/account/merge/"')

    def test_requires_sign_in(self):
        response = self.client.get(reverse("account_merge"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_your_own_username_is_refused(self):
        self.ask("OldMe")
        self.assertIsNone(UserData.objects.get(user=self.source).merge_into_user)

    def test_an_unknown_or_closed_account_is_refused(self):
        self.stranger.is_active = False
        self.stranger.save()
        for name in ("nobody", "stranger", ""):
            with self.assertRaises(MergeRefused):
                request_merge(self.source, name)

    def test_staff_accounts_are_refused_on_either_side(self):
        self.stranger.is_staff = True
        self.stranger.save()
        with self.assertRaises(MergeRefused):
            request_merge(self.source, "stranger")
        with self.assertRaises(MergeRefused):
            request_merge(self.stranger, "newme")

    def test_a_request_is_recorded_and_nobody_is_emailed(self):
        with patch("post_office.mail.send") as send:
            self.ask("NEWME")
        userdata = UserData.objects.get(user=self.source)
        self.assertEqual(userdata.merge_into_user, self.target)
        send.assert_not_called()
        response = self.client.get(reverse("account_merge"))
        self.assertContains(response, "Now sign in as <strong>newme</strong>")

    def test_cancel(self):
        self.ask()
        self.client.post(reverse("account_merge"), {"action": "cancel"})
        self.assertIsNone(UserData.objects.get(user=self.source).merge_into_user)

    def test_only_the_named_account_sees_it(self):
        self.ask()
        self.sign_in(self.target)
        self.assertContains(self.client.get(reverse("account_merge")), "asked to be merged into this account")
        self.sign_in(self.stranger)
        self.assertNotContains(self.client.get(reverse("account_merge")), "oldme")
        self.assertNotContains(self.client.get(reverse("account_merge") + "?from=oldme"), "Merge oldme into")

    def test_a_request_expires(self):
        self.ask()
        UserData.objects.filter(user=self.source).update(
            merge_requested_on=timezone.now() - datetime.timedelta(hours=account_merge.REQUEST_HOURS, minutes=1)
        )
        self.sign_in(self.target)
        self.assertNotContains(self.client.get(reverse("account_merge")), "oldme")
        with self.assertRaises(MergeRefused):
            accept_merge(self.target, self.source.pk)

    def test_decline(self):
        self.ask()
        self.sign_in(self.target)
        self.client.post(reverse("account_merge"), {"action": "decline", "source": self.source.pk})
        self.assertIsNone(UserData.objects.get(user=self.source).merge_into_user)


class MergeConfirmationTests(MergeTestCase):
    def test_the_kept_account_sees_what_it_gets(self):
        club = Club.objects.create(name="Killi Club")
        ClubMember.objects.create(club=club, user=self.source, name="Old Me")
        Lot.objects.create(lot_name="Guppies", quantity=1, user=self.source)
        SocialAccount.objects.create(user=self.source, provider="google", uid="g-123")
        self.ask()
        self.sign_in(self.target)
        response = self.client.get(reverse("account_merge") + "?from=oldme")
        self.assertContains(response, "Merge oldme into newme?")
        self.assertContains(response, "1 lot sold")
        self.assertContains(response, "1 club: Killi Club")
        self.assertContains(response, "Sign in with Google")
        self.assertContains(response, 'name="action" value="accept"')

    def test_a_stranger_cannot_accept(self):
        self.ask()
        self.sign_in(self.stranger)
        self.client.post(reverse("account_merge"), {"action": "accept", "source": self.source.pk})
        self.source.refresh_from_db()
        self.assertTrue(self.source.is_active)


class MergeTests(MergeTestCase):
    def accept(self):
        self.ask()
        self.sign_in(self.target)
        with patch("post_office.mail.send") as send, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("account_merge"), {"action": "accept", "source": self.source.pk})
        self.source.refresh_from_db()
        return send

    def test_data_moves_and_the_source_is_closed(self):
        lot = Lot.objects.create(lot_name="Guppies", quantity=1, user=self.source, added_by=self.source)
        self.accept()
        lot.refresh_from_db()
        self.assertEqual((lot.user, lot.added_by), (self.target, self.target))
        self.assertFalse(self.source.is_active)
        self.assertEqual(self.source.username, f"merged-user-{self.source.pk}")
        self.assertEqual(self.source.email, "")
        self.assertFalse(self.source.has_usable_password())
        self.target.refresh_from_db()
        self.assertEqual((self.target.username, self.target.email), ("newme", "new@example.com"))

    def test_social_sign_in_moves_and_email_does_not(self):
        SocialAccount.objects.create(user=self.source, provider="google", uid="g-123")
        self.accept()
        self.assertEqual(SocialAccount.objects.get(uid="g-123").user, self.target)
        self.assertFalse(EmailAddress.objects.filter(email="old@example.com").exists())
        self.assertFalse(EmailAddress.objects.filter(user=self.target, email="old@example.com").exists())

    def test_only_the_closed_account_is_emailed(self):
        send = self.accept()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "old@example.com")

    def test_ai_agents_move(self):
        UserAPIKey.objects.create(user=self.source, name="laptop", prefix="ak_test", key_hash="x")
        self.ask()
        self.sign_in(self.target)
        self.assertContains(self.client.get(reverse("account_merge") + "?from=oldme"), "1 AI agent or key")
        self.accept()
        self.assertEqual(UserAPIKey.objects.get(prefix="ak_test").user, self.target)

    def test_the_closed_account_is_signed_out(self):
        self.sign_in(self.source)
        session_cookie = self.client.cookies["sessionid"].value
        merge_accounts(self.source, self.target)
        self.client.cookies["sessionid"] = session_cookie
        response = self.client.get(reverse("account_merge"))
        self.assertEqual(response.status_code, 302)

    def test_bans_and_strikes_follow_and_self_bans_go(self):
        UserBan.objects.create(user=self.stranger, banned_user=self.source)
        UserBan.objects.create(user=self.stranger, banned_user=self.target)
        UserBan.objects.create(user=self.source, banned_user=self.target)
        CopyrightStrike.objects.create(user=self.source)
        merge_accounts(self.source, self.target)
        self.assertEqual(UserBan.objects.filter(user=self.stranger, banned_user=self.target).count(), 1)
        self.assertFalse(UserBan.objects.filter(user=self.target, banned_user=self.target).exists())
        self.assertEqual(CopyrightStrike.objects.filter(user=self.target).count(), 1)

    def test_one_per_person_rows_keep_the_kept_accounts(self):
        lot = Lot.objects.create(lot_name="Guppies", quantity=1, user=self.stranger)
        AbandonedBid.objects.create(lot=lot, user=self.source, stage="typed")
        AbandonedBid.objects.create(lot=lot, user=self.target, stage="typed")
        UserLabelPrefs.objects.create(user=self.source, font_size=12)
        UserLabelPrefs.objects.create(user=self.target, font_size=6)
        merge_accounts(self.source, self.target)
        self.assertEqual(AbandonedBid.objects.filter(user=self.target).count(), 1)
        self.assertEqual(UserLabelPrefs.objects.get(user=self.target).font_size, 6)

    def test_requests_naming_the_closed_account_are_cleared(self):
        request_merge(self.target, "oldme")
        request_merge(self.source, "newme")
        accept_merge(self.target, self.source.pk)
        self.assertIsNone(UserData.objects.get(user=self.target).merge_into_user)

    def test_it_runs_once(self):
        self.ask()
        accept_merge(self.target, self.source.pk)
        with self.assertRaises(MergeRefused):
            accept_merge(self.target, self.source.pk)
