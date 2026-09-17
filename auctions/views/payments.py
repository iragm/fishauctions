"""Connecting PayPal and Square accounts and taking payments through them.

:class:`PayPalAPIMixin` is the PayPal client (tokens, orders, subscriptions, refunds). OAuth
connect/callback pairs for both follow. Provider webhooks are in :mod:`auctions.views.webhooks`.
"""

import base64
import json
import logging
from decimal import Decimal
from urllib.parse import urlencode, urlparse

import channels.layers
import requests
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models.base import Model as Model
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import TemplateView, View
from django.views.generic.edit import (
    DeleteView,
)

from auctions.mobile.services.web_session import mark_session_opened_by_app, session_opened_by_app
from auctions.models import (
    SQUARE_OAUTH_SCOPES,
    ClubHistory,
    ClubMember,
    Invoice,
    InvoicePayment,
    PayPalSeller,
    SquareSeller,
    UserData,
)

from .base import (
    _ensure_invoice_renewal_state,
    _pop_club_for_payment_oauth,
    _process_invoice_membership_renewal,
    _stash_club_for_payment_oauth,
)

logger = logging.getLogger(__name__)


class PayPalRequestError(Exception):
    """Raised when a PayPal API request fails in a recoverable way for the caller."""


class PayPalAPIMixin:
    """PayPal platform partner API. Settings: PAYPAL_API_BASE, PAYPAL_CLIENT_ID, PAYPAL_SECRET,
    PARTNER_MERCHANT_ID, PAYPAL_BN_CODE, PAYPAL_WEBHOOK_ID.
    """

    def _paypal_auth(self):
        """``(client_id, secret)``: a non-OAuth club's own app credentials if set, else the site's."""
        creds = getattr(self, "club_paypal_credentials", None)
        if creds and creds[0] and creds[1]:
            return creds[0], creds[1]
        return settings.PAYPAL_CLIENT_ID, settings.PAYPAL_SECRET

    def _get_access_token(self):
        client_id, secret = self._paypal_auth()
        # Logged here: several callers show a generic message and would hide the failure.
        try:
            token_resp = requests.post(
                f"{settings.PAYPAL_API_BASE}/v1/oauth2/token",
                auth=(client_id, secret),
                headers={"Accept": "application/json", "Accept-Language": "en_US"},
                data={"grant_type": "client_credentials"},
                timeout=10,
            )
            token_resp.raise_for_status()
        except requests.RequestException as exc:
            resp = getattr(exc, "response", None)
            logger.error(
                "PayPal auth token request failed: %s status=%s debug_id=%s resp_text=%s",
                exc,
                getattr(resp, "status_code", None),
                resp.headers.get("Paypal-Debug-Id", "") if resp is not None else "",
                resp.text[:500] if resp is not None else "",
            )
            msg = f"PayPal auth token request failed: {exc}"
            raise PayPalRequestError(msg) from exc
        return token_resp.json()["access_token"]

    def _build_paypal_headers(self, merchant_id="", include_bn_code=True, token=None):
        """Common header builder for PayPal API calls"""
        headers = {
            "Authorization": f"Bearer {token or self._get_access_token()}",
            "Content-Type": "application/json",
        }
        # The BN code is ours; don't send it with a club's own credentials.
        using_club_creds = bool(getattr(self, "club_paypal_credentials", None))
        if include_bn_code and not using_club_creds and getattr(settings, "PAYPAL_BN_CODE", None):
            headers["PayPal-Partner-Attribution-Id"] = settings.PAYPAL_BN_CODE
        if merchant_id:
            headers["PayPal-Auth-Assertion"] = self._build_auth_assertion(merchant_id)
        return headers

    def _build_auth_assertion(self, merchant_payer_id):
        """Build unsigned PayPal-Auth-Assertion JWT for acting on behalf of a merchant."""
        header = {"alg": "none"}
        payload = {
            "iss": self._paypal_auth()[0],
            "payer_id": merchant_payer_id,  # obtained from partner referral flow
        }

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")

        unsigned_jwt = f"{b64(header)}.{b64(payload)}."
        return unsigned_jwt

    def _paypal_request(self, method, endpoint, *, json=None, params=None, merchant_id="", include_bn_code=True):
        """Single request helper used by get_from_paypal and post_to_paypal."""
        url = f"{settings.PAYPAL_API_BASE}/{str(endpoint).lstrip('/')}"
        headers = self._build_paypal_headers(merchant_id=merchant_id, include_bn_code=include_bn_code)
        self.paypal_debug = ""
        try:
            resp = requests.request(method, url, headers=headers, json=json, params=params, timeout=10)
            resp.raise_for_status()
            self.paypal_debug = resp.headers.get("Paypal-Debug-Id")
            return resp.json()
        except requests.HTTPError:
            debug_id = resp.headers.get("Paypal-Debug-Id", "")
            self.paypal_debug = debug_id
            safe_headers = dict(headers or {})
            if "Authorization" in safe_headers:
                safe_headers["Authorization"] = "Bearer ****"
            logger.error(
                "PayPal API call failed: %s %s status=%s debug_id=%s req_headers=%s req_params=%s req_json=%s resp_text=%s",
                method,
                url,
                resp.status_code,
                debug_id,
                safe_headers,
                params,
                json,
                resp.text[:1000],
            )
            msg = f"PayPal API call failed: {method} {url} status={resp.status_code} debug_id={debug_id}"
            raise PayPalRequestError(msg)
        except requests.RequestException as exc:
            # No response at all; log it, since callers show a generic message.
            logger.error(
                "PayPal API call failed (no response): %s %s error=%s req_params=%s req_json=%s",
                method,
                url,
                exc,
                params,
                json,
            )
            msg = f"PayPal API call failed (no response): {method} {url} error={exc}"
            raise PayPalRequestError(msg) from exc

    def post_to_paypal(self, endpoint, payload, include_bn_code=True):
        """POST JSON to a PayPal API endpoint and return parsed JSON."""
        return self._paypal_request("POST", endpoint, json=payload, include_bn_code=include_bn_code)

    def get_from_paypal(self, endpoint, include_bn_code=True, params=None):
        """GET from a PayPal API endpoint and return parsed JSON."""
        return self._paypal_request("GET", endpoint, params=params, include_bn_code=include_bn_code)

    def create_order(self, invoice, member_pk=""):
        """Create a PayPal order for an invoice. Returns an approval URL or None."""
        # A non-OAuth club pays through its own app. None means the site app.
        self.club_paypal_credentials = invoice.paypal_credentials
        currency = invoice.currency

        items = []
        # Totals summed from the exact per-item values: PayPal requires them to match, and refunds
        # make final_price differ from winning_price.
        item_total = Decimal("0.00")
        tax_total = Decimal("0.00")
        for lot in invoice.bought_lots_queryset:
            unit_amount = Decimal(str(lot.final_price)).quantize(Decimal("0.01"))
            item_tax = Decimal(str(lot.tax)).quantize(Decimal("0.01"))
            item_total += unit_amount
            tax_total += item_tax
            items.append(
                {
                    "name": f"{lot.lot_number_display} - {lot.lot_name}",
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{unit_amount:.2f}"},
                    "category": "PHYSICAL_GOODS",
                    "url": lot.full_lot_link,
                    "tax": {"currency_code": currency, "value": f"{item_tax:.2f}"},
                }
            )

        # Charge the rounded balance; the rounding delta becomes an adjustment line below.
        target_total = (Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)).quantize(Decimal("0.01"))
        item_total = item_total.quantize(Decimal("0.01"))
        tax_total = tax_total.quantize(Decimal("0.01"))

        # target_total = item_total + tax_total - discount: a negative adjustment is a discount, a
        # positive one an "Adjustments" line item.
        adjustment = (target_total - (item_total + tax_total)).quantize(Decimal("0.01"))

        discount_value = Decimal("0.00")
        if adjustment > 0:
            # Add an adjustment item and include in item_total
            items.append(
                {
                    "name": "Adjustments",
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{adjustment:.2f}"},
                    "category": "PHYSICAL_GOODS",
                }
            )
            item_total = (item_total + adjustment).quantize(Decimal("0.01"))
            adjustment = Decimal("0.00")
        elif adjustment < 0:
            # Use discount as a positive number
            discount_value = abs(adjustment)

        breakdown = {
            "item_total": {"currency_code": currency, "value": f"{item_total:.2f}"},
            "tax_total": {"currency_code": currency, "value": f"{tax_total:.2f}"},
        }
        if discount_value > 0:
            breakdown["discount"] = {"currency_code": currency, "value": f"{discount_value:.2f}"}

        if invoice.club:
            description = f"Club membership fee for {invoice.club.name}"[:127]
        elif invoice.auctiontos_user and invoice.auction:
            description = f"Bidder {invoice.auctiontos_user.bidder_number} in {invoice.auction.title}"[:127]
        else:
            description = "Membership fee"[:127]
        purchase_unit = {
            "description": description,
            "reference_id": str(invoice.pk),
            "amount": {
                "currency_code": currency,
                "value": f"{target_total:.2f}",
                "breakdown": breakdown,
            },
            "items": items,
        }
        if invoice.soft_descriptor:
            purchase_unit["soft_descriptor"] = invoice.soft_descriptor[:22]
        if invoice.club:
            if invoice.club.uses_own_paypal_credentials:
                # A club's own app is paid directly: no payee override, no platform fee.
                paypal_merchant_id = None
            elif invoice.club.uses_site_paypal:
                paypal_merchant_id = "admin"
            else:
                club_seller = invoice.club.effective_paypal_seller
                paypal_merchant_id = (
                    club_seller.paypal_merchant_id if club_seller and club_seller.paypal_merchant_id else None
                )
        elif invoice.auction:
            paypal_merchant_id = invoice.auction.paypal_information
        else:
            paypal_merchant_id = None
        if paypal_merchant_id and paypal_merchant_id != "admin":
            # Without a payee, payment goes to the platform account in .env.
            purchase_unit["payee"] = {"merchant_id": paypal_merchant_id}
            if settings.PAYPAL_PLATFORM_FEE and settings.PAYPAL_PLATFORM_FEE > 0:
                amt_value = Decimal(purchase_unit["amount"]["value"])
                fee_amount = (amt_value * settings.PAYPAL_PLATFORM_FEE / Decimal(100)).quantize(Decimal(0.01))
                if fee_amount > 0:
                    purchase_unit["payment_instruction"] = {
                        "platform_fees": [
                            {
                                "amount": {
                                    "currency_code": currency,
                                    "value": str(fee_amount),
                                }
                            }
                        ],
                        "disbursement_mode": "INSTANT",
                    }

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            # Forcing payment_source to the TOS email fails unless it's the buyer's primary PayPal
            # address, so it isn't set.
            "application_context": {
                "brand_name": settings.NAVBAR_BRAND,
                # The invoice uuid lets PayPalSuccessView capture with the same app that created the order.
                "return_url": self.request.build_absolute_uri(
                    reverse("paypal_success")
                    + "?"
                    + urlencode(
                        {"invoice": str(invoice.no_login_link), **({"member_pk": member_pk} if member_pk else {})}
                    )
                ),
                "cancel_url": self.request.build_absolute_uri(
                    reverse("club_detail", kwargs={"slug": invoice.club.slug})
                    if invoice.club and not invoice.auction
                    else reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link})
                ),
            },
        }

        order_data = self.post_to_paypal("v2/checkout/orders", payload)
        approval_url = None
        for link in order_data.get("links") or []:
            if link.get("rel") == "approve":
                approval_url = link.get("href")
                break
        self.order_id = order_data.get("id", "")
        if not approval_url:
            logger.error("PayPal order creation failed (platform): %s, debug_id %s", order_data, self.paypal_debug)
        return approval_url

    def handle_order(self, order_id):
        """Capture a PayPal order and process it. Returns (error_str, invoice)."""
        order_data = self.post_to_paypal(f"v2/checkout/orders/{order_id}/capture", {})
        if order_data.get("status") != "COMPLETED":
            return (
                "PayPal payment has not yet been completed, please ask the auction administrator to manually confirm payment.",
                None,
            )
        return self._process_captured_order(order_data)

    def _process_captured_order(self, order_data):
        """Process a captured PayPal order, from an API response or a webhook resource. Returns
        ``(error, invoice)``.
        """
        purchase_unit = order_data.get("purchase_units", [{}])[0]
        invoice_id = purchase_unit.get("reference_id")

        # Load invoice
        invoice = Invoice.objects.filter(pk=invoice_id).first()
        if not invoice:
            return (
                "No invoice associated with this PayPal order, please ask the auction administrator to manually confirm payment.",
                None,
            )

        capture = None
        try:
            capture = purchase_unit.get("payments", {}).get("captures", [None])[0]
        except Exception:
            capture = None

        amount_value = None
        currency = "USD"
        external_id = order_data.get("id")
        if capture:
            amount_value = capture.get("amount", {}).get("value")
            currency = capture.get("amount", {}).get("currency_code", currency)
            external_id = capture.get("id") or external_id

        # fallback to purchase_unit.amount
        if not amount_value:
            pu_amount = purchase_unit.get("amount", {}) or {}
            amount_value = pu_amount.get("value")
            currency = pu_amount.get("currency_code", currency)

        # payer info
        payer = order_data.get("payer", {}) or {}
        payer_name = None
        try:
            payer_name_parts = payer.get("name", {}) or {}
            given = payer_name_parts.get("given_name", "")
            surname = payer_name_parts.get("surname", "")
            payer_name = " ".join(p for p in (given, surname) if p).strip() or None
        except Exception:
            payer_name = None
        payer_email = payer.get("email_address")

        payer_address = None
        # prefer purchase_unit.shipping.address, fallback to payer.address
        shipping = purchase_unit.get("shipping", {}) or {}
        address_obj = (shipping.get("address") or {}) or (payer.get("address") or {})
        if address_obj:
            parts = []
            for k in (
                "address_line_1",
                "address_line_2",
                "admin_area_2",
                "admin_area_1",
                "postal_code",
                "country_code",
            ):
                v = address_obj.get(k)
                if v:
                    parts.append(v)
            if parts:
                payer_address = ", ".join(parts)

        if invoice.auctiontos_user:
            if payer_email and not invoice.auctiontos_user.email:
                invoice.auctiontos_user.email = payer_email
                invoice.auctiontos_user.save()
                if invoice.auction:
                    invoice.auction.create_history(
                        applies_to="USERS",
                        action=f"Added email {payer_email} to user {invoice.auctiontos_user.name} from PayPal payment",
                        user=None,
                    )
            if payer_address and payer_address != invoice.auctiontos_user.address:
                if invoice.auction:
                    invoice.auction.create_history(
                        applies_to="USERS",
                        action=f"Updated address for user {invoice.auctiontos_user.name} from PayPal payment.  Old address {invoice.auctiontos_user.address}",
                        user=None,
                    )
                invoice.auctiontos_user.address = payer_address[:500]
                invoice.auctiontos_user.save()
                if invoice.auctiontos_user.user and not invoice.auctiontos_user.user.userdata.address:
                    invoice.auctiontos_user.user.userdata.address = payer_address[:500]
                    invoice.auctiontos_user.user.userdata.save()

        amt = Decimal(str(amount_value)) if amount_value else Decimal("0.00")
        if not amt:
            return (
                "Unable to determine payment amount from PayPal order, please ask the auction administrator to manually confirm payment.",
                invoice,
            )
        payment, created = InvoicePayment.objects.update_or_create(
            external_id=external_id,
            defaults={
                "invoice": invoice,
                "amount": amt,
                "currency": currency,
                "payer_name": payer_name,
                "payer_email": payer_email,
                "payer_address": payer_address,
                "payment_method": "PayPal",
                "amount_available_to_refund": amt,
            },
        )
        try:
            invoice.recalculate()
        except Exception:
            logger.exception("recalculate failed for invoice %s after PayPal payment", invoice.pk)
        if created and invoice.auctiontos_user and invoice.auction:
            try:
                action = f"Payment received via PayPal for {invoice.auctiontos_user.name} ${payment.amount} ({payment.external_id})"
                invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
            except Exception:
                logger.exception("create_history failed for PayPal payment on invoice %s", invoice.pk)
        # Nothing owed on a DRAFT/UNPAID invoice: mark it PAID (rounded balance).
        if invoice.rounded_net_after_payments >= 0 and invoice.status in ("DRAFT", "UNPAID"):
            if not invoice.renewal_needed:
                try:
                    _ensure_invoice_renewal_state(invoice)
                except Exception:
                    logger.exception("Failed to ensure renewal state for invoice %s before PayPal PAID", invoice.pk)
            invoice.status = "PAID"
            invoice.save()
            try:
                _process_invoice_membership_renewal(invoice, payment_method="PayPal", external_id=payment.external_id)
            except Exception:
                logger.exception("membership renewal failed after PayPal payment on invoice %s", invoice.pk)
            if invoice.auction and invoice.auctiontos_user:
                try:
                    invoice.auction.create_history(
                        applies_to="INVOICES",
                        action=f"Invoice {invoice.auctiontos_user.name} automatically marked PAID after PayPal payment",
                    )
                except Exception:
                    logger.exception("create_history failed after PayPal payment on invoice %s", invoice.pk)
            # Only sent when paid through PayPal.
            if invoice.auction:
                try:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "invoice_paid", "pk": invoice.pk},
                    )
                except Exception:
                    logger.exception("Failed to send invoice_paid websocket for invoice %s (PayPal)", invoice.pk)

        return None, invoice

    def can_refund_invoice(self, invoice, amount):
        """Check if we can refund the given amount on this invoice via PayPal."""
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method="PayPal")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )
        # Refunds only the largest payment; partial refunds across payments aren't implemented.
        total_available = payment.amount_available_to_refund if payment else Decimal("0.00")
        if total_available >= amount:
            return True
        return False

    def refund_invoice(self, invoice, amount):
        """Refund an amount on this invoice through PayPal. Returns an error or None."""
        # Clubs on their own credentials have no webhook, so the refund would never be recorded.
        # They refund by hand in PayPal.
        if invoice.paypal_credentials:
            return (
                "Automatic refunds aren't available for this club's PayPal account. "
                "Please issue the refund manually in PayPal."
            )
        if not self.can_refund_invoice(invoice, amount):
            return "Unable to automatically refund payment"
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method="PayPal")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )
        payload = {"amount": {"value": str(amount), "currency_code": str(payment.currency)}}
        result = self.post_to_paypal(f"v2/payments/captures/{payment.external_id}/refund", payload)
        if result.get("status") != "COMPLETED":
            logger.error("PayPal refund failed: %s, debug_id: %s", result, self.paypal_debug)
            return "PayPal refund failed"
        # Recorded by the webhook (handle_refund).
        return None

    def handle_refund(self, refund_resource):
        """Record a refund webhook: find the payment via the ``up`` link's capture id and add a negative
        InvoicePayment keyed on the refund id. Returns ``(invoice, refund_payment)`` or ``(None, None)``.
        """
        refund_id = refund_resource.get("id")
        note_to_payer = refund_resource.get("note_to_payer") or refund_resource.get("note") or ""
        amount_obj = refund_resource.get("amount") or {}
        amount_value = amount_obj.get("value")
        currency = amount_obj.get("currency_code") or amount_obj.get("currency")

        # find capture id from the `up` link in the resource
        capture_id = None
        for item in refund_resource.get("links") or []:
            if item.get("rel") == "up" and item.get("href"):
                try:
                    capture_id = urlparse(item["href"]).path.rstrip("/").split("/")[-1]
                except Exception:
                    capture_id = None
                break

        if not capture_id:
            logger.warning("Refund resource missing capture 'up' link; resource=%s", refund_resource)
            return None, None

        # Find the original InvoicePayment by external_id == capture_id
        original_payment = InvoicePayment.objects.filter(external_id=capture_id).first()
        if not original_payment:
            logger.warning("No InvoicePayment found for capture id %s (refund %s)", capture_id, refund_id)
            return None, None
        invoice = original_payment.invoice

        # parse amount as Decimal and make negative
        try:
            refund_amt = Decimal(str(amount_value)) if amount_value is not None else None
        except Exception:
            logger.exception("Invalid refund amount in resource: %s", amount_value)
            refund_amt = None

        if refund_amt is None:
            logger.warning("Refund amount missing or invalid in refund resource %s", refund_id)
            return invoice, None

        refund_amt_signed = -abs(refund_amt)  # ensure negative

        # PayPal redelivers until 2xx: adjust the refundable balance only by the change, so repeats no-op.
        existing_refund = InvoicePayment.objects.filter(external_id=refund_id).first()
        previous_refund_abs = abs(existing_refund.amount) if existing_refund else Decimal("0.00")
        # Create a new InvoicePayment record for the refund.
        refund_payment, created = InvoicePayment.objects.update_or_create(
            external_id=refund_id,
            defaults={
                "invoice": invoice,
                "amount": refund_amt_signed,
                "currency": currency or original_payment.currency,
                "payer_name": (refund_resource.get("payer") or {}).get("name") or None,
                "payer_email": (refund_resource.get("payer") or {}).get("email_address") or None,
                "payer_address": None,
                "payment_method": "PayPal Refund",
                "memo": note_to_payer[:500],
            },
        )

        refund_delta = abs(refund_amt) - previous_refund_abs
        if refund_delta:
            original_payment.amount_available_to_refund -= refund_delta
            original_payment.save()

        invoice.recalculate()
        if created:
            action = f"Refund received via PayPal {refund_id} for capture {capture_id}: {refund_amt_signed} {currency}. Note: {note_to_payer}"
            invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
        return invoice, refund_payment


