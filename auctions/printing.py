"""Shared label-printing helpers.

The mismatch-warning matrix lives here so the ``/printing/`` template and the mobile prefs API
(``GET /api/mobile/labels/prefs/``) surface exactly the same warnings from the same saved prefs.
Warnings are advisory — they never block saving.
"""

# Presets that describe a thermal label roll vs. a sheet of Avery-style labels.
THERMAL_PRESETS = frozenset({"thermal_sm", "thermal_very_sm"})
SHEET_PRESETS = frozenset({"sm", "lg"})

WARNING_SHEET_METHOD_THERMAL_SIZE = (
    "Your label size is a thermal roll. Regular printers usually take letter/A4 label sheets — "
    "pick a sheet preset like Avery 18262, or switch the print method to Bluetooth."
)
WARNING_BLUETOOTH_SHEET_SIZE = (
    "Avery sheet presets won't fit a thermal label printer. Pick a thermal preset (or Custom matching your roll)."
)
WARNING_BLUETOOTH_TOO_LARGE = "No supported Bluetooth printer takes labels this large."

_MM_PER_UNIT = {"in": 25.4, "cm": 10.0}


def _label_size_mm(prefs):
    """The saved custom label size in millimetres, or ``None`` if it can't be determined."""
    factor = _MM_PER_UNIT.get(prefs.unit)
    if not factor or prefs.label_width is None or prefs.label_height is None:
        return None
    return prefs.label_width * factor, prefs.label_height * factor


def _fits_any_enabled_profile(width_mm, height_mm):
    """True if any enabled printer profile with declared max dimensions can take this label.

    Returns True (don't warn) when no enabled profile declares limits, since we can't prove it
    won't fit.
    """
    from auctions.models import ThermalPrinterProfile

    profiles = ThermalPrinterProfile.objects.filter(enabled=True).exclude(
        max_label_width_mm__isnull=True, max_label_height_mm__isnull=True
    )
    saw_limit = False
    for profile in profiles:
        max_w = profile.max_label_width_mm
        max_h = profile.max_label_height_mm
        if max_w is None and max_h is None:
            continue
        saw_limit = True
        if (max_w is None or width_mm <= max_w) and (max_h is None or height_mm <= max_h):
            return True
    # No profile declared a limit → we can't say it won't fit, so don't warn.
    return not saw_limit


def deterministic_warnings(method, preset):
    """Warnings that depend only on (method, preset) — the cells the live JS map can reproduce."""
    warnings = []
    if method in ("pdf", "system") and preset in THERMAL_PRESETS:
        warnings.append(WARNING_SHEET_METHOD_THERMAL_SIZE)
    if method == "bluetooth" and preset in SHEET_PRESETS:
        warnings.append(WARNING_BLUETOOTH_SHEET_SIZE)
    return warnings


def label_prefs_warnings(prefs):
    """Return the list of mismatch warnings for a :class:`UserLabelPrefs` instance.

    Server-side so the copy and rules iterate without an app release; both the web page and the
    mobile prefs API call this so the two always agree.
    """
    warnings = deterministic_warnings(prefs.print_method, prefs.preset)

    if prefs.print_method == "bluetooth" and prefs.preset == "custom":
        size = _label_size_mm(prefs)
        if size is not None and not _fits_any_enabled_profile(*size):
            warnings.append(WARNING_BLUETOOTH_TOO_LARGE)

    return warnings


def warning_matrix():
    """A ``{"method|preset": [warnings]}`` map the ``/printing/`` page embeds so the dropdown can
    re-render warnings live without a round-trip (the custom-too-large cell still needs the server)."""
    from auctions.models import UserLabelPrefs

    methods = [m[0] for m in UserLabelPrefs.PRINT_METHODS]
    presets = [p[0] for p in UserLabelPrefs.PRESETS]
    return {f"{method}|{preset}": deterministic_warnings(method, preset) for method in methods for preset in presets}
