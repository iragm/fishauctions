"""Validation and seed data for :class:`ThermalPrinterProfile` command programs.

A program is a JSON list of steps the mobile app runs to drive a Bluetooth thermal printer. Every
byte comes from these DB-stored programs, so adding a printer is a data change, not an app release.

Used by ``ThermalPrinterProfile.clean()``, the profiles mobile API, the seed migration and tests.

Schema v1 step types::

    {"tx": "10 ff fe 01"}                    # hex bytes (whitespace ignored)
    {"tx_text": "SIZE {width_mm} mm\\r\\n"}   # ASCII with placeholders (TSPL/ZPL/ESC-POS)
    {"tx_raster": true}                       # the packed 1-bit bitmap body
    {"delay_ms": 50}
    {"await": {"any_hex_prefix": ["AA"], "timeout_ms": 60000, "on_timeout": "warn"}}
    {"repeat_per_copy": [ ...steps... ]}      # run nested steps once per requested copy

Schema v2 additions (v1 rows keep working)::

    {"tx_text": "^GFA,{total_bytes},…"}       # width_bytes * height_px, incl. {u32le:total_bytes}
    {"tx_raster": {"encoding": "hex"}}        # ASCII-hex bitmap body (ZPL ^GFA, CPCL EG)
    "status_flags": {"values": {"07": ["no_ribbon", "cover_open"]}}   # exact codes, not bitmasks

Validation is version-agnostic: the app refuses schemas it doesn't know, and rejecting v2 here
would stop admins authoring v2 rows.
"""

import re

# Reported to the app as ``schema_version_max``.
PROGRAM_SCHEMA_VERSION = 2

# Placeholders for {tx}/{tx_text}. Scalars render as one byte (tx) or ASCII decimal (tx_text);
# u16le/u32le render as 2 or 4 little-endian bytes.
SCALAR_PLACEHOLDERS = frozenset(
    {
        "width_px",
        "height_px",
        "width_bytes",
        # v2: raster body size (width_bytes * height_px), which ZPL's ^GF needs.
        "total_bytes",
        "width_mm",
        "height_mm",
        "density",
        "paper_type",
        "copies",
    }
)
U16LE_PLACEHOLDERS = frozenset({"width_bytes", "height_px", "width_px"})
# 16 bits overflows on a 4x6" raster (~270kB), so size scalars have 32-bit forms.
U32LE_PLACEHOLDERS = frozenset({"total_bytes", "width_bytes", "height_px", "width_px"})
_WIDTH_FUNCTIONS = {"u16le": U16LE_PLACEHOLDERS, "u32le": U32LE_PLACEHOLDERS}

# A bare {name} in a `tx` hex template is one byte, so size scalars are always rejected there:
# a profile tested on a small label would silently truncate on a 4x6. Use {u16le:…}/{u32le:…}.
BARE_BYTE_PLACEHOLDERS = frozenset({"density", "paper_type", "copies"})

# A step is a dict with exactly one of these keys.
STEP_KEYS = frozenset({"tx", "tx_text", "tx_raster", "delay_ms", "await", "repeat_per_copy"})
_ON_TIMEOUT = frozenset({"warn", "fail"})
_AWAIT_KEYS = frozenset({"any_hex_prefix", "timeout_ms", "on_timeout"})
_SIZE_PARSE_KINDS = frozenset({"ascii_regex", "bytes"})
# "binary" is v1's raw bytes; "hex" doubles the size, so it's opt-in.
_RASTER_ENCODINGS = frozenset({"binary", "hex"})

# Conditions the app has a message for, used by status_flags.flags and .values. Typos are rejected.
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


# The language a profile's print program speaks, declared so the app can auto-select a profile
# when a probe identifies a language exactly one profile speaks.
COMMAND_LANGUAGE_CHOICES = [
    ("tspl", "TSPL / TSPL2 (TSC-compatible)"),
    ("escpos", "ESC/POS"),
    ("zpl", "ZPL"),
    ("cpcl", "CPCL"),
    ("d11s", "D11s vendor protocol"),
    ("other", "Other / vendor-specific"),
]


class ProgramValidationError(ValueError):
    """A printer command program failed validation. ``field`` names the offending JSONField for the admin form."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def _check_placeholders(text, field, *, bare_must_be_byte=False):
    """Validate every ``{placeholder}`` in *text*; ``bare_must_be_byte`` for ``tx`` hex templates."""
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
    """Validate a hex byte string: whitespace ignored, whole bytes between placeholders."""
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
    """``true`` (v1) or ``{"encoding": "binary"|"hex"}`` (v2). ``false`` is rejected: it would print a blank label."""
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
    """Validate one program (a list of steps), raising :class:`ProgramValidationError`."""
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
    """v2 ``status_flags.values``: exact status byte (hex, e.g. ``"0a"``) → conditions.

    For printers whose status is an enumeration: TSPL's ``<ESC>!?`` answers ``07`` for lid open, which
    a bitmask misreads. The app tries ``values`` first, then ``flags``.
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
    """Validate a match-pattern list (ble_name / model / manufacturer): regexes the app compiles."""
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
    """Validate every program on a :class:`ThermalPrinterProfile`."""
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
        # Declared, so the app can auto-select a profile from a language probe.
        "command_language": profile.command_language,
        "match": {
            "ble_name_patterns": profile.ble_name_patterns or [],
            # Matched against the GATT Device Information Service when the BLE name matches nothing.
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
# Seed data: the original in-app D11s driver, a TSPL profile, and a generic ESC/POS fallback.
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

# TSPL's <ESC>!? answers an enumeration, so the exact-code map is primary. Measured on a VEVOR
# Y486BT 2026-07-26: lid open with a full roll answers 0x07.
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
    # Fallback for unlisted codes; lossy on its own.
    "flags": {"cover_open": "01", "paper_jam": "02", "out_of_paper": "04", "printing": "20"},
}

