"""Validation + seed data for :class:`ThermalPrinterProfile` command programs.

A *program* is a small, declarative JSON list of steps the mobile app executes in order to
drive a Bluetooth thermal label printer. Every byte a printer receives is defined in these
programs (stored in the DB, editable in Django admin) — the app is a generic interpreter, so
adding a printer is a data change, not an app release.

This module owns the schema. It is imported by:

* ``ThermalPrinterProfile.clean()`` — reject an admin typo before it can brick a print,
* the profiles mobile API — serialise a profile for the app,
* the seed data migration — port the hardcoded D11s driver verbatim,
* tests — assert the checked-in seed data is valid.

Schema v1 step types::

    {"tx": "10 ff fe 01"}                    # hex bytes (whitespace ignored)
    {"tx_text": "SIZE {width_mm} mm\\r\\n"}   # ASCII with placeholders (TSPL/ZPL/ESC-POS)
    {"tx_raster": true}                       # the packed 1-bit bitmap body
    {"delay_ms": 50}
    {"await": {"any_hex_prefix": ["AA"], "timeout_ms": 60000, "on_timeout": "warn"}}
    {"repeat_per_copy": [ ...steps... ]}      # run nested steps once per requested copy

Schema v2 adds (all additive — every v1 row keeps working, and v1 stays the right
``schema_version`` for a profile that needs none of it)::

    {"tx_text": "^GFA,{total_bytes},…"}       # width_bytes * height_px, incl. {u32le:total_bytes}
    {"tx_raster": {"encoding": "hex"}}        # ASCII-hex bitmap body (ZPL ^GFA, CPCL EG)
    "status_flags": {"values": {"07": ["no_ribbon", "cover_open"]}}   # exact codes, not bitmasks

Validation is deliberately version-agnostic: a v2 construct in a row declaring
``schema_version: 1`` is a mistake the *app* catches (it only runs schemas it was built
with), and rejecting it here would make the admin unable to author the v2 row at all.
"""

import re

# The newest schema this deployment can describe. Reported to the app as ``schema_version_max``;
# the app runs a profile only when its ``schema_version`` is one it understands.
PROGRAM_SCHEMA_VERSION = 2

# Placeholders usable inside {tx}/{tx_text}. Scalar forms render as one byte (tx) / ASCII decimal
# (tx_text); the u16le/u32le forms render as little-endian 16/32-bit values (2/4 bytes).
SCALAR_PLACEHOLDERS = frozenset(
    {
        "width_px",
        "height_px",
        "width_bytes",
        # v2: width_bytes * height_px, i.e. the size of the raster body. ZPL's ^GF wants it twice,
        # and the schema has no arithmetic, so no v1 profile could express it.
        "total_bytes",
        "width_mm",
        "height_mm",
        "density",
        "paper_type",
        "copies",
    }
)
U16LE_PLACEHOLDERS = frozenset({"width_bytes", "height_px", "width_px"})
# 16 bits overflows on a real raster (a 4x6" label at 203dpi is ~270kB), so every size-ish scalar
# also has a 32-bit form.
U32LE_PLACEHOLDERS = frozenset({"total_bytes", "width_bytes", "height_px", "width_px"})
_WIDTH_FUNCTIONS = {"u16le": U16LE_PLACEHOLDERS, "u32le": U32LE_PLACEHOLDERS}

# A bare {name} inside a `tx` hex template renders as exactly one byte, so only genuinely
# byte-sized values may appear there. The size scalars are rejected unconditionally rather than
# "when the value happens to exceed 255": a profile authored against a small test label would
# otherwise validate and then silently truncate a length field on the first 4x6, printing half a
# label for a reason nobody can see. Use {u16le:…} / {u32le:…} instead.
BARE_BYTE_PLACEHOLDERS = frozenset({"density", "paper_type", "copies"})

# Every recognised step key. A step is a dict carrying exactly one of these.
STEP_KEYS = frozenset({"tx", "tx_text", "tx_raster", "delay_ms", "await", "repeat_per_copy"})
_ON_TIMEOUT = frozenset({"warn", "fail"})
_AWAIT_KEYS = frozenset({"any_hex_prefix", "timeout_ms", "on_timeout"})
_SIZE_PARSE_KINDS = frozenset({"ascii_regex", "bytes"})
# v2 raster encodings. "binary" is the v1 behaviour (raw packed bytes); "hex" doubles the bytes on
# the wire, so it stays opt-in per profile.
_RASTER_ENCODINGS = frozenset({"binary", "hex"})

