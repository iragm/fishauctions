"""Tests for club membership money: invoices, discounts, renewals and confirmation emails."""

import datetime
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.staticfiles.storage import staticfiles_storage
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    AuctionEditForm,
    ClubMembershipSettingsForm,
)
from auctions.models import (
    AuctionTOS,
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
        assert response.status_code == 401

    def test_invoice_status_button_non_admin_denied(self):
        """Non-admins can't change invoice status."""
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
        """The emailed no-login UUID can't change an invoice's status."""
        clubmoney_before = ClubMoney.objects.filter(invoice=self.invoice).count()
        url = f"/api/payinvoice/{self.invoice.no_login_link}/PAID"
        response = self.client.post(url)
        # No UUID status route exists.
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
        """An invalid status is rejected (404) and not written."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        original_status = self.invoice.status
        url = f"/api/payinvoice/{self.invoice.pk}/BANANA"
        response = self.client.post(url)
        assert response.status_code == 404
        self.invoice.refresh_from_db()
        assert self.invoice.status == original_status
        assert self.invoice.status in ("DRAFT", "UNPAID", "PAID")

    def test_invoice_status_button_non_admin_owner_denied(self):
        """A non-admin owner can't change status by pk or UUID, and no ClubMoney is booked."""
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
        """An auction admin can mark paid and unpaid by pk."""
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
        """The no-login UUID still shows the invoice."""
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
            membership_last_paid=timezone.localdate() - datetime.timedelta(days=370),
        )
        self.invoice.refresh_from_db()

    def test_membership_reminder_due_updates_when_membership_changes(self):
        self.member.membership_last_paid = timezone.localdate()
        self.member.save()
        self.member.refresh_from_db()
        self.assertIsNotNone(self.member.membership_expiration_reminder_due)

    def test_membership_reminder_due_not_set_for_free_membership(self):
        self.club.membership_annual_fee = None
        self.club.save(update_fields=["membership_annual_fee"])
        self.member.membership_last_paid = timezone.localdate()
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
        self.assertGreaterEqual(self.member.membership_last_paid, timezone.localdate())
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
    """PayPalSubscriptionWebhookView and its apply logic."""

    def setUp(self):
        super().setUp()
        # Own credentials, so subscriptions are supported.
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
        # Losing PayPal eligibility doesn't wipe the webhook id.
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
        self.assertEqual(member.membership_expiration_date, timezone.localdate() + datetime.timedelta(days=365))
        mock_email.assert_called_once()

    def test_active_subscription_creates_member_when_none(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(email="new@example.com"))
        member = ClubMember.objects.get(club=self.club, paypal_subscription_id="I-SUB1")
        self.assertEqual(member.email, "new@example.com")

    def test_active_subscription_books_club_money(self):
        # A renewal is cash into the club, like the manual button.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        member = ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment="25.00"))
        entry = self._membership_money().get()
        self.assertEqual(entry.amount, Decimal("25.00"))
        self.assertEqual(entry.date, timezone.localdate())
        self.assertIn("I-SUB1", entry.description)
        self.assertIn(str(member), entry.description)
        self.assertIsNone(entry.created_by)  # a webhook has no acting user

    def test_books_amount_paypal_actually_charged_not_club_fee(self):
        # List price is 25.00, but this subscriber pays 18.50.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(last_payment="18.50"))
        self.assertEqual(self._membership_money().get().amount, Decimal("18.50"))

    def test_duplicate_delivery_books_club_money_once(self):
        # PayPal retries; no double-counting.
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
        # UPDATED can move next_billing_time without a charge; booking is keyed on the payment.
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        with patch_views("maybe_send_membership_renewal_confirmation"):
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=365))
            # Same last_payment, later billing date.
            _apply_paypal_subscription_event(self.club, self._active_subscription(next_days=730))
        self.assertEqual(self._membership_money().count(), 1)

    def test_payment_booked_even_when_dates_did_not_move(self):
        # ACTIVATED can precede the first charge, whose money must still be booked.
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
        self.assertEqual(member.membership_expiration_date, timezone.localdate() + datetime.timedelta(days=730))

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
        self.assertEqual(member.membership_expiration_date, timezone.localdate() + datetime.timedelta(days=365))

    def test_unhandled_event_ignored_without_verification(self):
        from auctions.views import PayPalSubscriptionWebhookView

        with patch.object(PayPalSubscriptionWebhookView, "_identify_and_verify_club") as mock_verify:
            response = self._post_event({"event_type": "BILLING.SUBSCRIPTION.CREATED", "resource": {"id": "I-SUB1"}})
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ignored"})
        mock_verify.assert_not_called()

    def test_handled_event_without_subscription_id_ignored(self):
        # A lifecycle event without a resource id is ignored.
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
        # A one-off sale has no billing_agreement_id and is ignored.
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

        # Another club mustn't receive this subscriber.
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
            membership_expiration_date=timezone.localdate() + datetime.timedelta(days=365),
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
            membership_expiration_date=timezone.localdate() + datetime.timedelta(days=365),
        )
        with patch("auctions.tasks.send_club_member_email") as mock_send:
            maybe_send_membership_renewal_confirmation(member)
        self.assertNotIn("autopay", mock_send.call_args.kwargs["message_text"])


