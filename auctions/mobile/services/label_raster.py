"""Rasterize the label PDF so the Bluetooth PNG *is* the PDF.

The PNG the app sends to a thermal printer used to be drawn independently, in Pillow, from a
hand-rolled layout: a Code128 barcode where the PDF puts a QR code, different fields, different
typography, and no idea that ``Auction.label_print_fields`` or the user's ``UserLabelPrefs``
existed. Two implementations of "a lot label" drift the moment either is touched, and only one of
them was the one anybody had tuned.

So there is one layout now. WeasyPrint renders the same ``label_template.html`` the PDF uses, at
the same size, and pdfium rasterizes page one to the pixel grid the printer wants. Changing a label
means changing the template, once.

That layout costs about 110 ms a label and it is the same 110 ms every time the same label is
printed, so the result is cached on a hash of the label's own HTML -- see :func:`_cache_key`, which
is the part of this module worth reading before changing anything. :func:`render_lot_labels_png`
renders a whole run in one call for the batch endpoint; what that saves is round trips, not CPU.
"""

import hashlib
import io
import logging
import time

from django.core.cache import cache
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# How long a rendered PNG is kept. The key is a hash of the label's own HTML, so an edit to the lot,
# the auction's print fields or the user's label prefs produces a different key rather than a stale
# hit -- which is what lets the timeout be generous. What it buys: a reprint, a retry after a paper
# jam, and "print unprinted labels" over a batch mostly printed already all cost a cache read
# instead of a WeasyPrint render.
PNG_CACHE_SECONDS = 60 * 60 * 24
# Bumped by hand if the raster pipeline itself changes shape (different pdfium settings, a different
# fit rule) without the HTML changing.
PNG_CACHE_VERSION = 1

# A batch renders until one of these two runs out, whichever comes first, and hands the rest back for
# the caller to ask for again. The count keeps any one request small; the budget keeps a *slow*
# request small, which is the same promise stated in seconds -- a phone waiting on labels it is
# about to print serially wants the first few now, not all forty in a minute.
MAX_LABELS_PER_BATCH = 25
BATCH_TIME_BUDGET_SECONDS = 3.0


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


def _cache_key(html, *, width, height, dpi):
    """The cache key for a label: a hash of the HTML it renders from, plus the raster geometry.

    The HTML is the honest key. Everything that can change what comes out of the printer -- the lot
    name, the winner, the species row behind it, the auction's ``label_print_fields``, every field
    of the user's ``UserLabelPrefs`` -- reaches the PDF *through* this HTML and nothing else does, so
    a key derived from it cannot go stale and cannot collide across users who happen to share a lot.
    Rendering it costs about 4 ms against the ~100 ms of WeasyPrint and pdfium it stands in front of.
    """
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return f"label_png:{PNG_CACHE_VERSION}:{digest}:{width}x{height}@{dpi}"


def render_lot_label_png(lot, request, *, width, height, dpi):
    """The lot's label as a PNG that matches its PDF exactly, or ``None`` if it can't be produced.

    ``None`` (never an exception) means the caller should fall back to the standalone renderer:
    a lot with no auction has no label config to render against, and a deployment that hasn't
    installed pypdfium2 yet should still print *something*.

    Cached on the rendered HTML (see :func:`_cache_key`), so printing the same label twice --
    a reprint, a retry after a jam, the overlap between "print all" and "print unprinted" -- is a
    cache read rather than a second render.
    """
    from .label_pdf import build_label_view, render_view_pdf

    try:
        # single_label_page so a sheet preset renders as one label rather than a label in the corner
        # of a blank page; mark_printed=False because nothing has printed yet — the app posts
        # labels/printed/ for what actually comes out.
        view = build_label_view(lot, request, single_label_page=True, mark_printed=False)
        context = view.get_context_data()
        # Not necessarily byte-identical to what django_weasyprint renders (it adds its own base URL
        # and stylesheet handling), and it does not need to be: this is a fingerprint of the inputs,
        # not a copy of the output.
        html = render_to_string(view.template_name, context, request=view.request)
        key = _cache_key(html, width=width, height=height, dpi=dpi)
        cached = cache.get(key)
        if cached is not None:
            return cached
        png = rasterize_pdf(render_view_pdf(view, context), width=width, height=height, dpi=dpi)
        cache.set(key, png, PNG_CACHE_SECONDS)
        return png
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


def render_lot_labels_png(lots, request, *, width, height, dpi):
    """Render as many of *lots* as fit in one batch. Returns ``(rendered, remaining)``.

    ``rendered`` is a list of ``(lot, png_bytes_or_None)`` in the order given; ``remaining`` is the
    lots this batch did not get to, for the caller to ask for again. ``None`` for a lot means the
    same thing it means in :func:`render_lot_label_png` -- no label PDF could be produced -- and the
    endpoint turns those into the standalone renderer's fallback drawing.

    Batching is about **round trips, not CPU**. Measured on this codebase a label costs ~4 ms of
    template, ~95 ms of WeasyPrint and ~10 ms of pdfium, and rendering forty labels as one
    forty-page PDF saves about a fifth of that -- worth having, but nothing like the cost of forty
    separate HTTPS requests from a phone on hall wifi, each with its own TLS, auth, throttle and
    scheduling. So the labels are rendered one at a time (which is what makes each one individually
    cacheable) and it is the *request* that is batched.
    """
    lots = list(lots)
    rendered = []
    started = time.monotonic()
    for index, lot in enumerate(lots[:MAX_LABELS_PER_BATCH]):
        # Always render the first one: a budget that can return nothing is a loop that never ends.
        if index and time.monotonic() - started > BATCH_TIME_BUDGET_SECONDS:
            return rendered, lots[index:]
        rendered.append((lot, render_lot_label_png(lot, request, width=width, height=height, dpi=dpi)))
    return rendered, lots[len(rendered) :]
