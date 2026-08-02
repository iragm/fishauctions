"""
Custom middleware for the auctions application.
"""


class MobileAppMiddleware:
    """Flag requests coming from the native mobile app's WebView.

    The app sets a ``FishAuctionsApp`` token in its User-Agent; templates read
    ``request.is_mobile_app`` to drop web chrome (navbar, footer, install banners) that the
    app renders natively. Cheap and unconditional, so it stays near the top of the stack.

    ``request.mobile_app_platform`` ("ios", "android" or "") comes from the same header
    (``FishAuctionsApp/1.0 (Flutter; iOS)``), for the few places where the two phones genuinely
    differ — a Google Wallet button on an iPhone opens the system browser to do nothing useful.
    Empty outside the app, so a plain ``{% if %}`` on it is false for every web visitor.

    ``request.is_ios_app`` / ``request.is_android_app`` are the same fact as booleans, because
    templates can't compare a string without ``{% if x == "ios" %}`` noise. Tap to Pay needs them:
    Apple's review guide fixes the button's wording per platform ("Tap to Pay on iPhone" is a
    trademark and must not appear on an Android phone), so the copy branches on the platform, not
    just on being in the app.
    """

    #: The token the app appends to the WebView's default User-Agent, e.g.
    #: ``Mozilla/5.0 (iPhone; ...) FishAuctionsApp/1.0 (Flutter; iOS)``.
    APP_TOKEN = "FishAuctionsApp"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        marker = user_agent.find(self.APP_TOKEN)
        request.is_mobile_app = marker != -1
        platform = self._platform(user_agent, marker) if request.is_mobile_app else ""
        request.mobile_app_platform = platform
        request.is_ios_app = platform == "ios"
        request.is_android_app = platform == "android"
        return self.get_response(request)

    @classmethod
    def _platform(cls, user_agent, marker):
        """Read the platform out of the app's *own* token, not the rest of the User-Agent.

        Scanning the whole header for "ios" is wrong, and wrong in the direction that matters: the
        WebView's default User-Agent carries the device model, and an Android device model can
        contain those three letters (a "Kiosk-…" handheld is the easy example, and kiosk hardware is
        exactly what ends up on an auction check-in desk). Matching that beats the ``; Android)``
        the app itself wrote, and the phone is then told it's an iPhone — which puts "Tap to Pay on
        iPhone", an Apple trademark that may only appear on iOS, on an Android screen.

        So look only from our token up to the end of its parenthesised suffix. That segment is ours
        and contains nothing the device chose. Anything we can't read leaves the platform empty,
        which callers must treat as "unknown", never as a particular platform.
        """
        tail = user_agent[marker:]
        end = tail.find(")")
        token = (tail if end == -1 else tail[: end + 1]).lower()
        if "ios" in token:
            return "ios"
        if "android" in token:
            return "android"
        return ""


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
