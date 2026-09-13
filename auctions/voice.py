"""Voice-driven set winners: the grammar the mobile app listens with.

The app does the listening (native speech recognition), but the grammar lives here as data, like
:class:`~auctions.models.ThermalPrinterProfile`: which words an auctioneer actually says is an admin
edit, not an app release. ``GET /api/mobile/config/`` serves this from
:class:`~auctions.models.VoiceGrammar`; the app merges it over its own bundled defaults.

No model imports at module level -- ``models.py`` uses these functions as JSONField defaults, and
migrations reference them by dotted path, so they must stay importable and stay put.
"""

from django.core.cache import cache

# 'platform' = phone's own recognizer; 'biased' = platform + vocabulary hint; 'cloud' = server-side
# recognizer; 'spotter' = keyword-spotting only. The app decides what it can honour; not a guarantee.
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

# Slots a command event can fill. Both sides ignore slots they don't know.
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

# An utterance that opened no slot is stored with the slot blank. Group by `heard`, order by
# count -- a frequent miss is a candidate anchor synonym to add via VoiceGrammar.
SLOT_UNMATCHED = ""

# The recognizer hears the whole room continuously, so most transcribed phrases aren't commands.
# Log one row per session per interval, only for something long enough to plausibly be a command.
UNMATCHED_MIN_SECONDS = 5
UNMATCHED_MIN_TOKENS = 2


def default_anchors():
    """Words that say which field the number that follows belongs to.

    Order doesn't matter; keep entries lowercase (the app lowercases before comparing). The first
    word of ``price`` must stay ``dollars``: both recognizers format money before the app sees it
    ("twenty five dollars" arrives as ``$25``), so the app reads a currency symbol as the price
    anchor and substitutes this list's first word -- making that entry load-bearing.

    Keep in step with the app's bundled copy (``bundled_voice_grammar.dart``): a served list
    replaces the app's for that slot.
    """
    return {
        "lot": ["lot", "lot number", "item"],
        # "bitter"/"bidder": American English flaps the consonant in both, so no acoustic model
        # will ever tell them apart. Listed outright rather than left to the fuzzy pass.
        "bidder": ["bidder", "buyer", "bidder number", "paddle", "bitter"],
        "price": ["dollars", "dollar", "bucks"],
        "sold": ["sold", "hammer"],
        "unsold": ["no sale", "unsold", "pass"],
        "undo": ["undo", "scratch that"],
        "clear": ["clear", "cancel that", "start over"],
        "confirm": ["confirm", "yes", "correct"],
    }


def default_number_words():
    """Spoken number to digit value. "oh" is here because bidder numbers get read digit by digit."""
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
    """Pairs that are a coin flip acoustically: teens vs. tens differ by an unstressed syllable."""
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
    """How much each signal contributes to a command's confidence score.

    Exponents, not multipliers: ``asr**asr x keyword**keyword x match**match x
    ((1 - agreement) + agreement * agreed)``. 0 switches a signal off, 1 lets it count in full.

    ``asr`` = recognizer's own confidence, ``keyword`` = anchor word quality (1.0 canonical, 0.8
    synonym, 0.6 fuzzy), ``match`` = how well the value matched this auction's data, ``agreement``
    = two passes landing on the same answer.

    asr is weighted low (0.2): platforms report their own confidence badly, so a low ``asr`` should
    shade the score, not decide it. keyword is weighted low (0.5): at 1.0 no synonym could ever
    clear the ``unsure`` cutoff.
    """
    return {"asr": 0.2, "keyword": 0.5, "match": 1.0, "agreement": 0.4}


def default_thresholds():
    """Score cutoffs: >= ``confident`` fills green, >= ``unsure`` fills amber and asks, else no command.

    The gap between a canonical anchor with a value one edit away (0.765) and the 0.77 cutoff is
    deliberately thin: an almost-right value must ask, a deployment-configured word must not.
    """
    return {"confident": 0.77, "unsure": 0.5}


# Milliseconds a spoken value (lot, bidder, price) must stop changing before the app commits it
# early, instead of waiting for the recognizer's final result (~3s silence window). Actions
# ("sold", "undo") always wait for finals. The app clamps this to 200-2500; 0 means finals only.
DEFAULT_COMMIT_AFTER_MS = 700


def _as_confidence(value):
    """A score as a float, or None for anything that isn't one. Never raises: see ``log_command``."""
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def log_command(user, auction, *, log_id=None, slot="", heard="", chosen="", confidence=None, corrected_to=""):
    """Record (or amend) one voice command the set-winners page acted on. Returns the row's id.

    Pass ``log_id`` to amend a row this operator already wrote, so a correction lands on the same
    row rather than as an orphan. Scoped to the caller's own rows in this auction.

    Never raises for bad input: this is telemetry, and losing a sale to a logging error would be
    worse than losing the sample.
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


# Slots that are a whole command on their own: see :func:`_is_action_word`.
ACTION_SLOTS = (SLOT_SOLD, SLOT_UNSOLD, SLOT_UNDO, SLOT_CLEAR, SLOT_CONFIRM)


def _is_action_word(word, anchors=None):
    """Whether one word alone is (or is the plural of) an anchor for an action slot.

    Exempt from the two-token floor below: "sold" said alone is a real command, not room noise.
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
    """Record one utterance that matched nothing, for a final transcript with no command at all
    or one that scored below the ``unsure`` cutoff -- a near miss naming a word the grammar almost
    knows. Slot and ``chosen`` stay blank.

    Dropped when shorter than :data:`UNMATCHED_MIN_TOKENS` words, or when this session already
    logged one within :data:`UNMATCHED_MIN_SECONDS`. Returns the row's id, or None if dropped.

    Never raises for bad input, same reason as :func:`log_command`.
    """
    from auctions.models import VoiceCommandLog, VoiceGrammar

    heard = " ".join(str(heard or "").split())[:300]
    words = heard.split(" ")
    if len(words) < UNMATCHED_MIN_TOKENS:
        grammar = VoiceGrammar.load()
        if not _is_action_word(words[0], (grammar.anchors if grammar else None) or default_anchors()):
            return None
    # cache.add rate-limits with no window stored anywhere. Per session, not per user: two
    # handsets are two microphones in two parts of the room.
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
    """Shape a :class:`~auctions.models.VoiceGrammar` for the ``voice`` block of mobile config.

    ``None`` (no row saved) serves the defaults in this module rather than omitting the block --
    the server's defaults are the grammar; the app's bundled copy is only for before it first hears
    from us.
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
    """Everything the set-winners page needs to match a spoken command on its own.

    The app is what listens, but it can hear something and produce no command -- "it says heard:
    lot one and then nothing happens" -- with nothing on the page able to tell a grammar gap from a
    matcher that never ran. So the page gets the grammar plus this auction's own vocabulary and
    matches the transcript itself when no command arrives, the same way the app does.

    ``grammar`` is passed when the caller already loaded the singleton; ``None`` loads it, and no
    row at all falls back to defaults.
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
