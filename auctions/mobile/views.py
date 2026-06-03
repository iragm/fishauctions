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
GET /api/mobile/labels/<lot_pk>/
    Return structured label data for a lot.

    Response 200::

        {
          "label_data": {
            "lot_number": "42",
            "title": "Albino Oscar pair",
            "quantity": 2,
            "minimum_bid": "10.00",
            "buy_now_price": "25.00",
            "seller": "Bob Jones",
            "auction": "Spring 2024",
            "category": "Cichlids",
            "i_bred_this_fish": true,
            "custom_field_1": null
          },
          "metadata": {
            "generated_at": "2024-06-01T12:00:00Z",
            "lot_pk": 42,
            "supported_formats": ["png", "pdf", "raw_commands"]
          }
        }

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
"""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from auctions.models import Lot

from .permissions import IsMobileAuthenticated
from .serializers import (
    LotLabelResponseSerializer,
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

    def get(self, request):
        serializer = MobileUserSerializer(request.user)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class MobileDeviceRegisterView(APIView):
    """POST /api/mobile/devices/register/ — register or update a device."""

    permission_classes = [IsMobileAuthenticated]

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
    """GET /api/mobile/labels/<pk>/ — return structured label data for a lot."""

    permission_classes = [IsMobileAuthenticated]

    def get(self, request, pk):
        try:
            lot = Lot.objects.select_related("user", "auction", "species_category").get(pk=pk)
        except Lot.DoesNotExist:
            return Response({"detail": "Lot not found."}, status=status.HTTP_404_NOT_FOUND)

        payload = LabelService.get_lot_label_data(lot)
        serializer = LotLabelResponseSerializer(data=payload)
        serializer.is_valid()  # payload is constructed internally — always valid
        return Response(payload)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class MobilePaymentCreateView(APIView):
    """POST /api/mobile/payments/create/ — validate invoice and return Square SDK params."""

    permission_classes = [IsMobileAuthenticated]

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
            return Response({"detail": "You do not have permission to perform this action."}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            logger.warning("Mobile payment create failed: invalid request data.", exc_info=exc)
            return Response({"detail": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class MobilePaymentConfirmView(APIView):
    """POST /api/mobile/payments/confirm/ — charge the nonce from the mobile SDK."""

    permission_classes = [IsMobileAuthenticated]

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
            return Response({"detail": "You do not have permission to perform this action."}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            logger.warning("Mobile payment confirm failed: invalid request data.", exc_info=exc)
            return Response({"detail": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)
