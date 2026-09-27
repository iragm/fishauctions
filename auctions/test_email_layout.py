"""The HTML email layout: every seeded template and allauth's mail render on it, greet the same way,
and show the club logo and buttons."""

from types import SimpleNamespace
from unittest.mock import patch

from allauth.account.adapter import get_adapter
from allauth.core import context as allauth_context
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from post_office.models import EmailTemplate

from auctions.templatetags import email_tags


def render(source, **context):
    return Template(source).render(Context(context))


class LayoutTests(TestCase):
    def test_every_seeded_template_renders_as_a_full_page(self):
        """With an empty context, since a template that only renders with the right objects hides its syntax errors."""
        for template in EmailTemplate.objects.exclude(html_content=""):
            with self.subTest(template.name):
                out = render(template.html_content, domain="example.com")
                self.assertTrue(out.startswith("<!DOCTYPE html>"))
                self.assertIn("icon-footer.png", out)

    def test_every_seeded_text_part_renders(self):
        """post_office renders both parts inside ``mail.send``, so a text part that won't compile stops the send."""
        for template in EmailTemplate.objects.exclude(content=""):
            with self.subTest(template.name):
                self.assertIn("--", render(template.content, domain="example.com"))

    def test_every_seeded_template_opens_with_hey_and_has_no_sign_off(self):
        """The footer says who sent it, so a "Best wishes, <domain>" above it only repeats it."""
        for template in EmailTemplate.objects.all():
            for field in ("html_content", "content"):
                body = (getattr(template, field) or "").split("{% block content %}")[-1].strip()
                body = body.removeprefix("{% load email_tags %}")
                if not body:
                    continue
                with self.subTest(template.name, field=field):
                    self.assertTrue(body.startswith("Hey "), body[:40])
                    self.assertNotIn("Best wishes", body)

    def test_first_name_filter(self):
        for name, expected in (("Jamie Smith", "Jamie"), ("  Jamie  ", "Jamie"), ("Unknown", ""), ("", ""), (None, "")):
            with self.subTest(name):
                self.assertEqual(email_tags.first_name(name), expected)
        out = render('{% load email_tags %}Hey {{ name|first_name|default:"there" }},', name="")
        self.assertEqual(out, "Hey there,")

    def test_button_keeps_the_link_and_styles_it(self):
        out = render(
            '{% load email_tags %}{% email_button %}<a href="https://{{ domain }}/x/">Go</a>{% endemail_button %}',
            domain="example.com",
        )
        self.assertIn('href="https://example.com/x/"', out)
        self.assertIn("background-color:#375a7f", out)
        self.assertIn(">Go</a>", out)

    def test_link_button_escapes(self):
        out = email_tags.link_button("https://example.com/?a=1&b=2", "<Go>")
        self.assertIn("?a=1&amp;b=2", out)
        self.assertIn("&lt;Go&gt;", out)


class ClubHeaderTests(TestCase):
    HEADER = "{% load email_tags %}{% email_club_header %}"

    def club(self, icon):
        return SimpleNamespace(name="Tropical Fish Club", icon_thumbnail_url=icon)

    def test_found_through_whatever_the_send_site_passed(self):
        club = self.club("/media/club_icons/x.png")
        auction = SimpleNamespace(club=club)
        for context in (
            {"club": club},
            {"auction": auction},
            {"tos": SimpleNamespace(auction=auction)},
            {"invoice": SimpleNamespace(auction=auction)},
            {"lot": SimpleNamespace(auction=auction)},
        ):
            with self.subTest(list(context)):
                out = render(self.HEADER, domain="example.com", **context)
                self.assertIn('src="https://example.com/media/club_icons/x.png"', out)
                self.assertIn("Tropical Fish Club", out)

    def test_absolute_icon_url_is_left_alone(self):
        out = render(self.HEADER, domain="example.com", club=self.club("https://imagedelivery.net/a/b/club_icon"))
        self.assertIn('src="https://imagedelivery.net/a/b/club_icon"', out)

    def test_nothing_without_a_logo_or_a_club(self):
        for context in (
            {"club": self.club(None)},
            {"auction": SimpleNamespace(club=None)},
            {"lot": SimpleNamespace(auction=None)},
            {},
        ):
            with self.subTest(context):
                self.assertEqual(render(self.HEADER, domain="example.com", **context).strip(), "")


