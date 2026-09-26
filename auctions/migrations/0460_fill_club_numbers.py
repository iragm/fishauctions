"""Give every existing club its 10-digit number (0459 added the column empty; new clubs get one on save)."""

from random import randint

from django.db import migrations


def fill_numbers(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    taken = set(Club.objects.exclude(number__isnull=True).values_list("number", flat=True))
    for club in Club.objects.filter(number__isnull=True).only("pk"):
        number = randint(1_000_000_000, 9_999_999_999)
        while number in taken:
            number = randint(1_000_000_000, 9_999_999_999)
        taken.add(number)
        Club.objects.filter(pk=club.pk).update(number=number)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0459_club_number"),
    ]

    operations = [
        migrations.RunPython(fill_numbers, migrations.RunPython.noop),
    ]
