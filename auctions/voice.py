"""Voice-driven set winners: the grammar the mobile app listens with.

The app listens, but the grammar is data here, like
:class:`~auctions.models.ThermalPrinterProfile`: which words an auctioneer says is an admin edit,
not an app release. ``GET /api/mobile/config/`` serves it from
:class:`~auctions.models.VoiceGrammar`, and the app merges it over its bundled defaults.

No model imports at module level: ``models.py`` uses these as JSONField defaults and migrations
reference them by path.
"""

from django.core.cache import cache

# 'platform' = the phone's recognizer, 'biased' = platform plus a vocabulary hint, 'cloud' =
# server-side, 'spotter' = keyword spotting. The app decides what it can honour.
BACKEND_PLATFORM = "platform"
BACKEND_BIASED = "biased"
BACKEND_CLOUD = "cloud"
BACKEND_SPOTTER = "spotter"
BACKEND_CHOICES = [
    (BACKEND_PLATFORM, "Platform recognizer"),
    (BACKEND_BIASED, "Platform recognizer, vocabulary-biased"),
    (BACKEND_CLOUD, "Cloud recognizer"),
    (BACKEND_SPOTTER, "Keyword spotter"),
]

# Slots a command event can fill; both sides ignore ones they don't know.
SLOT_LOT = "lot"
SLOT_BIDDER = "bidder"
SLOT_PRICE = "price"
SLOT_SOLD = "sold"
SLOT_UNSOLD = "unsold"
SLOT_UNDO = "undo"
SLOT_CLEAR = "clear"
SLOT_CONFIRM = "confirm"
SLOT_CHOICES = [
    (SLOT_LOT, "Lot number"),
    (SLOT_BIDDER, "Bidder number"),
    (SLOT_PRICE, "Price"),
    (SLOT_SOLD, "Sold"),
    (SLOT_UNSOLD, "Unsold"),
    (SLOT_UNDO, "Undo"),
    (SLOT_CLEAR, "Clear"),
    (SLOT_CONFIRM, "Confirm"),
]
SLOTS = [slot for slot, _label in SLOT_CHOICES]

# An utterance that opened no slot is stored with a blank slot. Group by `heard` to find candidate
# anchor synonyms.
SLOT_UNMATCHED = ""

# The recognizer hears the whole room, so most phrases aren't commands: log one row per session per
# interval, and only for something long enough to be a command.
UNMATCHED_MIN_SECONDS = 5
UNMATCHED_MIN_TOKENS = 2


def default_anchors():
    """Words that say which field the number after them belongs to.

    Lowercase; order doesn't matter, except that the first word of ``price`` must stay ``dollars``:
    both recognizers format money before the app sees it, so the app substitutes this list's first word
    for a currency symbol.

    Keep in step with the app's bundled copy (``bundled_voice_grammar.dart``).
    """
    return {
        "lot": ["lot", "lot number", "item"],
        # "bitter"/"bidder": American English flaps both, so no acoustic model tells them apart.
        "bidder": ["bidder", "buyer", "bidder number", "paddle", "bitter"],
        "price": ["dollars", "dollar", "bucks"],
        "sold": ["sold", "hammer"],
        "unsold": ["no sale", "unsold", "pass"],
        "undo": ["undo", "scratch that"],
        "clear": ["clear", "cancel that", "start over"],
        "confirm": ["confirm", "yes", "correct"],
    }


def default_number_words():
    """Spoken number to digit. "oh" is here because bidder numbers are read digit by digit."""
    return {
        "zero": 0,
        "oh": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
        "hundred": 100,
        "thousand": 1000,
    }


def default_homophones():
    """Pairs that are a coin flip acoustically: teens and tens differ by an unstressed syllable."""
    return [
        ["13", "30"],
        ["14", "40"],
        ["15", "50"],
        ["16", "60"],
        ["17", "70"],
        ["18", "80"],
        ["19", "90"],
    ]


def default_weights():
    """How much each signal contributes to a command's confidence.

    Exponents: ``asr**asr x keyword**keyword x match**match x ((1 - agreement) + agreement * agreed)``.

    ``asr`` is the recognizer's own confidence (weighted 0.2, since platforms report it badly),
    ``keyword`` the anchor word quality (0.5, or no synonym could clear the ``unsure`` cutoff),
    ``match`` how well the value matched this auction's data, and ``agreement`` two passes agreeing.
    """
    return {"asr": 0.2, "keyword": 0.5, "match": 1.0, "agreement": 0.4}


def default_thresholds():
    """Score cutoffs: >= ``confident`` fills green, >= ``unsure`` fills amber and asks, else nothing.

    The gap between a canonical anchor with a value one edit away (0.765) and the cutoff is thin on
    purpose: an almost-right value must ask, a configured word must not.
    """
    return {"confident": 0.77, "unsure": 0.5}


# Milliseconds a spoken value must stop changing before the app commits it early, rather than
# waiting for the recognizer's final result. Actions always wait for finals. The app clamps to
# 200-2500; 0 means finals only.
DEFAULT_COMMIT_AFTER_MS = 700


