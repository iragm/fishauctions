"""Shared label-printing helpers.

The mismatch-warning matrix is here so ``/printing/`` and ``GET /api/mobile/labels/prefs/`` show
identical warnings from the same prefs; they are advisory and never block saving.

``plan_label`` measures text so ``label_template.html`` can lay out two fixed columns without
clipping (enforced by ``test_label_layout.py``): the left column is the lot number, QR code and the
short tags that fit whole; the right is the lot name, then the owner pinned to the bottom, with
species, custom fields, category and description filling what's left -- in a different priority
order when sold, since a sold label is read to collect a lot and an unsold one to sell it. A field
with nothing to say takes no space.
"""

import html
import math
import re

# Presets describing a thermal roll or a sheet of Avery-style labels.
THERMAL_PRESETS = frozenset({"thermal_sm", "thermal_very_sm"})
SHEET_PRESETS = frozenset({"sm", "lg"})

# One-line facts a label can carry, in print order. See split_label_tags for placement.
LABEL_TAG_FIELDS = (
    "quantity_label",
    "donation_label",
    "min_bid_label",
    "buy_now_label",
    "custom_checkbox_label",
    "custom_dropdown_label",
    "i_bred_this_fish_label",
    "auction_date",
)

# label_template.html sets this line-height, and split_label_tags counts lines with it.
LABEL_LINE_HEIGHT = 1.2

# DejaVu Serif advance widths for ASCII 32..126, in hundredths of an em. Non-ASCII counts as a full
# em, erring toward moving a tag right.
# fmt: off
_SERIF_WIDTHS = (
    32, 40, 46, 84, 64, 95, 89, 27, 39, 39, 50, 84, 32, 34, 32, 34, 64, 64, 64, 64, 64, 64, 64, 64,
    64, 64, 34, 34, 84, 84, 84, 54, 100, 72, 73, 77, 80, 73, 69, 80, 87, 40, 40, 75, 66, 102, 88, 82,
    67, 82, 75, 69, 67, 84, 72, 103, 71, 66, 69, 39, 34, 39, 84, 50, 50, 60, 64, 56, 64, 59, 37, 64,
    64, 32, 31, 61, 32, 95, 64, 60, 64, 64, 48, 51, 40, 64, 56, 86, 56, 56, 53, 64, 34, 64, 84,
)
# fmt: on
# Kerning and rounding slack: a tag that exactly fills the column goes right.
_FIT_SLACK = 0.97
# DejaVu Serif Bold, for the winner's name -- the one bold text plan_label fits.
# fmt: off
_SERIF_BOLD_WIDTHS = (
    35, 44, 52, 84, 70, 95, 90, 31, 47, 47, 52, 84, 35, 42, 35, 37, 70, 70, 70, 70, 70, 70, 70, 70,
    70, 70, 37, 37, 84, 84, 84, 59, 100, 78, 85, 80, 87, 76, 71, 85, 94, 47, 47, 87, 70, 111, 91, 87,
    75, 87, 83, 72, 74, 87, 78, 112, 78, 71, 73, 47, 37, 47, 84, 50, 50, 65, 70, 61, 70, 64, 43, 70,
    73, 38, 36, 69, 38, 106, 73, 67, 70, 70, 53, 56, 46, 73, 58, 86, 60, 58, 57, 64, 36, 64, 84,
)
# fmt: on


# The tag separator plan_label inserts; not ASCII, so its width is given here.
_OTHER_WIDTHS = {"·": 35}


def text_width_pt(text, font_size_pt, bold=False):
    """How wide *text* prints in the label face at *font_size_pt*, in points."""
    widths = _SERIF_BOLD_WIDTHS if bold else _SERIF_WIDTHS
    hundredths = sum(widths[ord(c) - 32] if 32 <= ord(c) < 127 else _OTHER_WIDTHS.get(c, 100) for c in str(text))
    return hundredths * font_size_pt / 100


