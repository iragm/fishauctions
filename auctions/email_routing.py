import re
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


#: ``<club-slug>-donations-<10 digits>``. The club slug is carried for readability only -- the
#: digits are what identify the vendor, so a club rename doesn't strand replies in flight.
DONATION_ALIAS_RE = re.compile(r"^(?P<club_slug>.+)-donations-(?P<key>\d{10})$")


def resolve_donation_alias(local_part):
    """Return ``{"vendor": <DonationVendor>}`` for a donation reply address, else None.

    Returns None for a well-formed address whose vendor has been deleted, whose club has since
    turned donation tracking off, or whose club isn't sending donation mail from this site -- all
    of which mean the same thing to the caller: drop it silently.
    """
    match = DONATION_ALIAS_RE.match(local_part or "")
    if not match:
        return None
    DonationVendor = apps.get_model("auctions", "DonationVendor")
    vendor = (
        DonationVendor.objects.filter(routing_key=match.group("key"), is_deleted=False).select_related("club").first()
    )
    if not vendor or not vendor.club.sends_donation_email:
        return None
    return {"vendor": vendor}


def resolve_routing_info(local_part):
    """Return forwarding info for the given alias local-part as a dict, or None.

    Recognised aliases:
    - ``info`` → site admin email
    - ``<club-slug>-auctions`` → oldest non-admin auction manager → oldest admin → site admin
    - ``<club-slug>-contact`` → oldest non-admin membership manager → oldest admin → **drop**
    - ``<club-slug>-donations-<10 digits>`` → the club's donation contact, **or nobody**
    - ``<auction-slug>`` → if club: oldest non-admin auction manager → oldest admin → auction creator;
                           if no club: auction creator directly

    Returns a dict ``{"recipient": <email>, "display_name": <name>}`` when
    the alias is recognised, or ``None`` if the alias does not match any known
    pattern (or the club contact has no configured recipient).
    Callers should treat ``None`` as "drop this message".

    Donation aliases add ``"kind": "donation"`` and ``"vendor_key"``, and are the one case where
    ``recipient`` may be an empty string: the message still needs to be posted back to
    ``/api/v1/email-routing/donation/`` to be recorded, even when no human is forwarded a copy.
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
        routing_email = club.contact_routing_email
        if not routing_email:
            return None
        return {"recipient": routing_email, "display_name": club.name}

    donation = resolve_donation_alias(local_part)
    if donation:
        vendor = donation["vendor"]
        # Unlike every other alias, a donation address is worth answering even with nowhere to
        # forward to: the reply's value is the record kept against the vendor, which the inbound
        # webhook writes. "recipient" may therefore be empty -- the Lambda drops the forward and
        # still posts the body. Clubs are steered towards exactly this setup on the settings page.
        return {
            "recipient": vendor.club.donation_routing_email or "",
            "display_name": vendor.club.name,
            "kind": "donation",
            "vendor_key": vendor.routing_key,
        }

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
