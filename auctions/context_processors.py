"""Values every template needs and no view should have to remember to pass.

Each of these runs on every render, so the expensive ones are wrapped in ``once_per_request``, and
the ones that write to the session only write when the value they store actually changes -- an
unconditional write here is a ``django_session`` UPDATE for every page anybody loads.
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

# Google One Tap is drawn once a visitor has this many page loads behind them, and always on the
# pages named below. See google_one_tap() for why it is rationed at all.
ONE_TAP_MIN_PRIOR_PAGE_VIEWS = 1
ONE_TAP_PAGE_VIEW_SESSION_KEY = "page_views_before_one_tap"
ONE_TAP_ALWAYS_SHOWN_ON = frozenset({"account_login", "account_signup"})
# Pages the prompt is never drawn on, however much browsing is behind the visitor: it lands on top
# of something it cannot share the screen with -- the club map, the promo page's video, a long FAQ,
# the terms somebody is reading, and the two big browse lists where it reads as noise rather than an
# offer. Keyed on the view rather than the URL name, so a second path onto the same page is covered.
ONE_TAP_NEVER_SHOWN_ON = frozenset({"AllAuctions", "AllLots", "ClubMap", "FAQ", "PromoSite", "UserAgreement"})
CRAWLER_USER_AGENTS = ("Googlebot", "Baiduspider")


def once_per_request(processor):
    """Run a context processor once per request, however many templates are rendered.

    Django binds the processors to each new ``RequestContext``, so a view that renders a partial as
    well as its page (an HTMx table, an el-pagination page, a rendered-to-string email preview) runs
    every one of these again -- and these ones query. The answer cannot change inside a request, so
    it is remembered on the request itself.
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
    """Return value if it's a known IANA tz name, else None.

    The user_timezone cookie is client-controllable and userdata.timezone is
    a free-text CharField. An invalid value would otherwise blow up
    `{% timezone user_timezone %}` in base.html and 500 every page (Django
    ticket #33674).
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
        # Master on/off switch for all ads, controlled by the SHOW_ADS env var.
        "show_ads": settings.SHOW_ADS,
    }


def google_oauth(request):
    """Which social sign-in buttons the web login/signup pages should draw.

    Each provider is independent: a deployment can configure any subset, and the pages fall back to
    the password form when none are set. Apple and Facebook need the *web* half of their config —
    Apple's Services ID (the native app's bundle id doesn't work for the browser redirect) and
    Facebook's app id and secret — so a mobile-only configuration correctly shows no web button.
    """
    return {
        "GOOGLE_OAUTH_LINK": (settings.GOOGLE_OAUTH_LINK or "").strip(),
        "GOOGLE_LOGIN_ENABLED": _google_login_enabled(),
        # The web Apple flow also needs the team key to build its client secret; without it the
        # redirect reaches Apple and fails there, so treat it as not configured.
        "APPLE_LOGIN_ENABLED": bool(
            settings.APPLE_SIGN_IN_SERVICES_ID and settings.APPLE_SIGN_IN_PRIVATE_KEY and settings.APPLE_SIGN_IN_KEY_ID
        ),
        "FACEBOOK_LOGIN_ENABLED": bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
    }


def _google_login_enabled():
    """Whether this deployment has a real Google client id, rather than one of .env.example's."""
    token = (settings.GOOGLE_OAUTH_LINK or "").strip()
    return bool(token) and token not in GOOGLE_OAUTH_PLACEHOLDER_VALUES


def _view_class_name(resolver_match):
    """The class behind a resolved URL, or "" for a function view."""
    view_class = getattr(resolver_match.func, "view_class", None)
    return view_class.__name__ if view_class is not None else ""


def _is_page_load(request):
    """A whole page the visitor asked for, rather than a fragment of one.

    HTMx re-renders partials against the same URL several times per page; counting those would let
    one page look like a browsing session and open the prompt on the visitor's first screen, which
    is the thing google_one_tap() exists to prevent.
    """
    if request.method != "GET" or getattr(request, "htmx", False):
        return False
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return not any(crawler in user_agent for crawler in CRAWLER_USER_AGENTS)


