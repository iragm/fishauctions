"""Part SUPPORT — a way to reach a human that works with no account.

App Store Connect requires a Support URL, and App Review opens it in a plain browser with no
session. The only candidate was ``/faq/``, which ended with the site owner's address for signed-in
users and the words "(Sign in to see email)" for everybody else -- substantive help, and then
nothing at all exactly where the contact method belongs. That is the shape of a Guideline 1.5
metadata rejection, and a metadata rejection costs a review round trip.

Hiding the address from anonymous visitors is a real measure against scrapers, so these tests hold
both halves at once: the address stays hidden, and there is still a way to reach somebody.
"""

from django.conf import settings
from django.core import mail as django_mail
from django.test import TestCase, override_settings
from django.urls import reverse

from auctions.forms import ContactForm
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


class SupportUrlWorksSignedOutTests(TestCase):
    """The FAQ is the Support URL; a reviewer arrives with no session."""

    def test_the_faq_offers_a_way_to_get_in_touch_when_signed_out(self):
        html = self.client.get(reverse("faq")).content.decode()
        self.assertIn(reverse("contact"), html)

    def test_the_dead_text_is_gone(self):
        self.assertNotIn("(Sign in to see email)", self.client.get(reverse("faq")).content.decode())

    def test_the_address_is_still_hidden_from_anonymous_visitors(self):
        # The whole reason the FAQ hid it. A support page that leaks it has solved the wrong half.
        html = self.client.get(reverse("faq")).content.decode()
        self.assertNotIn(settings.ADMINS[0][1], html)

    def test_the_contact_page_itself_needs_no_account(self):
        response = self.client.get(reverse("contact"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(settings.ADMINS[0][1], response.content.decode())


@isolated_cache("contact-form")
class ContactFormDeliveryTests(TestCase):
    """What the form actually does with a message.

    The rate limit is keyed on the client IP, which is 127.0.0.1 in every worker -- so this class
    needs a cache of its own or ``--parallel`` runs count each other's messages.
    """

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.url = reverse("contact")
        self.body = {"name": "Ada", "email": "ada@example.com", "message": "How do I join an auction?"}

    def test_a_signed_out_visitor_can_send_a_message(self):
        response = self.client.post(self.url, self.body, follow=True)
        self.assertEqual(response.status_code, 200)
        from post_office.models import Email

        sent = Email.objects.filter(to=[settings.ADMINS[0][1]]).first()
        self.assertIsNotNone(sent, "the message never reached the site owner")
        self.assertIn("How do I join an auction?", sent.message)
        self.assertIn("Ada", sent.subject)

    def test_the_reply_goes_back_to_whoever_wrote_in(self):
        # Reply-To, not From: the From address is the site's own routed sender, and on SES it is
        # rewritten anyway -- a visitor's address there would fail SPF.
        self.client.post(self.url, self.body)
        from post_office.models import Email

        sent = Email.objects.filter(to=[settings.ADMINS[0][1]]).first()
        self.assertEqual(sent.headers.get("Reply-To"), "ada@example.com")

    def test_an_incomplete_message_is_not_sent(self):
        from post_office.models import Email

        response = self.client.post(self.url, {"name": "Ada", "email": "not an address", "message": ""})
        self.assertEqual(response.status_code, 200)  # redisplayed with errors, not delivered
        self.assertFalse(Email.objects.filter(to=[settings.ADMINS[0][1]]).exists())

    def test_nothing_is_sent_through_the_regular_mail_backend(self):
        # Everything on this site queues through post_office; a direct send would bypass the queue
        # and the sender routing with it.
        self.client.post(self.url, self.body)
        self.assertEqual(len(django_mail.outbox), 0)

    @override_settings(RECAPTCHA_ENABLED=False)
    def test_the_captcha_is_dropped_when_the_site_has_no_keys(self):
        # Same rule as the signup and password-reset forms, so local and CI runs don't have to
        # solve one. With keys configured the field is required and django_recaptcha verifies it.
        self.assertNotIn("captcha", ContactForm().fields)

    @override_settings(RECAPTCHA_ENABLED=True)
    def test_the_captcha_is_required_when_the_site_has_keys(self):
        self.assertIn("captcha", ContactForm().fields)

    def test_one_address_cannot_fill_the_inbox(self):
        """The floor under reCAPTCHA: a site with no keys has no captcha at all, and a solved one
        is not a promise about the next thousand messages."""
        from post_office.models import Email

        from auctions.views import ContactView

        for _ in range(ContactView.MESSAGES_PER_HOUR + 3):
            self.client.post(self.url, self.body)
        self.assertEqual(Email.objects.filter(to=[settings.ADMINS[0][1]]).count(), ContactView.MESSAGES_PER_HOUR)

    def test_being_over_the_limit_says_so(self):
        # Not a silent drop: somebody who has written five messages in an hour needs to know the
        # sixth is not on its way.
        from auctions.views import ContactView

        for _ in range(ContactView.MESSAGES_PER_HOUR):
            self.client.post(self.url, self.body)
        response = self.client.post(self.url, self.body, follow=True)
        self.assertContains(response, "give us a little while")


class ContactFormSignedInTests(StandardTestCase):
    def test_a_signed_in_person_does_not_retype_their_name_and_address(self):
        self.client.force_login(self.user)
        form = ContactForm(user=self.user)
        self.assertEqual(form.fields["email"].initial, self.user.email)

    def test_the_faq_still_shows_the_address_to_a_signed_in_user(self):
        # The form is the way in for people with no account; it does not replace the address for
        # people who are already here.
        self.client.force_login(self.user)
        self.assertIn(settings.ADMINS[0][1], self.client.get(reverse("faq")).content.decode())
