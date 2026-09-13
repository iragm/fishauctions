"""Say in the admin what actually fills ``PageView.auction`` now.

``help_text`` only, so ``sqlmigrate`` prints ``(no-op)``: Django treats it as a non-database
attribute and emits nothing. That matters more here than it looks -- every FK on this site is
stored under a mangled ``table?constraint`` name MariaDB will not ``DROP``, so a migration that
rebuilt one would pass against a fresh test database and fail against the real one.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0431_clubhealth_people_here_alter_clubhealth_stage"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pageview",
            name="auction",
            field=models.ForeignKey(
                blank=True,
                help_text="Set when a visitor views the auction's rules page, its lot list or one of its lots, and deliberately left empty on organizer-facing pages so that view counts stay visitor counts. Rows written before 2026-09-09 have it on the rules page only.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="auctions.auction",
            ),
        ),
    ]
