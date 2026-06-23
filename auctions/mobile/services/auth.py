import logging

from django.contrib.auth import authenticate
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class MobileAuthService:
    """Handles credential validation for mobile JWT login.

    The mobile login path issues JWTs, but it must not be a weaker side door than the web login:
    it goes through allauth's authentication backend (which already understands username/email
    login) and then re-applies allauth's account-status gates — most importantly the mandatory
    email-verification policy — so an account that can't log in on the web can't log in here either.
    """

    @staticmethod
    def authenticate(credential: str, password: str, request=None) -> User | None:
        """Authenticate by username or email + password, honouring allauth's login policy.

        Returns the User instance on success, ``None`` on failure (bad credentials, inactive
        account, or — when ``ACCOUNT_EMAIL_VERIFICATION`` is mandatory — an unverified email).
        """
        user = authenticate(request=request, username=credential, password=password)
        if user is None:
            # credential may be an email. Emails are not unique in Django's User model, so try each
            # account sharing that email and log in the one whose password matches (if any).
            for candidate in User.objects.filter(email__iexact=credential):
                user = authenticate(request=request, username=candidate.username, password=password)
                if user is not None:
                    break

        if user is None:
            return None

        if not user.is_active:
            logger.info("Mobile login attempted for inactive user: %s", credential)
            return None

        if not MobileAuthService._email_verification_satisfied(user):
            logger.info("Mobile login blocked for user with unverified email: %s", credential)
            return None

        return user

    @staticmethod
    def _email_verification_satisfied(user) -> bool:
        """Mirror allauth's email-verification gate so mobile matches web login policy.

        When ``ACCOUNT_EMAIL_VERIFICATION`` is "mandatory", allauth refuses web login until the user
        has a verified email address; we enforce the same here. When it's "optional"/"none", login
        is allowed without verification (again matching the web), so we return True.
        """
        from allauth.account import app_settings as allauth_settings
        from allauth.account.utils import has_verified_email

        if allauth_settings.EMAIL_VERIFICATION != allauth_settings.EmailVerificationMethod.MANDATORY:
            return True
        return has_verified_email(user)