class PayPalConnectView(LoginRequiredMixin, PayPalAPIMixin, View):
    """Start the PayPal onboarding process for a seller"""

    def get(self, request):
        # The button is hidden when PayPal isn't enabled; guard the endpoint too.
        if not request.user.userdata.paypal_enabled:
            messages.error(request, "PayPal isn't enabled for your account.")
            return redirect(reverse("home"))
        _stash_club_for_payment_oauth(request)
        tracking_id = request.user.userdata.unsubscribe_link
        payload = {
            "tracking_id": tracking_id,
            "operations": [
                {
                    "operation": "API_INTEGRATION",
                    "api_integration_preference": {
                        "rest_api_integration": {
                            "integration_method": "PAYPAL",
                            "integration_type": "THIRD_PARTY",
                            "third_party_details": {"features": ["PAYMENT", "REFUND", "ACCESS_MERCHANT_INFORMATION"]},
                        }
                    },
                }
            ],
            "products": ["EXPRESS_CHECKOUT"],
            "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
            # take us to PayPalCallbackView when we are done
            "partner_config_override": {
                "return_url": request.build_absolute_uri(reverse("paypal_callback")),
                # "return_url_description": f"Continue on {settings.NAVBAR_BRAND}",
            },
        }
        data = self.post_to_paypal("v2/customer/partner-referrals", payload)

        # Extract the action_url from the links list
        action_url = next((link["href"] for link in data.get("links", []) if link.get("rel") == "action_url"), None)
        if not action_url:
            logger.error("PayPal onboarding failed %s, debug_id %s", data, self.paypal_debug)
            messages.error(request, "Unable to start PayPal onboarding process, please try again later.")
            return redirect(reverse("home"))
        # Redirect seller to PayPal to complete onboarding
        return redirect(action_url)


