"""Populate the speaker directory's topic vocabulary.

Topics are a closed list (auctions/speaker_topics.py) that nothing in the UI can add to, so the
rows have to exist before anyone opens the add-speaker form.  ``ensure_site_defaults`` also
creates them on every start, but that only helps if the entrypoint got that far -- doing it here
means the vocabulary lands as part of ``migrate``, in order, on every deploy and every fresh
database.

The list is imported rather than inlined on purpose.  This is reference data, not a historical
record: replaying this migration should produce the vocabulary the code currently expects, not a
snapshot of what it was in August 2026.  Adding a topic later is an edit to that module -- this
migration is already applied on existing databases, so the ``ensure_site_defaults`` hook is what
picks the addition up there.
"""

from django.db import migrations

from auctions.speaker_topics import STARTER_TOPICS


def create_topics(apps, schema_editor):
    SpeakerTopic = apps.get_model("auctions", "SpeakerTopic")
    for name in STARTER_TOPICS:
        # iexact so a differently-cased row somebody already made is left alone rather than
        # gaining a near-duplicate twin.  Slug is populated by AutoSlugField on save.
        if not SpeakerTopic.objects.filter(name__iexact=name).exists():
            SpeakerTopic.objects.create(name=name)


def remove_topics(apps, schema_editor):
    """Deliberately a no-op.

    Reversing would delete topics that speakers are now tagged with, silently dropping those
    associations. An unused vocabulary row costs nothing; a lost tagging can't be recovered.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0373_speaker_user_alter_speakertag_tag"),
    ]

    operations = [
        migrations.RunPython(create_topics, remove_topics),
    ]
