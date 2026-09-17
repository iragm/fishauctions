"""Turn a characterized :class:`~auctions.models.ObservedPrinter` into a draft printer profile.

The app learns most of what a profile needs by asking the printer -- which command language answers,
what its GATT tree looks like -- and derives the one thing no query can discover, what its status
byte means, by walking the user through four physical states whose meaning is known in advance.
Working that out for the Y486BT took someone with the hardware and an afternoon; this is that
afternoon as four button presses, done by whoever owns the printer.

What arrives is evidence, and this assembles it into a hypothesis. The drafted profile is created
**disabled**: the person who submitted the observation is holding the printer, and "Print test
label" is what confirms it.
"""

import logging
import re

from django.utils.text import slugify

from auctions.printer_programs import LANGUAGE_TEMPLATES

logger = logging.getLogger(__name__)

# Standard GATT services every BLE device carries; the vendor service is what's left after these.
_GENERIC_SERVICE_PREFIXES = ("1800", "1801", "180a", "180f", "1802", "1803", "1804", "181c")
# The short forms above expand into the Bluetooth base UUID.
_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

_WRITE_PROPERTIES = frozenset({"write", "writenr", "writewithoutresponse", "write_without_response"})
_NOTIFY_PROPERTIES = frozenset({"notify", "indicate"})

MAX_NOTE_CHARS = 4000


class DraftError(Exception):
    """This observation can't be turned into a profile, and says why."""


def profile_matches_observation(profile, observation):
    """Would *profile* now claim the printer in *observation*?

    Mirrors the app's matching against the advertised BLE name and the GATT Device Information Service.
    Used to find whose hand-identified printer a newly enabled profile has started supporting.
    """
    candidates = (
        (profile.ble_name_patterns, observation.ble_name),
        (profile.model_patterns, observation.model),
        (profile.manufacturer_patterns, observation.manufacturer),
    )
    for patterns, value in candidates:
        if not value:
            continue
        for pattern in patterns or []:
            try:
                if re.search(pattern, value, re.IGNORECASE):
                    return True
            except re.error:
                # A bad regex is rejected on save, but a row saved before that check existed must
                # not break every later profile edit.
                logger.warning("Ignoring invalid match pattern %r on profile %s", pattern, profile.slug)
    return False


def _short_uuid(uuid):
    """``0000180a-0000-1000-8000-00805f9b34fb`` → ``180a``; a vendor UUID is returned unchanged."""
    short = str(uuid).strip().lower()
    if short.endswith(_BASE_UUID_SUFFIX):
        return short[:8].lstrip("0") or "0"
    return short


def _is_generic_service(uuid):
    return _short_uuid(uuid) in _GENERIC_SERVICE_PREFIXES


def _properties(characteristic):
    return {str(p).strip().lower().replace("-", "") for p in characteristic.get("properties") or []}


def pick_gatt_ids(gatt):
    """Choose (service, write characteristic, notify characteristic) from a reported GATT tree.

    Blanks mean "discover at runtime", which is better than a confidently wrong id. Picking these wrong
    is silent: the Y486BT's first writable characteristic belongs to its radio module's control channel,
    so labels went into the radio's configuration with nothing printed and nothing errored.

    So only a non-generic service is considered, and its writable characteristics are ranked by how much
    they look like a data pipe rather than a config channel -- a pipe takes write-without-response and
    isn't readable. A heuristic, which is the other reason a draft ships disabled and the full tree goes
    into the notes.
    """
    if not isinstance(gatt, list):
        return "", "", ""
    for service in gatt:
        if not isinstance(service, dict):
            continue
        uuid = str(service.get("uuid") or "").strip().lower()
        if not uuid or _is_generic_service(uuid):
            continue
        characteristics = [c for c in service.get("characteristics") or [] if isinstance(c, dict) and c.get("uuid")]
        writable = [c for c in characteristics if _properties(c) & _WRITE_PROPERTIES]
        notifying = [c for c in characteristics if _properties(c) & _NOTIFY_PROPERTIES]
        if not writable:
            continue
        writable.sort(key=_pipe_rank)
        notify_uuid = str(notifying[0]["uuid"]).strip().lower() if notifying else ""
        return uuid, str(writable[0]["uuid"]).strip().lower(), notify_uuid
    return "", "", ""


def _pipe_rank(characteristic):
    """Sort key: lower is more likely to be the print data pipe rather than a config channel."""
    properties = _properties(characteristic)
    takes_writenr = bool(properties & {"writenr", "writewithoutresponse", "write_without_response"})
    readable = "read" in properties
    return (not takes_writenr, readable)


