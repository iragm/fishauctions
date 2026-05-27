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


def resolve_routing_info(local_part):
    """Return forwarding info for the given alias local-part as a dict, or None.

    Recognised aliases:
    - ``info`` → site admin email
    - ``<club-slug>-auctions`` → oldest non-admin auction manager → oldest admin → site admin
    - ``<club-slug>-contact`` → oldest non-admin membership manager → oldest admin → site admin
    - ``<auction-slug>`` → if club: oldest non-admin auction manager → oldest admin → auction creator;
                           if no club: auction creator directly

    Returns a dict ``{"recipient": <email>, "display_name": <name>}`` when
    the alias is recognised, or ``None`` if the alias does not match any known
    pattern (or the club contact has no configured recipient).
    Callers should treat ``None`` as "drop this message".
    """
    local_part = (local_part or "").strip().lower()
    if not local_part:
        return None
    if local_part == "info":
        return {"recipient": admin_routing_email(), "display_name": "Info"}

    Club = apps.get_model("auctions", "Club")
    Auction = apps.get_model("auctions", "Auction")

    if local_part.endswith("-auctions"):
        club_slug = local_part.removesuffix("-auctions")
        club = Club.objects.filter(slug=club_slug).first()
        if not club:
            return None
        return {"recipient": club.auction_routing_email, "display_name": club.name}

    if local_part.endswith("-contact"):
        club_slug = local_part.removesuffix("-contact")
        club = Club.objects.filter(slug=club_slug).first()
        if not club:
            return None
        return {"recipient": club.contact_routing_email, "display_name": club.name}

    auction = Auction.objects.filter(slug=local_part, is_deleted=False).select_related("created_by", "club").first()
    if auction:
        # If the auction belongs to a club, route through the club's auction recipient
        # (non-admin auction manager first, then admin, then auction creator).
        if auction.club:
            recipient = auction.club.auction_email_recipient
            if recipient and recipient.routing_email:
                return {"recipient": recipient.routing_email, "display_name": auction.title}
        # Fall back to the auction creator's email
        if auction.created_by and auction.created_by.email:
            return {"recipient": auction.created_by.email, "display_name": auction.title}

    return None


def resolve_routed_recipient(local_part):
    """Return the forwarding email address for the given alias local-part, or None.

    Thin wrapper around :func:`resolve_routing_info` for callers that only
    need the recipient address.
    """
    info = resolve_routing_info(local_part)
    return info["recipient"] if info else None
