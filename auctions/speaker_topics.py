"""The speaker directory's fixed topic vocabulary.

Topics are a closed list: people pick from these and nothing in the UI creates new ones. The NEC
WordPress export arrived with three spellings of "cichlids", two of "africa" and a "Cichids" typo,
which is what happens when every contributor can coin a topic. A new topic is an admin decision.

:data:`STARTER_TOPICS` is the vocabulary and :data:`TOPIC_ALIASES` maps the old NEC taxonomy onto
it. Anything unrecognised becomes "Other"; the names that meant nothing (:data:`DISCARDED_TOPICS`)
are dropped; and :data:`REVIEW_TOPICS` -- old names whose topic was retired and that nothing can
re-file -- land on "Other" *and* flag the speaker for a human.

Migration 0374 creates the rows, and ``ensure_site_defaults`` calls :func:`ensure_speaker_topics` on
every start, which picks up a topic added after 0374 ran. Both are idempotent.

A full test run flushes the test database, so a ``--keepdb`` run starts without these rows: tests
that need topics call :func:`ensure_speaker_topics` themselves.
"""

OTHER = "Other"

#: The vocabulary, in dropdown order ("Other" is forced last).
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

#: Old NEC names (casefolded) thrown away rather than mapped: every freshwater speaker is a
#: "Freshwater species" speaker, and "General" was the export's shrug. They cover 112 of 405
#: speakers, so folding them into "Other" would make the largest topic the one carrying no
#: information. A speaker whose only topic was one of these ends up with none, which is what the old
#: taxonomy told us about them.
DISCARDED_TOPICS = {
    "freshwater species",
    "freshwater fish",
    "general",
    "general interest",
}

#: Old names (casefolded) whose topic was retired and that nothing can re-file on its own.
#:
#: "Cichlids" went because 55 of its 67 speakers also carried a specific cichlid topic. "Freshwater
#: Invertebrates" was two subjects in a trench coat: shrimp people and snail people, now under
#: "Shrimp" and "Other". Which one a speaker belongs under needs somebody to read their talk list,
#: so these set :attr:`Speaker.topics_need_review` and the admin filter is the worklist. Not aliases:
#: an alias is a mapping we trust, and the point of these is that we don't.
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
    # One topic for all the rift lakes: the export's "Rift Lakes" doesn't say which, and a talk on
    # Malawi haps is usually a talk on Tanganyikans too.
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
    # Snails are the half of the old invertebrates topic with nowhere better to go.
    "snails": OTHER,
    "us native fish": "Native & Wild-Caught Fish",
    "water quality": "Water Quality",
}


def canonical_topic_name(raw_name):
    """Map an incoming topic name onto the vocabulary.

    Returns a name from :data:`STARTER_TOPICS`, falling back to "Other", so an import can't widen the
    vocabulary. Returns None for a blank name and for a :data:`DISCARDED_TOPICS` name.

    A :data:`REVIEW_TOPICS` name also lands on "Other", but as a placeholder: callers should ask
    :func:`topic_needs_review` too and flag the speaker.
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

    Separate from :func:`canonical_topic_name` because the two answers go to different places: the name
    to the speaker, this to the worklist.
    """
    return " ".join((raw_name or "").split()).casefold() in REVIEW_TOPICS


def ensure_speaker_topics():
    """Create any missing vocabulary rows. Returns how many were created."""
    from .models import SpeakerTopic

    created = 0
    for name in STARTER_TOPICS:
        # iexact, so a differently-cased existing row is left alone rather than twinned.
        if not SpeakerTopic.objects.filter(name__iexact=name).exists():
            SpeakerTopic.objects.create(name=name)
            created += 1
    return created
