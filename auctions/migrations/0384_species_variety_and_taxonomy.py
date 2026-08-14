"""Cultivars, taxonomy above the genus, and FishBase's aquarium-trade rating.

``variety``/``parent`` let a row be "Neocaridina davidi 'Blue Dream'" without pretending the
strain is a taxon: the row keeps the parent's genus and epithet, so breeder points, genus BAP
rules and the family-to-category mapping all still see a cherry shrimp.

``family``/``order`` come from FishBase's families table and are what
``auctions/species_categories.py`` maps to a site :class:`Category`, replacing the keyword guess.

``aquarium_use`` is FishBase's own ``Aquarium`` column.  Around 3,500 of its 36,000 fish are in
the trade; ranking those first is the difference between "ram" suggesting *Mikrogeophagus
ramirezi* and suggesting *Abramis brama*.

Only additive: an index on ``species`` (the epithet, now searchable on its own) and new nullable
columns.  No existing foreign key is rebuilt -- see the note in 0382 about mangled constraint
names.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0383_species_common_name_index"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="species",
            options={"ordering": ["scientific_name", "variety"], "verbose_name_plural": "Species"},
        ),
        migrations.AddField(
            model_name="species",
            name="aquarium_use",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="FishBase's aquarium-trade rating.  Species in the trade are ranked above the rest when suggesting a scientific name for a lot.",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="species",
            name="family",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Taxonomic family, e.g. Cichlidae.  Used to derive the lot category.",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="species",
            name="order",
            field=models.CharField(
                blank=True, db_index=True, help_text="Taxonomic order, e.g. Cichliformes.", max_length=100
            ),
        ),
        migrations.AddField(
            model_name="species",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                help_text="The nominal species this variety belongs to.  Only used on variety rows.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="varieties",
                to="auctions.species",
            ),
        ),
        migrations.AddField(
            model_name="species",
            name="variety",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Cultivar, strain or morph, e.g. Blue Dream.  Leave blank for a wild-type species.  A row with this set must also have a parent.",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="species",
            name="source",
            field=models.CharField(
                choices=[
                    ("fishbase", "FishBase"),
                    ("sealifebase", "SeaLifeBase"),
                    ("aquarium", "Aquarium trade list"),
                    ("manual", "Added by hand"),
                ],
                default="manual",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="species",
            name="species",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Second half of the scientific name (the specific epithet), e.g. reticulata",
                max_length=150,
            ),
        ),
    ]
