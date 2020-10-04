from django.conf import settings
from django.urls import include, path
from django.contrib import admin
from . import views
from django.contrib.auth.decorators import login_required

urlpatterns = [
    path('api/watchitem/<int:pk>/', login_required(views.watchOrUnwatch)),
    path('api/species/', login_required(views.getSpecies)),
    path('lots/', views.allLots.as_view(), name='allLots'),
    path('lots/<int:pk>/', views.viewAndBidOnLot.as_view()),
    path('lots/edit/<int:pk>/', login_required(views.LotUpdate.as_view())),
    path('lots/delete/<int:pk>/', views.LotDelete.as_view()),
    path('lots/new/', views.createLot, name='createLot'),
    path('lots/watched/', login_required(views.myWatched.as_view())),
    path('lots/won/', login_required(views.myWonLots.as_view())),
    path('lots/my/', login_required(views.myLots.as_view())),
    path('lots/all/', views.allLots.as_view(), name='allLots'),
    path('bids/', login_required(views.myBids.as_view())),
    path('', views.toDefaultLandingPage),
    path('about/', views.aboutSite, name='about'),
    path('account/', views.toAccount),
    path('invoices/', login_required(views.invoices.as_view())),
    path('invoices/<int:pk>/', login_required(views.invoice.as_view())),
    path('auctions/all/', views.allAuctions.as_view(), name='auctions'),
    path('auctions/new/', views.createAuction, name='createAuction'),
    path('auctions/edit/<slug:slug>/', views.AuctionUpdate.as_view()),
    path('auctions/<slug:slug>/', views.auction.as_view()),
    path('users/<int:pk>/', login_required(views.UserView.as_view())),
    path('users/edit/<int:pk>/', login_required(views.UserUpdate.as_view())),
]