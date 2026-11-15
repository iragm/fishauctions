"""Email domains the contact form silently ignores from signed-out visitors, seeded with one that spams it."""

from django.db import migrations, models

SEED = ["ultraleadgeneration.com"]


def seed(apps, schema_editor):
    BlockedEmailDomain = apps.get_model("auctions", "BlockedEmailDomain")
    for domain in SEED:
        BlockedEmailDomain.objects.get_or_create(domain=domain)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0533_club_thanks_email"),
    ]

    operations = [
        migrations.CreateModel(
            name="BlockedEmailDomain",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "domain",
                    models.CharField(
                        help_text="Like example.com; also blocks its subdomains.", max_length=253, unique=True
                    ),
                ),
                ("createdon", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["domain"],
            },
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
