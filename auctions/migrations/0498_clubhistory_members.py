from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0497_documentpicture"),
    ]

    operations = [
        migrations.AddField(
            model_name="clubhistory",
            name="members",
            field=models.ManyToManyField(blank=True, related_name="history", to="auctions.clubmember"),
        ),
    ]
