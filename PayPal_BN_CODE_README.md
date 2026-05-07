# PayPal Platform Partner BN Code Requirements

This document outlines the requirements to become an approved PayPal platform partner with a BN (Business/Partner Attribution) code, comparing the current implementation against PayPal's requirements.

## Overview

To become an approved platform partner with a BN code, you must:
1. Complete the PayPal integration checklist
2. Pass PayPal's review process
3. Be assigned a BN code by PayPal

The BN code enables revenue attribution - PayPal tracks transactions originated by partners and includes them in partner reports/commission calculations.

---

## Current Implementation Status

| Requirement | Status | Implementation |
|-------------|--------|---------------|
| Partner Referrals API | ✅ Done | `PayPalConnectView` (line 8029) |
| BN Code in Headers | ✅ Done | `_build_paypal_headers()` adds `PayPal-Partner-Attribution-Id` |
| Webhook Handler | ⚠️ Partial | `PayPalWebhookView` exists but incomplete |
| Partner Merchant ID | ⚠️ Config | `PARTNER_MERCHANT_ID` in settings (required) |
| Webhook Verification | ⚠️ Partial | Code exists but may not be wired up correctly |
| Integration Checklist | ✅ Done | Validates in `PayPalCallbackView` |

---

## Required Environment Variables

Create these in your `.env` file:

```bash
# Required for BN code tracking
PAYPAL_BN_CODE=YOUR_BN_CODE_FROM_PAYPAL
PARTNER_MERCHANT_ID=YOUR_MERCHANT_ID

# PayPal API Credentials (must be from a PayPal business account)
PAYPAL_CLIENT_ID=your_client_id
PAYPAL_CLIENT_SECRET=your_client_secret

# Webhook (required for full platform partner approval)
PAYPAL_WEBHOOK_ID=your_webhook_id

# Optional: Platform fee (percentage)
PAYPAL_PLATFORM_FEE=0

# Enable PayPal for users
PAYPAL_ENABLED_FOR_USERS=True
```

**Note:** `PARTNER_MERCHANT_ID` currently has no default and will default to empty string. This must be set for the partner integration to work.

---

## Integration Checklist Requirements

