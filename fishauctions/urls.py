import debug_toolbar
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("auctions.urls")),
    path("summernote/", include("django_summernote.urls")),
    path("admin/", admin.site.urls),
    path("", include("allauth.urls")),
    path("__debug__/", include(debug_toolbar.urls)),
]
