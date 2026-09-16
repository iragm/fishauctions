"""Values every template needs and no view should have to pass.

These run on every render, so the expensive ones use ``once_per_request`` and the session-writing
ones only write when the value changes -- an unconditional write is a ``django_session`` UPDATE per
page load.
"""

import functools
import zoneinfo

from django.conf import settings  # import the settings file

from auctions import dmca

DEFAULT_USER_TIMEZONE = "America/New_York"
GOOGLE_OAUTH_PLACEHOLDER_VALUES = {
    "unsecure",
    "secret",
    "secret.apps.googleusercontent.com",
}

# One Tap is drawn after this many page loads, and always on the pages named below.
ONE_TAP_MIN_PRIOR_PAGE_VIEWS = 1
ONE_TAP_PAGE_VIEW_SESSION_KEY = "page_views_before_one_tap"
ONE_TAP_ALWAYS_SHOWN_ON = frozenset({"account_login", "account_signup"})
# Pages the prompt is never drawn on: it covers something it can't share the screen with. Keyed on
# the view, so a second path to the same page is covered.
ONE_TAP_NEVER_SHOWN_ON = frozenset({"AllAuctions", "AllLots", "ClubFinderView", "FAQ", "PromoSite", "UserAgreement"})
CRAWLER_USER_AGENTS = ("Googlebot", "Baiduspider")


def once_per_request(processor):
    """Run a context processor once per request, however many templates render.

    Django rebinds processors per ``RequestContext``, so a page rendering partials would re-run these
    queries. The answer can't change within a request.
    """

    attribute = f"_context_processor_{processor.__name__}"

    @functools.wraps(processor)
    def wrapper(request):
        cached = getattr(request, attribute, None)
        if cached is None:
            cached = processor(request)
            setattr(request, attribute, cached)
        return cached

    return wrapper


def _safe_timezone(value: str | None) -> str | None:
    """Return value if it's a known IANA timezone name, else None.

    The cookie is client-controlled and userdata.timezone is free text; an invalid value would 500
    every page in ``{% timezone user_timezone %}`` (Django ticket #33674).
    """
    if value and value in zoneinfo.available_timezones():
        return value
    return None


def google_analytics(request):
    """Return google tracking codes and the global ad switch from settings"""
    return {
        "GOOGLE_MEASUREMENT_ID": settings.GOOGLE_MEASUREMENT_ID,
        "GOOGLE_TAG_ID": settings.GOOGLE_TAG_ID,
        "GOOGLE_ADSENSE_ID": settings.GOOGLE_ADSENSE_ID,
        # Master switch for all ads.
        "show_ads": settings.SHOW_ADS,
    }


def google_oauth(request):
    """Which social sign-in buttons the login and signup pages draw.

    Each provider is independent. Apple and Facebook need their *web* config (Apple's Services ID, not
    the app's bundle id), so a mobile-only setup shows no web button.
    """
    return {
        "GOOGLE_OAUTH_LINK": (settings.GOOGLE_OAUTH_LINK or "").strip(),
        "GOOGLE_LOGIN_ENABLED": _google_login_enabled(),
        # Without the team key the redirect reaches Apple and fails there.
        "APPLE_LOGIN_ENABLED": bool(
            settings.APPLE_SIGN_IN_SERVICES_ID and settings.APPLE_SIGN_IN_PRIVATE_KEY and settings.APPLE_SIGN_IN_KEY_ID
        ),
        "FACEBOOK_LOGIN_ENABLED": bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
    }


def _google_login_enabled():
    """Whether this deployment has a real Google client id rather than .env.example's."""
    token = (settings.GOOGLE_OAUTH_LINK or "").strip()
    return bool(token) and token not in GOOGLE_OAUTH_PLACEHOLDER_VALUES


def _view_class_name(resolver_match):
    """The class behind a resolved URL, or "" for a function view."""
    view_class = getattr(resolver_match.func, "view_class", None)
    return view_class.__name__ if view_class is not None else ""


