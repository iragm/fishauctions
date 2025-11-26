# Summary: Square Invoice Math Issue Fix

## Issue
**Title**: Square invoice math

**Description**: 
> User buys a lot for $1
> User pays $1 with square
> Payment OK
> Admin refunds the user $0.50
> Invoice now shows that that the club owes the user $0.50.

## Root Cause Analysis

The issue occurred when the Square refund API call failed but the lot's `partial_refund_percent` was still updated.

### How it happened:
1. User buys lot for $1 and pays via Square
2. Admin attempts 50% refund through the system
3. `Lot.refund()` method calls `square_refund()` to process the refund via Square API
4. Square API call fails (network error, API error, etc.)
5. **BUG**: Despite the failure, `partial_refund_percent` was still set to 50%
6. This reduced the buyer's `total_bought` from $1 to $0.50
7. But since the Square refund didn't process, no negative `InvoicePayment` was created
8. Result: `net_after_payments = -$0.50 + $1.00 = +$0.50` (club owes user)

## Solution

Modified the `Lot.refund()` method in `auctions/models.py` to:
1. Track whether the Square refund failed with a `square_refund_failed` flag
2. Only update `partial_refund_percent` if the Square refund succeeded OR wasn't attempted
3. If Square refund fails, log the error and create a LotHistory entry, but don't update `partial_refund_percent`

### Code Change
**File**: `auctions/models.py`
**Lines**: 3653-3685
**Changes**: 
- Added `square_refund_failed` flag to track refund failures
- Moved `partial_refund_percent` update into conditional block
- Only updates `partial_refund_percent` if Square refund didn't fail

## Impact

### Before Fix
- Square refund API failures caused invoice calculation errors
- Invoices would incorrectly show "club owes user" when they actually didn't
- Required manual database fixes to correct invoices

### After Fix
- Square refund failures leave the invoice unchanged
- Admin sees error message and can retry the refund
- Invoice calculations remain accurate and balanced
- LotHistory still records the attempted refund with error message

## Testing

Created comprehensive test suite in `auctions/test_square_refund_math.py` with 6 test cases:

1. **test_buyer_invoice_before_refund**: Verify initial state is correct
2. **test_seller_invoice_before_refund**: Verify seller invoice calculations
3. **test_square_refund_success_updates_partial_refund_percent**: Normal refund flow
4. **test_square_refund_failure_does_not_update_partial_refund_percent**: **Verifies the fix**
5. **test_manual_refund_without_square_payment**: Cash/check refunds still work
6. **test_issue_scenario_square_refund_and_partial_refund_percent_both_applied**: End-to-end verification
7. **test_issue_scenario_partial_refund_percent_set_without_square_refund**: Demonstrates the bug

## Files Changed

1. **auctions/models.py**: Fixed `Lot.refund()` method (12 lines changed)
2. **auctions/test_square_refund_math.py**: New test file (257 lines)
3. **SQUARE_REFUND_FIX.md**: Detailed technical documentation (90 lines)

## Edge Cases Handled

1. **Square refund succeeds**: ✓ Works as before
2. **Square refund fails**: ✓ Now prevents invoice imbalance (the fix)
3. **No Square payment (manual refund)**: ✓ Still allows partial_refund_percent update
4. **Refund amount is 0**: ✓ Clears the refund
5. **Refund amount equals current value**: ✓ No-op, just saves
6. **Multiple refunds**: ✓ Prevented by `no_more_refunds_possible` flag

## Minimal Change Approach

This fix adheres to the principle of making the smallest possible change:
- Only modified the `Lot.refund()` method
- Preserved all existing behavior except for the bug scenario
- No changes to database schema
- No changes to webhook processing
- No changes to invoice calculation logic
- Existing tests continue to pass

## Security Considerations

- No new security vulnerabilities introduced
- Change only affects error handling path
- Prevents invoice manipulation via failed API calls
- LotHistory continues to audit all refund attempts

## Backward Compatibility

✓ Fully backward compatible:
- No database migrations required
- No API changes
- Existing invoices not affected
- Manual refunds (non-Square) continue to work

## Recommendation for Deployment

1. Deploy the fix to production
2. Monitor error logs for Square refund failures
3. If refund failures occur, admins can retry manually
4. Consider adding retry logic for transient failures (future enhancement)
