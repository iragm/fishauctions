"""Client ID Metadata Document handling for the clients that actually turn up.

CIMD (``draft-ietf-oauth-client-id-metadata-document``) is how claude.ai connects: instead of
registering, it presents an ``https`` URL as its ``client_id`` and the authorization server fetches
that URL for the client's metadata. django-oauth-toolkit implements it, and its fetcher is
carefully hardened against SSRF, so this module keeps all of that and changes one thing.

**The one thing.** The toolkit maps a CIMD document onto a DOT ``Application``, which has a single
``authorization_grant_type`` column, so it insists the document declare exactly one non-refresh
grant type. Claude's document declares three::

    "grant_types": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"]

...so resolution fails, the client looks unknown, and the person who clicked Connect is shown
``invalid_request: Invalid client_id parameter value`` with nothing in it to act on. Nothing about
that is Claude misbehaving: RFC 7591's ``grant_types`` is the list of grants the client *may* use,
and an authorization server that does not offer one of them is supposed to ignore it, not refuse
the client. The JWT-bearer grant is not on offer here (``OAUTH2_GRANT_TYPES_SUPPORTED`` says so, and
so does the discovery document), and a client asking for it and not getting it is the system
working.

So: fetch exactly as the toolkit does, then drop the grant types this server does not advertise
before the document is mapped. Narrowing only -- a document that asks for nothing we support still
fails, which is the correct answer.
"""

from __future__ import annotations

import logging

from django.conf import settings
from oauth2_provider.cimd import SafeMetadataFetcher

logger = logging.getLogger(__name__)

#: Handled by the toolkit alongside ``authorization_code`` rather than being a grant of its own,
#: so it is kept whatever the discovery document says about it.
ALWAYS_KEEP = frozenset({"refresh_token"})


def supported_grant_types() -> frozenset[str]:
    """What this server advertises. Read from the setting so the two can never disagree."""
    declared = settings.OAUTH2_PROVIDER.get("OAUTH2_GRANT_TYPES_SUPPORTED") or ["authorization_code"]
    return frozenset(declared) | ALWAYS_KEEP


def narrow_grant_types(metadata: dict) -> dict:
    """A copy of ``metadata`` with grant types this server doesn't offer removed.

    Returns the original object untouched when there is nothing to drop, so the common case does
    not allocate and a document we have no opinion about is passed through byte for byte.
    """
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
    """The toolkit's fetcher, with :func:`narrow_grant_types` on the way out.

    Subclassed rather than replaced on purpose: the fetch itself is the security-sensitive half
    (https only, resolve and pin the IP, no redirects, tight timeouts, size cap) and none of it is
    worth reimplementing to change one list.
    """

    def fetch(self, client_id):
        metadata, max_age = super().fetch(client_id)
        return narrow_grant_types(metadata), max_age
