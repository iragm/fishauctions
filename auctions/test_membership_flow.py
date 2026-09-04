"""Club membership as money: invoices, discounts, renewals and the confirmation emails."""

import datetime
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    AuctionEditForm,
    ClubMembershipSettingsForm,
)
from auctions.models import (
    Club,
    ClubHistory,
    ClubMember,
    ClubMoney,
    Invoice,
    InvoicePayment,
    Lot,
    PayPalSeller,
)
from auctions.tests import StandardTestCase, patch_views


class InvoiceStatusButtonTests(StandardTestCase):
    """Test invoice status buttons can be clicked and update correctly"""

    def test_invoice_status_button_paid(self):
        """Admin can mark invoice as paid via button click"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = f"/api/payinvoice/{self.invoice.pk}/PAID"
        response = self.client.post(url)
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}, content: {response.content.decode()[:500]}"
        )
        # Verify the invoice was updated
        self.invoice.refresh_from_db()
        assert self.invoice.status == "PAID"
        # Verify response contains updated buttons with correct ID and status
        content = response.content.decode()
        assert f"id='invoice-buttons-{self.invoice.pk}'" in content, (
            f"Expected invoice-buttons ID in content: {content}"
        )
        assert f'id="{self.invoice.pk}_PAID"' in content
        assert "btn-success" in content  # Paid button should be success

    def test_invoice_status_button_draft(self):
        """Admin can mark invoice as draft (open) via button click"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        # First set to PAID
        self.invoice.status = "PAID"
        self.invoice.save()
        # Then change back to DRAFT
        url = f"/api/payinvoice/{self.invoice.pk}/DRAFT"
        response = self.client.post(url)
        assert response.status_code == 200
        self.invoice.refresh_from_db()
        assert self.invoice.status == "DRAFT"
        content = response.content.decode()
        assert f"id='invoice-buttons-{self.invoice.pk}'" in content
        assert "btn-primary active" in content  # the selected option is primary, the others secondary

    def test_invoice_status_button_anonymous_denied(self):
        """Anonymous users cannot change invoice status via the pk-based endpoint"""
        url = f"/api/payinvoice/{self.invoice.pk}/PAID"
        response = self.client.post(url)
        # DRF returns 401 for unauthenticated requests (TokenAuthentication is first)
        assert response.status_code == 401

    def test_invoice_status_button_non_admin_denied(self):
        """Non-admin users cannot change invoice status for an auction they don't administer"""
        # self.user_with_no_lots has a TOS for online_auction but is not an admin
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = f"/api/payinvoice/{self.invoice.pk}/PAID"
        response = self.client.post(url)
        assert response.status_code == 403
        # Invoice status should be unchanged
        self.invoice.refresh_from_db()
        assert self.invoice.status != "PAID"

    def test_invoice_status_button_auction_creator_allowed(self):
        """Auction creator can change invoice status"""
        # self.user is the creator of self.online_auction
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/api/payinvoice/{self.invoice.pk}/PAID"
        response = self.client.post(url)
        assert response.status_code == 200
        self.invoice.refresh_from_db()
        assert self.invoice.status == "PAID"

    def test_invoice_status_button_uuid_denied(self):
        """The invoice no-login UUID (emailed to the bidder) must NOT allow a status change.

        Regression test for the self-payment vulnerability: a bidder holding their invoice's
        no-login link could otherwise POST /api/payinvoice/<uuid>/PAID to mark their own
        invoice paid, which books club-ledger (ClubMoney) entries as if cash was received.
        """
        clubmoney_before = ClubMoney.objects.filter(invoice=self.invoice).count()
        url = f"/api/payinvoice/{self.invoice.no_login_link}/PAID"
        response = self.client.post(url)
        # No no-login/status-change route exists any more -> the UUID cannot resolve to the pk
        # endpoint, so this is rejected (404). It must never succeed.
        assert response.status_code in (401, 403, 404)
        self.invoice.refresh_from_db()
        assert self.invoice.status != "PAID"
        # No club-ledger entries should have been booked for this invoice.
        assert ClubMoney.objects.filter(invoice=self.invoice).count() == clubmoney_before

    def test_invoice_status_button_uuid_wrong_uuid_denied(self):
        """A bogus UUID returns 404"""
        import uuid  # noqa: PLC0415

        url = f"/api/payinvoice/{uuid.uuid4()}/PAID"
        response = self.client.post(url)
        assert response.status_code == 404

    def test_invoice_status_button_invalid_status_rejected(self):
        """An out-of-choices status string is rejected (404) and never written to the invoice."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        original_status = self.invoice.status
        url = f"/api/payinvoice/{self.invoice.pk}/BANANA"
        response = self.client.post(url)
        assert response.status_code == 404
        self.invoice.refresh_from_db()
        assert self.invoice.status == original_status
        assert self.invoice.status in ("DRAFT", "UNPAID", "PAID")

    def test_invoice_status_button_non_admin_owner_denied(self):
        """A non-admin who OWNS the invoice cannot change its status via pk or the emailed UUID.

        self.invoiceB belongs to self.tosB (user=self.userB), who is a bidder in the auction
        but not an admin. Neither the pk endpoint nor the no-login UUID may let them self-pay,
        and no ClubMoney ledger entry may be booked.
        """
        self.client.login(username=self.userB.username, password="testpassword")
        clubmoney_before = ClubMoney.objects.filter(invoice=self.invoiceB).count()
        # Authenticated non-admin owner via the pk endpoint -> forbidden.
        response = self.client.post(f"/api/payinvoice/{self.invoiceB.pk}/PAID")
        assert response.status_code == 403
        # Same owner via the emailed no-login UUID -> also rejected.
        response = self.client.post(f"/api/payinvoice/{self.invoiceB.no_login_link}/PAID")
        assert response.status_code in (401, 403, 404)
        self.invoiceB.refresh_from_db()
        assert self.invoiceB.status != "PAID"
        assert ClubMoney.objects.filter(invoice=self.invoiceB).count() == clubmoney_before

    def test_invoice_status_button_admin_can_mark_paid_and_unpaid(self):
        """An auction admin can still mark an invoice paid and back to unpaid via the pk path."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")
        assert response.status_code == 200
        self.invoice.refresh_from_db()
        assert self.invoice.status == "PAID"
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/UNPAID")
        assert response.status_code == 200
        self.invoice.refresh_from_db()
        assert self.invoice.status == "UNPAID"

    def test_invoice_no_login_uuid_view_still_works(self):
        """The emailed UUID link must still let the recipient VIEW their invoice (view-only route)."""
        url = reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link})
        response = self.client.get(url)
        assert response.status_code == 200


class ClubMembershipRenewalFlowTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Renewal Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            send_membership_expiration_reminders=True,
        )
        self.payment_user = User.objects.create_user(
            username="renewal_payment_user",
            password="testpass",
            email="renewal_payment_user@example.com",
        )
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_renewal")
        self.online_auction.club = self.club
        self.online_auction.add_membership_fee_to_invoices_for_expired_members = True
        self.online_auction.save()
        self.member = ClubMember.objects.create(
            club=self.club,
            user=self.online_tos.user,
            name="Renew Me",
            email=self.online_tos.email,
            membership_last_paid=timezone.now().date() - datetime.timedelta(days=370),
        )
        self.invoice.refresh_from_db()

    def test_membership_reminder_due_updates_when_membership_changes(self):
        self.member.membership_last_paid = timezone.now().date()
        self.member.save()
        self.member.refresh_from_db()
        self.assertIsNotNone(self.member.membership_expiration_reminder_due)

    def test_membership_reminder_due_not_set_for_free_membership(self):
        self.club.membership_annual_fee = None
        self.club.save(update_fields=["membership_annual_fee"])
        self.member.membership_last_paid = timezone.now().date()
        self.member.save()
        self.member.refresh_from_db()
        self.assertIsNone(self.member.membership_expiration_reminder_due)

    def test_invoice_membership_fee_applies_when_renewal_needed(self):
        self.invoice.renewal_needed = True
        self.invoice.save(update_fields=["renewal_needed"])
        self.assertEqual(self.invoice.membership_fee_amount, Decimal("25.00"))

    def test_invoice_renewal_toggle_requires_admin(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(
            reverse("invoice_renewal_toggle", kwargs={"pk": self.invoice.pk}),
            {"renewal_needed": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_invoice_renewal_toggle_updates(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(
            reverse("invoice_renewal_toggle", kwargs={"pk": self.invoice.pk}),
            {"renewal_needed": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.renewal_needed)

    def test_marking_invoice_paid_processes_membership_renewal(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        self.invoice.renewal_needed = True
        self.invoice.status = "UNPAID"
        self.invoice.save(update_fields=["renewal_needed", "status"])
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")
        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.member.refresh_from_db()
        self.assertTrue(self.invoice.renewal_processed)
        self.assertGreaterEqual(self.member.membership_last_paid, timezone.now().date())
        self.assertTrue(InvoicePayment.objects.filter(club_member=self.member, payment_target="CLUB_MEMBER").exists())

    @patch_views("maybe_send_membership_renewal_confirmation")
    def test_marking_invoice_paid_sends_membership_renewal_confirmation(self, mock_send):
        self.club.send_membership_renewal_confirmation = True
        self.club.save(update_fields=["send_membership_renewal_confirmation"])
        self.client.login(username=self.admin_user.username, password="testpassword")
        self.invoice.renewal_needed = True
        self.invoice.status = "UNPAID"
        self.invoice.save(update_fields=["renewal_needed", "status"])

        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")

        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()

    def test_invoice_membership_block_hidden_for_free_membership(self):
        self.club.membership_annual_fee = None
        self.club.save(update_fields=["membership_annual_fee"])
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Apply Renewal Club membership fee")


class PayPalSubscriptionWebhookTests(StandardTestCase):
    """The club membership subscription webhook (PayPalSubscriptionWebhookView) and its apply logic."""

    def setUp(self):
        super().setUp()
        # Own-credentials (non-OAuth) club, so supports_paypal_subscriptions is True and its webhook
        # can be identified/verified.
        self.club = Club.objects.create(
            name="Subscription Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            send_membership_renewal_confirmation=True,
            allow_non_oauth_paypal=True,
            paypal_client_id="club-client-id",
            paypal_secret="club-secret",
            paypal_webhook_id="WH-CLUB-1",
        )
        self.webhook_url = reverse("club_paypal_subscription_webhook")

    def _active_subscription(
        self, sub_id="I-SUB1", email="subscriber@example.com", next_days=365, last_payment="25.00", paid_days_ago=0
    ):
        next_time = (timezone.now() + datetime.timedelta(days=next_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        billing_info = {"next_billing_time": next_time}
        if last_payment is not None:
            paid_time = timezone.now() - datetime.timedelta(days=paid_days_ago)
            billing_info["last_payment"] = {
                "amount": {"currency_code": "USD", "value": last_payment},
                "time": paid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        return {
            "id": sub_id,
            "status": "ACTIVE",
            "subscriber": {"email_address": email},
            "billing_info": billing_info,
        }

    def _membership_money(self):
        return ClubMoney.objects.filter(club=self.club, category=ClubMoney.CATEGORY_MEMBERSHIP)

    def _headers(self):
        return {
            "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
            "HTTP_PAYPAL_CERT_URL": "https://api.paypal.com/cert",
            "HTTP_PAYPAL_TRANSMISSION_ID": "tid",
            "HTTP_PAYPAL_TRANSMISSION_SIG": "sig",
            "HTTP_PAYPAL_TRANSMISSION_TIME": "2026-07-24T00:00:00Z",
        }

    def _post_event(self, event, headers=True):
        extra = self._headers() if headers else {}
        return self.client.post(self.webhook_url, data=json.dumps(event), content_type="application/json", **extra)

    # --- Club.supports_paypal_subscriptions gating ---

    def test_supports_paypal_subscriptions(self):
        self.assertTrue(self.club.supports_paypal_subscriptions)
        oauth_user = User.objects.create_user(username="oauth_sub_user", password="x", email="oauth_sub@example.com")
        oauth_club = Club.objects.create(name="OAuth Club", membership_system="rolling")
        PayPalSeller.objects.create(user=oauth_user, club=oauth_club, paypal_merchant_id="merchant_x")
        self.assertFalse(oauth_club.supports_paypal_subscriptions)

    def test_form_hides_webhook_field_when_unsupported(self):
        oauth_club = Club.objects.create(name="OAuth Club 2", membership_system="rolling")
        form = ClubMembershipSettingsForm(instance=oauth_club, show_paypal_subscriptions=False)
        self.assertNotIn("paypal_webhook_id", form.fields)

    def test_form_shows_webhook_field_when_supported(self):
        form = ClubMembershipSettingsForm(instance=self.club, show_paypal_subscriptions=True)
        self.assertIn("paypal_webhook_id", form.fields)

    def test_hidden_field_does_not_blank_saved_webhook_id(self):
        # A club that loses PayPal eligibility must not have its saved webhook id wiped on save.
        form = ClubMembershipSettingsForm(
            instance=self.club,
            data={"membership_system": "rolling", "membership_annual_fee": "25.00"},
            show_paypal_subscriptions=False,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.club.refresh_from_db()
        self.assertEqual(self.club.paypal_webhook_id, "WH-CLUB-1")

    # --- _subscription_id_for_event ---

    def test_subscription_id_for_event(self):
        from auctions.views import PayPalSubscriptionWebhookView

        view = PayPalSubscriptionWebhookView()
        self.assertEqual(view._subscription_id_for_event("BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-1"}), "I-1")
        self.assertEqual(
            view._subscription_id_for_event("PAYMENT.SALE.COMPLETED", {"billing_agreement_id": "I-2"}), "I-2"
        )
        # A one-off sale carries no billing_agreement_id, and CREATED (approval-pending) is unhandled.
        self.assertEqual(view._subscription_id_for_event("PAYMENT.SALE.COMPLETED", {"id": "PAY-9"}), "")
        self.assertEqual(view._subscription_id_for_event("BILLING.SUBSCRIPTION.CREATED", {"id": "I-3"}), "")

    # --- _apply_paypal_subscription_event (no network) ---

    def test_active_subscription_links_member_and_extends(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        member = ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation") as mock_email:
            _apply_paypal_subscription_event(self.club, self._active_subscription())
        member.refresh_from_db()
        self.assertEqual(member.paypal_subscription_id, "I-SUB1")
        self.assertEqual(member.membership_expiration_date, (timezone.now() + datetime.timedelta(days=365)).date())
        mock_email.assert_called_once()

    def test_active_subscription_creates_member_when_none(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(email="new@example.com"))
        member = ClubMember.objects.get(club=self.club, paypal_subscription_id="I-SUB1")
        self.assertEqual(member.email, "new@example.com")

    def test_active_subscription_books_club_money(self):
        # A subscription renewal is cash into the club, exactly like the manual renewal button.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        member = ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment="25.00"))
        entry = self._membership_money().get()
        self.assertEqual(entry.amount, Decimal("25.00"))
        self.assertEqual(entry.date, timezone.now().date())
        self.assertIn("I-SUB1", entry.description)
        self.assertIn(str(member), entry.description)
        self.assertIsNone(entry.created_by)  # a webhook has no acting user

    def test_books_amount_paypal_actually_charged_not_club_fee(self):
        # The club's list price is 25.00, but this subscriber is grandfathered at 18.50.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment="18.50"))
        self.assertEqual(self._membership_money().get().amount, Decimal("18.50"))

    def test_duplicate_delivery_books_club_money_once(self):
        # PayPal retries and sends several events per cycle; the ledger must not double-count.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        sub = self._active_subscription()
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, sub)
            _apply_paypal_subscription_event(self.club, sub)
            _apply_paypal_subscription_event(self.club, sub)
        self.assertEqual(self._membership_money().count(), 1)

    def test_each_cycle_books_its_own_payment(self):
        # A genuine second charge (later date) is a separate ledger row.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=365, paid_days_ago=365))
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=730, paid_days_ago=0))
        self.assertEqual(self._membership_money().count(), 2)
        self.assertEqual(sum(e.amount for e in self._membership_money()), Decimal("50.00"))

    def test_billing_date_advance_without_new_payment_books_nothing_extra(self):
        # BILLING.SUBSCRIPTION.UPDATED can push next_billing_time with no new charge -- booking is
        # keyed on the payment, not on the membership advancing, so this must not invent revenue.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=365))
            # Same last_payment, later billing date.
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=730))
        self.assertEqual(self._membership_money().count(), 1)

    def test_payment_booked_even_when_dates_did_not_move(self):
        # ACTIVATED can land before the first charge posts; the follow-up PAYMENT.SALE.COMPLETED
        # doesn't advance any date, but its money still has to reach the ledger.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment=None))
            self.assertEqual(self._membership_money().count(), 0)
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment="25.00"))
        self.assertEqual(self._membership_money().get().amount, Decimal("25.00"))

    def test_junk_or_zero_payment_amount_books_nothing(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment="not-a-number"))
            _apply_paypal_subscription_event(self.club, self._active_subscription(sub_id="I-SUB2", last_payment="0.00"))
        self.assertEqual(self._membership_money().count(), 0)

    def test_cancelled_subscription_books_nothing(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        member = ClubMember.objects.create(
            club=self.club, name="Sub Member", email="subscriber@example.com", paypal_subscription_id="I-SUB1"
        )
        sub = self._active_subscription()
        sub["status"] = "CANCELLED"
        _apply_paypal_subscription_event(self.club, sub)
        self.assertEqual(self._membership_money().count(), 0)
        member.refresh_from_db()
        self.assertEqual(member.paypal_subscription_id, "")

    def test_duplicate_active_event_does_not_resend_email(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        sub = self._active_subscription()
        with patch_views("maybe_send_membership_renewal_confirmation") as mock_email:
            _apply_paypal_subscription_event(self.club, sub)
            _apply_paypal_subscription_event(self.club, sub)  # same cycle -> no change, no second email
        self.assertEqual(mock_email.call_count, 1)

    def test_renewal_advances_expiration_and_emails_again(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        member = ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation") as mock_email:
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=365))
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=730))
        self.assertEqual(mock_email.call_count, 2)
        member.refresh_from_db()
        self.assertEqual(member.membership_expiration_date, (timezone.now() + datetime.timedelta(days=730)).date())

    # --- ClubHistory: a subscription renewal has to leave the same trail a manual one does ---

    def test_subscription_renewal_writes_club_history(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=365))
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").get()
        self.assertIn("renewed via PayPal subscription", history.action)
        self.assertIsNone(history.user)  # a webhook has no acting user
        self.assertNotIn("I-SUB1", history.action)  # the id is masked, as it is in the logs

    def test_duplicate_subscription_event_writes_history_once(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        sub = self._active_subscription()
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, sub)
            _apply_paypal_subscription_event(self.club, sub)
        self.assertEqual(ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").count(), 1)

    def test_subscription_created_member_writes_club_history(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(email="new@example.com"))
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERS").get()
        self.assertIn("from PayPal subscription", history.action)

    def test_cancelled_subscription_writes_club_history(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(
            club=self.club, name="Sub Member", email="subscriber@example.com", paypal_subscription_id="I-SUB1"
        )
        sub = self._active_subscription()
        sub["status"] = "CANCELLED"
        _apply_paypal_subscription_event(self.club, sub)
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").get()
        self.assertIn("stopped auto-renewing", history.action)
        self.assertIn("paid-through date unchanged", history.action)

    def test_cancelled_clears_subscription_but_keeps_expiration(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        expiry = (timezone.now() + datetime.timedelta(days=100)).date()
        member = ClubMember.objects.create(
            club=self.club,
            name="Sub Member",
            email="subscriber@example.com",
            paypal_subscription_id="I-SUB1",
            membership_expiration_date=expiry,
        )
        cancelled = {
            "id": "I-SUB1",
            "status": "CANCELLED",
            "subscriber": {"email_address": "subscriber@example.com"},
        }
        _apply_paypal_subscription_event(self.club, cancelled)
        member.refresh_from_db()
        self.assertEqual(member.paypal_subscription_id, "")
        self.assertEqual(member.membership_expiration_date, expiry)

    def test_approval_pending_does_not_grant_membership(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        pending = {
            "id": "I-SUB1",
            "status": "APPROVAL_PENDING",
            "subscriber": {"email_address": "pending@example.com"},
        }
        with patch_views("maybe_send_membership_renewal_confirmation") as mock_email:
            _apply_paypal_subscription_event(self.club, pending)
        self.assertFalse(ClubMember.objects.filter(club=self.club, email__iexact="pending@example.com").exists())
        mock_email.assert_not_called()

    # --- post() integration ---

    def test_renewal_payment_extends_membership(self):
        from auctions.views import PayPalSubscriptionWebhookView

        member = ClubMember.objects.create(
            club=self.club,
            name="Sub Member",
            email="subscriber@example.com",
            paypal_subscription_id="I-SUB1",
            membership_expiration_date=(timezone.now() + datetime.timedelta(days=5)).date(),
        )
        event = {"event_type": "PAYMENT.SALE.COMPLETED", "resource": {"billing_agreement_id": "I-SUB1"}}
        with (
            patch.object(PayPalSubscriptionWebhookView, "_identify_and_verify_club", return_value=self.club),
            patch.object(PayPalSubscriptionWebhookView, "get_from_paypal", return_value=self._active_subscription()),
            patch_views("maybe_send_membership_renewal_confirmation"),
        ):
            response = self._post_event(event)
        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        self.assertEqual(member.membership_expiration_date, (timezone.now() + datetime.timedelta(days=365)).date())

    def test_unhandled_event_ignored_without_verification(self):
        from auctions.views import PayPalSubscriptionWebhookView

        with patch.object(PayPalSubscriptionWebhookView, "_identify_and_verify_club") as mock_verify:
            response = self._post_event({"event_type": "BILLING.SUBSCRIPTION.CREATED", "resource": {"id": "I-SUB1"}})
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ignored"})
        mock_verify.assert_not_called()

    def test_handled_event_without_subscription_id_ignored(self):
        # A handled lifecycle event whose resource has no id must be ignored outright -- never
        # verified, never applied.
        from auctions.views import PayPalSubscriptionWebhookView

        with patch.object(PayPalSubscriptionWebhookView, "_identify_and_verify_club") as mock_verify:
            missing = self._post_event({"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {}})
            empty = self._post_event({"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {"id": ""}})
            no_resource = self._post_event({"event_type": "BILLING.SUBSCRIPTION.ACTIVATED"})
        for response in (missing, empty, no_resource):
            self.assertEqual(response.status_code, 200)
            self.assertJSONEqual(response.content, {"status": "ignored"})
        mock_verify.assert_not_called()

    def test_sale_without_billing_agreement_id_ignored(self):
        # A one-off (non-subscription) PAYMENT.SALE.COMPLETED carries no billing_agreement_id and
        # must be ignored, not verified.
        from auctions.views import PayPalSubscriptionWebhookView

        with patch.object(PayPalSubscriptionWebhookView, "_identify_and_verify_club") as mock_verify:
            response = self._post_event({"event_type": "PAYMENT.SALE.COMPLETED", "resource": {"id": "PAY-9"}})
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ignored"})
        mock_verify.assert_not_called()

    def test_missing_headers_rejected(self):
        response = self._post_event(
            {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {"id": "I-SUB1"}}, headers=False
        )
        self.assertEqual(response.status_code, 400)

    def test_unverified_club_rejected(self):
        from auctions.views import PayPalSubscriptionWebhookView

        with patch.object(PayPalSubscriptionWebhookView, "_identify_and_verify_club", return_value=None):
            response = self._post_event({"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {"id": "I-SUB1"}})
        self.assertEqual(response.status_code, 400)

    def test_non_dict_body_rejected(self):
        response = self.client.post(
            self.webhook_url, data=json.dumps([1, 2, 3]), content_type="application/json", **self._headers()
        )
        self.assertEqual(response.status_code, 400)

    def test_event_only_matches_verifying_club(self):
        from auctions.views import PayPalSubscriptionWebhookView

        # A second webhook-configured club must not receive another club's subscriber.
        other = Club.objects.create(
            name="Other Sub Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("10.00"),
            allow_non_oauth_paypal=True,
            paypal_client_id="o",
            paypal_secret="o",
            paypal_webhook_id="WH-OTHER",
        )
        event = {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {"id": "I-NEW"}}
        with (
            patch.object(
                PayPalSubscriptionWebhookView,
                "_verify_for_club",
                side_effect=lambda club, headers, evt: club.pk == self.club.pk,
            ),
            patch.object(
                PayPalSubscriptionWebhookView,
                "get_from_paypal",
                return_value=self._active_subscription(sub_id="I-NEW", email="x@example.com"),
            ),
            patch_views("maybe_send_membership_renewal_confirmation"),
        ):
            response = self._post_event(event)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ClubMember.objects.filter(club=self.club, paypal_subscription_id="I-NEW").exists())
        self.assertFalse(ClubMember.objects.filter(club=other, paypal_subscription_id="I-NEW").exists())

    # --- renewal email manage link ---

    def test_renewal_email_includes_paypal_manage_link(self):
        from auctions.tasks import maybe_send_membership_renewal_confirmation

        member = ClubMember.objects.create(
            club=self.club,
            name="Sub Member",
            email="subscriber@example.com",
            paypal_subscription_id="I-SUB1",
            membership_expiration_date=(timezone.now() + datetime.timedelta(days=365)).date(),
        )
        with patch("auctions.tasks.send_club_member_email") as mock_send:
            maybe_send_membership_renewal_confirmation(member)
        mock_send.assert_called_once()
        self.assertIn("paypal.com/myaccount/autopay", mock_send.call_args.kwargs["message_text"])

    def test_renewal_email_omits_manage_link_without_subscription(self):
        from auctions.tasks import maybe_send_membership_renewal_confirmation

        member = ClubMember.objects.create(
            club=self.club,
            name="Manual Member",
            email="manual@example.com",
            membership_expiration_date=(timezone.now() + datetime.timedelta(days=365)).date(),
        )
        with patch("auctions.tasks.send_club_member_email") as mock_send:
            maybe_send_membership_renewal_confirmation(member)
        self.assertNotIn("autopay", mock_send.call_args.kwargs["message_text"])


class ClubMemberDiscountTests(StandardTestCase):
    """Tests for Auction.club_member_discount and Auction.alternate_split_mode.

    In this class, self.invoiceB belongs to tosB/userB who bought 3 lots at $10 each
    (its four adjustments cancel each other out), and self.invoice belongs to
    online_tos/self.user who sold those 3 lots plus one unsold lot.
    """

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Discount Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
        )
        self.online_auction.club = self.club
        self.online_auction.club_member_discount = 5
        self.online_auction.tax = 0
        self.online_auction.invoice_rounding = False
        self.online_auction.save()
        self.invoice.refresh_from_db()
        self.invoiceB.refresh_from_db()

    def _make_member(self, user, paid=True):
        days = 100 if paid else -100
        return ClubMember.objects.create(
            club=self.club,
            user=user,
            name=user.username,
            membership_last_paid=timezone.now().date() - datetime.timedelta(days=265),
            membership_expiration_date=timezone.now().date() + datetime.timedelta(days=days),
        )

    def test_no_discount_for_non_member(self):
        self.assertEqual(self.invoiceB.club_member_discount, 0)
        self.assertEqual(self.invoiceB.net, Decimal("-30.00"))

    def test_discount_applies_for_paid_member(self):
        self._make_member(self.userB)
        self.assertTrue(self.invoiceB.treat_as_club_member)
        self.assertEqual(self.invoiceB.club_member_discount, 5)
        self.assertEqual(self.invoiceB.net, Decimal("-25.00"))

    def test_no_discount_for_expired_member(self):
        self._make_member(self.userB, paid=False)
        self.assertFalse(self.invoiceB.treat_as_club_member)
        self.assertEqual(self.invoiceB.club_member_discount, 0)

    def test_no_discount_when_no_lots_bought(self):
        self._make_member(self.user)
        self.assertTrue(self.invoice.treat_as_club_member)
        self.assertEqual(self.invoice.club_member_discount, 0)

    def test_checking_renewal_applies_discount_for_expired_member(self):
        """An unpaid member's invoice shows club member pricing when the renewal box is checked"""
        self._make_member(self.userB, paid=False)
        self.invoiceB.renewal_needed = True
        self.invoiceB.save(update_fields=["renewal_needed"])
        self.assertTrue(self.invoiceB.treat_as_club_member)
        self.assertEqual(self.invoiceB.club_member_discount, 5)
        # 30 for lots bought, less the 5 discount, plus the 25 membership fee
        self.assertEqual(self.invoiceB.net, Decimal("-50.00"))

    def test_renewal_toggle_only_adds_fee_for_active_member(self):
        """An active member gets the discount either way; checking the box only adds the fee"""
        self._make_member(self.userB)
        self.assertEqual(self.invoiceB.net, Decimal("-25.00"))
        self.invoiceB.renewal_needed = True
        self.invoiceB.save(update_fields=["renewal_needed"])
        self.assertEqual(self.invoiceB.club_member_discount, 5)
        self.assertEqual(self.invoiceB.net, Decimal("-50.00"))

    def test_alternate_split_mode_off_ignores_manual_flag(self):
        self.online_auction.winning_bid_percent_to_club_for_club_members = 10
        self.online_auction.lot_entry_fee_for_club_members = 0
        self.online_auction.save()
        self.online_tos.is_club_member = True
        self.online_tos.save()
        # custom (the default for existing auctions) applies the alternate fees:
        # 3 sold lots at 10 * 90% = 27, less the 10 unsold lot fee
        self.assertEqual(self.invoice.total_sold, Decimal("17.00"))
        self.online_auction.alternate_split_mode = "off"
        self.online_auction.save()
        # standard fees: 3 sold lots at (10 * 75% - 2) = 16.50, less the 10 unsold lot fee.
        # Re-read: an invoice's totals are cached on the instance, and this one was worked out
        # before the auction's split mode changed. A request never holds an invoice across an
        # auction edit, so nothing invalidates it for us.
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total_sold, Decimal("6.50"))

    def test_club_member_mode_sets_flag_from_membership(self):
        self.online_auction.alternate_split_mode = "club_member"
        self.online_auction.save()
        self._make_member(self.user)
        self.assertFalse(self.online_tos.is_club_member)
        self.assertTrue(self.online_tos.update_alternate_split_from_membership())
        self.online_tos.refresh_from_db()
        self.assertTrue(self.online_tos.is_club_member)
        # a second call is a no-op
        self.assertFalse(self.online_tos.update_alternate_split_from_membership())

    def test_club_member_mode_does_not_set_flag_for_expired_member(self):
        self.online_auction.alternate_split_mode = "club_member"
        self.online_auction.save()
        self._make_member(self.user, paid=False)
        self.assertFalse(self.online_tos.update_alternate_split_from_membership())
        self.online_tos.refresh_from_db()
        self.assertFalse(self.online_tos.is_club_member)

    def test_custom_mode_does_not_touch_flag(self):
        self._make_member(self.user)
        self.assertFalse(self.online_tos.update_alternate_split_from_membership())
        self.online_tos.refresh_from_db()
        self.assertFalse(self.online_tos.is_club_member)

    def test_renewal_toggle_updates_alternate_split_flag(self):
        """Checking/unchecking renew membership updates the seller's alternate fees in club member mode"""
        self.online_auction.alternate_split_mode = "club_member"
        self.online_auction.save()
        self._make_member(self.userB, paid=False)
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(
            reverse("invoice_renewal_toggle", kwargs={"pk": self.invoiceB.pk}),
            {"renewal_needed": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invoice-club-member-discount-row")
        self.tosB.refresh_from_db()
        self.assertTrue(self.tosB.is_club_member)
        response = self.client.post(
            reverse("invoice_renewal_toggle", kwargs={"pk": self.invoiceB.pk}),
            {"renewal_needed": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.tosB.refresh_from_db()
        self.assertFalse(self.tosB.is_club_member)

    def test_paid_invoice_books_club_member_discount_in_ledger(self):
        from django.db.models import Sum

        self._make_member(self.userB)
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        entry = ClubMoney.objects.filter(invoice=self.invoiceB, category="club_member_discount").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.amount, Decimal("-5.00"))
        # all ledger entries for this invoice must reconcile to the cash that moved
        total = ClubMoney.objects.filter(invoice=self.invoiceB).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(total, -self.invoiceB.rounded_net)

    def _edit_form_data(self, overrides=None):
        auction = self.online_auction
        data = {
            "summernote_description": auction.summernote_description or "",
            "lot_entry_fee": str(auction.lot_entry_fee or "0"),
            "unsold_lot_fee": str(auction.unsold_lot_fee or "0"),
            "winning_bid_percent_to_club": str(auction.winning_bid_percent_to_club or "0"),
            "winning_bid_percent_to_club_for_club_members": str(
                auction.winning_bid_percent_to_club_for_club_members or "0"
            ),
            "lot_entry_fee_for_club_members": str(auction.lot_entry_fee_for_club_members or "0"),
            "pre_register_lot_discount_percent": str(auction.pre_register_lot_discount_percent or "0"),
            "alternate_split_mode": auction.alternate_split_mode,
            "alternative_split_label": auction.alternative_split_label or "",
            "club_member_discount": str(auction.club_member_discount or "0"),
            "tax": str(auction.tax or "0"),
            "online_bidding": auction.online_bidding,
            "date_start": auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": auction.date_end.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_rounding": str(auction.invoice_rounding),
            "only_whole_dollar_bids": "",
            "minimum_bid": str(auction.minimum_bid),
        }
        if overrides:
            data.update(overrides)
        return data

    def test_edit_form_club_member_mode_requires_club(self):
        form = AuctionEditForm(
            data=self._edit_form_data({"alternate_split_mode": "club_member", "club": ""}),
            instance=self.online_auction,
            user=self.user,
            cloned_from=None,
            user_timezone="UTC",
        )
        form.is_valid()
        self.assertIn("alternate_split_mode", form.errors)

    def test_edit_form_club_member_mode_forces_label_and_syncs_flags(self):
        ClubMember.objects.create(club=self.club, user=self.user, name="admin member", permission_admin=True)
        self._make_member(self.userB)
        form = AuctionEditForm(
            data=self._edit_form_data(
                {
                    "alternate_split_mode": "club_member",
                    "club": str(self.club.pk),
                    "alternative_split_label": "whatever",
                }
            ),
            instance=self.online_auction,
            user=self.user,
            cloned_from=None,
            user_timezone="UTC",
        )
        self.assertTrue(form.is_valid(), form.errors)
        auction = form.save()
        self.assertEqual(auction.alternative_split_label, "Club member")
        # everyone already in the auction gets the flag synced from their membership
        self.tosB.refresh_from_db()
        self.online_tos.refresh_from_db()
        self.assertTrue(self.tosB.is_club_member)
        self.assertFalse(self.online_tos.is_club_member)

    def test_edit_form_clearing_club_zeroes_discount(self):
        form = AuctionEditForm(
            data=self._edit_form_data({"club": "", "club_member_discount": "5"}),
            instance=self.online_auction,
            user=self.user,
            cloned_from=None,
            user_timezone="UTC",
        )
        self.assertTrue(form.is_valid(), form.errors)
        auction = form.save()
        self.assertEqual(auction.club_member_discount, 0)


class ClubMoneyRenewalConsistencyTests(StandardTestCase):
    """Guard the ClubMoney bookkeeping around membership renewals and invoices.

    These cover paths that previously lacked assertions on the ClubMoney that gets
    created, where the brittle behavior lives:
    - a membership renewal must book exactly ONE membership ClubMoney entry, never two
      (auction invoices book it via Invoice.sync_club_money; club-only
      invoices book it via _process_invoice_membership_renewal -- never both).
    - flipping an auction invoice PAID -> UNPAID -> PAID must not drift the club balance.
    - the self-service renewal invoice lookup must be idempotent (no invoice proliferation).
    """

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Money Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
        )
        self.payment_user = User.objects.create_user(
            username="money_payment_user", password="testpass", email="money_payment_user@example.com"
        )
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_money")
        self.online_auction.club = self.club
        self.online_auction.manage_users_through_club = True
        self.online_auction.add_membership_fee_to_invoices_for_expired_members = True
        self.online_auction.save()
        self.member = ClubMember.objects.create(
            club=self.club,
            user=self.online_tos.user,
            name="Renew Me",
            email=self.online_tos.email,
            membership_last_paid=timezone.now().date() - datetime.timedelta(days=370),
        )
        self.invoice.refresh_from_db()

    def _membership_entries(self):
        return ClubMoney.objects.filter(club=self.club, category=ClubMoney.CATEGORY_MEMBERSHIP)

    def _balance(self):
        from django.db.models import Sum

        return ClubMoney.objects.filter(club=self.club).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")

    def _membership_total(self):
        from django.db.models import Sum

        return self._membership_entries().aggregate(t=Sum("amount"))["t"] or Decimal("0.00")

    def test_auction_invoice_paid_books_single_membership_clubmoney(self):
        """Marking an auction renewal invoice PAID books exactly one membership ClubMoney.

        Regression test: a stray commit re-added the membership entry to
        _add_paid_entries without restoring the guard in
        _process_invoice_membership_renewal, so the fee was counted twice.
        """
        self.client.login(username=self.admin_user.username, password="testpassword")
        self.invoice.renewal_needed = True
        self.invoice.status = "UNPAID"
        self.invoice.save(update_fields=["renewal_needed", "status"])

        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")
        self.assertEqual(response.status_code, 200)

        entries = self._membership_entries()
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().amount, Decimal("25.00"))

    def test_auction_invoice_paid_unpaid_paid_is_balance_neutral(self):
        """Toggling an auction renewal invoice PAID -> UNPAID -> PAID must not drift the balance."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        self.invoice.renewal_needed = True
        self.invoice.status = "UNPAID"
        self.invoice.save(update_fields=["renewal_needed", "status"])

        self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")
        balance_after_first_paid = self._balance()

        self.client.post(f"/api/payinvoice/{self.invoice.pk}/UNPAID")
        self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")
        balance_after_second_paid = self._balance()

        self.assertEqual(balance_after_first_paid, balance_after_second_paid)
        # The membership grant is permanent, so the net membership revenue is one fee.
        self.assertEqual(self._membership_total(), Decimal("25.00"))

    def test_club_only_membership_invoice_books_single_membership_clubmoney(self):
        """A club-only (no auction) membership invoice books exactly one membership ClubMoney."""
        admin_member = ClubMember.objects.create(
            club=self.club, user=self.admin_user, name="Club Admin", permission_add_edit=True
        )
        invoice = Invoice.objects.create(
            club=self.club,
            club_member=admin_member,
            buyer=self.admin_user,
            status="UNPAID",
            renewal_needed=True,
        )
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(f"/api/payinvoice/{invoice.pk}/PAID")
        self.assertEqual(response.status_code, 200)

        entries = self._membership_entries().filter(invoice=invoice)
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().amount, Decimal("25.00"))

    def test_get_or_create_membership_invoice_idempotent_for_email_only_member(self):
        """Repeated lookups for an email-only member reuse one invoice (no proliferation)."""
        from auctions.views.club_pages import _get_or_create_membership_invoice

        email_member = ClubMember.objects.create(
            club=self.club,
            user=None,
            name="Email Only",
            email="email_only_member@example.com",
            membership_last_paid=timezone.now().date() - datetime.timedelta(days=400),
        )
        first = _get_or_create_membership_invoice(self.club, email_member)
        second = _get_or_create_membership_invoice(self.club, email_member)
        third = _get_or_create_membership_invoice(self.club, email_member)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.pk, third.pk)
        self.assertEqual(Invoice.objects.filter(club=self.club, auction=None, club_member=email_member).count(), 1)

    def test_manual_renew_books_membership_clubmoney(self):
        """The manual 'renew' admin action books a membership ClubMoney for paid clubs."""
        ClubMember.objects.create(club=self.club, user=self.admin_user, name="Club Admin", permission_add_edit=True)
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(reverse("club_member_renew", kwargs={"pk": self.member.pk}))
        self.assertEqual(response.status_code, 200)
        entries = self._membership_entries()
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().amount, Decimal("25.00"))


class ClubMembershipEmailTaskTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(
            name="Club Email Task Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            send_membership_expiration_reminders=True,
            send_membership_expiration_reminders_30_days=True,
            send_welcome_email_to_new_members=True,
        )
        self.payment_user = User.objects.create_user(
            username="club_email_task_payment_user",
            password="testpass",
            email="club_email_task_payment_user@example.com",
        )
        PayPalSeller.objects.create(user=self.payment_user, club=self.club, paypal_merchant_id="merchant_task")
        self.member = ClubMember.objects.create(
            club=self.club,
            name="Email Member",
            email="member@example.com",
            membership_expiration_date=timezone.now().date() + datetime.timedelta(days=30),
            membership_last_paid=timezone.now().date(),
        )

    @patch("auctions.tasks.mail.send")
    def test_daily_membership_task_sends_welcome_email(self, mock_send):
        # Its own beat task since the nightly membership work was split up: it used to sit below a
        # few thousand Discord API calls in one task body, under a 300-second soft time limit.
        from auctions.tasks import send_club_member_welcome_emails

        ClubMember.objects.filter(pk=self.member.pk).update(createdon=timezone.now() - datetime.timedelta(days=2))
        send_club_member_welcome_emails.run()

        self.member.refresh_from_db()
        self.assertTrue(self.member.welcome_email_sent)
        self.assertEqual(mock_send.call_args.kwargs["subject"], f"Welcome to the {self.club.name}!")
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERS").first()
        self.assertIsNotNone(history)
        self.assertIn("Sent welcome letter to", history.action)
        self.assertIn(self.member.email, history.action)

    @patch("auctions.tasks.mail.send")
    def test_daily_membership_task_logs_no_history_when_welcome_email_not_sent(self, mock_send):
        """A do-not-contact member gets no welcome email, so there's nothing to log."""
        from auctions.tasks import send_club_member_welcome_emails

        ClubMember.objects.filter(pk=self.member.pk).update(
            createdon=timezone.now() - datetime.timedelta(days=2),
            contact_status="do_not_contact",
        )
        send_club_member_welcome_emails.run()

        self.member.refresh_from_db()
        self.assertTrue(self.member.welcome_email_sent)
        self.assertFalse(mock_send.called)
        self.assertFalse(ClubHistory.objects.filter(club=self.club, action__contains="welcome letter").exists())

    @patch("auctions.tasks.mail.send")
    def test_daily_membership_task_sends_30_day_expiration_email(self, mock_send):
        from auctions.tasks import send_membership_expiration_reminders

        self.member.welcome_email_sent = True
        self.member.membership_expiration_reminder_30_days_due = timezone.now() - datetime.timedelta(minutes=1)
        self.member.save(update_fields=["welcome_email_sent", "membership_expiration_reminder_30_days_due"])

        send_membership_expiration_reminders.run()

        self.member.refresh_from_db()
        self.assertIsNone(self.member.membership_expiration_reminder_30_days_due)
        self.assertEqual(mock_send.call_args.kwargs["subject"], f"Your {self.club.name} membership expires in 30 days")
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").get()
        self.assertIn("Sent 30-day expiration reminder to", history.action)

    @patch("auctions.tasks.mail.send")
    def test_daily_membership_task_sends_day_before_expiration_email(self, mock_send):
        from auctions.tasks import send_membership_expiration_reminders

        ClubMember.objects.filter(pk=self.member.pk).update(
            welcome_email_sent=True,
            membership_expiration_date=timezone.now().date() + datetime.timedelta(days=1),
            membership_expiration_reminder_due=timezone.now() - datetime.timedelta(minutes=1),
        )

        send_membership_expiration_reminders.run()

        self.member.refresh_from_db()
        self.assertIsNone(self.member.membership_expiration_reminder_due)
        self.assertEqual(mock_send.call_args.kwargs["subject"], f"Your {self.club.name} membership expires tomorrow")
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").get()
        self.assertIn("Sent final expiration reminder to", history.action)

    @patch("auctions.tasks.mail.send")
    def test_renewal_confirmation_email_writes_club_history(self, mock_send):
        from auctions.tasks import maybe_send_membership_renewal_confirmation

        self.club.send_membership_renewal_confirmation = True
        self.club.save()
        self.assertTrue(maybe_send_membership_renewal_confirmation(self.member))
        history = ClubHistory.objects.filter(club=self.club, applies_to="MEMBERSHIP").get()
        self.assertIn("Sent renewal confirmation to", history.action)

    @patch("auctions.tasks.mail.send")
    def test_no_renewal_confirmation_history_when_member_cannot_be_emailed(self, mock_send):
        from auctions.tasks import maybe_send_membership_renewal_confirmation

        self.club.send_membership_renewal_confirmation = True
        self.club.save()
        self.member.contact_status = "do_not_contact"
        self.member.save(update_fields=["contact_status"])
        self.assertFalse(maybe_send_membership_renewal_confirmation(self.member))
        self.assertFalse(ClubHistory.objects.filter(club=self.club).exists())

    @patch("auctions.tasks.mail.send")
    def test_membership_email_falls_back_to_member_when_name_blank(self, mock_send):
        from auctions.tasks import send_club_member_email

        nameless = ClubMember.objects.create(
            club=self.club,
            name="",
            email="nameless@example.com",
        )
        send_club_member_email(nameless, "Subject", "Body")
        self.assertTrue(mock_send.called)
        kwargs = mock_send.call_args.kwargs
        self.assertIn("Dear Member,", kwargs["message"])
        self.assertIn("Dear Member,", kwargs["html_message"])


class ClubBarcodeViewTests(TestCase):
    def setUp(self):
        self.club = Club.objects.create(name="Barcode Club")

    def test_barcode_view_returns_svg(self):
        url = reverse("club_barcode", kwargs={"slug": self.club.slug, "value": 1234567890})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", response.content)

    def test_member_barcode_image_link_property(self):
        member = ClubMember.objects.create(club=self.club, name="Barcode Tester", email="b@example.com")
        # membership_number is auto-generated as a 10-digit string
        self.assertTrue(member.membership_number)
        link = member.barcode_image_link
        self.assertIn(f"/clubs/{self.club.slug}/barcode/{int(member.membership_number)}/", link)


class QuickCheckoutHTMXTests(StandardTestCase):
    def test_quick_checkout_shows_obvious_unsold_lot_warning(self):
        self.in_person_tos.bidder_number = "UNSOLD1"
        self.in_person_tos.save()
        Lot.objects.create(
            lot_name="Unsold in-person lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            quantity=1,
            active=True,
            donation=False,
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        self.client.force_login(self.admin_user)
        response = self.client.get(
            reverse(
                "auction_quick_checkout_htmx",
                kwargs={"slug": self.in_person_auction.slug, "filter": "UNSOLD1"},
            )
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("alert alert-warning", content)
        self.assertIn(invoice.unsold_lot_warning, content)

    def test_quick_checkout_app_shows_deep_link_and_hides_qr(self):
        # Inside the native app (FishAuctionsApp UA) the cashier taps the card on-device, so the
        # scan-a-QR flow is replaced by a fishauctions://pay/<pk> deep link and the QR is hidden.
        # Web visitors keep the existing QR/card checkout and never see the deep link. The gate reuses
        # the same request.is_mobile_app UA check that hides the web navbar in base.html.
        from unittest.mock import PropertyMock

        from auctions.views import QuickCheckoutHTMX

        self.in_person_tos.bidder_number = "APP1"
        self.in_person_tos.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        self.client.force_login(self.admin_user)
        url = reverse(
            "auction_quick_checkout_htmx",
            kwargs={"slug": self.in_person_auction.slug, "filter": "APP1"},
        )
        deep_link = f"fishauctions://pay/{invoice.pk}"

        # Force a Square QR into the context so the "hidden in-app" assertion is meaningful.
        with (
            patch.object(Invoice, "show_square_button", new_callable=PropertyMock, return_value=True),
            patch.object(Invoice, "reason_for_payment_not_available", new_callable=PropertyMock, return_value=""),
            patch.object(
                QuickCheckoutHTMX, "create_payment_link", return_value=("https://squareup.com/pay/fake", None)
            ),
        ):
            app_html = self.client.get(url, HTTP_USER_AGENT="FishAuctionsApp/1.0 (iOS)").content.decode("utf-8")
            web_html = self.client.get(url, HTTP_USER_AGENT="Mozilla/5.0").content.decode("utf-8")

        # In-app: native deep link shown, QR hidden.
        self.assertIn(deep_link, app_html)
        self.assertNotIn("Scan this code to pay with Square", app_html)
        # The explanatory template comment must never render as visible text.
        self.assertNotIn("deep-link to the on-device Tap to Pay screen", app_html)
        # Web: QR/card checkout shown, no deep link.
        self.assertNotIn(deep_link, web_html)
        self.assertIn("Scan this code to pay with Square", web_html)

    def test_quick_checkout_app_hides_deep_link_without_square(self):
        # Tap to Pay charges on the seller's Square account, so the in-app deep link must only appear
        # when Square is actually linked/authorized (show_square_button). With no Square account or
        # permission the button must not show, matching the Square QR gate and create_mobile_payment.
        from unittest.mock import PropertyMock

        self.in_person_tos.bidder_number = "APP2"
        self.in_person_tos.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        self.client.force_login(self.admin_user)
        url = reverse(
            "auction_quick_checkout_htmx",
            kwargs={"slug": self.in_person_auction.slug, "filter": "APP2"},
        )
        deep_link = f"fishauctions://pay/{invoice.pk}"

        with patch.object(Invoice, "show_square_button", new_callable=PropertyMock, return_value=False):
            app_html = self.client.get(url, HTTP_USER_AGENT="FishAuctionsApp/1.0 (iOS)").content.decode("utf-8")

        self.assertNotIn(deep_link, app_html)
        self.assertNotIn("Tap to Pay with card", app_html)

    def test_quick_checkout_camera_hidden_on_large_screens(self):
        """The self-scan camera ships on every checkout page but is hidden on large screens with a
        Bootstrap responsive class, so it only shows on small screens (phones + the app WebView).
        The server can't see the viewport, so gating is done client-side, not by User-Agent."""
        self.client.force_login(self.admin_user)
        url = reverse("auction_quick_checkout", kwargs={"slug": self.in_person_auction.slug})
        html = self.client.get(url).content.decode("utf-8")
        # The camera module is always shipped...
        self.assertIn("camera_scanner.js", html)
        # ...and the live-preview wrapper carries d-md-none so desktop never shows (or grabs) it.
        self.assertIn("d-md-none", html)

    def test_quick_checkout_scan_translates_paddle_barcode(self):
        """A scanned paddle barcode (11111 + bidder number) posted with ?barcode=1 resolves to the
        bidder holding that number, just as if the number had been typed in."""
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        self.client.force_login(self.admin_user)
        url = reverse(
            "auction_quick_checkout_htmx",
            kwargs={"slug": self.in_person_auction.slug, "filter": "11111555"},
        )
        content = self.client.get(url, {"barcode": "1"}).content.decode("utf-8")
        self.assertIn(f"invoice-buttons-{invoice.pk}", content)
