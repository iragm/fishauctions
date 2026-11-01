"""Pages keyed on a member's UUID or membership number stay out of search, caches and Google's scripts,
and the public membership-number page doesn't name the member."""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock, patch

from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import Club, ClubMember, Invoice, SquareSeller

GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


class MemberPagePrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="privacy_owner", password="pw", email="owner@example.com")
        cls.stranger = User.objects.create_user(username="privacy_stranger", password="pw", email="x@example.com")
        cls.admin = User.objects.create_user(username="privacy_admin", password="pw", email="admin@example.com")
        cls.club = Club.objects.create(name="Privacy club", show_member_barcode=True)
        cls.member = ClubMember.objects.create(
            club=cls.club, user=cls.owner, name="Secret Name", email="owner@example.com", membership_number=7
        )
        ClubMember.objects.create(club=cls.club, user=cls.admin, name="Club Admin", permission_view=True)

    def uuid_url(self):
        return reverse("club_member_by_uuid", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})

    def number_url(self):
        return reverse("club_member_by_number", kwargs={"slug": self.club.slug, "number": 7})

    def assert_private(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_member_pages_are_private(self):
        self.assert_private(self.client.get(self.uuid_url()))
        self.assert_private(self.client.get(self.number_url()))
        for name in ("club_member_unsubscribe", "club_member_resubscribe", "club_member_nocomm"):
            url = reverse(name, kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
            self.assert_private(self.client.get(url))
        url = reverse(
            "club_member_contact_pref", kwargs={"slug": self.club.slug, "uuid": self.member.uuid, "level": "all"}
        )
        self.assert_private(self.client.get(url))

    def test_ordinary_pages_are_not(self):
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertNotIn("X-Robots-Tag", response)
        self.assertEqual(response["Referrer-Policy"], "strict-origin-when-cross-origin")

    @override_settings(GOOGLE_MEASUREMENT_ID="G-TEST123", GOOGLE_TAG_ID="GTM-TEST123")
    def test_member_pages_load_no_google_scripts(self):
        self.assertNotContains(self.client.get(self.uuid_url()), "googletagmanager")
        self.assertNotContains(self.client.get(self.number_url()), "googletagmanager")
        self.assertContains(self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug})), "G-TEST123")

    def test_number_page_names_the_member_only_to_them_and_admins(self):
        self.assertNotContains(self.client.get(self.number_url()), "Secret Name")
        self.client.force_login(self.stranger)
        self.assertNotContains(self.client.get(self.number_url()), "Secret Name")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.number_url()), "Secret Name")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.number_url()), "Secret Name")

    def test_uuid_page_still_names_the_member(self):
        self.assertContains(self.client.get(self.uuid_url()), "Secret Name")

    def test_a_crawler_is_not_member_activity(self):
        ClubMember.objects.filter(pk=self.member.pk).update(last_club_activity=None)
        self.client.get(self.uuid_url(), HTTP_USER_AGENT=GOOGLEBOT)
        self.member.refresh_from_db()
        self.assertIsNone(self.member.last_club_activity)
        self.client.get(self.uuid_url())
        self.member.refresh_from_db()
        self.assertIsNotNone(self.member.last_club_activity)

    @override_settings(ALLOW_SEARCH_INDEXING=True)
    def test_robots_disallows_member_pages(self):
        body = self.client.get("/robots.txt").content.decode()
        for path in ("/clubs/*/member/", "/clubs/*/member-number/", "/invoices/", "/unsubscribe/"):
            self.assertIn(f"Disallow: {path}", body)
        self.assertIn("Sitemap:", body)


