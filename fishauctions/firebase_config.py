"""Parse the public Firebase client-config files that ship with the mobile build.

``google-services.json`` (Android) and ``GoogleService-Info.plist`` (iOS) hold only *public* values
— api key, app id, messaging sender id, project id, and the package/bundle id. They are NOT the
service-account key (that is ``FIREBASE_CREDENTIALS_JSON``, a secret used server-side to *send*
pushes). We parse them so the mobile config endpoint can hand the app the right Firebase project per
deployment.

Parsing never raises: a missing, unreadable, or malformed file yields ``None`` for that platform and
push simply isn't advertised for it (mirrors the graceful-degradation elsewhere in the app).
"""

import json
import logging
import plistlib
from pathlib import Path

logger = logging.getLogger(__name__)


def _first(seq):
    """First element of a non-empty sequence, else None (so a missing list degrades to None)."""
    return seq[0] if seq else None


def load_android_config(path):
    """Public Android Firebase config parsed from a ``google-services.json`` *path*, or None.

    Returns a flat dict: ``package_name``, ``api_key``, ``app_id``, ``messaging_sender_id``,
    ``project_id``. If the file has several Android clients, the first one is used.
    """
    if not path:
        return None
    try:
        with Path(path).open(encoding="utf-8") as f:
            data = json.load(f)
        project_info = data["project_info"]
        client = _first(data["client"])
        client_info = client["client_info"]
        return {
            "package_name": client_info["android_client_info"]["package_name"],
            "api_key": _first(client["api_key"])["current_key"],
            "app_id": client_info["mobilesdk_app_id"],
            "messaging_sender_id": project_info["project_number"],
            "project_id": project_info["project_id"],
        }
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        logger.warning("Could not parse Firebase Android config (google-services.json) at %s", path, exc_info=True)
        return None


def load_ios_config(path):
    """Public iOS Firebase config parsed from a ``GoogleService-Info.plist`` *path*, or None.

    Returns a flat dict: ``bundle_id``, ``api_key``, ``app_id``, ``messaging_sender_id``,
    ``project_id``. The plist is XML; ``plistlib`` reads it from a binary file object.
    """
    if not path:
        return None
    try:
        with Path(path).open("rb") as f:
            data = plistlib.load(f)
        return {
            "bundle_id": data["BUNDLE_ID"],
            "api_key": data["API_KEY"],
            "app_id": data["GOOGLE_APP_ID"],
            "messaging_sender_id": data["GCM_SENDER_ID"],
            "project_id": data["PROJECT_ID"],
        }
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        logger.warning("Could not parse Firebase iOS config (GoogleService-Info.plist) at %s", path, exc_info=True)
        return None


def load_firebase_client_config(android_path, ios_path):
    """Build the public per-platform Firebase config, omitting platforms with no valid file.

    Returns ``{"android": {...}, "ios": {...}}`` with only the platforms that parsed successfully, or
    an empty dict when neither is configured. Every value is public and safe to serve to the app.
    """
    config = {}
    android = load_android_config(android_path)
    if android:
        config["android"] = android
    ios = load_ios_config(ios_path)
    if ios:
        config["ios"] = ios
    return config