class ClubMemberDiscountTests(StandardTestCase):
    """Auction.club_member_discount and alternate_split_mode.

    self.invoiceB is tosB's 3 bought $10 lots (adjustments cancel out); self.invoice is online_tos's
    3 sold lots plus one unsold.
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
            membership_last_paid=timezone.localdate() - datetime.timedelta(days=265),
            membership_expiration_date=timezone.localdate() + datetime.timedelta(days=days),
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
        """Checking renewal gives an unpaid member club member pricing."""
        self._make_member(self.userB, paid=False)
        self.invoiceB.renewal_needed = True
        self.invoiceB.save(update_fields=["renewal_needed"])
        self.assertTrue(self.invoiceB.treat_as_club_member)
        self.assertEqual(self.invoiceB.club_member_discount, 5)
        # 30 for lots bought, less the 5 discount, plus the 25 membership fee
        self.assertEqual(self.invoiceB.net, Decimal("-50.00"))

    def test_renewal_toggle_only_adds_fee_for_active_member(self):
        """An active member already has the discount; checking the box only adds the fee."""
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
        # custom: 3 * 10 * 90% = 27, less the 10 unsold fee.
        self.assertEqual(self.invoice.total_sold, Decimal("17.00"))
        self.online_auction.alternate_split_mode = "off"
        self.online_auction.save()
        # standard: 3 * (10 * 75% - 2) = 16.50, less 10. Invoice totals are cached on the instance.
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
        """Toggling renewal updates the seller's alternate split in club_member mode."""
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
    """ClubMoney bookkeeping around renewals: one membership entry per renewal, no drift on PAID/UNPAID
    toggles, and idempotent renewal invoice lookup.
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
            membership_last_paid=timezone.localdate() - datetime.timedelta(days=370),
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
        """Marking an auction renewal invoice PAID books one membership ClubMoney."""
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
        """PAID -> UNPAID -> PAID doesn't change the balance."""
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
        self.assertEqual(self._membership_total(), Decimal("25.00"))

    def test_club_only_membership_invoice_books_single_membership_clubmoney(self):
        """A club-only membership invoice books one membership ClubMoney."""
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
        """Repeated lookups for an email-only member reuse one invoice."""
        from auctions.views.club_pages import _get_or_create_membership_invoice

        email_member = ClubMember.objects.create(
            club=self.club,
            user=None,
            name="Email Only",
            email="email_only_member@example.com",
            membership_last_paid=timezone.localdate() - datetime.timedelta(days=400),
        )
        first = _get_or_create_membership_invoice(self.club, email_member)
        second = _get_or_create_membership_invoice(self.club, email_member)
        third = _get_or_create_membership_invoice(self.club, email_member)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.pk, third.pk)
        self.assertEqual(Invoice.objects.filter(club=self.club, auction=None, club_member=email_member).count(), 1)

    def test_manual_renew_books_membership_clubmoney(self):
        """The manual renew action books membership ClubMoney."""
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
            mailing_address="PO Box 1, Springfield IL 62701",
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
            membership_expiration_date=timezone.localdate() + datetime.timedelta(days=30),
            membership_last_paid=timezone.localdate(),
        )

    @patch("auctions.tasks.mail.send")
    def test_daily_membership_task_sends_welcome_email(self, mock_send):
        # Its own beat task, split from the long nightly membership task.
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
            membership_expiration_date=timezone.localdate() + datetime.timedelta(days=1),
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
    def test_membership_email_greets_there_when_name_blank(self, mock_send):
        from auctions.tasks import send_club_member_email

        nameless = ClubMember.objects.create(
            club=self.club,
            name="",
            email="nameless@example.com",
        )
        send_club_member_email(nameless, "Subject", "Body")
        self.assertTrue(mock_send.called)
        kwargs = mock_send.call_args.kwargs
        self.assertIn("Hey there,", kwargs["message"])
        self.assertIn("Hey there,", kwargs["html_message"])


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
        self.assertIn(f"/clubs/{self.club.url_key}/barcode/{int(member.membership_number)}/", link)


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
        # In the app, a fishauctions://pay/<pk> deep link replaces the QR.
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

        # Force a Square QR so "hidden in-app" means something.
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
        # Only when Square is linked (show_square_button).
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
        """The self-scan camera is hidden on large screens with a responsive class, not by User-Agent."""
        self.client.force_login(self.admin_user)
        url = reverse("auction_quick_checkout", kwargs={"slug": self.in_person_auction.slug})
        html = self.client.get(url).content.decode("utf-8")
        # Hashed where collectstatic has run; see fishauctions/static_storage.py.
        self.assertIn(staticfiles_storage.url("js/camera_scanner.js"), html)
        self.assertIn("d-md-none", html)

    def test_quick_checkout_scan_translates_paddle_barcode(self):
        """A scanned paddle barcode (11111 + bidder number) resolves to that bidder."""
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        self.client.force_login(self.admin_user)
        url = reverse(
            "auction_quick_checkout_htmx",
            kwargs={"slug": self.in_person_auction.slug, "filter": "11111555"},
        )
        content = self.client.get(url, {"barcode": "1"}).content.decode("utf-8")
        self.assertIn(f"invoice-buttons-{invoice.pk}", content)


