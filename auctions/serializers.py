from rest_framework import serializers

from .models import ClubMember


class ClubMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClubMember
        fields = [
            "id",
            "user",
            "club",
            "first_name",
            "last_name",
            "email",
            "email_address_status",
            "phone_number",
            "address",
            "discord_id",
            "bap_points",
            "hap_points",
            "membership_last_paid",
            "createdon",
            "source",
            "is_deleted",
            "memo",
        ]
        read_only_fields = ["id", "user", "createdon", "club", "is_deleted"]


class ClubMemberIngestSerializer(serializers.Serializer):
    """Flexible ingest serializer for API key-authenticated external services."""

    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    memo = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        if not data.get("email") and not data.get("first_name") and not data.get("last_name"):
            msg = "Provide at least an email address or a name."
            raise serializers.ValidationError(msg)
        if data.get("email"):
            data["email"] = data["email"].lower().strip()
        for field in ("first_name", "last_name", "address", "memo", "phone_number"):
            if data.get(field):
                data[field] = data[field].strip()
        return data
