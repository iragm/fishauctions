from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0511_online_pickup_tomorrow_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="lot",
            name="removed_while_open",
            field=models.BooleanField(default=False),
        ),
    ]
