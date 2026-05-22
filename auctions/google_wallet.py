"""Helpers for talking to the Google Wallet REST API.

This module handles the OAuth2 access-token dance against
``https://oauth2.googleapis.com/token`` using the JWT-bearer assertion flow,
so we don't need ``google-auth`` as a dependency. PyJWT (already a project
dep) plus ``requests`` is enough.

Public entry points:
    is_configured()                       -> bool
    get_access_token()                    -> str | None  (cached in-memory)
    create_generic_class(club)            -> bool         (True on 200/409)
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

# Default class background — neutral dark, looks readable with white text.
DEFAULT_HEX_BG = "#1f2937"

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
    """Return a cached OAuth2 access token, refreshing on demand.

    Tokens are valid for one hour; we cache slightly less and refresh with a
    60s safety margin. Returns None when Wallet is not configured.
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
    """Return a publicly-reachable https URL for the club's icon, or "" if none/unusable.

    Google Wallet only accepts publicly resolvable https URLs for `logo.sourceUri.uri`,
    so we build one from the current Site domain and the thumbnailer's URL. If the
    deployment is on http only, we still return https — Google will reject it but
    that's a config issue, not a runtime one. Returns "" when there is no icon.
    """
    if not getattr(club, "icon", None):
        return ""
    try:
        from django.contrib.sites.models import Site
        from easy_thumbnails.files import get_thumbnailer

        thumbnailer = get_thumbnailer(club.icon)
        thumb = thumbnailer["club_icon"]
        domain = Site.objects.get_current().domain
        return f"https://{domain}{thumb.url}"
    except Exception:
        logger.exception("Could not build icon URL for club %s", club.pk)
        return ""


def _class_body(club) -> dict:
    body: dict = {
        "id": _class_id_for_club(club),
        "classTemplateInfo": {
            "cardTemplateOverride": {
                "cardRowTemplateInfos": [
                    {
                        "oneItem": {
                            "item": {"firstValue": {"fields": [{"fieldPath": "object.textModulesData['member_id']"}]}}
                        }
                    }
                ]
            }
        },
        "hexBackgroundColor": DEFAULT_HEX_BG,
    }
    icon_url = _absolute_icon_url(club)
    if icon_url:
        body["logo"] = {"sourceUri": {"uri": icon_url}}
    return body


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
        body["validTimeInterval"] = {"end": {"date": f"{member.membership_expiration_date.isoformat()}T23:59:59"}}
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
    """PATCH the member's Wallet object to state=EXPIRED so devices show it as expired.

    Returns True if Google confirms the object is now expired (200), or False if
    we couldn't tell (404 = object never existed = nothing to revoke, treated
    as success). Raises on transport / 5xx for Celery retry.
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
        # No object means the user never added the pass to Wallet — nothing to revoke.
        logger.info("Google Wallet object %s does not exist; nothing to expire", object_id)
        return False
    logger.error("Google Wallet expire failed for member %s: %s %s", member.pk, resp.status_code, resp.text)
    resp.raise_for_status()
    return False


def create_generic_class(club) -> bool:
    """Create-or-update the GenericClass for this club on Google Wallet.

    Tries POST first. On 409 (already exists) issues a PATCH with the same body,
    so re-running the task after the club icon / metadata changes will push the
    update to existing wallet passes. Raises on transport / 5xx for Celery retry.
    """
    if not is_configured():
        logger.info("Google Wallet not configured; skipping class creation for club %s", club.pk)
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
        # Class already exists — PATCH it so newly-set fields (e.g. logo) propagate
        # to passes already on user devices.
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
    # 4xx other than 409 indicates a config / data problem the caller must see.
    logger.error("Google Wallet class create failed for club %s: %s %s", club.pk, resp.status_code, resp.text)
    resp.raise_for_status()
    return False
