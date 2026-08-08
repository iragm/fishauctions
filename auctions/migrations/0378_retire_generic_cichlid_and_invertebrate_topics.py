"""Retire "Cichlids" and "Freshwater Invertebrates", add "Shrimp", and flag what needs a human.

Both removals are in auctions/speaker_topics.py; this is the half that can't live there, because
it is about the speakers already tagged with them.

"Cichlids" was a duplicate more than a topic: 55 of the 67 speakers carrying it also carried a
specific cichlid topic, so for them the row just drops.  The other 12 lose their only cichlid
signal, and no rule can tell whether they meant African, New World, Dwarf, Rift Lake or West
African -- so they are flagged instead of guessed at.

"Freshwater Invertebrates" was two audiences under one name.  Talk lists say which: a speaker
whose talks mention shrimp goes to the new "Shrimp" topic, one whose talks mention snails goes to
"Other" (snails have nowhere better, and that is a decision, not an oversight), and one whose
talks mention neither gets "Other" and a flag.  Speakers who do both get both.

Flagged rows are the worklist: `Topics need review` in the Django admin, or a search for
`needsreview` in the directory.  On a fresh database this whole migration is a no-op -- 0374
reads the current vocabulary, so neither retired topic was ever created.
"""

from django.db import migrations

SHRIMP = "Shrimp"
OTHER = "Other"

CICHLIDS = "Cichlids"
INVERTEBRATES = "Freshwater Invertebrates"

#: A speaker on one of these already says which cichlids they mean, so dropping the generic row
#: costs them nothing and there is nothing for anyone to review.
SPECIFIC_CICHLID_TOPICS = [
    "African Cichlids",
    "Dwarf Cichlids",
    "New World Cichlids",
    "Rift Lake Cichlids",
    "West African Cichlids",
]


def topic_named(SpeakerTopic, name):
    """Fetch a vocabulary row, creating it if this database predates it. Slug is automatic."""
    topic = SpeakerTopic.objects.filter(name__iexact=name).first()
    if not topic:
        topic = SpeakerTopic.objects.create(name=name)
    return topic


def flag(Speaker, speaker, old_topic):
    """Record that a person needs to decide where this speaker really belongs."""
    Speaker.objects.filter(pk=speaker.pk).update(topics_need_review=True, topic_review_note=f"Was on: {old_topic}")


def retire_topics(apps, schema_editor):
    Speaker = apps.get_model("auctions", "Speaker")
    SpeakerTopic = apps.get_model("auctions", "SpeakerTopic")

    cichlids = SpeakerTopic.objects.filter(name__iexact=CICHLIDS).first()
    if cichlids:
        specific = set(
            SpeakerTopic.objects.filter(name__in=SPECIFIC_CICHLID_TOPICS).values_list("pk", flat=True),
        )
        other = topic_named(SpeakerTopic, OTHER)
        for speaker in cichlids.speakers.all():
            held = set(speaker.topics.values_list("pk", flat=True)) - {cichlids.pk}
            if held & specific:
                continue
            if not held:
                # Otherwise dropping the row leaves them with no topic at all, which takes them
                # out of the topic menu entirely. "Other" is the safety net it was built to be.
                speaker.topics.add(other)
            flag(Speaker, speaker, CICHLIDS)
        cichlids.delete()

    invertebrates = SpeakerTopic.objects.filter(name__iexact=INVERTEBRATES).first()
    if invertebrates:
        shrimp = topic_named(SpeakerTopic, SHRIMP)
        other = topic_named(SpeakerTopic, OTHER)
        for speaker in invertebrates.speakers.all():
            talks = f"{speaker.bio} {speaker.programs}".casefold()
            keeps_shrimp = "shrimp" in talks
            keeps_snails = "snail" in talks
            if keeps_shrimp:
                speaker.topics.add(shrimp)
            if keeps_snails or not keeps_shrimp:
                speaker.topics.add(other)
            if not (keeps_shrimp or keeps_snails):
                # Nothing in their own words to go on -- "Other" is a placeholder here, not an
                # answer, so it comes with a flag.
                flag(Speaker, speaker, INVERTEBRATES)
        invertebrates.delete()


def remove_retired_tags(apps, schema_editor):
    """Delete the votes for the two tags that no longer exist.

    Left behind they are invisible -- `tag_counts()` walks TAG_DEFINITIONS, so a value that
    isn't in it is never counted or rendered -- but they'd still block the unique constraint if
    either name were ever reused, and an invisible row is worse than no row.
    """
    SpeakerTag = apps.get_model("auctions", "SpeakerTag")
    SpeakerTag.objects.filter(tag__in=["no_fee", "responsive"]).delete()


def reverse(apps, schema_editor):
    """Deliberately a no-op, the same reasoning as 0374 and 0375.

    Which cichlids a flagged speaker talks about, and whether an invertebrate speaker meant
    shrimp or snails, is exactly what the old topics never recorded.  A reverse could only
    re-create two rows and guess at who belonged on them.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0377_speaker_topic_review_note_speaker_topics_need_review_and_more"),
    ]

    operations = [
        migrations.RunPython(retire_topics, reverse),
        migrations.RunPython(remove_retired_tags, reverse),
    ]