class PayPalCallbackView(LoginRequiredMixin, PayPalAPIMixin, View):
    """After onboarding, PayPal redirects here"""

    def get_success_url(self):
        # Started from a club's settings page: go back there.
        if getattr(self, "linked_club", None):
            if self.error:
                messages.error(self.request, self.error)
            else:
                messages.success(
                    self.request,
                    f"PayPal account linked to {self.linked_club.name}. Members can now pay dues directly on this site.",
                )
            return redirect(reverse("club_membership_settings", kwargs={"slug": self.linked_club.slug}))
        success_url = reverse("home")
        if self.request.user.userdata.last_auction_created:
            success_url = self.request.user.userdata.last_auction_created.get_absolute_url()
        if self.error:
            messages.error(self.request, self.error)
        else:
            messages.success(
                self.request,
                "You're all set - PayPal account linked!  Your users will see a PayPal button on invoices.",
            )
            success_url += "?enable_online_payments=True"
        return redirect(success_url)

    def get(self, request):
        self.error = None
        self.valid = False
        self.linked_club = None
        tracking_id = request.GET.get("merchantId")
        merchant_id = request.GET.get("merchantIdInPayPal")
        partner_merchant_id = settings.PARTNER_MERCHANT_ID

        if not tracking_id or not merchant_id:
            self.error = "Missing ID from PayPal callback"
            return self.get_success_url()

        data = UserData.objects.filter(unsubscribe_link=tracking_id).first()
        if not data:
            self.error = "Could not find user for PayPal onboarding"
            return self.get_success_url()
        else:
            user = data.user

        # tracking_id must belong to this user, or someone could link another user's account.
        if user != request.user:
            logger.warning(
                "PayPal callback tracking_id mismatch: tracking_id belongs to user %s but request.user is %s",
                user.pk,
                request.user.pk,
            )
            self.error = "PayPal account does not match the logged-in user"
            return self.get_success_url()

        # Fetch referral info from PayPal
        merchant_info = self.get_from_paypal(
            f"v1/customer/partners/{partner_merchant_id}/merchant-integrations/{merchant_id}"
        )
        # Checklist: payments_receivable, confirmed email, oauth_third_party.
        currency = merchant_info.get("primary_currency", "USD")
        if not merchant_info.get("payments_receivable"):
            self.error = "Attention: You currently cannot receive payments due to restriction on your PayPal account. Please resolve any issues with PayPal and re-link your account here."
            return self.get_success_url()
        if not merchant_info.get("primary_email_confirmed"):
            self.error = "Attention: Please confirm your email address on https://www.paypal.com/businessprofile/settings in order to receive payments! You currently cannot receive payments.  Re-link your account here when finished."
            return self.get_success_url()
        oauth_integrations = merchant_info.get("oauth_integrations") or []
        oauth_ok = any(
            oi.get("integration_type") == "OAUTH_THIRD_PARTY" and oi.get("oauth_third_party")
            for oi in oauth_integrations
        )
        if not oauth_ok:
            self.error = "It doesn't look like you've granted us the third-party oauth integrations permissions. Please re-link your PayPal account and be sure to accept all requested permissions."
            return self.get_success_url()

        # update model
        seller, _ = PayPalSeller.objects.get_or_create(user=user)
        seller.paypal_merchant_id = merchant_id
        seller.payer_email = merchant_info.get("primary_email") or seller.payer_email
        seller.currency = currency
        # Started from a club's settings page: link the seller to that club.
        club = _pop_club_for_payment_oauth(request)
        if club:
            # One seller per club (OneToOneField): detach the previous one.
            existing = PayPalSeller.objects.filter(club=club).exclude(pk=seller.pk).first()
            if existing:
                existing.club = None
                existing.save(update_fields=["club"])
                ClubHistory.objects.create(
                    club=club,
                    user=request.user,
                    action=f"Replaced PayPal account {existing.payer_email or existing.user}",
                    applies_to="SETTINGS",
                )
            seller.club = club
            self.linked_club = club
        seller.save()
        if club:
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Connected PayPal account {seller.payer_email or seller.user}",
                applies_to="SETTINGS",
            )
        return self.get_success_url()