SEED_PROFILES = [
    {
        "slug": "d11s-aiyin",
        "name": "Fichero / AiYin D11s",
        "priority": 10,
        "command_language": "d11s",
        "ble_name_patterns": ["^d11", "^fichero", "^aiyin"],
        # Device Information Service fallback for a renamed unit; provisional until ObservedPrinter
        # reports confirm it. Both D11s rows claim ^d11, so priority decides.
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
        # User-facing: name the printer, not the protocol.
        "name": "TSPL label printer (VEVOR Y486BT, TSC-compatible)",
        # Ahead of escpos-raster (900), behind the D11s rows (10/20).
        "priority": 100,
        # status_flags.values needs a v2 reader; older apps skip this row.
        "schema_version": 2,
        "command_language": "tspl",
        "ble_name_patterns": ["^y486", "^y468"],
        "model_patterns": ["^y486"],
        # Empty: the Device Information Service reports the radio module ("Feasycom"), used in
        # unrelated products.
        "manufacturer_patterns": [],
        # Verified GATT ids, which must be pinned: the first writable characteristic (…6daa…) is
        # the radio module's control channel. The data pipe is …8841….
        "service_uuid": "49535343-fe7d-4ae5-8fa9-9fafd205e455",
        "write_characteristic_uuid": "49535343-8841-43f4-a8d4-ecbe34729bb3",
        "notify_characteristic_uuid": "49535343-1e4d-4bd9-ba61-23c647249616",
        # A pacing hint; the app still clamps chunks to the ATT MTU (185 on this unit).
        "chunk_size": 500,
        "chunk_delay_ms": 5,
        "prefer_write_with_response": True,
        "print_width_px": 832,  # 4.09" head at 203 dpi
        "dpi": 203,
        # TSPL BITMAP paints on a 0 bit, the opposite of ESC/POS; without this labels print solid black.
        "invert_raster": True,
        "max_label_width_mm": 104.0,
        "max_label_height_mm": None,
        # No GAP (the Y486BT self-calibrates; add "GAP 2 mm,0 mm\r\n" if die-cut stock mis-feeds).
        # Flip DIRECTION to 1 if labels print upside down. No `await`: TSPL has no completion ack.
        "print_program": [
            {"tx_text": "SIZE {width_mm} mm,{height_mm} mm\r\nDIRECTION 0\r\nREFERENCE 0,0\r\nCLS\r\n"},
            # BITMAP x,y,width_in_bytes,height_in_dots,mode, then the raster, then PRINT.
            {"tx_text": "BITMAP 0,0,{width_bytes},{height_px},0,"},
            {"tx_raster": True},
            {"tx_text": "\r\nPRINT {copies},1\r\n"},
        ],
        # TSPL real-time status query <ESC>!? → one status byte.
        "status_program": [{"tx": "1b 21 3f"}],
        "status_flags": _TSPL_STATUS_FLAGS,
        # Empty: TSPL has no media query (~!T and ~!I got no reply), so size comes from UserLabelPrefs.
        "label_size_program": [],
        "label_size_parse": {},
        "notes": "TSPL/TSC-compatible direct thermal. Verified against a VEVOR Y486BT 2026-07-26.",
    },
    {
        "slug": "escpos-raster",
        "name": "Other thermal printer (ESC/POS)",
        "priority": 900,
        "command_language": "escpos",
        # No patterns, so never auto-matched; used for unknown printers with discovered GATT ids.
        "ble_name_patterns": [],
        "model_patterns": [],
        "manufacturer_patterns": [],
        "service_uuid": "",
        "write_characteristic_uuid": "",
        "notify_characteristic_uuid": "",
        "chunk_size": 200,
        "chunk_delay_ms": 20,
        # A full 58 mm head at 203 dpi; 96 (the D11s head) would print a 12 mm strip.
        "print_width_px": 384,
        "dpi": 203,
        # Standard GS v 0 raster header, bitmap and feed.
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
# Per-language starting programs for the "Draft a profile from this observation" admin action
# (ObservedPrinterAdmin). print_width_px and dpi still need the printer's spec sheet.
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
    # ZPL needs v2: ^GF wants total_bytes, and ^GFA sends the bitmap as ASCII hex.
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
        # ~HS host status includes media size.
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
