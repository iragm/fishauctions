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
          // Which social sign-in buttons to draw. A provider whose key is empty/absent is hidden
          // entirely; configure none and the app just shows the password form.
          "apple_sign_in_enabled":   true,
          "facebook_app_id":         "1234567890",
          "brand_name":              "auction.fish",
          "terms_url":               "/tos/",
          // Omitted when this deployment has no privacy policy page; the app then draws no
          // privacy link rather than a dead one.
          "privacy_policy_url":      "/privacy/",
          // Optional; present only for platforms whose Firebase config file is set. Public values.
          "firebase": {
            "android": {"package_name": "...", "api_key": "...", "app_id": "...",
                        "messaging_sender_id": "...", "project_id": "..."},
            "ios":     {"bundle_id": "...", "api_key": "...", "app_id": "...",
                        "messaging_sender_id": "...", "project_id": "..."}
          },
          // Optional; the set-winners voice grammar, present only once an admin has configured
          // one (auctions.models.VoiceGrammar). Absent means "use the app's bundled grammar".
          // See auctions/voice.py for what each key does.
          "voice": {
            "enabled": true, "backend": "platform", "locale": "en_US", "prefer_on_device": true,
            "anchors": {"lot": ["lot", "item"], "…": []},
            "number_words": {"seventeen": 17},
            "homophones": [["15", "50"]],
            "weights": {"asr": 0.5, "keyword": 1.0, "snap": 1.0, "agreement": 0.4},
            "thresholds": {"confident": 0.85, "unsure": 0.5},
            "auto_submit_on_sold": true, "block_auto_submit_when_unsure": true
          }
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

POST /api/mobile/auth/social/
    Sign in with Apple, Google or Facebook — one endpoint, allauth's provider ids. Supersedes
    auth/google/ (which stays alive until older installs age out). Verifies the provider credential
    and then runs allauth's own social-login pipeline, so connecting to an existing account, the
    unique-email rules and the email-verification gate all behave exactly as they do on the web.

    Request::

        {
          "provider": "apple" | "google" | "facebook",
          "id_token": "...",             // google, apple, facebook Limited Login (iOS)
          "access_token": "...",         // facebook classic (Android)
          "authorization_code": "...",   // apple only — redeemed so deletion can revoke the grant
          "nonce": "<raw nonce>",        // apple + facebook limited; we check sha256(raw) == claim
          "email": "...",                // apple, FIRST authorization only — unauthenticated hint
          "first_name": "...",
          "last_name": "..."
        }

    Response 200 (signed in)::

        { "access": "<jwt>", "refresh": "<jwt>" }

    Response 200 (finish this on the web — Facebook gave no email, or the address needs
    confirming). The app opens ``continue_url`` in its restricted allauth WebView; that URL carries
    its own single-use credential because the WebView has neither a JWT nor a session yet::

        { "continue_url": "https://auction.fish/api/mobile/auth/social/continue/?t=...",
          "pending_token": "<opaque, single-use, ~15 min>",
          "detail": "Choose an email address to finish signing in." }

    Response 401::

        { "detail": "Invalid ID token." }

POST /api/mobile/auth/social/complete/
    Exchange a pending token for a JWT pair once the web continuation has finished. Whether the
    user may be signed in is re-derived from the database on every call (active, verified address,
    still connected to the provider account the flow started from) — the token only says which
    flow to look at.

    Request::  { "pending_token": "..." }
    Response 200:: { "access": "<jwt>", "refresh": "<jwt>" }
    Response 400:: { "detail": "..." }   // not finished / expired → the app asks them to retry

GET /api/mobile/auth/social/continue/?t=<token>
    Loaded by the WebView itself. Burns the token, rebuilds allauth's pending state in this
    browsing context's session, and redirects into the real allauth page (``/social/signup/`` or
    ``/confirm-email/``). Signs out anyone already signed in here first, so the done view below can
    only ever see an account this flow authenticated.

GET /api/mobile/auth/social/done/
    Where the web continuation ends (it's the ``next`` baked into the social login's state). The
    app watches for this exact path, closes the WebView and POSTs to complete/ — the path is a
    constant in the app, so it must not change.

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

Notifications
-------------
GET /api/mobile/notifications/prefs/
    The caller's two push toggles.

    Response 200::

        { "push_instead_of_email": false, "push_when_lots_sell": false }

