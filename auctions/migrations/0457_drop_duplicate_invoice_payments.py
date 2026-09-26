"""Clear the way for 0458's one-record-per-charge constraint on InvoicePayment.

A blank ``external_id`` becomes NULL, which is what "no provider id" already means everywhere else and
what the constraint lets repeat. Then, where one provider charge was recorded on an invoice more than
once (the Tap to Pay confirm racing the Square webhook), the first record is kept and the rest, which
counted the same money again, are deleted.
"""

from django.db import migrations
from django.db.models import Count


def drop_duplicates(apps, schema_editor):
    InvoicePayment = apps.get_model("auctions", "InvoicePayment")
    InvoicePayment.objects.filter(external_id="").update(external_id=None)
    groups = (
        InvoicePayment.objects.filter(invoice__isnull=False, external_id__isnull=False)
        .values("invoice", "external_id")
        .annotate(copies=Count("id"))
        .filter(copies__gt=1)
        .order_by()
    )
    for group in list(groups):
        pks = list(
            InvoicePayment.objects.filter(invoice_id=group["invoice"], external_id=group["external_id"])
            .order_by("createdon", "pk")
            .values_list("pk", flat=True)
        )
        InvoicePayment.objects.filter(pk__in=pks[1:]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0456_invoice_one_per_participant"),
    ]

    operations = [
        migrations.RunPython(drop_duplicates, migrations.RunPython.noop),
    ]
