"""Wallet status text, error-page logging, and the label-printing surfaces in the app."""

import datetime
import io
import json
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubAPIKey,
    ClubAPIKeyFieldMap,
    ClubHistory,
    ClubMember,
    ClubMoney,
    Lot,
    PageView,
    PickupLocation,
)
from auctions.services import save_new_lot
from auctions.test_wallet_passes import AppleWalletPassTests
from auctions.tests import StandardTestCase


def _raising_context_processor(request):
    """Used by ErrorPageLoggingTests to make base.html-extending pages unrenderable."""
    msg = "context processor boom"
    raise RuntimeError(msg)


class ErrorPageLoggingTests(TestCase):
    """Custom 404/500 handlers (auctions/error_views.py) must log the traceback that Django's
    get_exception_response() otherwise swallows -- prod was emailing traceback-less
    "Report at /byp8.php" 500s with no way to see the real cause."""

    def _broken_templates(self):
        import copy

        from django.conf import settings

        templates = copy.deepcopy(settings.TEMPLATES)
        templates[0]["OPTIONS"]["context_processors"].append("auctions.test_wallet_status._raising_context_processor")
        return templates

    def test_normal_404_still_renders_the_404_page(self):
        with override_settings(DEBUG=False):
            response = self.client.get("/this-page-does-not-exist.php")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)

    def test_404_render_failure_logs_the_real_traceback(self):
        # The 404 page extends base.html, so a broken context processor makes its render raise.
        # Django then falls back to the 500 handler; the handler must have logged the cause first.
        client = Client(raise_request_exception=False)
        with override_settings(DEBUG=False, TEMPLATES=self._broken_templates()):
            with self.assertLogs("auctions.errorpages", level="ERROR") as logs:
                response = client.get("/this-page-does-not-exist.php")
        self.assertEqual(response.status_code, 500, "a failed 404 render must fall back to the 500 page")
        joined = "\n".join(logs.output)
        self.assertIn("404 page render failed", joined)
        self.assertIn("context processor boom", joined, "the log must contain the swallowed traceback")

    def test_500_render_failure_serves_plaintext_and_logs(self):
        from django.test import RequestFactory

        from auctions import error_views

        request = RequestFactory().get("/whatever/")
        with patch.object(error_views.defaults, "server_error", side_effect=RuntimeError("500 template boom")):
            with self.assertLogs("auctions.errorpages", level="ERROR") as logs:
                response = error_views.error_500(request)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("500 template boom", "\n".join(logs.output))


class WalletHeaderTextTests(TestCase):
    """wallet_header_text drives the pass-type line on wallet passes: live paid/unpaid
    status for dues-charging clubs, static "Membership" everywhere else."""

    def _member(self, membership_system="january_first", fee=25, paid=True):
        from decimal import Decimal

        from auctions.models import Club, ClubMember

        club = Club.objects.create(
            name=f"Header club {membership_system} {fee} {paid}",
            membership_system=membership_system,
            membership_annual_fee=Decimal(fee),
        )
        expiration = timezone.now().date() + datetime.timedelta(days=30 if paid else -30)
        return ClubMember.objects.create(club=club, name="M", membership_expiration_date=expiration)

    def test_paid_member_of_fee_charging_club(self):
        self.assertEqual(self._member(paid=True).wallet_header_text, "Active Paid Membership")

    def test_lapsed_member_of_fee_charging_club(self):
        self.assertEqual(self._member(paid=False).wallet_header_text, "Unpaid Membership")

    def test_never_paid_member_of_fee_charging_club(self):
        from decimal import Decimal

        from auctions.models import Club, ClubMember

        club = Club.objects.create(
            name="Header club never paid", membership_system="rolling", membership_annual_fee=Decimal(25)
        )
        member = ClubMember.objects.create(club=club, name="M")
        self.assertEqual(member.wallet_header_text, "Unpaid Membership")

    def test_free_membership_club_stays_static(self):
        self.assertEqual(self._member(fee=0, paid=False).wallet_header_text, "Membership")

    def test_club_without_memberships_stays_static(self):
        self.assertEqual(self._member(membership_system="none", paid=False).wallet_header_text, "Membership")


class WalletStatusTextTests(TestCase):
    """wallet_status_text prints the expiration date on the card instead of letting
    the wallet apps expire the pass programmatically (which auto-archives it)."""

    def _member(self, membership_system="january_first", paid=True, expiration=None, last_paid=None):
        from decimal import Decimal

        from auctions.models import Club, ClubMember

        club = Club.objects.create(
            name=f"Status club {membership_system} {paid} {expiration} {last_paid}",
            membership_system=membership_system,
            membership_annual_fee=Decimal(25),
        )
        if expiration is None and last_paid is None:
            expiration = timezone.now().date() + datetime.timedelta(days=30 if paid else -30)
        return ClubMember.objects.create(
            club=club, name="M", membership_expiration_date=expiration, membership_last_paid=last_paid
        )

    def test_current_member_shows_valid_through(self):
        expiration = timezone.now().date() + datetime.timedelta(days=30)
        member = self._member(expiration=expiration)
        self.assertEqual(member.wallet_status_text, f"Valid through {expiration.strftime('%-d %b %Y')}")

    def test_lapsed_member_shows_printed_expiration_date(self):
        """A lapsed membership prints its (past) expiration date, not just 'Unpaid/expired'."""
        expiration = timezone.now().date() - datetime.timedelta(days=5)
        member = self._member(expiration=expiration)
        self.assertEqual(member.wallet_status_text, f"Expired {expiration.strftime('%-d %b %Y')}")

    def test_never_paid_member_has_no_date(self):
        from decimal import Decimal

        from auctions.models import Club, ClubMember

        club = Club.objects.create(
            name="Status club never paid", membership_system="rolling", membership_annual_fee=Decimal(25)
        )
        member = ClubMember.objects.create(club=club, name="M")
        self.assertEqual(member.wallet_status_text, "Unpaid/expired")

    def test_club_without_memberships_has_no_status(self):
        self.assertIsNone(self._member(membership_system="none").wallet_status_text)

    def test_google_object_patch_omits_valid_time_interval(self):
        """No validTimeInterval in the PATCH body — that field auto-archives lapsed passes."""
        from auctions.google_wallet import update_generic_object_for_member

        member = self._member(expiration=timezone.now().date() - datetime.timedelta(days=5))
        resp = MagicMock(status_code=200)
        with override_settings(
            GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX",
            GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="signer@example.iam.gserviceaccount.com",
            GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="fake-key",
        ):
            with patch("auctions.google_wallet.get_access_token", return_value="t"):
                with patch("auctions.google_wallet.requests.patch", return_value=resp) as patch_mock:
                    self.assertTrue(update_generic_object_for_member(member))
        self.assertNotIn("validTimeInterval", patch_mock.call_args.kwargs["json"])

    @override_settings(
        APPLE_WALLET_CERT_FILE="x",
        APPLE_WALLET_WWDR_FILE="y",
        APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example",
        APPLE_WALLET_TEAM_IDENTIFIER="TEAMID",
        APPLE_WALLET_ORGANIZATION_NAME="",
    )
    def test_apple_pass_json_omits_expiration_date(self):
        """No expirationDate in pass.json — that field greys out/archives lapsed passes."""
        from auctions.apple_wallet import _build_pass_json

        member = self._member(expiration=timezone.now().date() - datetime.timedelta(days=5))
        with patch("auctions.apple_wallet.ensure_apple_pass_auth_token", return_value="tok"):
            pass_json = _build_pass_json(member)
        self.assertNotIn("expirationDate", pass_json)
        statuses = [f["value"] for f in pass_json["generic"]["auxiliaryFields"] if f["key"] == "status"]
        self.assertTrue(statuses and statuses[0].startswith("Expired "))


