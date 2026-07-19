"""Error handlers that surface otherwise-swallowed tracebacks.

When rendering the 404 page itself raises, Django's ``get_exception_response()``
falls back to the 500 page WITHOUT logging the exception -- admins then get a
traceback-less "Report at /path" / "Internal Server Error: /path" email from the
top-level handler and the real cause is lost (django/core/handlers/exception.py
sends ``got_request_exception`` but never logs). These wrappers log the real
exception before letting Django's fallback proceed.
"""

import logging

from django.http import HttpResponseServerError
from django.views import defaults

logger = logging.getLogger("auctions.errorpages")


def error_404(request, exception=None):
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
