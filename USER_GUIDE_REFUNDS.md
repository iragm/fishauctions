# User Guide: Square Refund Processing After Fix

## What Changed

We fixed a critical bug in the refund system that could cause invoice calculation errors when Square refunds failed.

## For Auction Administrators

### Before the Fix
If you attempted to refund a lot that was paid via Square, and the Square API call failed (due to network issues, API errors, etc.), the system would:
- ❌ Reduce the lot price in the invoice calculations
- ❌ NOT create the actual refund payment record
- ❌ Show incorrect "club owes user" balance
- ❌ Require manual database fixes

### After the Fix
Now, if a Square refund fails, the system will:
- ✅ Keep the lot price unchanged in invoices
- ✅ NOT create incorrect balances
- ✅ Log the error for you to see
- ✅ Allow you to retry the refund

### How to Process Refunds

#### Square Refunds (Recommended)
1. Go to the lot's refund dialog
2. Enter the refund percentage (e.g., 50 for 50% refund)
3. Click "Refund"
4. System automatically:
   - Processes Square refund via API
   - Updates lot price in invoices
   - Records the refund payment
   - Creates audit log entry

**If the refund fails:**
- You'll see an error message in the lot history
- The invoice remains unchanged (no incorrect balance)
- You can retry the refund later
- Check your Square account to verify if refund actually processed

#### Manual Refunds (Cash/Check Payments)
For lots not paid via Square:
1. Go to the lot's refund dialog
2. Enter the refund percentage
3. Click "Refund"
4. System updates the invoice calculations
5. **You must manually refund the buyer** via cash, check, or other method

### What to Watch For

#### Normal Refund Success
```
Lot History: "Admin has issued a 50% refund on this lot. Square refund processed automatically."
```
- ✅ Lot price reduced in invoices
- ✅ Refund payment recorded
- ✅ Invoice balanced correctly

#### Square Refund Failure
```
Lot History: "Admin has issued a 50% refund on this lot. Square refund failed: [error details]"
```
- ⚠️ Lot price NOT reduced
- ⚠️ No refund payment recorded  
- ⚠️ Invoice unchanged
- 🔄 You can retry the refund

### Troubleshooting

**Q: I issued a refund but the invoice didn't change. Is this a bug?**
A: No, this is the new safety feature! If the Square API call failed, we prevent incorrect invoice calculations. Check the lot history for error details and retry the refund.

**Q: How do I know if a Square refund actually processed?**
A: Check:
1. Lot history for success/failure message
2. Invoice for refund payment record
3. Your Square dashboard for the refund transaction

**Q: Can I manually set the refund percentage in the admin?**
A: Yes, but be careful! If you manually set `partial_refund_percent` in the Django admin without processing a Square refund, you must also manually create a negative `InvoicePayment` record to keep the invoice balanced.

**Q: What if I need to refund more than once?**
A: Once a Square refund is processed, the `no_more_refunds_possible` flag prevents additional refunds on that lot. This prevents double-refunds and accounting errors.

### Best Practices

1. **Always use the refund dialog** instead of manually editing the database
2. **Check lot history** after issuing a refund to verify it succeeded
3. **Verify in Square dashboard** if you're unsure whether a refund processed
4. **Retry failed refunds** rather than manually editing the database
5. **For manual refunds** (non-Square), remember to refund the buyer yourself

## For Developers

### Testing Your Changes
If you modify refund-related code, run:
```bash
docker exec -it django python3 manage.py test auctions.test_square_refund_math
```

This will verify:
- Normal refund flow works correctly
- Failed refunds don't corrupt invoices
- Manual refunds (non-Square) still work
- Edge cases are handled properly

### Monitoring
Watch for these log messages:
```python
logger.error("Square refund failed for lot %s: %s", self.pk, error)
```

High frequency of these errors might indicate:
- Square API connectivity issues
- Invalid Square credentials
- Expired OAuth tokens
- Network problems

### Debugging Invoice Imbalances
If you find an invoice with incorrect balance:

1. Check if there's a `partial_refund_percent` on the lot:
   ```python
   lot = Lot.objects.get(pk=LOT_ID)
   print(f"partial_refund_percent: {lot.partial_refund_percent}")
   ```

2. Check for corresponding refund InvoicePayment:
   ```python
   invoice = lot.winner_invoice
   refunds = invoice.payments.filter(amount__lt=0)
   print(f"Refund payments: {refunds}")
   ```

3. If mismatch found (partial_refund_percent set but no refund payment):
   - This indicates a pre-fix bug occurrence
   - Manually create negative InvoicePayment OR reset partial_refund_percent
   - Document the fix in lot history

## Summary

This fix prevents invoice calculation errors when Square refunds fail, ensuring accurate accounting and reducing manual intervention needed to fix corrupted invoices.

**Key Improvement**: Failed refunds no longer corrupt invoices - they simply don't process, and you can retry.
