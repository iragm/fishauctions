"""How likely it is that anyone actually keeps a species, in three steps.

FishBase's own ``Aquarium`` column is the obvious signal and it is not enough on its own: it marks
3,475 of 36,132 fish as aquarium species, and *Chindongo saulosi* -- a mbuna in every African
cichlid club's auction -- is filed under "never/rarely", as are 75 of the 77 *Ancistrus*.  So
``trade_rank`` adds a middle step for "this species' genus is in the hobby even if this row isn't
flagged", and ``in_trade_override`` lets a person say so outright, which is what an admin adding a
species by hand is implicitly doing.

``trade_rank`` is denormalised on purpose: every suggestion lookup orders by it before taking a
``LIMIT``, and the alternatives are a correlated subquery per row or an ``IN`` clause holding all
1,142 genera with a species in the hobby.  ``Species.recompute_trade_ranks()`` fills it in; the
importer calls it, and it defaults to 2 so a database that hasn't run one yet simply ranks
everything equally rather than ranking it wrongly.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0385_split_legacy_species_names"),
    ]

    operations = [
        migrations.AddField(
            model_name="species",
            name="in_trade_override",
            field=models.BooleanField(
                blank=True,
                help_text="Overrules FishBase on whether this species is in the hobby.  Leave unset unless you know better than the source -- and you often will, because FishBase marks plenty of fish people obviously keep as 'never/rarely'.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="species",
            name="trade_rank",
            field=models.PositiveSmallIntegerField(
                db_index=True,
                default=2,
                help_text="Denormalised: 0 = in the hobby, 1 = its genus is, 2 = nothing says anyone keeps it.  Rebuilt by Species.recompute_trade_ranks(); don't edit by hand.",
            ),
        ),
    ]
