"""Mobile API views: everything under /api/mobile/.

JWT Bearer auth, except the auth endpoints that issue tokens and config/ (bearer optional). The
WebView-loaded social continue/done and web-session consume views use a Django session.

The contract is the serializers, this code, docs/app_printing_contract.md and the app repo's
BACKEND_SPEC.md. MobileConfigView is public: never put secrets in it.
"""

import base64
import hashlib
import json
import logging

from allauth.socialaccount.helpers import complete_social_login
from django.conf import settings
from django.contrib.auth import login, logout
from django.http import HttpResponse, HttpResponseRedirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from auctions import dmca, voice
from auctions.account_deletion import cancel_deletion
from auctions.models import (
    PRIVACY_POLICY_SLUG,
    Auction,
    BlogPost,
    Club,
    ClubMember,
    Lot,
    RemotePrintJob,
    ThermalPrinterProfile,
    UserData,
    UserLabelPrefs,
    VoiceGrammar,
    Watch,
)
from auctions.printer_programs import PROGRAM_SCHEMA_VERSION, serialize_profile

from .authentication import OptionalJWTAuthentication
from .menu import menu_for
from .permissions import IsMobileAuthenticated
from .renderers import PdfRenderer, PngRenderer
from .serializers import (
    ArEventBatchSerializer,
    ArObservationBatchSerializer,
    CheckinJoinSerializer,
    CheckinPingSerializer,
    CheckinSetLocationSerializer,
    CommandPaletteLogSerializer,
    MobileClubSerializer,
    MobileDeviceHeartbeatSerializer,
    MobileDeviceSerializer,
    MobileDeviceUnregisterSerializer,
    MobileGoogleAuthSerializer,
    MobileLabelBatchSerializer,
    MobileLabelPrefsSerializer,
    MobileLabelsPrintedSerializer,
    MobileLoginSerializer,
    MobileNotificationPrefsSerializer,
    MobilePaymentAttemptCloseSerializer,
    MobilePaymentConfirmSerializer,
    MobilePaymentCreateSerializer,
    MobileRemotePrintProgressSerializer,
    MobileRemotePrintResultSerializer,
    MobileSocialAuthSerializer,
    MobileSocialCompleteSerializer,
    MobileUserSerializer,
    MobileWatchSerializer,
    OfflineSyncSerializer,
    PrinterObservationSerializer,
)
from .services import ar as ar_service
from .services import checkin as checkin_service
from .services import printers as printer_service
from .services import remote_print
from .services.auth import MobileAuthService
from .services.checkin import _single_pickup_location
from .services.devices import DeviceService
from .services.labels import LabelService
from .services.payments import (
    PaymentAlreadyChargedError,
    PaymentService,
    PaymentVerificationError,
    SquareReconnectRequired,
    TapToPayAttemptOpen,
)
from .services.social_auth import (
    PENDING_TOKEN_SESSION_KEY,
    PROVIDER_APPLE,
    PendingSocialLogin,
    SocialAuthError,
    build_sociallogin,
    resolve_completed_user,
)
from .services.web_session import WebSessionService, mark_session_opened_by_app

# Log handoffs in under allauth's backend so the session matches a normal web sign-in.
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

        # Same as the web user_logged_in signal.
        cancel_deletion(user)
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

        # As in MobileLoginView: coming back cancels a pending deletion.
        cancel_deletion(user)
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

        try:
            social = SocialAccount.objects.select_related("user").get(provider="google", uid=google_sub)
            user = social.user
            return user if user.is_active else None
        except SocialAccount.DoesNotExist:
            pass

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            base = email.split("@")[0][:30] or "user"
            username = base
            suffix = 1
            while User.objects.filter(username=username).exists():
                username = f"{base[:27]}_{suffix}"
                suffix += 1
            user = User.objects.create_user(username=username, email=email)

        try:
            addr = EmailAddress.objects.get(user=user, email__iexact=email)
            if not addr.verified or not addr.primary:
                addr.verified = True
                addr.primary = True
                addr.save(update_fields=["verified", "primary"])
        except EmailAddress.DoesNotExist:
            EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)

        SocialAccount.objects.update_or_create(
            user=user,
            provider="google",
            defaults={"uid": google_sub},
        )

        return user if user.is_active else None


