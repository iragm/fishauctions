"""Clear google_calendar_is_public on clubs that connected before the flag changed meaning.

The field used to mean "make this calendar public", and we tried to do it via the Calendar ACL
API. That call always failed — it needs the calendar.acls scope, which we deliberately don't ask
for — so no calendar was ever actually made public. The field now means "the admin has shared it
themselves", and altering the default doesn't rewrite existing rows, so every club that connected
under the old default is wrongly flagged public: no "make it public" banner, and Google subscribe
links that don't work for members.
"""

from django.db import migrations


def clear_is_public(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    Club.objects.filter(google_calendar_is_public=True).update(google_calendar_is_public=False)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0358_alter_club_google_calendar_is_public"),
    ]

    operations = [
        migrations.RunPython(clear_is_public, migrations.RunPython.noop),
    ]
