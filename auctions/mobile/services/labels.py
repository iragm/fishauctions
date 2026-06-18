import logging

from .label_renderers import get_renderer, supported_formats

logger = logging.getLogger(__name__)


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
    def render_label(lot, fmt=None) -> tuple[bytes, str]:
        """Render *lot*'s label in ``fmt`` (default PNG).

        Returns ``(content_bytes, content_type)``. Raises ``ValueError`` for an unsupported format.
        """
        renderer = get_renderer(fmt)
        if renderer is None:
            msg = f"Unsupported label format {fmt!r}. Supported: {', '.join(supported_formats())}."
            raise ValueError(msg)
        label_data = LabelService.build_label_data(lot)
        return renderer.render(label_data), renderer.content_type
