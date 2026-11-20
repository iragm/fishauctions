"""Single senders the contact form silently ignores, signed in or not."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0534_blockedemaildomain"),
    ]

    operations = [
        migrations.CreateModel(
            name="BlockedEmailAddress",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("createdon", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["email"],
                "verbose_name_plural": "blocked email addresses",
            },
        ),
    ]
