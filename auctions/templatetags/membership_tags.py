import io
import logging

from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

# python-barcode is an optional runtime dep. Import lazily-but-eagerly here and
# tolerate ImportError so a missing wheel doesn't bring down everything that
# loads this template-tag library (e.g. the Celery worker's Django checks).
try:
    import barcode
    from barcode.writer import SVGWriter
except ImportError:  # pragma: no cover
    barcode = None
    SVGWriter = None

register = template.Library()
logger = logging.getLogger(__name__)


@register.simple_tag
def membership_barcode(value, barcode_type="code128"):
    """Render an inline SVG barcode for the given value.

    Uses python-barcode with the SVG writer. Returns an empty string on failure
    so a misconfigured pass cannot break the page.
    """
    if not value or barcode is None:
        return ""
    try:
        cls = barcode.get_barcode_class(barcode_type)
        # write_text=False hides the human-readable text under the bars; we render
        # the number ourselves with consistent typography in the template.
        buf = io.BytesIO()
        cls(str(value), writer=SVGWriter()).write(buf, options={"write_text": False, "module_height": 12.0})
        svg = buf.getvalue().decode("utf-8")
        # Strip the XML declaration so the SVG can be embedded directly in HTML.
        if svg.startswith("<?xml"):
            svg = svg.split("?>", 1)[1]
        return mark_safe(svg)
    except Exception:
        logger.exception("Failed to generate barcode for value=%r", value)
        return ""


@register.simple_tag
def google_wallet_save_url(member):
    """Return a 'Save to Google Wallet' URL for this member, or empty string.

    Requires the following Django settings to be set:
      GOOGLE_WALLET_ISSUER_ID            — numeric issuer ID from Google Wallet Console
      GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL — the issuer service account email
      GOOGLE_WALLET_SERVICE_ACCOUNT_KEY   — the PEM-encoded RSA private key

    If any setting is missing the tag returns "" so the template can hide the button.
    """
    if not member:
        return ""
    # Respect the club's per-mode visibility — when membership numbers are off
    # entirely, or restricted to paid members and this member isn't paid, no
    # Google Wallet URL should be exposed.
    if not member.club.show_member_barcode:
        return ""
    from auctions.google_wallet import (
        _object_visuals,
        is_configured,
        member_text_modules,
        member_valid_time_interval,
    )

    if not is_configured():
        return ""
    issuer_id = settings.GOOGLE_WALLET_ISSUER_ID
    service_account_email = settings.GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL
    private_key = settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY
    import jwt

    try:
        club = member.club
        # Wallet class IDs are immutable, so we use club.pk (stable) instead of
        # club.slug (mutable via AutoSlugField with always_update=True).
        class_id = f"{issuer_id}.membership_{club.pk}"
        object_id = f"{issuer_id}.member_{member.pk}"
        member_name = member.name or (member.user.get_full_name() or member.user.username if member.user else "Member")
        generic_object = {
            "id": object_id,
            "classId": class_id,
            "state": "ACTIVE",
            "cardTitle": {
                "defaultValue": {"language": "en-US", "value": club.name},
            },
            "header": {
                "defaultValue": {"language": "en-US", "value": "Membership"},
            },
            "subheader": {
                "defaultValue": {"language": "en-US", "value": member_name},
            },
            "textModulesData": member_text_modules(member),
            "barcode": {
                "type": "CODE_128",
                "value": str(member.membership_number),
                "alternateText": str(member.membership_number),
            },
            **_object_visuals(club, expired=member.wallet_status_is_expired),
        }
        valid_time_interval = member_valid_time_interval(member)
        if valid_time_interval:
            generic_object["validTimeInterval"] = valid_time_interval
        payload = {
            "iss": service_account_email,
            "aud": "google",
            "typ": "savetowallet",
            "origins": [],
            "payload": {"genericObjects": [generic_object]},
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        return f"https://pay.google.com/gp/v/save/{token}"
    except Exception:
        logger.exception("Failed to build Google Wallet save URL for member %s", getattr(member, "pk", None))
        return ""
