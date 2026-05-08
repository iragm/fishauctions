from django.contrib.auth.models import AnonymousUser
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.throttling import SimpleRateThrottle

from .models import ClubAPIKey


class APIKeyAuthentication(BaseAuthentication):
    """Authenticate requests using a ClubAPIKey.

    Accepts the key via:
      - X-API-Key header
      - Authorization: Api-Key <key> header

    Views that use this as their sole authenticator will accept ONLY API key
    auth — no session, no token, no anonymous access.
    """

    def authenticate_header(self, request):
        return "Api-Key"

    def authenticate(self, request):
        raw_key = request.META.get("HTTP_X_API_KEY", "")
        if not raw_key:
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Api-Key "):
                raw_key = auth_header[8:].strip()
        if not raw_key:
            msg = "API key required."
            raise AuthenticationFailed(msg)
        api_key = ClubAPIKey.verify(raw_key)
        if not api_key:
            msg = "Invalid or inactive API key."
            raise AuthenticationFailed(msg)
        request.api_key = api_key
        request.club = api_key.club
        # Return AnonymousUser so request.user is never None; the real credential
        # is the api_key stored on request.
        return (AnonymousUser(), api_key)


class ApiKeyThrottle(SimpleRateThrottle):
    scope = "api_key_default"

    def get_cache_key(self, request, view):
        api_key = getattr(request, "api_key", None)
        if not api_key:
            return None
        if api_key.rate_limit:
            self.num_requests = api_key.rate_limit
            self.duration = 3600
        return f"throttle_api_key_{api_key.pk}"
