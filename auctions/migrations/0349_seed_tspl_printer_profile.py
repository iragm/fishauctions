"""Seed the TSPL profile and declare every seeded row's command language.

Three things, all data:

* **``tspl-raster``** — none of the three existing profiles can drive a TSC-compatible label
  printer. Both D11s rows are a 96-dot head speaking a vendor ``10 ff …`` protocol and
  ``escpos-raster`` sends ``GS v 0``; a VEVOR Y486BT (and the large family of rebadged 4" Chinese
  label printers it belongs to) speaks TSPL/CPCL/ZPL and never ESC/POS. Picking ``d11s-aiyin`` for
  one produced the reported "the printer didn't confirm the print finished": the D11s stop opcode
  means nothing to it, its ``AA`` ack never arrives, and the ``on_timeout: warn`` branch fires.
  Verified on hardware 2026-07-26.

* **``command_language``** — stated on each row so a command-language probe can auto-select the
  only profile that speaks what a printer answered in, instead of the app inferring it from the
  program's bytes.

* **``escpos-raster`` rename** — "Raw ESC/POS raster (GS v 0)" is what the app shows a volunteer
  when it has to ask which printer is on the table. Only renamed if still at the old name, so an
  admin who has already retitled the row keeps their name.

Idempotent and keyed on slug, like 0320/0347.
"""

from django.db import migrations

from auctions.printer_programs import SEED_PROFILES

_TSPL = next(p for p in SEED_PROFILES if p["slug"] == "tspl-raster")
_LANGUAGES = {p["slug"]: p.get("command_language", "") for p in SEED_PROFILES}
_OLD_ESCPOS_NAME = "Raw ESC/POS raster (GS v 0)"
_NEW_ESCPOS_NAME = next(p["name"] for p in SEED_PROFILES if p["slug"] == "escpos-raster")


def seed(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")

    ThermalPrinterProfile.objects.update_or_create(
        slug=_TSPL["slug"],
        defaults={
            "name": _TSPL["name"],
            "enabled": True,
            "priority": _TSPL["priority"],
            "schema_version": _TSPL["schema_version"],
            "command_language": _TSPL["command_language"],
            "ble_name_patterns": _TSPL["ble_name_patterns"],
            "model_patterns": _TSPL["model_patterns"],
            "manufacturer_patterns": _TSPL["manufacturer_patterns"],
            "service_uuid": _TSPL["service_uuid"],
            "write_characteristic_uuid": _TSPL["write_characteristic_uuid"],
            "notify_characteristic_uuid": _TSPL["notify_characteristic_uuid"],
            "chunk_size": _TSPL["chunk_size"],
            "chunk_delay_ms": _TSPL["chunk_delay_ms"],
            "prefer_write_with_response": _TSPL["prefer_write_with_response"],
            "print_width_px": _TSPL["print_width_px"],
            "dpi": _TSPL["dpi"],
            "invert_raster": _TSPL["invert_raster"],
            "max_label_width_mm": _TSPL["max_label_width_mm"],
            "max_label_height_mm": _TSPL["max_label_height_mm"],
            "print_program": _TSPL["print_program"],
            "status_program": _TSPL["status_program"],
            "status_flags": _TSPL["status_flags"],
            "label_size_program": _TSPL["label_size_program"],
            "label_size_parse": _TSPL["label_size_parse"],
            "notes": _TSPL["notes"],
        },
    )

    # Only fill a blank — an admin who has already set a language on a row knows better than we do.
    for slug, language in _LANGUAGES.items():
        if language:
            ThermalPrinterProfile.objects.filter(slug=slug, command_language="").update(command_language=language)

    ThermalPrinterProfile.objects.filter(slug="escpos-raster", name=_OLD_ESCPOS_NAME).update(name=_NEW_ESCPOS_NAME)


def unseed(apps, schema_editor):
    ThermalPrinterProfile = apps.get_model("auctions", "ThermalPrinterProfile")
    ThermalPrinterProfile.objects.filter(slug=_TSPL["slug"]).delete()
    for slug, language in _LANGUAGES.items():
        if language:
            ThermalPrinterProfile.objects.filter(slug=slug, command_language=language).update(command_language="")
    ThermalPrinterProfile.objects.filter(slug="escpos-raster", name=_NEW_ESCPOS_NAME).update(name=_OLD_ESCPOS_NAME)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0348_observedprinter_characterized_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
