from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings


def email_routing_enabled():
    return bool(getattr(settings, "SES_ROUTE_EMAILS_ENABLED", False))


def email_routing_domain():
    domain = (getattr(settings, "EMAIL_ROUTING_DOMAIN", "") or getattr(settings, "SITE_DOMAIN", "") or "").strip()
    if not domain:
        return ""
    parsed = urlsplit(domain if "://" in domain else f"//{domain}")
    return (parsed.hostname or domain).strip().lower()


def build_routed_sender_address(local_part):
    if not email_routing_enabled():
        return None
    domain = email_routing_domain()
    local_part = (local_part or "").strip().lower()
    if not domain or not local_part:
        return None
    return f"{local_part}@{domain}"


def admin_routing_email():
    admins = getattr(settings, "ADMINS", [])
    if admins:
        return admins[0][1]
    return getattr(settings, "DEFAULT_FROM_EMAIL", "")


def resolve_routed_recipient(local_part):
    local_part = (local_part or "").strip().lower()
    if not local_part:
        return None
    if local_part == "info":
        return admin_routing_email()

    Club = apps.get_model("auctions", "Club")
    Auction = apps.get_model("auctions", "Auction")

    if local_part.endswith("-auctions"):
        club_slug = local_part.removesuffix("-auctions")
        club = Club.objects.filter(slug=club_slug).first()
        return club.auction_routing_email if club else admin_routing_email()

    if local_part.endswith("-memberships"):
        club_slug = local_part.removesuffix("-memberships")
        club = Club.objects.filter(slug=club_slug).first()
        return club.membership_routing_email if club else admin_routing_email()

    auction = Auction.objects.filter(slug=local_part).select_related("created_by").first()
    if auction and auction.created_by and auction.created_by.email:
        return auction.created_by.email

    return admin_routing_email()
