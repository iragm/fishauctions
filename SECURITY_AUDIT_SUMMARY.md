# Security Audit Summary

## Issue
Review each file and each URL to ensure that auction admin users can see names, emails, and other user information for their auctions, while unauthenticated users and non-admin users cannot access this information.

## Findings

### Critical Security Issues Fixed

#### 1. Missing Authentication on Autocomplete Views
**Risk Level: High**

**Issue:** The following autocomplete views were missing `LoginRequiredMixin`, allowing unauthenticated users to access the endpoints (even though permission_check would return empty results):
- `AuctionTOSAutocomplete` - Exposes bidder numbers and names
- `LotAutocomplete` - Exposes lot information including high bidder data

**Fix:** Added `LoginRequiredMixin` to both views to require authentication before accessing the endpoints.

**Files Changed:** `auctions/views.py`

#### 2. Missing Authentication on Admin Action Views
**Risk Level: High**

**Issue:** Multiple admin-only views were missing `LoginRequiredMixin`, relying only on `AuctionViewMixin.is_auction_admin` property. While this prevented unauthorized access, it's a defense-in-depth issue:

- `LotDeactivate` - Allows lot activation/deactivation
- `ImagesPrimary` - Sets primary image for lots
- `ImagesRotate` - Rotates lot images
- `AuctionChatDeleteUndelete` - Deletes/undeletes chat messages
- `AuctionShowHighBidder` - Reveals max bid information
- `AuctionUnsellLot` - Removes winner from lots

**Fix:** Added `LoginRequiredMixin` to all these views to require authentication as the first line of defense.

**Files Changed:** `auctions/views.py`

#### 3. Missing Authentication on Auction Admin Views
**Risk Level: High**

**Issue:** Multiple auction administration views that handle sensitive AuctionTOS and user data were missing `LoginRequiredMixin`:

- `PickupLocations` - Shows pickup locations for auction
- `PickupLocationsDelete` - Deletes pickup locations
- `PickupLocationsUpdate` - Updates pickup locations
- `PickupLocationsCreate` - Creates pickup locations
- `AuctionUpdate` - Edits auction settings
- `AuctionHistoryView` - Shows auction history with user actions
- `AuctionLots` - Lists all lots with seller/winner information
- `AuctionHelp` - Admin help page
- `AuctionUsers` - Lists all users/AuctionTOS entries (CRITICAL)
- `DynamicSetLotWinner` - Sets lot winners
- `BulkAddUsers` - Bulk adds users to auction (CRITICAL)
- `ImportFromGoogleDrive` - Imports user data from Google Drive (CRITICAL)
- `BulkAddLots` - Bulk adds lots
- `AuctionDelete` - Deletes auction
- `LotAdmin` - Lot administration
- `AuctionTOSAdmin` - Manages AuctionTOS entries (CRITICAL)
- `AuctionTOSDelete` - Deletes AuctionTOS entries (CRITICAL)
- `InvoiceCreateView` - Creates invoices
- `BulkSetLotsWon` - Sets multiple lots as won
- `InvoiceBulkUpdateStatus` - Updates invoice statuses
- `LotRefundDialog` - Issues refunds
- `AuctionStatsBarChartJSONView` - Provides auction statistics data
- `AuctionLabelConfig` - Configures label printing

**Fix:** Added `LoginRequiredMixin` to all these views. The `AuctionViewMixin.is_auction_admin` property provides the second layer of authorization (ensuring the user is an admin of the specific auction).

**Files Changed:** `auctions/views.py`

## Security Model

The application now properly implements a two-layer security model:

1. **Authentication Layer** (`LoginRequiredMixin`): Ensures the user is logged in
2. **Authorization Layer** (`AuctionViewMixin.is_auction_admin`): Ensures the logged-in user has permission to access the specific auction's data

### Permission Check Flow

