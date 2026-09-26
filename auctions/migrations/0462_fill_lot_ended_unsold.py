"""Flag in-person lots already ended unsold on set-winners, so an open invoice keeps its unsold lot fee.

Those lots have a date_end and the "as not sold" history line; undo clears date_end, and wind-down
deactivates strays without one. Online lots don't need the flag.
"""

from django.db import migrations
from django.db.models import Exists, OuterRef


def fill_ended_unsold(apps, schema_editor):
    Lot = apps.get_model("auctions", "Lot")
    LotHistory = apps.get_model("auctions", "LotHistory")
    ended = LotHistory.objects.filter(lot=OuterRef("pk"), changed_price=True, message__endswith=" as not sold")
    pks = list(
        Lot.objects.filter(
            auction__is_online=False,
            active=False,
            winning_price__isnull=True,
            date_end__isnull=False,
        )
        .filter(Exists(ended))
        .values_list("pk", flat=True)
    )
    for start in range(0, len(pks), 1000):
        Lot.objects.filter(pk__in=pks[start : start + 1000]).update(ended_unsold=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0461_lot_ended_unsold"),
    ]

    operations = [
        migrations.RunPython(fill_ended_unsold, migrations.RunPython.noop),
    ]