# Conditions the app has a user-facing message for. Used by status_flags.flags (bitmask) and
# status_flags.values (exact code). Unknown names are rejected here so a typo doesn't become a
# printer state nobody is ever told about.
STATUS_CONDITIONS = frozenset(
    {
        "cover_open",
        "out_of_paper",
        "paper_jam",
        "no_ribbon",
        "overheated",
        "low_battery",
        "printing",
        "paused",
        "error",
    }
)

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]*\Z")


# What a profile's print program actually speaks. Declared on the row rather than inferred from
# its bytes, so the app can auto-select a profile when a command-language probe identifies a
# language and exactly one profile speaks it — knowing a printer speaks TSPL doesn't tell you its
# printhead width or GATT ids, so one candidate means there is nothing to get wrong, and more than
# one is a genuine question worth putting to the user.
COMMAND_LANGUAGE_CHOICES = [
    ("tspl", "TSPL / TSPL2 (TSC-compatible)"),
    ("escpos", "ESC/POS"),
    ("zpl", "ZPL"),
    ("cpcl", "CPCL"),
    ("d11s", "D11s vendor protocol"),
    ("other", "Other / vendor-specific"),
]


class ProgramValidationError(ValueError):
    """A printer command program failed schema validation.

    ``field`` names the offending JSONField (``print_program`` …) so a ModelForm can attach the
    error to the right widget in the admin.
    """

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def _check_placeholders(text, field, *, bare_must_be_byte=False):
    """Validate every ``{placeholder}`` in *text*.

    ``bare_must_be_byte`` is set for ``tx`` hex templates, where a bare ``{name}`` renders as a
    single byte — see :data:`BARE_BYTE_PLACEHOLDERS`.
    """
    for token in _PLACEHOLDER_RE.findall(text):
        if ":" in token:
            fn, _, name = token.partition(":")
            allowed = _WIDTH_FUNCTIONS.get(fn)
            if allowed is None or name not in allowed:
                msg = f"Unknown placeholder {{{token}}}"
                raise ProgramValidationError(msg, field)
        elif token not in SCALAR_PLACEHOLDERS:
            msg = f"Unknown placeholder {{{token}}}"
            raise ProgramValidationError(msg, field)
        elif bare_must_be_byte and token not in BARE_BYTE_PLACEHOLDERS:
            msg = (
                f"{{{token}}} does not fit in one byte, and a bare placeholder in a hex tx step "
                f"renders as exactly one byte. Use {{u16le:{token}}} or {{u32le:{token}}} instead."
            )
            raise ProgramValidationError(msg, field)


def _check_hex_literal(text, field, *, allow_placeholders=True):
    """Validate a hex byte string. Whitespace is ignored; placeholders stand in for whole bytes.

    Each literal run between placeholders must be an even number of hex digits (whole bytes).
    """
    if allow_placeholders:
        _check_placeholders(text, field, bare_must_be_byte=True)
        literals = _PLACEHOLDER_RE.split(text)[::2]  # drop the captured placeholder bodies
    else:
        if "{" in text or "}" in text:
            msg = f"Placeholders are not allowed here: {text!r}"
            raise ProgramValidationError(msg, field)
        literals = [text]
    for literal in literals:
        compact = re.sub(r"\s+", "", literal)
        if not _HEX_RE.match(compact):
            msg = f"Invalid hex bytes: {literal!r}"
            raise ProgramValidationError(msg, field)
        if len(compact) % 2 != 0:
            msg = f"Hex must be whole bytes (even number of digits): {literal!r}"
            raise ProgramValidationError(msg, field)


