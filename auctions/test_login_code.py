"""Sign in with an emailed code (auctions/login_code.py): existing accounts, new ones, club-added people,
and the holes it closes."""

import re

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from auctions.models import AuctionTOS, Invoice, UserData
from auctions.test_signup_links import _OpenAuctionMixin
from auctions.tests import StandardTestCase

REQUEST = "account_request_login_code"
CONFIRM = "account_confirm_login_code"


class LoginCodeTests(StandardTestCase):
    def request_code(self, email, **extra):
        mail.outbox.clear()
        return self.client.post(reverse(REQUEST), {"email": email, **extra})

    def code_from_mail(self):
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        body = message.body
        match = re.search(r"^\s*([A-Z0-9-]{6,})\s*$", body, re.MULTILINE)
        self.assertIsNotNone(match, body)
        return match.group(1)

    def confirm(self, code, **extra):
        return self.client.post(reverse(CONFIRM), {"code": code, **extra})

    def signed_in_user(self):
        pk = self.client.session.get("_auth_user_id")
        return User.objects.filter(pk=pk).first() if pk else None

    def make_user(self, email, *, verified=True, password="secret-pass-123", primary_email=None):
        user = User.objects.create_user(f"u{User.objects.count()}", primary_email or email, password)
        EmailAddress.objects.create(user=user, email=email, verified=verified, primary=primary_email is None)
        return user

    # Existing accounts

    def test_existing_account_signs_in_and_keeps_its_password(self):
        user = self.make_user("has-account@example.com")
        self.request_code("has-account@example.com")
        response = self.confirm(self.code_from_mail())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.signed_in_user(), user)
        user.refresh_from_db()
        self.assertTrue(user.check_password("secret-pass-123"))

    def test_next_survives_both_steps(self):
        self.make_user("goes-places@example.com")
        mail.outbox.clear()
        response = self.client.post(
            reverse(REQUEST) + "?next=/lots/", {"email": "goes-places@example.com", "next": "/lots/"}
        )
        self.assertIn("next=", response["Location"])
        response = self.client.post(response["Location"], {"code": self.code_from_mail(), "next": "/lots/"})
        self.assertEqual(response["Location"], "/lots/")

    def test_three_wrong_codes_sign_in_nobody(self):
        self.make_user("guessed@example.com")
        self.request_code("guessed@example.com")
        for _ in range(3):
            self.confirm("WRONG1")
        self.assertIsNone(self.signed_in_user())
        # The real code is dead too: the attempt cap ended the process.
        self.confirm(self.code_from_mail())
        self.assertIsNone(self.signed_in_user())

    def test_a_squatters_password_stops_working(self):
        squatter = self.make_user("victim@example.com", verified=False, password="squatter-pass-1")
        self.request_code("victim@example.com")
        self.confirm(self.code_from_mail())
        self.assertEqual(self.signed_in_user(), squatter)
        squatter.refresh_from_db()
        self.assertFalse(squatter.has_usable_password())
        self.assertTrue(EmailAddress.objects.get(user=squatter, email="victim@example.com").verified)

    def test_a_squatters_social_login_goes_too(self):
        from allauth.socialaccount.models import SocialAccount

        squatter = self.make_user("victim2@example.com", verified=False)
        SocialAccount.objects.create(user=squatter, provider="facebook", uid="123")
        self.request_code("victim2@example.com")
        self.confirm(self.code_from_mail())
        self.assertEqual(self.signed_in_user(), squatter)
        self.assertFalse(SocialAccount.objects.filter(user=squatter).exists())

    def test_an_account_that_has_signed_in_keeps_its_password(self):
        # An old admin account with no address row, used at /admin/.
        user = User.objects.create_user("old_admin", "old-admin@example.com", "admin-pass-123")
        User.objects.filter(pk=user.pk).update(last_login=timezone.now())
        self.request_code("old-admin@example.com")
        self.confirm(self.code_from_mail())
        self.assertEqual(self.signed_in_user(), user)
        user.refresh_from_db()
        self.assertTrue(user.check_password("admin-pass-123"))

    def test_an_address_changed_while_the_code_was_out_signs_in_nobody(self):
        user = self.make_user("moving@example.com")
        self.request_code("moving@example.com")
        User.objects.filter(pk=user.pk).update(email="moved@example.com")
        EmailAddress.objects.filter(user=user).update(email="moved@example.com")
        self.confirm(self.code_from_mail())
        self.assertIsNone(self.signed_in_user())
        self.assertEqual(
            list(EmailAddress.objects.filter(user=user).values_list("email", flat=True)), ["moved@example.com"]
        )

    def test_an_account_made_while_the_code_was_out_is_used_not_duplicated(self):
        self.request_code("racing@example.com")
        existing = self.make_user("racing@example.com")
        self.confirm(self.code_from_mail())
        self.assertEqual(self.signed_in_user(), existing)
        self.assertEqual(User.objects.filter(email="racing@example.com").count(), 1)

    def test_inactive_account_gets_nothing(self):
        user = self.make_user("gone@example.com")
        User.objects.filter(pk=user.pk).update(is_active=False)
        count = User.objects.count()
        self.request_code("gone@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(User.objects.count(), count)

    # Addresses with no account

    def test_asking_creates_nothing_and_the_code_creates_the_account(self):
        count = User.objects.count()
        response = self.request_code("first.last.secret@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.count(), count)
        self.confirm(self.code_from_mail())
        user = self.signed_in_user()
        self.assertIsNotNone(user)
        self.assertEqual(User.objects.count(), count + 1)
        self.assertEqual(user.email, "first.last.secret@example.com")
        self.assertNotIn("secret", user.username)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(EmailAddress.objects.get(user=user).verified)
        userdata = UserData.objects.get(user=user)
        # The same email settings as any other new account.
        self.assertEqual(
            userdata.email_me_about_new_auctions, UserData._meta.get_field("email_me_about_new_auctions").default
        )
        self.assertTrue(userdata.email_me_about_new_local_lots)
        self.assertTrue(userdata.email_me_when_people_comment_on_my_lots)

    def test_known_and_unknown_addresses_look_the_same(self):
        self.make_user("known@example.com")
        known = self.request_code("known@example.com")
        known_subject = mail.outbox[0].subject
        self.client.logout()
        unknown = self.request_code("unknown@example.com")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known["Location"], unknown["Location"])
        self.assertEqual(mail.outbox[0].subject, known_subject)

    def test_club_added_person_gets_their_rows_and_name(self):
        tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Pat Example",
            email="club-added@example.com",
            manually_added=True,
            bidder_number="778",
        )
        self.request_code("club-added@example.com")
        self.confirm(self.code_from_mail())
        user = self.signed_in_user()
        tos.refresh_from_db()
        self.assertEqual(tos.user, user)
        self.assertEqual((user.first_name, user.last_name), ("Pat", "Example"))
        self.assertTrue(user.username.startswith("member"))

    def test_an_unverified_extra_address_belongs_to_nobody(self):
        other = self.make_user("mine@example.com")
        EmailAddress.objects.create(user=other, email="not-theirs@example.com", verified=False, primary=False)
        self.request_code("not-theirs@example.com")
        self.confirm(self.code_from_mail())
        user = self.signed_in_user()
        self.assertNotEqual(user, other)
        self.assertEqual(user.email, "not-theirs@example.com")
        self.assertFalse(EmailAddress.objects.filter(user=other, email="not-theirs@example.com").exists())

    def test_a_bounced_address_still_gets_its_code_and_the_bounce_clears(self):
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Bounced",
            email="bounces@example.com",
            email_address_status="BAD",
            manually_added=True,
            bidder_number="779",
        )
        self.request_code("bounces@example.com")
        self.confirm(self.code_from_mail())
        self.assertIsNotNone(self.signed_in_user())
        self.assertFalse(AuctionTOS.objects.filter(email="bounces@example.com", email_address_status="BAD").exists())

    # Entry points

    def test_login_page_offers_a_code(self):
        self.assertContains(self.client.get(reverse("account_login")), 'id="sign-in-code"')

    def test_sign_up_is_the_same_page_and_the_password_form_is_kept(self):
        page = self.client.get(reverse("account_signup"), {"next": "/lots/"})
        self.assertContains(page, 'id="sign-in-code"')
        self.assertContains(page, 'id="password-sign-up"')
        self.assertNotContains(page, 'id="signup_form"')
        legacy = self.client.get(reverse("account_signup"), {"password": "1"})
        self.assertContains(legacy, 'id="signup_form"')

    def test_password_sign_in_still_works_without_the_inbox(self):
        user = self.make_user("lost-inbox@example.com")
        page = self.client.get(reverse("account_login"))
        self.assertContains(page, 'id="password-sign-in"')
        self.client.post(reverse("account_login"), {"login": user.username, "password": "secret-pass-123"})
        self.assertEqual(self.signed_in_user(), user)

    def test_the_email_box_posts_its_next_along(self):
        self.make_user("from-login@example.com")
        mail.outbox.clear()
        response = self.client.post(reverse(REQUEST), {"email": "from-login@example.com", "next": "/lots/"})
        response = self.client.post(response["Location"], {"code": self.code_from_mail(), "next": "/lots/"})
        self.assertEqual(response["Location"], "/lots/")

    def test_invoice_button_prefills_the_address(self):
        tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Invoice viewer",
            email="invoice-viewer@example.com",
            manually_added=True,
            bidder_number="780",
        )
        invoice = Invoice.for_participant(tos, self.online_auction)
        page = self.client.get(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))
        self.assertContains(page, 'id="invoice-claim-code"')
        # Viewing the invoice made no account.
        self.assertFalse(User.objects.filter(email="invoice-viewer@example.com").exists())
        form = self.client.get(reverse(REQUEST), {"invoice": str(invoice.no_login_link)})
        self.assertContains(form, 'value="invoice-viewer@example.com"')


