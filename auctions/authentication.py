from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.throttling import SimpleRateThrottle

from .models import ClubAPIKey


class APIKeyAuthentication(BaseAuthentication):
    """Authenticate requests using a ClubAPIKey.

    Accepts the key via:
      - X-API-Key header
      - Authorization: Api-Key <key> header
    """

    def authenticate(self, request):
        raw_key = request.META.get("HTTP_X_API_KEY", "")
        if not raw_key:
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Api-Key "):
                raw_key = auth_header[8:].strip()
        if not raw_key:
            return None  # allow other authenticators to try
        api_key = ClubAPIKey.verify(raw_key)
        if not api_key:
            msg = "Invalid or inactive API key."
            raise AuthenticationFailed(msg)
        request.api_key = api_key
        request.club = api_key.club
        return (None, api_key)  # no Django user; api_key is the auth credential


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
