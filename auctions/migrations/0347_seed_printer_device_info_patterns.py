"""Seed model/manufacturer match patterns on the two D11s profiles.

A BLE name is user-editable and every reseller ships this board under a different one ("D11",
"Fichero", "AiYin", whatever they typed), so a name miss is the common case. The app now falls back
to the printer's GATT Device Information Service — but only if the profiles say what to match, which
rows seeded before 0346 don't.

Both D11s rows claim ``^d11`` (same printer, different internal board), so a model match falls
through to priority exactly as the name match already does. Only still-empty lists are filled, so an
admin who has already tuned a row keeps their patterns. These are provisional: correct them from the
Observed printers admin list once real units report in.
"""

from django.db import migrations

_PATTERNS = {
    "d11s-aiyin": {"model_patterns": ["^d11"], "manufacturer_patterns": ["aiyin", "fichero"]},
    "d11s-lujiang": {"model_patterns": ["^d11"], "manufacturer_patterns": ["lujiang"]},
}


def seed_patterns(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    for slug, patterns in _PATTERNS.items():
        for profile in ThermalPrinterProfile.objects.filter(slug=slug):
            fields = [field for field, value in patterns.items() if not getattr(profile, field) and value]
            for field in fields:
                setattr(profile, field, patterns[field])
            if fields:
                profile.save(update_fields=fields)


def unseed_patterns(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    for slug, patterns in _PATTERNS.items():
        for profile in ThermalPrinterProfile.objects.filter(slug=slug):
            fields = [field for field, value in patterns.items() if getattr(profile, field) == value]
            for field in fields:
                setattr(profile, field, [])
            if fields:
                profile.save(update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0346_thermalprinterprofile_manufacturer_patterns_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_patterns, unseed_patterns),
    ]
