from django.urls import reverse
from rest_framework import serializers

from auctions.models import MobileDevice

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

    class Meta:
        model = MobileDevice
        fields = [
            "id",
            "device_uuid",
            "device_name",
            "platform",
            "app_version",
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