def _validate_step(step, field, *, in_repeat=False):
    if not isinstance(step, dict):
        msg = f"Each step must be an object, got {type(step).__name__}"
        raise ProgramValidationError(msg, field)
    keys = set(step)
    unknown = keys - STEP_KEYS
    if unknown:
        msg = f"Unknown step key(s): {', '.join(sorted(unknown))}"
        raise ProgramValidationError(msg, field)
    step_keys = keys & STEP_KEYS
    if len(step_keys) != 1:
        msg = f"Each step must have exactly one action, got {sorted(step_keys) or 'none'}"
        raise ProgramValidationError(msg, field)
    (key,) = step_keys
    value = step[key]

    if key == "tx":
        if not isinstance(value, str):
            msg = "tx must be a string of hex bytes"
            raise ProgramValidationError(msg, field)
        _check_hex_literal(value, field)
    elif key == "tx_text":
        if not isinstance(value, str):
            msg = "tx_text must be a string"
            raise ProgramValidationError(msg, field)
        _check_placeholders(value, field)
    elif key == "tx_raster":
        _validate_tx_raster(value, field)
    elif key == "delay_ms":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            msg = "delay_ms must be a non-negative integer"
            raise ProgramValidationError(msg, field)
    elif key == "await":
        _validate_await(value, field)
    elif key == "repeat_per_copy":
        if in_repeat:
            msg = "repeat_per_copy cannot be nested"
            raise ProgramValidationError(msg, field)
        if not isinstance(value, list):
            msg = "repeat_per_copy must be a list of steps"
            raise ProgramValidationError(msg, field)
        for nested in value:
            _validate_step(nested, field, in_repeat=True)


def _validate_tx_raster(value, field):
    """``true`` (v1, raw bytes) or ``{"encoding": "binary"|"hex"}`` (v2).

    ``false`` is rejected: a step that does nothing is a typo, not an instruction to omit the
    label body — and omitting it prints a blank label with no error anywhere.
    """
    if value is True:
        return
    if not isinstance(value, dict):
        msg = 'tx_raster must be true or an object like {"encoding": "hex"}'
        raise ProgramValidationError(msg, field)
    unknown = set(value) - {"encoding"}
    if unknown:
        msg = f"Unknown tx_raster key(s): {', '.join(sorted(unknown))}"
        raise ProgramValidationError(msg, field)
    encoding = value.get("encoding", "binary")
    if encoding not in _RASTER_ENCODINGS:
        msg = f"tx_raster.encoding must be one of {sorted(_RASTER_ENCODINGS)}"
        raise ProgramValidationError(msg, field)


def _validate_await(value, field):
    if not isinstance(value, dict):
        msg = "await must be an object"
        raise ProgramValidationError(msg, field)
    unknown = set(value) - _AWAIT_KEYS
    if unknown:
        msg = f"Unknown await key(s): {', '.join(sorted(unknown))}"
        raise ProgramValidationError(msg, field)
    prefixes = value.get("any_hex_prefix", [])
    if not isinstance(prefixes, list):
        msg = "await.any_hex_prefix must be a list of hex strings"
        raise ProgramValidationError(msg, field)
    for prefix in prefixes:
        if not isinstance(prefix, str):
            msg = "await.any_hex_prefix entries must be hex strings"
            raise ProgramValidationError(msg, field)
        _check_hex_literal(prefix, field, allow_placeholders=False)
    timeout = value.get("timeout_ms")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0):
        msg = "await.timeout_ms must be a non-negative integer"
        raise ProgramValidationError(msg, field)
    on_timeout = value.get("on_timeout", "warn")
    if on_timeout not in _ON_TIMEOUT:
        msg = f"await.on_timeout must be one of {sorted(_ON_TIMEOUT)}"
        raise ProgramValidationError(msg, field)


def validate_program(program, field="print_program", *, required=False):
    """Validate one program (list of steps). Raise :class:`ProgramValidationError` on any problem."""
    if program in (None, ""):
        if required:
            msg = "A print program is required"
            raise ProgramValidationError(msg, field)
        return
    if not isinstance(program, list):
        msg = f"{field} must be a list of steps"
        raise ProgramValidationError(msg, field)
    if required and not program:
        msg = "A print program cannot be empty"
        raise ProgramValidationError(msg, field)
    for step in program:
        _validate_step(step, field)


def _check_status_condition(name, where):
    if name not in STATUS_CONDITIONS:
        msg = f"{where} names an unknown condition {name!r}; must be one of {sorted(STATUS_CONDITIONS)}"
        raise ProgramValidationError(msg, "status_flags")