class AppLoginCodeTests(StandardTestCase):
    """The app's two endpoints: no session, a challenge instead."""

    code_from_mail = LoginCodeTests.code_from_mail
    make_user = LoginCodeTests.make_user

    def app_request(self, email):
        mail.outbox.clear()
        return self.client.post(reverse("mobile-auth-code-request"), {"email": email}, content_type="application/json")

    def app_confirm(self, challenge, code):
        return self.client.post(
            reverse("mobile-auth-code-confirm"), {"challenge": challenge, "code": code}, content_type="application/json"
        )

    def test_a_new_address_gets_tokens_and_an_account(self):
        response = self.app_request("app-new@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="app-new@example.com").exists())
        response = self.app_confirm(response.json()["challenge"], self.code_from_mail())
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())
        user = User.objects.get(email="app-new@example.com")
        self.assertTrue(EmailAddress.objects.get(user=user).verified)

    def test_an_existing_account_gets_its_own_tokens(self):
        user = self.make_user("app-old@example.com")
        challenge = self.app_request("app-old@example.com").json()["challenge"]
        self.assertEqual(self.app_confirm(challenge, self.code_from_mail()).status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("secret-pass-123"))

    def test_three_wrong_codes_end_the_challenge(self):
        challenge = self.app_request("app-guess@example.com").json()["challenge"]
        code = self.code_from_mail()
        for _ in range(3):
            self.assertEqual(self.app_confirm(challenge, "WRONG1").status_code, 401)
        self.assertEqual(self.app_confirm(challenge, code).status_code, 401)
        self.assertFalse(User.objects.filter(email="app-guess@example.com").exists())

    def test_a_bad_address_is_refused(self):
        self.assertEqual(self.app_request("not-an-address").status_code, 400)

    def test_config_offers_it(self):
        self.assertTrue(self.client.get(reverse("mobile-config")).json().get("email_code_sign_in_enabled"))