@override_settings(
    GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX",
    GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="signer@example.iam.gserviceaccount.com",
    GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="fake-key",
)
class GoogleWalletStatusDisplayTests(TestCase):
    """The class template must show the membership_status module on the card front
    (cardTemplateOverride replaces the default layout, so an unlisted module is
    invisible there — this is why expiration dates were "not present"), and object
    PATCHes must keep the header (pass-type line) current."""

    def setUp(self):
        from decimal import Decimal

        from auctions.models import Club, ClubMember

        self.club = Club.objects.create(
            name="Status Display Club", membership_system="january_first", membership_annual_fee=Decimal(25)
        )
        self.member = ClubMember.objects.create(club=self.club, name="M")

    def test_class_template_includes_status_row(self):
        from auctions.google_wallet import _class_body

        rows = _class_body(self.club)["classTemplateInfo"]["cardTemplateOverride"]["cardRowTemplateInfos"]
        field_paths = [row["oneItem"]["item"]["firstValue"]["fields"][0]["fieldPath"] for row in rows]
        self.assertIn("object.textModulesData['member_id']", field_paths)
        self.assertIn(
            "object.textModulesData['membership_status']",
            field_paths,
            "without this row the 'Valid through ...' status never shows on the card front",
        )

    def test_object_patch_includes_live_header(self):
        from auctions.google_wallet import update_generic_object_for_member

        resp = MagicMock()
        resp.status_code = 200
        with patch("auctions.google_wallet.get_access_token", return_value="t"):
            with patch("auctions.google_wallet.requests.patch", return_value=resp) as patch_mock:
                self.assertTrue(update_generic_object_for_member(self.member))
        body = patch_mock.call_args.kwargs["json"]
        self.assertEqual(
            body["header"]["defaultValue"]["value"],
            "Unpaid Membership",
            "the pass-type line must update when membership status changes",
        )

    def test_save_url_jwt_uses_live_header(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from auctions.templatetags.membership_tags import google_wallet_save_url

        key_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with self.settings(GOOGLE_WALLET_SERVICE_ACCOUNT_KEY=key_pem.decode()):
            url = google_wallet_save_url(self.member)
        self.assertTrue(url.startswith("https://pay.google.com/gp/v/save/"))
        import jwt as _jwt

        payload = _jwt.decode(url.rsplit("/", 1)[1], options={"verify_signature": False})
        generic_object = payload["payload"]["genericObjects"][0]
        self.assertEqual(generic_object["header"]["defaultValue"]["value"], "Unpaid Membership")


class AppleWalletCertValidationTests(TestCase):
    """_load_signing_certs must accept Apple's DER .cer WWDR format and reject a WWDR
    that did not issue the Pass Type ID cert — a mismatched chain surfaces on devices
    as 'WWDR certificate missing' with no server-side trace otherwise."""

    def _settings(self, tmp_path, p12_path, wwdr_path):
        return self.settings(
            BASE_DIR=tmp_path,
            APPLE_WALLET_CERT_FILE=p12_path.name,
            APPLE_WALLET_CERT_PASSWORD="",
            APPLE_WALLET_WWDR_FILE=wwdr_path.name,
            APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
            APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345",
            APPLE_WALLET_ORGANIZATION_NAME="Test Org",
        )

    def test_der_encoded_wwdr_is_accepted(self):
        from pathlib import Path

        from auctions import apple_wallet
        from auctions.models import Club, ClubMember

        member = ClubMember.objects.create(club=Club.objects.create(name="DER club"), name="M")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path, wwdr_encoding="DER")
            apple_wallet._load_signing_certs.cache_clear()
            with self._settings(tmp_path, p12_path, wwdr_path):
                pkpass = apple_wallet.generate_pkpass_for_member(member)
        self.assertGreater(len(pkpass), 0)

    def test_mismatched_wwdr_raises_actionable_error(self):
        from pathlib import Path

        from auctions import apple_wallet

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path, chained=False)
            apple_wallet._load_signing_certs.cache_clear()
            with self._settings(tmp_path, p12_path, wwdr_path):
                with self.assertRaises(ValueError) as ctx:
                    apple_wallet._load_signing_certs()
        self.assertIn("WWDR", str(ctx.exception))
        self.assertIn("did not issue", str(ctx.exception))

    def test_missing_wwdr_file_raises_actionable_error(self):
        from pathlib import Path

        from auctions import apple_wallet

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, _wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path)
            apple_wallet._load_signing_certs.cache_clear()
            with self._settings(tmp_path, p12_path, Path(tmpdir) / "nonexistent.pem"):
                with self.assertRaises(ValueError) as ctx:
                    apple_wallet._load_signing_certs()
        self.assertIn("does not exist", str(ctx.exception))

    def test_check_apple_wallet_command_reports_success(self):
        from pathlib import Path

        from auctions import apple_wallet

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path)
            apple_wallet._load_signing_certs.cache_clear()
            out = io.StringIO()
            with self._settings(tmp_path, p12_path, wwdr_path):
                call_command("check_apple_wallet", stdout=out)
        self.assertIn("looks good", out.getvalue())

    def test_check_apple_wallet_command_reports_chain_mismatch(self):
        from pathlib import Path

        from auctions import apple_wallet

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path, chained=False)
            apple_wallet._load_signing_certs.cache_clear()
            out = io.StringIO()
            with self._settings(tmp_path, p12_path, wwdr_path):
                call_command("check_apple_wallet", stdout=out)
        self.assertIn("did not issue", out.getvalue())


