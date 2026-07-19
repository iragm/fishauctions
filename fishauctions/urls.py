from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("auctions.urls")),
    path("api/mobile/", include("auctions.mobile.urls")),
    path("summernote/", include("django_summernote.urls")),
    path("admin/", admin.site.urls),
    path("", include("allauth.urls")),
    # path("__debug__/", include(debug_toolbar.urls)),
]

# Same pages Django would serve by default, but a failed render logs the real
# traceback instead of Django silently falling back (see auctions/error_views.py).
handler404 = "auctions.error_views.error_404"
handler500 = "auctions.error_views.error_500"
