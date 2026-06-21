import logging
import re

from .label_renderers import get_renderer, supported_formats

logger = logging.getLogger(__name__)

# Output sizing. ``resolution`` and ``dpi`` are caller-supplied GET params; these are the defaults
# and the clamps that keep a public-facing endpoint from being asked to allocate a huge bitmap.
DEFAULT_WIDTH = 600
DEFAULT_HEIGHT = 400
DEFAULT_DPI = 203
MIN_DIMENSION = 16
MAX_DIMENSION = 4000
MIN_DPI = 36
MAX_DPI = 1200

_RESOLUTION_RE = re.compile(r"\s*(\d{1,5})\s*[xX×]\s*(\d{1,5})\s*\Z")


class LabelService:
    """Builds label data for auction lots and renders it to a printable image.

    Rendering is delegated to a pluggable :class:`LabelRenderer` (see ``label_renderers``), so this
    service produces images only — never TSPL / ZPL / ESC/POS printer commands. The mobile app sends
    the returned image straight to the Bluetooth printer.
    """

    @staticmethod
    def build_label_data(lot) -> dict:
        """Human-readable fields a renderer lays out onto a lot's label."""
        seller_name = (f"{lot.user.first_name} {lot.user.last_name}".strip() if lot.user else None) or (
            lot.user.username if lot.user else "Unknown"
        )
        buy_now = str(lot.buy_now_price) if lot.buy_now_price is not None else None

        return {
            "lot_number": str(lot.lot_number_display),
            "title": lot.lot_name,
            "quantity": lot.quantity,
            "minimum_bid": str(lot.reserve_price),
            "buy_now_price": buy_now,
            "seller": seller_name,
            "auction": lot.auction.title if lot.auction else None,
            "category": lot.species_category.name if lot.species_category else None,
            "i_bred_this_fish": lot.i_bred_this_fish,
            "custom_field_1": lot.custom_field_1 or None,
        }

    @staticmethod
    def parse_dimensions(resolution=None, dpi=None) -> tuple[int, int, int]:
        """Turn the ``resolution`` ("WIDTHxHEIGHT") and ``dpi`` GET params into validated ints.

        Defaults to 600x400 at 203 dpi. Raises ``ValueError`` on malformed or out-of-range input.
        """
        width, height = DEFAULT_WIDTH, DEFAULT_HEIGHT
        if resolution:
            match = _RESOLUTION_RE.match(resolution)
            if not match:
                msg = f"Invalid resolution {resolution!r}; expected WIDTHxHEIGHT, e.g. 600x400."
                raise ValueError(msg)
            width, height = int(match.group(1)), int(match.group(2))
        if not (MIN_DIMENSION <= width <= MAX_DIMENSION and MIN_DIMENSION <= height <= MAX_DIMENSION):
            msg = f"Resolution out of range; each dimension must be {MIN_DIMENSION}-{MAX_DIMENSION}px."
            raise ValueError(msg)

        dpi_value = DEFAULT_DPI
        if dpi:
            try:
                dpi_value = int(dpi)
            except (TypeError, ValueError):
                msg = f"Invalid dpi {dpi!r}; expected an integer."
                raise ValueError(msg) from None
            if not (MIN_DPI <= dpi_value <= MAX_DPI):
                msg = f"dpi out of range; must be {MIN_DPI}-{MAX_DPI}."
                raise ValueError(msg)
        return width, height, dpi_value

    @staticmethod
    def render_label(lot, fmt=None, *, resolution=None, dpi=None) -> tuple[bytes, str]:
        """Render *lot*'s label in ``fmt`` (default PNG) at the requested ``resolution``/``dpi``.

        ``resolution`` is a ``"WIDTHxHEIGHT"`` string and ``dpi`` an integer (both default to
        600x400 @ 203dpi). Returns ``(content_bytes, content_type)``. Raises ``ValueError`` for an
        unsupported format or malformed resolution/dpi.
        """
        renderer = get_renderer(fmt)
        if renderer is None:
            msg = f"Unsupported label format {fmt!r}. Supported: {', '.join(supported_formats())}."
            raise ValueError(msg)
        width, height, dpi_value = LabelService.parse_dimensions(resolution, dpi)
        label_data = LabelService.build_label_data(lot)
        return renderer.render(label_data, width=width, height=height, dpi=dpi_value), renderer.content_type
