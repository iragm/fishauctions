
from django.contrib import admin
from django.urls import include, path
import debug_toolbar
from django.conf import settings

urlpatterns = [
    path('', include('auctions.urls')),
    path('admin/', admin.site.urls),
    path('', include('allauth.urls')),
    path('__debug__/', include(debug_toolbar.urls)),
]