"""
Mobile API views.

All endpoints live under /api/mobile/ and require JWT Bearer authentication
(except auth/login and auth/refresh which issue / rotate tokens, and config/
which is public).

Config
------
GET /api/mobile/config/
    Public deployment config the app reads *before* sign-in to wire up the Square Mobile
    Payments SDK and Google Sign-In against the right deployment. Unauthenticated.

    PUBLIC VALUES ONLY — never add secrets here (see the view docstring).

    Response 200::

        {
          "square_application_id":   "sq0idp-xxxx",
          "square_environment":      "sandbox",   // or "production"
          "google_server_client_id": "xxxx.apps.googleusercontent.com",
          "brand_name":              "auction.fish"
        }

Authentication
--------------
POST /api/mobile/auth/login/
    Issue access + refresh tokens.

    Request::

        { "credential": "username_or_email", "password": "secret" }

    Response 200::

        {
          "access":  "<jwt>",
          "refresh": "<jwt>"
        }

    Response 401::

        { "detail": "Invalid credentials" }

POST /api/mobile/auth/google/
    Exchange a Google ID token (from the client-side Sign-In flow) for a JWT pair.
    Verifies the token against the configured Web OAuth client ID, rejects unverified
    emails, and finds or creates a local user linked to a Google SocialAccount.

    Request::

        { "id_token": "<google-id-token>" }

    Response 200::

        { "access": "<jwt>", "refresh": "<jwt>" }

    Response 401::

        { "detail": "Invalid ID token." }
        { "detail": "Google account email is not verified." }

POST /api/mobile/auth/refresh/
    Rotate a refresh token (old token is blacklisted).

    Request::

        { "refresh": "<jwt>" }

    Response 200::

        { "access": "<jwt>", "refresh": "<jwt>" }

GET /api/mobile/auth/me/
    Authenticated user profile (requires Bearer token).

    Response 200::

        {
          "id": 1,
          "username": "alice",
          "email": "alice@example.com",
          "first_name": "Alice",
          "last_name": "Smith",
          "is_staff": false,
          "date_joined": "2024-01-01T00:00:00Z"
        }

POST /api/mobile/auth/web-session/
    Pre-authenticate the WebView from the native JWT session (requires Bearer token). Mints a
    single-use, ~60s handoff token bound to the user and returns the URL the WebView should load as
    its initial request. No session is established here, and the session cookie never touches Dart.

    Response 200::

        { "handoff_url": "https://auction.fish/api/mobile/auth/web-session/consume/?t=<token>" }

GET /api/mobile/auth/web-session/consume/?t=<token>
    Loaded by the WebView itself (no Authorization header — the token is the credential). Atomically
    validates and burns the token, then logs the user into a real Django/allauth session: the
    sessionid cookie is set by the server on the redirect, keeping HttpOnly/Secure/SameSite. An
    optional ``next`` (same-host only) sets the redirect target; the default is the web home.

    Response 302 → ``next`` (default ``/``) on success, with ``Set-Cookie: sessionid=...``.
    Response 302 → ``/accounts/login/`` if the token is missing, expired, or already used (no session).

Devices
-------
POST /api/mobile/devices/register/
    Register or update a device record.

    Request::

        {
          "device_uuid": "550e8400-e29b-41d4-a716-446655440000",
          "device_name": "Alice's iPhone",
          "platform": "ios",
          "app_version": "1.0.0"
        }

    Response 200 (updated) / 201 (new)::

        {
          "id": 42,
          "device_uuid": "550e8400-e29b-41d4-a716-446655440000",
          "device_name": "Alice's iPhone",
          "platform": "ios",
          "app_version": "1.0.0",
          "created_at": "2024-01-01T00:00:00Z",
          "last_seen":  "2024-06-01T12:00:00Z"
        }

Clubs
-----
GET /api/mobile/clubs/mine/
    Clubs the authenticated user belongs to (same membership scoping as the web nav), sorted by
    name. ``url`` is the server-relative web club page for the WebView; ``icon_url`` is an absolute
    URL or null; ``is_admin`` is true when the user's membership has permission_admin.

    Response 200::

        {
          "clubs": [
            {
              "name": "My Club",
              "slug": "my-club",
              "url": "/clubs/my-club/",
              "icon_url": "https://auction.fish/media/club_icons/logo.png",
              "is_admin": true
            }
          ]
        }

Labels
------
GET /api/mobile/labels/<lot_pk>/?fmt=png&resolution=600x400&dpi=203
    Return the lot's label as a rendered image (default PNG) to send straight to a Bluetooth
    printer. The server owns layout/rendering; the app does not draw the label. ``fmt`` selects a
    registered renderer (currently ``png``); an unsupported format is a 400. (The param is ``fmt``,
    not ``format`` — DRF reserves ``?format=`` for content negotiation.) ``resolution``
    (``WIDTHxHEIGHT``, default ``600x400``) and ``dpi`` (default ``203``) size the raster: render at
    the printer's native width (e.g. a 96px-wide D11 label) so the app prints it 1:1 instead of
    downscaling a 600px image and smearing the embedded barcode. Bad resolution/dpi is a 400.

    Access is restricted to the lot's own seller or an admin of its auction (mirrors the web
    SingleLotLabelView). Others get 403; a missing/deleted lot is 404.

    Response 200:  binary image body with ``Content-Type: image/png``.

Payments
--------
The Flutter app uses Square's Mobile Payments SDK (Tap to Pay): it charges the card on-device
and returns a completed Square payment_id. There is no nonce and the server never calls
payments.create — confirm re-fetches the payment from Square and verifies it before recording.

Both endpoints are restricted to the merchant collecting payment — the auction creator, a
superuser, anyone with an is_admin AuctionTOS on the auction (so a Square auction needs no club),
or a club admin / money manager / auction manager for the invoice's club. The buyer is never
authorized: the device authorizes with the *seller's* Square account, so the access token must
not reach a buyer.

POST /api/mobile/payments/create/
    Validate an invoice and return the parameters needed to authorize the Mobile Payments SDK.
    The seller's OAuth access token is returned because the SDK authorizes on-device with
    authorize(accessToken, locationId). Charge with the returned ``reference_id`` so confirm and
    the Square webhook can bind the payment back to the invoice.

    Request::

        { "invoice_pk": 123 }

    Response 200::

        {
          "invoice_pk": 123,
          "amount": "35.00",
          "currency": "USD",
          "location_id": "LXXXXXXXXXXXXXXXX",
          "reference_id": "123",
          "access_token": "EAAA...",
          "idempotency_key": "taptopay-inv-123",
          "square_environment": "sandbox"
        }

POST /api/mobile/payments/confirm/
    Verify the on-device Tap to Pay charge (by payment_id) and record it on the invoice.

    Request::

        {
          "invoice_pk": 123,
          "payment_id": "GQTFp1ZlXdpoW4o6eGiZhbjosiDFf",
          "idempotency_key": "550e8400-..."
        }

    Response 200::

        {
          "payment_id": "GQTFp1ZlXdpoW4o6eGiZhbjosiDFf",
          "status": "COMPLETED",
          "receipt_number": "FXRE"
        }

    Response 409: the charge could not be verified against Square (status/amount/currency/location/
    reference mismatch, or Square was unreachable). The card may already have been charged — the
    Square webhook reconciles the same payment by reference_id, so the client should refresh the
    invoice before charging again rather than retrying blindly. A ``"code": "already_charged"`` body
    means the stable idempotency key returned an earlier charge already on the invoice (no new money
    moved); ``detail`` names the prior charge and remaining balance, which the client should show as-is
    so the cashier collects the rest another way instead of re-tapping.

Command palette
---------------
These are thin JWT wrappers over ``auctions.command_palette`` — the same shared module the
web palette uses — so search scoping, permissions and search-logging stay identical across web
and mobile. ``url`` values in items are server-relative web paths (e.g. ``/lots/42/foo/``);
the client decides whether to open them in a WebView or deep-link to a native screen by ``type``.

GET /api/mobile/command-palette/?q=<query>
    Grouped search results; an empty/absent ``q`` returns the default ("pick up where you left
    off") items. Never cached.

    Response 200::

        {
          "groups": [
            {
              "label": "Auctions",
              "items": [
                {
                  "type": "auction",   // page|auction|lot|club|clubmember|auctiontos|invoice|search
                  "title": "Spring 2024",
                  "subtitle": "My Club",
                  "url": "/auctions/spring-2024/",   // "" for type "search" (re-run, don't navigate)
                  "icon": "bi-hammer",               // Bootstrap Icons class
                  "id": 12                            // object pk, or null
                }
              ]
            }
          ]
        }

POST /api/mobile/command-palette/log/
    Upsert the user's current search-session row (one row per session, refined as the query
    changes). Pass the returned ``id`` back on subsequent calls.

    Request::

        {
          "id": 7,                 // optional: pk from a previous log response
          "search": "oscar",
          "result": "clicked",     // pending | bounce | clicked | abandoned
          "result_type": "lot",    // for "clicked": the opened item's type/url/id
          "result_url": "/lots/42/foo/",
          "result_object_id": 42
        }

    Response 200::

        { "id": 7 }
"""

