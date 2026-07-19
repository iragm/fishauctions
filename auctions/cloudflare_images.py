"""Cloudflare Images integration.

When the CLOUDFLARE_IMAGES_* settings are configured (see .env.example), images that
have a `cloudflare_image_id` (set by the migrate_to_cloudflare_images management
command) are served from Cloudflare's CDN with named variants instead of locally
generated easy-thumbnails files.  Everything falls back to the local thumbnailer when
Cloudflare is not configured or an image hasn't been migrated yet, so this can be
enabled and disabled freely.

API reference: https://developers.cloudflare.com/images/
"""

import json
import logging
from pathlib import Path

import requests
from django.conf import settings
from easy_thumbnails.files import get_thumbnailer

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/images/v1"

# Named variants served by Cloudflare.  Keys deliberately match the easy-thumbnails
# aliases in THUMBNAIL_ALIASES (settings.py) so the same name works for both systems.
# "public" (the full-size image) is created by Cloudflare automatically and is not
# listed here.  Sync these to Cloudflare with `manage.py migrate_to_cloudflare_images --setup`.
# Note: named variants crop from the center ("cover"); easy-thumbnails' "smart" crop has
# no direct equivalent, but center crop is close enough for these small thumbnails.
VARIANTS = {
    "ad": {"fit": "scale-down", "width": 250, "height": 150, "metadata": "none"},
    "lot_list": {"fit": "cover", "width": 250, "height": 150, "metadata": "none"},
    "club_icon": {"fit": "cover", "width": 128, "height": 128, "metadata": "none"},
    "club_icon_small": {"fit": "cover", "width": 32, "height": 32, "metadata": "none"},
    "google_wallet_logo": {"fit": "cover", "width": 660, "height": 660, "metadata": "none"},
}


# Stored in cloudflare_image_id when Cloudflare permanently rejects a file (unsupported
# format, too large...).  Serving falls back to the local file, and the migration
# command stops retrying it; replacing the image file clears this and retries.
UPLOAD_FAILED = "upload-failed"


class CloudflareImagesError(Exception):
    """The Cloudflare Images API returned an error or an unusable response"""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def enabled():
    return settings.CLOUDFLARE_IMAGES_ENABLED


def delivery_url(image_id, variant="public"):
    """Public URL to serve a migrated image from Cloudflare, at the given named variant"""
    if variant != "public" and variant not in VARIANTS:
        variant = "public"
    if settings.CLOUDFLARE_IMAGES_DOMAIN:
        base = f"https://{settings.CLOUDFLARE_IMAGES_DOMAIN}/cdn-cgi/imagedelivery"
    else:
        base = "https://imagedelivery.net"
    return f"{base}/{settings.CLOUDFLARE_IMAGES_ACCOUNT_HASH}/{image_id}/{variant}"


def image_url(field_file, cloudflare_image_id, alias=None):
    """URL for an image: from Cloudflare when migrated and enabled, else the local file.

    `alias` is a THUMBNAIL_ALIASES/VARIANTS name; None means the full-size image.
    Returns None if there is no image at all.
    """
    if cloudflare_image_id and cloudflare_image_id != UPLOAD_FAILED and enabled():
        return delivery_url(cloudflare_image_id, alias or "public")
    if field_file:
        if alias:
            try:
                return get_thumbnailer(field_file)[alias].url
            except Exception:
                # broken/missing source file; the full-size URL is a path and always safe
                logger.warning("Could not generate '%s' thumbnail for %s", alias, field_file.name)
        return field_file.url
    return None


def _api_url(path=""):
    if not (settings.CLOUDFLARE_IMAGES_ACCOUNT_ID and settings.CLOUDFLARE_IMAGES_API_TOKEN):
        msg = "CLOUDFLARE_IMAGES_ACCOUNT_ID and CLOUDFLARE_IMAGES_API_TOKEN must be set in .env"
        raise CloudflareImagesError(msg)
    return API_BASE.format(account_id=settings.CLOUDFLARE_IMAGES_ACCOUNT_ID) + path


def _headers():
    return {"Authorization": f"Bearer {settings.CLOUDFLARE_IMAGES_API_TOKEN}"}


def _check(response):
    """Validate a Cloudflare API response envelope and return its `result`"""
    try:
        payload = response.json()
    except ValueError as e:
        msg = f"Cloudflare API returned non-JSON response (HTTP {response.status_code})"
        raise CloudflareImagesError(msg, status_code=response.status_code) from e
    if not payload.get("success"):
        msg = f"Cloudflare API error (HTTP {response.status_code}): {payload.get('errors')}"
        raise CloudflareImagesError(msg, status_code=response.status_code)
    return payload.get("result") or {}


def upload(field_file, metadata=None):
    """Upload a local image file to Cloudflare Images and return its new image id"""
    url = _api_url()
    data = {}
    if metadata:
        data["metadata"] = json.dumps(metadata)
    with field_file.open("rb") as source:
        response = requests.post(
            url,
            headers=_headers(),
            files={"file": (Path(field_file.name).name, source)},
            data=data,
            timeout=60,
        )
    result = _check(response)
    image_id = result.get("id")
    if not image_id:
        msg = f"Cloudflare API did not return an image id: {result}"
        raise CloudflareImagesError(msg)
    return image_id


def delete(image_id):
    """Delete an image from Cloudflare Images"""
    response = requests.delete(_api_url(f"/{image_id}"), headers=_headers(), timeout=30)
    _check(response)


def sync_variants():
    """Create (or update, if they already exist) the named VARIANTS on Cloudflare"""
    for name, options in VARIANTS.items():
        body = {"id": name, "options": options, "neverRequireSignedURLs": True}
        response = requests.post(_api_url("/variants"), headers=_headers(), json=body, timeout=30)
        try:
            _check(response)
        except CloudflareImagesError:
            # POST fails when the variant already exists; update it in place instead
            response = requests.patch(
                _api_url(f"/variants/{name}"),
                headers=_headers(),
                json={"options": options, "neverRequireSignedURLs": True},
                timeout=30,
            )
            _check(response)