def _is_page_load(request):
    """A whole page the visitor asked for, rather than an HTMx fragment."""
    if request.method != "GET" or getattr(request, "htmx", False):
        return False
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return not any(crawler in user_agent for crawler in CRAWLER_USER_AGENTS)


@once_per_request
def google_one_tap(request):
    """Whether to draw Google's One Tap prompt on this page.

    A dismissal sets Google's ``g_state`` cookie and an escalating cooldown, so the prompt is rationed:
    withheld until a visitor has a page load behind them, and always drawn on sign-in and sign-up.
    ONE_TAP_NEVER_SHOWN_ON pages don't draw it but still count as browsing. Counting stops at the
    threshold, so returning visitors cost no session write.
    """
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if user is None or session is None:
        # An error page rendered without the auth and session middleware.
        return {"SHOW_GOOGLE_ONE_TAP": False}
    if user.is_authenticated:
        return {"SHOW_GOOGLE_ONE_TAP": False}
    if getattr(request, "is_mobile_app", False):
        # The app has its own native Google flow, and Google's script doesn't run in a WebView.
        return {"SHOW_GOOGLE_ONE_TAP": False}
    if not _google_login_enabled():
        return {"SHOW_GOOGLE_ONE_TAP": False}
    seen = session.get(ONE_TAP_PAGE_VIEW_SESSION_KEY, 0)
    if seen < ONE_TAP_MIN_PRIOR_PAGE_VIEWS and _is_page_load(request):
        session[ONE_TAP_PAGE_VIEW_SESSION_KEY] = seen + 1
    resolver_match = getattr(request, "resolver_match", None)
    if resolver_match is not None:
        if _view_class_name(resolver_match) in ONE_TAP_NEVER_SHOWN_ON:
            return {"SHOW_GOOGLE_ONE_TAP": False}
        if resolver_match.url_name in ONE_TAP_ALWAYS_SHOWN_ON:
            return {"SHOW_GOOGLE_ONE_TAP": True}
    return {"SHOW_GOOGLE_ONE_TAP": seen >= ONE_TAP_MIN_PRIOR_PAGE_VIEWS}


def theme(request):
    """return the theme from userdata"""
    theme = True  # dark
    return {"theme": theme}


def add_tz(request):
    """Add the timezone cookie (e.g. 'America/New_York'), set by JS from Intl.DateTimeFormat()."""
    user_timezone = ""
    user_timezone_set = False
    cookie_timezone = _safe_timezone(request.COOKIES.get("user_timezone"))
    if cookie_timezone:
        user_timezone = cookie_timezone
        user_timezone_set = True
    if not user_timezone:
        user_timezone = DEFAULT_USER_TIMEZONE
        if request.user.is_authenticated:
            # UserData is auto-created when user is saved
            saved = _safe_timezone(request.user.userdata.timezone)
            if saved:
                user_timezone = saved
    return {"user_timezone": user_timezone, "user_timezone_set": user_timezone_set}


def add_location(request):
    """request location if not set"""
    # Give the session a key, which PageView uses to identify anonymous visitors. Only when
    # missing: an unconditional assignment is a django_session UPDATE on every page load.
    if request.session.get("status") != "started":
        request.session["status"] = "started"
    has_user_location = False
    latitude_cookie = request.COOKIES.get("latitude")
    longitude_cookie = request.COOKIES.get("longitude")
    if latitude_cookie and longitude_cookie:
        has_user_location = True

    # Batch all user data updates into a single save operation
    needs_save = False
    if request.user.is_authenticated:
        # No cookies: the IP gives a location later, see set_user_location.py.
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0]
        else:
            ip = request.META.get("REMOTE_ADDR")
        # Only update if IP address has changed
        if request.user.userdata.last_ip_address != ip:
            request.user.userdata.last_ip_address = ip
            needs_save = True

        # The cookie is saved into userdata, never the other way.
        if latitude_cookie and longitude_cookie:
            # Compare as floats for precision.
            try:
                lat_float = float(latitude_cookie)
                lon_float = float(longitude_cookie)
                if request.user.userdata.latitude != lat_float or request.user.userdata.longitude != lon_float:
                    request.user.userdata.latitude = lat_float
                    request.user.userdata.longitude = lon_float
                    needs_save = True
            except (ValueError, TypeError):
                # Invalid cookie values, skip update
                pass

        timezone_cookie = _safe_timezone(request.COOKIES.get("user_timezone"))
        if timezone_cookie and request.user.userdata.timezone != timezone_cookie:
            request.user.userdata.timezone = timezone_cookie
            needs_save = True

        # Save only once if any changes were made
        if needs_save:
            request.user.userdata.save()

    return {"has_user_location": has_user_location}