class NameAndReminderTests(_OpenAuctionMixin, StandardTestCase):
    def nameless(self, email="nameless@example.com"):
        user = User.objects.create_user(f"member{User.objects.count()}", email, "pw-123456789")
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
        return user

    def test_sign_in_takes_the_name_a_club_typed(self):
        from auctions.signals import on_sign_in

        user = self.nameless("named-by-club@example.com")
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Robin Example",
            email="named-by-club@example.com",
            manually_added=True,
            bidder_number="781",
        )
        on_sign_in(user)
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Robin", "Example"))

    def test_joining_asks_a_nameless_person_for_their_name_on_the_join_form(self):
        from auctions.services import join_auction

        auction = self.open_auction()
        location = auction.location_qs.first()
        user = self.nameless()
        self.assertEqual(join_auction(user, auction, location)[2], "name")
        self.client.force_login(user)
        self.assertContains(self.client.get(auction.get_absolute_url()), 'name="first_name"')
        self.client.post(
            auction.get_absolute_url(),
            {
                "pickup_location": location.pk,
                "time_spent_reading_rules": 10,
                "first_name": "Sam",
                "last_name": "Example",
            },
        )
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Sam")
        self.assertTrue(AuctionTOS.objects.filter(user=user, auction=auction).exists())

    def test_reminder_on_dashboards_only_and_not_after_closing(self):
        user = self.nameless()
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("selling")), 'id="profile-reminder"')
        self.assertNotContains(self.client.get(reverse("allLots")), 'id="profile-reminder"')
        self.client.cookies["hide_profile_reminder"] = "true"
        self.assertNotContains(self.client.get(reverse("selling")), 'id="profile-reminder"')
