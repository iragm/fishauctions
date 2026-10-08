"""Voice set-winners: what the auctioneer said, read as a sale.

The phone sits by the auctioneer, who calls the auction the way they always do. The set-winners page
posts everything heard since the lot on the block came up -- the *window*, oldest segment first --
and :func:`interpret` answers with commands for the three fields and the save, in the shape the page
already applies.

The hard part is telling the bidding from the close. "Ten, twelve anyone? Going once, going twice,
sold to one oh four" is one sale, at ten, to bidder 104: twelve was asked for and nobody took it.

Rules that keep a wrong sale from being saved:

- A lot or bidder is only ever a value this auction has (``build_vocabulary``). Prices have no list,
  so they come from the close ("for ten", "$10", "ten dollars") or from the last bid the auctioneer
  *had* rather than asked for.
- "For", "to" and "won" are never digits. A recognizer that hears "sold for twelve" means twelve.
- A lot named after "sold" is the next lot's, never this one's.
- One close per answer. The page saves it and posts the rest (``carry``) as the next lot's window.
- A close missing its bidder or price waits for the rest. If a new lot or a new round of bidding
  arrives first, it is reported in ``missed`` rather than finished with somebody else's numbers.
- Saying the close twice ("Sold! One oh four, ten dollars") is one sale. ``previous`` is the sale the
  page just recorded, so a repeat at the start of the next window isn't sold again to the next lot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from auctions import voice

ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
TEENS = {
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
}
TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fourty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
SCALES = {"hundred": 100, "thousand": 1000}
#: Zero, but only inside a number: "one oh four". On its own "oh" is "oh".
ZEROS_INSIDE = {"oh", "o"}

#: Letters as people say them, for bidder numbers like "NM". Only ever read after a bidder word, and
#: only kept when the result is a bidder this auction has: "see", "you" and "why" are English first.
LETTER_NAMES = {
    "ay": "a",
    "bee": "b",
    "be": "b",
    "see": "c",
    "sea": "c",
    "dee": "d",
    "ee": "e",
    "ef": "f",
    "eff": "f",
    "gee": "g",
    "aitch": "h",
    "eye": "i",
    "jay": "j",
    "kay": "k",
    "el": "l",
    "ell": "l",
    "em": "m",
    "en": "n",
    "oh": "o",
    "pee": "p",
    "cue": "q",
    "ar": "r",
    "are": "r",
    "es": "s",
    "ess": "s",
    "tee": "t",
    "tea": "t",
    "you": "u",
    "vee": "v",
    "ex": "x",
    "why": "y",
    "zee": "z",
    "zed": "z",
}
NATO = {
    "alpha": "a",
    "alfa": "a",
    "bravo": "b",
    "charlie": "c",
    "delta": "d",
    "echo": "e",
    "foxtrot": "f",
    "golf": "g",
    "hotel": "h",
    "india": "i",
    "juliet": "j",
    "juliett": "j",
    "kilo": "k",
    "lima": "l",
    "mike": "m",
    "november": "n",
    "oscar": "o",
    "papa": "p",
    "quebec": "q",
    "romeo": "r",
    "sierra": "s",
    "tango": "t",
    "uniform": "u",
    "victor": "v",
    "whiskey": "w",
    "xray": "x",
    "yankee": "y",
    "zulu": "z",
}
DASH_WORDS = {"dash", "hyphen"}
#: The start of a number cut off where a recognizer ended a segment, then said whole at the start of the
#: next: "Lot four | Forty-two", "Lot forty | 44", "bidder one. | One oh four". The cut-off word goes.
CUT_OFF = {
    "two": {"twelve", "twenty"},
    "three": {"thirteen", "thirty"},
    "four": {"fourteen", "forty", "fourty"},
    "five": {"fifteen", "fifty"},
    "six": {"sixteen", "sixty"},
    "seven": {"seventeen", "seventy"},
    "eight": {"eighteen", "eighty"},
    "nine": {"nineteen", "ninety"},
}

#: "Sold *to* one oh four", "sold *for* ten", "sold *at* twenty five".
TO_WORDS = {"to"}
PRICE_CONNECTORS = {"for", "at"}
#: Said in front of a bidder number on top of the grammar's own bidder words.
BIDDER_MARKERS = {"number", "paddle", "card"}

#: A bid the auctioneer is asking for rather than has: "do I *hear* ten", "twelve *anyone*?"
ASK_BEFORE = {"hear", "give", "get", "start", "about", "say", "make", "need", "want", "try", "go"}
ASK_AFTER = {"anyone", "anybody", "somebody", "someone"}
#: Between an unanswered ask and "sold": the ask wasn't taken. "Twelve anyone? Going once..."
CLOSING_WORDS = {"going", "once", "twice", "warning", "last", "final"}
#: "Sold to one oh four, sorry, one oh five": the number after one of these replaces the one before it.
CORRECTION_WORDS = {"sorry", "correction", "actually", "mean", "rather"}
#: "Two dollars fifty" is two fifty only where cents are allowed, and only for amounts people say.
SPOKEN_CENTS = {10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95}
#: What noise makes of "sold". The grammar's own words are matched too.
SOLD_HEARD_AS = {("soul",), ("sole",)}
#: "this was sold", "these sold for twenty last time": past tense, not a close.
NOT_A_CLOSE_BEFORE = {"was", "were", "been", "already", "usually", "normally", "never", "these", "those", "they"}
NOT_A_CLOSE_AFTER = {"out"}
#: "this one", "the big one": a pronoun, not a bid.
ONE_IS_A_PRONOUN_AFTER = {
    "this",
    "that",
    "the",
    "which",
    "each",
    "every",
    "another",
    "any",
    "no",
    "big",
    "little",
    "nice",
}

#: Confidence for each way of hearing a value; the page's cutoffs (``VoiceGrammar.thresholds``) decide
#: green or amber.
SAID_OUTRIGHT = 0.95
AFTER_THE_CLOSE = 0.9
FROM_THE_BIDDING = 0.85
GUESSED = 0.7
SOUNDS_LIKE = 0.6

#: How long a sale whose price came from the bidding waits for a price said outright.
PRICE_MAY_FOLLOW_MS = 2500

#: How far either side of "sold" the close is looked for, in tokens.
CLOSE_REACH_AFTER = 18
CLOSE_REACH_BEFORE = 10


@dataclass
class Token:
    kind: str  # "money", "id", "num", "word", "punct"
    text: str
    start: int
    end: int
    amount: Decimal | None = None


@dataclass
class Vocabulary:
    """This auction's legal answers, matched without regard to case and returned as stored."""

    lots: list[str]
    bidders: list[str]
    whole_dollars: bool = True
    lot_lookup: dict = field(init=False)
    bidder_lookup: dict = field(init=False)

    def __post_init__(self):
        self.lot_lookup = {str(value).lower(): str(value) for value in self.lots}
        self.bidder_lookup = {str(value).lower(): str(value) for value in self.bidders}

    @classmethod
    def from_payload(cls, payload):
        """From ``mobile.services.voice.build_vocabulary``."""
        return cls(
            lots=[str(value) for value in payload.get("lot_numbers") or []],
            bidders=[str(value) for value in payload.get("bidder_numbers") or []],
            whole_dollars=bool(payload.get("only_whole_dollar_bids")),
        )


