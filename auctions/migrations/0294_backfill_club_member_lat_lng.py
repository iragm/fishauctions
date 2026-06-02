from django.db import migrations, models


def enqueue_geocoding(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    try:
        from auctions.tasks import geocode_club_member
    except ImportError:
        return

    pks = (
        ClubMember.objects.filter(
            lat__isnull=True,
            lng__isnull=True,
            is_deleted=False,
        )
        .filter(models.Q(address__gt="") | models.Q(user__isnull=False))
        .values_list("pk", flat=True)
    )

    for pk in pks:
        try:
            geocode_club_member.delay(pk)
        except Exception:
            import logging

            logging.getLogger(__name__).warning("geocode_club_member.delay(%s) failed; skipping", pk)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0293_club_mailchimp_access_token_and_more"),
    ]

    operations = [
        migrations.RunPython(enqueue_geocoding, migrations.RunPython.noop),
    ]
