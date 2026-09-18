"""
Custom middleware for the auctions application.
"""

from django.conf import settings


class ContentSecurityPolicyMiddleware:
    """Send ``settings.CONTENT_SECURITY_POLICY`` on every response that doesn't already have one.

    See the setting for what it deliberately leaves out. A response that sets its own header keeps
    it, so a page needing something looser can say so.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")
        if policy and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = policy
        return response


class ShortAnonymousSessionMiddleware:
    """Expire a session that never signed in after ``ANONYMOUS_SESSION_COOKIE_AGE``.

    ``SESSION_COOKIE_AGE`` is the signed-in lifetime. Anonymous sessions are the bulk of the table
    -- ``/api/pageview/`` creates one per beacon -- and keeping those for a year would mean
    ``clearsessions`` never reclaims anything.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        session = getattr(request, "session", None)
        user = getattr(request, "user", None)
        if session is None or getattr(user, "is_authenticated", False):
            return response
        # Only a session that already exists or is about to be written anyway. set_expiry() marks
        # the session modified, so touching an untouched one would *create* a row -- the opposite of
        # the point.
        if not session.session_key and not session.modified:
            return response
        short = getattr(settings, "ANONYMOUS_SESSION_COOKIE_AGE", None)
        if short and session.get_expiry_age() > short:
            session.set_expiry(short)
        return response


class MobileAppMiddleware:
    """Flag requests coming from the native mobile app's WebView.

    The app sets a ``FishAuctionsApp`` token in its User-Agent; templates read ``request.is_mobile_app``
    to drop web chrome (navbar, footer, install banners) the app renders natively. Cheap and
    unconditional, so it stays near the top of the stack.

    ``request.mobile_app_platform`` ("ios", "android" or "") comes from the same header, for the few
    places the two phones differ -- a Google Wallet button on an iPhone opens the system browser to do
    nothing useful. Empty outside the app, so a plain ``{% if %}`` on it is false for every web visitor.

    ``request.is_ios_app`` / ``request.is_android_app`` are the same fact as booleans, because templates
    can't compare a string without ``{% if x == "ios" %}`` noise. Tap to Pay needs them: "Tap to Pay on
    iPhone" is an Apple trademark that may only appear on iOS, so the copy branches on the platform
    rather than on being in the app.
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

        Scanning the whole header for "ios" is wrong in the direction that matters: the WebView's default
        User-Agent carries the device model, and an Android model can contain those three letters (a
        "Kiosk-…" handheld, and kiosk hardware is exactly what ends up on a check-in desk). That beats the
        ``; Android)`` the app wrote, and the phone is told it's an iPhone -- putting an Apple trademark on
        an Android screen.

        So look only from our token to the end of its parenthesised suffix: that segment is ours and holds
        nothing the device chose. Anything unreadable leaves the platform empty, which callers must treat
        as "unknown", never as a particular platform.
        """
        tail = user_agent[marker:]
        end = tail.find(")")
        token = (tail if end == -1 else tail[: end + 1]).lower()
        if "ios" in token:
            return "ios"
        if "android" in token:
            return "android"
        return ""
