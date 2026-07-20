"""
Custom middleware for the auctions application.
"""

from django.db import close_old_connections


class RecycleStaleDBConnections:
    """Health-check/recycle the DB connection at the start of every request.

    Django keeps a persistent DB connection per worker thread (``CONN_MAX_AGE``)
    and is meant to recycle or health-check it via ``close_old_connections``,
    which it wires to the ``request_started``/``request_finished`` signals. Under
    ASGI (gunicorn+uvicorn) our *sync* views run in asgiref's thread-sensitive
    executor thread, where the connection lives -- but those signals fire in the
    event-loop context, so the signal-driven cleanup does NOT reliably reach that
    connection. It then persists, goes idle past MariaDB's ``wait_timeout`` (8h
    default) overnight, and the next request to reuse it raises
    ``OperationalError (2006, 'Server has gone away')``. This is exactly the
    regression that appeared after the move to ASGI: ``CONN_MAX_AGE=0`` no longer
    closed connections because the close ran in the wrong context.

    Calling ``close_old_connections`` from a sync middleware runs it in the same
    thread-sensitive context as the view's ORM queries, so the stale/dead
    connection is recycled (via ``CONN_HEALTH_CHECKS`` and ``CONN_MAX_AGE``)
    before any query runs. Healthy connections within ``CONN_MAX_AGE`` are left
    in place and reused -- the performance win of persistent connections; only
    obsolete or dead ones are dropped. Keep this a plain sync middleware: the
    context guarantee is the whole point, so it must not be made async.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        close_old_connections()
        return self.get_response(request)


class MobileAppMiddleware:
    """Flag requests coming from the native mobile app's WebView.

    The app sets a ``FishAuctionsApp`` token in its User-Agent; templates read
    ``request.is_mobile_app`` to drop web chrome (navbar, footer, install banners) that the
    app renders natively. Cheap and unconditional, so it stays near the top of the stack.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_mobile_app = "FishAuctionsApp" in request.META.get("HTTP_USER_AGENT", "")
        return self.get_response(request)


class CrossOriginIsolationMiddleware:
    """
    Middleware to add Cross-Origin-Opener-Policy and Cross-Origin-Embedder-Policy headers.

    These headers are required for SharedArrayBuffer and WebAssembly.Memory serialization,
    which are needed by the Vosklet voice recognition library.

    Only applies to pages that need WebAssembly (voice recognition for lot winner selection).
    Other pages don't get these headers to allow YouTube embeds and other third-party content.

    More info: https://web.dev/coop-coep/
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only apply strict COEP headers to pages that need WebAssembly/voice recognition
        # This allows YouTube embeds and other third-party iframes to work on other pages
        if self._needs_cross_origin_isolation(request):
            # Add COOP header - allows popups while isolating the browsing context
            # Using 'same-origin' would be more secure but breaks OAuth popups
            response["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"

            # Add COEP header - requires all resources to be loaded with CORS or same-origin
            response["Cross-Origin-Embedder-Policy"] = "require-corp"
            response["Cross-Origin-Resource-Policy"] = "cross-origin"

        return response

    def _needs_cross_origin_isolation(self, request):
        """
        Check if the current request path needs cross-origin isolation.
        Only the dynamic lot winner page needs it for voice recognition.
        """
        # Voice recognition is used on the dynamic set lot winner page
        # Use endswith to avoid matching related paths like /lots/set-winners/undo/
        return request.path.endswith("/lots/set-winners/")