PATCH /api/mobile/notifications/prefs/
    Partial update — only the keys sent are written; the response is the stored state.

    Request::

        { "push_instead_of_email": true, "push_when_lots_sell": true }

    The write is never refused because push isn't configured or the account has no live device
    token: the preference is intent, exactly as on the web form. The app calls this as the last
    step of its opt-in gesture (OS permission → ``devices/register/`` → this PATCH), so a 404 here
    means a backend older than the endpoint and the app falls back to sending the user to
    /preferences/.

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

Auctions
--------
GET /api/mobile/auctions/last-used/
    The caller's current ("last used") auction, as read-only state the command palette fetches once
    when it opens to decide — client-side — whether to surface the native AR lot-scanning entry.
    No side effects (unlike ``checkin/ping/``). Always 200; a 404 means an older backend without
    this endpoint (the app then just omits the AR entry, same degrade-on-404 as the AR/check-in
    endpoints). Every field is null when there's no last-used auction or it was deleted.
    ``latitude``/``longitude`` are the single physical pickup location's coordinates, or null when
    there isn't exactly one with coordinates set.

    Response 200::

        {
          "slug":             "spring-fry-swap-2026",
          "title":            "Spring Fry Swap 2026",
          "is_online":        false,
          "pretty_much_over": false,
          "latitude":         40.4406,
          "longitude":        -79.9959
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

    ``Accept: application/pdf`` / ``image/png`` / ``*/*`` all negotiate (see
    ``auctions.mobile.renderers``); error bodies stay JSON.

    Response 200:  binary image body with ``Content-Type: image/png``.

POST /api/mobile/labels/printed/
    Mark labels as printed. The PDF views set ``label_printed`` as a side effect of rendering, but
    native Bluetooth printing never goes through them, so without this "print unprinted labels"
    never shrinks for anyone printing natively. The app posts the lots whose labels actually came
    out (including the ones sent before a failure or a cancel), fire-and-forget.

    Request::

        { "lots": [12, 13, 14] }

    Response 200::

        { "marked": 3 }

    Per-lot permission is the same rule as ``labels/<pk>/``. Lots the caller can't touch are
    silently skipped rather than failing the batch — some of them printed fine. Idempotent: a
    reprint posting the same pks is normal.

Voice
-----
GET /api/mobile/auctions/<slug>/voice/vocabulary/
    The lot and bidder numbers the app is allowed to hear in this auction, for voice-driven set
    winners. Admin-only (``Auction.permission_check``, the same test the set-winners page uses) and
    weak-ETagged, because the app refreshes it on a timer and after every save — bidders are added
    at the check-in desk while selling is running.

    **The values are strings and nothing normalizes them**: ``BOB-1`` and ``3-1`` are both ordinary
    lot numbers in a seller-dash auction, and bidder numbers are routinely text. The app owns the
    expansion into spoken forms; matching against values that really exist here is what makes
    "fifteen" vs "fifty" decidable.

    Response 200::

        {
          "lot_numbers":    ["1", "12", "BOB-1", "3-1"],   // unsold, non-deleted, non-banned
          "bidder_numbers": ["4", "17", "BOB"],            // + ClubMember when club-managed
          "only_whole_dollar_bids": true,
          "use_seller_dash_lot_numbering": false,
          "currency_symbol": "$"
        }

    Response 304 on a matching ``If-None-Match``. 403 when the caller doesn't administer the
    auction, 404 when there is no such auction.

Printers
--------
GET /api/mobile/printers/profiles/
    Every enabled ThermalPrinterProfile, priority-ordered, weak-ETagged (an ``If-None-Match`` hit
    is a 304) because the app caches it to print offline in an auction hall. Each profile's
    ``match`` section carries ``ble_name_patterns``, ``model_patterns`` and
    ``manufacturer_patterns`` (case-insensitive regexes) plus the optional GATT ids: the app tries
    the advertised BLE name first, and — since that name is user-editable and resellers rename the
    same board — falls back to what the printer reports over the GATT Device Information Service.

POST /api/mobile/printers/observed/
    Record a successful pairing. Fire-and-forget: the app ignores the body, and a 404 here just
    disables the call for that process, so this endpoint is optional and non-breaking.

    Request::

        {
          "ble_name": "D11-4C21",
          "manufacturer": "AiYin",         // omitted when the printer didn't say
          "model": "D11S",
          "firmware": "1.0.3",
          "hardware": "V2",
          "service_uuids": ["18f0", "180a", "1800"],
          "profile_slug": "d11s-aiyin",    // null/omitted when the user cancelled out
          "matched_by": "bleName",         // bleName | deviceInfo | serviceUuid | probe | manual

          // Everything below is optional and additive. The DIS often names the *radio module*
          // rather than the printer (a Y486BT reports "Feasycom" / "FSC-BT986"), which isn't
          // enough to author a profile from — so the app also asks each command language its
          // standard read-only status query, and reports the full GATT tree. Absent when the
          // printer matched a profile without probing.
          "probe_replies": {"tspl_status": {"hex": "00", "ascii": "."}},
          "probed_language": "tspl",       // tspl | escpos | zpl | cpcl | d11s | null
          "gatt": [                        // service/characteristic tree, with properties
            {"uuid": "49535343-fe7d-4ae5-8fa9-9fafd205e455",
             "characteristics": [
               {"uuid": "49535343-8841-43f4-a8d4-ecbe34729bb3", "properties": ["write", "writeNR"]},
               {"uuid": "49535343-1e4d-4bd9-ba61-23c647249616", "properties": ["notify"]}
             ]}
          ],

          // From the app's "Improve support" walkthrough: the printer's status reply in four
          // physical states whose meaning is known in advance, so the derived map is a
          // derivation rather than a guess. Sets `characterized`, the admin's work-queue filter.
          "status_captures": {
            "ready":                {"tspl_status": {"hex": "00"}},
            "cover_open":           {"tspl_status": {"hex": "01"}},
            "no_labels_cover_open": {"tspl_status": {"hex": "05"}},
            "no_labels":            {"tspl_status": {"hex": "04"}}
          },
          "derived_status_values": {"00": [], "01": ["cover_open"],
                                    "05": ["cover_open", "out_of_paper"], "04": ["out_of_paper"]},
          "status_ambiguities": ["01: cover_open and no_labels_cover_open are indistinguishable"]
        }

    Response 201 (new) / 200 (already seen)::

        { "id": 12, "times_seen": 3 }

Lots
----
POST /api/mobile/lots/<pk>/watch/
    Set the caller's watch state on a lot (e.g. from the AR preview card, without opening the full
    lot page). Idempotent — it sets, not toggles.

    Request::

        { "watch": true }

    Response 200::

        { "watched": true }

Payments
--------
The Flutter app uses Square's Mobile Payments SDK (Tap to Pay): it charges the card on-device
and returns a completed Square payment_id. There is no nonce and the server never calls
payments.create — confirm re-fetches the payment from Square and verifies it before recording.

All three endpoints are restricted to the merchant collecting payment — the auction creator, a
superuser, anyone with an is_admin AuctionTOS on the auction (so a Square auction needs no club),
or a club admin / money manager / auction manager for the invoice's club. The buyer is never
authorized: the device authorizes with the *seller's* Square account, so the access token must
not reach a buyer.

GET /api/mobile/payments/authorization/
    Seller credentials for warming up the reader before any invoice exists. Apple requires Tap to
    Pay to start preparing when the app foregrounds and its UI to appear within a second; the SDK
    only prepares once authorized, and authorizing per invoice inside ``create`` happens at the
    moment the cashier presses the button. ``can_accept_terms`` answers Apple's rule that only an
    administrator may accept the Tap to Pay terms.

    Always 200 for a signed-in user. ``access_token``/``location_id`` appear only when the user
    could charge right now — ``eligible: true`` with no credentials is normal (no Square account
    yet, or one that needs reconnecting) and means "show the setup UI, skip the warm-up".

    Response 200 (merchant)::

        {
          "eligible": true,
          "can_accept_terms": true,
          "access_token": "EAAA...",
          "location_id": "LXXXXXXXXXXXXXXXX",
          "seller_name": "Capital Cichlid Association"
        }

    Response 200 (signed in, not a merchant)::

        {
          "eligible": false,
          "can_accept_terms": false,
          "message": "Only an auction admin with a connected Square account can set up Tap to Pay."
        }

    ``message`` is rendered verbatim by the app, so the wording can change without an app release.

POST /api/mobile/payments/create/
    Validate an invoice and return the parameters needed to authorize the Mobile Payments SDK.
    The seller's OAuth access token is returned because the SDK authorizes on-device with
    authorize(accessToken, locationId). Charge with the returned ``reference_id`` so confirm and
    the Square webhook can bind the payment back to the invoice. Since that token is a
    merchant-wide credential, every successful create writes an auction (or club) history entry
    naming the admin who requested it — a create with no matching payment is worth a look.

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
          "receipt_number": "FXRE",
          // Square's hosted receipt; null when Square didn't supply one. The app shares it through
          // the OS share sheet so the customer gets a real receipt, not just a reference number.
          "receipt_url": "https://squareup.com/receipt/preview/GQTFp1ZlXdpoW4o6eGiZhbjosiDFf"
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

from auctions import voice
from auctions.account_deletion import cancel_deletion
from auctions.models import (
    PRIVACY_POLICY_SLUG,
    Auction,
    BlogPost,
    Club,
    ClubMember,
    Lot,
    ThermalPrinterProfile,
    UserData,
    UserLabelPrefs,
    VoiceGrammar,
    Watch,
)
from auctions.printer_programs import PROGRAM_SCHEMA_VERSION, serialize_profile

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
    MobileDeviceSerializer,
    MobileDeviceUnregisterSerializer,
    MobileGoogleAuthSerializer,
    MobileLabelPrefsSerializer,
    MobileLabelsPrintedSerializer,
    MobileLoginSerializer,
    MobileNotificationPrefsSerializer,
    MobilePaymentConfirmSerializer,
    MobilePaymentCreateSerializer,
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
from .services.auth import MobileAuthService
from .services.checkin import _single_pickup_location
from .services.devices import DeviceService
from .services.labels import LabelService
from .services.payments import (
    PaymentAlreadyChargedError,
    PaymentService,
    PaymentVerificationError,
    SquareReconnectRequired,
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

        # Signing in calls off a pending account deletion, exactly as it does on the web (where the
        # user_logged_in signal does it) -- the deletion page tells people that, and someone who
        # deleted from inside the app is most likely to come back through this endpoint.
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


class MobileSocialAuthView(APIView):
    """POST /api/mobile/auth/social/ — sign in with Apple, Google or Facebook.

    Verifies the provider credential (see ``services.social_auth`` for what "verified" means per
    provider) and then hands the result to allauth's own social-login pipeline. allauth decides
    whether that identity connects to an existing account, creates a new one, or needs the user to
    finish something first; this view only translates allauth's outcome into either a JWT pair or a
    pointer to the web continuation.

    Deliberately does *not* find-or-create users itself. Doing so means reimplementing
    ``SOCIALACCOUNT_EMAIL_AUTHENTICATION``, the unique-email conflict rules and the
    email-verification gate — and two of these three providers have materially weaker email
    guarantees than Google, so a second implementation is where an account-takeover bug would live.
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

        # allauth works on the Django request: it reads/writes the session and sets request.user on
        # login. DRF's wrapper caches its own (anonymous) user, so everything below goes through the
        # underlying request rather than `request`.
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
            # Last line of defence, and a no-op in a correctly configured deployment: allauth's
            # mandatory-verification gate should already have stopped an unverified address from
            # reaching a signed-in state. Re-checking here means a settings change can't silently
            # turn this endpoint into a weaker door than the web login.
            if not MobileAuthService.email_verification_satisfied(user):
                logger.warning("Social sign-in produced a session for unverified user %s; refusing.", user.pk)
                return Response({"detail": "Please verify your email address first."}, status=status.HTTP_403_FORBIDDEN)
            self._store_apple_refresh_token(sociallogin, provider, uid, data)
            # As in MobileLoginView: coming back cancels a pending deletion.
            cancel_deletion(user)
            refresh = RefreshToken.for_user(user)
            return Response(
                {"access": str(refresh.access_token), "refresh": str(refresh)},
                status=status.HTTP_200_OK,
            )

        # allauth resolved the identity to a real account but wouldn't sign it in, and the account
        # is disabled. Say so rather than sending them off to a signup form that can't help: the
        # web and password logins refuse a deactivated account the same way.
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
        """Redeem Apple's ``authorization_code`` so the account can be revoked when it's deleted.

        Apple hands out a refresh token only in exchange for that one-shot code, and only revocation
        makes deletion complete by Apple's rules. Sign-in itself does not depend on any of this, so
        every failure path here is a log line, not an error.
        """
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
        """allauth couldn't finish unattended — park the flow and point the app at the web.

        This is the routine case, not an error: Facebook usually supplies no email at all, so the
        user has to pick one on allauth's signup form; and a fresh, unverified address has to be
        confirmed before it can sign in. Both are real allauth pages, so the app opens them rather
        than reimplementing them.
        """
        session = django_request.session
        serialized_login = session.get("socialaccount_sociallogin")
        # Set when allauth created or connected a real account but stopped short of signing them in
        # -- an unconfirmed address, essentially always. (Not request.user: allauth blocks at the
        # verification stage *before* it logs anyone in, so that's still anonymous here.) Recording
        # it lets the app finish with a plain retry once they click the link in their inbox, and
        # gives ``resolve_completed_user`` something to cross-check the SocialAccount against.
        resolved_user_pk = getattr(getattr(sociallogin, "user", None), "pk", None)
        pending_token, continue_token = PendingSocialLogin.create(
            provider=provider,
            uid=uid,
            serialized_login=serialized_login,
            user_pk=resolved_user_pk,
        )
        # The pending state now lives in the cache record, so drop this request's copy: the app
        # carries no cookies into the WebView, and a stale half-finished login on a session the app
        # might reuse would race with the continuation.
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
    """POST /api/mobile/auth/social/complete/ — pick up a login the user finished on the web.

    The pending token says *which* flow to look at. Whether its user may actually be signed in is
    re-derived from the database every time (active, verified address, still connected to the
    provider account the flow started from) — see ``resolve_completed_user``.
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

    Loaded by the WebView itself, which has no JWT and no session, so the single-use token in the
    query string *is* the credential (the same shape as the web-session handoff). It rebuilds
    allauth's pending-signup state in this browsing context's own session and redirects into the
    real allauth page.
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

        # Anyone already signed in here is not who this flow is about, and leaving them signed in
        # would let the done view below bind the wrong account to this record. allauth's own
        # _authenticate does the same thing for the same reason.
        #
        # request._request, not request: this view sets authentication_classes = [], so DRF's own
        # request.user is AnonymousUser no matter who holds the session cookie. Only the underlying
        # Django request has been through AuthenticationMiddleware and knows.
        if request._request.user.is_authenticated:
            logout(request._request)

        serialized_login = record.get("sociallogin")
        if serialized_login:
            # Exactly where allauth's redirect_to_signup puts it, so its signup view picks it up as
            # if the flow had never left the browser. The /social/ alias (auctions/urls.py) rather
            # than allauth's own /3rdparty/ path, because the app's WebView allowlist is built
            # around /social/... and would refuse to load the other one.
            request.session["socialaccount_sociallogin"] = serialized_login
            target = reverse("mobile_socialaccount_signup")
        else:
            # Nothing to resume: the account exists and is waiting on email confirmation.
            target = reverse("account_email_verification_sent")
        request.session[PENDING_TOKEN_SESSION_KEY] = pending_token
        return HttpResponseRedirect(target)


class MobileSocialDoneView(APIView):
    """GET /api/mobile/auth/social/done/ — where the web continuation ends.

    allauth redirects here once the flow finishes (it's the ``next`` baked into the social login's
    state). The app watches for this exact path, closes the WebView and POSTs to ``complete/``. The
    path is a constant in the app, so it must not change.

    Binds whoever is signed in *in this browsing context* to the pending record. That's safe
    because the continue view above signs out any pre-existing user, so the only account that can
    be seen here is one this flow authenticated.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_auth"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        pending_token = request.session.pop(PENDING_TOKEN_SESSION_KEY, "")
        # request._request, not request: authentication_classes is empty, so DRF's request.user is
        # always anonymous. The session user is on the underlying Django request.
        user = request._request.user
        finished = bool(pending_token) and user.is_authenticated
        if finished:
            PendingSocialLogin.bind_user(pending_token, user.pk)
        # A human-readable page, not JSON: the app closes the WebView the instant it sees this path
        # and never reads the body, so the only person who ever sees this is someone whose WebView
        # didn't close — and raw JSON is a poor thing to leave them looking at.
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
        # Set after login(), which cycles the session key and would otherwise drop this.
        mark_session_opened_by_app(request.session)
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
    (NOT a client secret), the navbar brand, and the paths of the public terms and privacy pages.
    NEVER add secrets here: no OAuth access tokens, client secrets, API keys, signing keys, or
    anything else that must stay server-side.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        data = {
            "square_application_id": settings.SQUARE_APPLICATION_ID,
            "square_environment": settings.SQUARE_ENVIRONMENT,
            # Web OAuth client id used as the audience when verifying Google ID tokens in
            # /api/mobile/auth/google/; the app passes it as the Google Sign-In serverClientId.
            "google_server_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "brand_name": settings.NAVBAR_BRAND,
            # Absolute URL so the app can load the site icon without knowing the static layout.
            "icon_url": request.build_absolute_uri(static("android-chrome-512x512.png")),
            # Legal pages the app links natively from its login and sign-up screens (Apple requires
            # both to be reachable from inside the app at the point of sign-up). Server-relative:
            # the app rejects an off-host URL, since these open inside the signed-out login trap.
            "terms_url": reverse("tos"),
            # Which social sign-in buttons to draw. The app hides a provider entirely when its key
            # is absent, so a deployment that configures none of them simply shows the password
            # form. Note the deliberate asymmetry: Apple is a boolean because the native flow's
            # audience is the app's own bundle id and it needs nothing at runtime, whereas Google
            # needs its web client id above. Facebook's id is only half the story — the SDKs read it
            # from Info.plist / AndroidManifest.xml at launch and register an fb<app-id> URL scheme,
            # so it is *also* compiled into the build. This key decides whether to offer the button
            # and must agree with the compiled-in value, which is why Facebook is the one provider
            # where a fork needs its own build.
            "apple_sign_in_enabled": bool(settings.APPLE_ALLOWED_AUDIENCES),
            "facebook_app_id": settings.FACEBOOK_APP_ID,
        }
        # Omitted rather than pointing at a 404 if the page is missing on this deployment — the app
        # then draws no privacy link at all, which is the honest state.
        if BlogPost.objects.filter(slug=PRIVACY_POLICY_SLUG).exists():
            data["privacy_policy_url"] = reverse("privacy_policy")
        # Public Firebase client config per platform, parsed from the mobile config files. Only the
        # platforms whose file is configured appear; the whole key is omitted when neither is set.
        # Public values only (api key, app id, sender id, project id, package/bundle id) — no secrets.
        firebase = getattr(settings, "FIREBASE_CLIENT_CONFIG", None)
        if firebase:
            data["firebase"] = firebase
        # The set-winners voice grammar, when an admin has configured one. Omitted otherwise, which
        # the app reads as "use the grammar you shipped with" — so the key's absence is the normal
        # state, not a failure. `enabled: false` in a configured row is the kill switch: the app
        # reports supported=false and the page hides its microphone button, no release needed.
        grammar = VoiceGrammar.load()
        if grammar:
            data["voice"] = voice.serialize_grammar(grammar)
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
    """GET /api/mobile/labels/<pk>/?fmt=png&resolution=600x400&dpi=203 — rendered label image for a lot.

    The body is a plain HttpResponse of bytes, but DRF negotiates content *before* the view runs:
    without renderers that can satisfy them, ``Accept: application/pdf`` / ``Accept: image/png``
    were answered with 406 and label printing failed outright. JSON stays first so error payloads
    (``{"detail": …}``, which the app surfaces) still render as JSON for ordinary clients.
    """

    permission_classes = [IsMobileAuthenticated]
    renderer_classes = [JSONRenderer, PdfRenderer, PngRenderer]
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
                request=request,
            )
        except ValueError:
            logger.warning("Invalid label request.", exc_info=True)
            return Response(
                {"detail": "Invalid label request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return HttpResponse(content, content_type=content_type)


class MobileLabelsPrintedView(APIView):
    """POST /api/mobile/labels/printed/ — mark a batch of lot labels as printed.

    The PDF views set ``label_printed`` as a side effect of rendering
    (``LotLabelView.get_context_data`` → ``bulk_update``), and neither ``labels/<pk>/`` nor the
    ``fishauctions://print/`` deep-link path goes through them — so "print unprinted labels" would
    never shrink for anyone printing natively over Bluetooth. This closes that.

    Fire-and-forget from the app, and self-disabling: a 404 turns it off for the process, so a
    deployment without this endpoint behaves exactly as before.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        serializer = MobileLabelsPrintedSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        pks = serializer.validated_data["lots"]
        lots = Lot.objects.filter(pk__in=pks, is_deleted=False).select_related(
            "auctiontos_seller", "auctiontos_seller__auction"
        )
        # Lots the caller can't touch are skipped, not refused: a batch of forty is one print run,
        # and most of it printed fine. Same per-lot rule as GET labels/<pk>/.
        allowed = [lot for lot in lots if MobileLotLabelView._can_access(request.user, lot)]
        for lot in allowed:
            lot.label_printed = True
            lot.label_needs_reprinting = False
        # Matches what the PDF views write, so the two paths agree on what "printed" means.
        Lot.objects.bulk_update(allowed, ["label_printed", "label_needs_reprinting"])
        return Response({"marked": len(allowed)})


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


class MobileVoiceVocabularyView(APIView):
    """GET /api/mobile/auctions/<slug>/voice/vocabulary/ — the values voice may match against here.

    Auction-scoped and admin-only (the same ``permission_check`` the set-winners page runs). Nothing
    here is new exposure: every lot number and bidder number in it is already on the users and lots
    pages this operator is looking at.

    ETagged like ``printers/profiles/``, because the app refreshes on a timer and after every save
    — bidders get added at the check-in desk *while* selling is running, so a vocabulary fetched
    once at page load is stale within minutes, and most of those refreshes change nothing.

    There is deliberately no offline path: the app doesn't read voice vocabulary out of the offline
    snapshot. Voice without a live vocabulary would mark every field unsure anyway.
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
    """POST /api/mobile/printers/observed/ — record a printer that paired, and how it was identified.

    Fire-and-forget from the app (it ignores the response), so this is lenient by design: over-long
    strings are truncated rather than rejected, and only ``matched_by`` is required. The point is the
    admin list — every ``matched_by: "manual"`` row is a printer no profile claimed, and the
    model/manufacturer it reports is what belongs in that profile's match patterns.
    """

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
# Notifications
# ---------------------------------------------------------------------------


class MobileNotificationPrefsView(APIView):
    """GET/PATCH /api/mobile/notifications/prefs/ — the two push toggles, for the app's opt-in flow.

    The app raises the OS notification permission only where the answer means something, and
    "Enable" is one gesture: permission, then ``devices/register/``, then this PATCH. Without it the
    app could only get the permission and send the user to /preferences/ to finish — where the
    checkbox is greyed out until the page is reloaded with a live device.

    Stores intent, deliberately: a write is never refused because push isn't configured or the
    account has no device yet. That matches the web form, which keeps a stored value it can't honour
    right now, and it's the ordering the app relies on (registration is awaited before this PATCH,
    but a token can still be rejected later).
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
            result = PaymentService.create_mobile_payment(invoice_pk=invoice_pk, user=request.user, request=request)
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


class MobilePaymentAuthorizationView(APIView):
    """GET /api/mobile/payments/authorization/ — seller credentials for warming up Tap to Pay.

    Always 200 for a signed-in user; ``eligible`` says whether they can take payments at all. See
    ``PaymentService.get_payment_authorization`` for what each field means and when credentials are
    (and aren't) issued.

    Hands out the seller's merchant-wide OAuth token, so it carries the same JWT-only auth as
    ``create``/``confirm`` and the same "must be an auction/club admin" gate — a buyer reaching this
    would be handed a credential that can charge cards.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        return Response(PaymentService.get_payment_authorization(request.user), status=status.HTTP_200_OK)


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

    One exception: the native palette this serves injects its own "Lot scanning" and "Tap to Pay"
    rows, so the server's copies are left out here and the user isn't offered each twice. The web
    palette — which the app opens in preference to this one, and which can't inject anything —
    gets them from the same function.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_search"  # interactive search-as-you-type; mobile_api (200/hr) is too tight
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        from auctions import command_palette

        groups = command_palette.search(request, request.GET.get("q", ""), app_deep_links=False)
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


class MobileLastUsedAuctionView(APIView):
    """GET /api/mobile/auctions/last-used/ — the caller's current auction, for client-side gating.

    A read-only, side-effect-free lookup the command palette makes once when it opens, so it can
    decide locally whether to surface the native AR lot-scanning entry: the app computes distance
    from the device's live GPS to this auction's pickup coordinates and only offers AR (near-mode)
    for an in-person auction that isn't ``pretty_much_over``. This deliberately does *not* reuse
    ``checkin/ping/`` — that's a ~500 ft welcome geofence with real side effects (auto-check-in,
    one-shot nudge rows, ``last_auction_used`` writes); here we only report state.

    Always 200. Every field is null when the user has no ``last_auction_used`` or it points at a
    soft-deleted auction — the same "plain when unset/deleted" fallback as ``MyLastAuctionLots``. A
    404 is reserved for older backend builds that predate this endpoint, matching the app's standard
    degrade-on-404 for optional mobile endpoints (``ar/lots``, ``ar/positions``, ``checkin/ping``).

    ``latitude``/``longitude`` come from the auction's single physical pickup location (exactly one
    non-mail ``PickupLocation`` whose coordinates are set); null otherwise — ambiguous/no physical
    location, mail-only, or coordinates left at the ``(0, 0)`` "unset" sentinel the rest of the
    codebase excludes. A null pair tells the app "can't distance-gate, don't show AR near-mode".

    Response 200::

        {
          "slug":             "spring-fry-swap-2026",  // null when unset / deleted
          "title":            "Spring Fry Swap 2026",
          "is_online":        false,
          "pretty_much_over": false,
          "latitude":         40.4406,                 // null if no single physical location
          "longitude":        -79.9959
        }
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
        # PickupLocation coordinates default to (0, 0) rather than null — the codebase's "unset"
        # sentinel (see Auction.physical_location_qs / get_closest_location_distance_subquery, which
        # both exclude latitude=0, longitude=0). Report null there so the app reads it as "no usable
        # location" instead of distance-gating against a real point in the Gulf of Guinea.
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
    """GET /api/mobile/ar/lots/?auction=<slug>&lots=<pk,pk,...> — overlay + card metadata.

    Any authenticated user: returns nothing beyond the public lot page plus the caller's own
    watch/recommendation state. Up to 50 scanned pks per call.
    """

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
    """POST /api/mobile/ar/observations/ — ingest a batch of QR angle sightings.

    Any authenticated user (every scanning attendee is a data source). Junk detections (bad angles,
    stray/removed lots) are dropped silently; the batch still returns 202 with the accepted count.
    """

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
    """POST /api/mobile/ar/events/ — record AR interaction events (scan / zoom / zoom-all-the-way).

    Each event becomes a lot PageView tagged with an ``ar_*`` source, de-duped to one row per user per
    lot per event type, so the lot page can show how many users scanned / zoomed / zoomed all the way
    in — separately from ordinary page views. Foreign/unknown lots are dropped silently; returns 202
    with the accepted count.
    """

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
    """GET /api/mobile/ar/positions/?auction=<slug> — solved positions for not-sold, not-removed lots.

    Any authenticated user (locate mode needs it).
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_ar"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        auction = _get_ar_auction(request.GET.get("auction"))
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ar_service.positions_payload(auction))


class MobileLotWatchView(APIView):
    """POST /api/mobile/lots/<pk>/watch/ — set the caller's watch state on a lot.

    Lets the app watch/unwatch a lot straight from the AR preview card (or anywhere) without opening
    the full web lot page. Mirrors the web ``WatchOrUnwatch`` (JWT auth here instead of session/CSRF)
    and is idempotent: it *sets* the state to the boolean ``watch`` rather than toggling, so a retry
    is harmless. Returns the resulting state so the client can update its star without a re-fetch.

    Request::

        { "watch": true }

    Response 200::

        { "watched": true }
    """

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
    """POST /api/mobile/checkin/ping/ — the phone reports its position; the server decides everything.

    Returns display-ready ``actions`` (join offer / check-in confirmation / admin location offer).
    Never 404s for "nothing nearby" (that would trip the app's endpoint-missing degradation) — an
    empty ``{"actions": []}`` means no nudge right now.
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
    """POST /api/mobile/checkin/join/ — join the auction from the welcome prompt (no scrolling rules).

    Idempotent; auto-checks-in on check-in-mode auctions and returns the bidder number that check-in
    assigned. No distance re-check (the offer already required it and phones drift), but the auction
    must still be inside the welcome window, and 403s when the auction has app self-check-in turned
    off (``Auction.allow_self_checkin``) — those auctions hand out bidder numbers at the door.
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
    """POST /api/mobile/checkin/set-location/ — an admin pins the auction's location from their phone."""

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
    """GET /api/mobile/offline/snapshot/ — the caller's last admin auction + offline-screen data.

    Returns ``auction: null`` (still 200) when the caller administers no auction. A real 404 means
    the deployment predates this endpoint, and the app disables offline mode for the process.
    """

    permission_classes = [IsMobileAuthenticated]
    throttle_scope = "mobile_api"
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        from .services import offline

        auction = offline.get_last_admin_auction(request.user)
        return Response(offline.build_snapshot(auction))


class MobileOfflineSyncView(APIView):
    """POST /api/mobile/offline/sync/ — apply a batch of queued offline ops, then return a snapshot.

    The named auction must belong to the caller (``permission_check``); 403 otherwise. Ops apply in
    order, idempotently and per-op (never all-or-nothing); the response pairs each op's result with a
    fresh snapshot so one round trip both drains the queue and refreshes the phone.
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
