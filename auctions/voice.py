"""Voice-driven set winners: the grammar the mobile app listens with.

The app does the listening (native speech recognition — iOS ``WKWebView`` has no Web Speech API,
and the shell denies the WebView's microphone), but the *grammar* lives here, as data, for the same
reason a :class:`~auctions.models.ThermalPrinterProfile` does: which words a given auctioneer
actually says is the thing we'll be wrong about on day one, and the fix has to be an admin edit, not
an app release. ``GET /api/mobile/config/`` serves the block from
:class:`~auctions.models.VoiceGrammar`; the app merges it over the defaults it ships with, so a
deployment that has never touched the admin page still works.

Nothing in here imports models at module level — ``models.py`` uses these functions as JSONField
defaults, so they have to stay importable from it (and stay put: migrations reference them by
dotted path).
"""

from django.core.cache import cache

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

# An utterance that opened no slot is stored with the slot left blank. Those rows are the whole
# reason the log is honest: "bitter" for "bidder" matched nothing, produced no command and reached
# no table, so a log of accepted commands can only ever show words we already handle. Group these by
# `heard`, order by count, and anything frequent is a candidate anchor synonym — a VoiceGrammar edit
# that ships without an app release.
SLOT_UNMATCHED = ""

# What an unmatched utterance has to clear to be worth a row. The recognizer listens continuously
# and hears the whole room, so most of what it transcribes was never addressed to the app; logging
# every phrase would bury the misheard commands under a transcript of the auction hall. One row per
# session per interval, and only for something long enough to be a command in the first place.
UNMATCHED_MIN_SECONDS = 5
UNMATCHED_MIN_TOKENS = 2


