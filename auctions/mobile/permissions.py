from rest_framework.permissions import BasePermission


class IsMobileAuthenticated(BasePermission):
    """Require a valid JWT Bearer token. Session auth is explicitly excluded
    from mobile endpoints so web sessions cannot be used to call them."""

    message = "Valid JWT authentication credentials are required."

    def has_permission(self, request, view):
        from rest_framework_simplejwt.authentication import JWTAuthentication

        if not request.user or not request.user.is_authenticated:
            return False
        # Ensure auth was via JWT, not a session cookie
        authenticator = request.successful_authenticator
        return isinstance(authenticator, JWTAuthentication)
