import re
from email.utils import formataddr
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


def sender_with_display_name(display_name, address):
    """``Some Club <club-slug-contact@example.com>`` -- the From line as a person reads it.

    Gmail shows the display name and hides the address, so without one the From reads as a slug.
    ``formataddr`` rather than an f-string, because a club called ``Bob's "Fish" Club`` written into a
    header is a malformed From and the client loses the address behind it.

    Returns None when there is no routed address, which post_office reads as "use DEFAULT_FROM_EMAIL".
    """
    if not address:
        return None
    name = " ".join((display_name or "").split())
    if not name:
        return address
    return formataddr((name, address))


def admin_routing_email():
    admins = getattr(settings, "ADMINS", [])
    if admins:
        return admins[0][1]
    return getattr(settings, "DEFAULT_FROM_EMAIL", "")


def _is_on_routing_domain(address):
    """Whether mail to *address* would come straight back in through the inbound Lambda."""
    domain = email_routing_domain()
    return bool(domain) and address.rsplit("@", 1)[-1].strip().lower() == domain


#: ``<club-slug>-donations-<10 digits>``. The slug is for readability; the digits identify the
#: vendor, so a club rename doesn't strand replies in flight.
DONATION_ALIAS_RE = re.compile(r"^(?P<club_slug>.+)-donations-(?P<key>\d{10})$")


def resolve_donation_alias(local_part):
    """``{"vendor": <DonationVendor>}`` for a donation reply address, else None.

    None for a well-formed address whose vendor is gone, whose club turned donation tracking off, or
    whose club isn't sending donation mail from here -- all of which mean "drop it silently".
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
    """Forwarding info for an alias local-part, or None ("drop this message").

    Recognised aliases:

    - ``info``, ``support`` -> site admin
    - ``dmca`` -> the designated copyright agent (:mod:`auctions.dmca`), or the site admin when the
      agent address is itself on this domain
    - ``<club-slug>-auctions`` -> oldest non-admin auction manager, then admin, then site admin
    - ``<club-slug>-contact`` -> oldest non-admin membership manager, then admin, else drop
    - ``<club-slug>-donations-<10 digits>`` -> the club's donation contact, or nobody
    - ``<auction-slug>`` -> the club's auction manager, else the auction creator

    Returns ``{"recipient": ..., "display_name": ...}``. Donation aliases add ``"kind"`` and
    ``"vendor_key"``, and are the one case where ``recipient`` may be empty: the message still has to be
    posted to ``/api/v1/email-routing/donation/`` to be recorded.
    """
    local_part = (local_part or "").strip().lower()
    if not local_part:
        return None
    if local_part in ("info", "support"):
        return {"recipient": admin_routing_email(), "display_name": local_part.capitalize()}
    if local_part == "dmca":
        # The address published in the Copyright Office's public directory gets scraped, so it is an
        # alias rather than a mailbox, and re-pointing it is an .env edit rather than a $6 filing.
        # It must resolve to something: an agent address that drops mail is how AOL lost the safe
        # harbour in Ellison v. Robertson.
        #
        # DMCA_AGENT_EMAIL is normally this alias, and forwarding it to itself sends the copy back
        # through SES, where the Lambda's loop guard drops it. So a published address on this domain
        # means "the site admin".
        from auctions.dmca import agent_email

        recipient = agent_email()
        if not recipient or _is_on_routing_domain(recipient):
            recipient = admin_routing_email()
        return {"recipient": recipient, "display_name": "Copyright agent"}

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
        # Unlike every other alias, a donation address is worth answering with nowhere to forward
        # to: the reply's value is the record kept against the vendor. Clubs are steered towards
        # exactly this setup on the settings page.
        return {
            "recipient": vendor.club.donation_routing_email or "",
            "display_name": vendor.club.name,
            "kind": "donation",
            "vendor_key": vendor.routing_key,
        }

    auction = Auction.objects.filter(slug=local_part, is_deleted=False).select_related("created_by", "club").first()
    if auction:
        # A club auction routes through the club's recipient.
        if auction.club:
            recipient = auction.club.auction_email_recipient
            if recipient and recipient.routing_email:
                return {"recipient": recipient.routing_email, "display_name": auction.title}
        # Fall back to the auction creator's email
        if auction.created_by and auction.created_by.email:
            return {"recipient": auction.created_by.email, "display_name": auction.title}

    return None


def resolve_routed_recipient(local_part):
    """The forwarding address for an alias local-part, or None: a thin wrapper around
    :func:`resolve_routing_info`.
    """
    info = resolve_routing_info(local_part)
    return info["recipient"] if info else None