@dataclass
class Grammar:
    """The words from :class:`~auctions.models.VoiceGrammar`, as tuples of tokens per slot."""

    anchors: dict
    homophones: list
    extra_numbers: dict

    @classmethod
    def from_model(cls, grammar=None):
        anchors = (getattr(grammar, "anchors", None) or {}) or voice.default_anchors()
        defaults = voice.default_anchors()
        phrases = {}
        for slot in ("lot", "bidder", "price", "sold", "unsold", "undo"):
            words = anchors.get(slot) or defaults.get(slot) or []
            found = {tuple(str(phrase).lower().split()) for phrase in words if str(phrase).strip()}
            if slot == "sold":
                found |= SOLD_HEARD_AS
            phrases[slot] = sorted(found, key=len, reverse=True)
        known = set(ONES) | set(TEENS) | set(TENS) | set(SCALES) | ZEROS_INSIDE
        extra = {
            str(word).lower(): value
            for word, value in ((getattr(grammar, "number_words", None) or {}).items())
            if str(word).lower() not in known and isinstance(value, int) and value >= 0
        }
        homophones = (getattr(grammar, "homophones", None) or []) or voice.default_homophones()
        return cls(anchors=phrases, homophones=homophones, extra_numbers=extra)

    def alternatives(self, digits):
        """The values a homophone pair says ``digits`` might really have been."""
        out = []
        for pair in self.homophones:
            values = [str(value) for value in pair or []]
            if digits in values:
                out += [value for value in values if value != digits and value not in out]
        return out


@dataclass
class Command:
    slot: str
    value: str = ""
    confidence: float = SAID_OUTRIGHT
    heard: str = ""
    candidates: list = field(default_factory=list)

    def as_dict(self):
        return {
            "slot": self.slot,
            "value": self.value,
            "confidence": self.confidence,
            "heard": self.heard,
            "candidates": self.candidates,
        }


