"""Fix the seeded ``escpos-raster`` fallback print width.

The generic ESC/POS fallback was seeded at 96 dots — that's the D11s's 12 mm head, not a normal
58 mm thermal head, so it printed a ~12 mm strip. A full 58 mm head at 203 dpi is 384 dots. Only
the still-default 96 is bumped so an admin who deliberately tuned the row keeps their value; the
D11s rows stay at 96 (correct for their 12 mm head).
"""

from django.db import migrations

_SLUG = "escpos-raster"
_OLD = 96
_NEW = 384


def widen(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    ThermalPrinterProfile.objects.filter(slug=_SLUG, print_width_px=_OLD).update(print_width_px=_NEW)


def narrow(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    ThermalPrinterProfile.objects.filter(slug=_SLUG, print_width_px=_NEW).update(print_width_px=_OLD)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0328_invoice_date_paid"),
    ]

    operations = [
        migrations.RunPython(widen, narrow),
    ]
