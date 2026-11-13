"""Likely duplicate accounts and the one-time email telling them merging exists (``auctions/duplicate_accounts.py``)."""

import datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.utils import timezone
from post_office.models import EmailTemplate

from auctions import duplicate_accounts
from auctions.models import Club, ClubMember, PageView, UserData

SEND = "auctions.duplicate_accounts.mail.send"


class DuplicateAccountTests(TestCase):
    def make_user(self, username, first="Pat", last="Fishkeeper", ip=None):
        user = User.objects.create_user(username, f"{username}@example.com", "pw", first_name=first, last_name=last)
        UserData.objects.get_or_create(user=user)
        if ip:
            UserData.objects.filter(user=user).update(last_ip_address=ip)
        return user

    def view(self, user, ip):
        PageView.objects.create(user=user, url="/", title="t", ip_address=ip)

    def pairs(self):
        return duplicate_accounts.likely_duplicate_pairs()

    def test_same_name_and_same_ip_is_a_pair(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        b = self.make_user("pat2", ip="10.1.1.1")
        self.assertEqual(self.pairs(), [(a.pk, b.pk)])

    def test_same_name_page_view_ip_is_a_pair(self):
        a = self.make_user("pat1")
        b = self.make_user("pat2")
        self.view(a, "10.2.2.2")
        self.view(b, "10.2.2.2")
        self.assertEqual(self.pairs(), [(a.pk, b.pk)])

    def test_same_name_alone_is_not(self):
        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.9.9.9")
        self.assertEqual(self.pairs(), [])

    def test_same_ip_alone_is_not(self):
        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("sam1", first="Sam", last="Other", ip="10.1.1.1")
        self.assertEqual(self.pairs(), [])

    def test_a_busy_address_is_no_evidence(self):
        a = self.make_user("pat1")
        b = self.make_user("pat2")
        for user in [a, b] + [self.make_user(f"other{i}", first="O", last=str(i)) for i in range(4)]:
            self.view(user, "10.3.3.3")
        self.assertEqual(self.pairs(), [])

    def test_staff_and_inactive_are_left_out(self):
        self.make_user("pat1", ip="10.1.1.1")
        staff = self.make_user("pat2", ip="10.1.1.1")
        staff.is_staff = True
        staff.save()
        self.assertEqual(self.pairs(), [])
        staff.is_staff = False
        staff.is_active = False
        staff.save()
        self.assertEqual(self.pairs(), [])

    def test_flagged_club_members_with_different_names_on_the_accounts(self):
        a = self.make_user("pat1", first="", last="", ip="10.1.1.1")
        b = self.make_user("pat2", first="", last="", ip="10.1.1.1")
        club = Club.objects.create(name="Duplicate Test Club")
        ClubMember.objects.create(club=club, name="Robert Fishkeeper", user=a)
        ClubMember.objects.create(club=club, name="Bob Fishkeeper", user=b)
        self.assertEqual(self.pairs(), [(a.pk, b.pk)])

    def test_one_word_names_are_not_enough(self):
        a = self.make_user("pat1", first="", last="", ip="10.1.1.1")
        b = self.make_user("pat2", first="", last="", ip="10.1.1.1")
        club = Club.objects.create(name="Duplicate Test Club")
        ClubMember.objects.create(club=club, name="Pat", user=a)
        ClubMember.objects.create(club=club, name="Pat", user=b)
        self.assertEqual(self.pairs(), [])

    def test_each_account_is_emailed_once_and_the_email_names_nobody_else(self):
        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        with patch(SEND) as send:
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 2)
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 0)
        self.assertEqual(sorted(c.args[0] for c in send.call_args_list), ["pat1@example.com", "pat2@example.com"])
        for c in send.call_args_list:
            context = c.kwargs["context"]
            self.assertEqual(c.kwargs["template"], "account_merge_suggestion")
            self.assertEqual(set(context), {"user", "domain", "unsubscribe"})
            self.assertEqual(context["user"].email, c.args[0])
        self.assertEqual(UserData.objects.filter(merge_suggested_on__isnull=False).count(), 2)

    def test_unsubscribed_accounts_are_not_emailed(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        UserData.objects.filter(user=a).update(has_unsubscribed=True)
        with patch(SEND) as send:
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 1)
        self.assertEqual(send.call_args.args[0], "pat2@example.com")

    def test_a_failed_send_is_tried_again_next_run(self):
        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        with patch(SEND, side_effect=RuntimeError):
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 0)
        self.assertFalse(UserData.objects.filter(merge_suggested_on__isnull=False).exists())
        with patch(SEND):
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 2)

    def test_one_inbox_gets_one_email(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        b = self.make_user("pat2", ip="10.1.1.1")
        User.objects.filter(pk=b.pk).update(email=a.email.upper())
        with patch(SEND) as send:
            self.assertEqual(duplicate_accounts.send_merge_suggestions(), 1)
        send.assert_called_once()

    def test_an_inbox_told_last_week_is_not_told_again(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        b = self.make_user("pat2", ip="10.1.1.1")
        User.objects.filter(pk=b.pk).update(email=a.email.upper())
        with patch(SEND) as send:
            duplicate_accounts.send_merge_suggestions()
            duplicate_accounts.send_merge_suggestions()
        send.assert_called_once()
        self.assertEqual(duplicate_accounts.likely_duplicate_pairs(), [])

    def test_pairs_nobody_can_be_told_about_are_not_checked(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        b = self.make_user("pat2", ip="10.1.1.1")
        UserData.objects.filter(user=a).update(merge_suggested_on=timezone.now())
        UserData.objects.filter(user=b).update(has_unsubscribed=True)
        with patch.object(duplicate_accounts._Connections, "shared") as shared:
            self.assertEqual(duplicate_accounts.likely_duplicate_pairs(), [])
        shared.assert_not_called()

    def test_a_time_limit_during_a_send_keeps_the_claim(self):
        from celery.exceptions import SoftTimeLimitExceeded

        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        with patch(SEND, side_effect=SoftTimeLimitExceeded), self.assertRaises(SoftTimeLimitExceeded):
            duplicate_accounts.send_merge_suggestions()
        self.assertEqual(UserData.objects.filter(merge_suggested_on__isnull=False).count(), 1)

    def test_an_old_last_ip_is_no_evidence(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        UserData.objects.filter(user=a).update(last_activity=timezone.now() - datetime.timedelta(days=800))
        self.assertEqual(self.pairs(), [])

    def test_accounts_already_merging_are_left_out(self):
        a = self.make_user("pat1", ip="10.1.1.1")
        b = self.make_user("pat2", ip="10.1.1.1")
        UserData.objects.filter(user=a).update(merge_into_user=b, merge_requested_on=timezone.now())
        self.assertEqual(self.pairs(), [])

    def test_a_spent_budget_checks_nothing_more(self):
        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        with patch("auctions.duplicate_accounts._clock", return_value=1000):
            self.assertEqual(duplicate_accounts.likely_duplicate_pairs(deadline=10), [])
        self.assertEqual(len(duplicate_accounts.likely_duplicate_pairs(deadline=None)), 1)

    def test_the_weekly_task_sends(self):
        from auctions.tasks import suggest_account_merges

        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        with patch(SEND) as send:
            suggest_account_merges.apply()
            suggest_account_merges.apply()
        self.assertEqual(send.call_count, 2)

    def test_the_command_only_counts_without_send(self):
        self.make_user("pat1", ip="10.1.1.1")
        self.make_user("pat2", ip="10.1.1.1")
        out = StringIO()
        with patch(SEND) as send:
            call_command("suggest_account_merges", stdout=out)
        send.assert_not_called()
        self.assertIn("2 accounts would be emailed", out.getvalue())
        self.assertNotIn("10.1.1.1", out.getvalue())
        self.assertNotIn("pat1", out.getvalue())

    def test_the_template_renders(self):
        user = self.make_user("pat1")
        template = EmailTemplate.objects.get(name="account_merge_suggestion")
        context = Context({"user": user, "domain": "example.com", "unsubscribe": "x"})
        html = Template(template.html_content).render(context)
        text = Template(template.content).render(context)
        self.assertIn("https://example.com/account/merge/", html)
        self.assertIn("Hey Pat,", text)