def default_anchors():
    """Words that say *which field* the number that follows belongs to.

    Order doesn't matter to the matcher; the app accepts any of them. Keep entries lowercase — the
    app lowercases the utterance before comparing.

    The **first** word of ``price`` is the one exception, and it has to stay ``dollars``. Both
    recognizers format money out of the transcript before the app ever sees it: "twenty five dollars"
    arrives as ``$25`` from iOS ``SFTranscription.formattedString`` and from Android's
    ``RESULTS_RECOGNITION``, so the spoken anchor is absent from almost every real utterance and the
    price slot never filled. The app now reads a currency symbol immediately in front of a number as
    the price anchor, substituting the canonical (first) word of this list — which is what lets a
    deployment rename the anchor without breaking, and what makes the first entry load-bearing.

    Keep this in step with the app's bundled copy (``bundled_voice_grammar.dart``). A served list
    *replaces* the app's for that slot, so a word missing here is a word the app stops accepting the
    moment this block reaches it.
    """
    return {
        "lot": ["lot", "lot number", "item"],
        # "bitter" is not a mishearing to forgive, it is the same sound: American English flaps the
        # consonant in both words, so there is nothing in the audio that separates them and no
        # acoustic model will ever fix it. Listed outright rather than left to the app's fuzzy pass,
        # which scores a guess (0.6) and leaves every bidder amber -- and which the page's own
        # matcher would otherwise have to guess its way to as well.
        "bidder": ["bidder", "buyer", "bidder number", "paddle", "bitter"],
        "price": ["dollars", "dollar", "bucks"],
        "sold": ["sold", "hammer"],
        "unsold": ["no sale", "unsold", "pass"],
        "undo": ["undo", "scratch that"],
        "clear": ["clear", "cancel that", "start over"],
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

    These are **exponents**, not multipliers. The app scores a command as
    ``asr**asr × keyword**keyword × match**match × ((1 - agreement) + agreement × agreed)``, so a
    weight of 0 switches a signal off entirely and 1 lets it count in full.

    ``asr`` is the recognizer's own confidence, ``keyword`` the anchor word's quality (1.0 for the
    first word of a slot's list, 0.8 for one of the synonyms after it, 0.6 for a fuzzy hit),
    ``match`` how cleanly the value matched something this auction actually has, ``agreement`` two
    passes landing on the same answer.

    **The key is ``match``.** This table said ``snap`` until 2026-09-12, and the app has always read
    ``match`` -- so the one weight describing the vocabulary match was unreachable from the admin,
    and every edit to it did nothing at all.

    Two of these are deliberately low, and both were 1.0-scale mistakes that made voice feel broken:

    - **asr 0.2.** The platforms report their own confidence badly -- iOS on-device results and
      Android partials say -1 ("don't know") constantly, and a phone that reports an honest 0.6 for
      a sentence it heard perfectly used to drag every field under the confident cutoff, which with
      ``block_auto_submit_when_unsure`` meant no "sold" ever saved. At 0.2 a recognizer's doubt
      shades the score instead of deciding it.
    - **keyword 0.5.** At 1.0 no synonym could ever clear the cutoff: "buyer" scored 0.8 against a
      0.85 threshold however perfectly the bidder number matched. That made the entire point of this
      table -- add the word your auctioneer actually says -- produce nothing but amber fields that
      then blocked the save.
    """
    return {"asr": 0.2, "keyword": 0.5, "match": 1.0, "agreement": 0.4}


def default_thresholds():
    """Score cutoffs: at or above ``confident`` the page fills the field green, at or above
    ``unsure`` it fills it amber and asks, below ``unsure`` the app sends no command at all.

    0.77 is where it is because of what has to land on either side of it. With
    :func:`default_weights` and a recognizer that reports nothing (asr 0.8, so ``asr**0.2`` = 0.956):

    ======================================  =====  ========
    reading                                 score  tier
    ======================================  =====  ========
    canonical anchor + a real value         0.956  green
    configured synonym + a real value       0.855  green
    canonical anchor + value one edit away  0.765  amber
    fuzzy anchor ("bitter") + a real value  0.741  amber
    canonical anchor + two values fit        0.622  amber
    canonical anchor + no such value here   0.593  amber
    fuzzy anchor + no such value here       0.459  dropped
    ======================================  =====  ========

    The gap between the third row and the cutoff is 0.005, which is thin and deliberate: a value one
    edit from a real one has to ask, and a word the deployment configured itself must not have to.
    A phone that reports an honest 0.5 still fills a canonical match green (0.871), which is the
    failure this number was moved to fix.
    """
    return {"confident": 0.77, "unsure": 0.5}


# How long a spoken *value* (lot, bidder, price) has to stop changing in the partial transcript
# before the app writes it, in milliseconds. Before this existed a value waited for the recognizer's
# final result, which only arrives once its three-second silence window has run out -- five or six
# seconds between "lot one" and a filled field, and the window stays long on purpose because every
# utterance end costs a restart's deafness. Actions ("sold", "undo") still act on finals only.
#
# Served in the ``voice`` block of mobile config. The app clamps what it gets to 200-2500, so a typo
# can neither commit mid-word nor outwait the window this exists to beat, and reads 0 as "finals
# only" -- the kill switch if early values misbehave in a hall, and the reason to serve the number
# rather than bake it into the app.
DEFAULT_COMMIT_AFTER_MS = 700


def _as_confidence(value):
    """A score as a float, or None for anything that isn't one. Never raises: see ``log_command``."""
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


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
        "confidence": _as_confidence(confidence),
    }

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


# The slots that are a whole command on their own. An utterance that is only one of these words is
# somebody selling a lot, not somebody walking past the phone -- see :func:`_is_action_word`.
ACTION_SLOTS = (SLOT_SOLD, SLOT_UNSOLD, SLOT_UNDO, SLOT_CLEAR, SLOT_CONFIRM)


def _is_action_word(word, anchors=None):
    """Whether one word on its own is (or is the plural of) an anchor for an action slot.

    "sold", heard and matched by nothing, is the single most useful row this table can hold, and the
    two-token floor was the reason it never appeared in it. Everything else about that floor stays:
    the recognizer hears the whole room, and one-word utterances are mostly the room.
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
    """Record one utterance that matched nothing — the row a log of accepted commands can't hold.

    Written for a final transcript that produced no command at all (``confidence`` None: there was
    no score, because nothing scored), and for one that produced a command below the ``unsure``
    cutoff, which is a near miss and names a word the grammar nearly knows. The slot is blank
    because none was opened, and ``chosen`` stays blank because nothing was filled in.

    Dropped rather than logged when the utterance is shorter than :data:`UNMATCHED_MIN_TOKENS`
    words, or when this session already logged one inside :data:`UNMATCHED_MIN_SECONDS` — the page
    applies the same two rules before posting, and this is the side that decides, because the table
    is the thing being protected. Returns the row's id, or None when it was dropped.

    Never raises for bad input, for the reason :func:`log_command` doesn't: losing a sale to a
    logging error would be a considerably worse bug than losing the sample.
    """
    from auctions.models import VoiceCommandLog, VoiceGrammar

    heard = " ".join(str(heard or "").split())[:300]
    words = heard.split(" ")
    if len(words) < UNMATCHED_MIN_TOKENS:
        grammar = VoiceGrammar.load()
        if not _is_action_word(words[0], (grammar.anchors if grammar else None) or default_anchors()):
            return None
    # cache.add only succeeds when nothing is there and the key expires by itself, so the rate limit
    # needs no window stored anywhere and nothing to clean up. Per session rather than per user: an
    # operator running two handsets is two microphones in two parts of the room, not one.
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

    ``None`` -- nobody has made a row -- serves the defaults in this module rather than omitting the
    block, and that is the point rather than a convenience. The app carries a bundled copy of these
    values for a first run that never reached the server, and while the block was optional that copy
    was also what every deployment without a row actually ran on: :func:`page_config` has always
    fallen back to the functions here, so retuning a default moved the *page* and left the *app*
    scoring the same utterance by last year's numbers. The server's defaults are the grammar; the
    app's are what it does when it has never heard from us.
    """
    if grammar is None:
        # An unsaved row, so "no grammar configured" serves exactly what creating one in the admin
        # would start as -- one definition of the defaults rather than two that can drift.
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
    """Everything the set-winners *page* needs to understand a spoken command on its own.

    The app is the one listening, and until now that was also the only thing that could *match*:
    the page received ``command`` events and did as it was told. That works right up until the app
    hears something and produces no command -- which is what "it says heard: lot one and then
    nothing happens" is -- and at that point there is nothing on the page that can tell the
    difference between a grammar that does not know the word and a matcher that never ran.

    So the page gets the grammar and this auction's own vocabulary as well, and matches the
    transcript itself when a command does not arrive. That is deliberately the same trick the app
    uses and the reason it can be strict: it is not transcribing freely and repairing the text
    afterwards, it is checking the words against the lot and bidder numbers that actually exist
    here. A page-side fallback is also the shape a fix has to take in this feature -- the app is
    shipped through two app stores and this file exists precisely so that "the auctioneer says
    something we did not expect" is a server-side change.

    ``grammar`` is passed in when the caller has already loaded the singleton; ``None`` means load
    it, and no row at all means the defaults, which are what the app ships with.
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
