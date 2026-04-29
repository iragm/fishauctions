from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0241_clubmember_possible_duplicate"),
    ]

    operations = [
        migrations.AddField(
            model_name="clubdiscordrole",
            name="role_id",
            field=models.CharField(blank=True, help_text="Discord role snowflake ID", max_length=20),
        ),
        migrations.AddField(
            model_name="clubdiscordrole",
            name="is_default",
            field=models.BooleanField(default=False, help_text="Assign this role to users who register via Discord"),
        ),
    ]
