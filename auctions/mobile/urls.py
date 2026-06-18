from django.urls import path

from .views import (
    MobileCommandPaletteLogView,
    MobileCommandPaletteView,
    MobileDeviceRegisterView,
    MobileLoginView,
    MobileLotLabelView,
    MobilePaymentConfirmView,
    MobilePaymentCreateView,
    MobileTokenRefreshView,
    MobileUserMeView,
)

urlpatterns = [
    # Auth
    path("auth/login/", MobileLoginView.as_view(), name="mobile-auth-login"),
    path("auth/refresh/", MobileTokenRefreshView.as_view(), name="mobile-auth-refresh"),
    path("auth/me/", MobileUserMeView.as_view(), name="mobile-auth-me"),
    # Devices
    path("devices/register/", MobileDeviceRegisterView.as_view(), name="mobile-device-register"),
    # Labels
    path("labels/<int:pk>/", MobileLotLabelView.as_view(), name="mobile-label-lot"),
    # Payments
    path("payments/create/", MobilePaymentCreateView.as_view(), name="mobile-payment-create"),
    path("payments/confirm/", MobilePaymentConfirmView.as_view(), name="mobile-payment-confirm"),
    # Command palette
    path("command-palette/", MobileCommandPaletteView.as_view(), name="mobile-command-palette"),
    path("command-palette/log/", MobileCommandPaletteLogView.as_view(), name="mobile-command-palette-log"),
]
