import ipaddress
import logging
import urllib.request

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Single-club auctions always manage participants through the club (it can't be turned off), so new
# ones start in a club-managed mode. We default to "all" (auto-add every member with bidding enabled):
# it's the only mode valid for online auctions, and check-in mode -- while still available as an
# opt-in -- is an extra step that tends to confuse new admins, so it isn't the default.
SINGLE_CLUB_DEFAULT_MANAGE_MODE = "all"

_SERVER_IP_CACHE_KEY = "site_setup_server_public_ip"
_SERVER_IP_CACHE_SECONDS = 60 * 60 * 24  # a server's public IP rarely changes


def single_club_mode_enabled() -> bool:
    return bool(getattr(settings, "SINGLE_CLUB_MODE", False))


def single_club_name() -> str:
    """The single club is named after the site's navbar brand (no separate env var)."""
    return (getattr(settings, "NAVBAR_BRAND", "") or "Default Club").strip() or "Default Club"


def site_paypal_configured() -> bool:
    return bool(getattr(settings, "PAYPAL_CLIENT_ID", "") and getattr(settings, "PAYPAL_SECRET", ""))


def get_server_public_ip() -> str | None:
    """Best-effort lookup of this server's public IP for DNS setup instructions.

    Cached for a day. Returns ``None`` if it can't be determined (e.g. no
    outbound network), in which case callers should fall back to generic text.
    """
    cached = cache.get(_SERVER_IP_CACHE_KEY)
    if cached is not None:
        # Empty string is cached as a sentinel for "looked up, couldn't find it".
        return cached or None
    ip = None
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=2) as response:  # noqa: S310
            candidate = response.read().decode().strip()
        ipaddress.ip_address(candidate)  # raises ValueError if not a valid IP
        ip = candidate
    except Exception:  # noqa: BLE001 - any failure just means we show generic text
        logger.info("Could not determine server public IP for setup checklist", exc_info=True)
    cache.set(_SERVER_IP_CACHE_KEY, ip or "", _SERVER_IP_CACHE_SECONDS)
    return ip


def get_single_club(*, create: bool = False):
    if not single_club_mode_enabled():
        return None

    from .models import Club

    club_name = single_club_name()
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

    from .models import UserData

    try:
        userdata = user.userdata
    except UserData.DoesNotExist:
        return member

    if userdata.club_id != club.pk:
        userdata.club = club
        userdata.save(update_fields=["club"])
    return member