def _club_membership_success_url(club, member_pk):
    """Post-payment URL for a membership invoice: member-number page, else UUID page, else club page."""
    if club and member_pk:
        member = ClubMember.objects.filter(pk=member_pk, club=club, is_deleted=False).first()
        if member and member.membership_number:
            return reverse("club_member_by_number", kwargs={"slug": club.slug, "number": member.membership_number})
        if member:
            return reverse("club_member_by_uuid", kwargs={"slug": club.slug, "uuid": member.uuid})
    if club:
        return reverse("club_detail", kwargs={"slug": club.slug})
    return None


class CreatePayPalOrderView(PayPalAPIMixin, View):
    """Create a PayPal order for an invoice and redirect to PayPal checkout"""

    def _invoice_error_redirect(self, invoice):
        if invoice.club:
            return redirect(reverse("club_membership_pay", kwargs={"slug": invoice.club.slug}))
        return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))

    def dispatch(self, request, *args, **kwargs):
        self.invoice = get_object_or_404(Invoice, no_login_link=kwargs.pop("uuid"))
        error = self.invoice.reason_for_payment_not_available
        if not self.invoice.show_paypal_button:
            error = "PayPal payments are not available"
        if error:
            messages.error(request, error)
            return self._invoice_error_redirect(self.invoice)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """Create the order"""
        member_pk = request.POST.get("member_pk") or request.GET.get("member_pk") or ""
        try:
            approval_url = self.create_order(self.invoice, member_pk=member_pk)
        except (requests.RequestException, PayPalRequestError):
            messages.error(request, "Payment provider rejected the order. Please try again or contact the organizer.")
            return self._invoice_error_redirect(self.invoice)
        if not approval_url:
            messages.error(request, "Payment provider rejected the order. Please try again or contact the organizer.")
            return self._invoice_error_redirect(self.invoice)
        return redirect(approval_url)