@once_per_request
def google_one_tap(request):
    """Whether to draw Google's One Tap prompt on this page.

    One Tap is a budget, not a banner. Closing it sets Google's ``g_state`` cookie for the whole
    origin and starts an escalating cooldown -- hours, then days, then weeks -- and under FedCM the
    browser runs a quiet period of its own that no callback reports back to us. A prompt spent on
    somebody who was about to leave is a prompt they do not get on the page where they meant to
    sign up, and nothing in the page can tell that it was spent.

    So it is rationed on intent: withheld until a visitor has a page load behind them, and always
    drawn on sign-in and sign-up, where it cannot be wasted and where the button in
    `account/login.html` and `account/signup.html` is there anyway if it has been.

    Layout still overrides intent. ONE_TAP_NEVER_SHOWN_ON is the list of pages a floating prompt
    cannot share the screen with, and it wins over everything below it -- but those pages still
    count, because reading the FAQ or working down the lot list is exactly the browsing the gate is
    trying to detect. Suppressing the prompt there is not the same as pretending the visit did not
    happen.

    Counting stops at the threshold, because past it the answer cannot change again: a visitor
    costs one extra session write, once, and returning visitors cost none.
    """
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if user is None or session is None:
        # An error page rendered off a request that never reached the auth and session middleware.
        # There is nobody to prompt, and raising here would replace the error with a worse one.
        return {"SHOW_GOOGLE_ONE_TAP": False}
    if user.is_authenticated:
        return {"SHOW_GOOGLE_ONE_TAP": False}
    if getattr(request, "is_mobile_app", False):
        # The app has its own native Google flow (MobileSocialAuthView), and Google's script does
        # not run in an embedded WebView regardless.
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
    """
    Add timezone cookie - example: 'America/New_York'
    This is set via js with Intl.DateTimeFormat().resolvedOptions().timeZone
    """
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
                # user_timezone_set = True # don't set this to true, we want to make it current with js
    return {"user_timezone": user_timezone, "user_timezone_set": user_timezone_set}


def add_location(request):
    """request location if not set"""
    # Set a value so the session gets a key -- PageView identifies anonymous visitors by it.
    # Only when it is missing: assigning it unconditionally marks the session modified on every
    # request, which is a django_session UPDATE for every page anybody loads, signed in or not.
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
        # UserData is auto-created when user is saved
        # No cookies?  No worries - we'll get the IP address and get the location from that later - see set_user_location.py
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0]
        else:
            ip = request.META.get("REMOTE_ADDR")
        # Only update if IP address has changed
        if request.user.userdata.last_ip_address != ip:
            request.user.userdata.last_ip_address = ip
            needs_save = True

        # if cookie exists, save into userdata
        # we don't set the cookie from userdata, it only goes the other way
        if latitude_cookie and longitude_cookie:
            # Only update if values have changed
            # Convert cookie strings to float for comparison to handle precision
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
        # Whether this deployment has a registered DMCA agent to publish. False hides the footer
        # and menu links, because /dmca/ 404s without one -- see auctions/dmca.py.
        "dmca_configured": dmca.is_configured(),
        # When the whole site is one club, the club name duplicates the navbar
        # brand, so templates can hide it.
        "single_club_mode": getattr(settings, "SINGLE_CLUB_MODE", False),
        # Natural-language command palette. False (no LLM configured, or this user hasn't opted
        # in) means the palette renders exactly as it did before the feature existed -- no
        # microphone, no assist.
        "palette_assist_enabled": _palette_assist_enabled(request),
    }


def _palette_assist_enabled(request):
    """Whether the command palette should offer natural-language/voice commands to this user."""
    from auctions.palette_assist import assist_enabled_for

    return assist_enabled_for(getattr(request, "user", None))


@once_per_request
def label_print_method(request):
    """Expose the user's saved label print method so per-lot print buttons can pick a target.

    In the mobile app, a "bluetooth" method makes the per-lot print button emit a
    fishauctions://print/<pk> deep link (native Bluetooth printing) instead of the web label PDF.
    Defaults to "pdf" for anonymous users and users who've never set label preferences.
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

        # One query through the membership rows, rather than a values_list of club ids and then a
        # second query for the clubs themselves.
        clubs = list(
            Club.objects.filter(members__user=request.user, members__is_deleted=False).order_by("name").distinct()
        )
        return {"user_clubs": clubs}
    return {"user_clubs": []}


def account_nav(request):
    """The Account setup sidebar, on the pages that are part of it and nowhere else.

    `base.html` needs the answer before it lays the row out (the sidebar is a column beside the
    content, exactly as the club sidebar is), which is why this is a context processor rather than
    an inclusion tag: a tag can render the menu but cannot tell the template whether there is one.

    It also records the visit, so /account/setup/ can send somebody back where they were. That is a
    write in a render path, which the `add_location` processor above already does; `remember()`
    only touches the session when the value actually changes, and only for a GET, so a form post
    that re-renders with errors can't rewrite it.
    """
    from auctions import account_nav as nav

    active = nav.active_page(request)
    if not active:
        return {"account_nav_active": None, "account_nav_groups": []}
    if request.method == "GET":
        nav.remember(request, active)
    return {"account_nav_active": active, "account_nav_groups": nav.groups_for(request.user, active)}
