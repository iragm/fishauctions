import logging

from django.conf import settings

logger = logging.getLogger(__name__)

SINGLE_CLUB_MANAGE_MODE_CHOICES = {"all", "checkin"}


def single_club_mode_enabled() -> bool:
    return bool(getattr(settings, "SINGLE_CLUB_MODE", False))


def single_club_manage_mode() -> str:
    mode = (getattr(settings, "SINGLE_CLUB_MANAGE_MODE", "checkin") or "checkin").strip().lower()
    if mode not in SINGLE_CLUB_MANAGE_MODE_CHOICES:
        return "checkin"
    return mode


def site_paypal_configured() -> bool:
    return bool(getattr(settings, "PAYPAL_CLIENT_ID", "") and getattr(settings, "PAYPAL_SECRET", ""))


def get_single_club(*, create: bool = False):
    if not single_club_mode_enabled():
        return None

    from .models import Club

    club_name = (getattr(settings, "SINGLE_CLUB_NAME", "") or "Default Club").strip() or "Default Club"
    club = Club.objects.filter(name=club_name).order_by("pk").first()
    if not club and create:
        club = Club.objects.create(
            name=club_name,
            allow_joining=True,
            enable_club_page=True,
            enable_membership=True,
            use_site_paypal_account=site_paypal_configured(),
            allow_non_oauth_paypal=False,
        )
    if not club:
        return None

    update_fields = []
    desired_values = {
        "allow_joining": True,
        "enable_club_page": True,
        "enable_membership": True,
        "allow_non_oauth_paypal": False,
        "use_site_paypal_account": site_paypal_configured(),
    }
    for field_name, desired_value in desired_values.items():
        if getattr(club, field_name) != desired_value:
            setattr(club, field_name, desired_value)
            update_fields.append(field_name)
    if update_fields:
        club.save(update_fields=update_fields)
    return club


def ensure_single_club_membership_for_user(user):
    if not user or not single_club_mode_enabled():
        return None

    club = get_single_club(create=False)
    if not club:
        return None

    from .models import ClubMember

    member = ClubMember.objects.filter(club=club, user=user, is_deleted=False).order_by("pk").first()
    if not member:
        member = ClubMember.objects.create(
            club=club,
            user=user,
            name=(user.get_full_name() or user.username or user.email or "").strip(),
            email=user.email or "",
            source="single_club_mode",
        )
    else:
        update_fields = []
        desired_name = (user.get_full_name() or user.username or user.email or "").strip()
        if desired_name and member.name != desired_name:
            member.name = desired_name
            update_fields.append("name")
        if user.email and member.email != user.email:
            member.email = user.email
            update_fields.append("email")
        if update_fields:
            member.save(update_fields=update_fields)

    try:
        userdata = user.userdata
    except Exception:
        return member

    if userdata.club_id != club.pk:
        userdata.club = club
        userdata.save(update_fields=["club"])
    return member
