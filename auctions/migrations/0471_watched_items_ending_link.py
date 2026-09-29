"""The watched-lots email links to the auction its lots are ending in, as ``sendnotifications`` works out.

``/lots/watched/`` opened the buying dashboard on the last auction the person joined, and watching a lot
isn't joining its auction. A template edited in the admin keeps whatever link no longer matches.
"""

from django.db import migrations

#: ``(field, old, new)``. The text part isn't HTML, so the URL's ``&`` must not become ``&amp;``.
CHANGES = [
    ("html_content", "https://{{domain}}/lots/watched/?src=email", "{{ watched_url }}"),
    ("content", "https://{{domain}}/lots/watched/", "{{ watched_url|safe }}"),
]


def _apply(apps, forward):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name="watched_items_ending"):
        changed = set()
        for field, old, new in CHANGES:
            if not forward:
                old, new = new, old
            body = getattr(template, field) or ""
            if body.count(old) == 1:
                setattr(template, field, body.replace(old, new, 1))
                changed.add(field)
        if changed:
            template.save(update_fields=sorted(changed))


def forwards(apps, schema_editor):
    _apply(apps, forward=True)


def backwards(apps, schema_editor):
    _apply(apps, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0470_lot_queue_pointer_and_push_cooldown"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
