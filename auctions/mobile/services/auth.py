import logging

from django.contrib.auth import authenticate
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class MobileAuthService:
    """Handles credential validation for mobile JWT login."""

    @staticmethod
    def authenticate(credential: str, password: str, request=None) -> User | None:
        """Authenticate by username or email + password.

        Returns the User instance on success, None on failure.
        """
        user = authenticate(request=request, username=credential, password=password)
        if user is None:
            # credential may be an email. Emails are not unique in Django's User model, so try each
            # account sharing that email and log in the one whose password matches (if any).
            for candidate in User.objects.filter(email__iexact=credential):
                user = authenticate(request=request, username=candidate.username, password=password)
                if user is not None:
                    break

        if user is not None and not user.is_active:
            logger.info("Mobile login attempted for inactive user: %s", credential)
            return None
        return user
