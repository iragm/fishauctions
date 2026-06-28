from django.urls import path

from .views import (
    MobileCommandPaletteLogView,
    MobileCommandPaletteView,
    MobileDeviceRegisterView,
    MobileGoogleAuthView,
    MobileLoginView,
    MobileLotLabelView,
    MobilePaymentConfirmView,
    MobilePaymentCreateView,
    MobileTokenRefreshView,
    MobileUserMeView,
    MobileWebSessionConsumeView,
    MobileWebSessionView,
)

urlpatterns = [
    # Auth
    path("auth/login/", MobileLoginView.as_view(), name="mobile-auth-login"),
    path("auth/google/", MobileGoogleAuthView.as_view(), name="mobile-auth-google"),
    path("auth/refresh/", MobileTokenRefreshView.as_view(), name="mobile-auth-refresh"),
    path("auth/me/", MobileUserMeView.as_view(), name="mobile-auth-me"),
    # Pre-authenticate the WebView from the native JWT session (one-time handoff token).
    path("auth/web-session/", MobileWebSessionView.as_view(), name="mobile-auth-web-session"),
    path(
        "auth/web-session/consume/",
        MobileWebSessionConsumeView.as_view(),
        name="mobile-auth-web-session-consume",
    ),
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
