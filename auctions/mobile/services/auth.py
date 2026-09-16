import logging

from django.contrib.auth import authenticate
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class MobileAuthService:
    """Credential validation for mobile JWT login.

    The mobile login path issues JWTs, but must not be a weaker side door than the web login: it goes
    through allauth's authentication backend, which already understands username/email login, and then
    re-applies allauth's account-status gates -- most importantly mandatory email verification -- so an
    account that can't log in on the web can't log in here either.
    """

    @staticmethod
    def authenticate(credential: str, password: str, request=None) -> User | None:
        """Authenticate by username or email + password, honouring allauth's login policy.

        Returns the User on success and ``None`` on failure: bad credentials, an inactive account, or an
        unverified email where ``ACCOUNT_EMAIL_VERIFICATION`` is mandatory.
        """
        user = authenticate(request=request, username=credential, password=password)
        if user is None:
            # The credential may be an email, and emails are not unique in Django's User model: try
            # each account sharing it and log in whichever password matches.
            for candidate in User.objects.filter(email__iexact=credential):
                user = authenticate(request=request, username=candidate.username, password=password)
                if user is not None:
                    break

        if user is None:
            return None

        if not user.is_active:
            logger.info("Mobile login attempted for inactive user: %s", credential)
            return None

        if not MobileAuthService.email_verification_satisfied(user):
            logger.info("Mobile login blocked for user with unverified email: %s", credential)
            return None

        return user

    @staticmethod
    def email_verification_satisfied(user) -> bool:
        """Mirror allauth's email-verification gate, so mobile matches the web.

        With ``ACCOUNT_EMAIL_VERIFICATION`` "mandatory", allauth refuses web login until an address is
        verified; "optional" and "none" allow it, and so do we.
        """
        from allauth.account import app_settings as allauth_settings
        from allauth.account.utils import has_verified_email

        if allauth_settings.EMAIL_VERIFICATION != allauth_settings.EmailVerificationMethod.MANDATORY:
            return True
        return has_verified_email(user)
