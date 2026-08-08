"""The speaker directory's fixed topic vocabulary.

Topics are a closed list, not free text: people adding a speaker pick from these, and nothing
in the UI creates new ones.  That is deliberate -- the NEC WordPress export arrived with three
spellings of "cichlids", two of "africa", and a "Cichids" typo, which is exactly what happens
when every contributor can coin a topic.  A new topic is an admin decision, made here or in the
Django admin.

:data:`STARTER_TOPICS` is the vocabulary.  :data:`TOPIC_ALIASES` maps the old NEC taxonomy (and
the obvious typos and synonyms) onto it, so importing the export lands on these names rather
than adding 49 more.  Anything unrecognised becomes "Other" rather than a new row, and the few
old names that meant nothing at all (:data:`DISCARDED_TOPICS`) are dropped instead.
:data:`REVIEW_TOPICS` is the third case: old names whose topic has since been retired and that
nothing can re-file automatically, so they land on "Other" *and* flag the speaker for a human.

The rows are created by migration 0374, so they land as part of ``migrate`` on every deploy and
every fresh database.  ``ensure_site_defaults`` calls :func:`ensure_speaker_topics` on every
start as well, which is what picks up a topic added to this list *after* 0374 has already run
on a database.  Both are idempotent.

One consequence to know about: a full test run flushes the test database, so a subsequent
``--keepdb`` run starts with these rows gone.  Tests that need topics call
:func:`ensure_speaker_topics` themselves rather than relying on the migration.
"""

OTHER = "Other"

#: The vocabulary, in the order it should read in a dropdown ("Other" is forced last).
STARTER_TOPICS = [
    "African Cichlids",
    "Aquascaping",
    "Brackish",
    "Catfish",
    "Characins & Tetras",
    "Club & Hobby History",
    "Collecting & Travel",
    "Commercial Fish Facilities",
    "Conservation & CARES",
    "Cyprinids & Barbs",
    "DIY Projects",
    "Dwarf Cichlids",
    "Filtration",
    "Fish Breeding",
    "Fish Health & Disease",
    "Fish Rooms",
    "Goldfish & Koi",
    "Killifish",
    "Labyrinth Fish (Bettas & Gouramis)",
    "Livebearers",
    "Loaches",
    "Marine Fish",
    "Nano Tanks",
    "Native & Wild-Caught Fish",
    "New World Cichlids",
    "Non-Fish Species",
    "Nutrition & Foods",
    "Photography",
    "Plants",
    "Ponds & Water Gardens",
    "Products & Equipment",
    "Rainbowfish",
    "Reef & Invertebrates",
    "Rift Lake Cichlids",
    "Shipping & Moving Fish",
    "Showing & Judging",
    "Shrimp",
    "Water Quality",
    "West African Cichlids",
    OTHER,
]

#: Old NEC taxonomy names (casefolded) that are deliberately thrown away rather than mapped.
#:
#: These two say nothing: every freshwater speaker is a "Freshwater species" speaker, and
#: "General" was the export's shrug.  They cover 112 of the 405 speakers, so folding them into
#: "Other" would make the largest topic in the directory the one that carries no information --
#: and "Other" is meant to be the safety net for names we don't recognise, not a bucket we fill
#: on purpose.  A speaker whose only export topic was one of these ends up with no topics, which
#: is exactly what the old taxonomy told us about them.
DISCARDED_TOPICS = {
    "freshwater species",
    "freshwater fish",
    "general",
    "general interest",
}

#: Old names (casefolded) whose topic has been retired and that nothing can re-file on its own.
#:
#: "Cichlids" went because 55 of the 67 speakers carrying it also carried a specific cichlid
#: topic, so the generic row was mostly a duplicate that made the topic menu longer without
#: telling anyone anything.  "Freshwater Invertebrates" went because it was two subjects in a
#: trench coat: shrimp people and snail people, who now belong under "Shrimp" and "Other"
#: respectively.  Which one a given speaker belongs under is a judgement call that needs
#: somebody to read their talk list, so these land on "Other" and set
#: :attr:`Speaker.topics_need_review` -- the admin's "Topics need review" filter is the
#: worklist.  Deliberately not folded into :data:`TOPIC_ALIASES`: an alias is a mapping we
#: trust, and the whole point of these is that we don't.
REVIEW_TOPICS = {
    "cichlids",
    "cichids",
    "invertebrates",
    "freshwater invertebrates",
    "freshwater inverts",
}

