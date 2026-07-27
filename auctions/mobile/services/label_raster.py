"""Rasterize the label PDF so the Bluetooth PNG *is* the PDF.

The PNG the app sends to a thermal printer used to be drawn independently, in Pillow, from a
hand-rolled layout: a Code128 barcode where the PDF puts a QR code, different fields, different
typography, and no idea that ``Auction.label_print_fields`` or the user's ``UserLabelPrefs``
existed. Two implementations of "a lot label" drift the moment either is touched, and only one of
them was the one anybody had tuned.

So there is one layout now. WeasyPrint renders the same ``label_template.html`` the PDF uses, at
the same size, and pdfium rasterizes page one to the pixel grid the printer wants. Changing a label
means changing the template, once.
"""

import io
import logging

logger = logging.getLogger(__name__)


def rasterize_pdf(pdf_bytes, *, width, height, dpi):
    """Render page one of *pdf_bytes* into a ``width`` x ``height`` PNG at ``dpi``.

    The page is scaled to fit without distortion and centred on white, so a printer whose label
    aspect ratio doesn't quite match the user's page setup gets even margins rather than stretched
    text. Returns PNG bytes.
    """
    import pypdfium2
    from PIL import Image

    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page = pdf[0]
        # pdfium works in points; `scale` is output pixels per point.
        page_width, page_height = page.get_width(), page.get_height()
        if page_width <= 0 or page_height <= 0:
            # RuntimeError, not ValueError: the caller treats ValueError as the expected "this lot
            # has no label PDF" miss, and a degenerate page is a genuine fault worth logging loudly.
            msg = f"PDF page has no size ({page_width}x{page_height})"
            raise RuntimeError(msg)
        scale = min(width / page_width, height / page_height)
        rendered = page.render(scale=scale, draw_annots=False).to_pil().convert("RGB")
    finally:
        pdf.close()

    if rendered.size != (width, height):
        canvas = Image.new("RGB", (width, height), "white")
        canvas.paste(rendered, ((width - rendered.width) // 2, (height - rendered.height) // 2))
        rendered = canvas

    buffer = io.BytesIO()
    rendered.save(buffer, format="PNG", dpi=(dpi, dpi))
    return buffer.getvalue()


def render_lot_label_png(lot, request, *, width, height, dpi):
    """The lot's label as a PNG that matches its PDF exactly, or ``None`` if it can't be produced.

    ``None`` (never an exception) means the caller should fall back to the standalone renderer:
    a lot with no auction has no label config to render against, and a deployment that hasn't
    installed pypdfium2 yet should still print *something*.
    """
    from .label_pdf import render_single_lot_pdf

    try:
        # single_label_page so a sheet preset renders as one label rather than a label in the corner
        # of a blank page; mark_printed=False because nothing has printed yet — the app posts
        # labels/printed/ for what actually comes out.
        pdf_bytes, _ = render_single_lot_pdf(lot, request, single_label_page=True, mark_printed=False)
        return rasterize_pdf(pdf_bytes, width=width, height=height, dpi=dpi)
    except ValueError:
        # The expected miss: a lot with no auction has no label configuration to render against.
        logger.info("Lot %s has no label PDF to rasterize; drawing a fallback label.", getattr(lot, "pk", None))
        return None
    except Exception:
        # Anything else is a real fault. Still degrade rather than 500 — somebody is standing at a
        # check-in table with a queue behind them and an approximate label beats no label — but log
        # it as an error so it isn't quietly tolerated forever.
        logger.exception(
            "Could not rasterize the label PDF for lot %s; falling back to the standalone renderer.",
            getattr(lot, "pk", None),
        )
        return None