import hashlib
import json
import logging

from django.conf import settings
from django.contrib.auth import login
from django.http import HttpResponse, HttpResponseRedirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from auctions.models import Club, ClubMember, Lot, ThermalPrinterProfile, UserLabelPrefs
from auctions.printer_programs import PROGRAM_SCHEMA_VERSION, serialize_profile

from .permissions import IsMobileAuthenticated
from .serializers import (
    CommandPaletteLogSerializer,
    MobileClubSerializer,
    MobileDeviceSerializer,
    MobileDeviceUnregisterSerializer,
    MobileGoogleAuthSerializer,
    MobileLabelPrefsSerializer,
    MobileLoginSerializer,
    MobilePaymentConfirmSerializer,
    MobilePaymentCreateSerializer,
    MobileUserSerializer,
)
from .services.auth import MobileAuthService
from .services.devices import DeviceService
from .services.labels import LabelService
from .services.payments import (
    PaymentAlreadyChargedError,
    PaymentService,
    PaymentVerificationError,
    SquareReconnectRequired,
)
from .services.web_session import WebSessionService

# The allauth backend is what the web login uses; logging the handoff in under the same backend
# keeps the resulting session indistinguishable from a normal web sign-in.
_ALLAUTH_BACKEND = "allauth.account.auth_backends.AuthenticationBackend"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class MobileLoginView(APIView):
    """POST /api/mobile/auth/login/ — issue JWT token pair."""

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileLoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = MobileAuthService.authenticate(
            credential=serializer.validated_data["credential"],
            password=serializer.validated_data["password"],
            request=request,
        )
        if user is None:
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_200_OK,
        )


