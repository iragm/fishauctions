from django.conf import settings # import the settings file
from auctions.models import UserData

def google_analytics(request):
    """Return google tracking codes from settings"""
    return {'GOOGLE_MEASUREMENT_ID': settings.GOOGLE_MEASUREMENT_ID, 'GOOGLE_TAG_ID': settings.GOOGLE_TAG_ID}

def theme(request):
    """return the theme from userdata"""
    theme = True # dark
    if request.user.is_authenticated:
        userData, created = UserData.objects.get_or_create(
            user = request.user,
            defaults={},
        )
        theme = userData.use_dark_theme
    return {'theme': theme}

def add_location(request):
    """request location if not set"""
    has_user_location = False
    try:
        if request.COOKIES['latitude'] and request.COOKIES['longitude']:
            has_user_location = True
    except:
        pass
    if request.user.is_authenticated:
        userData, created = UserData.objects.get_or_create(
            user = request.user,
            defaults={},
        )
        # if cookie exists, save into userdata
        # we don't set the cookie from userdata, it only goes the other way
        try:
            if request.COOKIES['latitude'] and request.COOKIES['longitude']:
                userData.latitude = request.COOKIES['latitude']
                userData.longitude = request.COOKIES['longitude']
                userData.save()
        except:
            pass
    return {'has_user_location': has_user_location}

def dismissed_cookies_tos(request):
    """return True to hide cookie banner, False to show it"""
    hide_tos_banner = False # show by default
    try:
        if request.COOKIES['hide_tos_banner']:
            hide_tos_banner = True
    except:
        pass
    if request.user.is_authenticated:
        userData, created = UserData.objects.get_or_create(
            user = request.user,
            defaults={},
        )
        if userData.dismissed_cookies_tos:
            hide_tos_banner = True
        else:
            try:
                if request.COOKIES['hide_tos_banner']:
                    userData.dismissed_cookies_tos = True
                    userData.save()
            except:
                pass
    return {'hide_tos_banner': hide_tos_banner}