class UniqueViewsCountTest(StandardTestCase):
    """Auction.unique_views counts distinct logged-in users plus anonymous sessions that never
    also appear on a logged-in row. The rewrite computes the anonymous set difference in Python
    instead of a NOT IN (subquery) anti-join (which made MariaDB full-scan auctions_pageview for
    hours); this pins the counting semantics so that optimization can't drift them."""

    def test_unique_views_dedupes_anonymous_login_transition(self):
        auction = self.online_auction
        # Anonymous session viewing the auction rules page, recorded twice -- distinct collapses it.
        PageView.objects.create(auction=auction, user=None, session_id="s1")
        PageView.objects.create(auction=auction, user=None, session_id="s1")
        # Anonymous session viewing a lot page (reaches the auction via lot_number__auction).
        PageView.objects.create(lot_number=self.lot, user=None, session_id="s2")
        # Logged-in view (stores the user, NULL session).
        PageView.objects.create(lot_number=self.lot, user=self.userB, session_id=None)
        # "Browsed anonymously then logged in": the same session_id appears both anonymously and on
        # a logged-in row, so it must NOT also be counted as an anonymous session.
        PageView.objects.create(auction=auction, user=None, session_id="s3")
        PageView.objects.create(auction=auction, user=self.user, session_id="s3")
        # Noise: a different auction's view must not leak in.
        PageView.objects.create(auction=self.in_person_auction, user=None, session_id="other")

        # logged_in = distinct users {userB, user}; anonymous = {s1, s2, s3} - {s3} = {s1, s2}.
        self.assertEqual(auction.unique_views, {"total": 4, "logged_in": 2, "anonymous": 2})


class MobileAppLabelPrintingVisibilityTests(StandardTestCase):
    """Label/barcode printing must be reachable inside the native app exactly as it is on the web.

    Regression (reported 2026-07-25): every batch/bulk print entry point was wrapped in
    ``{% if not request.is_mobile_app %}`` on the assumption that the app always prints natively
    over Bluetooth. That only ever held for the per-lot button on the lot page, and only for one of
    three print methods -- users on the PDF or System-printer method (the default) lost label
    printing entirely inside the app. The app intercepts these downloads itself, so the links must
    render for every user agent.
    """

    APP_UA = "FishAuctionsApp/1.0 (iOS)"
    WEB_UA = "Mozilla/5.0"

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Printing Club")
        ClubMember.objects.create(club=self.club, user=self.admin_user, permission_admin=True)
        self.in_person_auction.club = self.club
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.in_person_auction.save()
        userdata = self.user.userdata
        userdata.last_auction_used = self.in_person_auction
        userdata.save()

    def _get(self, url, user_agent, user=None):
        self.client.force_login(user or self.user)
        return self.client.get(url, HTTP_USER_AGENT=user_agent)

    def _assert_same_for_app_and_web(self, url, needle, user=None):
        """The link must render identically under both user agents."""
        for ua in (self.WEB_UA, self.APP_UA):
            response = self._get(url, ua, user=user)
            self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code} for {ua}")
            self.assertIn(needle, response.content.decode("utf-8"), f"{needle} missing from {url} for UA {ua}")

    def test_printing_prefs_page_shows_print_labels_button(self):
        """user_labels.html: 'Print labels for <auction>'."""
        self._assert_same_for_app_and_web(
            reverse("printing"),
            reverse("print_my_labels", kwargs={"slug": self.in_person_auction.slug}),
        )

    def test_selling_dashboard_shows_print_labels_button(self):
        """auctions/partials/lot_user_table_header.html, on lot lists."""
        self._assert_same_for_app_and_web(
            reverse("selling"),
            reverse("print_my_labels", kwargs={"slug": self.in_person_auction.slug}),
        )

    def test_auction_page_shows_my_print_labels_button(self):
        """auction.html: the seller's own 'Print Labels'."""
        Lot.objects.create(
            lot_name="a lot so user_has_lots is true",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            user=self.user,
            quantity=1,
        )
        self._assert_same_for_app_and_web(
            self.in_person_auction.url,
            reverse("print_my_labels", kwargs={"slug": self.in_person_auction.slug}),
        )

    def test_auction_page_admin_actions_show_print_labels(self):
        """auction.html admin actions + auction_ribbon.html dropdown: 'Print labels'."""
        self._assert_same_for_app_and_web(
            self.in_person_auction.url,
            reverse("auction_printing", kwargs={"slug": self.in_person_auction.slug}),
            user=self.admin_user,
        )

    def test_auction_printing_page_itself_loads_in_app(self):
        self._assert_same_for_app_and_web(
            reverse("auction_printing", kwargs={"slug": self.in_person_auction.slug}),
            "Print labels",
            user=self.admin_user,
        )

    def test_bulk_add_lots_shows_save_and_print(self):
        """auctions/bulk_add_lots.html: the 'Save and print labels' submit."""
        self._assert_same_for_app_and_web(
            reverse(
                "bulk_add_lots",
                kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.in_person_tos.bidder_number},
            ),
            "Save and print labels",
        )

    def test_quick_check_in_shows_print_barcodes_link(self):
        """auctions/quick_check_in_users.html: 'print barcodes to scan here'."""
        self._assert_same_for_app_and_web(
            reverse("auction_quick_check_in", kwargs={"slug": self.in_person_auction.slug}),
            reverse("club_barcode_labels", kwargs={"slug": self.club.slug}),
            user=self.admin_user,
        )

    def test_self_check_in_shows_print_barcodes_link(self):
        """auctions/self_check_in.html: 'Print barcodes here.'"""
        self.in_person_auction.manage_users_through_club = "checkin"
        self.in_person_auction.save()
        self._assert_same_for_app_and_web(
            reverse("auction_self_check_in", kwargs={"slug": self.in_person_auction.slug}),
            reverse("club_barcode_labels", kwargs={"slug": self.club.slug}),
            user=self.admin_user,
        )

    def test_club_barcode_labels_page_itself_loads_in_app(self):
        self._assert_same_for_app_and_web(
            reverse("club_barcode_labels", kwargs={"slug": self.club.slug}),
            "Print",
            user=self.admin_user,
        )

    def test_users_table_print_links_are_reachable_at_every_width(self):
        """The users table's 'Print labels' / 'Print only N unprinted labels' are not UA-gated, and
        the desktop column and the phone Actions dropdown cover complementary widths.

        The ``Lot labels`` column is ``d-md-table-cell d-none`` (md and up only), so on a phone the
        links have to come from the row's Actions dropdown -- whose items carry ``d-md-none`` (below
        md only). Neither width may lose a link.
        """
        tos = self.in_person_tos
        for i in range(3):
            Lot.objects.create(
                lot_name=f"users table lot {i}",
                auction=self.in_person_auction,
                auctiontos_seller=tos,
                quantity=1,
                label_printed=(i == 0),
            )
        self.assertEqual(tos.unprinted_label_count, 2)
        print_all_url = reverse(
            "print_labels_by_bidder_number",
            kwargs={"slug": self.in_person_auction.slug, "bidder_number": tos.bidder_number},
        )
        unprinted_url = reverse(
            "print_unprinted_labels_by_bidder_number",
            kwargs={"slug": self.in_person_auction.slug, "bidder_number": tos.bidder_number},
        )

        # md and up: the Lot labels column carries both.
        column = tos.print_labels_html
        self.assertIn(print_all_url, column)
        self.assertIn(unprinted_url, column)

        # Below md: the column is hidden, so the Actions dropdown must carry both, marked d-md-none
        # so they appear exactly where the column does not.
        dropdown = tos.actions_dropdown_html
        for url in (print_all_url, unprinted_url):
            self.assertIn(url, dropdown)
            item = next(chunk for chunk in dropdown.split("<span class='dropdown-item") if url in chunk)
            self.assertTrue(item.startswith(" d-md-none"), f"{url} is not shown at phone widths: {item[:80]}")

    def test_no_printing_template_still_gates_on_the_app_user_agent(self):
        """Guard against the gate creeping back in. The only legitimate request.is_mobile_app uses
        left in printing templates are app-only *additions* (the native Bluetooth per-lot button and
        the Bluetooth connect card), never a wrapper that hides a web print link."""
        template_dir = Path(__file__).resolve().parent / "templates"
        allowed = {"view_lot_images.html", "printing_extras.html"}
        offenders = []
        for path in template_dir.rglob("*.html"):
            if path.name in allowed:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if "is_mobile_app" not in line:
                    continue
                if re.search(r"print|label|barcode", line, re.IGNORECASE):
                    offenders.append(f"{path.relative_to(template_dir)}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [], "Label printing must not be gated on the mobile app UA:\n" + "\n".join(offenders)
        )


