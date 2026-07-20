from django.urls import path

from .views import (
    MobileArLotsView,
    MobileArObservationsView,
    MobileArPositionsView,
    MobileCheckinJoinView,
    MobileCheckinPingView,
    MobileCheckinSetLocationView,
    MobileCommandPaletteLogView,
    MobileCommandPaletteView,
    MobileConfigView,
    MobileDeviceRegisterView,
    MobileDeviceUnregisterView,
    MobileGoogleAuthView,
    MobileLabelPrefsView,
    MobileLoginView,
    MobileLotLabelView,
    MobileLotWatchView,
    MobileMyClubsView,
    MobileOfflineSnapshotView,
    MobileOfflineSyncView,
    MobilePaymentConfirmView,
    MobilePaymentCreateView,
    MobilePrinterProfilesView,
    MobileTokenRefreshView,
    MobileUserMeView,
    MobileWebSessionConsumeView,
    MobileWebSessionView,
)

urlpatterns = [
    # Public config (no auth) — read before sign-in
    path("config/", MobileConfigView.as_view(), name="mobile-config"),
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
    # Clubs
    path("clubs/mine/", MobileMyClubsView.as_view(), name="mobile-clubs-mine"),
    # Devices
    path("devices/register/", MobileDeviceRegisterView.as_view(), name="mobile-device-register"),
    path("devices/unregister/", MobileDeviceUnregisterView.as_view(), name="mobile-device-unregister"),
    # Printers
    path("printers/profiles/", MobilePrinterProfilesView.as_view(), name="mobile-printer-profiles"),
    # Labels
    path("labels/prefs/", MobileLabelPrefsView.as_view(), name="mobile-label-prefs"),
    path("labels/<int:pk>/", MobileLotLabelView.as_view(), name="mobile-label-lot"),
    # Lots
    path("lots/<int:pk>/watch/", MobileLotWatchView.as_view(), name="mobile-lot-watch"),
    # Payments
    path("payments/create/", MobilePaymentCreateView.as_view(), name="mobile-payment-create"),
    path("payments/confirm/", MobilePaymentConfirmView.as_view(), name="mobile-payment-confirm"),
    # Command palette
    path("command-palette/", MobileCommandPaletteView.as_view(), name="mobile-command-palette"),
    path("command-palette/log/", MobileCommandPaletteLogView.as_view(), name="mobile-command-palette-log"),
    # AR lot scanning
    path("ar/lots/", MobileArLotsView.as_view(), name="mobile-ar-lots"),
    path("ar/observations/", MobileArObservationsView.as_view(), name="mobile-ar-observations"),
    path("ar/positions/", MobileArPositionsView.as_view(), name="mobile-ar-positions"),
    # Proximity check-in & welcome
    path("checkin/ping/", MobileCheckinPingView.as_view(), name="mobile-checkin-ping"),
    path("checkin/join/", MobileCheckinJoinView.as_view(), name="mobile-checkin-join"),
    path("checkin/set-location/", MobileCheckinSetLocationView.as_view(), name="mobile-checkin-set-location"),
    # Offline mode (in-person sale)
    path("offline/snapshot/", MobileOfflineSnapshotView.as_view(), name="mobile-offline-snapshot"),
    path("offline/sync/", MobileOfflineSyncView.as_view(), name="mobile-offline-sync"),
]
