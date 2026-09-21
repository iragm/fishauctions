"""OpenID Connect on top of the OAuth 2.1 server, for the one thing OAuth alone can't say: who.

``/mcp/`` needs no identity -- a bearer token already names a user. This exists because ChatGPT's
plugin directory uses a *verified email address* to keep a work account from linking a plugin into a
personal workspace, and the only way it can have one is an OIDC UserInfo endpoint advertising the
``openid`` and ``email`` scopes. That is the whole feature: two claims, read off the account the
token already belongs to.

Two things have to be true together or the connection breaks rather than degrades:

* **A key, or nothing.** ``OIDC_ENABLED`` is set from whether ``OIDC_RSA_KEYFILE`` loaded, because
  the discovery document advertising ``openid`` is a promise to mint an ID token, and a server with
  no RSA key can only do that with a client secret -- which a public client connecting over CIMD or
  DCR does not have.
* **Every client is RS256.** The toolkit signs an ID token with ``Application.algorithm`` and
  registration leaves that blank, so an app registered before this file existed would answer the
  first ``openid`` request with an unsigned JWT header. :func:`sign_with_rs256` fills it in on the
  way into the database, which is every path -- DCR, CIMD and the admin -- in one place.

``email_verified`` is the allauth fact, never ``True`` because we have a string: a workspace
restriction built on an unverified address is worth nothing, and saying so is what lets the other
end trust it.
"""

from __future__ import annotations

import logging

from django.db.models.signals import pre_save
from django.dispatch import receiver
from oauth2_provider.oauth2_validators import OAuth2Validator

logger = logging.getLogger(__name__)


def _email(request) -> str:
    return getattr(request.user, "email", "") or ""


def _email_verified(request) -> bool:
    """Whether allauth has confirmed the address :func:`_email` returns."""
    from allauth.account.models import EmailAddress

    email = _email(request)
    if not email:
        return False
    return EmailAddress.objects.filter(user=request.user, email__iexact=email, verified=True).exists()


class Validator(OAuth2Validator):
    """The toolkit's validator plus the two claims the ``email`` scope carries.

    ``get_additional_claims`` takes no request on purpose: the toolkit reads that signature to mean
    the claims are values it may list in ``claims_supported`` on the discovery document, and calls
    each one with the request when it actually needs the answer.
    """

    def get_additional_claims(self):
        return {"email": _email, "email_verified": _email_verified}


@receiver(pre_save, sender="oauth2_provider.Application")
def sign_with_rs256(sender, instance, **kwargs):
    """Give a client with no signing algorithm RS256, so an ``openid`` request can be answered.

    Registration (DCR and CIMD both) never sets one, and the failure is at the far end of a flow a
    person is standing in the middle of.
    """
    from django.conf import settings

    if instance.algorithm:
        return
    if not settings.OAUTH2_PROVIDER.get("OIDC_ENABLED"):
        return
    instance.algorithm = sender.RS256_ALGORITHM
