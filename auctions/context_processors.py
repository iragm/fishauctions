from django.conf import settings # import the settings file

def google_analytics(request):
    # return the value you want as a dictionnary. you may add multiple values in there.
    return {'GOOGLE_MEASUREMENT_ID': settings.GOOGLE_MEASUREMENT_ID, 'GOOGLE_TAG_ID': settings.GOOGLE_TAG_ID}