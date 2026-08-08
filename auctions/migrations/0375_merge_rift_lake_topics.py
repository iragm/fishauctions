"""Collapse the three rift-lake topics into one, and drop the two catch-all topics.

0374 seeded a vocabulary with separate Malawi, Tanganyika and Victoria topics.  Separating them
doesn't survive contact with the data: the NEC export's only rift-lake name is "Rift Lakes",
which doesn't say which lake, and a talk on one of the lakes is usually a talk on all three.
"Freshwater Fish" and "General Interest" go for the opposite reason -- they applied to 112 of
the 405 imported speakers and said nothing about any of them.

On a fresh database this is a no-op: 0374 reads the current list from auctions/speaker_topics.py,
so the removed topics were never created.  It only has work to do on a database that ran 0374
before this change, which is where the rows and their taggings already exist.
"""

from django.db import migrations

RIFT_LAKE = "Rift Lake Cichlids"

MERGED_INTO_RIFT_LAKE = [
    "Lake Malawi Cichlids",
    "Lake Tanganyika Cichlids",
    "Lake Victoria Cichlids",
]

DISCARDED = [
    "Freshwater Fish",
    "General Interest",
]


def merge_topics(apps, schema_editor):
    SpeakerTopic = apps.get_model("auctions", "SpeakerTopic")

    lakes = list(SpeakerTopic.objects.filter(name__in=MERGED_INTO_RIFT_LAKE))
    if lakes:
        # Retag before deleting: dropping the row would take its taggings with it, and someone
        # tagged "Lake Tanganyika Cichlids" is a rift lake speaker under the new vocabulary.
        rift_lake = SpeakerTopic.objects.filter(name__iexact=RIFT_LAKE).first()
        if not rift_lake:
            # ensure_site_defaults would create this on the next start; doing it here means the
            # retagging can't land before the row it needs exists.  Slug comes from AutoSlugField.
            rift_lake = SpeakerTopic.objects.create(name=RIFT_LAKE)
        for lake in lakes:
            for speaker in lake.speakers.all():
                speaker.topics.add(rift_lake)
            lake.delete()

    # No retagging for these two: there is nothing to say about a speaker whose topic was
    # "General Interest", and moving them to "Other" would just relabel the same non-answer.
    SpeakerTopic.objects.filter(name__in=DISCARDED).delete()


def unmerge_topics(apps, schema_editor):
    """Deliberately a no-op -- the same reasoning as 0374.

    Which of the merged speakers belonged to which lake is exactly the information that didn't
    exist in the first place, so a reverse can only guess or drop taggings.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0374_speaker_topic_vocabulary"),
    ]

    operations = [
        migrations.RunPython(merge_topics, unmerge_topics),
    ]