class PayPalSuccessView(PayPalAPIMixin, View):
    """Capture PayPal order after approval and mark payment complete"""

    def get(self, request, *args, **kwargs):
        order_id = request.GET.get("token")
        # Only the app that created the order can capture it; the return URL carries the invoice uuid.
        # The captured order's reference_id still decides which invoice is credited.
        invoice_uuid = request.GET.get("invoice")
        if invoice_uuid:
            invoice_for_creds = Invoice.objects.filter(no_login_link=invoice_uuid).first()
            if invoice_for_creds:
                self.club_paypal_credentials = invoice_for_creds.paypal_credentials
        error, invoice = self.handle_order(order_id)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Payment completed successfully. Thank you!")
        member_pk = request.GET.get("member_pk") or ""
        if invoice and invoice.club:
            return redirect(_club_membership_success_url(invoice.club, member_pk))
        if invoice:
            return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))
        return redirect(reverse("home"))


class PayPalInfoView(TemplateView):
    template_name = "auctions/paypal_seller.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context["seller"] = PayPalSeller.objects.filter(user=self.request.user).first()
            context["auction"] = self.request.user.userdata.last_auction_created
        else:
            context["seller"] = None
            context["auction"] = None
        return context


class PayPalSellerDeleteView(LoginRequiredMixin, DeleteView):
    template_name = "auctions/paypal_seller_confirm_delete.html"
    model = PayPalSeller

    def get_object(self, queryset=None):
        return get_object_or_404(PayPalSeller, user=self.request.user)

    def get_success_url(self):
        return reverse("paypal_seller")


