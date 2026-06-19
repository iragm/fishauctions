"""
Mobile API views.

All endpoints live under /api/mobile/ and require JWT Bearer authentication
(except auth/login and auth/refresh which issue / rotate tokens).

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

Labels
------
GET /api/mobile/labels/<lot_pk>/?fmt=png
    Return the lot's label as a rendered image (default PNG) to send straight to a Bluetooth
    printer. The server owns layout/rendering; the app does not draw the label. ``fmt`` selects a
    registered renderer (currently ``png``); an unsupported format is a 400. (The param is ``fmt``,
    not ``format`` — DRF reserves ``?format=`` for content negotiation.)

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
          "idempotency_key": "550e8400-...",
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
    invoice before charging again rather than retrying blindly.

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

import logging

from django.conf import settings
from django.contrib.auth import login
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from auctions.models import Lot

from .permissions import IsMobileAuthenticated
from .serializers import (
    CommandPaletteLogSerializer,
    MobileDeviceSerializer,
    MobileLoginSerializer,
    MobilePaymentConfirmSerializer,
    MobilePaymentCreateSerializer,
    MobileUserSerializer,
)
from .services.auth import MobileAuthService
from .services.devices import DeviceService
from .services.labels import LabelService
from .services.payments import PaymentService, PaymentVerificationError
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
        try:
            device, created = DeviceService.register_or_update(
                user=request.user,
                device_uuid=data["device_uuid"],
                device_name=data.get("device_name", ""),
                platform=data.get("platform", ""),
                app_version=data.get("app_version", ""),
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


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class MobileLotLabelView(APIView):
    """GET /api/mobile/labels/<pk>/?format=png — rendered label image for a lot."""

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

        # NB: param is "fmt", not "format" — DRF reserves ?format= for its own content negotiation.
        try:
            content, content_type = LabelService.render_label(lot, request.GET.get("fmt"))
        except ValueError:
            logger.warning("Invalid label format requested.", exc_info=True)
            return Response(
                {"detail": "Invalid label format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return HttpResponse(content, content_type=content_type)


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
