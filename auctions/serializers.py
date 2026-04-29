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
