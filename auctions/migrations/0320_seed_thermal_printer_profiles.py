"""Seed the built-in thermal printer profiles.

Ports the previously hardcoded in-app D11s driver into editable ThermalPrinterProfile rows so day-one
behaviour is identical, plus a generic ESC/POS raster fallback. Idempotent (keyed on slug); the
reverse removes only these seeded slugs.
"""

from django.db import migrations

from auctions.printer_programs import SEED_PROFILES


def seed_profiles(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    for profile in SEED_PROFILES:
        ThermalPrinterProfile.objects.update_or_create(
            slug=profile["slug"],
            defaults={
                "name": profile["name"],
                "enabled": True,
                "priority": profile["priority"],
                "schema_version": 1,
                "ble_name_patterns": profile.get("ble_name_patterns", []),
                "service_uuid": profile.get("service_uuid", ""),
                "write_characteristic_uuid": profile.get("write_characteristic_uuid", ""),
                "notify_characteristic_uuid": profile.get("notify_characteristic_uuid", ""),
                "chunk_size": profile.get("chunk_size", 200),
                "chunk_delay_ms": profile.get("chunk_delay_ms", 20),
                "prefer_write_with_response": profile.get("prefer_write_with_response", True),
                "print_width_px": profile.get("print_width_px", 96),
                "dpi": profile.get("dpi", 203),
                "invert_raster": profile.get("invert_raster", False),
                "max_label_width_mm": profile.get("max_label_width_mm"),
                "max_label_height_mm": profile.get("max_label_height_mm"),
                "print_program": profile["print_program"],
                "status_program": profile.get("status_program", []),
                "status_flags": profile.get("status_flags", {}),
                "label_size_program": profile.get("label_size_program", []),
                "label_size_parse": profile.get("label_size_parse", {}),
                "notes": profile.get("notes", ""),
            },
        )


def unseed_profiles(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    slugs = [p["slug"] for p in SEED_PROFILES]
    ThermalPrinterProfile.objects.filter(slug__in=slugs).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0319_thermalprinterprofile_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_profiles, unseed_profiles),
    ]
