from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0523_custom_checkbox_help_without_cares"),
    ]

    operations = [
        migrations.AddField(
            model_name="lot",
            name="checked_in",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
