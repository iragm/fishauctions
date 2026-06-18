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
GET /api/mobile/labels/<lot_pk>/?format=png
    Return the lot's label as a rendered image (default PNG) to send straight to a Bluetooth
    printer. The server owns layout/rendering; the app does not draw the label. ``format`` selects
    a registered renderer (currently ``png``); an unsupported format is a 400.

    Access is restricted to the lot's own seller or an admin of its auction (mirrors the web
    SingleLotLabelView). Others get 403; a missing/deleted lot is 404.

    Response 200:  binary image body with ``Content-Type: image/png``.

Payments
--------
POST /api/mobile/payments/create/
    Validate an invoice and return Square SDK initialisation parameters.

    Request::

        { "invoice_pk": 123 }

    Response 200::

        {
          "invoice_pk": 123,
          "amount": "35.00",
          "currency": "USD",
          "location_id": "LXXXXXXXXXXXXXXXX",
          "idempotency_key": "550e8400-...",
          "square_application_id": "sq0idp-...",
          "square_environment": "sandbox"
        }

POST /api/mobile/payments/confirm/
    Charge the nonce returned by the Square mobile SDK.

    Request::

        {
          "invoice_pk": 123,
          "source_id": "cnon:card-nonce-ok",
          "idempotency_key": "550e8400-..."
        }

    Response 200::

        {
          "payment_id": "GQTFp1ZlXdpoW4o6eGiZhbjosiDFf",
          "status": "COMPLETED",
          "receipt_number": "FXRE"
        }

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

from django.http import HttpResponse
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
from .services.payments import PaymentService

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

        try:
            content, content_type = LabelService.render_label(lot, request.GET.get("format"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

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
    """POST /api/mobile/payments/confirm/ — charge the nonce from the mobile SDK."""

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
                source_id=data["source_id"],
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
