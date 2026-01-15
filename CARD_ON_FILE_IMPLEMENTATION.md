# Square Card-on-File Implementation

## Overview

This document describes the implementation of card-on-file functionality for Square payments to support pre-authorization capabilities (issue #642).

## Purpose

The card-on-file feature allows users to save payment methods with sellers for:
- Pre-authorizations before auction participation
- Streamlined checkout experiences
- Recurring payments
- Automated charge processing

## Architecture

### Database Schema

#### SquareCustomerCard Model

A new model that stores references to Square customer profiles and saved payment methods.

**Fields:**
- `user` (ForeignKey): Django user who owns the card
- `square_seller` (ForeignKey): The seller's Square account
- `square_customer_id` (EncryptedCharField): Square customer ID
- `square_card_id` (EncryptedCharField): Square card payment method token
- `card_last_4` (CharField): Last 4 digits for display
- `card_brand` (CharField): Card brand (VISA, MASTERCARD, etc.)
- `card_exp_month` (PositiveIntegerField): Expiration month
- `card_exp_year` (PositiveIntegerField): Expiration year  
- `is_active` (BooleanField): Whether card is currently active
- `created_on` (DateTimeField): When card was added
- `updated_on` (DateTimeField): Last update timestamp

**Security:**
- Only stores tokenized references (customer_id, card_id), never actual card data
- Sensitive identifiers encrypted at rest using `EncryptedCharField`
- Follows PCI DSS compliance by delegating card data to Square
- Unique constraint: one customer record per user per seller

### API Integration

#### SquareSeller Model Extensions

Three new methods added to the `SquareSeller` model:

##### 1. `get_or_create_customer(user, email=None)`
Creates or retrieves a Square customer profile for a user.

**Flow:**
1. Check if customer record already exists in database
2. If exists, verify customer still exists in Square
3. If not exists, create new customer via Square Customers API
4. Save customer ID to database
5. Return customer ID

**Parameters:**
- `user`: Django User object
- `email`: Optional email address (defaults to user.email)

**Returns:**
- `(customer_id, error_message)` tuple

##### 2. `save_card_on_file(user, card_nonce, email=None)`
Saves a card on file using a card nonce from Square Web Payments SDK.

**Flow:**
1. Get or create Square customer
2. Create card using the nonce via Square Cards API
3. Extract card metadata (last 4, brand, expiration)
4. Save card details to database
5. Return card ID

**Parameters:**
- `user`: Django User object
- `card_nonce`: Card nonce from Square Web Payments SDK
- `email`: Optional email address

**Returns:**
- `(card_id, error_message)` tuple

##### 3. `charge_card_on_file(user, amount, invoice=None, idempotency_key=None)`
Charges a saved card on file.

**Flow:**
1. Retrieve active customer card from database
2. Check if card is expired
3. Get seller's location ID
4. Create payment via Square Payments API using card_id
5. Handle errors (expired card, insufficient funds, etc.)
6. Return payment ID

**Parameters:**
- `user`: Django User object
- `amount`: Decimal amount to charge
- `invoice`: Optional Invoice object for reference
- `idempotency_key`: Optional idempotency key

**Returns:**
- `(payment_id, error_message)` tuple

## Implementation Details

### Minimal Changes

The implementation follows the principle of minimal modifications to existing code:

1. **New Model Only**: Added `SquareCustomerCard` model without modifying existing models
2. **Extension Methods**: Added methods to `SquareSeller` without changing existing methods
3. **No View Changes**: Implementation is backend-only, views can be added later
4. **Backwards Compatible**: Existing payment link flow remains unchanged

### Security Considerations

1. **Encryption**: Customer and card IDs encrypted using `django-encrypted-model-fields`
2. **PCI Compliance**: No card data (numbers, CVV) stored locally
3. **Token-Based**: Only Square-provided tokens stored
4. **Expiration Checking**: Automatic detection and marking of expired cards
5. **Error Handling**: Comprehensive error messages without exposing sensitive data

### Testing

Comprehensive test suite added in `auctions/tests.py`:

- `SquareCustomerCardTests` class with 7 test cases:
  - Model creation and validation
  - Unique constraint enforcement  
  - Display methods (string representation, admin display)
  - Expiration date checking
  - Related name access (user.square_cards, seller.customer_cards)
  - Admin interface integration

## Usage Example

```python
from auctions.models import SquareSeller, SquareCustomerCard
from django.contrib.auth.models import User

# Get seller and user
seller = SquareSeller.objects.get(user__username='seller')
buyer = User.objects.get(username='buyer')

# Save a card (in a view, card_nonce comes from Square Web Payments SDK)
card_id, error = seller.save_card_on_file(
    user=buyer,
    card_nonce='cnon_1234567890abcdef',  # From Square JS SDK
    email='buyer@example.com'
)

if error:
    print(f"Error saving card: {error}")
else:
    print(f"Card saved successfully: {card_id}")
    
# Charge the card later
payment_id, error = seller.charge_card_on_file(
    user=buyer,
    amount=Decimal('50.00'),
    invoice=some_invoice
)

if error:
    print(f"Error charging card: {error}")
else:
    print(f"Payment successful: {payment_id}")
```

## Square API Endpoints Used

1. **Customers API**:
   - `POST /v2/customers` - Create customer
   - `GET /v2/customers/{customer_id}` - Retrieve customer

2. **Cards API**:
   - `POST /v2/cards` - Create card from nonce

3. **Payments API**:
   - `POST /v2/payments` - Charge card on file

## Future Enhancements

To complete card-on-file functionality for pre-authorizations, these additions would be needed:

1. **Views & Templates**:
   - Card management page for users
   - Add card flow with Square Web Payments SDK
   - Remove card functionality

2. **Pre-Authorization Logic**:
   - Auction setting for pre-auth requirements
   - Pre-auth window (e.g., 4 hours before auction)
   - Bid blocking if pre-auth insufficient
   - Auto-capture on invoice completion

3. **Webhook Handlers**:
   - Handle card expiration notifications
   - Handle customer deletion events
   - Update card status based on Square events

4. **Admin Features**:
   - View customer cards in admin panel
   - Manually mark cards inactive
   - Refund via saved card

## Complexity Assessment

**Level: Medium**

**Pros:**
- ✅ Clean API design with Square SDK v43
- ✅ Existing OAuth infrastructure handles authentication
- ✅ Encrypted storage already configured
- ✅ Webhook framework exists
- ✅ Payment flow patterns established

**Cons:**
- ⚠️ Requires frontend integration with Square Web Payments SDK
- ⚠️ Additional webhook handlers needed
- ⚠️ Pre-auth timing logic complex
- ⚠️ Card expiration management ongoing maintenance
- ⚠️ Testing requires Square sandbox environment

**Estimated Effort:**
- Backend foundation (this PR): **Complete**
- Frontend card management UI: **2-3 days**
- Pre-auth business logic: **3-5 days**
- Webhook handlers: **1-2 days**
- Testing & refinement: **2-3 days**
- **Total**: ~10-15 days for full implementation

## Migration

The database migration `0220_add_square_customer_card_model.py` creates the new table with proper indexes and constraints. No data migration needed as this is a new feature.

Run migration:
```bash
python manage.py migrate auctions
```

## References

- Square Customers API: https://developer.squareup.com/reference/square/customers-api
- Square Cards API: https://developer.squareup.com/reference/square/cards-api  
- Square Payments API: https://developer.squareup.com/reference/square/payments-api
- Square Web Payments SDK: https://developer.squareup.com/docs/web-payments/overview
- Issue #642: https://github.com/iragm/fishauctions/issues/642