def draft_slug(observation):
    """A stable, readable slug from the model or BLE name.

    Deliberately not uniquified: two people reporting the same printer should refresh one draft rather
    than race to create two rows differing by suffix.
    """
    return (slugify(observation.model or observation.ble_name or "") or f"printer-{observation.pk}")[:50]


def _draft_notes(observation):
    """The raw evidence, verbatim, so whoever confirms the draft can see what it was built from."""
    lines = [
        f"Drafted from ObservedPrinter #{observation.pk} "
        f"({observation.ble_name or 'unnamed'} / {observation.manufacturer or '?'} "
        f"{observation.model or '?'}), seen {observation.times_seen}x.",
        "",
        "STILL NEEDS A HUMAN: print_width_px and dpi come off the printer's spec sheet, and the "
        "profile stays disabled until a test label confirms it.",
    ]
    if observation.manufacturer:
        # The DIS often names the radio module rather than the printer -- a Y486BT reports
        # "Feasycom", which ships in dozens of unrelated products -- so matching on it would claim
        # other vendors' hardware. Drafted anyway, and flagged.
        lines += [
            "",
            f"CHECK manufacturer_patterns: the printer reported {observation.manufacturer!r}. If that is "
            "the Bluetooth module rather than the printer's maker, clear the pattern -- it would "
            "claim unrelated hardware from the same module vendor.",
        ]
    if observation.probed_language:
        lines += ["", f"Probed command language: {observation.probed_language}"]
    if observation.probe_replies:
        lines += ["", f"Probe replies: {observation.probe_replies}"]
    if observation.gatt:
        # The ids above are a heuristic pick from this tree; a wrong write characteristic prints
        # nothing and reports nothing.
        lines += ["", f"GATT tree: {observation.gatt}"]
    if observation.status_captures:
        lines += ["", f"Status captures: {observation.status_captures}"]
    if observation.status_ambiguities:
        lines += ["", "Status ambiguities (states this printer cannot tell apart):"]
        lines += [f"  - {one}" for one in observation.status_ambiguities]
    return "\n".join(lines)[:MAX_NOTE_CHARS]


def draft_profile_from_observation(observation):
    """Create or refresh a disabled :class:`ThermalPrinterProfile` from *observation*.

    Returns ``(profile, created)``, raising :class:`DraftError` without a command language, since the
    print program is the one part no probe can discover.

    Re-running updates the existing draft rather than piling up rows, but never touches an enabled
    profile: at that point a human has taken ownership of it.
    """
    from auctions.models import ThermalPrinterProfile

    language = (observation.probed_language or "").strip().lower()
    template = LANGUAGE_TEMPLATES.get(language)
    if template is None:
        msg = (
            f"no print program template for command language {language or '(none probed)'} — "
            "author the print_program by hand, or add a template to printer_programs.LANGUAGE_TEMPLATES"
        )
        raise DraftError(msg)

    service_uuid, write_uuid, notify_uuid = pick_gatt_ids(observation.gatt)

    status_values = observation.derived_status_values if isinstance(observation.derived_status_values, dict) else {}
    status_flags = {"byte": 0, "values": status_values} if status_values else {}
    # v2 is required by the template's own program (ZPL and CPCL need {total_bytes} and a hex
    # raster) or by an exact-code status map.
    schema_version = max(template["schema_version"], 2 if status_values else 1)

    slug = draft_slug(observation)
    # An enabled row is one a human owns, including every seeded profile, so a draft never
    # overwrites one; a disabled draft is refreshed in place.
    if ThermalPrinterProfile.objects.filter(slug=slug, enabled=True).exists():
        msg = f"profile {slug} already exists and is enabled; edit it directly rather than redrafting"
        raise DraftError(msg)

    fields = {
        "name": (observation.model or observation.ble_name or slug)[:100],
        # Disabled: a draft is a hypothesis until a test label confirms it.
        "enabled": False,
        # Behind every hand-written row, ahead of the escpos-raster catch-all.
        "priority": 500,
        "schema_version": schema_version,
        "command_language": language,
        "ble_name_patterns": [],
        "model_patterns": [f"^{re.escape(observation.model)}"] if observation.model else [],
        "manufacturer_patterns": [re.escape(observation.manufacturer)] if observation.manufacturer else [],
        "service_uuid": service_uuid,
        "write_characteristic_uuid": write_uuid,
        "notify_characteristic_uuid": notify_uuid,
        "print_program": template["print_program"],
        "status_program": template["status_program"],
        "status_flags": status_flags,
        "invert_raster": template["invert_raster"],
        "print_width_px": template["print_width_px"],
        "notes": _draft_notes(observation),
    }
    profile, created = ThermalPrinterProfile.objects.update_or_create(slug=slug, defaults=fields)
    return profile, created