class ClubBarcodeLabelsPDFTests(StandardTestCase):
    """ "Download PDF" with nothing filled in used to 404, which reads as a broken feature."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Barcode Club")
        ClubMember.objects.create(club=self.club, user=self.admin_user, permission_admin=True)
        self.url = reverse("club_barcode_labels_pdf", kwargs={"slug": self.club.slug})
        self.client.force_login(self.admin_user)

    def test_an_empty_form_comes_back_to_the_page_with_a_reason(self):
        response = self.client.get(self.url, {"label_type": ""}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, reverse("club_barcode_labels", kwargs={"slug": self.club.slug}))
        self.assertIn("nothing to print", " ".join(str(m) for m in response.context["messages"]).lower())

    def test_a_row_with_a_type_but_no_value_is_the_same_as_an_empty_one(self):
        response = self.client.get(self.url, {"label_type": "bidder_paddle", "bidder_number": ""})
        self.assertEqual(response.status_code, 302)

    def test_a_complete_row_still_produces_a_pdf(self):
        response = self.client.get(self.url, {"label_type": "bidder_paddle", "bidder_number": "12"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")


class ClubMemberMembershipStatusFilterTests(TestCase):
    """The membership chips on the member list ("Paid club member", "Expiring soon", "Unpaid",
    "Never paid") must agree with ClubMember.is_paid_member.

    They used to read membership_expiration_date and nothing else, so every member whose dues
    were recorded only as a last-paid date (CSV imports, older rosters, auction-invoice
    renewals) came back as unpaid *and* never-paid, and never as paid or expiring.
    """

    def setUp(self):
        self.today = timezone.now().date()
        self.club = Club.objects.create(
            name="Membership Filter Club",
            membership_system="rolling",
            membership_annual_fee=Decimal(20),
        )
        self.current = ClubMember.objects.create(
            club=self.club,
            name="Current Member",
            membership_expiration_date=self.today + datetime.timedelta(days=200),
        )
        self.expiring = ClubMember.objects.create(
            club=self.club,
            name="Expiring Member",
            membership_expiration_date=self.today + datetime.timedelta(days=10),
        )
        self.expired = ClubMember.objects.create(
            club=self.club,
            name="Expired Member",
            membership_expiration_date=self.today - datetime.timedelta(days=1),
        )
        self.never_paid = ClubMember.objects.create(club=self.club, name="Never Paid Member")
        self.paid_without_expiration = ClubMember.objects.create(
            club=self.club,
            name="Last Paid Only Member",
            membership_last_paid=self.today - datetime.timedelta(days=30),
        )

    def _names(self, query):
        from auctions.filters import ClubMemberFilter

        qs = ClubMember.objects.filter(club=self.club)
        return set(ClubMemberFilter({"query": query}, queryset=qs).qs.values_list("name", flat=True))

    def test_paid_matches_is_paid_member(self):
        expected = {member.name for member in ClubMember.objects.filter(club=self.club) if member.is_paid_member}
        self.assertEqual(self._names("current"), expected)
        self.assertIn(self.paid_without_expiration.name, self._names("current"))

    def test_unpaid_is_the_complement_of_paid(self):
        self.assertEqual(self._names("expired"), {self.expired.name, self.never_paid.name})

    def test_never_paid_excludes_members_with_a_last_paid_date(self):
        self.assertEqual(self._names("never"), {self.never_paid.name})

    def test_expiring_soon_uses_the_explicit_expiration(self):
        self.assertEqual(self._names("expiring"), {self.expiring.name})

    def test_expiring_soon_covers_a_rolling_expiration_derived_from_last_paid(self):
        derived = ClubMember.objects.create(
            club=self.club,
            name="Derived Expiring Member",
            membership_last_paid=self.today - datetime.timedelta(days=355),
        )
        self.assertEqual(derived.effective_expiration_date, self.today + datetime.timedelta(days=10))
        self.assertIn(derived.name, self._names("expiring"))

    def test_january_first_derived_expiration_only_counts_inside_the_window(self):
        from auctions.filters import membership_expiring_soon_q

        club = Club.objects.create(
            name="January Club", membership_system="january_first", membership_annual_fee=Decimal(20)
        )
        paid_this_year = ClubMember.objects.create(
            club=club, name="Paid This Year", membership_last_paid=datetime.date(2026, 3, 1)
        )
        ClubMember.objects.create(club=club, name="Paid Last Year", membership_last_paid=datetime.date(2025, 3, 1))
        # Three weeks out from the January 1st those memberships roll over on.
        december = ClubMember.objects.filter(club=club).filter(membership_expiring_soon_q(datetime.date(2026, 12, 20)))
        self.assertEqual(list(december), [paid_this_year])
        # Mid-year there is no January 1st in the next 30 days, so nothing is expiring.
        midyear = ClubMember.objects.filter(club=club).filter(membership_expiring_soon_q(datetime.date(2026, 6, 1)))
        self.assertEqual(list(midyear), [])

    def test_club_admin_page_applies_the_expiring_chip(self):
        admin = User.objects.create_user(username="chip_admin", password="testpass", email="chip@example.com")
        ClubMember.objects.create(club=self.club, user=admin, name="Chip Admin", permission_add_edit=True)
        self.client.login(username="chip_admin", password="testpass")
        url = reverse("club_admin", kwargs={"slug": self.club.slug})
        response = self.client.get(url, {"query": "expiring"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["filter"].qs.values_list("name", flat=True)), [self.expiring.name])

    def test_expires_column_shows_the_derived_date_instead_of_expired(self):
        from auctions.tables import ClubMemberHTMxTable

        table = ClubMemberHTMxTable([], club_has_fee=True)
        rendered = table.render_membership_expiration_date(None, self.paid_without_expiration)
        self.assertIn(self.paid_without_expiration.effective_expiration_date.strftime("%b"), rendered)
        self.assertNotIn("Expired", rendered)


class ClubMemberResendCardTests(TestCase):
    """The "Resend membership card" action on the member list."""

    def setUp(self):
        self.club = Club.objects.create(
            name="Resend Card Club", show_member_barcode=True, membership_annual_fee=Decimal(20)
        )
        self.admin = User.objects.create_user(username="resend_admin", password="testpass", email="ra@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, name="Resend Admin", permission_add_edit=True)
        self.plain = User.objects.create_user(username="resend_plain", password="testpass", email="rp@example.com")
        ClubMember.objects.create(club=self.club, user=self.plain, name="Resend Plain")
        self.member = ClubMember.objects.create(club=self.club, name="John Smith", email="john@example.com")
        self.confirm_url = reverse("club_member_confirm", kwargs={"pk": self.member.pk, "action": "resend_card"})
        self.action_url = reverse("club_member_resend_card", kwargs={"pk": self.member.pk})

    def test_the_actions_dropdown_offers_it(self):
        self.client.login(username="resend_admin", password="testpass")
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Resend membership card")
        self.assertContains(response, self.confirm_url)

    def test_the_dropdown_hides_it_when_the_club_has_no_membership_cards(self):
        self.club.show_member_barcode = False
        self.club.save()
        self.client.login(username="resend_admin", password="testpass")
        response = self.client.get(reverse("club_admin", kwargs={"slug": self.club.slug}))
        self.assertNotContains(response, "Resend membership card")

    def test_the_confirm_names_the_member_and_their_email(self):
        self.client.login(username="resend_admin", password="testpass")
        response = self.client.get(self.confirm_url)
        self.assertContains(response, "Email John Smith (john@example.com) a link to their membership card")

    def test_sending_emails_the_member_and_records_it(self):
        self.client.login(username="resend_admin", password="testpass")
        with patch("auctions.tasks.mail.send") as send:
            response = self.client.post(self.action_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send.called)
        self.assertEqual(send.call_args.args[0], "john@example.com")
        self.assertIn("membership card", send.call_args.kwargs["subject"])
        self.assertIn(self.member.member_page_url, send.call_args.kwargs["html_message"])
        self.assertTrue(
            ClubHistory.objects.filter(
                club=self.club, action__contains="Emailed membership card to John Smith"
            ).exists()
        )

    def test_sending_always_emails_even_for_a_push_subscriber(self):
        """The admin confirmed an email, so this must not turn into a push notification."""
        self.client.login(username="resend_admin", password="testpass")
        with patch("auctions.notifications.user_prefers_push", return_value=True):
            with patch("auctions.tasks.mail.send") as send:
                self.client.post(self.action_url)
        self.assertTrue(send.called)

    def test_a_member_with_no_email_is_reported_not_silently_skipped(self):
        self.member.email = ""
        self.member.save()
        self.client.login(username="resend_admin", password="testpass")
        with patch("auctions.tasks.mail.send") as send:
            response = self.client.post(self.action_url)
        self.assertFalse(send.called)
        self.assertContains(response, "no email address on file")
        self.assertFalse(ClubHistory.objects.filter(club=self.club, action__contains="Emailed membership").exists())

    def test_do_not_contact_members_are_not_emailed(self):
        self.member.contact_status = "do_not_contact"
        self.member.save()
        self.client.login(username="resend_admin", password="testpass")
        with patch("auctions.tasks.mail.send") as send:
            response = self.client.post(self.action_url)
        self.assertFalse(send.called)
        self.assertContains(response, "do-not-contact")

    def test_it_needs_permission_to_edit_members(self):
        self.client.login(username="resend_plain", password="testpass")
        self.assertEqual(self.client.get(self.confirm_url).status_code, 403)
        self.assertEqual(self.client.post(self.action_url).status_code, 403)

    def test_it_is_unreachable_when_the_club_has_no_membership_cards(self):
        self.club.show_member_barcode = False
        self.club.save()
        self.client.login(username="resend_admin", password="testpass")
        self.assertEqual(self.client.get(self.confirm_url).status_code, 404)
        self.assertEqual(self.client.post(self.action_url).status_code, 404)


@override_settings(
    GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX",
    GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="signer@example.iam.gserviceaccount.com",
    GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="fake-key",
    APPLE_WALLET_CERT_FILE="pass.p12",
    APPLE_WALLET_WWDR_FILE="wwdr.cer",
    APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
    APPLE_WALLET_TEAM_IDENTIFIER="TEAM123",
)
class MembershipEmailWalletButtonTests(TestCase):
    """Membership emails carry the barcode, so they also carry the wallet buttons — but only for
    the wallets this site is actually configured for."""

    GOOGLE_URL = "https://pay.google.com/gp/v/save/tok"

    def setUp(self):
        self.club = Club.objects.create(
            name="Wallet Email Club", show_member_barcode=True, membership_annual_fee=Decimal(20)
        )
        self.member = ClubMember.objects.create(club=self.club, name="Wallet Member", email="wallet@example.com")

    def _send(self):
        from auctions.tasks import send_club_member_email

        with patch("auctions.templatetags.membership_tags.google_wallet_save_url", return_value=self.GOOGLE_URL):
            with patch("auctions.tasks.mail.send") as send:
                send_club_member_email(self.member, "Welcome", "You're in.", email_type="welcome")
        self.assertTrue(send.called)
        return send.call_args.kwargs["html_message"], send.call_args.kwargs["message"]

    def test_the_buttons_sit_directly_below_the_barcode(self):
        html, _ = self._send()
        barcode_at = html.index(self.member.barcode_image_link_png)
        google_at = html.index("Add to Google Wallet")
        apple_at = html.index("Add to Apple Wallet")
        self.assertLess(barcode_at, google_at)
        self.assertLess(google_at, apple_at)
        self.assertIn(self.GOOGLE_URL, html)
        apple_path = reverse(
            "club_member_apple_wallet_by_uuid", kwargs={"slug": self.club.slug, "uuid": self.member.uuid}
        )
        self.assertIn(apple_path, html)

    def test_the_plain_text_part_carries_both_links(self):
        _, text = self._send()
        self.assertIn(f"Add to Google Wallet: {self.GOOGLE_URL}", text)
        self.assertIn("Add to Apple Wallet: https://", text)

    def test_only_the_configured_wallets_appear(self):
        with override_settings(APPLE_WALLET_PASS_TYPE_IDENTIFIER=""):
            html, text = self._send()
        self.assertIn("Add to Google Wallet", html)
        self.assertNotIn("Add to Apple Wallet", html)
        self.assertNotIn("Add to Apple Wallet", text)

    def test_no_buttons_without_a_membership_card(self):
        self.club.show_member_barcode = False
        self.club.save()
        html, text = self._send()
        self.assertNotIn("Add to Google Wallet", html)
        self.assertNotIn("Add to Apple Wallet", html)
        self.assertNotIn("Add to Google Wallet", text)
        self.assertNotIn("Add to Apple Wallet", text)

    def test_wallet_links_helper_is_empty_when_nothing_is_configured(self):
        from auctions.tasks import wallet_links

        with override_settings(
            GOOGLE_WALLET_ISSUER_ID="",
            APPLE_WALLET_PASS_TYPE_IDENTIFIER="",
        ):
            self.assertEqual(wallet_links(self.member), ("", ""))

    def test_the_email_settings_preview_shows_the_buttons(self):
        editor = User.objects.create_user(username="wallet_editor", password="testpass", email="we@example.com")
        ClubMember.objects.create(club=self.club, user=editor, name="Wallet Editor", permission_edit_club=True)
        self.client.login(username="wallet_editor", password="testpass")
        response = self.client.get(reverse("club_email_settings", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add to Google Wallet")
        self.assertContains(response, "Add to Apple Wallet")


class ClubMemberRenewAPITests(TestCase):
    """The API-key renew action: find the member by email or create them, renew, and hand back the
    complete member record with the new expiration."""

    def setUp(self):
        self.owner = User.objects.create_user(username="renew_api_owner", password="testpass", email="ra@example.com")
        self.club = Club.objects.create(
            name="Renew API Club", membership_system="rolling", membership_annual_fee=Decimal(25)
        )
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Website",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.owner,
            can_add_club_members=True,
        )
        self.raw_key = raw_key
        self.url = reverse("api_club_member_renew", kwargs={"slug": self.club.slug})
        self.today = timezone.now().date()

    def _enable(self):
        self.api_key.can_renew_memberships = True
        self.api_key.save(update_fields=["can_renew_memberships"])

    def _post(self, payload):
        return self.client.post(self.url, payload, content_type="application/json", HTTP_X_API_KEY=self.raw_key)

    def test_it_requires_the_renew_permission(self):
        response = self._post({"email": "someone@example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ClubMember.objects.filter(club=self.club).exists())

    def test_it_requires_an_email_to_match_on(self):
        self._enable()
        response = self._post({"name": "No Email"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())

    def test_it_renews_an_existing_member_matched_by_email(self):
        self._enable()
        member = ClubMember.objects.create(
            club=self.club,
            name="Mike Smith",
            email="mike@example.com",
            membership_expiration_date=self.today - datetime.timedelta(days=10),
        )
        response = self._post({"email": "MIKE@example.com"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["created"])
        self.assertEqual(body["id"], member.pk)
        self.assertEqual(body["membership_expiration_date"], str(self.today + datetime.timedelta(days=365)))
        self.assertEqual(body["membership_last_paid"], str(self.today))
        self.assertEqual(body["name"], "Mike Smith")
        self.assertTrue(body["wallet_link"].endswith(member.member_page_url))
        member.refresh_from_db()
        self.assertTrue(member.is_paid_member)
        self.assertEqual(ClubMember.objects.filter(club=self.club).count(), 1)

    def test_it_creates_the_member_when_the_email_is_new(self):
        self._enable()
        response = self._post(
            {"email": "new@example.com", "first_name": "New", "last_name": "Member", "phone_number": "555-0143"}
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["created"])
        member = ClubMember.objects.get(club=self.club, email="new@example.com")
        self.assertEqual(member.name, "New Member")
        self.assertEqual(member.phone_number, "555-0143")
        self.assertEqual(member.source, "Website")
        self.assertEqual(member.membership_expiration_date, self.today + datetime.timedelta(days=365))
        self.assertEqual(body["membership_expiration_date"], str(member.membership_expiration_date))
        self.assertTrue(
            ClubHistory.objects.filter(club=self.club, action__contains="membership renewal").exists(),
            "creating a member through a renewal should be visible in club history",
        )

    def test_a_renewal_is_recorded_in_history_and_the_ledger(self):
        self._enable()
        ClubMember.objects.create(club=self.club, name="Ledger Member", email="ledger@example.com")
        self._post({"email": "ledger@example.com"})
        self.assertTrue(
            ClubHistory.objects.filter(
                club=self.club,
                action__contains=f"Renewed membership for Ledger Member via API key [{self.api_key.prefix}]",
            ).exists()
        )
        money = ClubMoney.objects.get(club=self.club, category=ClubMoney.CATEGORY_MEMBERSHIP)
        self.assertEqual(money.amount, Decimal(25))
        self.assertIn("Ledger Member", money.description)

    def test_blank_values_do_not_wipe_details_already_on_file(self):
        self._enable()
        member = ClubMember.objects.create(
            club=self.club,
            name="Keep Me",
            email="keep@example.com",
            phone_number="555-1111",
            address="1 Fish Lane",
        )
        response = self._post({"email": "keep@example.com", "name": "", "phone_number": "", "address": ""})
        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        self.assertEqual(member.name, "Keep Me")
        self.assertEqual(member.phone_number, "555-1111")
        self.assertEqual(member.address, "1 Fish Lane")

    def test_supplied_details_are_applied_before_the_renewal(self):
        self._enable()
        member = ClubMember.objects.create(club=self.club, name="Old Name", email="update@example.com")
        response = self._post({"email": "update@example.com", "name": "New Name", "phone_number": "555-2222"})
        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        self.assertEqual(member.name, "New Name")
        self.assertEqual(member.phone_number, "555-2222")
        self.assertEqual(response.json()["name"], "New Name")

    def test_it_honors_the_keys_field_mapping(self):
        self._enable()
        ClubAPIKeyFieldMap.objects.create(api_key=self.api_key, external_field="email_address", internal_field="email")
        response = self._post({"email_address": "mapped@example.com", "name": "Mapped Member"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ClubMember.objects.filter(club=self.club, email="mapped@example.com").exists())

    def test_a_january_first_club_lands_on_january_first(self):
        self._enable()
        self.club.membership_system = "january_first"
        self.club.save()
        ClubMember.objects.create(club=self.club, name="Jan Member", email="jan@example.com")
        response = self._post({"email": "jan@example.com"})
        self.assertEqual(response.json()["membership_expiration_date"], str(datetime.date(self.today.year + 1, 1, 1)))

    def test_deactivated_members_are_not_matched(self):
        self._enable()
        ClubMember.objects.create(club=self.club, name="Gone", email="gone@example.com", is_deleted=True)
        response = self._post({"email": "gone@example.com", "name": "Back Again"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ClubMember.objects.filter(club=self.club, email="gone@example.com").count(), 2)

    def test_the_renewal_confirmation_email_is_sent(self):
        self._enable()
        self.club.send_membership_renewal_confirmation = True
        self.club.save()
        ClubMember.objects.create(club=self.club, name="Confirm Member", email="confirm@example.com")
        with patch("auctions.tasks.mail.send") as send:
            self._post({"email": "confirm@example.com"})
        self.assertTrue(send.called)
        self.assertIn("renewed", send.call_args.kwargs["subject"])


class ClubManagedMergeKeepsMembershipDatesTests(TestCase):
    """Merging duplicate participants in a club-managed auction must not throw away the
    membership the surviving record is entitled to.

    The duplicate is frequently the row that was renewed, and dropping its dates left the member
    reading as unpaid everywhere (the member list, the wallet pass, the membership filters)."""

    def setUp(self):
        self.club = Club.objects.create(
            name="Merge Dates Club", membership_system="rolling", membership_annual_fee=Decimal(20)
        )
        self.auction = Auction.objects.create(
            created_by=User.objects.create_user(username="merge_owner", password="testpass", email="mo@example.com"),
            title="Club Managed Merge Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
            club=self.club,
            manage_users_through_club="all",
        )
        self.location = PickupLocation.objects.create(
            name="merge location", auction=self.auction, pickup_time=timezone.now() + datetime.timedelta(days=2)
        )
        self.today = timezone.now().date()

    def _shadow_tos(self, member):
        """The AuctionTOS the club-managed auction auto-creates for a new member."""
        return AuctionTOS.objects.get(auction=self.auction, clubmember=member)

    def test_the_later_membership_dates_survive_the_merge(self):
        keeper = ClubMember.objects.create(club=self.club, name="Keeper", email="keeper@example.com")
        renewed = ClubMember.objects.create(
            club=self.club,
            name="Renewed Duplicate",
            email="renewed@example.com",
            membership_last_paid=self.today,
            membership_expiration_date=self.today + datetime.timedelta(days=365),
        )
        keeper_tos = self._shadow_tos(keeper)
        duplicate_tos = self._shadow_tos(renewed)

        keeper_tos.merge_duplicate(duplicate_tos, reason="test merge")

        keeper.refresh_from_db()
        self.assertEqual(keeper.membership_expiration_date, self.today + datetime.timedelta(days=365))
        self.assertEqual(keeper.membership_last_paid, self.today)
        self.assertTrue(keeper.is_paid_member)

    def test_an_earlier_duplicate_does_not_shorten_the_membership(self):
        keeper = ClubMember.objects.create(
            club=self.club,
            name="Long Keeper",
            email="long@example.com",
            membership_expiration_date=self.today + datetime.timedelta(days=300),
            membership_last_paid=self.today,
        )
        stale = ClubMember.objects.create(
            club=self.club,
            name="Stale Duplicate",
            email="stale@example.com",
            membership_expiration_date=self.today - datetime.timedelta(days=100),
            membership_last_paid=self.today - datetime.timedelta(days=465),
        )
        keeper_tos = self._shadow_tos(keeper)
        duplicate_tos = self._shadow_tos(stale)

        keeper_tos.merge_duplicate(duplicate_tos, reason="test merge")

        keeper.refresh_from_db()
        self.assertEqual(keeper.membership_expiration_date, self.today + datetime.timedelta(days=300))
        self.assertEqual(keeper.membership_last_paid, self.today)


class CloseModalResponseEscapingTests(TestCase):
    """close_modal_response writes an inline <script>, so anything it interpolates must be inert.

    json.dumps alone is not enough: it leaves "<" untouched, so a member name containing
    "</script>" would end the tag early and run whatever followed as markup.
    """

    def test_a_toast_cannot_break_out_of_the_script_tag(self):
        from auctions.views import close_modal_response

        response = close_modal_response(toast="</script><img src=x onerror=alert(1)>")
        body = response.content.decode()
        self.assertNotIn("</script><img", body)
        self.assertNotIn("<img", body)
        self.assertEqual(body.count("</script>"), 2)

    def test_the_detail_payload_cannot_break_out_of_the_script_tag(self):
        from auctions.views import close_modal_response

        response = close_modal_response("reload-page", redirect_url="/x?a=</script><script>alert(1)</script>")
        body = response.content.decode()
        self.assertEqual(body.count("<script>"), 1)
        self.assertEqual(body.count("</script>"), 1)
        self.assertIn("\\u003C", body)

    def test_a_toast_is_html_escaped_for_the_toast_plugin(self):
        """The plugin concatenates the title into markup, so tags must arrive already escaped.

        The title is escaped twice over: HTML-escaped for the plugin, then JSON-escaped for the
        script tag, so assert on the title the browser hands the plugin rather than on the wire.
        """
        from auctions.views import close_modal_response

        response = close_modal_response(toast="<b>Bob</b> & Sons has no email address on file.")
        body = response.content.decode()
        self.assertNotIn("<b>", body)
        title = json.loads(body.split("toast(")[1].split(");")[0])["title"]
        self.assertEqual(title, "&lt;b&gt;Bob&lt;/b&gt; &amp; Sons has no email address on file.")

    def test_extra_triggers_still_ride_along_as_a_plain_json_header(self):
        from auctions.views import close_modal_response

        response = close_modal_response(None, extra_triggers={"clubMemberListChanged": None})
        self.assertEqual(response["HX-Trigger"], '{"clubMemberListChanged": null}')


class ClubMemberToastEscapingTests(TestCase):
    """The resend-card view puts a member-supplied name in a toast — the real path to the sink."""

    def setUp(self):
        self.club = Club.objects.create(name="Toast Escaping Club", show_member_barcode=True)
        self.admin = User.objects.create_user(username="toast_admin", password="testpass", email="ta@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, name="Toast Admin", permission_add_edit=True)
        self.member = ClubMember.objects.create(club=self.club, name="</script><img src=x onerror=alert(1)>", email="")

    def test_a_hostile_member_name_reaches_the_page_inert(self):
        self.client.login(username="toast_admin", password="testpass")
        response = self.client.post(reverse("club_member_resend_card", kwargs={"pk": self.member.pk}))
        body = response.content.decode()
        self.assertContains(response, "no email address on file")
        self.assertNotIn("<img", body)
        self.assertEqual(body.count("</script>"), 2)


class LotOwnershipWithUnlinkedTosTests(StandardTestCase):
    """A seller whose lot has user=None must still be able to manage that lot.

    Lots added through an auction copy their owner from AuctionTOS.user, which is null whenever
    the TOS wasn't attached to an account when the lot was saved (an admin-imported bidder list,
    or a record orphaned by the email-change guard in AuctionTOS.save()). Those lots showed up on
    the seller's invoice and selling dashboard but every edit was refused with "Only the lot
    creator can edit a lot".
    """

    def setUp(self):
        super().setUp()
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.seller = User.objects.create_user(
            username="unlinked_seller", password="testpassword", email="seller@example.com"
        )
        self.open_auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Open for lot submission",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=the_future,
            lot_submission_end_date=the_future,
            winning_bid_percent_to_club=25,
        )
        self.open_location = PickupLocation.objects.create(
            name="open location", auction=self.open_auction, pickup_time=the_future
        )
        # An admin-imported bidder: the email is the seller's, but nothing links it to their account
        self.unlinked_tos = AuctionTOS.objects.create(
            auction=self.open_auction,
            pickup_location=self.open_location,
            name="Unlinked Seller",
            email="seller@example.com",
            bidder_number="601",
            manually_added=True,
        )
        AuctionTOS.objects.filter(pk=self.unlinked_tos.pk).update(user=None)
        self.unlinked_tos.refresh_from_db()
        self.orphaned_lot = Lot.objects.create(
            lot_name="Subulina octona",
            auction=self.open_auction,
            auctiontos_seller=self.unlinked_tos,
            quantity=1,
        )

    def edit_url(self):
        return reverse("edit_lot", kwargs={"pk": self.orphaned_lot.pk})

    def test_is_owned_by_matches_on_tos_email(self):
        assert self.orphaned_lot.user is None
        assert self.orphaned_lot.is_owned_by(self.seller) is True
        assert self.orphaned_lot.is_owned_by(self.user_who_does_not_join) is False

    def test_is_owned_by_matches_on_tos_user(self):
        AuctionTOS.objects.filter(pk=self.unlinked_tos.pk).update(user=self.user_who_does_not_join, email="")
        self.orphaned_lot.refresh_from_db()
        assert self.orphaned_lot.is_owned_by(self.user_who_does_not_join) is True
        assert self.orphaned_lot.is_owned_by(self.seller) is False

    def test_is_owned_by_ignores_lots_with_no_seller(self):
        lot = Lot.objects.create(lot_name="No seller", quantity=1)
        assert lot.is_owned_by(self.seller) is False

    def test_seller_can_open_the_edit_page(self):
        # LotValidation redirects anyone without contact info, ownership aside
        self.seller.first_name = "Un"
        self.seller.last_name = "Linked"
        self.seller.save()
        self.seller.userdata.address = "123 test street"
        self.seller.userdata.save()
        self.client.login(username="unlinked_seller", password="testpassword")
        response = self.client.get(self.edit_url())
        assert response.status_code == 200

    def test_other_users_still_cannot_edit(self):
        self.client.login(username="no_joins", password="testpassword")
        response = self.client.get(self.edit_url(), follow=True)
        assert "Only the lot creator can edit a lot" in response.content.decode()

    def test_seller_can_delete(self):
        self.client.login(username="unlinked_seller", password="testpassword")
        response = self.client.post(reverse("delete_lot", kwargs={"pk": self.orphaned_lot.pk}), follow=True)
        assert response.status_code == 200
        self.orphaned_lot.refresh_from_db()
        assert self.orphaned_lot.is_deleted is True

    def test_seller_can_manage_images(self):
        assert self.orphaned_lot.image_permission_check(self.seller) is True
        assert self.orphaned_lot.image_permission_check(self.user_who_does_not_join) is False

    def test_new_lots_record_the_seller_even_when_the_tos_is_unlinked(self):
        lot = save_new_lot(
            Lot(lot_name="Added by the seller", quantity=1),
            auction=self.open_auction,
            tos=self.unlinked_tos,
            added_by=self.seller,
        )
        assert lot.user == self.seller

    def test_an_admin_adding_lots_for_someone_else_is_not_recorded_as_the_owner(self):
        lot = save_new_lot(
            Lot(lot_name="Added by an admin", quantity=1),
            auction=self.open_auction,
            tos=self.unlinked_tos,
            added_by=self.admin_user,
        )
        assert lot.user is None
        assert lot.added_by == self.admin_user

    def test_backfill_command_repairs_stored_lot_user(self):
        call_command("backfill_lot_users")
        self.orphaned_lot.refresh_from_db()
        assert self.orphaned_lot.user == self.seller

    def test_backfill_command_dry_run_changes_nothing(self):
        call_command("backfill_lot_users", "--dry-run")
        self.orphaned_lot.refresh_from_db()
        assert self.orphaned_lot.user is None

    def test_backfill_command_uses_a_linked_tos_before_email(self):
        AuctionTOS.objects.filter(pk=self.unlinked_tos.pk).update(user=self.user_who_does_not_join)
        call_command("backfill_lot_users")
        self.orphaned_lot.refresh_from_db()
        assert self.orphaned_lot.user == self.user_who_does_not_join

    def test_logging_in_links_the_tos_and_claims_its_lots(self):
        AuctionTOS.objects.filter(pk=self.unlinked_tos.pk).update(manually_added=False)
        self.client.login(username="unlinked_seller", password="testpassword")
        self.unlinked_tos.refresh_from_db()
        self.orphaned_lot.refresh_from_db()
        assert self.unlinked_tos.user == self.seller
        assert self.orphaned_lot.user == self.seller


class ParticipantDropdownMirrorsTheClubPageTests(StandardTestCase):
    """Managing a member from the auction is supposed to be the same job as from /clubs/x/admin/.

    It was not: the participant row's Actions menu offered Renew, Set expiration date and
    Membership number, and stopped there -- so an admin working the users page for a club-managed
    auction could not resend somebody's card or deactivate them without going to find the club.
    """

    def setUp(self):
        super().setUp()
        from auctions.views.base import _upsert_clubmember_shadow_tos

        self.club = Club.objects.create(name="Dropdown Club", active=True, show_member_barcode=True)
        self.in_person_auction.club = self.club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()
        self.member = ClubMember.objects.create(
            club=self.club, name="Dropdown Dave", email="dave@example.com", bidder_number="81"
        )
        self.tos = _upsert_clubmember_shadow_tos(self.in_person_auction, self.member)

    def test_the_card_can_be_resent_from_the_auction(self):
        html = self.tos.actions_dropdown_html
        self.assertIn(
            reverse("club_member_confirm", kwargs={"pk": self.member.pk, "action": "resend_card"}),
            html,
        )
        self.assertIn("Resend membership card", html)

    def test_a_member_can_be_deactivated_from_the_auction(self):
        html = self.tos.actions_dropdown_html
        self.assertIn(reverse("club_member_confirm", kwargs={"pk": self.member.pk, "action": "delete"}), html)
        self.assertIn("Deactivate club member", html)

    def test_a_deactivated_member_is_offered_reactivation_instead(self):
        self.member.is_deleted = True
        self.member.save()
        html = self.tos.actions_dropdown_html
        self.assertIn(reverse("club_member_reactivate", kwargs={"pk": self.member.pk}), html)
        self.assertNotIn("Deactivate club member", html)
        # No card to resend for somebody who is not a member; the confirm view 404s on it too.
        self.assertNotIn("Resend membership card", html)

    def test_a_club_with_no_cards_is_offered_neither_card_action(self):
        self.club.show_member_barcode = False
        self.club.save()
        self.tos.refresh_from_db()
        html = self.tos.actions_dropdown_html
        self.assertNotIn("Resend membership card", html)
        self.assertNotIn("Membership number", html)
        # Deactivate has nothing to do with cards and stays.
        self.assertIn("Deactivate club member", html)

    def test_an_auction_with_no_club_gets_none_of_it(self):
        self.in_person_auction.manage_users_through_club = ""
        self.in_person_auction.club = None
        self.in_person_auction.save()
        tos = AuctionTOS.objects.get(pk=self.tos.pk)
        html = tos.actions_dropdown_html
        self.assertNotIn("Resend membership card", html)
        self.assertNotIn("Deactivate club member", html)
