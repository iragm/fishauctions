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
# Labels
# ---------------------------------------------------------------------------


class LabelDataSerializer(serializers.Serializer):
    """Inner label_data returned by GET /api/mobile/labels/<pk>/."""

    lot_number = serializers.CharField()
    title = serializers.CharField()
    quantity = serializers.IntegerField()
    minimum_bid = serializers.CharField()
    buy_now_price = serializers.CharField(allow_null=True)
    seller = serializers.CharField()
    auction = serializers.CharField(allow_null=True)
    category = serializers.CharField(allow_null=True)
    i_bred_this_fish = serializers.BooleanField()
    custom_field_1 = serializers.CharField(allow_null=True)


class LabelMetadataSerializer(serializers.Serializer):
    """Inner metadata returned by GET /api/mobile/labels/<pk>/."""

    generated_at = serializers.CharField()
    lot_pk = serializers.IntegerField()
    supported_formats = serializers.ListField(child=serializers.CharField())


class LotLabelResponseSerializer(serializers.Serializer):
    """Full response from GET /api/mobile/labels/<pk>/."""

    label_data = LabelDataSerializer()
    metadata = LabelMetadataSerializer()


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
    idempotency_key = serializers.CharField()
    square_application_id = serializers.CharField()
    square_environment = serializers.CharField()


class MobilePaymentConfirmSerializer(serializers.Serializer):
    """Request body for POST /api/mobile/payments/confirm/."""

    invoice_pk = serializers.IntegerField(min_value=1)
    source_id = serializers.CharField(help_text="Nonce returned by the Square mobile SDK")
    idempotency_key = serializers.CharField(help_text="Key from the create response — must match")


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