def dismissed_cookies_tos(request):
    """return True to hide cookie banner, False to show it"""
    hide_tos_banner = False  # show by default
    hide_tos_cookie = request.COOKIES.get("hide_tos_banner")
    if hide_tos_cookie:
        hide_tos_banner = True
    if request.user.is_authenticated:
        # UserData is auto-created when user is saved
        if request.user.userdata.dismissed_cookies_tos:
            hide_tos_banner = True
        elif hide_tos_cookie:
            request.user.userdata.dismissed_cookies_tos = True
            request.user.userdata.save()
    return {"hide_tos_banner": hide_tos_banner}


@once_per_request
def site_config(request):
    return {
        "navbar_brand": settings.NAVBAR_BRAND,
        "copyright_message": settings.COPYRIGHT_MESSAGE,
        "show_footer_icon": settings.SHOW_FOOTER_ICON,
        "enable_club_finder": settings.ENABLE_CLUB_FINDER,
        "enable_help": settings.ENABLE_HELP,
        "enable_promo_page": settings.ENABLE_PROMO_PAGE,
        "recaptcha_enabled": getattr(settings, "RECAPTCHA_ENABLED", False),
        # Hides the footer and menu links, since /dmca/ 404s without one -- see auctions/dmca.py.
        "dmca_configured": dmca.is_configured(),
        # In single-club mode the club name duplicates the navbar brand.
        "single_club_mode": getattr(settings, "SINGLE_CLUB_MODE", False),
        # False (no LLM, or not opted in) means the palette renders as it did before the feature.
        "palette_assist_enabled": _palette_assist_enabled(request),
    }


def _palette_assist_enabled(request):
    """Whether the palette offers natural-language and voice commands to this user."""
    from auctions.palette_assist import assist_enabled_for

    return assist_enabled_for(getattr(request, "user", None))


@once_per_request
def label_print_method(request):
    """The user's saved label print method, so per-lot print buttons pick a target.

    In the app, "bluetooth" makes the button emit a fishauctions://print/<pk> deep link. Defaults to
    "pdf".
    """
    method = "pdf"
    if request.user.is_authenticated:
        from auctions.models import UserLabelPrefs

        method = (
            UserLabelPrefs.objects.filter(user=request.user).values_list("print_method", flat=True).first() or "pdf"
        )
    return {"user_print_method": method}


@once_per_request
def user_clubs(request):
    if request.user.is_authenticated:
        from auctions.models import Club

        # One query through the membership rows.
        clubs = list(
            Club.objects.filter(members__user=request.user, members__is_deleted=False).order_by("name").distinct()
        )
        return {"user_clubs": clubs}
    return {"user_clubs": []}


def account_nav(request):
    """The Account setup sidebar, on the pages that are part of it.

    A context processor rather than an inclusion tag because `base.html` needs to know whether there is
    a sidebar before laying the row out. It also records the visit so /account/setup/ can send somebody
    back; ``remember()`` only writes on a GET when the value changes.
    """
    from auctions import account_nav as nav

    active = nav.active_page(request)
    if not active:
        return {"account_nav_active": None, "account_nav_groups": []}
    if request.method == "GET":
        nav.remember(request, active)
    return {"account_nav_active": active, "account_nav_groups": nav.groups_for(request.user, active)}
