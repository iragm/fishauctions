"""The parts of an outgoing email that anti-spam law requires, and the donation footer's address.

Not style: CAN-SPAM wants a postal address and a working opt-out on bulk commercial mail, CASL wants
the sender identified in every commercial message and exempts less. Both are invisible when you read
the email, which is what makes them worth a test. See :mod:`auctions.email_footer`.
"""

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse

from auctions.email_footer import EXAMPLE_MAILING_ADDRESS, UNSET_MAILING_ADDRESS, mailing_address
from auctions.models import Club, DonationVendor, UserData

REAL_ADDRESS = "Some Fish Club, PO Box 1, Burlington VT 05401"

FOOTER_HTML = "{% load email_tags %}{% email_footer %}"
FOOTER_TEXT = "{% load email_tags %}{% email_footer_text %}"


def render(source, **context):
    return Template(source).render(Context(context))


class EveryEmailTemplateCarriesTheFooterTests(TestCase):
    """The footer is on every ``post_office`` template, so a new one can't ship without it."""

    def test_every_template_body_includes_the_footer_tag(self):
        from post_office.models import EmailTemplate

        missing = []
        for template in EmailTemplate.objects.all():
            if (template.html_content or "").strip() and FOOTER_HTML not in template.html_content:
                missing.append(f"{template.name}.html_content")
            if (template.content or "").strip() and FOOTER_TEXT not in template.content:
                missing.append(f"{template.name}.content")
        if missing:
            self.fail(
                "These email templates have no sender-identification footer. Add "
                f"'{FOOTER_HTML}' (or the _text twin) in a data migration:\n  " + "\n  ".join(missing)
            )

    def test_there_are_templates_to_check(self):
        """A migration that stopped seeding templates would otherwise make the test above vacuous."""
        from post_office.models import EmailTemplate

        self.assertGreater(EmailTemplate.objects.count(), 10)


@override_settings(MAILING_ADDRESS=REAL_ADDRESS, NAVBAR_BRAND="Fish Auctions")
class FooterContentsTests(TestCase):
    def test_html_footer_names_the_sender_and_the_address(self):
        out = render(FOOTER_HTML, domain="example.com")
        self.assertIn("Fish Auctions", out)
        self.assertIn(REAL_ADDRESS, out)

    def test_text_footer_names_the_sender_and_the_address(self):
        out = render(FOOTER_TEXT, domain="example.com")
        self.assertIn("Fish Auctions", out)
        self.assertIn(REAL_ADDRESS, out)

    def test_no_opt_out_line_without_an_unsubscribe_token(self):
        for source in (FOOTER_HTML, FOOTER_TEXT):
            self.assertNotIn("unsubscribe", render(source, domain="example.com"))

    def test_opt_out_line_appears_when_the_send_site_passes_a_token(self):
        for source in (FOOTER_HTML, FOOTER_TEXT):
            out = render(source, domain="example.com", unsubscribe="tok-en")
            self.assertIn("https://example.com/unsubscribe/tok-en/", out)

    def test_the_opt_out_link_is_a_real_url_that_unsubscribes(self):
        """The link has to work: an opt-out mechanism that 404s is not an opt-out mechanism."""
        user = User.objects.create_user(username="u", email="u@example.com", password="x")
        userdata, _ = UserData.objects.get_or_create(user=user)
        out = render(FOOTER_TEXT, domain="example.com", unsubscribe=userdata.unsubscribe_link)
        path = out.split("https://example.com")[1].split()[0]
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        userdata.refresh_from_db()
        self.assertTrue(userdata.has_unsubscribed)


class UnconfiguredAddressTests(TestCase):
    def test_placeholders_are_treated_as_no_address(self):
        for placeholder in (
            UNSET_MAILING_ADDRESS,
            EXAMPLE_MAILING_ADDRESS,
            # The admin checklist prints this line capitalized differently from .env.example.
            "123 Your Street, Anytown, USA",
            "",
            "   ",
        ):
            with override_settings(MAILING_ADDRESS=placeholder):
                self.assertEqual(mailing_address(), "")

    def test_footer_omits_an_unconfigured_address_rather_than_printing_the_placeholder(self):
        with override_settings(MAILING_ADDRESS=UNSET_MAILING_ADDRESS, NAVBAR_BRAND="Fish Auctions"):
            for source in (FOOTER_HTML, FOOTER_TEXT):
                out = render(source, domain="example.com")
                self.assertNotIn("No address configured", out)
                self.assertIn("Fish Auctions", out)


class DonationRequestNeedsAnAddressTests(TestCase):
    """A donation request is unambiguously bulk commercial mail to a stranger, so it can't go out
    with only the club's name where the postal address belongs."""

    def setUp(self):
        # Saved straight onto the model: a club that switched tracking on before the form asked for
        # an address is exactly the case this has to survive.
        self.club = Club.objects.create(
            name="Some Fish Club", donation_mailing_address="", enable_donation_tracking=True
        )
        self.vendor = DonationVendor.objects.create(club=self.club, name="Pet Shop", email="shop@example.com")

    def test_footer_refuses_to_build_without_an_address(self):
        from auctions import donations

        with self.assertRaises(donations.MissingMailingAddress):
            donations.unsubscribe_footer(self.vendor)

    def test_footer_has_the_address_and_the_opt_out_once_it_is_set(self):
        from auctions import donations

        self.club.donation_mailing_address = "PO Box 1, Burlington VT 05401"
        self.club.save()
        footer = donations.unsubscribe_footer(self.vendor)
        self.assertIn("PO Box 1, Burlington VT 05401", footer)
        self.assertIn(str(self.vendor.unsubscribe_url), footer)

    def test_the_form_will_not_switch_tracking_on_without_an_address(self):
        """Where an admin finds out, before any vendor is contacted."""
        from auctions.forms import ClubDonationSettingsForm

        form = ClubDonationSettingsForm(
            instance=self.club,
            data={
                "enable_donation_tracking": True,
                "donation_email_mode": Club.DONATION_EMAIL_MODE_COPY,
                "donation_followup_days": 14,
                "donation_mailing_address": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("donation_mailing_address", form.errors)

    def test_contacting_a_vendor_without_an_address_offers_the_settings_page(self):
        """A club that switched tracking on before that rule existed gets a screen, not a traceback."""
        user = User.objects.create_user(username="clubadmin", email="a@example.com", password="x")
        UserData.objects.get_or_create(user=user)
        user.is_superuser = True
        user.save()
        self.client.force_login(user)
        response = self.client.get(reverse("club_donation_contact", kwargs={"pk": self.vendor.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mailing address")
        self.assertContains(response, reverse("club_donation_settings", kwargs={"slug": self.club.slug}))