class MobileGoogleAuthView(APIView):
    """POST /api/mobile/auth/google/ — exchange a Google ID token for a JWT pair."""

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileGoogleAuthSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        client_id = settings.GOOGLE_OAUTH_CLIENT_ID
        if not client_id:
            logger.error("GOOGLE_OAUTH_CLIENT_ID is not configured; Google auth is unavailable.")
            return Response(
                {"detail": "Google authentication is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token as google_id_token

            idinfo = google_id_token.verify_oauth2_token(
                serializer.validated_data["id_token"],
                google_requests.Request(),
                audience=client_id,
            )
        except ValueError as exc:
            logger.warning("Google ID token verification failed.", exc_info=exc)
            return Response({"detail": "Invalid ID token."}, status=status.HTTP_401_UNAUTHORIZED)

        if not idinfo.get("email_verified"):
            return Response(
                {"detail": "Google account email is not verified."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = self._get_or_create_user(idinfo["email"], idinfo["sub"])
        if user is None:
            return Response({"detail": "Unable to authenticate."}, status=status.HTTP_401_UNAUTHORIZED)

        refresh = RefreshToken.for_user(user)
        return Response(
            {"access": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _get_or_create_user(email: str, google_sub: str):
        from allauth.account.models import EmailAddress
        from allauth.socialaccount.models import SocialAccount
        from django.contrib.auth.models import User

        # Fastest path: existing SocialAccount with this Google sub → return its user
        try:
            social = SocialAccount.objects.select_related("user").get(provider="google", uid=google_sub)
            user = social.user
            return user if user.is_active else None
        except SocialAccount.DoesNotExist:
            pass

        # Mirror SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT: find existing user by email
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            # New user — generate a unique username from the email local part
            base = email.split("@")[0][:30] or "user"
            username = base
            suffix = 1
            while User.objects.filter(username=username).exists():
                username = f"{base[:27]}_{suffix}"
                suffix += 1
            user = User.objects.create_user(username=username, email=email)

        # Google has attested this email is verified — ensure allauth agrees so the
        # mandatory-email-verification gate doesn't block the newly linked user.
        try:
            addr = EmailAddress.objects.get(user=user, email__iexact=email)
            if not addr.verified or not addr.primary:
                addr.verified = True
                addr.primary = True
                addr.save(update_fields=["verified", "primary"])
        except EmailAddress.DoesNotExist:
            EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)

        # Link (or update) the SocialAccount for this user
        SocialAccount.objects.update_or_create(
            user=user,
            provider="google",
            defaults={"uid": google_sub},
        )

        return user if user.is_active else None


class MobileTokenRefreshView(TokenRefreshView):
    """POST /api/mobile/auth/refresh/ — rotate a refresh token."""

    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]


class MobileUserMeView(APIView):
    """GET /api/mobile/auth/me/ — return the authenticated user's profile."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        serializer = MobileUserSerializer(request.user)
        return Response(serializer.data)


class MobileWebSessionView(APIView):
    """POST /api/mobile/auth/web-session/ — mint a one-time WebView handoff token.

    Bridges the native JWT session into a real Django/allauth session so the WebView is
    pre-authenticated after a single native sign-in. No session is established here — we only mint
    a single-use, short-TTL token bound to the user and hand back the URL the WebView should load.
    The session cookie itself is set later by the consume view (server-set, all flags intact); it
    never touches the Dart layer.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        token = WebSessionService.create_handoff_token(request.user)
        handoff_url = request.build_absolute_uri(f"{reverse('mobile-auth-web-session-consume')}?t={token}")
        return Response({"handoff_url": handoff_url}, status=status.HTTP_200_OK)


class MobileWebSessionConsumeView(APIView):
    """GET /api/mobile/auth/web-session/consume/?t=<token> — log the WebView in, then redirect.

    Loaded by the WebView itself (no Authorization header — the token is the credential). On a valid
    token we call django.contrib.auth.login() with the allauth backend, so SessionMiddleware sets the
    sessionid cookie with the configured HttpOnly/Secure/SameSite flags on the redirect response. A
    missing/expired/already-used token establishes no session and redirects to the web login page.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        user = WebSessionService.consume_handoff_token(request.GET.get("t", ""))
        if user is None:
            return HttpResponseRedirect(reverse("account_login"))

        # login() cycles the session key and rotates the CSRF token; SessionMiddleware /
        # CsrfViewMiddleware then set sessionid (+ csrftoken) on this redirect, with all cookie flags.
        login(request, user, backend=_ALLAUTH_BACKEND)
        return HttpResponseRedirect(self._safe_next(request))

    @staticmethod
    def _safe_next(request):
        """Honour ?next= only if it points back at this host, else fall back to the web home."""
        next_url = request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return next_url
        return settings.LOGIN_REDIRECT_URL


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class MobileConfigView(APIView):
    """GET /api/mobile/config/ — public deployment config for the app.

    Unauthenticated on purpose: the app fetches this *before* any sign-in to wire up the Square
    Mobile Payments SDK and Google Sign-In against the right deployment.

    PUBLIC VALUES ONLY. Everything returned here is shipped to every device and is safe to expose
    publicly — these same values already appear in the web app's client-side code: the Square
    *application* id (NOT the secret), the Square environment name, the Google OAuth *client* id
    (NOT a client secret), and the navbar brand. NEVER add secrets here: no OAuth access tokens,
    client secrets, API keys, signing keys, or anything else that must stay server-side.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        return Response(
            {
                "square_application_id": settings.SQUARE_APPLICATION_ID,
                "square_environment": settings.SQUARE_ENVIRONMENT,
                # Web OAuth client id used as the audience when verifying Google ID tokens in
                # /api/mobile/auth/google/; the app passes it as the Google Sign-In serverClientId.
                "google_server_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "brand_name": settings.NAVBAR_BRAND,
                # Absolute URL so the app can load the site icon without knowing the static layout.
                "icon_url": request.build_absolute_uri(static("android-chrome-512x512.png")),
            }
        )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class MobileDeviceRegisterView(APIView):
    """POST /api/mobile/devices/register/ — register or update a device."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileDeviceSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        # Only pass fcm_token through when the client actually sent the key, so a registration that
        # omits it doesn't wipe a previously stored token.
        fcm_token = data.get("fcm_token") if "fcm_token" in serializer.initial_data else None
        try:
            device, created = DeviceService.register_or_update(
                user=request.user,
                device_uuid=data["device_uuid"],
                device_name=data.get("device_name", ""),
                platform=data.get("platform", ""),
                app_version=data.get("app_version", ""),
                fcm_token=fcm_token,
            )
        except ValueError:
            logger.warning("Device registration/update validation failed.", exc_info=True)
            return Response(
                {"detail": "Invalid device registration data."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = MobileDeviceSerializer(device)
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(response_serializer.data, status=http_status)


class MobileDeviceUnregisterView(APIView):
    """POST /api/mobile/devices/unregister/ — clear a device's FCM token at sign-out.

    Keeps the row (for stats) but stops pushes to it. The app calls this during sign-out, right
    before dropping the JWT, so a signed-out phone never shows the previous user's notifications.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileDeviceUnregisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        found = DeviceService.unregister(user=request.user, device_uuid=serializer.validated_data["device_uuid"])
        if not found:
            return Response({"detail": "Device not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Clubs
# ---------------------------------------------------------------------------


class MobileMyClubsView(APIView):
    """GET /api/mobile/clubs/mine/ — clubs the authenticated user belongs to.

    Reuses the same membership scoping as the web ``user_clubs`` context processor
    (a non-deleted ClubMember row), sorted by name, and flags ``is_admin`` for clubs
    where the user's membership carries permission_admin.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        memberships = ClubMember.objects.filter(user=request.user, is_deleted=False)
        club_ids = memberships.values_list("club_id", flat=True)
        admin_club_ids = set(memberships.filter(permission_admin=True).values_list("club_id", flat=True))
        clubs = list(Club.objects.filter(pk__in=club_ids).order_by("name"))
        for club in clubs:
            club.is_admin = club.pk in admin_club_ids

        serializer = MobileClubSerializer(clubs, many=True, context={"request": request})
        return Response({"clubs": serializer.data})


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class MobileLotLabelView(APIView):
    """GET /api/mobile/labels/<pk>/?fmt=png&resolution=600x400&dpi=203 — rendered label image for a lot."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    @staticmethod
    def _can_access(user, lot):
        """Seller of the lot, or an admin of its auction — mirrors web SingleLotLabelView.

        With a seller TOS, the TOS owner or an auction admin may print; without one (an
        unassigned/personal lot) only the lot's own user may.
        """
        tos = lot.auctiontos_seller
        if tos:
            if tos.user_id and tos.user_id == user.pk:
                return True
            return bool(tos.auction and tos.auction.permission_check(user))
        return bool(lot.user_id and lot.user_id == user.pk)

    def get(self, request, pk):
        try:
            lot = Lot.objects.select_related(
                "user",
                "auction",
                "species_category",
                "auctiontos_seller",
                "auctiontos_seller__auction",
                "auctiontos_seller__user",
            ).get(pk=pk, is_deleted=False)
        except Lot.DoesNotExist:
            return Response({"detail": "Lot not found."}, status=status.HTTP_404_NOT_FOUND)

        if not self._can_access(request.user, lot):
            return Response(
                {"detail": "You do not have permission to print this lot's label."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ?fmt=pdf renders a single-lot PDF with the user's UserLabelPrefs via the same WeasyPrint
        # pipeline as the web SingleLotLabelView — so a lot printed from the fishauctions://print/<pk>
        # deep link matches one printed from the website. The PNG path is unchanged.
        fmt = (request.GET.get("fmt") or "").lower()
        if fmt == "pdf":
            from .services.label_pdf import render_single_lot_pdf

            try:
                content, content_type = render_single_lot_pdf(lot, request)
            except ValueError:
                logger.warning("Invalid label PDF request.", exc_info=True)
                return Response({"detail": "Invalid label request."}, status=status.HTTP_400_BAD_REQUEST)
            return HttpResponse(content, content_type=content_type)

        # NB: param is "fmt", not "format" — DRF reserves ?format= for its own content negotiation.
        # ?resolution=WIDTHxHEIGHT&dpi=N control the output raster (default 600x400 @ 203dpi).
        try:
            content, content_type = LabelService.render_label(
                lot,
                request.GET.get("fmt"),
                resolution=request.GET.get("resolution"),
                dpi=request.GET.get("dpi"),
            )
        except ValueError:
            logger.warning("Invalid label request.", exc_info=True)
            return Response(
                {"detail": "Invalid label request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return HttpResponse(content, content_type=content_type)


# ---------------------------------------------------------------------------
# Printer profiles + label preferences
# ---------------------------------------------------------------------------


class MobilePrinterProfilesView(APIView):
    """GET /api/mobile/printers/profiles/ — every enabled thermal printer profile, priority-ordered.

    The app caches this (printing must work offline at an auction hall) and refreshes opportunistically,
    so we hand back a weak ETag; an ``If-None-Match`` that matches gets a 304.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        profiles = ThermalPrinterProfile.objects.filter(enabled=True).order_by("priority", "name")
        data = {
            "schema_version_max": PROGRAM_SCHEMA_VERSION,
            "profiles": [serialize_profile(p) for p in profiles],
        }
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        etag = f'"{digest}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED)
        response = Response(data)
        response["ETag"] = etag
        return response


class MobileLabelPrefsView(APIView):
    """GET/PATCH /api/mobile/labels/prefs/ — the user's label prefs + computed warnings.

    PATCH accepts any writable subset (used by the app's "use printer-reported size" confirmation);
    prefs are auto-created if missing and are always the caller's own.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        prefs, _ = UserLabelPrefs.objects.get_or_create(user=request.user)
        return Response(MobileLabelPrefsSerializer(prefs).data)

    def patch(self, request):
        prefs, _ = UserLabelPrefs.objects.get_or_create(user=request.user)
        serializer = MobileLabelPrefsSerializer(prefs, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class MobilePaymentCreateView(APIView):
    """POST /api/mobile/payments/create/ — validate invoice and return Square SDK params."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobilePaymentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invoice_pk = serializer.validated_data["invoice_pk"]
        try:
            result = PaymentService.create_mobile_payment(invoice_pk=invoice_pk, user=request.user)
        except LookupError as exc:
            logger.warning("Mobile payment create failed: invoice lookup error.", exc_info=exc)
            return Response({"detail": "Resource not found."}, status=status.HTTP_404_NOT_FOUND)
        except PermissionError as exc:
            logger.warning("Mobile payment create failed: permission denied.", exc_info=exc)
            return Response(
                {"detail": "You do not have permission to perform this action."}, status=status.HTTP_403_FORBIDDEN
            )
        except SquareReconnectRequired as exc:
            # Surface a distinguishable signal (not a generic 400) so the app can show a
            # "Reconnect Square" prompt instead of a flat error.
            logger.info("Mobile payment create blocked: Square account needs reconnect.", exc_info=exc)
            return Response(
                {"detail": "Square account reconnect required.", "code": "square_reconnect_required"},
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            logger.warning("Mobile payment create failed: invalid request data.", exc_info=exc)
            return Response({"detail": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class MobilePaymentConfirmView(APIView):
    """POST /api/mobile/payments/confirm/ — verify the on-device Tap to Pay charge."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobilePaymentConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        try:
            result = PaymentService.confirm_mobile_payment(
                invoice_pk=data["invoice_pk"],
                payment_id=data["payment_id"],
                idempotency_key=data["idempotency_key"],
                user=request.user,
            )
        except LookupError as exc:
            logger.warning("Mobile payment confirm failed: invoice lookup error.", exc_info=exc)
            return Response({"detail": "Resource not found."}, status=status.HTTP_404_NOT_FOUND)
        except PermissionError as exc:
            logger.warning("Mobile payment confirm failed: permission denied.", exc_info=exc)
            return Response(
                {"detail": "You do not have permission to perform this action."}, status=status.HTTP_403_FORBIDDEN
            )
        except PaymentAlreadyChargedError as exc:
            # The stable idempotency key made Square return an earlier charge that's already on the
            # invoice; no new money moved. Surface the specific, actionable message (prior amount +
            # remaining balance) so the cashier collects the rest another way instead of re-tapping
            # the same deduped charge. Caught before PaymentVerificationError (its parent).
            logger.info("Mobile payment confirm: idempotency-key reuse returned a prior charge.", exc_info=exc)
            # exc.user_message is an explicit, operator-facing string set when the error is raised — not
            # the exception's stringification — so no stack trace/internals leak into the response.
            return Response({"detail": exc.user_message, "code": "already_charged"}, status=status.HTTP_409_CONFLICT)
        except PaymentVerificationError as exc:
            # The card may already have been charged on-device; the Square webhook reconciles the
            # same payment by reference_id, so tell the operator to refresh rather than retry blindly.
            logger.warning("Mobile payment confirm failed: charge could not be verified.", exc_info=exc)
            return Response(
                {
                    "detail": (
                        "We couldn't confirm this charge automatically. If the card was charged, the "
                        "payment should appear on the invoice within a minute — refresh to check before "
                        "charging again."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            logger.warning("Mobile payment confirm failed: invalid request data.", exc_info=exc)
            return Response({"detail": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------


class MobileCommandPaletteView(APIView):
    """GET /api/mobile/command-palette/?q=<query> — grouped palette results.

    Thin wrapper over ``command_palette.search`` (the same function the web view calls) so the
    behaviour is identical; only the auth differs (JWT here, session+CSRF on the web).
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_search"  # interactive search-as-you-type; mobile_api (200/hr) is too tight
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        from auctions import command_palette

        groups = command_palette.search(request, request.GET.get("q", ""))
        response = Response({"groups": groups})
        # Results are personalised — keep them out of any intermediary cache (matches the web view).
        response["Cache-Control"] = "private, no-store"
        return response


class MobileCommandPaletteLogView(APIView):
    """POST /api/mobile/command-palette/log/ — upsert the current search-session row.

    Mirrors the web ``CommandPaletteLogView``; reuses ``command_palette.log_search`` so page-hit
    bumping and the one-row-per-session behaviour stay consistent across web and mobile.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_search"  # paired with each palette search; shares the search budget
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        from auctions import command_palette

        serializer = CommandPaletteLogSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        search_id = command_palette.log_search(
            request.user,
            search_id=data.get("id"),
            search=data.get("search", ""),
            result=data.get("result") or None,
            result_type=data.get("result_type", ""),
            result_url=data.get("result_url", ""),
            result_object_id=data.get("result_object_id"),
        )
        return Response({"id": search_id})