def _validate_status_values(values):
    """v2 ``status_flags.values``: exact status byte → the conditions it means.

    A bitmask can't express an *enumeration*, and TSPL's ``<ESC>!?`` answers one: a Y486BT with
    nothing but its lid open answers ``07``, which a bitmask reading decodes as out-of-paper AND
    jammed AND open — telling the user to load labels that are sitting right there.

    Keys are one status byte written as hex — ``"0a"`` — which is how a printer's manual prints
    them and what the app reads first. The app tries an exact ``values`` match, then falls back to
    the ``flags`` bitmask, so a partial map is fine.
    """
    if not isinstance(values, dict):
        msg = "status_flags.values must be an object mapping status codes to condition lists"
        raise ProgramValidationError(msg, "status_flags")
    for code, conditions in values.items():
        if isinstance(code, str):
            compact = code.strip()
            if not compact or not _HEX_RE.match(compact) or len(compact) > 2:
                msg = f'status_flags.values key {code!r} must be a one-byte code, e.g. "07"'
                raise ProgramValidationError(msg, "status_flags")
        elif not isinstance(code, int) or isinstance(code, bool) or not (0 <= code <= 255):
            msg = f"status_flags.values key {code!r} must be a one-byte code"
            raise ProgramValidationError(msg, "status_flags")
        if not isinstance(conditions, list):
            msg = f"status_flags.values[{code}] must be a list of condition names (use [] for ready)"
            raise ProgramValidationError(msg, "status_flags")
        for condition in conditions:
            if not isinstance(condition, str):
                msg = f"status_flags.values[{code}] entries must be strings"
                raise ProgramValidationError(msg, "status_flags")
            _check_status_condition(condition, f"status_flags.values[{code}]")


def _validate_status_flags(status_flags):
    if not status_flags:
        return
    if not isinstance(status_flags, dict):
        msg = "status_flags must be an object"
        raise ProgramValidationError(msg, "status_flags")
    unknown = set(status_flags) - {"byte", "flags", "values"}
    if unknown:
        msg = f"Unknown status_flags key(s): {', '.join(sorted(unknown))}"
        raise ProgramValidationError(msg, "status_flags")
    if "byte" in status_flags and not isinstance(status_flags["byte"], int):
        msg = "status_flags.byte must be an integer"
        raise ProgramValidationError(msg, "status_flags")
    flags = status_flags.get("flags", {})
    if not isinstance(flags, dict):
        msg = "status_flags.flags must be an object"
        raise ProgramValidationError(msg, "status_flags")
    for name, mask in flags.items():
        _check_status_condition(name, "status_flags.flags")
        masks = mask if isinstance(mask, list) else [mask]
        for one in masks:
            if isinstance(one, str):
                if not _HEX_RE.match(one) or not one:
                    msg = f"status_flags.flags[{name}] has invalid hex mask {one!r}"
                    raise ProgramValidationError(msg, "status_flags")
            elif not isinstance(one, int) or isinstance(one, bool):
                msg = f"status_flags.flags[{name}] must be an int or hex string"
                raise ProgramValidationError(msg, "status_flags")
    if "values" in status_flags:
        _validate_status_values(status_flags["values"])


def _validate_label_size_parse(label_size_parse):
    if not label_size_parse:
        return
    if not isinstance(label_size_parse, dict):
        msg = "label_size_parse must be an object"
        raise ProgramValidationError(msg, "label_size_parse")
    kind = label_size_parse.get("kind")
    if kind is not None and kind not in _SIZE_PARSE_KINDS:
        msg = f"label_size_parse.kind must be one of {sorted(_SIZE_PARSE_KINDS)}"
        raise ProgramValidationError(msg, "label_size_parse")
    pattern = label_size_parse.get("pattern")
    if kind == "ascii_regex" and pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            msg = f"label_size_parse.pattern is not a valid regex: {exc}"
            raise ProgramValidationError(msg, "label_size_parse") from exc