class AllauthEmailTests(TestCase):
    """allauth renders ``<prefix>_message.html`` beside the .txt, so its mail gets the layout too."""

    @staticmethod
    def request(user):
        # render_mail renders with the request, so the site's context processors run.
        request = RequestFactory().get("/")
        request.user = user
        request.session = SessionStore()
        return request

    def render_mail(self, prefix, **context):
        with allauth_context.request_context(self.request(self.user)):
            msg = get_adapter().render_mail(prefix, "jamie@example.com", {"user": self.user, **context})
        html = next(body for body, mime in msg.alternatives if mime == "text/html")
        return msg.body, html

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="jamie", email="jamie@example.com", first_name="Jamie")

    def test_confirmation_email(self):
        text, html = self.render_mail(
            "account/email/email_confirmation_signup", activate_url="https://example.com/confirm/k/"
        )
        self.assertTrue(text.startswith("Hey Jamie,"))
        self.assertIn("https://example.com/confirm/k/", text)
        self.assertNotIn("Thank you", text)
        self.assertIn("Hey Jamie,", html)
        self.assertIn('href="https://example.com/confirm/k/"', html)
        self.assertIn("Confirm your email address", html)
        self.assertIn("icon-footer.png", html)

    def test_password_reset_email(self):
        text, html = self.render_mail(
            "account/email/password_reset_key", password_reset_url="https://example.com/reset/k/", username="jamie"
        )
        self.assertIn("https://example.com/reset/k/", text)
        self.assertIn("Reset your password", html)
        self.assertIn("jamie", html)

    def test_no_user_greets_there_even_when_someone_is_signed_in(self):
        """The request's user is whoever asked, not the stranger this mail goes to."""
        with allauth_context.request_context(self.request(self.user)):
            msg = get_adapter().render_mail(
                "account/email/unknown_account",
                "x@example.com",
                {"email": "x@example.com", "signup_url": "https://example.com/signup/"},
            )
        self.assertTrue(msg.body.startswith("Hey there,"))
        self.assertIn("Hey there,", msg.alternatives[0][0])
        self.assertNotIn("Jamie", msg.body + msg.alternatives[0][0])
        self.assertIn("Sign up", msg.alternatives[0][0])


class AllauthFlowTests(TestCase):
    """The real views, so the real context allauth builds: what reaches the outbox is what people get."""

    def setUp(self):
        patcher = patch("auctions.forms.recaptcha_is_configured", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def html(self, message):
        return next(body for body, mime in message.alternatives if mime == "text/html")

    def test_password_reset_for_a_known_address(self):
        User.objects.create_user(username="jamie", email="jamie@example.com", password="x", first_name="Jamie")
        self.client.post(reverse("account_reset_password"), {"email": "jamie@example.com"})
        message = mail.outbox[-1]
        self.assertTrue(message.body.startswith("Hey Jamie,"))
        self.assertIn("Reset your password", self.html(message))
        self.assertIn("icon-footer.png", self.html(message))

    def test_password_reset_for_an_unknown_address_never_greets_the_visitor(self):
        visitor = User.objects.create_user(username="visitor", password="x", first_name="Vera")
        self.client.force_login(visitor)
        self.client.post(reverse("account_reset_password"), {"email": "stranger@example.com"})
        message = mail.outbox[-1]
        self.assertEqual(message.to, ["stranger@example.com"])
        for part in (message.body, self.html(message)):
            self.assertIn("Hey there,", part)
            self.assertNotIn("Vera", part)

    def test_signup_confirmation(self):
        self.client.post(
            reverse("account_signup"),
            {
                "email": "new@example.com",
                "first_name": "Nia",
                "last_name": "Jones",
                "username": "nia",
                "password1": "a-Long-passw0rd!",
                "password2": "a-Long-passw0rd!",
            },
        )
        message = mail.outbox[-1]
        self.assertEqual(message.to, ["new@example.com"])
        self.assertTrue(message.body.startswith("Hey Nia,"))
        self.assertIn("Confirm your email address", self.html(message))
