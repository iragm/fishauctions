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
            # Try email-based lookup and retry
            try:
                user_obj = User.objects.get(email__iexact=credential)
                user = authenticate(request=request, username=user_obj.username, password=password)
            except (User.DoesNotExist, User.MultipleObjectsReturned):
                pass

        if user is not None and not user.is_active:
            logger.info("Mobile login attempted for inactive user: %s", credential)
            return None
        return user
