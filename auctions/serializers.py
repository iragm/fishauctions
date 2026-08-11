from datetime import timezone as date_tz

from rest_framework import serializers

from .models import BapAward, ClubMember, Lot

CLUB_MEMBER_API_KEY_EXCLUDED_FIELDS = frozenset(
    {
        "id",
        "user",
        "club",
        "uuid",
        "createdon",
        "added_by",
        "is_deleted",
        "possible_duplicate",
        "last_discord_role_assigned",
        "discord_role_override",
        "membership_number",
        "source",  # set server-side from the API key name; not caller-writable
        "permission_admin",
        "permission_view",
        "permission_export",
        "permission_add_edit",
        "permission_edit_club",
        "permission_money",
        "permission_manage_auctions",
        "permission_manage_bap",
        "permission_manage_donations",
        "bap_points",
        "hap_points",
        "culture_points",
        "bap_points_ytd",
        "hap_points_ytd",
        "culture_points_ytd",
    }
)
CLUB_MEMBER_API_KEY_WRITE_FIELDS = tuple(
    field.name for field in ClubMember._meta.fields if field.name not in CLUB_MEMBER_API_KEY_EXCLUDED_FIELDS
)
CLUB_MEMBER_API_KEY_MAPPING_FIELDS = (*CLUB_MEMBER_API_KEY_WRITE_FIELDS, "first_name", "last_name")


class ClubMemberSerializer(serializers.ModelSerializer):
    wallet_link = serializers.ReadOnlyField()
    simple_membership_link = serializers.ReadOnlyField()
    # lat/lng are intentionally excluded from the API to protect member location privacy.
    # Only the rounded distance to the club is exposed. Do not add lat/lng here.
    distance_to = serializers.SerializerMethodField()

    def get_distance_to(self, obj):
        """Return distance from club to member in miles, rounded to 10 miles, or null."""
        val = getattr(obj, "distance_to", None)
        return int(val) if val is not None else None

    class Meta:
        model = ClubMember
        fields = [
            "id",
            "club",
            "name",
            "email",
            "email_address_status",
            "phone_number",
            "address",
            "discord_id",
            "bap_points",
            "hap_points",
            "membership_last_paid",
            "membership_expiration_date",
            "membership_expiration_reminder_due",
            "createdon",
            "source",
            "is_deleted",
            "memo",
            "membership_number",
            "wallet_link",
            "simple_membership_link",
            "distance_to",
        ]
        # membership_expiration_date is reported but not writable here: it is set by renewals
        # (the renew endpoint / Renew button) so the ledger and club history always agree with it.
        read_only_fields = [
            "id",
            "createdon",
            "club",
            "is_deleted",
            "membership_number",
            "membership_expiration_date",
        ]


class ClubMemberIngestSerializer(serializers.Serializer):
    """Flexible ingest serializer for API key-authenticated external services.

    Accepts either a single ``name`` field, or ``first_name``/``last_name``
    (which are combined into ``name``). At least one of those, or ``email``,
    must be provided.
    """

    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    memo = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        first = (data.pop("first_name", "") or "").strip()
        last = (data.pop("last_name", "") or "").strip()
        name = (data.get("name", "") or "").strip()
        if not name and (first or last):
            name = f"{first} {last}".strip()
        if name:
            data["name"] = name
        else:
            data.pop("name", None)
        if not data.get("email") and not data.get("name"):
            msg = "Provide at least an email address or a name."
            raise serializers.ValidationError(msg)
        if data.get("email"):
            data["email"] = data["email"].lower().strip()
        for field in ("address", "memo", "phone_number"):
            if data.get(field):
                data[field] = data[field].strip()
        return data


class ClubMemberAPIKeySerializer(serializers.ModelSerializer):
    """Writable serializer for ClubMember records created or updated via API keys."""

    id = serializers.IntegerField(read_only=True)
    # source is set server-side from the API key name and must not be overridden by callers
    source = serializers.CharField(read_only=True)
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)

    class Meta:
        model = ClubMember
        fields = ["id", "source", *CLUB_MEMBER_API_KEY_WRITE_FIELDS, "first_name", "last_name"]

    def validate(self, data):
        first = (data.pop("first_name", "") or "").strip()
        last = (data.pop("last_name", "") or "").strip()
        name = (data.get("name", "") or "").strip()
        if not name and (first or last):
            name = f"{first} {last}".strip()
        if name:
            data["name"] = name
        elif "name" in data:
            data["name"] = ""

        if data.get("email"):
            data["email"] = data["email"].lower().strip()
        for field in (
            "address",
            "memo",
            "phone_number",
            "discord_id",
            "discord_username",
        ):
            if data.get(field):
                data[field] = data[field].strip()
        if not self.instance and not data.get("email") and not data.get("name"):
            msg = "Provide at least an email address or a name."
            raise serializers.ValidationError(msg)
        return data


