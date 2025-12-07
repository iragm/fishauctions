from django.conf import settings  # import the settings file


def google_analytics(request):
    """Return google tracking codes from settings"""
    return {
        "GOOGLE_MEASUREMENT_ID": settings.GOOGLE_MEASUREMENT_ID,
        "GOOGLE_TAG_ID": settings.GOOGLE_TAG_ID,
        "GOOGLE_ADSENSE_ID": settings.GOOGLE_ADSENSE_ID,
    }


def google_oauth(request):
    return {"GOOGLE_OAUTH_LINK": settings.GOOGLE_OAUTH_LINK}


def theme(request):
    """return the theme from userdata"""
    theme = True  # dark
    show_ads = True
    if request.user.is_authenticated:
        # UserData is auto-created when user is saved
        show_ads = request.user.userdata.show_ads
    # ads off for everyone!  (at least for now...we made $46 in a year from google ads, what a joke!)
    show_ads = False
    return {"theme": theme, "show_ads": show_ads}


def add_tz(request):
    """
    Add timezone cookie - example: 'America/New_York'
    This is set via js with Intl.DateTimeFormat().resolvedOptions().timeZone
    """
    user_timezone = ""
    user_timezone_set = False
    cookie_timezone = request.COOKIES.get("user_timezone")
    if cookie_timezone:
        user_timezone = cookie_timezone
        user_timezone_set = True
    if not user_timezone:
        user_timezone = "America/New_York"  # default timezone if not set
        if request.user.is_authenticated:
            # UserData is auto-created when user is saved
            if request.user.userdata.timezone:
                user_timezone = request.user.userdata.timezone
                # user_timezone_set = True # don't set this to true, we want to make it current with js
    return {"user_timezone": user_timezone, "user_timezone_set": user_timezone_set}


def add_location(request):
    """request location if not set"""
    # set some value to generate the session id
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
        
        timezone_cookie = request.COOKIES.get("user_timezone")
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


def site_config(request):
    return {
        "navbar_brand": settings.NAVBAR_BRAND,
        "copyright_message": settings.COPYRIGHT_MESSAGE,
        "enable_club_finder": settings.ENABLE_CLUB_FINDER,
        "enable_help": settings.ENABLE_HELP,
        "enable_promo_page": settings.ENABLE_PROMO_PAGE,
    }
