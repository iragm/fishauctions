from django.db import migrations


def create_locations(apps, schema_editor):
    Location = apps.get_model("auctions", "Location")

    if not Location.objects.exists():
        Location.objects.bulk_create(
            [
                Location(name="Other"),
                Location(name="Australia"),
                Location(name="Africa"),
                Location(name="South America"),
                Location(name="Europe"),
                Location(name="Canada"),
                Location(name="United States"),
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0177_alter_pageview_date_start_alter_pageview_session_id"),
    ]

    operations = [
        migrations.RunPython(create_locations),
    ]
