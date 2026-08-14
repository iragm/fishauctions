"""Split the hand-typed species names into genus + epithet.

Migration 0382 turned the old ``Product`` table into ``Species`` and added ``genus``/``species``,
but it could only default them to empty -- the rows that were already there are a scientific name
somebody typed into one box, with nothing to split it.

Two things go wrong while they stay that way, and both are quiet:

* every lookup in :mod:`auctions.species_matching` is indexed on ``genus``, so a legacy row is
  invisible to a search even when it is exactly what the user typed;
* ``Species.save()`` rebuilds ``scientific_name`` from genus and epithet, so saving one of these
  rows -- an admin ticking "breeder points" is enough -- used to blank the only name it had.

``save()`` now guards against the second on its own; this fixes the rows that already exist so
neither depends on somebody remembering to re-run the import.  Only rows with a name and no genus
are touched, so it is a no-op on every imported row and safe to re-run.
"""

from django.db import migrations


def split_names(apps, schema_editor):
    Species = apps.get_model("auctions", "Species")
    updates = []
    for species in Species.objects.filter(genus="", species="").exclude(scientific_name="").iterator():
        parts = species.scientific_name.split()
        if not parts:
            continue
        species.genus = parts[0][:100]
        species.species = " ".join(parts[1:])[:150]
        updates.append(species)
        if len(updates) >= 500:
            Species.objects.bulk_update(updates, ["genus", "species"])
            updates = []
    if updates:
        Species.objects.bulk_update(updates, ["genus", "species"])


def unsplit(apps, schema_editor):
    """Nothing to undo: scientific_name was never changed, and clearing the split would restore
    exactly the bug this fixes."""


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0384_species_variety_and_taxonomy"),
    ]

    operations = [
        migrations.RunPython(split_names, unsplit),
    ]