@dataclass
class Reading:
    """What :func:`interpret` makes of a window.

    ``carry`` is the window after the close it found, to use once the page has acted on it (None: no
    close yet). ``keep`` is the window to use straight away, with a repeated close or a reported miss
    taken off the front (None: unchanged). ``missed`` is sales heard that could not be finished, each
    ``{"lot", "heard"}``. ``note`` says what a waiting close still needs, or why a number was ignored.
    ``wait`` is how many milliseconds the page should hold a sale for anything else heard first: a price
    taken from the bidding, at the very end of what was heard, may be about to be said outright.
    """

    commands: list = field(default_factory=list)
    carry: list | None = None
    keep: list | None = None
    missed: list = field(default_factory=list)
    note: str = ""
    wait: int = 0

    def as_dict(self):
        return {
            "commands": [command.as_dict() for command in self.commands],
            "carry": self.carry,
            "keep": self.keep,
            "missed": self.missed,
            "note": self.note,
            "wait": self.wait,
        }


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<money>[$£€¥]\s?\d[\d,]*(?:\.\d+)?)
    | (?P<id>(?:\d+|[a-z]+)(?:-(?:\d+|[a-z]+))+)
    | (?P<num>\d[\d,]*(?:\.\d+)?)
    | (?P<word>[a-z]+(?:'[a-z]+)?)
    | (?P<punct>[.,!?;:|])
    """,
    re.VERBOSE,
)
#: Between segments: a pause the recognizer heard, read like a full stop.
SEGMENT_BREAK = " | "


def _normalize(text):
    text = str(text or "").lower().replace("’", "'").replace("#", " number ")
    # "n.m." is two letters, not two sentences.
    text = re.sub(r"\b((?:[a-z]\.){2,})", lambda match: " ".join(match.group(1).split(".")), text)
    # "forty-two" and "one-oh-four" are words; "101-1" and "bob-1" are lot numbers.
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    return text


def _amount(text):
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def tokenize(text):
    """Tokens of ``text``, with offsets into the text as given (normalizing never changes its length)."""
    tokens = []
    for match in _TOKEN_RE.finditer(_normalize(text)):
        kind = match.lastgroup
        raw = match.group(0)
        token = Token(kind, raw, match.start(), match.end())
        if kind == "money":
            token.amount = _amount(raw[1:].strip())
            token.text = raw[1:].strip().replace(",", "")
        elif kind == "num":
            token.amount = _amount(raw)
            token.text = raw.replace(",", "")
        tokens.append(token)
    return tokens


def _is_number_word(tokens, index, grammar):
    token = tokens[index]
    if token.kind == "num":
        return True
    if token.kind != "word":
        return False
    word = token.text
    if word in ONES or word in TEENS or word in TENS or word in grammar.extra_numbers:
        return True
    if word in SCALES:
        return index > 0 and (_is_number_word(tokens, index - 1, grammar) or tokens[index - 1].text == "a")
    if word in ZEROS_INSIDE:
        return index > 0 and _is_number_word(tokens, index - 1, grammar)
    if word == "a":
        return index + 1 < len(tokens) and tokens[index + 1].text in SCALES
    if word == "and":
        # "one hundred and five"
        return (
            index > 0
            and tokens[index - 1].text in SCALES
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "word"
            and (tokens[index + 1].text in ONES or tokens[index + 1].text in TEENS or tokens[index + 1].text in TENS)
        )
    return False


@dataclass
class Group:
    """One number as English groups words: "twenty five" is one, "five six" is two."""

    value: int | Decimal
    digits: str
    start: int
    end: int  # exclusive, token index


def number_groups(tokens, start, end, grammar):
    """The numbers in ``tokens[start:end]``, a run of number words with nothing else between them."""
    groups = []
    index = start
    while index < end:
        token = tokens[index]
        first = index
        if token.kind == "num":
            value = token.amount if token.amount is not None else Decimal(0)
            index += 1
            if index < end and tokens[index].text in SCALES and value == value.to_integral_value():
                value = int(value) * SCALES[tokens[index].text]
                index += 1
                value, index = _below_hundred_tail(tokens, index, end, value)
                groups.append(Group(value, str(value), first, index))
                continue
            groups.append(Group(value, token.text, first, index))
            continue
        value, index = _cardinal(tokens, index, end, grammar)
        if index == first:
            # A word that only counts next to other numbers ("oh", "and") with nothing to attach to.
            index += 1
            continue
        groups.append(Group(value, str(value), first, index))
    return groups


def _below_hundred(tokens, index, end):
    """A number under 100 at ``index``: (value, next index), or (None, index)."""
    if index >= end or tokens[index].kind != "word":
        return None, index
    word = tokens[index].text
    if word in TENS:
        value = TENS[word]
        index += 1
        if index < end and tokens[index].kind == "word" and ONES.get(tokens[index].text, 0) > 0:
            value += ONES[tokens[index].text]
            index += 1
        return value, index
    if word in TEENS:
        return TEENS[word], index + 1
    if word in ONES:
        return ONES[word], index + 1
    return None, index


def _below_hundred_tail(tokens, index, end, value):
    """After "hundred": an optional "and", then the rest under a hundred."""
    look = index
    if look < end and tokens[look].text == "and":
        look += 1
    rest, after = _below_hundred(tokens, look, end)
    if rest is not None and rest > 0:
        return value + rest, after
    return value, index


def _cardinal(tokens, index, end, grammar):
    first = index
    word = tokens[index].text
    if word in ZEROS_INSIDE:
        return 0, index + 1
    if word in grammar.extra_numbers:
        return grammar.extra_numbers[word], index + 1
    if word == "a" and index + 1 < end and tokens[index + 1].text in SCALES:
        value, index = 1, index + 1
    else:
        value, index = _below_hundred(tokens, index, end)
        if value is None:
            return 0, first
    if index < end and tokens[index].text == "hundred" and 0 < value < 100:
        value, index = _below_hundred_tail(tokens, index + 1, end, value * 100)
    if index < end and tokens[index].text == "thousand" and 0 < value < 1000:
        value *= 1000
        index += 1
        rest, after = _below_hundred(tokens, index, end)
        if rest is not None and after < end and tokens[after].text == "hundred":
            rest, after = _below_hundred_tail(tokens, after + 1, end, rest * 100)
        if rest is not None and rest > 0:
            value, index = value + rest, after
    return value, index


def number_run_end(tokens, index, grammar):
    """Where the run of number words starting at ``index`` ends (exclusive)."""
    end = index
    while end < len(tokens) and _is_number_word(tokens, end, grammar):
        end += 1
    return end


# ---------------------------------------------------------------------------
# Reading identifiers
# ---------------------------------------------------------------------------


@dataclass
class Match:
    value: str
    start: int
    end: int
    confidence: float = SAID_OUTRIGHT
    candidates: list = field(default_factory=list)


def _letters(tokens, allow_single):
    """ "b o b", "bee oh bee", "bravo oscar bravo" -> "bob". None unless every token is a letter."""
    out = ""
    for token in tokens:
        if token.kind != "word":
            return None
        if len(token.text) == 1:
            out += token.text
        elif token.text in NATO:
            out += NATO[token.text]
        elif token.text in LETTER_NAMES:
            out += LETTER_NAMES[token.text]
        else:
            return None
    if len(out) < 2 and not allow_single:
        return None
    return out or None


def _numeric_readings(tokens, grammar):
    """Every way a run of number words might be an identifier: "one oh four" -> 104, and split at a dash
    nobody said for seller-dash lots: "one oh one one" -> 101-1.
    """
    local = list(tokens)
    groups = number_groups(local, 0, len(local), grammar)
    if not groups or any(group.end - group.start <= 0 for group in groups):
        return set()
    if any(isinstance(group.value, Decimal) and group.value != group.value.to_integral_value() for group in groups):
        return set()
    digits = [group.digits if isinstance(group.value, Decimal) else str(group.value) for group in groups]
    joined = "".join(digits)
    out = {joined}
    if len(groups) > 1:
        for split in range(1, len(digits)):
            out.add("".join(digits[:split]) + "-" + "".join(digits[split:]))
    return out


def readings(tokens, grammar, allow_single_letter=False):
    """Candidate identifiers for a span of tokens, lowercased."""
    if not tokens:
        return set()
    texts = [token.text for token in tokens]
    for index, text in enumerate(texts):
        if text in DASH_WORDS and 0 < index < len(texts) - 1:
            left = readings(tokens[:index], grammar, allow_single_letter=True)
            right = readings(tokens[index + 1 :], grammar, allow_single_letter=True)
            return {f"{a}-{b}" for a in left for b in right}
    if len(tokens) == 1 and tokens[0].kind == "id":
        return {tokens[0].text}
    if all(
        token.kind == "num" or (token.kind == "word" and _is_number_word(tokens, i, grammar))
        for i, token in enumerate(tokens)
    ):
        return _numeric_readings(tokens, grammar)
    out = set()
    if len(tokens) == 1 and tokens[0].kind == "word" and (len(tokens[0].text) > 1 or allow_single_letter):
        out.add(tokens[0].text)
    letters = _letters(tokens, allow_single_letter)
    if letters:
        out.add(letters)
    # "bob one" for BOB-1: a letter part, then a number part.
    for split in range(1, len(tokens)):
        head, tail = tokens[:split], tokens[split:]
        if all(
            token.kind == "num" or token.text in ONES or token.text in TEENS or token.text in TENS for token in tail
        ):
            head_readings = readings(head, grammar, allow_single_letter=True)
            tail_readings = _numeric_readings(tail, grammar)
            for a in head_readings:
                if not a or a[-1].isdigit():
                    continue
                for b in tail_readings:
                    out.add(f"{a}-{b}")
                    out.add(f"{a}{b}")
    return out


def _id_ish(token):
    if token.kind in ("num", "id"):
        return True
    if token.kind != "word":
        return False
    return (
        token.text in ONES
        or token.text in TEENS
        or token.text in TENS
        or token.text in SCALES
        or token.text in ZEROS_INSIDE
        or token.text in DASH_WORDS
        or token.text in NATO
        or token.text in LETTER_NAMES
        or token.text == "and"
        or len(token.text) <= 6
    )


def match_value(tokens, index, lookup, grammar, allow_single_letter=False, max_tokens=6):
    """The longest run at ``index`` that reads as one of ``lookup``'s values, or None.

    Two values for one run is an ambiguity, returned with both as candidates. Nothing for the run but
    something for its homophone ("fifteen" for "fifty") is returned unsure, with the homophone.
    """
    end = index
    while end < len(tokens) and end - index < max_tokens and _id_ish(tokens[end]):
        end += 1
    split = _split_number(tokens, index, end, lookup, grammar)
    if split:
        return split
    for stop in range(end, index, -1):
        span = tokens[index:stop]
        hits = []
        for reading in readings(span, grammar, allow_single_letter=allow_single_letter):
            value = lookup.get(reading)
            if value is not None and value not in hits:
                hits.append(value)
        if len(hits) == 1:
            return Match(hits[0], index, stop)
        if hits:
            return Match(hits[0], index, stop, SOUNDS_LIKE, hits)
    # Only for a plain number: a homophone of a word is a different word.
    stop = number_run_end(tokens, index, grammar)
    if stop > index:
        groups = number_groups(tokens, index, stop, grammar)
        if groups:
            group = groups[0]
            hits = [lookup[alt.lower()] for alt in grammar.alternatives(str(group.value)) if alt.lower() in lookup]
            if hits:
                return Match(hits[0], index, group.end, SOUNDS_LIKE, hits)
    return None


def number_to_words(value):
    """105 -> ["one", "hundred", "five"]: how a whole number under a million is said."""
    names = {number: word for word, number in {**ONES, **TEENS, **TENS}.items() if word != "fourty"}
    if value < 20:
        return [names[value]]
    if value < 100:
        return [names[value - value % 10]] + ([names[value % 10]] if value % 10 else [])
    if value < 1000:
        return [names[value // 100], "hundred"] + (number_to_words(value % 100) if value % 100 else [])
    if value < 1_000_000:
        return number_to_words(value // 1000) + ["thousand"] + (number_to_words(value % 1000) if value % 1000 else [])
    return []


def _spoken(token):
    """The words a number token is said with: "44" -> forty four; a number word is itself."""
    if token.kind == "num" and token.amount is not None and token.amount == token.amount.to_integral_value():
        return number_to_words(int(token.amount))
    if _number_word(token):
        return [token.text]
    return []


def _number_word(token):
    return token.kind == "num" or (
        token.kind == "word"
        and (token.text in ONES or token.text in TEENS or token.text in TENS or token.text in ZEROS_INSIDE)
    )


def _split_number(tokens, index, end, lookup, grammar):
    """A number a pause cut in two, or None.

    Only where the cut shows: a half ends or starts on "oh", which no whole number does ("one oh | four",
    "one | oh four"), or the first half is nothing on its own and nothing but a break follows it. Never
    when the second half is a price: "to seventeen. Four dollars" is bidder 17 for $4. A boundary word said
    twice is gone already (``Window._cut_off``).
    """
    if end <= index or end >= len(tokens) or not all(_number_word(token) for token in tokens[index:end]):
        return None
    after = end
    while after < len(tokens) and tokens[after].kind == "punct" and tokens[after].text != "?":
        after += 1
    if after == end or after >= len(tokens) or not _number_word(tokens[after]):
        return None
    stop = after
    while stop < len(tokens) and _number_word(tokens[stop]) and stop - after < 4:
        stop += 1
    if stop < len(tokens) and _anchor_at(tokens, stop, grammar.anchors["price"]):
        return None
    left = tokens[index:end]
    alone = any(lookup.get(reading) is not None for reading in readings(left, grammar))
    only_a_break = all(token.text == "|" for token in tokens[end:after])
    if not (left[-1].text in ZEROS_INSIDE or tokens[after].text in ZEROS_INSIDE or (only_a_break and not alone)):
        return None
    right = tokens[after:stop]
    for cut in range(len(right), 0, -1):
        hits = [lookup[reading] for reading in readings(left + right[:cut], grammar) if reading in lookup]
        if hits:
            return Match(hits[0], index, after + cut)
    return None


def _anchor_at(tokens, index, phrases):
    """The length of the anchor phrase at ``index``, or 0. Plurals count: "lots", "bidders", "dollar"."""
    for phrase in phrases:
        if index + len(phrase) > len(tokens):
            continue
        words = [tokens[index + offset].text for offset in range(len(phrase))]
        if any(tokens[index + offset].kind != "word" for offset in range(len(phrase))):
            continue
        if tuple(words) == phrase:
            return len(phrase)
        if len(phrase) == 1:
            word = words[0]
            if word.endswith("s") and not word.endswith("ss") and word[:-1] == phrase[0]:
                return 1
            if phrase[0].endswith("s") and word == phrase[0][:-1]:
                return 1
    return 0


_VOICING = {"t": "d", "d": "d", "s": "z", "z": "z", "p": "b", "b": "b", "k": "g", "g": "g", "f": "v", "v": "v"}


def _sounds_like(word, anchor):
    """One edit apart with voiced/voiceless pairs free: "bitter" and "better" for "bidder"."""
    if len(word) < 5 or len(anchor) < 5 or abs(len(word) - len(anchor)) > 1:
        return False
    a = "".join(_VOICING.get(c, c) for c in word)
    b = "".join(_VOICING.get(c, c) for c in anchor)
    if a == b:
        return True
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))


def _fuzzy_anchor(tokens, index, phrases):
    if tokens[index].kind != "word":
        return False
    return any(len(phrase) == 1 and _sounds_like(tokens[index].text, phrase[0]) for phrase in phrases)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


@dataclass
class Mention:
    """A lot or bidder said with its marker word: "lot forty two", "to one oh four", "paddle fifty"."""

    slot: str
    match: Match
    marker: int  # token index of the marker word


@dataclass
class Price:
    value: Decimal
    start: int
    end: int
    confidence: float
    how: str  # "said" (money, a price word, "for"/"at") or "bid" (from the bidding) or "bare"
    candidates: list = field(default_factory=list)


@dataclass
class Cue:
    slot: str  # "sold", "unsold", "undo"
    start: int
    end: int


class Window:
    """The tokens of everything heard, and what can be found in them."""

    def __init__(self, segments, vocabulary, grammar):
        self.segments = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
        self.text = SEGMENT_BREAK.join(self.segments)
        self.vocabulary = vocabulary
        self.grammar = grammar
        self.tokens = self._mend(tokenize(self.text))
        self.cues = self._cues()
        self.lots = self._lot_mentions()

    def _mend(self, tokens):
        """Drop the break between two segments where it falls inside a phrase.

        A recognizer ends a segment where it decides to, and that can be between "lot" and "forty
        three", or inside "one oh | four". Read as a pause, the first loses the lot announcement and the
        second turns bidder 104 into bidder 10.
        """
        mended = []
        skip_to = 0
        for index, token in enumerate(tokens):
            if index < skip_to:
                continue
            if token.text == "|":
                cut = self._cut_off(mended, tokens, index)
                if cut is not None:
                    del mended[cut:]
                    skip_to = index + 1
                    while skip_to < len(tokens) and tokens[skip_to].kind == "punct":
                        skip_to += 1
                    continue
            # Nobody ends a number on "oh": "one oh. | Four" is 104 with a full stop in the wrong place.
            if token.kind == "punct" and mended and mended[-1].text in ZEROS_INSIDE and len(mended) > 1:
                after = index
                while after < len(tokens) and tokens[after].kind == "punct":
                    after += 1
                if after < len(tokens) and _is_number_word(mended, len(mended) - 2, self.grammar):
                    if tokens[after].kind == "num" or tokens[after].text in ONES:
                        continue
            if token.text == "|" and mended and index + 1 < len(tokens) and tokens[index + 1].kind != "punct":
                # "Paddle. | Seventeen": the recognizer's full stop at its own segment end isn't the speaker's.
                while len(mended) > 1 and mended[-1].text in (".", ",") and mended[-2].kind == "word":
                    word = mended[-2]
                    if (
                        word.text in TO_WORDS | PRICE_CONNECTORS | BIDDER_MARKERS
                        or _anchor_at([word], 0, self.grammar.anchors["lot"])
                        or _anchor_at([word], 0, self.grammar.anchors["bidder"])
                    ):
                        mended.pop()
                    else:
                        break
                before = mended[-1]
                expects = before.kind == "word" and (
                    before.text in TO_WORDS
                    or before.text in PRICE_CONNECTORS
                    or before.text in BIDDER_MARKERS
                    or before.text in DASH_WORDS
                    or _anchor_at([before], 0, self.grammar.anchors["lot"])
                    or _anchor_at([before], 0, self.grammar.anchors["bidder"])
                )
                numbers = _is_number_word(mended, len(mended) - 1, self.grammar) and _number_word(tokens[index + 1])
                if expects or numbers:
                    continue
            mended.append(token)
        return mended

    @staticmethod
    def _cut_off(mended, tokens, index):
        """Where to cut ``mended`` back to when the break at ``index`` follows a number cut off mid-word
        ("four | forty-two"), or None.
        """
        back = len(mended) - 1
        while back >= 0 and mended[back].kind == "punct":
            back -= 1
        ahead = index + 1
        while ahead < len(tokens) and tokens[ahead].kind == "punct":
            ahead += 1
        if back < 0 or ahead >= len(tokens) or not _number_word(mended[back]):
            return None
        said = _spoken(tokens[ahead])
        if said and (said[0] == mended[back].text or said[0] in CUT_OFF.get(mended[back].text, ())):
            return back
        return None

    # -- finding things ------------------------------------------------------

    def _isolated(self, start, end):
        before = start == 0 or self.tokens[start - 1].kind == "punct"
        after = end >= len(self.tokens) or self.tokens[end].kind == "punct"
        return before and after

    def _cues(self):
        cues = []
        tokens = self.tokens
        index = 0
        while index < len(tokens):
            found = None
            for slot in ("unsold", "sold", "undo"):
                length = _anchor_at(tokens, index, self.grammar.anchors[slot])
                if length:
                    found = (slot, length)
                    break
            if not found:
                index += 1
                continue
            slot, length = found
            end = index + length
            before = tokens[index - 1].text if index else ""
            after = tokens[end].text if end < len(tokens) else ""
            keep = True
            if slot == "sold" and (before in NOT_A_CLOSE_BEFORE or after in NOT_A_CLOSE_AFTER):
                keep = False
            # "Pass" and "undo" are ordinary words in a sentence; only alone are they a command.
            if slot == "unsold" and length == 1 and tokens[index].text != "unsold" and not self._isolated(index, end):
                keep = False
            if slot == "undo" and not self._isolated(index, end):
                keep = False
            if keep:
                cues.append(Cue(slot, index, end))
            index = end
        return cues

    def _lot_mentions(self):
        mentions = []
        tokens = self.tokens
        for index in range(len(tokens)):
            length = _anchor_at(tokens, index, self.grammar.anchors["lot"])
            if not length:
                continue
            found = match_value(tokens, index + length, self.vocabulary.lot_lookup, self.grammar)
            if found:
                mentions.append(Mention("lot", found, index))
        return mentions

    def bidder_after_marker(self, index):
        """A bidder number after a marker at ``index`` ("to", "bidder", "number", ...): Mention or None."""
        tokens = self.tokens
        if index >= len(tokens) or tokens[index].kind != "word":
            return None
        word = tokens[index].text
        anchor = _anchor_at(tokens, index, self.grammar.anchors["bidder"])
        if word in TO_WORDS:
            after = index + 1
            # "to the lady in the back, number seventeen" is found by its own marker.
            next_anchor = _anchor_at(tokens, after, self.grammar.anchors["bidder"]) if after < len(tokens) else 0
            if next_anchor:
                after += next_anchor
            elif after < len(tokens) and tokens[after].text in BIDDER_MARKERS:
                after += 1
            found = match_value(
                tokens, after, self.vocabulary.bidder_lookup, self.grammar, allow_single_letter=bool(next_anchor)
            )
            return Mention("bidder", found, index) if found else None
        if anchor or word in BIDDER_MARKERS:
            after = index + (anchor or 1)
            found = match_value(tokens, after, self.vocabulary.bidder_lookup, self.grammar, allow_single_letter=True)
            return Mention("bidder", found, index) if found else None
        if _fuzzy_anchor(tokens, index, self.grammar.anchors["bidder"]):
            found = match_value(tokens, index + 1, self.vocabulary.bidder_lookup, self.grammar)
            if found:
                found.confidence = min(found.confidence, SOUNDS_LIKE)
                return Mention("bidder", found, index)
        return None

    def bidders(self, start, end):
        """Bidder mentions with a marker word whose number starts in ``[start, end)``."""
        out = []
        index = start
        while index < end:
            mention = self.bidder_after_marker(index)
            if mention and mention.match.start < end:
                out.append(mention)
                index = mention.match.end
                continue
            index += 1
        return out

    def _price_word_after(self, index):
        """The length of the "dollars"/"bucks" at ``index``, or 0."""
        if index >= len(self.tokens):
            return 0
        return _anchor_at(self.tokens, index, self.grammar.anchors["price"])

    def prices(self, start, end, skip=()):
        """Every amount in ``[start, end)``, how it was said, and whether it was asked for or had.

        ``skip`` are token ranges already read as a lot or bidder number.
        """
        tokens = self.tokens
        found = []
        index = start
        skipped = set()
        for first, last in skip:
            skipped.update(range(first, last))
        while index < end:
            if index in skipped:
                index += 1
                continue
            token = tokens[index]
            if token.kind == "money":
                stop = index + 1 + self._price_word_after(index + 1)
                amount, stop = self._cents(token.amount, stop, end)
                found.append(Price(amount, index, stop, SAID_OUTRIGHT, "said"))
                index = stop
                continue
            if not _is_number_word(tokens, index, self.grammar) or tokens[index].text in ZEROS_INSIDE:
                index += 1
                continue
            run_end = number_run_end(tokens, index, self.grammar)
            for group in number_groups(tokens, index, run_end, self.grammar):
                if any(position in skipped for position in range(group.start, group.end)):
                    continue
                if self._pronoun(group):
                    continue
                word = self._price_word_after(group.end)
                connector = group.start > 0 and tokens[group.start - 1].text in PRICE_CONNECTORS
                stop = group.end + word
                amount = Decimal(group.value)
                if word:
                    amount, stop = self._cents(amount, stop, end)
                how = "said" if (word or connector) else "bare"
                found.append(Price(amount, group.start, stop, SAID_OUTRIGHT if how == "said" else GUESSED, how))
            index = max(run_end, index + 1)
        return found

    def _cents(self, amount, index, end):
        """ "two dollars and fifty cents", "two dollars fifty": cents after the price word."""
        tokens = self.tokens
        look = index
        said_and = look < end and tokens[look].text == "and"
        if said_and:
            look += 1
        if look < end and _is_number_word(tokens, look, self.grammar):
            run_end = number_run_end(tokens, look, self.grammar)
            groups = number_groups(tokens, look, run_end, self.grammar)
            if groups and isinstance(groups[0].value, int) and 0 < groups[0].value < 100:
                cents = groups[0].value
                after = groups[0].end
                said_cents = after < end and tokens[after].text in ("cents", "cent")
                if said_cents:
                    after += 1
                # "Four dollars, seventeen" is four dollars to bidder seventeen.
                if said_cents or said_and or (not self.vocabulary.whole_dollars and cents in SPOKEN_CENTS):
                    return amount + Decimal(cents) / 100, after
        return amount, index

    def _pronoun(self, group):
        """ "This one", "the big one": not a bid of a dollar."""
        tokens = self.tokens
        if group.end - group.start != 1 or tokens[group.start].text != "one":
            return False
        return group.start > 0 and tokens[group.start - 1].text in ONE_IS_A_PRONOUN_AFTER

    def asked(self, price):
        """Whether the auctioneer was asking for this bid rather than had it: "do I hear ten?"."""
        tokens = self.tokens
        for back in range(price.start - 1, max(-1, price.start - 4), -1):
            # "Do I hear ten? Ten dollars": the answer isn't part of the question.
            if tokens[back].text in ("?", ".", "!", "|"):
                break
            if tokens[back].text in ASK_BEFORE:
                return True
        after = price.end
        if after < len(tokens) and (tokens[after].text == "?" or tokens[after].text in ASK_AFTER):
            return True
        return False

    def words(self, start, end):
        return sum(1 for token in self.tokens[start:end] if token.kind != "punct")

    def text_of(self, start, end):
        if start >= end or start >= len(self.tokens):
            return ""
        end = min(end, len(self.tokens))
        return self.text[self.tokens[start].start : self.tokens[end - 1].end].replace(SEGMENT_BREAK.strip(), "").strip()

    def rest_after(self, end):
        """The window from token ``end`` on, split back into segments."""
        while end < len(self.tokens) and self.tokens[end].kind == "punct":
            end += 1
        if end >= len(self.tokens):
            return []
        rest = self.text[self.tokens[end].start :]
        return [part.strip() for part in rest.split(SEGMENT_BREAK.strip()) if part.strip(" .,!?;:")]

    def a_new_round(self, start, end):
        """Whether ``[start, end)`` is another lot's bidding or description rather than more of one close."""
        if any(start <= mention.marker < end for mention in self.lots):
            return True
        if self.words(start, end) >= 14:
            return True
        return len(self.prices(start, end)) >= 3


# ---------------------------------------------------------------------------
# Reading the close
# ---------------------------------------------------------------------------


@dataclass
class Close:
    cue: Cue
    lot: str
    lot_heard: str = ""
    bidder: Match | None = None
    bidder_heard: str = ""
    price: Price | None = None
    end: int = 0
    ignored_bidder: str = ""

    @property
    def complete(self):
        return bool(self.lot and self.bidder and self.price)


def _format_price(value, whole_dollars):
    value = Decimal(value)
    if value <= 0 or value > Decimal("999999.99"):
        return None
    if value == value.to_integral_value():
        return str(int(value))
    if whole_dollars:
        return None
    return f"{value.quantize(Decimal('0.01'))}"


def _read_close(window, cue, region_start, region_end, lot):
    """Bidder and price for the close at ``cue``, looking no further than ``[region_start, region_end)``."""
    tokens = window.tokens
    close = Close(cue=cue, lot=lot, end=cue.end)
    after_end = _close_reach(window, cue, region_end)
    before_start = max(region_start, cue.start - CLOSE_REACH_BEFORE)

    # Bidder: said with a marker after the cue, or right after the cue with no marker ("Sold! 104."), or
    # with a marker just before it ("ten to 104, sold"). The first one: a later one is the next lot's,
    # unless a correction says otherwise (_corrections).
    after = window.bidders(cue.end, after_end)
    used = []
    if after:
        mention = after[0]
        close.bidder = mention.match
        used.append((mention.match.start, mention.match.end))
        close.end = max(close.end, mention.match.end)
    else:
        bare = _bare_bidder_after(window, cue, after_end)
        if bare:
            close.bidder = bare
            used.append((bare.start, bare.end))
            close.end = max(close.end, bare.end)
        else:
            before = window.bidders(before_start, cue.start)
            if before:
                mention = before[-1]
                close.bidder = mention.match
                used.append((mention.match.start, mention.match.end))
    if close.bidder is None:
        close.ignored_bidder = _unknown_bidder(window, cue.end, after_end)

    # Price: said in the close ("for ten", "$10", "ten dollars"), else the last bid the auctioneer had.
    lot_ranges = [(mention.match.start, mention.match.end) for mention in window.lots]
    # "I have ten from bidder one oh four": 104 is who, not how much.
    bidder_ranges = [(mention.match.start, mention.match.end) for mention in window.bidders(region_start, region_end)]
    skip = used + lot_ranges + bidder_ranges
    said_after = [price for price in window.prices(cue.end, after_end, skip) if price.how == "said"]
    bidding = window.prices(region_start, cue.start, skip)
    had = [price for price in bidding if not window.asked(price)]
    if said_after:
        close.price = said_after[0]
        close.end = max(close.end, close.price.end)
        if had:
            chant = had[-1].value
            if chant != close.price.value and str(chant) in [
                str(Decimal(alt)) for alt in window.grammar.alternatives(str(close.price.value))
            ]:
                # "Sold for fifty" after bidding that stopped at fifteen: one of them is misheard.
                close.price = Price(close.price.value, close.price.start, close.price.end, SOUNDS_LIKE, "said", [chant])
    elif had:
        last = had[-1]
        close.price = Price(last.value, last.start, last.end, FROM_THE_BIDDING, "bid")
        later_asks = [
            price
            for price in bidding
            if price.start > last.start
            and price.value > last.value
            and not any(token.text in CLOSING_WORDS for token in tokens[price.end : cue.start])
        ]
        earlier = [price.value for price in had[:-1] if price.value != last.value]
        if later_asks:
            # "Ten, twelve anyone? Sold": somebody may have nodded at twelve.
            close.price.confidence = GUESSED
            close.price.candidates = [later_asks[-1].value]
        elif earlier and str(earlier[-1]) in window.grammar.alternatives(str(last.value)):
            # "Ten, fifteen, fifty": fifteen said again, heard as fifty.
            close.price.confidence = SOUNDS_LIKE
            close.price.candidates = [earlier[-1]]
        elif last.how == "said":
            close.price.confidence = SAID_OUTRIGHT if cue.start - last.end <= 6 else FROM_THE_BIDDING
    else:
        # "Sold, 104, 10": what's left after the bidder.
        bare = [price for price in window.prices(cue.end, after_end, skip) if price.how == "bare"]
        if bare:
            close.price = Price(bare[0].value, bare[0].start, bare[0].end, GUESSED, "bare")
            close.end = max(close.end, bare[0].end)
    _corrections(window, close, after_end)
    # Trailing punctuation that belongs to the close.
    while close.end < len(tokens) and tokens[close.end].kind == "punct" and tokens[close.end].text != "|":
        close.end += 1
    return close


def _corrections(window, close, after_end):
    """ "To one oh four, sorry, one oh five": the number after the correction replaces whichever of the
    bidder or the price was said just before it.
    """
    tokens = window.tokens
    for index in range(close.cue.end, min(after_end, len(tokens))):
        if tokens[index].text not in CORRECTION_WORDS:
            continue
        after = index + 1
        while after < len(tokens) and tokens[after].kind == "punct":
            after += 1
        bidder_end = close.bidder.end if close.bidder and close.bidder.end <= index else -1
        price_end = close.price.end if close.price and close.price.end <= index else -1
        if bidder_end < 0 and price_end < 0:
            continue
        if bidder_end > price_end:
            found = match_value(tokens, after, window.vocabulary.bidder_lookup, window.grammar)
            if found:
                close.bidder = found
                close.end = max(close.end, found.end)
        else:
            run_end = number_run_end(tokens, after, window.grammar)
            groups = number_groups(tokens, after, run_end, window.grammar) if run_end > after else []
            if groups:
                stop = groups[0].end + window._price_word_after(groups[0].end)
                close.price = Price(Decimal(groups[0].value), after, stop, SAID_OUTRIGHT, "said")
                close.end = max(close.end, stop)


def _close_reach(window, cue, region_end):
    """Where the close's details stop: a few words on, or sooner at the next lot's first ask ("who'll
    give two?") or announcement.
    """
    tokens = window.tokens
    end = min(region_end, cue.end + CLOSE_REACH_AFTER, len(tokens))
    for index in range(cue.end, end):
        text = tokens[index].text
        if text == "?" or text in ASK_BEFORE or text in ASK_AFTER:
            return index
        if _anchor_at(tokens, index, window.grammar.anchors["lot"]) and match_value(
            tokens, index + 1, window.vocabulary.lot_lookup, window.grammar
        ):
            return index
    return end


def _bare_bidder_after(window, cue, after_end):
    """ "Sold! One oh four." -- a bidder number straight after the cue, or straight after its price."""
    tokens = window.tokens
    index = cue.end
    while index < after_end and tokens[index].kind == "punct":
        index += 1
    for _ in range(2):
        if index >= after_end:
            return None
        # Skip a price said first: "sold, ten dollars, one oh four".
        prices = [price for price in window.prices(index, after_end) if price.start == index and price.how == "said"]
        if prices:
            index = prices[0].end
            while index < after_end and tokens[index].kind == "punct":
                index += 1
            continue
        found = match_value(tokens, index, window.vocabulary.bidder_lookup, window.grammar)
        if not found:
            return None
        after = found.end
        if after < len(tokens) and _anchor_at(tokens, after, window.grammar.anchors["price"]):
            # "Sold, ten dollars": a price, not bidder ten.
            return None
        if after < len(tokens) and tokens[after].text in TO_WORDS:
            # "Sold ten to 104": ten is the price.
            return None
        found.confidence = AFTER_THE_CLOSE
        return found
    return None


def _unknown_bidder(window, start, end):
    """A number said as a bidder that this auction doesn't have, for the note."""
    tokens = window.tokens
    for index in range(start, min(end, len(tokens))):
        word = tokens[index].text
        if word in TO_WORDS or word in BIDDER_MARKERS or _anchor_at(tokens, index, window.grammar.anchors["bidder"]):
            after = index + 1
            if after < len(tokens) and tokens[after].text in BIDDER_MARKERS:
                after += 1
            stop = number_run_end(tokens, after, window.grammar)
            if stop > after:
                groups = number_groups(tokens, after, stop, window.grammar)
                if groups and not (after < len(tokens) and _anchor_at(tokens, stop, window.grammar.anchors["price"])):
                    return "".join(str(group.value) for group in groups)
    return ""


def _lot_before(window, start, end, lot):
    """The lot this close is for: the last one announced in ``[start, end)``, else ``lot``."""
    announced = [mention for mention in window.lots if start <= mention.marker < end]
    if announced:
        mention = announced[-1]
        return mention.match.value, window.text_of(mention.marker, mention.match.end)
    return lot, ""


def interpret(segments, vocabulary, grammar, *, lot="", previous=None, next_lot=None):
    """Read the window and say what to do with it. Returns a :class:`Reading`.

    ``lot`` is what the form's lot field holds now. ``previous`` is ``{"lot", "winner", "price"}`` of a
    sale recorded moments ago, when this window is what followed it. ``next_lot(lot)`` gives the lot
    after ``lot`` in the lot queue (or None), for when a close goes missing without a new lot being
    announced.
    """
    window = Window(segments, vocabulary, grammar)
    reading = Reading()
    form_lot = str(lot or "")
    current_lot = form_lot
    start = _after_the_sale(window, reading, previous) if previous else 0
    cues = list(window.cues)
    position = 0
    while position < len(cues):
        cue = cues[position]
        if cue.start < start:
            position += 1
            continue
        start = _unclosed(window, reading, start, cue.start, current_lot)
        lot_here, lot_heard = _lot_before(window, start, cue.start, current_lot)

        if cue.slot == "undo":
            if previous and start == 0 and window.words(0, cue.start) == 0:
                reading.commands.append(Command("undo", heard=window.text_of(cue.start, cue.end)))
                reading.carry = window.rest_after(cue.end)
                return reading
            position += 1
            continue

        if cue.slot == "unsold":
            if not lot_here:
                reading.note = "Heard no sale, but not which lot"
                return _keep_from(reading, window, start)
            _set_lot(reading, lot_here, form_lot, lot_heard)
            reading.commands.append(Command("unsold", heard=window.text_of(cue.start, cue.end)))
            reading.carry = window.rest_after(cue.end)
            return reading

        # What ends this close: a lot announced after it, or a sale cue after a new round of bidding.
        stop = len(window.tokens)
        stop_reason = ""
        for mention in window.lots:
            if mention.marker >= cue.end:
                stop, stop_reason = mention.marker, "lot"
                break
        later = [other for other in cues[position + 1 :] if other.start < stop]
        for other in later:
            if window.a_new_round(cue.end, other.start):
                stop, stop_reason = other.start, "cue"
                break
        # A lot's bidding starts when it's announced.
        region = max([start] + [mention.marker for mention in window.lots if start <= mention.marker < cue.start])
        close = _read_close(window, cue, region, stop, lot_here)
        close.lot_heard = lot_heard
        # More of the same close before the stop ("Sold! ... sold to 104"): read it whole.
        for other in later:
            if other.start >= stop:
                break
            more = _read_close(window, other, region, stop, lot_here)
            close.bidder = close.bidder or more.bidder
            close.price = close.price or more.price
            close.end = max(close.end, more.end)

        if previous and start == 0 and not window.a_new_round(0, cue.start) and lot_here == current_lot:
            # The sale just recorded, said again or corrected before the next lot started. Again is
            # nothing; corrected is for a person, since it's already saved.
            same = (
                close.bidder
                and close.price
                and close.bidder.value.lower() == str(previous.get("winner", "")).lower()
                and _same_amount(close.price.value, previous.get("price"))
            )
            if not same and (close.bidder or close.price):
                reading.missed.append(
                    {"lot": str(previous.get("lot", "")), "heard": window.text_of(cue.start, close.end)}
                )
            start = close.end
            position += 1
            continue

        price = _format_price(close.price.value, vocabulary.whole_dollars) if close.price else None
        if close.complete and price:
            _set_lot(reading, close.lot, form_lot, close.lot_heard)
            reading.commands += _value_commands(close, window)
            reading.commands.append(Command("sold", heard=window.text_of(cue.start, close.end)))
            reading.carry = window.rest_after(close.end)
            if close.price.how == "bid" and not reading.carry:
                # "Sold. Bidder fifty." -- and "fifteen dollars" may be on its way.
                reading.wait = PRICE_MAY_FOLLOW_MS
            return reading

        if stop_reason:
            # Something else started before this close was finished: report it, and read on with the
            # lot that comes next.
            if close.lot or close.bidder or close.price:
                reading.missed.append({"lot": close.lot, "heard": window.text_of(cue.start, close.end)})
            if stop_reason == "lot":
                current_lot = ""
                start = stop
            else:
                following = next_lot(close.lot) if (next_lot and close.lot) else None
                current_lot = str(following or "")
                start = close.end
            position += 1
            continue

        # Waiting for the rest of this close.
        _set_lot(reading, close.lot, form_lot, close.lot_heard)
        reading.commands += _value_commands(close, window)
        reading.commands.append(Command("sold", heard=window.text_of(cue.start, close.end)))
        missing = [
            name for name, have in (("lot number", close.lot), ("bidder", close.bidder), ("price", price)) if not have
        ]
        if close.ignored_bidder and not close.bidder:
            reading.note = f"No bidder {close.ignored_bidder} in this auction"
        elif close.price and not price:
            reading.note = "This auction only takes whole dollars"
        elif missing:
            reading.note = "Waiting for the " + " and ".join(missing)
        return _keep_from(reading, window, start)

    # No close (yet): the lot, if one was announced.
    start = _unclosed(window, reading, start, len(window.tokens), current_lot)
    lot_here, lot_heard = _lot_before(window, start, len(window.tokens), current_lot)
    _set_lot(reading, lot_here, form_lot, lot_heard)
    return _keep_from(reading, window, start)


def _after_the_sale(window, reading, previous):
    """The start of the window once whatever was said about the sale just saved is set aside.

    A price or bidder said again straight after a close ("Bidder fifty. | Fifteen dollars.") is that
    sale's, not the next lot's bidding. When it's the other half of a homophone pair from what was saved
    -- fifteen for fifty -- or comes after "sorry", the saved sale may be wrong, and a person is told.
    """
    tokens = window.tokens
    end = min(len(tokens), 12)
    for cue in window.cues:
        end = min(end, cue.start)
    for mention in window.lots:
        end = min(end, mention.marker)
    if end <= 0:
        return 0
    said = [price for price in window.prices(0, end) if price.how == "said" and not window.asked(price)]
    bidders = window.bidders(0, end)
    if not said and not bidders:
        return 0
    saved_price = str(previous.get("price", ""))
    saved_bidder = str(previous.get("winner", "")).lower()
    doubts = [price for price in said if str(price.value) in window.grammar.alternatives(saved_price)]
    doubts += [
        mention for mention in bidders if mention.match.value.lower() in window.grammar.alternatives(saved_bidder)
    ]
    corrected = any(token.text in CORRECTION_WORDS for token in tokens[:end]) and (
        any(not _same_amount(price.value, saved_price) for price in said)
        or any(mention.match.value.lower() != saved_bidder for mention in bidders)
    )
    if doubts or corrected:
        reading.missed.append({"lot": str(previous.get("lot", "")), "heard": window.text_of(0, end)})
    last = max([price.end for price in said] + [mention.match.end for mention in bidders])
    return last


def _unclosed(window, reading, start, end, current_lot):
    """Where reading should go on from, once a lot that changed hands without a close is reported.

    "Bidder fifty. Fifteen dollars. Lot forty-four" with no "sold" (it came back "soul") is lot 43 sold
    to somebody nobody recorded. A new lot announced after a bidder was named says so.
    """
    announced = [
        mention for mention in window.lots if start <= mention.marker < end and mention.match.value != current_lot
    ]
    if not announced or not current_lot:
        return start
    first = announced[0]
    named = window.bidders(start, first.marker)
    if named:
        reading.missed.append({"lot": current_lot, "heard": window.text_of(named[-1].marker, first.marker)})
        return first.marker
    return start


def _set_lot(reading, value, form_lot, heard):
    """Put ``value`` in the lot field, unless it's there already. Blank empties it: after a missed sale
    with no way to tell which lot is next, a lot must be heard before anything else is sold.
    """
    if str(value or "") != form_lot:
        reading.commands.append(Command("lot", str(value or ""), heard=heard))


def _keep_from(reading, window, start):
    """Drop what has been dealt with (a repeated close, a reported miss) from the page's window."""
    if start > 0:
        reading.keep = window.rest_after(start)
    return reading


def _same_amount(value, other):
    try:
        return Decimal(str(value)) == Decimal(str(other))
    except (InvalidOperation, ValueError, TypeError):
        return False


def _value_commands(close, window):
    commands = []
    if close.bidder:
        commands.append(
            Command(
                "bidder",
                close.bidder.value,
                close.bidder.confidence,
                window.text_of(close.bidder.start, close.bidder.end),
                [value for value in close.bidder.candidates if value != close.bidder.value],
            )
        )
    if close.price:
        value = _format_price(close.price.value, window.vocabulary.whole_dollars)
        candidates = [
            formatted
            for formatted in (_format_price(other, window.vocabulary.whole_dollars) for other in close.price.candidates)
            if formatted and formatted != value
        ]
        if value:
            commands.append(
                Command(
                    "price",
                    value,
                    close.price.confidence,
                    window.text_of(close.price.start, close.price.end),
                    candidates,
                )
            )
    return commands
