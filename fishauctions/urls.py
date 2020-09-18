
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('', include('auctions.urls')),
    path('admin/', admin.site.urls),
    path('', include('allauth.urls')),
]

# from django.conf import settings
# from django.urls import include, path
# from django.contrib import admin
# #from . import views
# #from auctions import views
# #from wagtail.admin import urls as wagtailadmin_urls
# #from wagtail.core import urls as wagtail_urls
# #from wagtail.documents import urls as wagtaildocs_urls
# from django.contrib.auth.decorators import login_required
# #from search import views as search_views

# urlpatterns = [
#     path('django-admin/', admin.site.urls),

#     #path('admin/', include(wagtailadmin_urls)),
#     #path('documents/', include(wagtaildocs_urls)),

#     #path('search/', search_views.search, name='search'),
#     path('watchitem/<int:pk>/', login_required(views.watchOrUnwatch)),
#     path('lots/', views.allLots.as_view(), name='allLots'),
#     path('lots/<int:pk>/', login_required(views.viewAndBidOnLot.as_view())),
#     path('lots/new/', views.createLot, name='createLot'),
#     path('lots/watched/', login_required(views.myWatched.as_view())),
#     path('lots/won/', login_required(views.myWonLots.as_view())),
#     path('bids/', login_required(views.myBids.as_view())),
#     path('lots/my/', login_required(views.myLots.as_view())),
#     path('all_lots/', views.allLots.as_view(), name='allLots'),
#     path('lots/all/', views.allLots.as_view(), name='allLots'),
#     #path('submit_bid/', views.bidView),
#     path('', views.toAllLots),
#     #path('/', views.landingPage, name='landingPage'),
#     path('about/', views.aboutAuction, name='aboutAuction'),
#     path('invoice/', login_required(views.invoice.as_view())),
# ]

# if settings.DEBUG:
#     from django.conf.urls.static import static
#     from django.contrib.staticfiles.urls import staticfiles_urlpatterns

#     # Serve static and media files from development server
#     urlpatterns += staticfiles_urlpatterns()
#     urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# urlpatterns = urlpatterns + [
#     # For anything not caught by a more specific rule above, hand over to
#     # Wagtail's page serving mechanism. This should be the last pattern in
#     # the list:
#     path('', include('allauth.urls')),
#     path("", include(wagtail_urls)),

#     # Alternatively, if you want Wagtail pages to be served from a subpath
#     # of your site, rather than the site root:
#     #    path("pages/", include(wagtail_urls)),
# ]
