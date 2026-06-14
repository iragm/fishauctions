from django.db import migrations
from django.db.models import Q


def reset_invoice_ledger(apps, schema_editor):
    """Wipe and re-derive the invoice-booked club ledger under the cash-basis rewrite.

    Entries booked from invoices before the rewrite used the old (mis-balanced) logic and
    now-retired categories, so they are cleared and re-derived from the current state of
    each invoice via ``Invoice.sync_club_money``. Only invoices belonging to a club's
    auction are touched; manual treasurer entries (no invoice) and club-only membership
    entries (an invoice with no auction) are left exactly as they are.

    On a fresh database there are no such invoices, so this is a no-op for new installs/CI.
    """
    from auctions.models import ClubMoney, Invoice

    invoice_ids = list(
        Invoice.objects.filter(
            Q(auction__club__isnull=False) | Q(auction__isnull=True, auctiontos_user__auction__club__isnull=False)
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    if not invoice_ids:
        return

    ClubMoney.objects.filter(invoice_id__in=invoice_ids).delete()

    invoices = Invoice.objects.filter(pk__in=invoice_ids).select_related(
        "auction", "auction__club", "auctiontos_user", "auctiontos_user__auction", "auctiontos_user__auction__club"
    )
    for invoice in invoices.iterator(chunk_size=200):
        invoice.sync_club_money()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0304_alter_clubmoney_category"),
    ]

    operations = [
        # Data-only reset; there is nothing meaningful to restore on reverse.
        migrations.RunPython(reset_invoice_ledger, migrations.RunPython.noop),
    ]
