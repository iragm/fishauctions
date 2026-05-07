from django.utils import timezone

from .models import ClubHistory, ClubMember

INGEST_ALLOWED_FIELDS = {"first_name", "last_name", "email", "phone_number", "address", "memo"}


def map_fields(data: dict, api_key) -> dict:
    """Rename incoming keys using ClubAPIKeyFieldMap records for this api_key."""
    mapping = {m.external_field: m.internal_field for m in api_key.field_mappings.all()}
    return {mapping.get(k, k): v for k, v in data.items()}


def create_club_member_from_api(validated_data: dict, club, api_key):
    """Create a ClubMember from API-validated data, skipping duplicates by email.

    Logs a ClubHistory entry and updates api_key.last_used_at.
    Returns (member, created: bool).
    """
    email = validated_data.get("email", "")
    member = None

    if email:
        member = ClubMember.objects.filter(club=club, email=email, is_deleted=False).first()

    created = member is None
    if created:
        member = ClubMember(club=club, source="api", added_by=None)
        for field, value in validated_data.items():
            if field in INGEST_ALLOWED_FIELDS:
                setattr(member, field, value)
        member.save()

    label = "Added" if created else "Duplicate skipped for"
    ClubHistory.objects.create(
        club=club,
        user=None,
        action=f"{label} member via API ({api_key.name}): {member}",
        applies_to="MEMBERS",
    )

    api_key.last_used_at = timezone.now()
    api_key.save(update_fields=["last_used_at"])

    return member, created
