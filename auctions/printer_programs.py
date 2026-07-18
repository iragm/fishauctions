"""Validation + seed data for :class:`ThermalPrinterProfile` command programs.

A *program* is a small, declarative JSON list of steps the mobile app executes in order to
drive a Bluetooth thermal label printer. Every byte a printer receives is defined in these
programs (stored in the DB, editable in Django admin) — the app is a generic interpreter, so
adding a printer is a data change, not an app release.

This module owns the schema (v1). It is imported by:

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
"""

import re

PROGRAM_SCHEMA_VERSION = 1

# Placeholders usable inside {tx}/{tx_text}. Scalar forms render as one byte (tx) / ASCII decimal
# (tx_text); the u16le form renders as a little-endian 16-bit value (2 bytes).
SCALAR_PLACEHOLDERS = frozenset(
    {
        "width_px",
        "height_px",
        "width_bytes",
        "width_mm",
        "height_mm",
        "density",
        "paper_type",
        "copies",
    }
)
U16LE_PLACEHOLDERS = frozenset({"width_bytes", "height_px", "width_px"})

# Every recognised step key. A step is a dict carrying exactly one of these.
STEP_KEYS = frozenset({"tx", "tx_text", "tx_raster", "delay_ms", "await", "repeat_per_copy"})
_ON_TIMEOUT = frozenset({"warn", "fail"})
_AWAIT_KEYS = frozenset({"any_hex_prefix", "timeout_ms", "on_timeout"})
_SIZE_PARSE_KINDS = frozenset({"ascii_regex", "bytes"})

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]*\Z")


class ProgramValidationError(ValueError):
    """A printer command program failed schema validation.

    ``field`` names the offending JSONField (``print_program`` …) so a ModelForm can attach the
    error to the right widget in the admin.
    """

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def _check_placeholders(text, field):
    for token in _PLACEHOLDER_RE.findall(text):
        if ":" in token:
            fn, _, name = token.partition(":")
            if fn != "u16le" or name not in U16LE_PLACEHOLDERS:
                msg = f"Unknown placeholder {{{token}}}"
                raise ProgramValidationError(msg, field)
        elif token not in SCALAR_PLACEHOLDERS:
            msg = f"Unknown placeholder {{{token}}}"
            raise ProgramValidationError(msg, field)


def _check_hex_literal(text, field, *, allow_placeholders=True):
    """Validate a hex byte string. Whitespace is ignored; placeholders stand in for whole bytes.

    Each literal run between placeholders must be an even number of hex digits (whole bytes).
    """
    if allow_placeholders:
        _check_placeholders(text, field)
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
        if value is not True:
            msg = "tx_raster must be the literal true"
            raise ProgramValidationError(msg, field)
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


def _validate_status_flags(status_flags):
    if not status_flags:
        return
    if not isinstance(status_flags, dict):
        msg = "status_flags must be an object"
        raise ProgramValidationError(msg, "status_flags")
    if "byte" in status_flags and not isinstance(status_flags["byte"], int):
        msg = "status_flags.byte must be an integer"
        raise ProgramValidationError(msg, "status_flags")
    flags = status_flags.get("flags", {})
    if not isinstance(flags, dict):
        msg = "status_flags.flags must be an object"
        raise ProgramValidationError(msg, "status_flags")
    for name, mask in flags.items():
        masks = mask if isinstance(mask, list) else [mask]
        for one in masks:
            if isinstance(one, str):
                if not _HEX_RE.match(one) or not one:
                    msg = f"status_flags.flags[{name}] has invalid hex mask {one!r}"
                    raise ProgramValidationError(msg, "status_flags")
            elif not isinstance(one, int) or isinstance(one, bool):
                msg = f"status_flags.flags[{name}] must be an int or hex string"
                raise ProgramValidationError(msg, "status_flags")


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
        "match": {
            "ble_name_patterns": profile.ble_name_patterns or [],
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

SEED_PROFILES = [
    {
        "slug": "d11s-aiyin",
        "name": "Fichero / AiYin D11s",
        "priority": 10,
        "ble_name_patterns": ["^d11", "^fichero", "^aiyin"],
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
        "ble_name_patterns": ["^d11", "^fichero", "^aiyin"],
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
        "slug": "escpos-raster",
        "name": "Raw ESC/POS raster (GS v 0)",
        "priority": 900,
        # No name patterns → never auto-matched; the app falls back to it for an unknown printer
        # by writing to the first writable characteristic (blank GATT ids = discover).
        "ble_name_patterns": [],
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
