"""Helpers for Apple Wallet (PassKit): .pkpass generation and pass-update pushes.

Unlike Google Wallet, Apple does not expose a REST API — passes are signed
zip archives generated server-side and served directly to the user. We sign
the manifest with PKCS#7 (detached, DER-encoded) using the project's existing
``cryptography`` dep, and draw fallback icon/logo PNGs on the fly with Pillow.

Installed passes update live via the PassKit web service (auctions/passkit_views.py):
each pass embeds webServiceURL + a per-member authenticationToken, devices register
here with an APNs push token, and send_pass_update_notification() pokes APNs (empty
push, topic = pass type ID, authenticated with the same Pass Type ID cert) so the
device re-fetches the latest .pkpass.

Public entry points:
    is_configured()                          -> bool
    generate_pkpass_for_member(member)       -> bytes  (raw .pkpass zip data)
    ensure_apple_pass_auth_token(member)     -> str    (per-pass web service secret)
    send_pass_update_notification(registration) -> bool (True = registration still valid)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import secrets
import tempfile
import zipfile
from functools import lru_cache

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
from cryptography.hazmat.primitives.serialization.pkcs7 import PKCS7Options, PKCS7SignatureBuilder
from django.conf import settings
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

APNS_URL = "https://api.push.apple.com"  # Pass Type ID certs are production-only; no sandbox.

# Card background — same dark slate we use for the Google Wallet class so the
# two cards feel consistent.
DEFAULT_BACKGROUND_RGB = (31, 41, 55)
DEFAULT_FOREGROUND_RGB = (255, 255, 255)
# Card background for a lapsed/expired membership — a dark red that stays readable
# with white text. Apple passes can't color one field red, so the whole card is
# tinted to signal the "Unpaid/expired" state.
EXPIRED_BACKGROUND_RGB = (153, 27, 27)


def is_configured() -> bool:
    return bool(
        getattr(settings, "APPLE_WALLET_CERT_FILE", "")
        and getattr(settings, "APPLE_WALLET_WWDR_FILE", "")
        and getattr(settings, "APPLE_WALLET_PASS_TYPE_IDENTIFIER", "")
        and getattr(settings, "APPLE_WALLET_TEAM_IDENTIFIER", "")
    )


def _read_cert_file(path):
    """Read a cert file, converting the two operator-error cases into clear messages:
    a missing file and an unreadable one (host-copied files land root-owned in the
    bind-mounted repo root — see check_apple_wallet)."""
    try:
        with path.open("rb") as f:
            return f.read()
    except FileNotFoundError:
        msg = f"Apple Wallet file {path} does not exist. Paths are relative to the repo root ({settings.BASE_DIR})."
        raise ValueError(msg) from None
    except PermissionError:
        msg = (
            f"Apple Wallet file {path} is not readable by the app user. "
            f"Fix on the host from the project root: sudo chown {os.getuid()}:{os.getgid()} {path.name}"
        )
        raise ValueError(msg) from None


def _load_wwdr_cert(data: bytes):
    """Parse the WWDR intermediate from PEM or DER.

    Apple distributes WWDR certs as DER (.cer); operators often convert to PEM.
    Accept both so the download can be dropped in place unmodified.
    """
    try:
        return x509.load_pem_x509_certificate(data)
    except ValueError:
        return x509.load_der_x509_certificate(data)


@lru_cache(maxsize=1)
def _load_signing_certs():
    """Load and cache the Pass Type ID cert/key plus the WWDR intermediate.

    Cached for the life of the process. If the operator rotates certs they need
    to bounce the worker (true of every dep we cache in-memory).

    Raises ValueError with an operator-actionable message when a file is
    missing/unreadable, the .p12 is incomplete, or the WWDR intermediate is not
    the one that issued the Pass Type ID cert — iPhones silently refuse to add a
    pass whose embedded chain doesn't verify, and validators report it as
    "WWDR certificate missing", so fail loudly at signing time instead.
    """
    cert_path = settings.BASE_DIR / settings.APPLE_WALLET_CERT_FILE
    wwdr_path = settings.BASE_DIR / settings.APPLE_WALLET_WWDR_FILE
    password = (settings.APPLE_WALLET_CERT_PASSWORD or "").encode() or None
    private_key, signer_cert, _additional = pkcs12.load_key_and_certificates(_read_cert_file(cert_path), password)
    if private_key is None or signer_cert is None:
        msg = "Apple Wallet .p12 did not contain both a private key and a certificate."
        raise ValueError(msg)
    wwdr_cert = _load_wwdr_cert(_read_cert_file(wwdr_path))
    try:
        signer_cert.verify_directly_issued_by(wwdr_cert)
    except Exception as exc:
        msg = (
            f"The WWDR certificate in {wwdr_path.name} (subject: {wwdr_cert.subject.rfc4514_string()}) "
            f"did not issue the Pass Type ID certificate in {cert_path.name} "
            f"(issuer: {signer_cert.issuer.rfc4514_string()}). Download the matching WWDR intermediate "
            f"(usually G4: https://www.apple.com/certificateauthority/AppleWWDRCAG4.cer) — "
            f"a mismatched chain makes iPhones refuse the pass ('WWDR certificate missing'). ({exc})"
        )
        raise ValueError(msg) from exc
    return private_key, signer_cert, wwdr_cert


def _placeholder_png(text: str, size: tuple[int, int]) -> bytes:
    """Generate a simple solid-color PNG with centered text as a fallback asset.

    Apple Wallet requires at minimum icon.png (29x29) and icon@2x.png (58x58),
    plus a logo.png (max 160x50). When the club has no uploaded icon we draw
    something minimal and consistent.
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