def split_label_tags(values, *, width_pt, height_pt, font_size_pt):
    """Split one label's non-empty tag *values* into ``(left, right)``.

    A tag stays left if it fits on one line and a line is free. Order is kept, so a short tag after a
    long one can still take a free left line.
    """
    lines = int(height_pt // (font_size_pt * LABEL_LINE_HEIGHT))
    left, right = [], []
    for value in values:
        if len(left) < lines and text_width_pt(value, font_size_pt) <= width_pt * _FIT_SLACK:
            left.append(value)
        else:
            right.append(value)
    return left, right


def wrapped_lines(text, *, width_pt, font_size_pt, bold=False):
    """How many lines *text* takes in a column *width_pt* wide; a newline starts a new line.

    Breaks where WeasyPrint does -- at spaces, after "/" or "-", and inside an overlong word -- and
    where the two could differ, counts more lines rather than fewer.
    """
    lines = 0
    space = text_width_pt(" ", font_size_pt, bold)
    for paragraph in str(text).splitlines():
        used = None  # width taken on the current line; None before the paragraph's first word
        for word in paragraph.split():
            # Split after each "/" or "-", keeping the delimiter on the piece it ends.
            for index, piece in enumerate(p for p in re.split(r"(?<=[/-])", word) if p):
                gap = space if index == 0 else 0
                width = text_width_pt(piece, font_size_pt, bold)
                if used is not None and used + gap + width <= width_pt:
                    used += gap + width
                    continue
                # A new line, plus as many more as a piece wider than the column needs.
                extra = max(math.ceil(width / width_pt) - 1, 0)
                lines += 1 + extra
                used = width - extra * width_pt
        if used is None:
            lines += 1  # a blank line: <br><br> in a description
    return lines


def plan_label(label, *, print_fields, geometry):
    """Decide what goes where on one lot label, so ``label_template.html`` only draws it.

    *geometry* is the label view's context. Sets ``tags_left``/``tags_right``; ``name_lines``,
    ``tags_lines`` and ``location_lines``; ``species_line`` and ``species_is_scientific``; ``details``,
    the rest of the middle band that fits whole; and ``description_lines``. A count of 0 leaves the
    field off.
    """
    font = geometry["font_size"]
    small = geometry["description_font_size"]
    tag_font = geometry["tag_font_size"]
    right_width = (geometry["label_width"] - geometry["first_column_width"]) * 72

    def line(size):
        return size * LABEL_LINE_HEIGHT

    def block(text, size, bold=False):
        return wrapped_lines(text, width_pt=right_width, font_size_pt=size, bold=bold) * line(size)

    # An empty tag value must not hold a line a real one needs.
    tag_height = geometry["label_height"] * 72 - line(font)  # under the lot number...
    if "qr_code" in print_fields:
        tag_height -= geometry["qr_size"] * 72  # ...and the QR code
    tags = [value for value in (getattr(label, field) for field in LABEL_TAG_FIELDS if field in print_fields) if value]
    label.tags_left, label.tags_right = split_label_tags(
        tags, width_pt=geometry["first_column_width"] * 72, height_pt=tag_height, font_size_pt=tag_font
    )

    # The owner's name is measured first and never clipped; half a point off for rounding.
    budget = geometry["label_height"] * 72 - 0.5
    if label.sold:
        # "Winner:" is regular, but measuring it bold only errs toward a spare line.
        budget -= block(f"Winner: {label.winner_name}", font, bold=True)
    else:
        seller = f"Seller: {label.seller_name}" if "seller_name" in print_fields else ""
        email = label.seller_email if "seller_email" in print_fields and label.seller_email else ""
        # A long email prints at seller_email_font_size, an em ratio.
        email_font = font * (
            float(label.seller_email_font_size.removesuffix("em")) if label.seller_email_font_size else 1
        )
        if seller and email and text_width_pt(f"{seller} ", font) + text_width_pt(email, email_font) <= right_width:
            owner_lines = 1
        else:
            owner_lines = wrapped_lines(seller, width_pt=right_width, font_size_pt=font) + wrapped_lines(
                email, width_pt=right_width, font_size_pt=email_font
            )
        budget -= owner_lines * line(font)

    def needs(text, size, cap=None):
        lines = wrapped_lines(text, width_pt=right_width, font_size_pt=size) if text else 0
        return min(lines, cap) if cap else lines

    location = label.winner_location if label.sold and label.auction.multi_location else ""
    name_wanted = needs(label.lot_name if "lot_name" in print_fields else "", font, geometry["name_lines"])
    tags_wanted = needs(" · ".join(label.tags_right), tag_font)
    if label.sold:
        # Sold: read to get the lot to its winner, so name and location come before tags.
        priority = (
            ("name_lines", name_wanted, font),
            ("location_lines", needs(location, font), font),
            ("tags_lines", tags_wanted, tag_font),
        )
    else:
        # Unsold: the tags are what sells it, so a long name can't push them off.
        priority = (
            ("name_lines", name_wanted, font),
            ("tags_lines", tags_wanted, tag_font),
            ("location_lines", 0, font),
        )
    given = dict.fromkeys((key for key, _, _ in priority), 0)
    # First two get one line each before either gets a second.
    for key, wanted, size in priority[:2]:
        if wanted and budget >= line(size):
            given[key] = 1
            budget -= line(size)
    # Then each grows back in the same order; the third takes what is left.
    for key, wanted, size in priority:
        while given[key] < wanted and budget >= line(size):
            given[key] += 1
            budget -= line(size)
    for key, value in given.items():
        setattr(label, key, value)
    if given["tags_lines"] < tags_wanted:
        # Tags are the top of the middle band: if they're cut, nothing below prints, and dropping
        # "Min: $25" while keeping the category would make the category look more important.
        budget = 0

    # The name the seller did *not* type -- see Lot.scientific_name_line.
    label.species_line, label.species_is_scientific = "", False
    if "scientific_name" in print_fields:
        if label.scientific_name_line:
            label.species_line, label.species_is_scientific = label.scientific_name_line, True
        elif label.common_name_line:
            label.species_line = label.common_name_line

    middle_band = (
        ("species", label.species_line, small),
        ("custom_field", label.custom_field_1 if "custom_field_1" in print_fields else "", small),
        ("category", str(label.category) if "category" in print_fields and label.category else "", font),
    )
    label.details = []
    for key, text, size in middle_band:
        if not text:
            continue
        needed = block(text, size)
        if needed > budget:
            budget = 0  # clipped from the bottom: nothing after the first field that doesn't fit
            break
        label.details.append(key)
        budget -= needed

    label.description_lines = 0
    if "description_label" in print_fields and budget > 0:
        # description_label keeps only <br> from the seller's HTML.
        text = html.unescape(re.sub(r"<[^>]*>", "", re.sub(r"<br\s*/?>", "\n", label.description_label)))
        if text.strip():
            description_lines = wrapped_lines(text, width_pt=right_width, font_size_pt=small)
            label.description_lines = min(int(budget // line(small)), description_lines)


WARNING_SHEET_METHOD_THERMAL_SIZE = (
    "Your label size is a thermal roll. Regular printers usually take letter/A4 label sheets — "
    "pick a sheet preset like Avery 18262, or switch the print method to Bluetooth."
)
WARNING_BLUETOOTH_SHEET_SIZE = (
    "Avery sheet presets won't fit a thermal label printer. Pick a thermal preset (or Custom matching your roll)."
)
WARNING_BLUETOOTH_TOO_LARGE = "No supported Bluetooth printer takes labels this large."

_MM_PER_UNIT = {"in": 25.4, "cm": 10.0}


def inches_per_unit(unit):
    """One *unit* in inches. Custom sizes are saved in ``UserLabelPrefs.unit`` and the templates write
    inches; both label views convert through this.
    """
    return _MM_PER_UNIT.get(unit, 25.4) / 25.4


def _label_size_mm(prefs):
    """The saved custom label size in millimetres, or ``None`` if it can't be determined."""
    factor = _MM_PER_UNIT.get(prefs.unit)
    if not factor or prefs.label_width is None or prefs.label_height is None:
        return None
    return prefs.label_width * factor, prefs.label_height * factor


def _fits_any_enabled_profile(width_mm, height_mm):
    """True if any enabled printer profile with declared dimensions can take this label.

    True (don't warn) when no enabled profile declares limits, since we can't prove it won't fit.
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
    # No profile declared a limit, so we can't say it won't fit.
    return not saw_limit


def deterministic_warnings(method, preset):
    """Warnings that depend only on (method, preset) -- the cells the live JS map can reproduce."""
    warnings = []
    if method in ("pdf", "system") and preset in THERMAL_PRESETS:
        warnings.append(WARNING_SHEET_METHOD_THERMAL_SIZE)
    if method == "bluetooth" and preset in SHEET_PRESETS:
        warnings.append(WARNING_BLUETOOTH_SHEET_SIZE)
    return warnings


def label_prefs_warnings(prefs):
    """The mismatch warnings for a :class:`UserLabelPrefs`.

    Server-side, so the copy and rules change without an app release and the web page and mobile API
    agree.
    """
    warnings = deterministic_warnings(prefs.print_method, prefs.preset)

    if prefs.print_method == "bluetooth" and prefs.preset == "custom":
        size = _label_size_mm(prefs)
        if size is not None and not _fits_any_enabled_profile(*size):
            warnings.append(WARNING_BLUETOOTH_TOO_LARGE)

    return warnings


def warning_matrix():
    """A ``{"method|preset": [warnings]}`` map ``/printing/`` embeds, so the dropdown re-renders warnings
    without a round trip (the custom-too-large cell still needs the server).
    """
    from auctions.models import UserLabelPrefs

    methods = [m[0] for m in UserLabelPrefs.PRINT_METHODS]
    presets = [p[0] for p in UserLabelPrefs.PRESETS]
    return {f"{method}|{preset}": deterministic_warnings(method, preset) for method in methods for preset in presets}
