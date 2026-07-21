import math

from django.urls import reverse
from rest_framework import serializers

from auctions.mobile.services.ar import MAX_DETECTIONS_PER_FRAME, MAX_FRAMES_PER_BATCH
from auctions.mobile.services.offline import MAX_OPS_PER_SYNC
from auctions.models import MobileDevice, UserLabelPrefs

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class MobileLoginSerializer(serializers.Serializer):
    """Credentials accepted by POST /api/mobile/auth/login/."""

    credential = serializers.CharField(
        help_text="Username or email address",
    )
    password = serializers.CharField(write_only=True)


class MobileGoogleAuthSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/auth/google/."""

    id_token = serializers.CharField(write_only=True, help_text="Google ID token from the client-side sign-in flow")


class MobileUserSerializer(serializers.Serializer):
    """Read-only user profile returned by GET /api/mobile/auth/me/."""

    id = serializers.IntegerField(source="pk")
    username = serializers.CharField()
    email = serializers.EmailField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    is_staff = serializers.BooleanField()
    date_joined = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# Clubs
# ---------------------------------------------------------------------------


class MobileClubSerializer(serializers.Serializer):
    """A club the authenticated user belongs to, for GET /api/mobile/clubs/mine/.

    ``url`` is the server-relative web club page (opened in the WebView); ``icon_url`` is an
    absolute URL (or null) so the app can load the square club logo directly. ``is_admin`` reflects
    a ClubMember row with permission_admin for this user — the view annotates each Club with it.
    """

    name = serializers.CharField()
    slug = serializers.CharField()
    url = serializers.SerializerMethodField()
    icon_url = serializers.SerializerMethodField()
    is_admin = serializers.BooleanField()

    def get_url(self, club):
        return reverse("club_detail", kwargs={"slug": club.slug})

    def get_icon_url(self, club):
        if not club.icon:
            return None
        url = club.icon.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class MobileDeviceSerializer(serializers.ModelSerializer):
    """Serialiser for MobileDevice registration / update."""

    device_uuid = serializers.UUIDField()
    # FCM registration token. Optional so an app build without push still registers cleanly.
    fcm_token = serializers.CharField(required=False, allow_blank=True, default="")

    class Meta:
        model = MobileDevice
        fields = [
            "id",
            "device_uuid",
            "device_name",
            "platform",
            "app_version",
            "fcm_token",
            "created_at",
            "last_seen",
        ]
        read_only_fields = ["id", "created_at", "last_seen"]

    def validate_platform(self, value):
        allowed = {c[0] for c in MobileDevice.PLATFORM_CHOICES}
        if value and value not in allowed:
            msg = f"platform must be one of: {', '.join(sorted(allowed))}"
            raise serializers.ValidationError(msg)
        return value


class MobileDeviceUnregisterSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/devices/unregister/."""

    device_uuid = serializers.UUIDField()


# ---------------------------------------------------------------------------
# Label printing
# ---------------------------------------------------------------------------


