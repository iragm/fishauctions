"""Request and response shapes for the mobile app's API under ``/api/mobile/``.

App-generated session IDs are char, not UUID: MariaDB's UUID column rejects some variant nibbles.
"""

import math

from django.urls import reverse
from rest_framework import serializers

from auctions.mobile.services.ar import (
    AR_EVENT_TYPES,
    MAX_AR_EVENTS_PER_BATCH,
    MAX_DETECTIONS_PER_FRAME,
    MAX_FRAMES_PER_BATCH,
)
from auctions.mobile.services.offline import MAX_OPS_PER_SYNC
from auctions.mobile.services.social_auth import SUPPORTED_PROVIDERS
from auctions.models import MobileDevice, ObservedPrinter, RemotePrintJob, UserData, UserLabelPrefs

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


class MobileSocialAuthSerializer(serializers.Serializer):
    """POST /api/mobile/auth/social/ — one shape for all three providers.

    Verification lives in ``auctions.mobile.services.social_auth``, not here.
    """

    provider = serializers.ChoiceField(choices=SUPPORTED_PROVIDERS)
    id_token = serializers.CharField(required=False, allow_blank=True, write_only=True)
    access_token = serializers.CharField(required=False, allow_blank=True, write_only=True)
    authorization_code = serializers.CharField(required=False, allow_blank=True, write_only=True)
    # Raw nonce; the provider token holds its sha256.
    nonce = serializers.CharField(required=False, allow_blank=True, max_length=256, write_only=True)
    email = serializers.CharField(required=False, allow_blank=True, max_length=254)
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)


class MobileSocialCompleteSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/auth/social/complete/."""

    pending_token = serializers.CharField(max_length=128, write_only=True)


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
    """A club the user belongs to, for GET /api/mobile/clubs/mine/.

    ``url`` is relative, ``icon_url`` absolute or null; the view annotates ``is_admin``.
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


class MobileDeviceHeartbeatSerializer(serializers.Serializer):
    """POST /api/mobile/devices/heartbeat/ — the phone is awake, and whether it can print.

    ``print_ready`` is the app's own answer (printer paired and profile resolves), not derived from
    ``print_method``.
    """

    device_uuid = serializers.UUIDField()
    print_ready = serializers.BooleanField(required=False, default=False)
    printer_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    # Echoed for display; not what print_ready is computed from.
    print_method = serializers.CharField(required=False, allow_blank=True, default="", max_length=20)


# ---------------------------------------------------------------------------
# Label printing
# ---------------------------------------------------------------------------


class MobileLabelPrefsSerializer(serializers.ModelSerializer):
    """The user's UserLabelPrefs plus ``auctions.printing.label_prefs_warnings``, as on /printing/."""

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


class MobileLabelsPrintedSerializer(serializers.Serializer):
    """POST /api/mobile/labels/printed/ — lots whose labels actually came out, including before a failure."""

    lots = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=True, max_length=1000)
    # Labels sent that didn't come out; they go back to unprinted and are flagged for reprint.
    failed = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True, max_length=1000, default=list
    )
    # Conditions from auctions.printer_programs.STATUS_CONDITIONS, the profiles' vocabulary.
    conditions = serializers.ListField(
        child=serializers.CharField(max_length=40), required=False, allow_empty=True, max_length=20, default=list
    )
    # The app's own words, logged verbatim.
    message = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)

    def validate_conditions(self, value):
        from auctions.printer_programs import STATUS_CONDITIONS

        unknown = [one for one in value if one not in STATUS_CONDITIONS]
        if unknown:
            msg = f"Unknown printer condition(s): {', '.join(sorted(unknown))}. Known: {', '.join(sorted(STATUS_CONDITIONS))}."
            raise serializers.ValidationError(msg)
        return value


class MobileLabelBatchSerializer(serializers.Serializer):
    """POST /api/mobile/labels/batch/ — render a print run; unrendered lots come back as ``remaining``.

    ``resolution`` and ``dpi`` default as on GET labels/<pk>/ (600x400 @ 203dpi).
    """

    lots = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=1000)
    resolution = serializers.CharField(required=False, allow_blank=True, default="", max_length=20)
    dpi = serializers.IntegerField(required=False, allow_null=True, default=None)


