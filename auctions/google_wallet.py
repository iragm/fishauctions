"""Helpers for the Google Wallet REST API.

Does the OAuth2 JWT-bearer token dance against ``https://oauth2.googleapis.com/token`` with PyJWT
and ``requests``, so ``google-auth`` isn't a dependency.

Entry points: ``is_configured()``, ``get_access_token()`` (cached), ``create_generic_class(club)``.
"""

from __future__ import annotations

import logging
import threading
import time

import jwt
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - not a secret
WALLET_API_BASE = "https://walletobjects.googleapis.com/walletobjects/v1"
ISSUER_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"

# Neutral dark, readable with white text.
DEFAULT_HEX_BG = "#1f2937"
# A lapsed membership tints the whole card: Wallet can't colour one field red.
EXPIRED_HEX_BG = "#991b1b"

_token_lock = threading.Lock()
_cached_token: dict = {"value": None, "expires_at": 0.0}


def is_configured() -> bool:
    return bool(
        getattr(settings, "GOOGLE_WALLET_ISSUER_ID", "")
        and getattr(settings, "GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL", "")
        and getattr(settings, "GOOGLE_WALLET_SERVICE_ACCOUNT_KEY", "")
    )


def _build_assertion() -> str:
    now = int(time.time())
    payload = {
        "iss": settings.GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL,
        "scope": ISSUER_SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(payload, settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY, algorithm="RS256")


def get_access_token() -> str | None:
    """A cached OAuth2 access token, refreshed on demand, or None when Wallet isn't configured.

    Tokens last an hour; this caches slightly less, with a 60s margin.
    """
    if not is_configured():
        return None
    with _token_lock:
        now = time.time()
        if _cached_token["value"] and _cached_token["expires_at"] - 60 > now:
            return _cached_token["value"]
        assertion = _build_assertion()
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        _cached_token["value"] = body["access_token"]
        _cached_token["expires_at"] = now + float(body.get("expires_in", 3600))
        return _cached_token["value"]


def _class_id_for_club(club) -> str:
    return f"{settings.GOOGLE_WALLET_ISSUER_ID}.membership_{club.pk}"


def _absolute_icon_url(club) -> str:
    """A publicly reachable https URL for the club's icon, or "" if there is none.

    Google only accepts public https URLs for `logo.sourceUri.uri`, so this builds one from the current
    Site domain. An http-only deployment still gets https, which Google rejects -- a config problem, not
    a runtime one.
    """
    if not getattr(club, "icon", None):
        return ""
    try:
        from django.contrib.sites.models import Site

        from auctions import cloudflare_images

        cloudflare_url = cloudflare_images.image_url(None, club.cloudflare_image_id, "google_wallet_logo")
        if cloudflare_url:
            # imagedelivery.net URLs are already absolute and publicly reachable
            return cloudflare_url
        from easy_thumbnails.files import get_thumbnailer

        thumbnailer = get_thumbnailer(club.icon)
        thumb = thumbnailer["google_wallet_logo"]
        domain = Site.objects.get_current().domain
        return f"https://{domain}{thumb.url}"
    except Exception:
        logger.exception("Could not build icon URL for club %s", club.pk)
        return ""


def _class_body(club) -> dict:
    # Per Google's docs, `logo` and `hexBackgroundColor` are not GenericClass fields -- they live on
    # GenericObject, and setting them here is silently ignored. See _object_visuals().
    #
    # cardTemplateOverride replaces the default layout: only rows listed here show on the card front,
    # and the membership_status row is what makes "Valid through ..." visible. Class changes reach
    # Google only when re-pushed -- run `manage.py sync_google_wallet_classes`.
    return {
        "id": _class_id_for_club(club),
        "classTemplateInfo": {
            "cardTemplateOverride": {
                "cardRowTemplateInfos": [
                    {
                        "oneItem": {
                            "item": {"firstValue": {"fields": [{"fieldPath": "object.textModulesData['member_id']"}]}}
                        }
                    },
                    {
                        "oneItem": {
                            "item": {
                                "firstValue": {"fields": [{"fieldPath": "object.textModulesData['membership_status']"}]}
                            }
                        }
                    },
                ]
            }
        },
    }


def _object_visuals(club, expired: bool = False) -> dict:
    """Logo and background fields for a GenericObject, derived from the club.

    These belong on the per-member object, so they go into both the save-to-wallet JWT and any later
    PATCH. ``expired`` tints the card red.
    """
    visuals: dict = {"hexBackgroundColor": EXPIRED_HEX_BG if expired else DEFAULT_HEX_BG}
    icon_url = _absolute_icon_url(club)
    if icon_url:
        visuals["logo"] = {
            "sourceUri": {"uri": icon_url},
            "contentDescription": {"defaultValue": {"language": "en-US", "value": f"{club.name} logo"}},
        }
    return visuals


def _status_text_module(member) -> dict | None:
    """A 'Membership' text module with the member's wallet status line, or None when the club doesn't run
    memberships.
    """
    status = member.wallet_status_text
    if not status:
        return None
    return {"id": "membership_status", "header": "Membership", "body": status}


def member_text_modules(member) -> list:
    """textModulesData for a member's GenericObject: member ID, plus status where it applies."""
    modules = [{"id": "member_id", "header": "Member ID", "body": str(member.membership_number)}]
    status_module = _status_text_module(member)
    if status_module:
        modules.append(status_module)
    return modules


def _object_id_for_member(member) -> str:
    return f"{settings.GOOGLE_WALLET_ISSUER_ID}.member_{member.pk}"


def _member_display_name(member) -> str:
    if member.name:
        return member.name
    if member.user:
        return member.user.get_full_name() or member.user.username
    return "Member"


def update_generic_object_for_member(member) -> bool:
    """PATCH member object fields that should reflect current club/member data."""
    if not is_configured():
        return False
    token = get_access_token()
    if not token:
        return False
    object_id = _object_id_for_member(member)
    member_name = _member_display_name(member)
    body = {
        "cardTitle": {
            "defaultValue": {"language": "en-US", "value": member.club.name},
        },
        # Keep the pass-type line live: paid or unpaid for dues-charging clubs.
        "header": {
            "defaultValue": {"language": "en-US", "value": member.wallet_header_text},
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
        **_object_visuals(member.club, expired=member.wallet_status_is_expired),
    }
    # No validTimeInterval: a past end date makes Wallet archive the pass off the device. A lapsed
    # membership keeps an active pass tinted red with an "Expired <date>" line.
    resp = requests.patch(
        f"{WALLET_API_BASE}/genericObject/{object_id}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code == 200:
        logger.info("Patched Google Wallet object %s", object_id)
        return True
    if resp.status_code == 404:
        logger.info("Google Wallet object %s does not exist; nothing to patch", object_id)
        return False
    logger.error("Google Wallet object patch failed for member %s: %s %s", member.pk, resp.status_code, resp.text)
    resp.raise_for_status()
    return False


def expire_generic_object_for_member(member) -> bool:
    """PATCH the member's object to state=EXPIRED so devices show it as expired.

    True when Google confirms it; False when it never existed (404, nothing to revoke). Raises on
    transport or 5xx errors for Celery to retry.
    """
    if not is_configured():
        return False
    token = get_access_token()
    if not token:
        return False
    object_id = _object_id_for_member(member)
    resp = requests.patch(
        f"{WALLET_API_BASE}/genericObject/{object_id}",
        json={"state": "EXPIRED"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code == 200:
        logger.info("Expired Google Wallet object %s", object_id)
        return True
    if resp.status_code == 404:
        # No object means the pass was never added to Wallet.
        logger.info("Google Wallet object %s does not exist; nothing to expire", object_id)
        return False
    logger.error("Google Wallet expire failed for member %s: %s %s", member.pk, resp.status_code, resp.text)
    resp.raise_for_status()
    return False


def create_generic_class(club) -> bool:
    """Create or update this club's GenericClass.

    POST first, PATCHing the same body on 409. The class carries only template fields; per-pass visuals
    live on each member's GenericObject. Raises on 5xx for retry.
    """
    if not is_configured():
        return False
    token = get_access_token()
    if not token:
        return False
    body = _class_body(club)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{WALLET_API_BASE}/genericClass", json=body, headers=headers, timeout=20)
    if resp.status_code == 200:
        logger.info("Created Google Wallet class %s for club %s", body["id"], club.pk)
        return True
    if resp.status_code == 409:
        patch_resp = requests.patch(
            f"{WALLET_API_BASE}/genericClass/{body['id']}", json=body, headers=headers, timeout=20
        )
        if patch_resp.status_code == 200:
            logger.info("Patched Google Wallet class %s for club %s", body["id"], club.pk)
            return True
        logger.error(
            "Google Wallet class patch failed for club %s: %s %s",
            club.pk,
            patch_resp.status_code,
            patch_resp.text,
        )
        patch_resp.raise_for_status()
        return False
    logger.error("Google Wallet class create failed for club %s: %s %s", club.pk, resp.status_code, resp.text)
    resp.raise_for_status()
    return False