def validate_match_patterns(patterns, field):
    """Validate one of the match-pattern lists (ble_name / model / manufacturer).

    These are case-insensitive regexes the *app* compiles, so a bad one there is invisible until
    a printer fails to pair. Reject it in the admin instead.
    """
    if not patterns:
        return
    if not isinstance(patterns, list):
        msg = f"{field} must be a list of regex strings"
        raise ProgramValidationError(msg, field)
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            msg = f"{field} entries must be non-empty strings, got {pattern!r}"
            raise ProgramValidationError(msg, field)
        try:
            re.compile(pattern)
        except re.error as exc:
            msg = f"{field} entry {pattern!r} is not a valid regex: {exc}"
            raise ProgramValidationError(msg, field) from exc


def validate_profile_programs(
    *,
    print_program,
    status_program=None,
    label_size_program=None,
    status_flags=None,
    label_size_parse=None,
):
    """Validate every program on a :class:`ThermalPrinterProfile`. Used by ``clean()`` and tests."""
    validate_program(print_program, "print_program", required=True)
    validate_program(status_program, "status_program")
    validate_program(label_size_program, "label_size_program")
    _validate_status_flags(status_flags)
    _validate_label_size_parse(label_size_parse)


def serialize_profile(profile):
    """Shape a :class:`ThermalPrinterProfile` for GET /api/mobile/printers/profiles/."""
    return {
        "slug": profile.slug,
        "name": profile.name,
        "schema_version": profile.schema_version,
        "priority": profile.priority,
        # Stated rather than inferred. The app can work a profile's language out from its print
        # program (TSPL if it contains BITMAP, ESC/POS if 1d7630 …) and still falls back to that
        # for older deployments — but it needs the language to auto-select a profile when a probe
        # identifies one and exactly one profile speaks it, which is what removes the "pick your
        # printer type" dialog.
        "command_language": profile.command_language,
        "match": {
            "ble_name_patterns": profile.ble_name_patterns or [],
            # Matched against the GATT Device Information Service when the BLE name (which the
            # user can rename) matches nothing.
            "model_patterns": profile.model_patterns or [],
            "manufacturer_patterns": profile.manufacturer_patterns or [],
            "service_uuid": profile.service_uuid,
            "write_characteristic_uuid": profile.write_characteristic_uuid,
            "notify_characteristic_uuid": profile.notify_characteristic_uuid,
        },
        "transport": {
            "chunk_size": profile.chunk_size,
            "chunk_delay_ms": profile.chunk_delay_ms,
            "prefer_write_with_response": profile.prefer_write_with_response,
        },
        "raster": {
            "print_width_px": profile.print_width_px,
            "dpi": profile.dpi,
            "invert": profile.invert_raster,
            "max_label_width_mm": profile.max_label_width_mm,
            "max_label_height_mm": profile.max_label_height_mm,
        },
        "print_program": profile.print_program,
        "status_program": profile.status_program or [],
        "status_flags": profile.status_flags or {},
        "label_size_program": profile.label_size_program or [],
        "label_size_parse": profile.label_size_parse or {},
    }


# ---------------------------------------------------------------------------
# Seed data — ports the hardcoded in-app D11s driver verbatim so day-one
# behaviour is identical, plus a generic ESC/POS raster fallback. Imported by
# the seed data migration and asserted valid in tests.
# ---------------------------------------------------------------------------

_D11S_PRINT_PROGRAM_COMMON = [
    {"tx": "10 ff 10 00 {density}"},
    {"delay_ms": 100},
    {"tx": "10 ff 84 {paper_type}"},
    {"delay_ms": 50},
    {
        "repeat_per_copy": [
            {"tx": "00 00 00 00 00 00 00 00 00 00 00 00"},
            {"delay_ms": 50},
            {"tx": "10 ff fe 01"},
            {"delay_ms": 50},
            {"tx": "1d 76 30 00 {u16le:width_bytes} {u16le:height_px}"},
            {"tx_raster": True},
            {"delay_ms": 500},
            {"tx": "1d 0c"},
            {"delay_ms": 300},
        ]
    },
    {"tx": "10 ff fe 45"},
    {"await": {"any_hex_prefix": ["AA", "4F4B"], "timeout_ms": 60000, "on_timeout": "warn"}},
]

_D11S_STATUS_FLAGS = {
    "byte": -1,
    "flags": {
        "printing": "01",
        "cover_open": "02",
        "out_of_paper": "04",
        "low_battery": "08",
        "overheated": "50",
    },
}