class SquareAPIMixin:
    """Square payment links, through SquareSeller methods. OAuth only."""

    def create_payment_link(self, invoice, member_pk=""):
        """Create a Square payment link. Returns ``(payment_url, error_message)``."""
        if invoice.club:
            seller = invoice.club.effective_square_seller
        elif invoice.auction:
            # Auction invoices have club=None; route through the auction or a club auction charges
            # the creator's personal account.
            seller = invoice.auction.effective_square_seller
        else:
            seller = None
        if not seller:
            return None, "Seller has not connected their Square account"

        return seller.create_payment_link(invoice, self.request, member_pk=member_pk)


class SquareConnectView(LoginRequiredMixin, View):
    """Start the Square OAuth process for a seller"""

    def get(self, request):
        # Square access must be granted before onboarding (a fraud control). Land on square_seller,
        # where the gate is explained and access can be requested.
        if not request.user.userdata.square_enabled:
            messages.error(
                request,
                "We review each account by hand before enabling card payments - ask us and we'll "
                "usually have you set up the same day.",
            )
            return redirect(reverse("square_seller"))
        # Remember the flow started in the app, so the callback can return to it. ?return_to_app=1
        # is the app's explicit signal from a browser view with no session.
        if session_opened_by_app(request) or request.GET.get("return_to_app"):
            mark_session_opened_by_app(request.session)
        _stash_club_for_payment_oauth(request)
        # The user's unsubscribe_link is the OAuth state.
        state = request.user.userdata.unsubscribe_link

        square_auth_url = (
            "https://connect.squareupsandbox.com/oauth2/authorize"
            if settings.SQUARE_ENVIRONMENT == "sandbox"
            else "https://connect.squareup.com/oauth2/authorize"
        )

        # Must match the Square app config and the token exchange.
        redirect_uri = request.build_absolute_uri(reverse("square_callback"))
        # Build OAuth parameters
        params = {
            "client_id": settings.SQUARE_APPLICATION_ID,
            "scope": " ".join(SQUARE_OAUTH_SCOPES),
            "state": state,
            # "session": "false",  # Don't require login if already logged in
            "redirect_uri": redirect_uri,
        }

        # Build redirect URL
        oauth_url = f"{square_auth_url}?{urlencode(params)}"
        return redirect(oauth_url)


