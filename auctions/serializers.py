from rest_framework import serializers

from .models import ClubMember

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
        "permission_admin",
        "permission_view",
        "permission_export",
        "permission_add_edit",
        "permission_edit_club",
        "permission_manage_auctions",
        "permission_manage_bap",
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
    class Meta:
        model = ClubMember
        fields = [
            "id",
            "user",
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
            "uuid",
            "membership_expiration_reminder_due",
            "createdon",
            "source",
            "is_deleted",
            "memo",
        ]
        read_only_fields = ["id", "user", "createdon", "club", "is_deleted"]


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
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)

    class Meta:
        model = ClubMember
        fields = ["id", *CLUB_MEMBER_API_KEY_WRITE_FIELDS, "first_name", "last_name"]

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
            "source",
            "discord_id",
            "discord_username",
        ):
            if data.get(field):
                data[field] = data[field].strip()
        if not self.instance and not data.get("email") and not data.get("name"):
            msg = "Provide at least an email address or a name."
            raise serializers.ValidationError(msg)
        return data


class BapAwardAPIKeyCreateSerializer(serializers.Serializer):
    """Simple serializer for adding BAP points to a club member."""

    points = serializers.IntegerField(min_value=1)
    date = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, data):
        if data.get("notes"):
            data["notes"] = data["notes"].strip()
        return data
