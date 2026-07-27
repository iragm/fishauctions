"""Recording which Bluetooth printers users actually pair, and how they were identified.

Backs POST /api/mobile/printers/observed/. The app fires this once per successful pairing and
ignores the response, so nothing here may be strict enough to lose a row: over-long strings are
truncated rather than rejected, because a printer nobody has a profile for is exactly the row
worth keeping (see :class:`auctions.models.ObservedPrinter`).
"""

import json

from auctions.models import ObservedPrinter

MAX_SERVICE_UUIDS = 32  # a chatty printer advertises a handful; cap the JSON blob regardless
# Ceiling on each free-form JSON blob (probe replies, the GATT tree, status captures). A real
# payload is well under a kilobyte; anything past this is a bug or an attempt to fill the column,
# and dropping it is better than 400-ing a report the app won't retry.
MAX_JSON_BYTES = 20000


def _truncate(value, field):
    """Coerce to a string clipped to the model's max_length (never a 400 for a long string)."""
    text = ("" if value is None else str(value)).strip()
    return text[: ObservedPrinter._meta.get_field(field).max_length]


def _clean_service_uuids(uuids):
    """Lowercase, de-dupe (order-preserving) and cap the advertised service UUID list."""
    seen = []
    for raw in uuids or []:
        one = str(raw).strip().lower()
        if one and one not in seen:
            seen.append(one)
        if len(seen) >= MAX_SERVICE_UUIDS:
            break
    return seen


def _clean_json(value, empty):
    """Accept a JSON blob of the expected container type, or fall back to *empty*.

    Truncation here means dropping the whole blob (there is no sensible way to clip a tree), so the
    cap is generous. A wrong type is dropped rather than rejected — same reasoning as ``_truncate``:
    a printer nobody has a profile for is exactly the row worth keeping.
    """
    if not isinstance(value, type(empty)):
        return empty
    if not value:
        return empty
    try:
        if len(json.dumps(value)) > MAX_JSON_BYTES:
            return empty
    except (TypeError, ValueError):
        return empty
    return value


def record_observation(user, data):
    """Upsert the (user, ble_name, model, profile_slug) row for one pairing.

    Returns (observation, created). The device-info fields, service list and matched_by are
    refreshed on every sighting — a firmware update or a newly seeded profile should show the
    current truth — and ``times_seen`` counts pairings. ``printed_ok`` only ever latches true:
    once a printer has produced a label, a later pairing that didn't print doesn't unsay it.
    """
    fields = {
        "ble_name": _truncate(data.get("ble_name"), "ble_name"),
        "manufacturer": _truncate(data.get("manufacturer"), "manufacturer"),
        "model": _truncate(data.get("model"), "model"),
        "firmware": _truncate(data.get("firmware"), "firmware"),
        "hardware": _truncate(data.get("hardware"), "hardware"),
        "profile_slug": _truncate(data.get("profile_slug"), "profile_slug"),
        "matched_by": data.get("matched_by", ""),
        "service_uuids": _clean_service_uuids(data.get("service_uuids")),
        "printed_ok": bool(data.get("printed_ok")),
        "probe_replies": _clean_json(data.get("probe_replies"), {}),
        "probed_language": _truncate(data.get("probed_language"), "probed_language").lower(),
        "gatt": _clean_json(data.get("gatt"), []),
        "status_captures": _clean_json(data.get("status_captures"), {}),
        "derived_status_values": _clean_json(data.get("derived_status_values"), {}),
        "status_ambiguities": _clean_json(data.get("status_ambiguities"), []),
    }
    # The work-queue flag: a row with captured status replies has everything needed to write a
    # profile. Never unset by a later plain pairing — see the merge below.
    fields["characterized"] = bool(fields["status_captures"])
    key = {
        "user": user,
        "ble_name": fields["ble_name"],
        "model": fields["model"],
        "profile_slug": fields["profile_slug"],
    }
    # get_or_create already resolves the unique-constraint race (create in a savepoint, re-get on
    # IntegrityError), which is all two phones pairing the same printer at once need.
    observation, created = ObservedPrinter.objects.get_or_create(defaults=fields, **key)
    if created:
        return observation, True

    observation.manufacturer = fields["manufacturer"]
    observation.firmware = fields["firmware"]
    observation.hardware = fields["hardware"]
    observation.matched_by = fields["matched_by"]
    observation.service_uuids = fields["service_uuids"]
    observation.printed_ok = observation.printed_ok or fields["printed_ok"]
    observation.times_seen += 1

    # Probe and characterization evidence latches: most pairings carry none (the app only probes
    # when nothing matched, and only characterizes when the user walks the sheet), so overwriting
    # with the empty default would throw away the one report that was worth having.
    for field in ("probe_replies", "probed_language", "gatt"):
        if fields[field]:
            setattr(observation, field, fields[field])
    if fields["status_captures"]:
        # Replaced as a set — a fresh run supersedes an older one, and an empty ambiguity list is
        # a real result ("this printer can tell every state apart"), not a missing one.
        observation.status_captures = fields["status_captures"]
        observation.derived_status_values = fields["derived_status_values"]
        observation.status_ambiguities = fields["status_ambiguities"]
        observation.characterized = True

    observation.save(
        update_fields=[
            "manufacturer",
            "firmware",
            "hardware",
            "matched_by",
            "service_uuids",
            "printed_ok",
            "times_seen",
            "probe_replies",
            "probed_language",
            "gatt",
            "status_captures",
            "derived_status_values",
            "status_ambiguities",
            "characterized",
            "last_seen",
        ]
    )
    return observation, False