class MobileRemotePrintProgressSerializer(serializers.Serializer):
    """POST /api/mobile/printjobs/<uuid>/progress/ — one label went out. Best-effort."""

    status = serializers.ChoiceField(
        choices=[RemotePrintJob.STATUS_PRINTING], required=False, default=RemotePrintJob.STATUS_PRINTING
    )
    printed = serializers.IntegerField(min_value=0, required=False, default=0)
    total = serializers.IntegerField(min_value=0, required=False, default=0)


class MobileRemotePrintResultSerializer(serializers.Serializer):
    """POST /api/mobile/printjobs/<uuid>/result/ — the batch is over. ``message`` is shown verbatim."""

    status = serializers.ChoiceField(choices=[RemotePrintJob.STATUS_PRINTED, RemotePrintJob.STATUS_FAILED])
    printed = serializers.IntegerField(min_value=0, required=False, default=0)
    total = serializers.IntegerField(min_value=0, required=False, default=0)
    message = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class MobileNotificationPrefsSerializer(serializers.ModelSerializer):
    """GET/PATCH /api/mobile/notifications/prefs/ — the app's two push toggles, both optional on write."""

    push_instead_of_email = serializers.BooleanField(source="push_notifications_instead_of_email", required=False)
    push_when_lots_sell = serializers.BooleanField(source="push_notifications_when_lots_sell", required=False)
    running_total = serializers.BooleanField(source="show_running_total_notification", required=False)

    class Meta:
        model = UserData
        fields = ["push_instead_of_email", "push_when_lots_sell", "running_total"]


class PrinterObservationSerializer(serializers.Serializer):
    """POST /api/mobile/printers/observed/ — one pairing.

    Permissive: strings are truncated by the service, and only ``matched_by`` is required.
    """

    ble_name = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    manufacturer = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    model = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    firmware = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    hardware = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    service_uuids = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        allow_empty=True,
        allow_null=True,
        default=list,
    )
    # Empty when the user cancelled the manual dialog.
    profile_slug = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    matched_by = serializers.ChoiceField(choices=[c[0] for c in ObservedPrinter.MATCHED_BY_CHOICES])
    # Reserved for a post-first-print confirmation; not sent yet.
    printed_ok = serializers.BooleanField(required=False, default=False)

    # Probe results, absent when matched without probing. JSONField so unexpected shapes are
    # still recorded; the service caps size and never 400s.
    probe_replies = serializers.JSONField(required=False, allow_null=True, default=dict)
    probed_language = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    gatt = serializers.JSONField(required=False, allow_null=True, default=list)
    status_captures = serializers.JSONField(required=False, allow_null=True, default=dict)
    derived_status_values = serializers.JSONField(required=False, allow_null=True, default=dict)
    status_ambiguities = serializers.JSONField(required=False, allow_null=True, default=list)


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
    # Confirm and the Square webhook bind the payment to the invoice with this (str(invoice.pk)).
    reference_id = serializers.CharField()
    # The SDK's authorize(accessToken, locationId) needs the seller's token on the device.
    access_token = serializers.CharField()
    # Unique per call: the SDK's paymentAttemptId. See TapToPayAttempt.
    attempt_id = serializers.CharField(help_text="Per-attempt id for the on-device charge (paymentAttemptId)")
    # Old name, for older app builds.
    idempotency_key = serializers.CharField(help_text="Deprecated alias of attempt_id")
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


