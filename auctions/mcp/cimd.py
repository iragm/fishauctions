"""Client ID Metadata Document handling for the clients that actually turn up.

CIMD lets claude.ai and ChatGPT connect by presenting an ``https`` URL as their ``client_id``
instead of registering. The toolkit maps that document onto a public DOT ``Application`` and fails
resolution -- ``invalid_request: Invalid client_id parameter value``, nothing else -- on two things
real clients send:

* more than one non-refresh grant type (Claude's declares three), because ``Application`` has one
  ``authorization_grant_type`` column;
* a ``token_endpoint_auth_method`` other than ``"none"``. ChatGPT's says ``private_key_jwt`` while
  its ``token_endpoint_auth_methods_supported`` offers ``["none", "private_key_jwt"]``, and it picks
  from the intersection with what we advertise, which is ``"none"``.

Both are narrowed to what this server advertises before the document is mapped.
"""

from __future__ import annotations

import logging

from django.conf import settings
from oauth2_provider.cimd import SafeMetadataFetcher

logger = logging.getLogger(__name__)

#: Always kept regardless of the discovery document.
ALWAYS_KEEP = frozenset({"refresh_token"})


def supported_grant_types() -> frozenset[str]:
    """What this server advertises, read from settings."""
    declared = settings.OAUTH2_PROVIDER.get("OAUTH2_GRANT_TYPES_SUPPORTED") or ["authorization_code"]
    return frozenset(declared) | ALWAYS_KEEP


def narrow_grant_types(metadata: dict) -> dict:
    """Copy of ``metadata`` with unsupported grant types removed; unchanged input if nothing to drop."""
    declared = metadata.get("grant_types")
    if not isinstance(declared, list):
        return metadata
    kept = [grant for grant in declared if grant in supported_grant_types()]
    if kept == declared:
        return metadata
    dropped = [grant for grant in declared if grant not in kept]
    logger.info("Ignoring unsupported grant types %s from a client id metadata document", dropped)
    narrowed = dict(metadata)
    narrowed["grant_types"] = kept
    return narrowed


def narrow_auth_method(metadata: dict) -> dict:
    """Copy of ``metadata`` claiming ``"none"`` when the client offers it and we advertise it.

    Only ever narrows to public: a document that doesn't list ``"none"`` among the methods it
    supports is passed through, and the toolkit refuses it as before.
    """
    declared = metadata.get("token_endpoint_auth_method", "none")
    if declared == "none":
        return metadata
    offered = metadata.get("token_endpoint_auth_methods_supported")
    advertised = settings.OAUTH2_PROVIDER.get("OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED") or []
    if not isinstance(offered, list) or "none" not in offered or "none" not in advertised:
        return metadata
    logger.info("Treating a client id metadata document's %r token endpoint auth as 'none'", declared)
    narrowed = dict(metadata)
    narrowed["token_endpoint_auth_method"] = "none"
    return narrowed


class ClientMetadataFetcher(SafeMetadataFetcher):
    """The toolkit's SSRF-hardened fetcher, narrowed to what we advertise on the way out."""

    def fetch(self, client_id):
        metadata, max_age = super().fetch(client_id)
        return narrow_auth_method(narrow_grant_types(metadata)), max_age
