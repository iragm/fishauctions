"""Helpers for generating Apple Wallet (PassKit) .pkpass files.

Unlike Google Wallet, Apple does not expose a REST API — passes are signed
zip archives generated server-side and served directly to the user. We sign
the manifest with PKCS#7 (detached, DER-encoded) using the project's existing
``cryptography`` dep, and draw fallback icon/logo PNGs on the fly with Pillow.

Public entry points:
    is_configured()                       -> bool
    generate_pkpass_for_member(member)    -> bytes  (raw .pkpass zip data)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12
from cryptography.hazmat.primitives.serialization.pkcs7 import PKCS7Options, PKCS7SignatureBuilder
from django.conf import settings
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# Card background — same dark slate we use for the Google Wallet class so the
# two cards feel consistent.
DEFAULT_BACKGROUND_RGB = (31, 41, 55)
DEFAULT_FOREGROUND_RGB = (255, 255, 255)


def is_configured() -> bool:
    return bool(
        getattr(settings, "APPLE_WALLET_CERT_FILE", "")
        and getattr(settings, "APPLE_WALLET_WWDR_FILE", "")
        and getattr(settings, "APPLE_WALLET_PASS_TYPE_IDENTIFIER", "")
        and getattr(settings, "APPLE_WALLET_TEAM_IDENTIFIER", "")
    )


@lru_cache(maxsize=1)
def _load_signing_certs():
    """Load and cache the Pass Type ID cert/key plus the WWDR intermediate.

    Cached for the life of the process. If the operator rotates certs they need
    to bounce the worker (true of every dep we cache in-memory).
    """
    cert_path = settings.BASE_DIR / settings.APPLE_WALLET_CERT_FILE
    wwdr_path = settings.BASE_DIR / settings.APPLE_WALLET_WWDR_FILE
    password = (settings.APPLE_WALLET_CERT_PASSWORD or "").encode() or None
    with cert_path.open("rb") as f:
        private_key, signer_cert, _additional = pkcs12.load_key_and_certificates(f.read(), password)
    if private_key is None or signer_cert is None:
        msg = "Apple Wallet .p12 did not contain both a private key and a certificate."
        raise ValueError(msg)
    with wwdr_path.open("rb") as f:
        wwdr_cert = x509.load_pem_x509_certificate(f.read())
    return private_key, signer_cert, wwdr_cert


def _placeholder_png(text: str, size: tuple[int, int]) -> bytes:
    """Generate a simple solid-color PNG with centered text as a fallback asset.

    Apple Wallet requires at minimum icon.png (29x29) and icon@2x.png (58x58),
    plus a logo.png (max 160x50). We don't have per-club logo assets in the DB,
    so we draw something minimal and consistent.
    """
    img = Image.new("RGB", size, DEFAULT_BACKGROUND_RGB)
    draw = ImageDraw.Draw(img)
    # Default font scales with image height; no external font needed.
    bbox = draw.textbbox((0, 0), text, font=None)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(
        ((size[0] - text_w) / 2, (size[1] - text_h) / 2),
        text,
        fill=DEFAULT_FOREGROUND_RGB,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _build_pass_json(member) -> dict:
    """Build the pass.json content for a member."""
    club = member.club
    member_name = member.name or (member.user.get_full_name() or member.user.username if member.user else "Member")
    pass_json: dict = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER,
        "teamIdentifier": settings.APPLE_WALLET_TEAM_IDENTIFIER,
        "organizationName": settings.APPLE_WALLET_ORGANIZATION_NAME or club.name,
        "serialNumber": f"member-{member.pk}",
        "description": f"{club.name} membership card",
        "backgroundColor": f"rgb{DEFAULT_BACKGROUND_RGB}",
        "foregroundColor": f"rgb{DEFAULT_FOREGROUND_RGB}",
        "labelColor": "rgb(200, 200, 200)",
        "logoText": club.name,
        "generic": {
            "primaryFields": [
                {"key": "name", "label": "Member", "value": member_name},
            ],
            "secondaryFields": [
                {"key": "memberId", "label": "Member ID", "value": str(member.membership_number)},
            ],
            "auxiliaryFields": [],
        },
        # Both barcode (legacy iOS 6-8) and barcodes (iOS 9+) for broadest compatibility.
        "barcode": {
            "format": "PKBarcodeFormatCode128",
            "message": str(member.membership_number),
            "messageEncoding": "iso-8859-1",
            "altText": str(member.membership_number),
        },
        "barcodes": [
            {
                "format": "PKBarcodeFormatCode128",
                "message": str(member.membership_number),
                "messageEncoding": "iso-8859-1",
                "altText": str(member.membership_number),
            }
        ],
    }
    if member.membership_expiration_date:
        # Apple wants ISO 8601 with timezone offset; treat the date as end-of-day UTC.
        pass_json["expirationDate"] = f"{member.membership_expiration_date.isoformat()}T23:59:59Z"
        pass_json["generic"]["auxiliaryFields"].append(
            {
                "key": "expires",
                "label": "Expires",
                "value": f"{member.membership_expiration_date.isoformat()}T00:00",
                "dateStyle": "PKDateStyleMedium",
                "timeStyle": "PKDateStyleNone",
            }
        )
    return pass_json


def _sign_manifest(manifest_bytes: bytes) -> bytes:
    """Return a PKCS#7 detached, DER-encoded signature for the manifest."""
    private_key, signer_cert, wwdr_cert = _load_signing_certs()
    return (
        PKCS7SignatureBuilder()
        .set_data(manifest_bytes)
        .add_signer(signer_cert, private_key, hashes.SHA256())
        .add_certificate(wwdr_cert)
        .sign(Encoding.DER, [PKCS7Options.DetachedSignature, PKCS7Options.NoCapabilities])
    )


def generate_pkpass_for_member(member) -> bytes:
    """Build, sign and zip a .pkpass for the given ClubMember. Returns the raw bytes."""
    if not is_configured():
        msg = "Apple Wallet is not configured (missing cert / pass type / team ID)."
        raise RuntimeError(msg)

    # Build the files that go inside the pkpass.
    initials = "".join(p[:1] for p in (member.club.name or "M").split()[:2]).upper() or "M"
    files: dict[str, bytes] = {
        "pass.json": json.dumps(_build_pass_json(member), separators=(",", ":")).encode("utf-8"),
        "icon.png": _placeholder_png(initials, (29, 29)),
        "icon@2x.png": _placeholder_png(initials, (58, 58)),
        "logo.png": _placeholder_png(member.club.name[:24] or "Membership", (160, 50)),
    }

    # Apple requires SHA-1 hashes for each file in manifest.json. The signature
    # itself is SHA-256; mixing is allowed and standard practice on modern iOS.
    manifest = {name: hashlib.sha1(data, usedforsecurity=False).hexdigest() for name, data in files.items()}
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    files["manifest.json"] = manifest_bytes
    files["signature"] = _sign_manifest(manifest_bytes)

    # Zip with deterministic ordering so the same pass produces identical bytes
    # — helpful for debugging and for clients that cache by content.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return buf.getvalue()
