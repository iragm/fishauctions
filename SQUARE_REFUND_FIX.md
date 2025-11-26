# Square Invoice Math Issue - Fix Documentation

## Problem Description

When an admin issues a refund on a lot that was paid for via Square, the invoice calculations could become incorrect if the Square refund API call failed.

### Root Cause

The `Lot.refund()` method would:
1. Attempt to process a Square refund via the Square API
2. **ALWAYS** set `partial_refund_percent` on the lot, regardless of whether the Square refund succeeded or failed
3. Wait for a Square webhook to create the negative `InvoicePayment` record

If the Square refund failed (API error, network issue, etc.), step 2 would still execute, causing an imbalance:
- The lot's `partial_refund_percent` would be updated, reducing the buyer's `total_bought` in invoice calculations
- But no negative `InvoicePayment` would be created (since the Square refund didn't process)
- Result: `net_after_payments = reduced_net + original_payment` would show a positive balance (club owes user)

### Example Scenario

1. User buys lot for $1
2. User pays $1 with Square → `InvoicePayment(amount=$1)`
3. Invoice shows: `net = -$1`, `total_payments = $1`, `net_after_payments = $0` ✓
4. Admin attempts 50% refund via `lot.refund(50, admin)`
5. Square API call fails (e.g., network timeout)
6. **BUG**: `partial_refund_percent = 50` is set anyway
7. Invoice now shows:
   - `total_bought = $1 * 50% = $0.50`
   - `net = -$0.50`
   - `total_payments = $1` (no refund InvoicePayment created)
   - `net_after_payments = -$0.50 + $1 = $0.50` ✗ (club owes user!)

## Solution

Modified `Lot.refund()` to:
1. Track whether the Square refund failed with a `square_refund_failed` flag
2. Only update `partial_refund_percent` if the Square refund succeeded OR wasn't attempted
3. If Square refund fails, log the error but don't update `partial_refund_percent`

### Code Changes

**File**: `auctions/models.py`
**Method**: `Lot.refund()`

```python
def refund(self, amount, user, message=None):
    square_refund_failed = False
    
    if amount and amount != self.partial_refund_percent:
        if self.square_refund_possible and not self.no_more_refunds_possible:
            error = self.square_refund(amount)
            if error:
                # NEW: Set flag instead of continuing
                square_refund_failed = True
                # ... logging ...
        # ... create LotHistory ...
    
    # NEW: Only update if Square refund didn't fail
    if not square_refund_failed:
        self.partial_refund_percent = amount
        self.save()
```

## Testing

Added comprehensive tests in `auctions/test_square_refund_math.py`:

1. **test_square_refund_success_updates_partial_refund_percent**: Verifies normal refund flow works
2. **test_square_refund_failure_does_not_update_partial_refund_percent**: Verifies fix prevents invoice imbalance
3. **test_manual_refund_without_square_payment**: Verifies manual refunds (cash/check) still work
4. **test_issue_scenario_square_refund_and_partial_refund_percent_both_applied**: Verifies correct final state
5. **test_issue_scenario_partial_refund_percent_set_without_square_refund**: Demonstrates the bug scenario

## Impact

### Before Fix
- Square refund failures could cause invoice imbalances
- Admins would see incorrect "club owes user" amounts
- Manual intervention required to fix invoices

### After Fix
- Square refund failures leave the invoice unchanged
- Admins see error message and can retry
- Invoice remains accurate and balanced

## Notes

- Manual refunds (no Square payment) continue to work as expected
- The fix doesn't address webhook delays (webhook should arrive within seconds/minutes)
- If an admin manually sets `partial_refund_percent` via Django admin without using the refund dialog, they must manually create the corresponding InvoicePayment record
