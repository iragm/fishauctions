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

The validator also closes one RFC 8707 gap for ``/mcp/admin/``
(:meth:`Validator._check_and_set_request_resource`): it is the site's one validator class, so the
token endpoint's rules live here too.
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

    def _check_and_set_request_resource(self, request):
        """The toolkit's RFC 8707 handling, minus one escalation: a token for ``/mcp/admin`` only from
        a grant or refresh token that already named it.

        The toolkit lets a token request add a ``resource`` when the grant (or refresh token) named
        none, so a connection consented to without the admin warning -- or a long-lived refresh token
        from an everyday connection -- could otherwise be swapped for an admin one at the token
        endpoint, never passing ``auctions.mcp.consent``.
        """
        from oauthlib.oauth2.rfc6749 import errors

        from .auth import is_admin_path

        super()._check_and_set_request_resource(request)
        wanted = {uri for uri in request.resource or [] if is_admin_path(_path(uri))}
        if not wanted:
            return
        if request.grant_type == "authorization_code":
            from oauth2_provider.models import get_grant_model

            grant = get_grant_model().objects.filter(code=request.code, application=request.client).first()
            had = set((grant.resource or []) if grant else [])
        elif request.grant_type == "refresh_token":
            had = set(getattr(getattr(request, "refresh_token_instance", None), "resource", None) or [])
        else:
            had = set()
        if not wanted <= had:
            raise errors.CustomOAuth2Error(
                error="invalid_target",
                description="An admin connection must be asked for when signing in, not added afterwards.",
                request=request,
            )


def _path(uri) -> str:
    from urllib.parse import urlsplit

    return urlsplit(str(uri)).path


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
