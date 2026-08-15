"""Backfill the two columns 0387 changed the meaning of.

Two unrelated one-off passes, kept in one migration because both exist to stop 0387 from changing
anybody's data underneath them:

1. The normalised name columns.  Empty on every existing row, and an empty column means every
   common-name lookup misses -- so this has to run before the new matcher does.
2. ``Club.points_per_lot`` is now nullable, and null is what "use the per-category points"
   means.  Every club currently storing 0 meant that, because 0 was the default and the old code
   read it as "fall through"; turning those into NULL keeps them behaving exactly as they did,
   and leaves 0 free to mean nought from now on.

Deliberately **not** here: switching ``Auction.use_scientific_name`` off for auctions that are
already taking lots.  It reads like a kindness -- no new field appearing mid-sale -- but
``use_scientific_name`` is in ``AuctionCreateView.fields_to_clone``, so the club that copies this
season's auction to make next season's would inherit the "off" and never see the feature again.
A new field on an in-flight add-lot form is a day's mild surprise; a setting that silently
propagates forward is permanent.  On by default, everywhere, and an admin who doesn't want it
unticks it.

The normaliser is spelled out here rather than imported from models.py on purpose: this migration
has to keep producing the values it produced the day it ran, and a migration that changes its mind
later because a function moved on is how a column and its index quietly stop agreeing.
"""

import re

from django.db import migrations

BATCH_SIZE = 2000


def normalize(text):
    text = re.sub(r"['‘’ʼ`]+", "", (text or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()[:120]


def _fill(model, source_field, target_field):
    """Write ``normalize(source)`` into ``target`` for every row that needs it.  Returns the count."""
    changed = 0
    batch = []
    for row in model.objects.only("pk", source_field, target_field).iterator(chunk_size=BATCH_SIZE):
        value = normalize(getattr(row, source_field))
        if getattr(row, target_field) != value:
            setattr(row, target_field, value)
            batch.append(row)
        if len(batch) >= BATCH_SIZE:
            model.objects.bulk_update(batch, [target_field])
            changed += len(batch)
            batch = []
    if batch:
        model.objects.bulk_update(batch, [target_field])
        changed += len(batch)
    return changed


def fill_normalized_names(apps, schema_editor):
    _fill(apps.get_model("auctions", "SpeciesCommonName"), "name", "name_normalized")
    _fill(apps.get_model("auctions", "Species"), "common_name", "common_name_normalized")


def blank_zero_points_per_lot(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    Club.objects.filter(points_per_lot=0).update(points_per_lot=None)


def unblank_points_per_lot(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    Club.objects.filter(points_per_lot__isnull=True).update(points_per_lot=0)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0387_species_common_name_normalized_and_more"),
    ]

    operations = [
        migrations.RunPython(fill_normalized_names, migrations.RunPython.noop),
        migrations.RunPython(blank_zero_points_per_lot, unblank_points_per_lot),
    ]