# TSPL's real-time status query <ESC>!? answers an *enumeration*, not independent bits, so the
# exact-code map is the truth and the bitmask below is only the fallback for a code not listed.
# Measured on a VEVOR Y486BT 2026-07-26: lid open + a full roll loaded answers 0x07.
_TSPL_STATUS_FLAGS = {
    "byte": 0,
    "values": {
        "00": [],
        "01": ["cover_open"],
        "02": ["paper_jam"],
        "03": ["paper_jam", "cover_open"],
        "04": ["out_of_paper"],
        "05": ["out_of_paper", "cover_open"],
        "06": ["no_ribbon"],
        "07": ["no_ribbon", "cover_open"],
        "08": ["no_ribbon", "paper_jam"],
        "0a": ["no_ribbon", "out_of_paper"],
        "10": ["paused"],
        "20": ["printing"],
        "80": ["error"],
    },
    # Kept as the fallback for a code the map doesn't list. Lossy on its own — 0x07 decodes here as
    # cover_open AND paper_jam AND out_of_paper at once — which is exactly why `values` exists.
    "flags": {"cover_open": "01", "paper_jam": "02", "out_of_paper": "04", "printing": "20"},
}

SEED_PROFILES = [
    {
        "slug": "d11s-aiyin",
        "name": "Fichero / AiYin D11s",
        "priority": 10,
        "command_language": "d11s",
        "ble_name_patterns": ["^d11", "^fichero", "^aiyin"],
        # Device Information Service fallback for a renamed unit. Provisional until real units
        # report in via ObservedPrinter — widen/correct these from that admin list rather than
        # guessing again. Both D11s rows claim ^d11 (they are the same printer, different internal
        # board), so a model match still falls through to priority, exactly as the name match does.
        "model_patterns": ["^d11"],
        "manufacturer_patterns": ["aiyin", "fichero"],
        "service_uuid": "000018f0-0000-1000-8000-00805f9b34fb",
        "write_characteristic_uuid": "00002af1-0000-1000-8000-00805f9b34fb",
        "notify_characteristic_uuid": "00002af0-0000-1000-8000-00805f9b34fb",
        "chunk_size": 200,
        "chunk_delay_ms": 20,
        "print_width_px": 96,
        "dpi": 203,
        "print_program": _D11S_PRINT_PROGRAM_COMMON,
        "status_program": [{"tx": "10 ff 40"}],
        "status_flags": _D11S_STATUS_FLAGS,
        "notes": "Ported from the original hardcoded in-app D11s driver (AiYin board).",
    },
    {
        "slug": "d11s-lujiang",
        "name": "Fichero / AiYin D11s (LuJiang board)",
        "priority": 20,
        "command_language": "d11s",
        "ble_name_patterns": ["^d11", "^fichero", "^aiyin"],
        "model_patterns": ["^d11"],
        "manufacturer_patterns": ["lujiang"],
        "service_uuid": "000018f0-0000-1000-8000-00805f9b34fb",
        "write_characteristic_uuid": "00002af1-0000-1000-8000-00805f9b34fb",
        "notify_characteristic_uuid": "00002af0-0000-1000-8000-00805f9b34fb",
        "chunk_size": 200,
        "chunk_delay_ms": 20,
        "print_width_px": 96,
        "dpi": 203,
        # Identical to the AiYin board except the enable/stop opcodes.
        "print_program": [
            {"tx": "10 ff 10 00 {density}"},
            {"delay_ms": 100},
            {"tx": "10 ff 84 {paper_type}"},
            {"delay_ms": 50},
            {
                "repeat_per_copy": [
                    {"tx": "00 00 00 00 00 00 00 00 00 00 00 00"},
                    {"delay_ms": 50},
                    {"tx": "10 ff f1 03"},
                    {"delay_ms": 50},
                    {"tx": "1d 76 30 00 {u16le:width_bytes} {u16le:height_px}"},
                    {"tx_raster": True},
                    {"delay_ms": 500},
                    {"tx": "1d 0c"},
                    {"delay_ms": 300},
                ]
            },
            {"tx": "10 ff f1 45"},
            {"await": {"any_hex_prefix": ["AA", "4F4B"], "timeout_ms": 60000, "on_timeout": "warn"}},
        ],
        "status_program": [{"tx": "10 ff 40"}],
        "status_flags": _D11S_STATUS_FLAGS,
        "notes": "D11s LuJiang internal board — differs from AiYin only by enable/stop opcodes.",
    },
    {
        "slug": "tspl-raster",
        # Profile names are user-facing — when the app has to ask which printer this is, it shows
        # this string to an auction volunteer looking at a box on a table. Name the printer, not
        # the protocol.
        "name": "TSPL label printer (VEVOR Y486BT, TSC-compatible)",
        # Ahead of escpos-raster (900), behind the D11s rows (10/20).
        "priority": 100,
        # Uses status_flags.values, which only a v2 reader understands. An older app build will
        # correctly ignore this row rather than mis-decode the status byte.
        "schema_version": 2,
        "command_language": "tspl",
        "ble_name_patterns": ["^y486", "^y468"],
        "model_patterns": ["^y486"],
        # Deliberately empty. The Y486BT's Device Information Service reports "Feasycom" /
        # "FSC-BT986" — its *radio module*, which ships in dozens of unrelated products. Matching on
        # it would claim other vendors' hardware.
        "manufacturer_patterns": [],
        # VERIFIED GATT ids. The Y486BT is a Feasycom FSC-BT986 running Microchip's transparent-UART
        # service. These MUST be pinned: the service's first *writable* characteristic is …6daa…,
        # the module's CONTROL channel, so discovery-by-guessing writes label rasters into the
        # radio's configuration instead of printing. The data pipe is …8841….
        "service_uuid": "49535343-fe7d-4ae5-8fa9-9fafd205e455",
        "write_characteristic_uuid": "49535343-8841-43f4-a8d4-ecbe34729bb3",
        "notify_characteristic_uuid": "49535343-1e4d-4bd9-ba61-23c647249616",
        # A 3x2 label at 203 dpi is ~31 KB; a 4x6 is ~124 KB. 200-byte chunks at 20 ms would spend
        # ~12 s in pure pacing delay. The app still clamps every chunk to the live ATT MTU (185 on
        # this unit), so this is a pacing hint.
        "chunk_size": 500,
        "chunk_delay_ms": 5,
        "prefer_write_with_response": True,
        "print_width_px": 832,  # 4.09" head at 203 dpi
        "dpi": 203,
        # TSPL BITMAP prints on a *0* bit ("one = not painted, zero = painted"), the opposite of
        # ESC/POS. Without this every label comes out solid black, which burns through a roll fast.
        "invert_raster": True,
        "max_label_width_mm": 104.0,
        "max_label_height_mm": None,
        # No GAP command: the Y486BT calibrates its own gap on power-up, and a wrong GAP makes it
        # feed blank labels hunting for a notch. If a user reports mis-feeds on die-cut stock, add
        # "GAP 2 mm,0 mm\r\n" to the first tx_text. DIRECTION 0 is the TSPL default; flip to 1 if
        # labels come out upside down on some unit. No `await` step: TSPL has no print-completion
        # ack, and waiting for one is what produced "the printer didn't confirm the print finished".
        "print_program": [
            {"tx_text": "SIZE {width_mm} mm,{height_mm} mm\r\nDIRECTION 0\r\nREFERENCE 0,0\r\nCLS\r\n"},
            # BITMAP x,y,width_in_BYTES,height_in_DOTS,mode — the binary raster follows the comma
            # immediately, then PRINT terminates the job.
            {"tx_text": "BITMAP 0,0,{width_bytes},{height_px},0,"},
            {"tx_raster": True},
            {"tx_text": "\r\nPRINT {copies},1\r\n"},
        ],
        # TSPL real-time status query <ESC>!? → one status byte.
        "status_program": [{"tx": "1b 21 3f"}],
        "status_flags": _TSPL_STATUS_FLAGS,
        # Empty on purpose: TSPL has no standard "what media is loaded" query. The Y486BT was probed
        # with ~!T and ~!I and returned no notify frame at all, while <ESC>!? on the same link
        # answered immediately — its label recognition is internal and never reported over the wire.
        # The label size keeps coming from the user's UserLabelPrefs.
        "label_size_program": [],
        "label_size_parse": {},
        "notes": "TSPL/TSC-compatible direct thermal. Verified against a VEVOR Y486BT 2026-07-26.",
    },
    {
        "slug": "escpos-raster",
        # Named for what the user is looking at, not the protocol they've never heard of.
        "name": "Other thermal printer (ESC/POS)",
        "priority": 900,
        "command_language": "escpos",
        # No match patterns → never auto-matched; the app falls back to it for an unknown printer
        # by writing to the first writable characteristic (blank GATT ids = discover).
        "ble_name_patterns": [],
        "model_patterns": [],
        "manufacturer_patterns": [],
        "service_uuid": "",
        "write_characteristic_uuid": "",
        "notify_characteristic_uuid": "",
        "chunk_size": 200,
        "chunk_delay_ms": 20,
        # 384 dots = a full 58 mm ESC/POS printhead (203 dpi). The D11s rows use 96 (their 12 mm
        # head); this generic fallback must span a normal thermal head or it prints a ~12 mm strip.
        "print_width_px": 384,
        "dpi": 203,
        # Just the standard GS v 0 raster header + bitmap + feed — no vendor wrapper commands.
        "print_program": [
            {
                "repeat_per_copy": [
                    {"tx": "1d 76 30 00 {u16le:width_bytes} {u16le:height_px}"},
                    {"tx_raster": True},
                    {"delay_ms": 200},
                    {"tx": "1d 0c"},
                    {"delay_ms": 200},
                ]
            }
        ],
        "notes": "Generic fallback for printers that speak plain ESC/POS raster; editable per printer.",
    },
]