class CarriedMembershipTests(StandardTestCase):
    """A membership carried with another member's: its dates follow the carrier's, and nothing offers to
    renew it on its own.
    """

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.club = Club.objects.create(
            name="Family Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            send_membership_expiration_reminders=True,
        )
        payment_user = User.objects.create_user(
            username="family_payment_user", password="testpass", email="family_payment_user@example.com"
        )
        PayPalSeller.objects.create(user=payment_user, club=self.club, paypal_merchant_id="merchant_family")
        self.online_auction.club = self.club
        self.online_auction.manage_users_through_club = True
        self.online_auction.add_membership_fee_to_invoices_for_expired_members = True
        self.online_auction.save()
        ClubMember.objects.create(
            club=self.club, user=self.admin_user, name="Club Admin", permission_view=True, permission_add_edit=True
        )
        self.carrier = ClubMember.objects.create(
            club=self.club,
            name="Carrier",
            email="carrier@example.com",
            membership_last_paid=self.today - datetime.timedelta(days=360),
            membership_expiration_date=self.today + datetime.timedelta(days=5),
        )
        self.carried = ClubMember.objects.create(
            club=self.club,
            user=self.online_tos.user,
            name="Carried",
            email=self.online_tos.email,
            membership_carried_by=self.carrier,
        )
        self.invoice.refresh_from_db()

    def test_dates_follow_the_carrier(self):
        from auctions.views.club_members import renew_club_member

        self.assertEqual(self.carried.membership_expiration_date, self.carrier.membership_expiration_date)
        self.assertIsNone(self.carried.membership_expiration_reminder_due)
        renew_club_member(self.carrier)
        self.carried.refresh_from_db()
        self.assertEqual(self.carried.membership_expiration_date, self.today + datetime.timedelta(days=370))
        self.assertEqual(self.carried.membership_last_paid, self.today)
        self.assertIsNone(self.carried.membership_expiration_reminder_due)
        self.assertIsNone(self.carried.membership_expiration_reminder_30_days_due)

    def test_the_member_is_never_offered_payment(self):
        from auctions.views.club_pages import _membership_renewal_state

        self.assertFalse(_membership_renewal_state(self.club, self.carried)[2])
        self.assertTrue(_membership_renewal_state(self.club, self.carrier)[2])
        self.client.force_login(self.online_tos.user)
        response = self.client.get(reverse("club_membership_pay", kwargs={"slug": self.club.slug}))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Your membership comes with Carrier")
        self.assertIsNone(response.context["membership_invoice"])

    def test_admins_cannot_renew_it(self):
        from auctions.tables import ClubMemberHTMxTable
        from auctions.views.club_members import renew_club_member

        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("club_member_renew", kwargs={"pk": self.carried.pk})
        response = self.client.get(url)
        self.assertContains(response, "Carried with Carrier")
        self.assertNotContains(response, f'hx-post="{url}"')
        self.assertEqual(self.client.post(url).status_code, 400)
        response = self.client.get(
            reverse("club_member_renew_page", kwargs={"slug": self.club.slug, "pk": self.carried.pk})
        )
        self.assertEqual(response.status_code, 302)
        with self.assertRaises(ValueError):
            renew_club_member(self.carried)
        table = ClubMemberHTMxTable([], can_add_edit=True, can_manage_membership=True)
        self.assertNotIn(url, table.render_actions(None, self.carried))
        self.assertNotIn(url, table.render_membership_expiration_date(None, self.carried))
        carrier_url = reverse("club_member_renew", kwargs={"pk": self.carrier.pk})
        self.assertIn(carrier_url, table.render_actions(None, self.carrier))
        self.carried.refresh_from_db()
        self.assertEqual(self.carried.membership_expiration_date, self.carrier.membership_expiration_date)

    def test_auction_invoices_never_add_the_fee(self):
        from auctions.views.base import _should_mark_invoice_renewal_needed

        self.assertFalse(_should_mark_invoice_renewal_needed(self.invoice))
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(
            reverse("invoice_renewal_toggle", kwargs={"pk": self.invoice.pk}), {"renewal_needed": "1"}
        )
        self.assertEqual(response.status_code, 400)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.renewal_needed)

    def test_dues_already_taken_renew_the_carrier(self):
        invoice = Invoice.objects.create(
            club=self.club, club_member=self.carried, buyer=self.online_tos.user, status="UNPAID", renewal_needed=True
        )
        self.client.login(username=self.admin_user.username, password="testpassword")
        self.assertEqual(self.client.post(f"/api/payinvoice/{invoice.pk}/PAID").status_code, 200)
        self.carrier.refresh_from_db()
        self.carried.refresh_from_db()
        self.assertEqual(self.carrier.membership_expiration_date, self.today + datetime.timedelta(days=370))
        self.assertEqual(self.carried.membership_expiration_date, self.carrier.membership_expiration_date)

    def test_renewal_lists_and_reminders_skip_it(self):
        from auctions.filters import ClubMemberFilter
        from auctions.tasks import _run_reminder_pass

        def names(query):
            qs = ClubMember.objects.filter(club=self.club)
            return set(ClubMemberFilter({"query": query}, queryset=qs).qs.values_list("name", flat=True))

        self.assertEqual(names("expiring"), {"Carrier"})
        self.assertNotIn("Carried", names("expired"))
        self.assertFalse(self.carried.compute_mailchimp_tags()["expiring-soon"])
        self.assertTrue(self.carrier.compute_mailchimp_tags()["expiring-soon"])
        due = timezone.now() - datetime.timedelta(hours=1)
        ClubMember.objects.filter(pk__in=[self.carrier.pk, self.carried.pk]).update(
            membership_expiration_reminder_due=due
        )
        with patch("auctions.tasks._send_one_reminder") as send:
            _run_reminder_pass(timezone.now(), self.today, "membership_expiration_reminder_due", "", "", "test")
        self.assertEqual([call.args[0].pk for call in send.call_args_list], [self.carrier.pk])

    def _form(self, member, carrier, **kwargs):
        from django.forms.models import model_to_dict

        from auctions.forms import ClubMemberAdminForm

        fields = ClubMemberAdminForm(instance=member, club=self.club).fields
        data = {key: ("" if value is None else value) for key, value in model_to_dict(member, fields=fields).items()}
        data["membership_carried_by"] = carrier.pk if carrier else ""
        return ClubMemberAdminForm(data, instance=member, club=self.club, **kwargs)

    def test_the_edit_form_sets_it_one_level_deep(self):
        from auctions.forms import ClubMemberAdminForm

        other = ClubMember.objects.create(club=self.club, name="Other")
        form = self._form(other, self.carrier)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        other.refresh_from_db()
        self.assertEqual(other.membership_expiration_date, self.carrier.membership_expiration_date)
        # The carrier already carries others, and a carried member carries nobody.
        self.assertFalse(self._form(self.carrier, other).is_valid())
        self.assertFalse(self._form(other, self.carried).is_valid())
        self.assertNotIn("membership_carried_by", ClubMemberAdminForm(club=self.club).fields)
        # From a club-managed auction's user list too: some clubs keep their whole list there.
        self.assertIn(
            "membership_carried_by",
            ClubMemberAdminForm(instance=self.carried, club=self.club, auction=self.online_auction).fields,
        )

    def test_the_edit_modal_and_its_autocomplete(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("clubmember_admin", kwargs={"pk": self.carried.pk}))
        self.assertContains(response, "Membership carried with")
        response = self.client.get(
            reverse("club-member-autocomplete"),
            {"forward": json.dumps({"club_slug": self.club.slug, "carrier_for": self.carrier.pk})},
        )
        names = {result["text"].split(" (")[0] for result in response.json()["results"]}
        self.assertIn("Club Admin", names)
        self.assertNotIn("Carrier", names)
        self.assertNotIn("Carried", names)

    def _merge(self, source, target):
        self.client.login(username=self.admin_user.username, password="testpassword")
        return self.client.post(
            reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": source.pk}),
            {"step": "review", "target": target.pk, "name": target.name, "email": target.email or ""},
        )

    def test_deactivating_the_carrier_frees_who_it_carried(self):
        self.carrier.is_deleted = True
        self.carrier.save(update_fields=["is_deleted"])
        self.carried.refresh_from_db()
        self.assertIsNone(self.carried.membership_carried_by)
        # Its own reminders and renewals are back.
        self.assertIsNotNone(self.carried.membership_expiration_reminder_due)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="no longer carried").exists())

    def test_merging_a_carried_member_into_its_carrier(self):
        self.assertEqual(self._merge(self.carried, self.carrier).status_code, 302)
        self.carrier.refresh_from_db()
        self.carried.refresh_from_db()
        self.assertIsNone(self.carrier.membership_carried_by)
        self.assertTrue(self.carried.is_deleted)
        self.assertFalse(self.carrier.carried_memberships.filter(is_deleted=False).exists())

    def test_merging_the_carrier_into_a_member_it_carries(self):
        self.assertEqual(self._merge(self.carrier, self.carried).status_code, 302)
        self.carried.refresh_from_db()
        self.assertIsNone(self.carried.membership_carried_by)
        self.assertEqual(self.carried.membership_expiration_date, self.today + datetime.timedelta(days=5))

    def test_merging_a_carried_member_into_a_new_record_keeps_it_carried(self):
        target = ClubMember.objects.create(club=self.club, name="Carried Again", email="again@example.com")
        self.assertEqual(self._merge(self.carried, target).status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.membership_carried_by, self.carrier)
        self.assertEqual(target.membership_expiration_date, self.carrier.membership_expiration_date)

    def test_merging_into_a_record_with_its_own_later_dues_keeps_them(self):
        own = self.today + datetime.timedelta(days=300)
        target = ClubMember.objects.create(
            club=self.club, name="Paid Myself", email="paid@example.com", membership_expiration_date=own
        )
        self._merge(self.carried, target)
        target.refresh_from_db()
        self.assertIsNone(target.membership_carried_by)
        self.assertEqual(target.membership_expiration_date, own)

    def test_a_box_ticked_before_they_were_carried_comes_off(self):
        from auctions.views.base import _ensure_invoice_renewal_state

        Invoice.objects.filter(pk=self.invoice.pk).update(renewal_needed=True, renewal_manually_set=True)
        self.invoice.refresh_from_db()
        _ensure_invoice_renewal_state(self.invoice)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.renewal_needed)

    def test_deleting_the_carrier_frees_who_it_carried(self):
        self.carrier.delete()
        self.carried.refresh_from_db()
        self.assertIsNone(self.carried.membership_carried_by)
        self.assertIsNotNone(self.carried.membership_expiration_reminder_due)

    def test_a_stray_subscription_leaves_the_carriers_own_alone(self):
        from auctions.views.webhooks import _apply_paypal_subscription_event

        ClubMember.objects.filter(pk=self.carrier.pk).update(paypal_subscription_id="I-CARRIER")
        ClubMember.objects.filter(pk=self.carried.pk).update(paypal_subscription_id="I-STRAY")
        _apply_paypal_subscription_event(
            self.club,
            {
                "id": "I-STRAY",
                "status": "ACTIVE",
                "billing_info": {"next_billing_time": "2099-01-01T00:00:00Z"},
            },
        )
        self.carrier.refresh_from_db()
        self.assertEqual(self.carrier.paypal_subscription_id, "I-CARRIER")
        self.assertEqual(self.carrier.membership_expiration_date, self.today + datetime.timedelta(days=5))

    def test_the_palette_keeps_it_when_editing_an_auction_participant(self):
        from auctions.palette_actions import _update_through_the_club

        request = self.client.request().wsgi_request
        request.user = self.admin_user
        problem = _update_through_the_club(request, self.online_auction, self.carried, {"phone_number": "555-0100"})
        self.assertIsNone(problem)
        self.carried.refresh_from_db()
        self.assertEqual(self.carried.membership_carried_by, self.carrier)

    def test_merging_the_carrier_moves_who_it_carries(self):
        target = ClubMember.objects.create(club=self.club, name="Target", email="target@example.com")
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.post(
            reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": self.carrier.pk}),
            {"step": "review", "target": target.pk, "name": "Target", "email": "target@example.com"},
        )
        self.assertEqual(response.status_code, 302)
        self.carried.refresh_from_db()
        self.assertEqual(self.carried.membership_carried_by, target)