```python
# Step 1: LoginRequiredMixin ensures user is authenticated
# Step 2: AuctionViewMixin.is_auction_admin checks if user can access auction
def is_auction_admin(self):
    result = self.auction.permission_check(self.request.user)
    if not result and not self.allow_non_admins:
        raise PermissionDenied()
    return result

# Step 3: Auction.permission_check determines if user is admin
def permission_check(self, user):
    # User is auction creator
    if self.created_by == user:
        return True
    # User is superuser
    if user.is_superuser:
        return True
    # User is marked as admin in AuctionTOS
    if AuctionTOS.objects.filter(is_admin=True, user=user, auction=self.pk).exists():
        return True
    return False
```

## Views That Intentionally Allow Non-Admin Access

The following views properly use `allow_non_admins = True` and have appropriate permission checks:

- `ViewLot` - Public lot viewing (with admin-only features gated by `is_auction_admin`)
- `AuctionInfo` - Public auction information page
- `InvoiceView` - Allows users to view their own invoices (checks ownership)
- `InvoiceNoLoginView` - Allows invoice access via UUID link (for users without accounts)
- `LotLabelView` - Allows users to print their own labels (uses `@login_required` in URLs)

## Data Exposure Summary

### Protected Data (Admin Only)
The following sensitive data is now properly protected and only accessible to auction admins:

- **AuctionTOS Information:**
  - User names
  - Email addresses
  - Phone numbers
  - Physical addresses
  - Bidder numbers
  - Memo fields (admin notes)
  - Pickup locations
  - Club membership status

- **User Activity Data:**
  - Lot submission history
  - Bidding history
  - Invoice information
  - Chat messages
  - Page view statistics

- **Auction Administration:**
  - User management (add/edit/delete)
  - Invoice management
  - Lot winner assignment
  - Refund processing

### Public Data (Anyone Can Access)
The following data remains publicly accessible as intended:

- Auction title, description, dates
- Public lot listings with images
- Current bid amounts (for non-sealed bids)
- Seller username (not email/phone/address)
- Auction location (general area, not pickup addresses)

## Testing

A comprehensive security test suite was created in `auctions/test_security.py` that tests:

1. Unauthenticated users cannot access any admin views
2. Authenticated non-admin users cannot access admin views
3. Auction admins can access views for their auctions only
4. Auction creators can access views for their auctions
5. Admins cannot access data from other auctions they don't manage

### Test Coverage

The test suite includes tests for:
- AuctionTOS autocomplete endpoint
- AuctionTOS admin CRUD operations
- Auction users list
- Auction report CSV export
- Bulk user operations
- Email composition to users
- Memo field updates
- AuctionTOS validation endpoint

## Recommendations

1. **Code Review:** All views should be reviewed to ensure they properly inherit from `LoginRequiredMixin` when they access authenticated user data
2. **Regular Audits:** Periodic security audits should be performed, especially when adding new views
3. **Template Checks:** Review templates to ensure they properly use `{% if is_auction_admin %}` tags to hide admin-only features
4. **API Endpoints:** All API endpoints (URLs starting with `/api/`) should be carefully reviewed for authentication and authorization
5. **Defense in Depth:** Continue the practice of having both authentication (LoginRequiredMixin) and authorization (AuctionViewMixin) layers

## Files Modified

- `auctions/views.py` - Added `LoginRequiredMixin` to 28 view classes
- `auctions/test_security.py` - Created comprehensive security test suite (new file)

## Verification

To verify the fixes:

1. Run the security test suite:
   ```bash
   docker exec -it django python3 manage.py test auctions.test_security
   ```

2. Manually test key scenarios:
   - Attempt to access `/api/auctiontos-autocomplete/` without logging in
   - Attempt to access `/auctions/<slug>/users/` as a non-admin user
   - Verify auction admins can access all admin features
   - Verify users cannot access data from auctions they don't manage

## Conclusion

All identified security issues have been addressed. The application now properly enforces that:
- ✅ Auction admin users can see names, emails, and other user information for their auctions
- ✅ Unauthenticated users cannot access sensitive user information
- ✅ Non-admin authenticated users cannot access sensitive user information
- ✅ Admins can only access data for auctions they manage

The fixes follow Django security best practices and implement proper defense-in-depth with both authentication and authorization layers.
