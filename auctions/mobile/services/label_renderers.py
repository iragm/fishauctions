"""Pluggable label rendering for the mobile app.

The mobile app prints lot labels over Bluetooth by sending the printer a rendered *image*: the
server owns layout/rendering so every platform prints identical labels and the app needs no font or
layout logic. This module deliberately produces images only — it does NOT emit printer command
languages (TSPL / ZPL / ESC/POS).

Renderers are pluggable: implement :class:`LabelRenderer` and register it in ``LABEL_RENDERERS`` to
add a new output format (e.g. a higher-DPI raster or a PDF renderer) without touching the view or
the service.
"""

import io
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class LabelRenderer(ABC):
    """Renders a label-data dict into the bytes of one concrete output format."""

    format: str = ""
    content_type: str = ""

    @abstractmethod
    def render(self, label_data: dict) -> bytes:
        """Return the encoded label (e.g. PNG bytes) for ``label_data``."""
        raise NotImplementedError


class PngLabelRenderer(LabelRenderer):
    """Render a lot label as a PNG with Pillow.

    Black-on-white raster sized for a typical thermal label: a large lot number, the title, seller /
    quantity / price, and a Code128 barcode of the lot number for scanning. Pillow's scalable default
    font is used, so no font files are required at deploy time.
    """

    format = "png"
    content_type = "image/png"

    WIDTH = 600
    HEIGHT = 400
    MARGIN = 18

    def render(self, label_data: dict) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        def font(size):
            return ImageFont.load_default(size=size)

        img = Image.new("RGB", (self.WIDTH, self.HEIGHT), "white")
        draw = ImageDraw.Draw(img)
        x = self.MARGIN
        y = self.MARGIN
        max_w = self.WIDTH - 2 * self.MARGIN

        lot_number = str(label_data.get("lot_number") or "")
        title = str(label_data.get("title") or "")
        seller = str(label_data.get("seller") or "")
        quantity = label_data.get("quantity")
        auction = label_data.get("auction")
        min_bid = label_data.get("minimum_bid")
        buy_now = label_data.get("buy_now_price")

        # Lot number — the most important thing on the label.
        draw.text((x, y), lot_number, fill="black", font=font(56))
        y += 66

        # Title, wrapped to the label width.
        y = self._draw_wrapped(draw, title, (x, y), font(30), max_w, line_height=34)
        y += 6

        # Seller + quantity.
        meta = seller
        if quantity:
            meta = f"{meta}  ·  Qty {quantity}" if meta else f"Qty {quantity}"
        if meta:
            y = self._draw_wrapped(draw, meta, (x, y), font(24), max_w, line_height=28)

        # Price.
        price_bits = []
        if min_bid not in (None, ""):
            price_bits.append(f"Min ${min_bid}")
        if buy_now not in (None, ""):
            price_bits.append(f"Buy ${buy_now}")
        if price_bits:
            draw.text((x, y), "   ".join(price_bits), fill="black", font=font(24))

        # Barcode of the lot number, pinned bottom-right (best-effort — never break the label over it).
        self._draw_barcode(img, lot_number)

        # Auction name, bottom-left.
        if auction:
            draw.text((x, self.HEIGHT - self.MARGIN - 18), str(auction), fill="black", font=font(18))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _draw_wrapped(draw, text, origin, font, max_width, line_height):
        x, y = origin
        if not text:
            return y
        line = ""
        for word in text.split():
            candidate = f"{line} {word}".strip()
            if not line or draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                draw.text((x, y), line, fill="black", font=font)
                y += line_height
                line = word
        if line:
            draw.text((x, y), line, fill="black", font=font)
            y += line_height
        return y

    def _draw_barcode(self, img, value):
        if not value:
            return
        try:
            import barcode
            from barcode.writer import ImageWriter
            from PIL import Image

            code = barcode.get("code128", value, writer=ImageWriter())
            buf = io.BytesIO()
            code.write(buf, options={"write_text": False, "module_height": 8.0, "quiet_zone": 1.0})
            buf.seek(0)
            bc = Image.open(buf).convert("RGB")
            target_w = self.WIDTH // 2
            bc = bc.resize((target_w, max(1, int(bc.height * (target_w / bc.width)))))
            img.paste(bc, (self.WIDTH - target_w - self.MARGIN, self.HEIGHT - bc.height - self.MARGIN))
        except Exception:
            logger.warning("Could not render barcode for label value %r", value, exc_info=True)


# Registry of available output formats. Add new renderers here to expose new formats.
LABEL_RENDERERS = {
    PngLabelRenderer.format: PngLabelRenderer(),
}
DEFAULT_LABEL_FORMAT = PngLabelRenderer.format


def get_renderer(fmt) -> LabelRenderer | None:
    """Return the renderer for ``fmt`` (or the default when blank/None); None if unsupported."""
    return LABEL_RENDERERS.get((fmt or DEFAULT_LABEL_FORMAT).lower())


def supported_formats() -> list[str]:
    return sorted(LABEL_RENDERERS.keys())