def _as_confidence(value):
    """A score as a float, or None for anything else. Never raises: see ``log_command``."""
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def log_command(user, auction, *, log_id=None, slot="", heard="", chosen="", confidence=None, corrected_to=""):
    """Record or amend one voice command the set-winners page acted on; returns the row id.

    ``log_id`` amends a row this operator wrote, so a correction lands on it rather than as an orphan.
    Never raises for bad input: losing a sale to a logging error would be worse than losing the sample.
    """
    from auctions.models import VoiceCommandLog

    if slot not in SLOTS:
        return None
    fields = {
        "heard": str(heard or "")[:300],
        "chosen": str(chosen or "")[:100],
        "corrected_to": str(corrected_to or "")[:100],
        "confidence": _as_confidence(confidence),
    }

    if log_id:
        existing = VoiceCommandLog.objects.filter(pk=log_id, auction=auction, user=user).first()
        if existing:
            # Only overwrite fields the caller actually filled in.
            for name, value in fields.items():
                if value not in (None, ""):
                    setattr(existing, name, value)
            existing.save()
            return existing.pk
    return VoiceCommandLog.objects.create(auction=auction, user=user, slot=slot, **fields).pk


# Slots that are a whole command on their own; see :func:`_is_action_word`.
ACTION_SLOTS = (SLOT_SOLD, SLOT_UNSOLD, SLOT_UNDO, SLOT_CLEAR, SLOT_CONFIRM)


def _is_action_word(word, anchors=None):
    """Whether one word alone is (or pluralises) an anchor for an action slot.

    Exempt from the two-token floor: "sold" said alone is a command, not room noise.
    """
    word = " ".join(str(word or "").split()).lower()
    if not word:
        return False
    stems = {word}
    if word.endswith("s") and not word.endswith("ss"):
        stems.add(word[:-1])
    anchors = anchors or default_anchors()
    for slot in ACTION_SLOTS:
        for phrase in anchors.get(slot) or []:
            if str(phrase).strip().lower() in stems:
                return True
    return False


def log_unmatched(user, auction, *, heard="", confidence=None, session_key=""):
    """Record one utterance that matched nothing, or scored below the ``unsure`` cutoff. Slot and
    ``chosen`` stay blank.

    Dropped when shorter than :data:`UNMATCHED_MIN_TOKENS` words, or when this session logged one
    within :data:`UNMATCHED_MIN_SECONDS`. Returns the row id, or None. Never raises.
    """
    from auctions.models import VoiceCommandLog, VoiceGrammar

    heard = " ".join(str(heard or "").split())[:300]
    words = heard.split(" ")
    if len(words) < UNMATCHED_MIN_TOKENS:
        grammar = VoiceGrammar.load()
        if not _is_action_word(words[0], (grammar.anchors if grammar else None) or default_anchors()):
            return None
    # cache.add rate-limits with no stored window. Per session, not per user: two handsets are two
    # microphones in two parts of the room.
    scope = session_key or f"user-{getattr(user, 'pk', '')}"
    if not cache.add(f"voice-unmatched:{auction.pk}:{scope}", 1, UNMATCHED_MIN_SECONDS):
        return None
    return VoiceCommandLog.objects.create(
        auction=auction,
        user=user,
        slot=SLOT_UNMATCHED,
        heard=heard,
        confidence=_as_confidence(confidence),
    ).pk


def serialize_grammar(grammar):
    """Shape a :class:`~auctions.models.VoiceGrammar` for mobile config's ``voice`` block.

    ``None`` serves this module's defaults rather than omitting the block: the server's defaults are
    the grammar, and the app's bundled copy is only for before it first hears from us.
    """
    if grammar is None:
        from .models import VoiceGrammar

        grammar = VoiceGrammar()
    return {
        "enabled": grammar.enabled,
        "backend": grammar.backend,
        "locale": grammar.locale,
        "prefer_on_device": grammar.prefer_on_device,
        "anchors": grammar.anchors,
        "number_words": grammar.number_words,
        "homophones": grammar.homophones,
        "weights": grammar.weights,
        "thresholds": grammar.thresholds,
        "commit_after_ms": grammar.commit_after_ms,
        "auto_submit_on_sold": grammar.auto_submit_on_sold,
        "block_auto_submit_when_unsure": grammar.block_auto_submit_when_unsure,
    }


def page_config(auction, grammar=None):
    """Everything the set-winners page needs to match a spoken command itself.

    The app listens, but it can hear something and produce no command, with nothing on the page able to
    tell a grammar gap from a matcher that never ran. So the page gets the grammar plus this auction's
    vocabulary. ``grammar`` is passed when the caller loaded the singleton.
    """
    from .mobile.services import voice as voice_service
    from .models import VoiceGrammar

    if grammar is None:
        grammar = VoiceGrammar.load()
    thresholds = (grammar.thresholds if grammar else None) or default_thresholds()
    defaults = default_thresholds()
    config = {
        "enabled": grammar.enabled if grammar else True,
        "confident": thresholds.get("confident", defaults["confident"]),
        "unsure": thresholds.get("unsure", defaults["unsure"]),
        "block_auto_submit_when_unsure": grammar.block_auto_submit_when_unsure if grammar else True,
        "auto_submit_on_sold": grammar.auto_submit_on_sold if grammar else True,
        "anchors": (grammar.anchors if grammar else None) or default_anchors(),
        "number_words": (grammar.number_words if grammar else None) or default_number_words(),
        "homophones": (grammar.homophones if grammar else None) or default_homophones(),
    }
    config.update(voice_service.build_vocabulary(auction))
    return config
