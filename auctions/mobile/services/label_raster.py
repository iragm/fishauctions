"""Rasterize the label PDF, so the Bluetooth PNG *is* the PDF.

The PNG used to be drawn independently in Pillow from a hand-rolled layout -- a Code128 barcode
where the PDF puts a QR code, different fields and typography, and no knowledge of
``Auction.label_print_fields`` or ``UserLabelPrefs``. Two implementations of "a lot label" drift the
moment either is touched.

So WeasyPrint renders the same ``label_template.html`` at the same size and pdfium rasterizes page
one. Changing a label means changing the template, once.

That costs about 110 ms a label, every time the same label prints, so the result is cached on a hash
of the label's HTML (:func:`_cache_key`). :func:`render_lot_labels_png` renders a run in one call
for the batch endpoint, which saves round trips rather than CPU.
"""

import hashlib
import io
import logging
import time

from django.core.cache import cache
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# How long a rendered PNG is kept. The key is a hash of the label's HTML, so an edit produces a
# different key rather than a stale hit, which is what lets the timeout be generous: a reprint, a
# retry after a jam, and the overlap between "print all" and "print unprinted" all become cache reads.
PNG_CACHE_SECONDS = 60 * 60 * 24
# Bumped by hand if the raster pipeline changes shape without the HTML changing.
PNG_CACHE_VERSION = 1

# A batch renders until one of these runs out and hands the rest back. The count keeps a request
# small; the budget keeps a slow request small -- a phone printing serially wants the first few now.
MAX_LABELS_PER_BATCH = 25
BATCH_TIME_BUDGET_SECONDS = 3.0


def rasterize_pdf(pdf_bytes, *, width, height, dpi):
    """Render page one of *pdf_bytes* into a ``width`` x ``height`` PNG at ``dpi``.

    Scaled to fit without distortion and centred on white, so a mismatched aspect ratio gets even
    margins rather than stretched text.
    """
    import pypdfium2
    from PIL import Image

    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page = pdf[0]
        # pdfium works in points; `scale` is output pixels per point.
        page_width, page_height = page.get_width(), page.get_height()
        if page_width <= 0 or page_height <= 0:
            # RuntimeError, not ValueError: the caller treats ValueError as "no label PDF".
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


def _cache_key(html, *, width, height, dpi):
    """The cache key for a label: a hash of its HTML, plus the raster geometry.

    The HTML is the honest key -- the lot name, the winner, the species, the auction's
    ``label_print_fields`` and every field of ``UserLabelPrefs`` reach the PDF through it and nothing
    else does -- so the key can't go stale or collide. Hashing costs ~4 ms against ~100 ms of rendering.
    """
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return f"label_png:{PNG_CACHE_VERSION}:{digest}:{width}x{height}@{dpi}"


def render_lot_label_png(lot, request, *, width, height, dpi):
    """The lot's label as a PNG matching its PDF, or ``None`` if it can't be produced.

    ``None`` (never an exception) means fall back to the standalone renderer: a lot with no auction has
    no label config, and a deployment without pypdfium2 should still print something. Cached on the
    rendered HTML (:func:`_cache_key`).
    """
    from .label_pdf import build_label_view, render_view_pdf

    try:
        # single_label_page so a sheet preset renders one label rather than one in the corner of a
        # blank page; mark_printed=False because the app posts labels/printed/ for what comes out.
        view = build_label_view(lot, request, single_label_page=True, mark_printed=False)
        context = view.get_context_data()
        # Not byte-identical to what django_weasyprint renders, and it needn't be: this is a
        # fingerprint of the inputs, not a copy of the output.
        html = render_to_string(view.template_name, context, request=view.request)
        key = _cache_key(html, width=width, height=height, dpi=dpi)
        cached = cache.get(key)
        if cached is not None:
            return cached
        png = rasterize_pdf(render_view_pdf(view, context), width=width, height=height, dpi=dpi)
        cache.set(key, png, PNG_CACHE_SECONDS)
        return png
    except ValueError:
        # The expected miss: a lot with no auction has no label configuration.
        logger.info("Lot %s has no label PDF to rasterize; drawing a fallback label.", getattr(lot, "pk", None))
        return None
    except Exception:
        # A real fault. Still degrade rather than 500 -- an approximate label beats no label at a
        # check-in table -- but log it as an error.
        logger.exception(
            "Could not rasterize the label PDF for lot %s; falling back to the standalone renderer.",
            getattr(lot, "pk", None),
        )
        return None


def render_lot_labels_png(lots, request, *, width, height, dpi):
    """Render as many of *lots* as fit in one batch; returns ``(rendered, remaining)``.

    ``rendered`` is ``(lot, png_bytes_or_None)`` in the order given, where ``None`` means the same as in
    :func:`render_lot_label_png`.

    Batching is about round trips, not CPU: a label costs ~4 ms of template, ~95 ms of WeasyPrint and
    ~10 ms of pdfium, and one forty-page PDF would save about a fifth of that -- nothing like forty
    HTTPS requests from a phone on hall wifi. So labels render one at a time (which makes each
    cacheable) and the request is what's batched.
    """
    lots = list(lots)
    rendered = []
    started = time.monotonic()
    for index, lot in enumerate(lots[:MAX_LABELS_PER_BATCH]):
        # Always render the first: a budget that can return nothing is a loop that never ends.
        if index and time.monotonic() - started > BATCH_TIME_BUDGET_SECONDS:
            return rendered, lots[index:]
        rendered.append((lot, render_lot_label_png(lot, request, width=width, height=height, dpi=dpi)))
    return rendered, lots[len(rendered) :]
