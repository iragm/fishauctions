import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0240_club_enable_club_page"),
    ]

    operations = [
        migrations.AddField(
            model_name="clubmember",
            name="possible_duplicate",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="duplicate_of",
                to="auctions.clubmember",
            ),
        ),
    ]
