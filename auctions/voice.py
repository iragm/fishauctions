"""Voice-driven set winners: the grammar the mobile app listens with.

The app does the listening (native speech recognition — iOS ``WKWebView`` has no Web Speech API,
and the shell denies the WebView's microphone), but the *grammar* lives here, as data, for the same
reason a :class:`~auctions.models.ThermalPrinterProfile` does: which words a given auctioneer
actually says is the thing we'll be wrong about on day one, and the fix has to be an admin edit, not
an app release. ``GET /api/mobile/config/`` serves the block from
:class:`~auctions.models.VoiceGrammar`; the app merges it over the defaults it ships with, so a
deployment that has never touched the admin page still works.

Nothing in here imports models — ``models.py`` uses these functions as JSONField defaults, so they
have to stay importable from it (and stay put: migrations reference them by dotted path).
"""

# How the app should listen. 'platform' is the phone's own recognizer; 'biased' feeds it the
# auction's vocabulary as a contextual hint where the OS supports that; 'cloud' is a server-side
# recognizer; 'spotter' is keyword-spotting only. The app decides what it can actually honour and
# reports back through voiceGetState() — this is a request, not a guarantee.
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

# The slots a command event can fill. Both sides ignore slots they don't know, which is what lets
# either add one without shipping the other.
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


def default_anchors():
    """Words that say *which field* the number that follows belongs to.

    Order doesn't matter; the app matches any of them. Keep entries lowercase — the app lowercases
    the utterance before comparing.
    """
    return {
        "lot": ["lot", "lot number", "item"],
        "bidder": ["bidder", "buyer", "bidder number"],
        "price": ["dollars", "dollar", "bucks"],
        "sold": ["sold", "hammer"],
        "unsold": ["no sale", "unsold", "pass"],
        "undo": ["undo", "scratch that"],
        "clear": ["clear", "cancel that"],
        "confirm": ["confirm", "yes", "correct"],
    }


def default_number_words():
    """Spoken number → digit value, for expanding an utterance back into a number.

    "oh" is here because bidder numbers get read out digit by digit ("four oh two").
    """
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
    """Pairs that are a coin flip acoustically, so the matcher knows to consider both.

    The teens and their matching tens are the whole problem in a room with a PA system: "fifteen"
    and "fifty" differ by an unstressed syllable nobody enunciates while selling fast.
    """
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

    ``asr`` is the recognizer's own confidence, ``keyword`` an anchor word being present, ``snap`` a
    clean match against a value that actually exists in this auction's vocabulary, ``agreement``
    two passes landing on the same answer.
    """
    return {"asr": 0.5, "keyword": 1.0, "snap": 1.0, "agreement": 0.4}


def default_thresholds():
    """Score cutoffs: at or above ``confident`` the page fills the field green, at or above
    ``unsure`` it fills it amber and asks, below ``unsure`` the app sends no command at all."""
    return {"confident": 0.85, "unsure": 0.5}


def log_command(user, auction, *, log_id=None, slot="", heard="", chosen="", confidence=None, corrected_to=""):
    """Record (or amend) one voice command the set-winners page acted on. Returns the row's id.

    Pass ``log_id`` to amend a row this operator already wrote — that's how a correction lands on
    the same row as the command it corrects, instead of arriving as an orphan nobody can pair up.
    Amending is scoped to the caller's own rows in this auction, so an id from somewhere else
    silently starts a new row rather than editing a stranger's.

    Never raises for bad input: this is telemetry on a page whose real job is selling lots fast, and
    losing a sale to a logging error would be a considerably worse bug than losing the sample.
    """
    from auctions.models import VoiceCommandLog

    if slot not in SLOTS:
        return None
    fields = {
        "heard": str(heard or "")[:300],
        "chosen": str(chosen or "")[:100],
        "corrected_to": str(corrected_to or "")[:100],
    }
    try:
        fields["confidence"] = None if confidence in (None, "") else float(confidence)
    except (TypeError, ValueError):
        fields["confidence"] = None

    if log_id:
        existing = VoiceCommandLog.objects.filter(pk=log_id, auction=auction, user=user).first()
        if existing:
            # A correction knows what the operator typed, not what was originally heard; only
            # overwrite fields the caller actually filled in.
            for name, value in fields.items():
                if value not in (None, ""):
                    setattr(existing, name, value)
            existing.save()
            return existing.pk
    return VoiceCommandLog.objects.create(auction=auction, user=user, slot=slot, **fields).pk


def serialize_grammar(grammar):
    """Shape a :class:`~auctions.models.VoiceGrammar` for the ``voice`` block of mobile config."""
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
        "auto_submit_on_sold": grammar.auto_submit_on_sold,
        "block_auto_submit_when_unsure": grammar.block_auto_submit_when_unsure,
    }
