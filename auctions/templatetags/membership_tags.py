import io
import logging

import barcode
from barcode.writer import SVGWriter
from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

register = template.Library()
logger = logging.getLogger(__name__)


@register.simple_tag
def membership_barcode(value, barcode_type="code128"):
    """Render an inline SVG barcode for the given value.

    Uses python-barcode with the SVG writer. Returns an empty string on failure
    so a misconfigured pass cannot break the page.
    """
    if not value:
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
    issuer_id = getattr(settings, "GOOGLE_WALLET_ISSUER_ID", "")
    service_account_email = getattr(settings, "GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL", "")
    private_key = getattr(settings, "GOOGLE_WALLET_SERVICE_ACCOUNT_KEY", "")
    if not (issuer_id and service_account_email and private_key):
        return ""
    try:
        import jwt
    except ImportError:
        logger.warning("PyJWT not available; cannot generate Google Wallet link")
        return ""
    try:
        club = member.club
        class_id = f"{issuer_id}.membership_{club.slug}"
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
            "textModulesData": [
                {"id": "member_id", "header": "Member ID", "body": str(member.membership_number)},
            ],
            "barcode": {
                "type": "CODE_128",
                "value": str(member.membership_number),
                "alternateText": str(member.membership_number),
            },
        }
        if member.membership_expiration_date:
            generic_object["validTimeInterval"] = {
                "end": {"date": f"{member.membership_expiration_date.isoformat()}T23:59:59"}
            }
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