class WelcomeLetterTests(StandardTestCase):
    """Who gets the club's welcome letter when they're added, and who gets it when they first pay."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Welcome Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            send_welcome_email_to_new_members=True,
        )
        self.in_person_auction.club = self.club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()

    def _participant(self, email="john@example.com"):
        from auctions.services import ensure_club_member

        member, _created = ensure_club_member(self.in_person_auction, name="John", email=email)
        return member

    def test_an_auction_welcomes_who_it_adds_by_default(self):
        self.assertTrue(self.in_person_auction.send_club_welcome_letter)
        self.assertTrue(self._participant().send_welcome_email)

    def test_an_auction_can_hold_the_letter_until_they_pay(self):
        from auctions.views.club_members import renew_club_member

        self.in_person_auction.send_club_welcome_letter = False
        self.in_person_auction.save()
        john = self._participant()
        self.assertEqual((john.send_welcome_email, john.welcome_email_sent), (False, False))
        # Held until he pays at checkout.
        renew_club_member(john)
        john.refresh_from_db()
        self.assertTrue(john.send_welcome_email)
        self.assertFalse(john.welcome_email_sent)

    def test_a_letter_already_sent_is_not_sent_again(self):
        from auctions.views.club_members import renew_club_member

        # Welcomed when they joined; an edit since then unticked the box.
        member = ClubMember.objects.create(
            club=self.club, name="Welcomed", send_welcome_email=False, welcome_email_sent=True
        )
        renew_club_member(member)
        member.refresh_from_db()
        self.assertEqual((member.send_welcome_email, member.welcome_email_sent), (False, True))

    def test_a_renewal_is_not_a_first_payment(self):
        from auctions.views.club_members import renew_club_member

        member = ClubMember.objects.create(
            club=self.club,
            name="Old Hand",
            send_welcome_email=False,
            welcome_email_sent=True,
            membership_last_paid=timezone.localdate() - datetime.timedelta(days=400),
        )
        renew_club_member(member)
        member.refresh_from_db()
        self.assertFalse(member.send_welcome_email)
        self.assertTrue(member.welcome_email_sent)

    def test_adding_all_participants_sends_no_letters(self):
        ClubMember.objects.create(
            club=self.club, user=self.admin_user, name="Club Admin", permission_view=True, permission_add_edit=True
        )
        self.online_auction.club = self.club
        self.online_auction.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(email="participant@example.com")
        self.client.post(reverse("auction_add_users_to_club", kwargs={"slug": self.online_auction.slug}))
        added = ClubMember.objects.filter(club=self.club, source=str(self.online_auction.title)[:200])
        self.assertTrue(added.exists())
        self.assertFalse(added.filter(send_welcome_email=True).exists())

    def test_both_fields_say_when_the_letter_goes_and_grey_out_without_one(self):
        from auctions.forms import AuctionEditForm, ClubMemberAdminForm

        def fields():
            member_field = ClubMemberAdminForm(club=self.club).fields["send_welcome_email"]
            auction_field = AuctionEditForm(
                instance=self.in_person_auction, user=self.admin_user, cloned_from=None, user_timezone="UTC"
            ).fields["send_club_welcome_letter"]
            return member_field, auction_field

        for field in fields():
            self.assertFalse(field.disabled)
            self.assertIn("first pay dues", field.help_text)
        self.club.send_welcome_email_to_new_members = False
        self.club.save()
        self.in_person_auction.refresh_from_db()
        for field in fields():
            self.assertTrue(field.disabled)
            self.assertIn("doesn't send welcome letters", field.help_text)

    def test_a_csv_row_can_ask_for_the_letter(self):
        from django.test import RequestFactory

        from auctions.views.club_reports import ClubMemberCSVImportView

        view = ClubMemberCSVImportView()
        view.club = self.club
        view.request = RequestFactory().get("/")
        view.request.user = self.admin_user
        asked = view._create_member(view._parse_member_row({"Email": "a@example.com", "Send Welcome Letter": "yes"}))
        quiet = view._create_member(view._parse_member_row({"Email": "b@example.com"}))
        self.assertEqual((asked.send_welcome_email, asked.welcome_email_sent), (True, False))
        self.assertEqual((quiet.send_welcome_email, quiet.welcome_email_sent), (False, False))

    def test_the_backfill_marks_unreached_imports_done(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("auctions.migrations.0478_welcome_letter_settings")
        imported = ClubMember.objects.create(club=self.club, name="Imported", source="csv")
        joined = ClubMember.objects.create(club=self.club, name="Joined", source="joined")
        migration.mark_imported_members_welcomed(apps, None)
        imported.refresh_from_db()
        joined.refresh_from_db()
        self.assertEqual((imported.welcome_email_sent, imported.send_welcome_email), (True, False))
        self.assertEqual((joined.welcome_email_sent, joined.send_welcome_email), (False, True))