class BapAwardSummarySerializer(serializers.ModelSerializer):
    """The points already recorded against a lot, nested inside ClubBapLotSerializer."""

    auto_awarded = serializers.SerializerMethodField()

    def get_auto_awarded(self, obj):
        """True when the site awarded these points itself; false when a person did."""
        return obj.awarded_by_id is None

    class Meta:
        model = BapAward
        fields = ["id", "date", "points", "hap_points", "cap_points", "notes", "auto_awarded"]


class ClubBapLotSerializer(serializers.ModelSerializer):
    """Read-only view of one lot from a club's auction, for external BAP systems.

    Everything here is already visible to club admins on the BAP pages; this is the same data in a
    shape an outside breeder-award program can match on (seller/winner email are the join keys),
    plus everything the site knows about whether the lot earns points.
    """

    lot_id = serializers.IntegerField(source="pk", read_only=True)
    # Always a string: Lot.lot_number_display is an int for plain numbering and a string like
    # "101-1" under seller-dash numbering, and a caller shouldn't have to handle both types.
    lot_number_display = serializers.CharField(read_only=True)
    seller_name = serializers.SerializerMethodField()
    seller_email = serializers.SerializerMethodField()
    winner_name = serializers.ReadOnlyField()
    winner_email = serializers.ReadOnlyField()
    # UTC, so every timestamp in the response reads the same regardless of the club's timezone
    timestamp = serializers.DateTimeField(source="date_end", read_only=True, default_timezone=date_tz.utc)
    sold = serializers.ReadOnlyField()
    category = serializers.SerializerMethodField()
    program = serializers.CharField(source="bap_placeholder", read_only=True)
    custom_checkbox_name = serializers.SerializerMethodField()
    bap_eligible = serializers.SerializerMethodField()
    bap_ineligible_reason = serializers.SerializerMethodField()
    bap_ineligible_reason_display = serializers.SerializerMethodField()
    bap_award = serializers.SerializerMethodField()

    # Lot.seller_name / seller_email return the literal "Unknown" when a lot has neither an
    # AuctionTOS nor a user; blank is friendlier for a caller matching on these.
    def get_seller_name(self, obj):
        return "" if obj.seller_name == "Unknown" else obj.seller_name

    def get_seller_email(self, obj):
        return "" if obj.seller_email == "Unknown" else obj.seller_email

    def get_category(self, obj):
        return obj.species_category.name if obj.species_category else ""

    def get_custom_checkbox_name(self, obj):
        """The auction's label for custom_checkbox, so the flag means something to the caller."""
        if obj.auction and obj.auction.use_custom_checkbox_field:
            return obj.auction.custom_checkbox_name or ""
        return ""

    @staticmethod
    def _no_bap_reason(obj):
        """Lot.sold_lot_no_bap_reason hits the database, so evaluate it once per lot."""
        if not hasattr(obj, "_cached_no_bap_reason"):
            obj._cached_no_bap_reason = obj.sold_lot_no_bap_reason
        return obj._cached_no_bap_reason

    def get_bap_eligible(self, obj):
        return self._no_bap_reason(obj) is None

    def get_bap_ineligible_reason(self, obj):
        return self._no_bap_reason(obj) or ""

    def get_bap_ineligible_reason_display(self, obj):
        reason = self._no_bap_reason(obj)
        return dict(Lot.BAP_REASON_CHOICES).get(reason, reason or "")

    def get_bap_award(self, obj):
        try:
            award = obj.bap_award
        except BapAward.DoesNotExist:
            return None
        return BapAwardSummarySerializer(award).data

    class Meta:
        model = Lot
        fields = [
            "lot_id",
            "lot_number_display",
            "lot_name",
            "quantity",
            "seller_name",
            "seller_email",
            "winner_name",
            "winner_email",
            "timestamp",
            "sold",
            "category",
            "program",
            "i_bred_this_fish",
            "donation",
            "custom_checkbox",
            "custom_checkbox_name",
            "bap_eligible",
            "bap_ineligible_reason",
            "bap_ineligible_reason_display",
            "bap_auto_reason",
            "bap_points_awarded",
            "manually_approved",
            "bap_award",
        ]


class BapAwardAPIKeyCreateSerializer(serializers.Serializer):
    """Simple serializer for adding BAP points to a club member."""

    points = serializers.IntegerField(min_value=1)
    date = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, data):
        if data.get("notes"):
            data["notes"] = data["notes"].strip()
        return data
