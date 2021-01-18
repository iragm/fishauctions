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