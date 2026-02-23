"""
Custom middleware for the auctions application.
"""


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