class SquareCallbackView(LoginRequiredMixin, View):
    """Square's OAuth redirect."""

    def get(self, request):
        # Get authorization code and state from Square
        code = request.GET.get("code")
        state = request.GET.get("state")
        error = request.GET.get("error")
        error_description = request.GET.get("error_description")
        if error:
            messages.error(request, f"Square authorization failed: {error_description or error}")
            return redirect(reverse("square_seller"))

        if not code or not state:
            messages.error(request, "Missing authorization code from Square")
            return redirect(reverse("square_seller"))

        # Verify state matches user's unsubscribe_link for security
        if state != request.user.userdata.unsubscribe_link:
            messages.error(request, "Invalid state parameter - please try again")
            return redirect(reverse("square_seller"))

        # Exchange authorization code for access token
        try:
            from square import Square
            from square.client import SquareEnvironment

            # Determine environment
            env = (
                SquareEnvironment.SANDBOX if settings.SQUARE_ENVIRONMENT == "sandbox" else SquareEnvironment.PRODUCTION
            )
            # No token for the exchange; an empty string causes "Illegal header value".
            client = Square(environment=env)

            # Must match the authorization request.
            redirect_uri = request.build_absolute_uri(reverse("square_callback"))

            result = client.o_auth.obtain_token(
                client_id=settings.SQUARE_APPLICATION_ID,
                client_secret=settings.SQUARE_CLIENT_SECRET,
                code=code,
                grant_type="authorization_code",
                redirect_uri=redirect_uri,
            )
            # Raises on error.
            access_token = result.access_token
            refresh_token = result.refresh_token if hasattr(result, "refresh_token") else None
            expires_at = result.expires_at if hasattr(result, "expires_at") else None
            merchant_id = result.merchant_id if hasattr(result, "merchant_id") else None

            if not access_token or not merchant_id:
                logger.error("Square OAuth token exchange failed: Missing required fields in response")
                messages.error(request, "Failed to connect Square account. Please try again.")
                return redirect(reverse("square_seller"))

            merchant_client = Square(
                environment=env,
                token=access_token,
            )

            list_resp = merchant_client.merchants.get("me")
            email = getattr(list_resp, "owner_email", None)
            currency = getattr(list_resp, "currency", "USD")

            # Save or update SquareSeller
            seller, created = SquareSeller.objects.get_or_create(user=request.user)
            seller.square_merchant_id = merchant_id
            seller.access_token = access_token
            seller.refresh_token = refresh_token
            # Square OAuth grants the full requested set; supports_tap_to_pay reads this.
            seller.scopes = " ".join(SQUARE_OAUTH_SCOPES)
            if expires_at:
                from datetime import datetime

                # Handle ISO 8601 format
                try:
                    seller.token_expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    # If expires_at is already a datetime object
                    if isinstance(expires_at, datetime):
                        seller.token_expires_at = expires_at
            seller.currency = currency
            seller.payer_email = email
            club = _pop_club_for_payment_oauth(request)
            if club:
                existing = SquareSeller.objects.filter(club=club).exclude(pk=seller.pk).first()
                if existing:
                    existing.club = None
                    existing.save(update_fields=["club"])
                    ClubHistory.objects.create(
                        club=club,
                        user=request.user,
                        action=f"Replaced Square account {existing.payer_email or existing.user}",
                        applies_to="SETTINGS",
                    )
                seller.club = club
            seller.save()
            if club:
                ClubHistory.objects.create(
                    club=club,
                    user=request.user,
                    action=f"Connected Square account {seller.payer_email or seller.user}",
                    applies_to="SETTINGS",
                )
                messages.success(
                    request,
                    f"Square account linked to {club.name}. Members can now pay dues directly on this site.",
                )
                return self._done(request, reverse("club_membership_settings", kwargs={"slug": club.slug}), seller)

            messages.success(
                request,
                "You're all set - Square account linked! Your users will see a Square button on invoices.",
            )

            # Redirect to last auction or home
            if request.user.userdata.last_auction_created:
                return self._done(
                    request,
                    request.user.userdata.last_auction_created.get_absolute_url() + "?enable_square_payments=True",
                    seller,
                )
            return self._done(request, reverse("square_seller"), seller)

        except Exception as e:
            logger.exception("Error during Square OAuth: %s", e)
            # Provide more specific error message if it's an API error
            if hasattr(e, "body") and isinstance(e.body, dict):
                error_msg = e.body.get("message", str(e))
                error_type = e.body.get("type", "unknown")
                logger.error("Square OAuth API Error: type=%s, message=%s", error_type, error_msg)
                messages.error(
                    request, f"Square OAuth failed: {error_msg}. Please check your Square application settings."
                )
            else:
                messages.error(request, "An error occurred connecting your Square account. Please try again.")
            return redirect(reverse("square_seller"))

    @staticmethod
    def _done(request, web_url, seller):
        """End a successful connect: a confirmation page if the app started it, else ``web_url``.

        Apple's Tap to Pay review wants onboarding finished in the app (2.2), but the OAuth exchange is
        server-side, so the merchant lands on a web page. It redirects to
        ``fishauctions-oauth://square-connected``, which the app's auth session closes on, and says "tap
        Done" for anything else. Deliberately not ``fishauctions://``, which only the app's WebView sees.
        See ``square_connected_app.html``.
        """
        if not session_opened_by_app(request):
            return redirect(web_url)
        return render(
            request,
            "auctions/square_connected_app.html",
            {"seller": seller, "web_url": web_url},
        )