#: Old NEC taxonomy name (casefolded) -> vocabulary name.
TOPIC_ALIASES = {
    "africa": "African Cichlids",
    "african": "African Cichlids",
    "west african": "West African Cichlids",
    # One topic for all the rift lakes, on purpose. The export's "Rift Lakes" doesn't say which
    # lake, and a talk on Malawi haps is usually a talk on Tanganyikans too -- splitting them
    # meant guessing on import and asking people to guess again on the add-speaker form.
    "rift lakes": "Rift Lake Cichlids",
    "rift lake": "Rift Lake Cichlids",
    "lake victoria region": "Rift Lake Cichlids",
    "lake victoria": "Rift Lake Cichlids",
    "lake malawi": "Rift Lake Cichlids",
    "malawi": "Rift Lake Cichlids",
    "lake tanganyika": "Rift Lake Cichlids",
    "tanganyika": "Rift Lake Cichlids",
    "australia": "Rainbowfish",
    "cares species": "Conservation & CARES",
    "conservation": "Conservation & CARES",
    "catfish": "Catfish",
    "central and south america": "New World Cichlids",
    "new world (south / central america)": "New World Cichlids",
    "characins and other characiformes": "Characins & Tetras",
    "collecting & travel": "Collecting & Travel",
    "far east": "Collecting & Travel",
    "madagascar & asian": "Collecting & Travel",
    "company products": "Products & Equipment",
    "cyprinids": "Cyprinids & Barbs",
    "disease": "Fish Health & Disease",
    "health": "Fish Health & Disease",
    "diy (do it yourself)": "DIY Projects",
    "diy": "DIY Projects",
    "dwarf": "Dwarf Cichlids",
    "filtration": "Filtration",
    "fish breeding": "Fish Breeding",
    "fish rooms": "Fish Rooms",
    "history and hobby-related talks": "Club & Hobby History",
    "killifish": "Killifish",
    "labyrinth fish": "Labyrinth Fish (Bettas & Gouramis)",
    "livebearers": "Livebearers",
    "loaches & related cypriniformes": "Loaches",
    "marine": "Marine Fish",
    "moving with fish": "Shipping & Moving Fish",
    "shipping": "Shipping & Moving Fish",
    "nano tanks": "Nano Tanks",
    "nutrition": "Nutrition & Foods",
    "other fish species": OTHER,
    "other non-fish species talk": "Non-Fish Species",
    "photography": "Photography",
    "plants": "Plants",
    "ponds & water features": "Ponds & Water Gardens",
    "preparing to show fish": "Showing & Judging",
    "showing": "Showing & Judging",
    "professional breeder facilities": "Commercial Fish Facilities",
    "rainbowfish": "Rainbowfish",
    "reef & brackish": "Reef & Invertebrates",
    "shrimp": "Shrimp",
    "dwarf shrimp": "Shrimp",
    # Snails are the half of the old invertebrates topic that has nowhere better to go.  Said
    # out loud here so the next person doesn't read it as an oversight and "fix" it.
    "snails": OTHER,
    "us native fish": "Native & Wild-Caught Fish",
    "water quality": "Water Quality",
}


def canonical_topic_name(raw_name):
    """Map an incoming topic name onto the vocabulary.

    Returns a name from :data:`STARTER_TOPICS`, falling back to "Other" for anything not
    recognised -- so an import can never widen the vocabulary on its own.  Returns None for a
    blank name and for a :data:`DISCARDED_TOPICS` name, both of which callers skip entirely.

    A :data:`REVIEW_TOPICS` name also lands on "Other", but that is a placeholder rather than an
    answer: callers should ask :func:`topic_needs_review` as well and flag the speaker.
    """
    cleaned = " ".join((raw_name or "").split())
    if not cleaned:
        return None
    folded = cleaned.casefold()
    if folded in DISCARDED_TOPICS:
        return None
    if folded in REVIEW_TOPICS:
        return OTHER
    if folded in TOPIC_ALIASES:
        return TOPIC_ALIASES[folded]
    for topic in STARTER_TOPICS:
        if topic.casefold() == folded:
            return topic
    return OTHER


def topic_needs_review(raw_name):
    """True when this name lands on "Other" only because its real topic was retired.

    Separate from :func:`canonical_topic_name` because the two answers go to different places:
    the name goes on the speaker, this goes on the worklist.
    """
    return " ".join((raw_name or "").split()).casefold() in REVIEW_TOPICS


def ensure_speaker_topics():
    """Create any missing vocabulary rows. Returns how many were created."""
    from .models import SpeakerTopic

    created = 0
    for name in STARTER_TOPICS:
        # iexact so a differently-cased row that already exists is left alone rather than
        # gaining a near-duplicate twin.
        if not SpeakerTopic.objects.filter(name__iexact=name).exists():
            SpeakerTopic.objects.create(name=name)
            created += 1
    return created