Per [PayPal's Integration Checklist](https://developer.paypal.com/docs/multiparty/integration-checklist/), the following must be implemented:

### 1. Partner Referral API ✅

**Requirement:** Use the Partner Referral API to onboard sellers.

**Current Implementation:**
- `PayPalConnectView` (views.py:8029-8065) creates Partner Referrals
- Payload includes operations with `API_INTEGRATION` and `THIRD_PARTY` integration type
- Features requested: `PAYMENT`, `REFUND`, `ACCESS_MERCHANT_INFORMATION`

**Status:** ✅ Complete

### 2. Create Order After Button Click ✅

**Requirement:** Call Create Order API only after buyer clicks PayPal checkout button.

**Current Implementation:**
- `CreatePayPalOrderView` is called when user initiates PayPal checkout
- Order is created server-side after button click

**Status:** ✅ Complete

### 3. BN Code in All Payments ✅

**Requirement:** Pass BN code (`PayPal-Partner-Attribution-Id`) with each payment.

**Current Implementation:**
- `_build_paypal_headers()` (views.py:7603-7610) adds the header:
  ```python
  if include_bn_code and getattr(settings, "PAYPAL_BN_CODE", None):
      headers["PayPal-Partner-Attribution-Id"] = settings.PAYPAL_BN_CODE
  ```
- Applied to all API calls via `_paypal_request()`

**Status:** ✅ Complete

### 4. Direct Checkout Flow ✅

**Requirement:** Checkout button takes buyers directly to PayPal without intermediate steps.

**Current Implementation:**
- `CreatePayPalOrderView` creates order, returns redirect URL
- Buyer redirected directly to PayPal for payment

**Status:** ✅ Complete

### 5. Debug ID Storage 🔲

**Requirement:** Store `Paypal-Debug-Id` header returned in API responses.

**Current Implementation:**
- `paypal_debug` is captured from response headers (line 7637)
- Used in error logging but not persistently stored

**Status:** ⚠️ May need to store for support/review

### 6. Webhook Configuration 🔲

**Requirement:** Set up webhooks and handle events.

**Current Implementation:**
- `PayPalWebhookView` exists at line 11052
- Attempts webhook signature verification
- Handles `MERCHANT.ONBOARDING.COMPLETED` and `MERCHANT.PARTNER-CONSENT.REVOKED`

**Issues:**
1. `webhook_id` is commented out in verification payload (line 11108)
2. Uses wrong endpoint for OAuth token (sends verify_payload instead of grant_type)
3. Doesn't validate webhook_id is configured before attempting verification

**Status:** ⚠️ Needs fixes

### 7. Transaction Recording 🔲

**Requirement:** Record test transaction IDs and debug IDs for PayPal review.

**Current Implementation:**
- `InvoicePayment` model stores external_id (transaction ID)
- `paypal_debug` from responses logged but not stored with transaction

**Status:** ⚠️ Should record debug IDs alongside transactions

---

## What's Missing / Needs Work

### 1. PARTNER_MERCHANT_ID Must Be Set

The code uses `settings.PARTNER_MERCHANT_ID` (lines 8090, 8105) but there's no required validation in settings.

**Action Required:**
- Set `PARTNER_MERCHANT_ID` in environment
- Add validation during startup that it's configured when PayPal is enabled

### 2. Webhook Verification is Broken

The current webhook verification code has issues:

```python
# Current (broken):
response = self.post_to_paypal("/v1/oauth2/token", payload=verify_payload)  # Wrong!
platform_token = response.get("access_token", None)

# Verify endpoint also wrong - sends empty payload
verify_data = self.post_to_paypal("/v1/notifications/verify-webhook-signature", payload={})
```

**Should be:**
1. Get OAuth token using client credentials grant
2. Include webhook_id in verification
3. Pass verification payload correctly

### 3. Missing Required Features in Onboarding

The current third_party_details requests:
```python
"features": ["PAYMENT", "REFUND", "ACCESS_MERCHANT_INFORMATION"]
```

May need to add more features per PayPal requirements:
```python
"features": [
    "PAYMENT",
    "REFUND", 
    "ACCESS_MERCHANT_INFORMATION",
    # Potentially additional scopes
]
```

### 4. PAYPAL_WEBHOOK_ID Not Configured

- No validation this is set
- Not included in webhook verification
- Required for production approval

### 5. Missing Integration Features

The callback validates:
- ✅ `payments_receivable`
- ✅ `primary_email_confirmed`
- ✅ `oauth_third_party`

But doesn't check for all required capabilities in the response.

---

## Steps to Complete

### Priority 1: Configuration

1. [ ] Set `PARTNER_MERCHANT_ID` in `.env` - your PayPal merchant ID
2. [ ] Set `PAYPAL_BN_CODE` - will be provided by PayPal when you apply
3. [ ] Register webhook in PayPal developer dashboard and set `PAYPAL_WEBHOOK_ID`

### Priority 2: Fix Webhook Verification

2. [ ] Fix `PayPalWebhookView.post()` to properly verify webhook signatures:
   - Get access token using client credentials grant (not the verification payload)
   - Include `webhook_id` in verification payload
   - Pass the correct verification payload to verify-webhook-signature

### Priority 3: Record Debug IDs

3. [ ] Store `Paypal-Debug-Id` with transaction records for support
4. [ ] Record test transaction IDs for PayPal review

### Priority 4: Apply for Approval

5. [ ] Complete sandbox testing with all seller flows
6. [ ] Fill out PayPal's partner form: https://developer.paypal.com/docs/multiparty/#tell-us-about-your-platform
7. [ ] Contact PayPal representative for review

---

## Testing Checklist

Before applying, verify in sandbox:

- [ ] New seller can complete onboarding flow
- [ ] Existing seller can reconnect after disconnecting
- [ ] Seller disconnection via PayPal triggers webhook (or manual unlink)
- [ ] Payment creates order and captures successfully
- [ ] Refunds work correctly
- [ ] Webhook events are processed correctly
- [ ] BN code appears in API request headers (check PayPal dashboard)

---

## References

- [PayPal Multiparty Documentation](https://developer.paypal.com/docs/multiparty/)
- [Seller Onboarding Before Payment](https://developer.paypal.com/docs/multiparty/seller-onboarding/before-payment/)
- [Integration Checklist](https://developer.paypal.com/docs/multiparty/integration-checklist/)
- [Partner Program FAQs](https://www.paypal.com/webapps/mpp/partner-programme/faqs)