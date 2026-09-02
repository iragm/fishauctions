"""Authentication classes for mobile endpoints.

Everything under /api/mobile/ takes a JWT Bearer token and nothing else -- session auth is
deliberately excluded so a web cookie can never be used to call these (see
:class:`auctions.mobile.permissions.IsMobileAuthenticated`). This module holds the one endpoint that
wants a *softer* version of that rule.
"""

import logging

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger(__name__)


class OptionalJWTAuthentication(JWTAuthentication):
    """Authenticate a Bearer token when one is sent, and never turn a bad one into a 401.

    For an endpoint that is public but has one per-user field in it -- /api/mobile/config/, whose
    `menu` block is built from the caller. The app sends `Authorization` on every request including
    that one, so it authenticates whenever it can; but the config endpoint is also read *before*
    sign-in, to wire up Square, Firebase and the social sign-in buttons, and it has always answered
    200 to anybody.

    Hence the swallowed failure. An expired or malformed token means "anonymous", not 401: a phone
    whose access token aged out overnight would otherwise get no config at all -- no Square
    application id, no Firebase, no voice grammar -- because one optional block of it needed a
    fresh token. The cost is that such a phone briefly sees the signed-out menu; the app refetches
    on sign-in (`ConfigService.loadForCurrentUser`) and keeps the last good payload meanwhile, so
    that resolves itself, while a 401 here would not.

    Use it only where the endpoint is genuinely public. Anything that returns private data wants
    plain `JWTAuthentication` + `IsMobileAuthenticated`, where a bad token must be an error.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            # Covers simplejwt's InvalidToken (expired, wrong signature, blacklisted, no such user),
            # which subclasses DRF's AuthenticationFailed. Logged at debug: on a public endpoint
            # this is an ordinary event, not an incident.
            logger.debug("Ignoring an unusable bearer token on a public endpoint", exc_info=True)
            return None