def _icon_png(club, size: tuple[int, int]) -> bytes:
    """Return PNG bytes sized to `size`, using the club icon if set.

    Falls back to the text placeholder if the club has no icon or the file can't
    be opened. Always returns RGB (no alpha) since some Wallet clients have been
    finicky about transparency on the icon slot.
    """
    if getattr(club, "icon", None):
        try:
            with club.icon.open("rb") as f:
                with Image.open(f) as src:
                    img = src.convert("RGB").copy()
            img.thumbnail(size, Image.LANCZOS)
            # Pad to exact target size with the brand background — Apple displays
            # icon.png at a fixed square in the lock-screen UI, so consistency
            # beats letting iOS rescale.
            canvas = Image.new("RGB", size, DEFAULT_BACKGROUND_RGB)
            offset = ((size[0] - img.width) // 2, (size[1] - img.height) // 2)
            canvas.paste(img, offset)
            buf = io.BytesIO()
            canvas.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception:
            logger.exception("Failed to render club icon for Apple Wallet; falling back to placeholder")
    initials = "".join(p[:1] for p in (club.name or "M").split()[:2]).upper() or "M"
    return _placeholder_png(initials, size)


def ensure_apple_pass_auth_token(member) -> str:
    """Return the member's PassKit web service auth token, generating it on first use.

    Persisted with a queryset update so lazily minting a token doesn't re-fire the
    ClubMember save signals (which would queue a pointless wallet-update push).
    """
    if not member.apple_pass_auth_token:
        member.apple_pass_auth_token = secrets.token_hex(16)
        type(member).objects.filter(pk=member.pk).update(apple_pass_auth_token=member.apple_pass_auth_token)
    return member.apple_pass_auth_token


def _web_service_url() -> str:
    """Base URL for the PassKit web service; devices append /v1/... themselves."""
    from django.contrib.sites.models import Site

    try:
        domain = Site.objects.get_current().domain
    except Site.DoesNotExist:
        domain = "localhost"
    return f"https://{domain}/passkit"


def _build_pass_json(member) -> dict:
    """Build the pass.json content for a member.

    Every pass carries webServiceURL + authenticationToken so installed passes stay
    live: devices register with the PassKit web service and get an APNs poke (see
    send_pass_update_notification) whenever pass-visible content changes, then
    re-fetch the newest .pkpass from here.

    A pass for a club that has turned off member barcodes — or for a member who has
    been deactivated — is marked voided; that is the only way to remotely kill an
    installed Apple pass (there is no delete API), so the web service keeps serving
    those passes voided instead of 404ing.
    """
    club = member.club
    member_name = member.name or (member.user.get_full_name() or member.user.username if member.user else "Member")
    expired = member.wallet_status_is_expired
    background_rgb = EXPIRED_BACKGROUND_RGB if expired else DEFAULT_BACKGROUND_RGB
    pass_json: dict = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER,
        "teamIdentifier": settings.APPLE_WALLET_TEAM_IDENTIFIER,
        "organizationName": settings.APPLE_WALLET_ORGANIZATION_NAME or club.name,
        "serialNumber": f"member-{member.pk}",
        "webServiceURL": _web_service_url(),
        "authenticationToken": ensure_apple_pass_auth_token(member),
        "description": f"{club.name} membership card",
        "backgroundColor": f"rgb{background_rgb}",
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
    # Membership status line: "Unpaid/expired" or "Valid through 1 Jan 2025".
    # None only when the club doesn't run memberships, leaving the pass unchanged.
    status_text = member.wallet_status_text
    if status_text:
        pass_json["generic"]["auxiliaryFields"].append({"key": "status", "label": "Status", "value": status_text})
    expiration = member.effective_expiration_date
    if expiration:
        # Apple wants ISO 8601 with timezone offset; treat the date as end-of-day UTC.
        # Apple greys out an expired pass automatically once this date passes.
        pass_json["expirationDate"] = f"{expiration.isoformat()}T23:59:59Z"
    if not club.show_member_barcode or member.is_deleted:
        pass_json["voided"] = True
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

    # Build the files that go inside the pkpass. icon.png / icon@2x.png are
    # square (Apple displays them at the lock-screen notification thumbnail
    # size); logo.png appears in the top-left of the pass. When the club has
    # uploaded an icon we use it for both — otherwise we fall back to a text
    # placeholder rendered from the club name initials.
    club = member.club
    files: dict[str, bytes] = {
        "pass.json": json.dumps(_build_pass_json(member), separators=(",", ":")).encode("utf-8"),
        "icon.png": _icon_png(club, (29, 29)),
        "icon@2x.png": _icon_png(club, (58, 58)),
        "logo.png": _icon_png(club, (50, 50)),
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


@lru_cache(maxsize=1)
def _apns_cert_path() -> str:
    """Write the Pass Type ID cert + key as one PEM file for APNs client TLS auth.

    APNs pass-update pushes authenticate with the same certificate that signs the
    pass, but the TLS layer only loads certs from disk — so the .p12 contents are
    re-serialized to a mode-0600 temp file once per process (mkstemp is 0600 by
    default; the key already sits on the same disk inside the .p12).
    """
    private_key, signer_cert, _wwdr = _load_signing_certs()
    pem = signer_cert.public_bytes(Encoding.PEM) + private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    fd, path = tempfile.mkstemp(prefix="apns-cert-", suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return path


@lru_cache(maxsize=1)
def _apns_client() -> httpx.Client:
    """Shared HTTP/2 client for APNs (APNs rejects HTTP/1.1)."""
    return httpx.Client(http2=True, cert=_apns_cert_path(), timeout=10)


def send_pass_update_notification(registration) -> bool:
    """Tell one registered device its pass changed; the device then re-fetches it.

    The push is an empty payload with the pass type identifier as the topic — that
    is the entire PassKit update protocol. Returns False (and deletes the
    registration) when APNs says the token is dead, e.g. the user removed the pass
    while offline. Raises httpx.HTTPError on transient failures so Celery retries.
    """
    response = _apns_client().post(
        f"{APNS_URL}/3/device/{registration.push_token}",
        json={"aps": {}},
        headers={
            "apns-topic": settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER,
            "apns-push-type": "alert",
            "apns-priority": "10",
        },
    )
    if response.status_code == 200:
        return True
    try:
        reason = response.json().get("reason", "")
    except ValueError:
        reason = ""
    # 410 = token permanently gone; the 400-reasons below mean this token can never
    # work for this topic. Drop the registration instead of retrying forever.
    if response.status_code == 410 or reason in ("BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"):
        logger.info(
            "Dropping dead APNs registration pk=%s (status=%s reason=%s)",
            registration.pk,
            response.status_code,
            reason,
        )
        registration.delete()
        return False
    response.raise_for_status()
    return True