class MobileLabelPrefsSerializer(serializers.ModelSerializer):
    """The user's UserLabelPrefs plus the computed mismatch warnings.

    Used by GET/PATCH /api/mobile/labels/prefs/. ``warnings`` is read-only and comes from the same
    ``auctions.printing.label_prefs_warnings`` the web /printing/ page uses, so app and web agree.
    """

    warnings = serializers.SerializerMethodField()

    class Meta:
        model = UserLabelPrefs
        fields = [
            "print_method",
            "preset",
            "unit",
            "label_width",
            "label_height",
            "empty_labels",
            "print_border",
            "warnings",
        ]

    def get_warnings(self, obj):
        from auctions.printing import label_prefs_warnings

        return label_prefs_warnings(obj)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class MobilePaymentCreateSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/payments/create/."""

    invoice_pk = serializers.IntegerField(min_value=1)


class MobilePaymentCreateResponseSerializer(serializers.Serializer):
    """Response from POST /api/mobile/payments/create/."""

    invoice_pk = serializers.IntegerField()
    amount = serializers.CharField(help_text="Decimal string, e.g. '15.00'")
    currency = serializers.CharField()
    location_id = serializers.CharField()
    # The client must charge with this reference_id so confirm (and the Square webhook) can bind the
    # payment to the invoice. Matches the web convention (str(invoice.pk)).
    reference_id = serializers.CharField()
    # The Mobile Payments SDK authorizes on-device with authorize(accessToken, locationId), so we
    # ship the seller's OAuth access token to the device by design (the SDK requires it).
    access_token = serializers.CharField()
    # Stable, invoice-derived (NOT random) so a retried create -> tap for the same invoice dedupes to a
    # single on-device Square charge instead of double-charging.
    idempotency_key = serializers.CharField(help_text="Stable per-invoice idempotency key for the on-device charge")
    square_environment = serializers.CharField()


class MobilePaymentConfirmSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/payments/confirm/."""

    invoice_pk = serializers.IntegerField(min_value=1)
    payment_id = serializers.CharField(help_text="Square payment id from the on-device Tap to Pay charge")
    idempotency_key = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Key from the create response. Accepted for contract compatibility only; the charge is "
            "verified by payment_id against Square and this is not used to charge."
        ),
    )


class MobilePaymentConfirmResponseSerializer(serializers.Serializer):
    """Response from POST /api/mobile/payments/confirm/."""

    payment_id = serializers.CharField()
    status = serializers.CharField()
    receipt_number = serializers.CharField(allow_null=True)


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------


class CommandPaletteLogSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/command-palette/log/.

    Mirrors the web ``command_palette_log`` view: every field is optional so the client can
    upsert a single search-session row as the query is refined, then finalise it as ``clicked``
    / ``abandoned`` / ``bounce``. ``result`` is intentionally a free CharField (not a ChoiceField):
    ``command_palette.log_search`` coerces any unknown value to ``pending``, matching the web's
    leniency and keeping the contract forward-compatible.
    """

    id = serializers.IntegerField(
        required=False, allow_null=True, help_text="pk of the in-progress search row, from a previous log response"
    )
    search = serializers.CharField(required=False, allow_blank=True, default="")
    result = serializers.CharField(
        required=False, allow_blank=True, default="", help_text="pending | bounce | clicked | abandoned"
    )
    result_type = serializers.CharField(required=False, allow_blank=True, default="")
    result_url = serializers.CharField(required=False, allow_blank=True, default="")
    result_object_id = serializers.IntegerField(required=False, allow_null=True)


# ---------------------------------------------------------------------------
# AR lot scanning
# ---------------------------------------------------------------------------


class ArDetectionSerializer(serializers.Serializer):
    """One QR sighting inside a camera frame. Angle bounds are checked in the service (a junk
    detection is dropped, not a 400), so only structure is validated here."""

    lot = serializers.IntegerField()
    bearing_deg = serializers.FloatField()
    depression_deg = serializers.FloatField()
    quality = serializers.FloatField(required=False, default=1.0)


class ArFrameSerializer(serializers.Serializer):
    """All detections seen in a single camera frame (they share a pose, so they constrain lots
    relative to each other)."""

    frame_id = serializers.CharField(max_length=32)
    captured_at = serializers.DateTimeField()
    # Phone's integrated gyro heading at capture (deg, ccw-positive about gravity, zero at session
    # start, cumulative/unwrapped). Absent/null ⇒ no gyro data ("unknown", never "didn't turn"); the
    # solver uses it as heading odometry between frames.
    yaw_deg = serializers.FloatField(required=False, allow_null=True)
    # Phone's absolute compass heading at capture: degrees CW from MAGNETIC north (0=N, 90=E),
    # tilt-compensated, for the camera's forward axis. Absent/null ⇒ no compass reading ("unknown").
    # The server corrects magnetic→true (WMM declination) and uses it to fix each island's absolute
    # orientation. Unlike yaw_deg (relative gyro odometry), this is an absolute bearing.
    heading_deg = serializers.FloatField(required=False, allow_null=True)
    # Phone GPS fix at capture (WGS84 degrees). Send both or neither; absent/null ⇒ no fix. Used only
    # to anchor disconnected islands' base locations, so a coarse fix is fine — but the app should omit
    # them (or send null) when it has no location permission or no fix, rather than sending (0, 0).
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    detections = ArDetectionSerializer(many=True, max_length=MAX_DETECTIONS_PER_FRAME)

    def validate_yaw_deg(self, value):
        # Junk guard: a runaway integrator can report absurd values; drop them (treat as "unknown")
        # rather than 400 the whole batch.
        if value is not None and abs(value) > 36000:
            return None
        return value

    def validate_heading_deg(self, value):
        # Same "junk is dropped, never 400" philosophy as yaw: null passes through, and any
        # non-finite (json accepts NaN/Infinity literals) or wildly out-of-range value is discarded
        # as "unknown". A survivor is normalized to [0, 360) since it is an absolute compass bearing.
        if value is None:
            return None
        if not math.isfinite(value) or not (-360.0 <= value <= 360.0):
            return None
        return value % 360.0

    def validate(self, attrs):
        # GPS is all-or-nothing and must be in range; an out-of-range or half-supplied fix is dropped
        # (treated as "no fix") rather than 400-ing the batch, and (0, 0) is the classic "no fix"
        # sentinel so we discard it too.
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        bad = (
            lat is None
            or lon is None
            or not (-90.0 <= lat <= 90.0)
            or not (-180.0 <= lon <= 180.0)
            or (lat == 0.0 and lon == 0.0)
        )
        if bad:
            attrs["latitude"] = None
            attrs["longitude"] = None
        return attrs


class MobileWatchSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/lots/<pk>/watch/ — set (not toggle) the caller's watch state."""

    watch = serializers.BooleanField()


class ArObservationBatchSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/ar/observations/."""

    auction = serializers.CharField()
    # Opaque client-generated grouping token — see LotObservation.session_id. Accept it as a plain
    # string (not a UUIDField): the app doesn't guarantee RFC-4122 variant bits, and the DB column is
    # a varchar, so validating/normalizing it as a UUID here would only reintroduce breakage.
    session_id = serializers.CharField(max_length=36)
    # Device-reported horizontal camera FOV the bearings were computed against. Present ⇒ the batch's
    # rows are marked fov_calibrated (tighter bearing σ in the solver); absent ⇒ assumed-FOV fallback.
    fov_hdeg = serializers.FloatField(required=False, allow_null=True)
    frames = ArFrameSerializer(many=True, max_length=MAX_FRAMES_PER_BATCH)


# ---------------------------------------------------------------------------
# Proximity check-in & welcome
# ---------------------------------------------------------------------------


class CheckinPingSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/checkin/ping/ — the phone's current position."""

    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)


class CheckinJoinSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/checkin/join/."""

    auction = serializers.CharField()


class CheckinSetLocationSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/checkin/set-location/."""

    auction = serializers.CharField()
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)


# ---------------------------------------------------------------------------
# Offline mode (in-person sale)
# ---------------------------------------------------------------------------


class OfflineSyncSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/offline/sync/.

    ``ops`` is deliberately validated loosely — as a list of free-form dicts — not with a per-type
    serializer. The contract is per-op and never all-or-nothing: a malformed or conflicting op must
    still let the rest of the batch apply, so structure/semantics are checked op-by-op in
    ``auctions.mobile.services.offline`` (which returns a per-op status), not rejected here. Only the
    batch envelope is enforced: an auction slug and at most ``MAX_OPS_PER_SYNC`` ops (the app chunks).
    """

    auction = serializers.CharField()
    ops = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=True,
        max_length=MAX_OPS_PER_SYNC,
    )
