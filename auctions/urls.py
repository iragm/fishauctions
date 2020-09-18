from django.conf import settings
from django.urls import include, path
from django.contrib import admin
from . import views
#from auctions import views
#from wagtail.admin import urls as wagtailadmin_urls
#from wagtail.core import urls as wagtail_urls
#from wagtail.documents import urls as wagtaildocs_urls
from django.contrib.auth.decorators import login_required
#from search import views as search_views

urlpatterns = [
    path('admin/', admin.site.urls),

    #path('admin/', include(wagtailadmin_urls)),
    #path('documents/', include(wagtaildocs_urls)),

    #path('search/', search_views.search, name='search'),
    path('watchitem/<int:pk>/', login_required(views.watchOrUnwatch)),
    path('lots/', views.allLots.as_view(), name='allLots'),
    path('lots/<int:pk>/', login_required(views.viewAndBidOnLot.as_view())),
    path('lots/new/', views.createLot, name='createLot'),
    path('lots/watched/', login_required(views.myWatched.as_view())),
    path('lots/won/', login_required(views.myWonLots.as_view())),
    path('bids/', login_required(views.myBids.as_view())),
    path('lots/my/', login_required(views.myLots.as_view())),
    path('all_lots/', views.allLots.as_view(), name='allLots'),
    path('lots/all/', views.allLots.as_view(), name='allLots'),
    #path('submit_bid/', views.bidView),
    path('', views.toAllLots),
    #path('/', views.landingPage, name='landingPage'),
    path('about/', views.aboutAuction, name='aboutAuction'),
    path('invoice/', login_required(views.invoice.as_view())),
]