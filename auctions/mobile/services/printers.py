"""Recording which Bluetooth printers users actually pair, and how they were identified.

Backs POST /api/mobile/printers/observed/. The app fires this once per successful pairing and
ignores the response, so nothing here may be strict enough to lose a row: over-long strings are
truncated rather than rejected, because a printer nobody has a profile for is exactly the row
worth keeping (see :class:`auctions.models.ObservedPrinter`).
"""

from auctions.models import ObservedPrinter

MAX_SERVICE_UUIDS = 32  # a chatty printer advertises a handful; cap the JSON blob regardless


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
    }
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
    observation.save(
        update_fields=[
            "manufacturer",
            "firmware",
            "hardware",
            "matched_by",
            "service_uuids",
            "printed_ok",
            "times_seen",
            "last_seen",
        ]
    )
    return observation, False