class MobilePaymentAttemptCloseSerializer(serializers.Serializer):
    """POST /api/mobile/payments/attempt/close/ — without it a declined card blocks the retry."""

    attempt_id = serializers.CharField(max_length=45)
    outcome = serializers.ChoiceField(
        choices=("canceled", "failed"),
        help_text="How the attempt ended without capturing. A capture is closed by confirm, not here.",
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
    """POST /api/mobile/command-palette/log/, like the web ``command_palette_log``.

    All optional; ``result`` is a free CharField because ``log_search`` coerces unknown values to
    ``pending``.
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


class FiniteOrNullFloatField(serializers.FloatField):
    """A float where inf or nan becomes None, rather than 400ing the whole AR batch."""

    def to_internal_value(self, data):
        try:
            if not math.isfinite(float(data)):
                return None
        except (TypeError, ValueError, OverflowError):
            pass  # let FloatField raise its own error
        return super().to_internal_value(data)


class ArDetectionSerializer(serializers.Serializer):
    """One QR sighting; angle bounds are checked in the service, which drops junk."""

    lot = serializers.IntegerField()
    bearing_deg = serializers.FloatField()
    depression_deg = serializers.FloatField()
    quality = serializers.FloatField(required=False, default=1.0)


class ArFrameSerializer(serializers.Serializer):
    """All detections in one camera frame, which share a pose."""

    frame_id = serializers.CharField(max_length=32)
    captured_at = serializers.DateTimeField()
    # Cumulative gyro heading (deg, ccw, zero at session start). Null means unknown.
    yaw_deg = FiniteOrNullFloatField(required=False, allow_null=True)
    # Tilt-compensated compass heading, degrees CW from magnetic north. Null means unknown. The
    # server corrects to true north and uses it to orient islands.
    heading_deg = FiniteOrNullFloatField(required=False, allow_null=True)
    # GPS fix (WGS84), both or neither, never (0, 0). Only used to look up magnetic declination.
    latitude = FiniteOrNullFloatField(required=False, allow_null=True)
    longitude = FiniteOrNullFloatField(required=False, allow_null=True)
    # Cumulative dead-reckoning displacement (m) in yaw_deg's frame: +x camera forward at yaw 0,
    # +y to its left. Both or neither; (0, 0) is valid (the origin).
    odo_x_m = FiniteOrNullFloatField(required=False, allow_null=True)
    odo_y_m = FiniteOrNullFloatField(required=False, allow_null=True)
    detections = ArDetectionSerializer(many=True, max_length=MAX_DETECTIONS_PER_FRAME)

    def validate_yaw_deg(self, value):
        # Absurd values from a runaway integrator become unknown.
        if value is not None and abs(value) > 36000:
            return None
        return value

    def validate_heading_deg(self, value):
        # Out-of-range becomes unknown; survivors are normalized to [0, 360).
        if value is None:
            return None
        if not (-360.0 <= value <= 360.0):
            return None
        return value % 360.0

    def validate(self, attrs):
        # Out-of-range, half-supplied or (0, 0) fixes are dropped.
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

        # Half-supplied or over 10 km drops both; (0, 0) is valid.
        ox = attrs.get("odo_x_m")
        oy = attrs.get("odo_y_m")
        odo_bad = (ox is None) != (oy is None) or (ox is not None and (abs(ox) > 10000.0 or abs(oy) > 10000.0))
        if odo_bad:
            attrs["odo_x_m"] = None
            attrs["odo_y_m"] = None
        return attrs


class MobileWatchSerializer(serializers.Serializer):
    watch = serializers.BooleanField()


class ArEventSerializer(serializers.Serializer):
    lot = serializers.IntegerField()
    event = serializers.ChoiceField(choices=AR_EVENT_TYPES)


class ArEventBatchSerializer(serializers.Serializer):
    """POST /api/mobile/ar/events/ — unknown lots are dropped in the service."""

    auction = serializers.CharField()
    events = ArEventSerializer(many=True, max_length=MAX_AR_EVENTS_PER_BATCH)


class ArObservationBatchSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/ar/observations/."""

    auction = serializers.CharField()
    # Opaque client token (see LotObservation.session_id); not validated as a UUID.
    session_id = serializers.CharField(max_length=36)
    # The camera's horizontal FOV; present marks rows fov_calibrated.
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
    """POST /api/mobile/offline/sync/.

    ``ops`` are free-form dicts, checked one at a time in ``auctions.mobile.services.offline`` so one
    bad op doesn't reject the batch. At most ``MAX_OPS_PER_SYNC``.
    """

    auction = serializers.CharField()
    ops = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=True,
        max_length=MAX_OPS_PER_SYNC,
    )