class ClubInvoiceSquarePrefillTests(TestCase):
    """The membership-number page's pay buttons work for anybody, so Square's checkout must not show
    them the member's email."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="prefill_owner", password="pw", email="owner@fishclub.dev")
        cls.club = Club.objects.create(name="Prefill club", membership_annual_fee=Decimal(20))
        cls.member = ClubMember.objects.create(
            club=cls.club,
            user=cls.owner,
            name="Owner",
            email="owner@fishclub.dev",
            membership_last_paid=timezone.localdate() - datetime.timedelta(days=400),
        )
        cls.invoice = Invoice.objects.create(club=cls.club, club_member=cls.member, buyer=cls.owner, status="UNPAID")
        cls.seller = SquareSeller(user=cls.owner, currency="USD")

    def prefill_for(self, user, data=None):
        request = RequestFactory().post("/", data or {})
        request.user = user
        client = MagicMock()
        client.checkout.payment_links.create.return_value = MagicMock(payment_link=MagicMock(url="https://sq/x"))
        with (
            patch.object(SquareSeller, "get_square_client", return_value=client),
            patch.object(SquareSeller, "get_location_id", return_value="LOC"),
            patch.object(Invoice, "rounded_net_after_payments", new_callable=PropertyMock, return_value=-20),
        ):
            url, error = self.seller.create_payment_link(self.invoice, request)
        self.assertEqual((url, error), ("https://sq/x", None))
        return client.checkout.payment_links.create.call_args.kwargs["pre_populated_data"]

    def test_anonymous_payer_sees_no_email(self):
        self.assertNotIn("buyer_email", self.prefill_for(AnonymousUser()))

    def test_another_account_sees_no_email(self):
        other = User.objects.create_user(username="prefill_other", password="pw")
        self.assertNotIn("buyer_email", self.prefill_for(other))

    def test_the_member_signed_in_gets_their_email_filled_in(self):
        self.assertEqual(self.prefill_for(self.owner).get("buyer_email"), "owner@fishclub.dev")

    def test_paying_from_the_members_own_uuid_page_fills_it_in(self):
        data = {"member_uuid": str(self.member.uuid)}
        self.assertEqual(self.prefill_for(AnonymousUser(), data).get("buyer_email"), "owner@fishclub.dev")

    def test_another_members_uuid_fills_in_nothing(self):
        other = ClubMember.objects.create(club=self.club, name="Other", email="other@fishclub.dev")
        for value in (str(other.uuid), "not-a-uuid"):
            self.assertNotIn("buyer_email", self.prefill_for(AnonymousUser(), {"member_uuid": value}))

    def test_uuid_page_posts_the_uuid_and_number_page_does_not(self):
        from auctions.views.club_pages import _get_or_create_membership_invoice

        invoice = _get_or_create_membership_invoice(self.club, self.member)
        with (
            patch.object(Invoice, "show_payment_button", new_callable=PropertyMock, return_value=True),
            patch.object(Invoice, "show_square_button", new_callable=PropertyMock, return_value=True),
            patch("auctions.views.club_pages._membership_renewal_state", return_value=(True, False, True, True)),
        ):
            uuid_page = self.client.get(
                reverse("club_member_by_uuid", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})
            )
            number_page = self.client.get(
                reverse(
                    "club_member_by_number",
                    kwargs={"slug": self.club.slug, "number": self.member.membership_number},
                )
            )
        self.assertContains(uuid_page, invoice.no_login_link)
        self.assertContains(uuid_page, f'name="member_uuid" value="{self.member.uuid}"')
        self.assertContains(number_page, invoice.no_login_link)
        self.assertNotContains(number_page, "member_uuid")


class SuccessRedirectTests(TestCase):
    def test_uuid_page_only_for_the_invoices_member(self):
        from auctions.views.payments import _club_membership_success_url

        club = Club.objects.create(name="Redirect club")
        mine = ClubMember.objects.create(club=club, name="Mine")
        theirs = ClubMember.objects.create(club=club, name="Theirs")
        ClubMember.objects.filter(pk=theirs.pk).update(membership_number=0)  # no number: the UUID page
        their_invoice = Invoice.objects.create(club=club, club_member=theirs, status="UNPAID")
        my_invoice = Invoice.objects.create(club=club, club_member=mine, status="UNPAID")
        self.assertIn(str(theirs.uuid), _club_membership_success_url(club, theirs.pk, their_invoice))
        self.assertNotIn(str(theirs.uuid), _club_membership_success_url(club, theirs.pk, my_invoice))


class EmailedKeyPagesArePrivateTests(TestCase):
    def test_allauth_confirm_email_page(self):
        response = self.client.get(reverse("account_confirm_email", kwargs={"key": "nope"}))
        self.assertIn("noindex", response.get("X-Robots-Tag", ""))

    def test_reverify_page(self):
        response = self.client.get(reverse("reverify_email_confirm", kwargs={"token": "nope"}))
        self.assertIn("noindex", response.get("X-Robots-Tag", ""))
