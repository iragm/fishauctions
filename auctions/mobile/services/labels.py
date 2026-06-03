import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# Formats a future printing implementation may provide without changing the API contract.
SUPPORTED_LABEL_FORMATS = ["png", "pdf", "raw_commands"]


class LabelService:
    """Generates structured label payloads for auction lots.

    The service intentionally returns only data — it performs no Bluetooth,
    TSPL, ZPL, or ESC/POS work.  Future implementations should subclass or
    wrap this service to render the payload into a specific format
    (PNG / PDF / raw printer commands) without altering the API contract.
    """

    @staticmethod
    def get_lot_label_data(lot) -> dict:
        """Build a label payload for *lot*.

        Returns a dict with two top-level keys:
          ``label_data`` — human-readable fields for rendering.
          ``metadata``   — generation context consumed by the mobile client.
        """
        seller_name = (f"{lot.user.first_name} {lot.user.last_name}".strip() if lot.user else None) or (
            lot.user.username if lot.user else "Unknown"
        )

        buy_now = None
        if lot.buy_now_price is not None:
            buy_now = str(lot.buy_now_price)

        return {
            "label_data": {
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
            },
            "metadata": {
                "generated_at": timezone.now().isoformat(),
                "lot_pk": lot.pk,
                "supported_formats": SUPPORTED_LABEL_FORMATS,
            },
        }