class MobileSocialAuthView(APIView):
    """POST /api/mobile/auth/social/ — Apple, Google or Facebook sign-in through allauth's pipeline.

    Never find-or-creates users itself: allauth owns email conflicts and verification.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileSocialAuthSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        # DRF caches its own anonymous user, so use the underlying Django request.
        django_request = request._request

        try:
            sociallogin = build_sociallogin(django_request, data)
        except SocialAuthError as exc:
            logger.info("Mobile social sign-in rejected: %s", exc)
            return Response(
                {"detail": "Unable to complete social sign-in."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        provider = sociallogin.account.provider
        uid = sociallogin.account.uid
        response = complete_social_login(django_request, sociallogin)
        user = getattr(django_request, "user", None)

        if user is not None and user.is_authenticated:
            # Re-check so a settings change can't weaken this below the web login.
            if not MobileAuthService.email_verification_satisfied(user):
                logger.warning("Social sign-in produced a session for unverified user %s; refusing.", user.pk)
                return Response({"detail": "Please verify your email address first."}, status=status.HTTP_403_FORBIDDEN)
            self._store_apple_refresh_token(sociallogin, provider, uid, data)
            cancel_deletion(user)
            refresh = RefreshToken.for_user(user)
            return Response(
                {"access": str(refresh.access_token), "refresh": str(refresh)},
                status=status.HTTP_200_OK,
            )

        resolved = getattr(sociallogin, "user", None)
        if resolved is not None and resolved.pk and not resolved.is_active:
            logger.info("Social sign-in refused for inactive user %s.", resolved.pk)
            return Response(
                {"detail": "This account can't be signed in to."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return self._pending(request, django_request, response, sociallogin, provider, uid)

    @staticmethod
    def _store_apple_refresh_token(sociallogin, provider, uid, data):
        """Redeem Apple's authorization_code so account deletion can revoke it; failures only log."""
        from allauth.socialaccount.models import SocialAccount

        from auctions.apple_signin import redeem_authorization_code, store_tokens

        code = (data.get("authorization_code") or "").strip()
        if provider != PROVIDER_APPLE or not code:
            return
        token_data = redeem_authorization_code(code)
        if not token_data:
            return
        account = SocialAccount.objects.filter(provider=provider, uid=uid).first()
        if account is None:
            logger.warning("Apple sign-in completed but no SocialAccount for uid %s; not storing tokens.", uid)
            return
        try:
            store_tokens(account, token_data)
        except Exception:
            logger.exception("Failed to store Apple tokens for social account %s.", account.pk)

    @staticmethod
    def _pending(request, django_request, response, sociallogin, provider, uid):
        """allauth couldn't finish unattended, so park the flow and send the app to allauth's web page."""
        session = django_request.session
        serialized_login = session.get("socialaccount_sociallogin")
        # Set when allauth connected an account but didn't sign in, so a retry can finish.
        resolved_user_pk = getattr(getattr(sociallogin, "user", None), "pk", None)
        pending_token, continue_token = PendingSocialLogin.create(
            provider=provider,
            uid=uid,
            serialized_login=serialized_login,
            user_pk=resolved_user_pk,
        )
        # State is in the cache record now; flush so a reused session can't race it.
        session.flush()

        continue_url = request.build_absolute_uri(f"{reverse('mobile-auth-social-continue')}?t={continue_token}")
        return Response(
            {
                "continue_url": continue_url,
                "pending_token": pending_token,
                "detail": MobileSocialAuthView._pending_detail(response),
            },
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _pending_detail(response):
        """What to tell the user, based on where allauth was heading next."""
        location = response.get("Location", "") if hasattr(response, "get") else ""
        if location and reverse("account_email_verification_sent") in location:
            return "Check your email to confirm your address, then try again."
        return "Choose an email address to finish signing in."


class MobileSocialCompleteView(APIView):
    """POST /api/mobile/auth/social/complete/ — pick up a login finished on the web.

    Whether the user may sign in is re-derived every time; see ``resolve_completed_user``.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileSocialCompleteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        pending_token = serializer.validated_data["pending_token"]
        user = resolve_completed_user(pending_token)
        if user is None:
            return Response(
                {"detail": "That sign-in isn't finished yet. Complete it in the browser, then try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Single use: the JWT pair is the durable credential from here on.
        PendingSocialLogin.discard(pending_token)
        cancel_deletion(user)
        refresh = RefreshToken.for_user(user)
        return Response(
            {"access": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_200_OK,
        )


class MobileSocialContinueView(APIView):
    """GET /api/mobile/auth/social/continue/?t=<token> — hand an unfinished login to the WebView.

    The query-string token is the credential.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        claimed = PendingSocialLogin.consume_continue_token(request.GET.get("t", ""))
        if claimed is None:
            return HttpResponseRedirect(reverse("account_login"))
        pending_token, record = claimed

        # Sign out any existing user so the done view binds only this flow's account.
        if request._request.user.is_authenticated:
            logout(request._request)

        serialized_login = record.get("sociallogin")
        if serialized_login:
            # The app's WebView allowlist covers /social/, not /3rdparty/.
            request.session["socialaccount_sociallogin"] = serialized_login
            target = reverse("mobile_socialaccount_signup")
        else:
            target = reverse("account_email_verification_sent")
        request.session[PENDING_TOKEN_SESSION_KEY] = pending_token
        return HttpResponseRedirect(target)


class MobileSocialDoneView(APIView):
    """GET /api/mobile/auth/social/done/ — the app closes the WebView on this exact path; don't change it.

    Binds the signed-in user to the pending record; the continue view signed out anyone else first.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        pending_token = request.session.pop(PENDING_TOKEN_SESSION_KEY, "")
        user = request._request.user  # DRF's request.user is anonymous here; use the Django one.
        finished = bool(pending_token) and user.is_authenticated
        if finished:
            PendingSocialLogin.bind_user(pending_token, user.pk)
        # The app closes the WebView without reading this.
        message = (
            "You're all set. You can close this window."
            if finished
            else "This sign-in isn't finished. Please close this window and try again."
        )
        return HttpResponse(f"<!doctype html><meta charset='utf-8'><title>Signed in</title><p>{message}</p>")


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
    """POST /api/mobile/auth/web-session/ — mint a single-use, short-lived WebView handoff token."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        token = WebSessionService.create_handoff_token(request.user)
        handoff_url = request.build_absolute_uri(f"{reverse('mobile-auth-web-session-consume')}?t={token}")
        return Response({"handoff_url": handoff_url}, status=status.HTTP_200_OK)


class MobileWebSessionConsumeView(APIView):
    """GET /api/mobile/auth/web-session/consume/?t=<token> — log the WebView in, then redirect.

    A bad token creates no session and redirects to web login.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        user = WebSessionService.consume_handoff_token(request.GET.get("t", ""))
        if user is None:
            return HttpResponseRedirect(reverse("account_login"))

        login(request, user, backend=_ALLAUTH_BACKEND)
        # After login(), which cycles the session key.
        mark_session_opened_by_app(request.session)
        return HttpResponseRedirect(self._safe_next(request))

    @staticmethod
    def _safe_next(request):
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
    """GET /api/mobile/config/ — public deployment config, fetched before sign-in. Never secrets.

    `menu` is per-user when a bearer token is sent, so never cache without varying on the caller.
    """

    authentication_classes = [OptionalJWTAuthentication]
    permission_classes = []
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        data = {
            "square_application_id": settings.SQUARE_APPLICATION_ID,
            "square_environment": settings.SQUARE_ENVIRONMENT,
            "google_server_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "brand_name": settings.NAVBAR_BRAND,
            "icon_url": request.build_absolute_uri(static("android-chrome-512x512.png")),
            "terms_url": reverse("tos"),
            # Facebook's id must match the one compiled into the app.
            "apple_sign_in_enabled": bool(settings.APPLE_ALLOWED_AUDIENCES),
            "facebook_app_id": settings.FACEBOOK_APP_ID,
        }
        # Omitted when unset, rather than linking to a 404.
        if BlogPost.objects.filter(slug=PRIVACY_POLICY_SLUG).exists():
            data["privacy_policy_url"] = reverse("privacy_policy")
        if dmca.is_configured():
            data["dmca_url"] = reverse("dmca")
        firebase = getattr(settings, "FIREBASE_CLIENT_CONFIG", None)
        if firebase:
            data["firebase"] = firebase
        data["voice"] = voice.serialize_grammar(VoiceGrammar.load())
        data["menu"] = menu_for(request.user)
        return Response(data)


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
        # Only when sent, so omitting it doesn't wipe a stored token.
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
    """POST /api/mobile/devices/unregister/ — clear the FCM token at sign-out; the row stays."""

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


class MobileDeviceHeartbeatView(APIView):
    """POST /api/mobile/devices/heartbeat/ — the phone is awake, and whether it can print.

    Remote printing depends on it; see services.remote_print.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileDeviceHeartbeatSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        device = remote_print.heartbeat(
            request.user,
            data["device_uuid"],
            print_ready=data.get("print_ready", False),
            printer_name=data.get("printer_name", ""),
            print_method=data.get("print_method", ""),
        )
        if device is None:
            return Response({"detail": "Device not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Remote print jobs (computer -> phone)
# ---------------------------------------------------------------------------


class MobileRemotePrintJobMixin:
    """The job, or 404, including for another user's job."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get_job(self, request, job_uuid):
        return RemotePrintJob.objects.filter(uuid=job_uuid, user=request.user).first()


class MobileRemotePrintProgressView(MobileRemotePrintJobMixin, APIView):
    """POST /api/mobile/printjobs/<uuid>/progress/ — best-effort; ignored once a result is in."""

    def post(self, request, job_uuid):
        job = self.get_job(request, job_uuid)
        if job is None:
            return Response({"detail": "Job not found."}, status=status.HTTP_404_NOT_FOUND)
        if job.is_terminal:
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = MobileRemotePrintProgressSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        # Counts only ever go up: progress posts can arrive out of order.
        job.printed_count = max(job.printed_count, data.get("printed", 0))
        if data.get("total"):
            job.total_count = data["total"]
        job.status = RemotePrintJob.STATUS_PRINTING
        job.save(update_fields=["printed_count", "total_count", "status", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MobileRemotePrintResultView(MobileRemotePrintJobMixin, APIView):
    """POST /api/mobile/printjobs/<uuid>/result/ — the batch is over.

    ``message`` is shown verbatim; ``printed > 0`` also marks those labels printed.
    """

    def post(self, request, job_uuid):
        job = self.get_job(request, job_uuid)
        if job is None:
            return Response({"detail": "Job not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MobileRemotePrintResultSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        # A cancelled job can't report; one the page gave up on can.
        if job.status == RemotePrintJob.STATUS_CANCELLED:
            return Response(status=status.HTTP_204_NO_CONTENT)
        job.status = data["status"]
        job.printed_count = max(job.printed_count, data.get("printed", 0))
        if data.get("total"):
            job.total_count = data["total"]
        job.message = data.get("message", "")
        job.save(update_fields=["status", "printed_count", "total_count", "message", "updated_at"])
        job.mark_labels_printed(job.printed_count)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Clubs
# ---------------------------------------------------------------------------


class MobileMyClubsView(APIView):
    """GET /api/mobile/clubs/mine/ — the user's clubs, scoped like the web ``user_clubs``."""

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
    """GET /api/mobile/labels/<pk>/?fmt=png&resolution=600x400&dpi=203 — a rendered label.

    JSONRenderer first in renderer_classes, or image Accept headers 406 before the view runs.
    """

    permission_classes = [IsMobileAuthenticated]
    renderer_classes = [JSONRenderer, PdfRenderer, PngRenderer]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    @staticmethod
    def _can_access(user, lot):
        """Seller of the lot, or an admin of its auction, like web SingleLotLabelView."""
        tos = lot.auctiontos_seller
        if tos:
            if lot.is_owned_by(user):
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

        # Same WeasyPrint pipeline as web SingleLotLabelView.
        fmt = (request.GET.get("fmt") or "").lower()
        if fmt == "pdf":
            from .services.label_pdf import render_single_lot_pdf

            try:
                content, content_type = render_single_lot_pdf(lot, request)
            except ValueError:
                logger.warning("Invalid label PDF request.", exc_info=True)
                return Response({"detail": "Invalid label request."}, status=status.HTTP_400_BAD_REQUEST)
            return HttpResponse(content, content_type=content_type)

        # "fmt": DRF reserves ?format=.
        try:
            content, content_type = LabelService.render_label(
                lot,
                request.GET.get("fmt"),
                resolution=request.GET.get("resolution"),
                dpi=request.GET.get("dpi"),
                request=request,
            )
        except ValueError:
            logger.warning("Invalid label request.", exc_info=True)
            return Response(
                {"detail": "Invalid label request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return HttpResponse(content, content_type=content_type)


class MobileLotLabelBatchView(APIView):
    """POST /api/mobile/labels/batch/ — a print run's PNGs in one request.

    Returns ``labels`` rendered and ``remaining``; the app posts the rest again. Lots the caller can't
    print are skipped. Marks nothing printed.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        from .services.label_raster import render_lot_labels_png

        serializer = MobileLabelBatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        try:
            width, height, dpi = LabelService.parse_dimensions(data.get("resolution"), data.get("dpi"))
        except ValueError:
            # Logged, not returned: never echo an exception to the caller.
            logger.warning("Invalid label batch request.", exc_info=True)
            return Response({"detail": "Invalid label request."}, status=status.HTTP_400_BAD_REQUEST)

        pks = list(dict.fromkeys(data["lots"]))  # de-duped, order kept: it is the print order
        by_pk = Lot.objects.filter(pk__in=pks, is_deleted=False).select_related(
            "user",
            "auction",
            "species_category",
            "auctiontos_seller",
            "auctiontos_seller__auction",
            "auctiontos_seller__user",
        )
        by_pk = {lot.pk: lot for lot in by_pk}
        wanted, skipped = [], []
        for pk in pks:
            lot = by_pk.get(pk)
            if lot is None:
                skipped.append({"lot": pk, "detail": "Lot not found."})
            elif not MobileLotLabelView._can_access(request.user, lot):
                skipped.append({"lot": pk, "detail": "You do not have permission to print this lot's label."})
            else:
                wanted.append(lot)

        rendered, remaining = render_lot_labels_png(wanted, request, width=width, height=height, dpi=dpi)
        labels = []
        for lot, png in rendered:
            if png is None:
                # No auction: fall back like the single-lot endpoint.
                png, _content_type = LabelService.render_label(
                    lot, "png", resolution=data.get("resolution") or None, dpi=dpi
                )
            labels.append({"lot": lot.pk, "content_type": "image/png", "png": base64.b64encode(png).decode("ascii")})
        return Response(
            {
                "labels": labels,
                "remaining": [lot.pk for lot in remaining],
                "skipped": skipped,
                "resolution": f"{width}x{height}",
                "dpi": dpi,
            }
        )


class MobileLabelsPrintedView(APIView):
    """POST /api/mobile/labels/printed/ — which labels came out and which didn't.

    Bluetooth printing bypasses the PDF views' ``label_printed`` side effect. ``failed`` lots go back
    to unprinted and are flagged for reprinting.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileLabelsPrintedSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        # A lot in both lists: "it did not come out" wins.
        failed_pks = set(data["failed"])
        printed_pks = [pk for pk in data["lots"] if pk not in failed_pks]
        lots = Lot.objects.filter(pk__in=set(printed_pks) | failed_pks, is_deleted=False).select_related(
            "auctiontos_seller", "auctiontos_seller__auction"
        )
        # Skipped, not refused: same per-lot rule as GET labels/<pk>/.
        allowed = {lot.pk: lot for lot in lots if MobileLotLabelView._can_access(request.user, lot)}
        marked = []
        for pk in printed_pks:
            lot = allowed.get(pk)
            if lot is None:
                continue
            lot.label_printed = True
            lot.label_needs_reprinting = False
            marked.append(lot)
        failed = []
        for pk in failed_pks:
            lot = allowed.get(pk)
            if lot is None:
                continue
            lot.label_printed = False
            lot.label_needs_reprinting = True
            failed.append(lot)
        Lot.objects.bulk_update(marked + failed, ["label_printed", "label_needs_reprinting"])
        if failed:
            logger.warning(
                "User %s reported %s label(s) that did not print (conditions=%s): %s",
                request.user.pk,
                len(failed),
                ",".join(data["conditions"]) or "none reported",
                data["message"] or "no message",
            )
        return Response({"marked": len(marked), "failed": len(failed)})


# ---------------------------------------------------------------------------
# Printer profiles + label preferences
# ---------------------------------------------------------------------------


class MobilePrinterProfilesView(APIView):
    """GET /api/mobile/printers/profiles/ — enabled thermal printer profiles, weak-ETagged."""

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


class MobileVoiceVocabularyView(APIView):
    """GET /api/mobile/auctions/<slug>/voice/vocabulary/ — lot and bidder numbers for voice.

    Admin-only, weak-ETagged since it's refetched as bidders join.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request, slug):
        from .services import voice as voice_service

        auction = Auction.objects.filter(slug=slug, is_deleted=False).first()
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        if not auction.permission_check(request.user):
            return Response(
                {"detail": "You do not have permission to sell lots in this auction."},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = voice_service.build_vocabulary(auction)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        etag = f'"{digest}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED)
        response = Response(data)
        response["ETag"] = etag
        return response


class MobilePrinterObservedView(APIView):
    """POST /api/mobile/printers/observed/ — record a paired printer. Lenient; ``matched_by`` required."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = PrinterObservationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        observation, created = printer_service.record_observation(request.user, serializer.validated_data)
        return Response(
            {"id": observation.pk, "times_seen": observation.times_seen},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class MobileLabelPrefsView(APIView):
    """GET/PATCH /api/mobile/labels/prefs/ — the caller's label prefs and warnings, auto-created."""

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
# Notifications
# ---------------------------------------------------------------------------


class MobileNotificationPrefsView(APIView):
    """GET/PATCH /api/mobile/notifications/prefs/ — the two push toggles.

    Stores intent, like the web form: never refused for lack of push config or a device token.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    @staticmethod
    def _userdata(request):
        userdata, _ = UserData.objects.get_or_create(user=request.user)
        return userdata

    def get(self, request):
        return Response(MobileNotificationPrefsSerializer(self._userdata(request)).data)

    def patch(self, request):
        serializer = MobileNotificationPrefsSerializer(self._userdata(request), data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class MobilePaymentCreateView(APIView):
    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobilePaymentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invoice_pk = serializer.validated_data["invoice_pk"]
        try:
            result = PaymentService.create_mobile_payment(invoice_pk=invoice_pk, user=request.user, request=request)
        except LookupError as exc:
            logger.warning("Mobile payment create failed: invoice lookup error.", exc_info=exc)
            return Response({"detail": "Resource not found."}, status=status.HTTP_404_NOT_FOUND)
        except PermissionError as exc:
            logger.warning("Mobile payment create failed: permission denied.", exc_info=exc)
            return Response(
                {"detail": "You do not have permission to perform this action."}, status=status.HTTP_403_FORBIDDEN
            )
        except TapToPayAttemptOpen as exc:
            # An open attempt may already have charged the card; user_message is shown verbatim.
            logger.info("Mobile payment create blocked: an attempt is still open.", exc_info=exc)
            return Response(
                {"detail": exc.user_message, "code": "attempt_in_progress"},
                status=status.HTTP_409_CONFLICT,
            )
        except SquareReconnectRequired as exc:
            logger.info("Mobile payment create blocked: Square account needs reconnect.", exc_info=exc)
            return Response(
                {"detail": "Square account reconnect required.", "code": "square_reconnect_required"},
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            logger.warning("Mobile payment create failed: invalid request data.", exc_info=exc)
            return Response({"detail": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class MobilePaymentAuthorizationView(APIView):
    """GET /api/mobile/payments/authorization/ — seller credentials for warming up Tap to Pay.

    Hands out the seller's OAuth token, so it has the same admin gate as create and confirm.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        return Response(PaymentService.get_payment_authorization(request.user), status=status.HTTP_200_OK)


class MobilePaymentAttemptCloseView(APIView):
    """POST /api/mobile/payments/attempt/close/ — the SDK returned without capturing.

    Without it, ``create`` refuses the retry after a decline. Closing a closed attempt succeeds.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobilePaymentAttemptCloseSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        try:
            result = PaymentService.close_attempt(
                attempt_id=data["attempt_id"], outcome=data["outcome"], user=request.user
            )
        except LookupError as exc:
            logger.info("Mobile payment attempt close: unknown attempt.", exc_info=exc)
            return Response({"detail": "Resource not found."}, status=status.HTTP_404_NOT_FOUND)
        except PermissionError as exc:
            logger.warning("Mobile payment attempt close failed: permission denied.", exc_info=exc)
            return Response(
                {"detail": "You do not have permission to perform this action."}, status=status.HTTP_403_FORBIDDEN
            )
        except ValueError as exc:
            logger.warning("Mobile payment attempt close failed: invalid request data.", exc_info=exc)
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
            # Idempotency-key reuse returned an earlier charge; caught before PaymentVerificationError.
            logger.info("Mobile payment confirm: idempotency-key reuse returned a prior charge.", exc_info=exc)
            return Response({"detail": exc.user_message, "code": "already_charged"}, status=status.HTTP_409_CONFLICT)
        except PaymentVerificationError as exc:
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
    """GET /api/mobile/command-palette/?q=<query> — ``command_palette.search`` with JWT auth.

    Leaves out the "Lot scanning" and "Tap to Pay" rows, which the native palette adds itself.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_search"  # interactive search-as-you-type; mobile_api (200/hr) is too tight
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        from auctions import command_palette

        groups = command_palette.search(request, request.GET.get("q", ""), app_deep_links=False)
        response = Response({"groups": groups})
        # Personalised results.
        response["Cache-Control"] = "private, no-store"
        return response


class MobileCommandPaletteLogView(APIView):
    """POST /api/mobile/command-palette/log/ — ``command_palette.log_search``, like the web view."""

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


class MobileLastUsedAuctionView(APIView):
    """GET /api/mobile/auctions/last-used/ — the caller's current auction. Read-only, always 200.

    Coordinates are null unless the auction has exactly one physical PickupLocation with real ones.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        auction = getattr(request.user.userdata, "last_auction_used", None)
        if auction is None or auction.is_deleted:
            return Response(
                {
                    "slug": None,
                    "title": None,
                    "is_online": None,
                    "pretty_much_over": None,
                    "latitude": None,
                    "longitude": None,
                }
            )
        location = _single_pickup_location(auction)
        # (0, 0) means unset.
        has_coordinates = location is not None and not (location.latitude == 0 and location.longitude == 0)
        return Response(
            {
                "slug": auction.slug,
                "title": auction.title,
                "is_online": auction.is_online,
                "pretty_much_over": auction.pretty_much_over,
                "latitude": location.latitude if has_coordinates else None,
                "longitude": location.longitude if has_coordinates else None,
            }
        )


# ---------------------------------------------------------------------------
# AR lot scanning
# ---------------------------------------------------------------------------


def _get_ar_auction(slug):
    """Resolve a non-deleted auction by slug for the AR endpoints, or None (→ 404)."""
    if not slug:
        return None
    return Auction.objects.filter(slug=slug, is_deleted=False).first()


class MobileArLotsView(APIView):
    """GET /api/mobile/ar/lots/?auction=<slug>&lots=<pk,...> — overlay and card data, up to 50 lots."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_ar"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        auction = _get_ar_auction(request.GET.get("auction"))
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)

        pks = []
        for raw in (request.GET.get("lots") or "").split(","):
            raw = raw.strip()
            if raw.isdigit():
                pks.append(int(raw))
        # De-dupe while preserving order, then cap.
        seen = set()
        pks = [p for p in pks if not (p in seen or seen.add(p))][: ar_service.MAX_LOTS_PER_METADATA_CALL]

        lots = ar_service.build_lot_metadata(auction, pks, request.user, request)
        return Response(
            {
                "auction": {"slug": auction.slug, "title": auction.title},
                "lots": lots,
            }
        )


class MobileArObservationsView(APIView):
    """POST /api/mobile/ar/observations/ — QR sightings. Junk is dropped silently; still 202."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_ar"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = ArObservationBatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        auction = _get_ar_auction(str(data["auction"]))
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)

        accepted = ar_service.ingest_observations(
            auction,
            request.user,
            session_id=data["session_id"],
            fov_hdeg=data.get("fov_hdeg"),
            frames=data["frames"],
        )
        return Response({"accepted": accepted}, status=status.HTTP_202_ACCEPTED)


class MobileArEventsView(APIView):
    """POST /api/mobile/ar/events/ — AR events as lot PageViews with an ``ar_*`` source, de-duped."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_ar"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = ArEventBatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        auction = _get_ar_auction(str(data["auction"]))
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)

        accepted = ar_service.record_ar_events(auction, request.user, data["events"], request)
        return Response({"accepted": accepted}, status=status.HTTP_202_ACCEPTED)


class MobileArPositionsView(APIView):
    """GET /api/mobile/ar/positions/?auction=<slug> — solved positions for unsold, unremoved lots."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_ar"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        auction = _get_ar_auction(request.GET.get("auction"))
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ar_service.positions_payload(auction))


class MobileLotWatchView(APIView):
    """POST /api/mobile/lots/<pk>/watch/ — set (not toggle) the watch state, like ``WatchOrUnwatch``."""

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request, pk):
        serializer = MobileWatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if lot is None:
            return Response({"detail": "Lot not found."}, status=status.HTTP_404_NOT_FOUND)

        watch = serializer.validated_data["watch"]
        if watch:
            Watch.objects.get_or_create(lot_number=lot, user=request.user)
        else:
            Watch.objects.filter(lot_number=lot, user=request.user).delete()
        return Response({"watched": watch})


# ---------------------------------------------------------------------------
# Proximity check-in & welcome
# ---------------------------------------------------------------------------


class MobileCheckinPingView(APIView):
    """POST /api/mobile/checkin/ping/ — the phone's position; returns display-ready ``actions``.

    Never 404s for nothing nearby (the app reads 404 as a missing endpoint); returns empty actions.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_checkin"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = CheckinPingSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        actions = checkin_service.evaluate_ping(
            request.user,
            serializer.validated_data["latitude"],
            serializer.validated_data["longitude"],
        )
        return Response({"actions": actions})


class MobileCheckinJoinView(APIView):
    """POST /api/mobile/checkin/join/ — join from the welcome prompt. Idempotent.

    Auto-checks-in on check-in-mode auctions. No distance re-check, but must be in the welcome window;
    403 when ``Auction.allow_self_checkin`` is off.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_checkin"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = CheckinJoinSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        auction = Auction.objects.filter(slug=serializer.validated_data["auction"], is_deleted=False).first()
        if auction is None or auction.is_online:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        if not auction.in_welcome_window():
            return Response(
                {"detail": "This auction isn't open for check-in right now."}, status=status.HTTP_400_BAD_REQUEST
            )
        tos, checked_in = checkin_service.join_auction(request.user, auction)
        if tos is None:
            return Response(
                {"detail": "Check in with an auction volunteer to get your bidder number."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {
                "joined": True,
                "checked_in": checked_in,
                "bidder_number": tos.bidder_number or "",
                "rules_url": auction.get_absolute_url(),
            }
        )


class MobileCheckinSetLocationView(APIView):
    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_checkin"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = CheckinSetLocationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        auction = Auction.objects.filter(slug=serializer.validated_data["auction"], is_deleted=False).first()
        if auction is None or auction.is_online:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        if not auction.permission_check(request.user):
            return Response({"detail": "You are not an admin of this auction."}, status=status.HTTP_403_FORBIDDEN)
        set_ok = checkin_service.set_auction_location(
            auction,
            request.user,
            serializer.validated_data["latitude"],
            serializer.validated_data["longitude"],
        )
        if not set_ok:
            return Response(
                {"detail": "This auction has no single pickup location to pin."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"set": True})


# ---------------------------------------------------------------------------
# Offline mode (in-person sale)
# ---------------------------------------------------------------------------


class MobileOfflineSnapshotView(APIView):
    """GET /api/mobile/offline/snapshot/ — the caller's last admin auction and offline-screen data.

    ``auction: null`` when they administer none; a 404 means an older deployment.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        from .services import offline

        auction = offline.get_last_admin_auction(request.user)
        return Response(offline.build_snapshot(auction))


class MobileOfflineSyncView(APIView):
    """POST /api/mobile/offline/sync/ — apply queued offline ops, then return a snapshot.

    Ops apply in order, idempotently, one at a time (never all-or-nothing).
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        from .services import offline

        serializer = OfflineSyncSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        auction = Auction.objects.filter(slug=serializer.validated_data["auction"], is_deleted=False).first()
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        if not auction.permission_check(request.user):
            return Response(
                {"detail": "You do not have permission to sync this auction."},
                status=status.HTTP_403_FORBIDDEN,
            )

        results = offline.apply_ops(auction, request.user, serializer.validated_data["ops"])
        return Response({"results": results, "snapshot": offline.build_snapshot(auction)})
