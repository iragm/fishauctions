from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator


def validate_username_no_at_symbol(value):
    """Disallow the @ symbol in usernames to avoid confusion with email addresses."""
    if "@" in value:
        msg = "Usernames cannot contain the @ symbol."
        raise ValidationError(msg)


# Used by ACCOUNT_USERNAME_VALIDATORS in settings to apply to all allauth signup flows.
# Includes Django's default username validators plus the @ restriction.
USERNAME_VALIDATORS = [
    UnicodeUsernameValidator(),
    MaxLengthValidator(150),
    validate_username_no_at_symbol,
]
