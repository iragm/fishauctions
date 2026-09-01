"""Part SUPPORT — /support/, and a way to reach a human that works with no account.

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
        self.assertIn(reverse("support"), html)

    def test_the_dead_text_is_gone(self):
        self.assertNotIn("(Sign in to see email)", self.client.get(reverse("faq")).content.decode())

    def test_the_address_is_still_hidden_from_anonymous_visitors(self):
        # The whole reason the FAQ hid it. A support page that leaks it has solved the wrong half.
        html = self.client.get(reverse("faq")).content.decode()
        self.assertNotIn(settings.ADMINS[0][1], html)

    def test_the_support_page_itself_needs_no_account(self):
        response = self.client.get(reverse("support"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(settings.ADMINS[0][1], response.content.decode())


class SupportPageIsTheHelpPageTests(TestCase):
    """/support/ is where somebody goes when they are stuck, and the message form is the last
    resort on it rather than the whole of it: an agent answers a question about their own auction
    in seconds, the FAQ answers the common ones, and the videos cover running an auction end to
    end. All four are on the one page, so a reader with no session gets all of them."""

    def setUp(self):
        self.html = self.client.get(reverse("support")).content.decode()

    def test_it_leads_with_connecting_an_agent(self):
        self.assertIn(reverse("user_api_keys"), self.html)
        self.assertIn("connect an AI agent", self.html)

    def test_it_links_the_faq(self):
        self.assertIn(reverse("faq"), self.html)

    def test_both_tutorial_videos_are_on_it(self):
        self.assertIn(settings.ONLINE_TUTORIAL_YOUTUBE_ID, self.html)
        self.assertIn(settings.IN_PERSON_TUTORIAL_YOUTUBE_ID, self.html)

    def test_the_videos_start_collapsed(self):
        # Half an hour of video and two long chapter lists, above the form somebody came here to
        # use. The button says what is behind it; there is no bare hamburger on this site.
        self.assertIn('id="tutorial-videos"', self.html)
        self.assertIn("Watch the tutorial videos", self.html)
        self.assertNotIn('class="collapse show" id="tutorial-videos"', self.html)

    def test_the_chapter_list_comes_with_them(self):
        self.assertIn("Jump to content in this video", self.html)
        self.assertIn(settings.ONLINE_TUTORIAL_CHAPTERS[1][1], self.html)

    def test_the_message_form_is_still_there(self):
        self.assertIn("Reach out, always happy to chat", self.html)
        self.assertIn('name="message"', self.html)


class OldContactUrlStillWorksTests(TestCase):
    """The App Store metadata, every email that has ever quoted the address, and the app itself
    point at /contact/. Moving the page without leaving the old address working is how a Support
    URL turns into a 404 between one release and the next."""

    def test_contact_redirects_to_support(self):
        response = self.client.get("/contact/")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("support"))

    def test_it_lands_on_the_real_page(self):
        response = self.client.get("/contact/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Need help?", response.content.decode())


class VideoEmbedFitsItsContainerTests(TestCase):
    """The player used to be built at a fixed 583px below 1024px wide, which is wider than every
    phone -- so every page carrying a tutorial scrolled sideways, and an iframe is out of flow's
    reach, so nothing else on the page could shrink to compensate. Sized in CSS now."""

    def test_the_embed_asks_for_no_pixel_size(self):
        from django.template.loader import render_to_string

        html = render_to_string("youtube_embed.html", {"videoId": "abc123", "chapters": []})
        self.assertNotIn("583", html)
        self.assertNotIn("875", html)
        self.assertIn("video-container", html)

    def test_the_stylesheet_caps_it_at_the_page_width(self):
        from pathlib import Path

        from django.conf import settings as django_settings

        css = Path(django_settings.BASE_DIR, "auctions/static/css/auction_site.css").read_text()
        block = css.split(".video-container {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 100%", block)
        self.assertIn("aspect-ratio", block)


@isolated_cache("contact-form")
class SupportFormDeliveryTests(TestCase):
    """What the form actually does with a message.

    The rate limit is keyed on the client IP, which is 127.0.0.1 in every worker -- so this class
    needs a cache of its own or ``--parallel`` runs count each other's messages.
    """

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.url = reverse("support")
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

        from auctions.views import SupportView

        for _ in range(SupportView.MESSAGES_PER_HOUR + 3):
            self.client.post(self.url, self.body)
        self.assertEqual(Email.objects.filter(to=[settings.ADMINS[0][1]]).count(), SupportView.MESSAGES_PER_HOUR)

    def test_being_over_the_limit_says_so(self):
        # Not a silent drop: somebody who has written five messages in an hour needs to know the
        # sixth is not on its way.
        from auctions.views import SupportView

        for _ in range(SupportView.MESSAGES_PER_HOUR):
            self.client.post(self.url, self.body)
        response = self.client.post(self.url, self.body, follow=True)
        self.assertContains(response, "give us a little while")


class SupportFormSignedInTests(StandardTestCase):
    """Signed in, the form is one box. The site knows who they are and where to write back, so
    asking again is a field to read past and an answer we would not trust anyway."""

    def test_a_signed_in_person_is_not_asked_for_their_name_and_address(self):
        form = ContactForm(user=self.user)
        self.assertNotIn("name", form.fields)
        self.assertNotIn("email", form.fields)
        self.assertIn("message", form.fields)

    def test_the_page_only_shows_them_the_message_box(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("support")).content.decode()
        self.assertIn('name="message"', html)
        self.assertNotIn('name="email"', html)

    def test_the_reply_goes_to_the_account_not_to_whatever_was_posted(self):
        from post_office.models import Email

        self.client.force_login(self.user)
        self.client.post(
            reverse("support"),
            {"message": "hello", "name": "Somebody Else", "email": "attacker@example.com"},
        )
        sent = Email.objects.filter(to=[settings.ADMINS[0][1]]).first()
        self.assertIsNotNone(sent)
        self.assertEqual(sent.headers.get("Reply-To"), self.user.email)
        self.assertNotIn("attacker@example.com", sent.message)

    def test_a_signed_in_account_with_no_email_is_still_asked_for_one(self):
        # There is nothing to reply to otherwise, which is the whole point of the form.
        self.user.email = ""
        form = ContactForm(user=self.user)
        self.assertIn("email", form.fields)

    def test_the_faq_no_longer_shows_the_address_to_anybody(self):
        # It used to print it to every signed-in account, which is one scraped session away from
        # publishing it. /support/ reaches the same inbox without putting it on a page.
        self.client.force_login(self.user)
        html = self.client.get(reverse("faq")).content.decode()
        self.assertNotIn(settings.ADMINS[0][1], html)
        self.assertIn(reverse("support"), html)

    @override_settings(ENABLE_HELP=True)
    def test_the_auction_help_page_sends_them_here_instead(self):
        # ENABLE_HELP is off by default, and off the page redirects home rather than rendering.
        self.client.force_login(self.user)
        html = self.client.get(reverse("auction_help", kwargs={"slug": self.online_auction.slug})).content.decode()
        self.assertNotIn(settings.ADMINS[0][1], html)
        self.assertIn(reverse("support"), html)
