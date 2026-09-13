"""Client ID Metadata Document handling for the clients that actually turn up.

CIMD lets claude.ai connect by presenting an ``https`` URL as its ``client_id`` instead of
registering. The toolkit's fetcher maps that document onto a DOT ``Application``, which has one
``authorization_grant_type`` column, so a document declaring more than one grant type (Claude's
declares three) fails resolution with ``invalid_request: Invalid client_id parameter value``. Fix:
drop grant types this server doesn't advertise before the document is mapped.
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


class ClientMetadataFetcher(SafeMetadataFetcher):
    """The toolkit's SSRF-hardened fetcher, with :func:`narrow_grant_types` on the way out."""

    def fetch(self, client_id):
        metadata, max_age = super().fetch(client_id)
        return narrow_grant_types(metadata), max_age
