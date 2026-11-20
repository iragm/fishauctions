"""Sign in with an emailed code (auctions/login_code.py): existing accounts, new ones, club-added people,
and the holes it closes."""

import re

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from auctions.models import AuctionTOS, Invoice, UserData
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
        self.assertFalse(userdata.email_me_about_new_auctions)
        self.assertFalse(userdata.email_me_about_new_in_person_auctions)
        self.assertFalse(userdata.email_me_about_new_local_lots)
        self.assertFalse(userdata.email_me_about_new_lots_ship_to_location)
        self.assertFalse(userdata.email_me_about_new_features)
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

    def test_the_box_turns_on_nearby_auction_mail(self):
        self.request_code("wants-news@example.com")
        self.confirm(self.code_from_mail(), email_me_about_auctions="on")
        userdata = UserData.objects.get(user=self.signed_in_user())
        self.assertTrue(userdata.email_me_about_new_auctions)
        self.assertTrue(userdata.email_me_about_new_in_person_auctions)
        self.assertFalse(userdata.email_me_about_new_local_lots)

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

    def test_no_mail_to_an_address_that_bounces(self):
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Bounced",
            email="bounces@example.com",
            email_address_status="BAD",
            manually_added=True,
            bidder_number="779",
        )
        response = self.request_code("bounces@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    # Entry points

    def test_login_page_offers_a_code(self):
        self.assertContains(self.client.get(reverse("account_login")), 'id="sign-in-code"')

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
