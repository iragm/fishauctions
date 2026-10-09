"""Error handlers that surface otherwise-swallowed tracebacks.

When rendering the 404 page itself raises, Django's ``get_exception_response()``
falls back to the 500 page WITHOUT logging the exception -- admins then get a
traceback-less "Report at /path" / "Internal Server Error: /path" email from the
top-level handler and the real cause is lost (django/core/handlers/exception.py
sends ``got_request_exception`` but never logs). These wrappers log the real
exception before letting Django's fallback proceed.

A 404 for anything other than a page (``/favicon.ico``, a missing image or script) is plain text.
The 404 page renders ``base.html`` and so ``{{ csrf_token }}``; for a first-time visitor, whose
favicon request goes out before the page's own response has set a cookie, that set a second
``csrftoken`` over the one the page was rendered with, and every POST from the page then 403'd.
"""

import logging

from django.http import HttpResponseNotFound, HttpResponseServerError
from django.views import defaults

logger = logging.getLogger("auctions.errorpages")

# Sec-Fetch-Dest values for which a person sees the response as a page.
PAGE_DESTINATIONS = {"document", "iframe", "frame", "embed", "object"}


def wants_page(request):
    """False for a browser's subresource request (an image, a script, a fetch), which nobody reads as a page."""
    destination = request.headers.get("Sec-Fetch-Dest")
    if destination:
        return destination in PAGE_DESTINATIONS
    # Browsers too old to send Sec-Fetch-Dest still ask for an icon with an image-only Accept.
    return not request.headers.get("Accept", "").startswith("image/")


def error_404(request, exception=None):
    if not wants_page(request):
        return HttpResponseNotFound("Not Found", content_type="text/plain")
    try:
        return defaults.page_not_found(request, exception)
    except Exception:
        logger.exception("404 page render failed for %s; Django will fall back to the 500 page", request.path)
        # Re-raise so get_exception_response() still serves the 500 page.
        raise


def error_500(request):
    try:
        return defaults.server_error(request)
    except Exception:
        logger.exception("500 page render failed for %s; serving plain-text fallback", request.path)
        # Raising here would leave the ASGI handler to emit its own bare
        # "Internal Server Error" -- return the same thing but with the cause logged.
        return HttpResponseServerError("Internal Server Error", content_type="text/plain")
