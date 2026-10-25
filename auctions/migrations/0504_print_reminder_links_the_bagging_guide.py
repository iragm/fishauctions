"""The print-your-labels email links to the bagging guide in the help, not the old blog post.

Nothing is undone on the way back: a help link that was there before can't be told from one this added.
"""

from django.db import migrations

#: The path alone, so a host or ``{{domain}}`` spelled differently in the admin still matches.
OLD = "/blog/transporting-fish/"
NEW = "/help/bagging-fish/"


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name="auction_print_reminder"):
        changed = []
        for field in ("content", "html_content"):
            body = getattr(template, field) or ""
            if OLD in body:
                setattr(template, field, body.replace(OLD, NEW))
                changed.append(field)
        if changed:
            template.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0503_location_check"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