# ---------------------------------------------------------------------------
# Per-language starting points for a drafted profile
#
# Used by the "Draft a profile from this observation" admin action (see
# ObservedPrinterAdmin): a characterized ObservedPrinter knows the printer's command language, its
# GATT tree and what its status byte means, but not what bytes to send. These supply that — the
# same programs as the seeded rows, so a drafted profile starts from something known to drive real
# hardware. ``print_width_px`` and ``dpi`` still need a human with the printer's spec sheet.
# ---------------------------------------------------------------------------

_TSPL_SEED = next(p for p in SEED_PROFILES if p["slug"] == "tspl-raster")
_ESCPOS_SEED = next(p for p in SEED_PROFILES if p["slug"] == "escpos-raster")

LANGUAGE_TEMPLATES = {
    "tspl": {
        "print_program": _TSPL_SEED["print_program"],
        "status_program": _TSPL_SEED["status_program"],
        "invert_raster": True,
        "print_width_px": 832,
        "schema_version": 1,
    },
    "escpos": {
        "print_program": _ESCPOS_SEED["print_program"],
        "status_program": [],
        "invert_raster": False,
        "print_width_px": 384,
        "schema_version": 1,
    },
    "d11s": {
        "print_program": _D11S_PRINT_PROGRAM_COMMON,
        "status_program": [{"tx": "10 ff 40"}],
        "invert_raster": False,
        "print_width_px": 96,
        "schema_version": 1,
    },
    # ZPL needs schema v2 twice over: ^GF wants total_bytes (no v1 arithmetic) and ^GFA carries the
    # bitmap as ASCII hex rather than raw bytes.
    "zpl": {
        "print_program": [
            {
                "repeat_per_copy": [
                    {"tx_text": "^XA^FO0,0^GFA,{total_bytes},{total_bytes},{width_bytes},"},
                    {"tx_raster": {"encoding": "hex"}},
                    {"tx_text": "^FS^XZ\r\n"},
                ]
            }
        ],
        # ~HS host status: three ASCII lines of comma-separated settings, media size included.
        "status_program": [{"tx_text": "~HS"}],
        "invert_raster": False,
        "print_width_px": 832,
        "schema_version": 2,
    },
    # CPCL's EG is width-in-bytes, height, x, y then ASCII hex.
    "cpcl": {
        "print_program": [
            {
                "repeat_per_copy": [
                    {"tx_text": "! 0 200 200 {height_px} {copies}\r\n"},
                    {"tx_text": "EG {width_bytes} {height_px} 0 0 "},
                    {"tx_raster": {"encoding": "hex"}},
                    {"tx_text": "\r\nPRINT\r\n"},
                ]
            }
        ],
        "status_program": [],
        "invert_raster": False,
        "print_width_px": 576,
        "schema_version": 2,
    },
}
