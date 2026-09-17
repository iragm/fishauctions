"""Django settings for fishauctions. Reads .env; variables are documented in .env.example."""

import datetime
import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from fishauctions._env import env_has_real_value, parse_bool_env, require_secure_prod_secrets
from fishauctions.firebase_config import load_firebase_client_config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve(strict=True).parent.parent

ADMINS = [("Admin", os.environ.get("ADMIN_EMAIL", "admin@example.com"))]


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("SECRET_KEY", "unsecure")

# Fail closed: unset DEBUG is False. DEBUG also switches email, PayPal and Square to non-production.
DEBUG = parse_bool_env(os.environ.get("DEBUG"), default=False)

# Fail fast on insecure production defaults. Narrow on purpose: feature secrets fail at use time.
if not DEBUG:
    require_secure_prod_secrets(
        {
            "SECRET_KEY": SECRET_KEY,
            "DATABASE_PASSWORD": os.environ.get("DATABASE_PASSWORD"),
            "REDIS_PASSWORD": os.environ.get("REDIS_PASSWORD"),
        }
    )

TEMPLATE_STRING_IF_INVALID = ""

ALLOWED_HOSTS = [
    "localhost",
    "web",
    "nginx",  # Allow Selenium tests to connect via nginx service name
    "127.0.0.1",
    "0.0.0.0",
    os.environ.get("SITE_DOMAIN", ""),
    os.environ.get("ALLOWED_HOST_1", ""),
    os.environ.get("ALLOWED_HOST_2", ""),
    os.environ.get("ALLOWED_HOST_3", ""),
]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "https://" + os.environ.get("SITE_DOMAIN", ""),
    "https://" + os.environ.get("ALLOWED_HOST_1", ""),
    "https://" + os.environ.get("ALLOWED_HOST_2", ""),
    "https://" + os.environ.get("ALLOWED_HOST_3", ""),
]


# Logs go to /home/logs, bind-mounted from ./logs so they survive deploys. If unwritable, fall back
# with a warning rather than crash-looping web and celery over file ownership.
def _first_writable_dir(candidates):
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(candidate, os.W_OK):
            return candidate
    return None


_preferred_log_dir = Path("/home/logs")
LOG_DIR = _first_writable_dir([_preferred_log_dir, Path("/home/app/logs"), BASE_DIR / "logs"])
if LOG_DIR is None:
    # Nothing writable: keep the preferred path so the error names what to fix.
    LOG_DIR = _preferred_log_dir
elif LOG_DIR != _preferred_log_dir:
    sys.stderr.write(
        f"WARNING: {_preferred_log_dir} is not writable; log files will go to {LOG_DIR} instead.\n"
        f"On the host, from the project root, run: sudo chown -R <PUID>:<PGID> ./logs (or rerun ./update.sh)\n"
    )

# Each service writes its own files (LOG_SERVICE_NAME): RotatingFileHandler isn't multiprocess-safe.
# Gunicorn workers still share one file; worst case is a garbled line.
_log_service = os.environ.get("LOG_SERVICE_NAME", "")
_log_suffix = f"-{_log_service}" if _log_service else ""

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module}.{funcName}:{lineno} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{name} {levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "django_file": {
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / f"django{_log_suffix}.log"),
            "maxBytes": 1024 * 1024 * 5,  # 5 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
        "root_file": {
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / f"root{_log_suffix}.log"),
            "maxBytes": 1024 * 1024 * 5,  # 5 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
        "mail_admins": {"level": "ERROR", "class": "django.utils.log.AdminEmailHandler"},
        "null": {
            "class": "logging.NullHandler",
        },
    },
    "root": {
        "handlers": ["console", "root_file"],
        "level": os.getenv("LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["django_file"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "weasyprint": {
            # 'handlers': ["null"],
            "level": "WARNING",
            "propagate": False,
        },
        "fontTools": {
            # 'handlers': ["null"],
            "level": "WARNING",
            "propagate": False,
        },
        # Also to file, so 500s are findable after the email is gone.
        "django.request": {
            "handlers": ["mail_admins", "django_file"],
            "level": "ERROR",
            "propagate": False,
        },
        # Consumer errors are otherwise invisible; ERROR+ emails admins.
        "auctions.consumers": {
            "handlers": ["console", "root_file", "mail_admins"],
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        # Bidding logic: a swallowed error here means a bid may have failed, so page us.
        "auctions.bidding": {
            "handlers": ["console", "root_file", "mail_admins"],
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        # Unhandled exceptions escaping the websocket ASGI app (see asgi.py middleware).
        "auctions.websocket": {
            "handlers": ["console", "root_file", "mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
        # Errors rendering the 404/500 pages, which Django swallows (see auctions/error_views.py).
        "auctions.errorpages": {
            "handlers": ["console", "root_file", "mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}

if DEBUG:
    # Never email admins in development.
    for _logger_config in LOGGING.get("loggers", {}).values():
        handlers = _logger_config.get("handlers")
        if handlers and "mail_admins" in handlers:
            _logger_config["handlers"] = [h for h in handlers if h != "mail_admins"]

# Channels
CHANNEL_LAYERS = {
    "default": {
        # Pub/sub, not the BRPOP layer: its blocking read died at 5s under redis-py 7, dropping
        # lot-page sockets every ~6s and silently losing bids.
        "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
        "CONFIG": {
            "hosts": [
                (
                    "redis://:"
                    + os.environ.get("REDIS_PASSWORD", "unsecure")
                    + "@"
                    + os.environ.get("REDIS_HOST", "redis")
                    + ":6379/0"
                )
            ],
            # "hosts": [('127.0.0.1', 6379)],
        },
    },
}

# Application definition
INSTALLED_APPS = [
    "auctions",
    "dal",
    "dal_select2",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_extensions",
    "django_summernote",
    # 'site_settings',
    "crispy_forms",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # Always installed; whether a button shows depends on SOCIALACCOUNT_PROVIDERS config.
    "allauth.socialaccount.providers.apple",
    "allauth.socialaccount.providers.facebook",
    "django_filters",
    "bootstrap_datepicker_plus",
    "el_pagination",
    "easy_thumbnails",
    "post_office",
    "location_field",
    "channels",
    # 'debug_toolbar', # handy for SQL, but silences errors in channels; also uncomment in MIDDLEWARE and urls.py
    "markdownfield",
    "qr_code",
    "django_tables2",
    "django_htmx",
    "crispy_bootstrap5",
    "django_recaptcha",
    "chartjs",
    "django_ses",
    "webpush",
    "django_celery_beat",
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt.token_blacklist",
    # OAuth 2.1 server for /mcp/. Optional: everything checks auctions.mcp.auth.oauth_enabled().
    "oauth2_provider",
]
ASGI_APPLICATION = "fishauctions.asgi.application"
MIDDLEWARE = [
    # "debug_toolbar.middleware.DebugToolbarMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "auctions.middleware.MobileAppMiddleware",  # Sets request.is_mobile_app from the User-Agent
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "fishauctions.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "auctions.context_processors.google_analytics",
                "auctions.context_processors.google_oauth",
                "auctions.context_processors.google_one_tap",
                "auctions.context_processors.theme",
                "auctions.context_processors.add_location",
                "auctions.context_processors.dismissed_cookies_tos",
                "auctions.context_processors.site_config",
                "auctions.context_processors.add_tz",
                "auctions.context_processors.user_clubs",
                "auctions.context_processors.label_print_method",
                "auctions.context_processors.account_nav",
            ],
            "string_if_invalid": TEMPLATE_STRING_IF_INVALID,
        },
    },
]

WSGI_APPLICATION = "fishauctions.wsgi.application"

LANGUAGE_CODE = "en-us"

TIME_ZONE = os.environ.get("TIME_ZONE", "America/New_York")

USE_I18N = False

USE_L10N = False

USE_TZ = True

DATETIME_FORMAT = "M j, Y P e"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# MD5 hashers for test runs; see the module docstring.
TEST_RUNNER = "fishauctions.test_runner.FastTestRunner"

SITE_ID = 1
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", "127.0.0.1")
SETUP_COMPLETE = parse_bool_env(os.environ.get("SETUP_COMPLETE") or None, default=False)
# On by default. The club is named after NAVBAR_BRAND.
SINGLE_CLUB_MODE = parse_bool_env(os.environ.get("SINGLE_CLUB_MODE") or None, default=True)


STATIC_URL = "static/"
STATIC_ROOT = "/home/app/web/staticfiles/"

# Content-hashed static names so nginx can cache /static/ for a year (fishauctions/static_storage.py).
# Django skips hashing when DEBUG is on.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "fishauctions.static_storage.CacheBustedStaticFilesStorage"},
}

AUTHENTICATION_BACKENDS = [
    # Needed to login by username in Django admin, regardless of `allauth`
    "django.contrib.auth.backends.ModelBackend",
    # `allauth` specific authentication methods, such as login by e-mail
    "allauth.account.auth_backends.AuthenticationBackend",
]

# for https://pypi.org/project/django-webpush/
WEBPUSH_SETTINGS = {
    "VAPID_PUBLIC_KEY": os.environ.get("VAPID_PUBLIC_KEY", "abcde"),
    "VAPID_PRIVATE_KEY": os.environ.get("VAPID_PRIVATE_KEY", "abc"),
    "VAPID_ADMIN_EMAIL": os.environ.get("ADMIN_EMAIL", "admin@example.com"),
}

# Use MariaDB for both dev and testing
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DATABASE_ENGINE", "django.db.backends.mysql"),
        "NAME": os.environ.get("DATABASE_NAME", "auctions"),
        "USER": os.environ.get("DATABASE_USER", "mysqluser"),
        "PASSWORD": os.environ.get("DATABASE_PASSWORD", "unsecure"),
        "HOST": os.environ.get("DATABASE_HOST", "db"),
        "PORT": os.environ.get("DATABASE_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        # Must stay 0 under ASGI: each request's thread is torn down at the end, so a positive value
        # reuses nothing and leaks the connection.
        "CONN_MAX_AGE": 0,  # don't reuse connections for ASGI
        "CONN_HEALTH_CHECKS": True,
        "TEST": {
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_unicode_ci",
        },
    }
}


ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS = True
LOGIN_REDIRECT_URL = "/"
LOGIN_URL = "/login/"
ACCOUNT_FORMS = {
    "signup": "auctions.forms.CustomSignupForm",
    "reset_password": "auctions.forms.CustomResetPasswordForm",
}
ACCOUNT_USERNAME_VALIDATORS = "auctions.validators.USERNAME_VALIDATORS"
# ACCOUNT_AUTHENTICATION_METHOD = "username_email"
ACCOUNT_LOGIN_METHODS = {"username", "email"}
ACCOUNT_CONFIRM_EMAIL_ON_GET = True
ACCOUNT_SIGNUP_FIELDS = ["email*", "first_name*", "last_name*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_LOGIN_ON_PASSWORD_RESET = True
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"
ACCOUNT_CHANGE_EMAIL = True

SESSION_COOKIE_AGE = 1209600 * 100

# Redis-cached sessions with the database behind them: no session query per request, and a Redis
# restart loses nobody.
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# The mobile WebView handoff relies on these server-set cookie flags. Secure outside DEBUG only, since
# dev runs plain http.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

if DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "post_office.EmailBackend"

# EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

POST_OFFICE_EMAIL_BACKEND = os.environ.get("POST_OFFICE_EMAIL_BACKEND", "django_ses.SESBackend")
_site_domain_raw = os.environ.get("SITE_DOMAIN", "127.0.0.1").strip()
_parsed_site_domain = urlsplit(_site_domain_raw if "://" in _site_domain_raw else f"//{_site_domain_raw}")
EMAIL_ROUTING_DOMAIN = (_parsed_site_domain.hostname or _site_domain_raw).strip().lower()
SES_ROUTE_EMAILS_ENABLED = POST_OFFICE_EMAIL_BACKEND == "django_ses.SESBackend" and bool(EMAIL_ROUTING_DOMAIN)
# FCM service-account key (path or inline JSON). Absent: push is off and notifications email instead.
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
# Public Firebase client config for /api/mobile/config/, from the mobile build's files. Never the key.
FIREBASE_ANDROID_CONFIG_FILE = os.environ.get("FIREBASE_ANDROID_CONFIG_FILE", "").strip()
FIREBASE_IOS_CONFIG_FILE = os.environ.get("FIREBASE_IOS_CONFIG_FILE", "").strip()
FIREBASE_CLIENT_CONFIG = load_firebase_client_config(FIREBASE_ANDROID_CONFIG_FILE, FIREBASE_IOS_CONFIG_FILE)
INBOUND_ROUTING_SECRET = os.environ.get("INBOUND_ROUTING_SECRET", "").strip()
# App-association files served from /.well-known/ (auctions.app_links). Per deployment; blank 404s
# rather than claiming no apps, which would look like a working setup.
#
# ANDROID_APP_LINKS: "package=SHA256_FINGERPRINT", comma-separated; the fingerprint Play re-signs with.
# IOS_APP_LINKS: "TEAMID.bundle.id", comma-separated.
ANDROID_APP_LINKS = [entry for entry in os.environ.get("ANDROID_APP_LINKS", "").split(",") if entry.strip()]
IOS_APP_LINKS = [entry for entry in os.environ.get("IOS_APP_LINKS", "").split(",") if entry.strip()]
DEFAULT_FROM_EMAIL = (
    f"info@{EMAIL_ROUTING_DOMAIN}"
    if SES_ROUTE_EMAILS_ENABLED
    else os.environ.get("DEFAULT_FROM_EMAIL", "user@example.com")
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL

POST_OFFICE = {
    "MAX_RETRIES": 4,
    "RETRY_INTERVAL": datetime.timedelta(minutes=15),  # Schedule to be retried 15 minutes later
    "BACKENDS": {
        "default": POST_OFFICE_EMAIL_BACKEND,
    },
    "CELERY_ENABLED": True,  # Enable Celery for immediate email delivery
}
# Also a privacy setting: sent mail holds addresses. Cleaned daily by tasks.cleanup_mail.
MAIL_RETENTION_DAYS = int(os.environ.get("MAIL_RETENTION_DAYS", "30"))
# django-ses configuration
AWS_SES_AUTO_THROTTLE = 0.5
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_SES_REGION_NAME = os.environ.get("AWS_SES_REGION_NAME", "us-east-1")
AWS_SES_REGION_ENDPOINT = os.environ.get("AWS_SES_REGION_ENDPOINT", "email.us-east-1.amazonaws.com")
USE_SES_V2 = True
AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET", "")
# AWS_SES_FROM_EMAIL is deliberately NOT set: SES uses it to override every message's From header,
# which discarded all the auctions.email_routing aliases. Unset, the message's own from_email is used.

EMAIL_USE_TLS = parse_bool_env(os.environ.get("EMAIL_USE_TLS") or None, default=True)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = os.environ.get("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_SUBJECT_PREFIX = ""

RECAPTCHA_PUBLIC_KEY = os.environ.get("RECAPTCHA_PUBLIC_KEY", "")
RECAPTCHA_PRIVATE_KEY = os.environ.get("RECAPTCHA_PRIVATE_KEY", "")
RECAPTCHA_ENABLED = env_has_real_value(RECAPTCHA_PUBLIC_KEY) and env_has_real_value(RECAPTCHA_PRIVATE_KEY)

CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

MEDIA_URL = "media/"
MEDIA_ROOT = "/home/app/web/mediafiles/"

EL_PAGINATION_PER_PAGE = 20
# SITE_URL = os.environ.get('SITE_URL', BASE_URL)

THUMBNAIL_ALIASES = {
    "": {
        "ad": {"size": (250, 150), "crop": False},
        "lot_list": {"size": (250, 150), "crop": "smart"},
        # Club icon: shown inline next to club names.
        "club_icon": {"size": (128, 128), "crop": "smart"},
        "club_icon_small": {"size": (32, 32), "crop": "smart"},
        # Speaker headshot: square, used in the speaker list, map info windows and detail panel.
        "speaker": {"size": (200, 200), "crop": "smart"},
        # Google Wallet logo: exactly 660x660 JPEG (non-square is rejected, hence upscale).
        "google_wallet_logo": {"size": (660, 660), "crop": "smart", "upscale": True, "format": "JPEG", "quality": 90},
    },
}
THUMBNAIL_DEFAULT_STORAGE_ALIAS = "default"

# Cloudflare Images: when account id, API token and hash are all set, migrated images are served
# from Cloudflare using variants that mirror THUMBNAIL_ALIASES (auctions/cloudflare_images.py).
CLOUDFLARE_IMAGES_ACCOUNT_ID = os.environ.get("CLOUDFLARE_IMAGES_ACCOUNT_ID", "")
CLOUDFLARE_IMAGES_API_TOKEN = os.environ.get("CLOUDFLARE_IMAGES_API_TOKEN", "")
CLOUDFLARE_IMAGES_ACCOUNT_HASH = os.environ.get("CLOUDFLARE_IMAGES_ACCOUNT_HASH", "")
# Optional: serve from your own (Cloudflare-proxied) domain instead of imagedelivery.net
CLOUDFLARE_IMAGES_DOMAIN = os.environ.get("CLOUDFLARE_IMAGES_DOMAIN", "")
CLOUDFLARE_IMAGES_ENABLED = bool(
    CLOUDFLARE_IMAGES_ACCOUNT_ID and CLOUDFLARE_IMAGES_API_TOKEN and CLOUDFLARE_IMAGES_ACCOUNT_HASH
)

# Edge cache purge: a zone-scoped token, separate from the Images one. /media/ is cached thirty days,
# so without it a deleted file (e.g. a DMCA takedown) stays served. Unset is safe and logged.
CLOUDFLARE_ZONE_ID = os.environ.get("CLOUDFLARE_ZONE_ID", "")
CLOUDFLARE_CACHE_PURGE_API_TOKEN = os.environ.get("CLOUDFLARE_CACHE_PURGE_API_TOKEN", "")

SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# Trust nginx's X-Forwarded-Proto so build_absolute_uri() gives https.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS. Env-driven and off by default because the header is sticky: ramp max-age per environment
# (3600, 86400, 604800, 31536000). Not tied to DEBUG, since local prod-mirror boxes run DEBUG=False
# on 127.0.0.1. Leave INCLUDE_SUBDOMAINS and PRELOAD off unless certain.
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = parse_bool_env(os.environ.get("SECURE_HSTS_INCLUDE_SUBDOMAINS"), default=False)
SECURE_HSTS_PRELOAD = parse_bool_env(os.environ.get("SECURE_HSTS_PRELOAD"), default=False)

# Sign in with Apple: web OAuth uses the Services ID, the native app's token uses the bundle id. Both
# are audiences; allauth uses the FIRST for web OAuth, so the Services ID must come first.
APPLE_SIGN_IN_SERVICES_ID = os.environ.get("APPLE_SIGN_IN_SERVICES_ID", "").strip()
APPLE_SIGN_IN_BUNDLE_ID = os.environ.get("APPLE_SIGN_IN_BUNDLE_ID", "").strip()
APPLE_SIGN_IN_TEAM_ID = os.environ.get("APPLE_SIGN_IN_TEAM_ID", "").strip()
APPLE_SIGN_IN_KEY_ID = os.environ.get("APPLE_SIGN_IN_KEY_ID", "").strip()
# The .p8 key, a filename next to .env. Needed for web OAuth and to revoke Apple grants on account
# deletion (auctions/apple_signin.py).
APPLE_SIGN_IN_KEY_FILE = os.environ.get("APPLE_SIGN_IN_KEY_FILE", "").strip()
APPLE_SIGN_IN_PRIVATE_KEY = ""
if APPLE_SIGN_IN_KEY_FILE:
    _apple_key_path = BASE_DIR / APPLE_SIGN_IN_KEY_FILE
    try:
        APPLE_SIGN_IN_PRIVATE_KEY = _apple_key_path.read_text()
    except OSError as _apple_key_err:
        # Boots anyway: native sign-in still works; web OAuth and revocation don't.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "APPLE_SIGN_IN_KEY_FILE=%s could not be read (%s: %s); Apple web login and token revocation disabled.",
            _apple_key_path,
            type(_apple_key_err).__name__,
            _apple_key_err,
        )
# Audiences accepted on a native Apple identity token. Order matters (see above).
APPLE_ALLOWED_AUDIENCES = [aud for aud in (APPLE_SIGN_IN_SERVICES_ID, APPLE_SIGN_IN_BUNDLE_ID) if aud]

# Must match the app id compiled into the mobile build; decides whether the button is offered.
FACEBOOK_APP_ID = os.environ.get("FACEBOOK_APP_ID", "").strip()
FACEBOOK_APP_SECRET = os.environ.get("FACEBOOK_APP_SECRET", "").strip()

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
        "OAUTH_PKCE_ENABLED": True,
        "FETCH_USERINFO": True,
    }
}
if APPLE_ALLOWED_AUDIENCES:
    SOCIALACCOUNT_PROVIDERS["apple"] = {
        "APP": {
            "client_id": ",".join(APPLE_ALLOWED_AUDIENCES),
            # allauth's naming: `secret` is the Key ID, `key` the Team ID, .p8 in certificate_key.
            "secret": APPLE_SIGN_IN_KEY_ID,
            "key": APPLE_SIGN_IN_TEAM_ID,
            "settings": {"certificate_key": APPLE_SIGN_IN_PRIVATE_KEY},
        },
    }
if FACEBOOK_APP_ID and FACEBOOK_APP_SECRET:
    SOCIALACCOUNT_PROVIDERS["facebook"] = {
        "APP": {
            "client_id": FACEBOOK_APP_ID,
            "secret": FACEBOOK_APP_SECRET,
            # Facebook doesn't verify emails, so they must never sign into an existing account. Set at
            # app level: the provider-level key can only turn it on, and the global switch is on.
            "settings": {"email_authentication": False, "verified_email": False},
        },
        "METHOD": "oauth2",
        "SCOPE": ["email", "public_profile"],
    }
# Links a provider sign-in to an existing account by email, verified addresses only (hence Facebook's
# exclusion above).
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_LOGIN_ON_GET = True
# allauth defaults already, pinned because the mobile social endpoint depends on them.
SOCIALACCOUNT_EMAIL_REQUIRED = True
SOCIALACCOUNT_EMAIL_VERIFICATION = "mandatory"
SOCIALACCOUNT_AUTO_SIGNUP = True
# Needed to revoke Apple grants on account deletion, which drops these rows.
SOCIALACCOUNT_STORE_TOKENS = True
SOCIALACCOUNT_ADAPTER = "auctions.social_adapter.FishAuctionsSocialAccountAdapter"

INTERNAL_IPS = [
    #    '127.0.0.1', # uncomment this to enable the django debug toolbar
]

VIEW_WEIGHT = 1
BID_WEIGHT = 10
WEIGHT_AGAINST_TOP_INTEREST = 20

GOOGLE_MEASUREMENT_ID = os.environ.get("GOOGLE_MEASUREMENT_ID", "")
GOOGLE_TAG_ID = os.environ.get("GOOGLE_TAG_ID", "")
GOOGLE_ADSENSE_ID = os.environ.get("GOOGLE_ADSENSE_ID", "")
# Master on/off switch for all ads (AdSense and internal campaign ads). Default on.
SHOW_ADS = parse_bool_env(os.environ.get("SHOW_ADS") or None, default=True)

GOOGLE_OAUTH_LINK = os.environ.get("GOOGLE_OAUTH_LINK", "")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"

LOCATION_FIELD_PATH = "/static/location_field"
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GOOGLE_MAPS_ENABLED = env_has_real_value(GOOGLE_MAPS_API_KEY)
GOOGLE_MAPS_MAP_ID = os.environ.get("GOOGLE_MAPS_MAP_ID") or "DEMO_MAP_ID"

LOCATION_FIELD = {
    "map.provider": "google",
    "map.zoom": 13,
    "search.provider": "google",
    "search.suffix": "",
    # Google
    "provider.google.api": "//maps.google.com/maps/api/js?sensor=false",
    "provider.google.api_key": GOOGLE_MAPS_API_KEY,
    "provider.google.api_libraries": "",
    "provider.google.map.type": "ROADMAP",
    # misc
    "resources.root_path": LOCATION_FIELD_PATH,
    "resources.media": {
        "js": (LOCATION_FIELD_PATH + "/js/form.js",),
    },
}

GOOGLE_MAPS_SERVER_API_KEY = os.environ.get("GOOGLE_MAPS_SERVER_API_KEY", "")

STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Email about the site when a user adds an unverified email address to their auction.
SEND_WELCOME_EMAIL = True

DATA_UPLOAD_MAX_NUMBER_FIELDS = 20000

NAVBAR_BRAND = os.environ.get("NAVBAR_BRAND", "auction.fish")
WEBSITE_FOCUS = os.environ.get("WEBSITE_FOCUS", "items")
COPYRIGHT_MESSAGE = os.environ.get(
    "COPYRIGHT_MESSAGE",
    "bottom text",
)
SHOW_FOOTER_ICON = parse_bool_env(os.environ.get("SHOW_FOOTER_ICON") or None, default=True)
I_BRED_THIS_FISH_LABEL = os.environ.get("I_BRED_THIS_FISH_LABEL", "I bred this fish/propagated this plant")
ALLOW_USERS_TO_CREATE_AUCTIONS = parse_bool_env(os.environ.get("ALLOW_USERS_TO_CREATE_AUCTIONS") or None, default=True)
ALLOW_USERS_TO_CREATE_LOTS = parse_bool_env(os.environ.get("ALLOW_USERS_TO_CREATE_LOTS") or None, default=True)
PAYPAL_ENABLED_FOR_USERS = parse_bool_env(os.environ.get("PAYPAL_ENABLED_FOR_USERS") or None, default=False)
# New users get the assistant by default; per user in the admin, or `manage.py change_assistant off`.
# Still requires a configured model.
ASSISTANT_ENABLED_FOR_USERS = parse_bool_env(os.environ.get("ASSISTANT_ENABLED_FOR_USERS") or None, default=True)
SQUARE_ENABLED_FOR_USERS = parse_bool_env(os.environ.get("SQUARE_ENABLED_FOR_USERS") or None, default=False)
USERS_ARE_TRUSTED_BY_DEFAULT = parse_bool_env(os.environ.get("USERS_ARE_TRUSTED_BY_DEFAULT") or None, default=True)
UNTRUSTED_MESSAGE = os.environ.get(
    "UNTRUSTED_MESSAGE", "You cannot currently promote auctions.  Please contact the website administrator for access."
)
ENABLE_PROMO_PAGE = parse_bool_env(os.environ.get("ENABLE_PROMO_PAGE") or None, default=False)
ENABLE_CLUB_FINDER = parse_bool_env(os.environ.get("ENABLE_CLUB_FINDER") or None, default=True)
ENABLE_HELP = parse_bool_env(os.environ.get("ENABLE_HELP") or None, default=False)
MAILING_ADDRESS = os.environ.get("MAILING_ADDRESS", "No address configured")
WEEKLY_PROMO_MESSAGE = os.environ.get("WEEKLY_PROMO_MESSAGE", "")

# --- DMCA designated agent ---------------------------------------------------------------------
# Published at /dmca/ only if all five resolve, and must match the Copyright Office filing (which
# lapses after three years). ADMIN_EMAIL and MAILING_ADDRESS stand in for the last two.
DMCA_SERVICE_PROVIDER_NAME = os.environ.get("DMCA_SERVICE_PROVIDER_NAME", "")
DMCA_AGENT_NAME = os.environ.get("DMCA_AGENT_NAME", "")
DMCA_AGENT_PHONE = os.environ.get("DMCA_AGENT_PHONE", "")
DMCA_AGENT_EMAIL = os.environ.get("DMCA_AGENT_EMAIL", "")
DMCA_AGENT_ADDRESS = os.environ.get("DMCA_AGENT_ADDRESS", "")

# Command palette assist (auctions/llm.py). Enabled only with an API key.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5-nano")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# Optional: point at any OpenAI-compatible endpoint (proxy, local model) instead of OpenAI.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
# minimal / low / medium / high, or blank to omit. See llm.DEFAULT_REASONING_EFFORT.
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "minimal")

# Public repository for the /mcp/ read_source tool. Paths resolve against the repo's own file list,
# so nothing on this server's disk is reachable. Blank turns the tool off.
SOURCE_CODE_URL = os.environ.get("SOURCE_CODE_URL", "https://github.com/iragm/fishauctions")
SOURCE_CODE_BRANCH = os.environ.get("SOURCE_CODE_BRANCH", "master")

# Too common to use for lot recommendations or category guessing.
IGNORE_WORDS = [
    "albino",
    "red",
    "blue",
    "pair",
    "super",
    "fish",
    "black",
    "breeding",
    "group",
    "fry",
    "female",
    "water",
    "male",
    "trio",
    "green",
    "lot",
    "fin",
    "yellow",
    "gold",
    "large",
    "donation",
    "young",
    "filter",
    "white",
    "fire",
    "blood",
    "and",
    "orange",
    "bag",
    "qty",
    "juvies",
    "starter",
    "adult",
    "hardy",
    "with",
    "small",
    "size",
    "breeders",
    "brown",
    "breeder",
    "pack",
    "two",
    "pink",
    "proven",
    "better",
    "than",
    "more",
    "adults",
    "inch",
    "from",
    "wild",
    "bunch",
    "superb",
    "the",
    "double",
    "reverse",
    "new",
    "test",
]
ONLINE_TUTORIAL_YOUTUBE_ID = "mNcOjAakC4c"
ONLINE_TUTORIAL_CHAPTERS = (
    (0, "Intro"),
    (45, "Account creation"),
    (1 * 60 + 25, "Auction creation"),
    (1 * 60 + 49, "Setting the location to exchange lots"),
    (2 * 60 + 37, "Rules"),
    (4 * 60 + 2, "Joining the auction"),
    (5 * 60 + 2, "Adding lots"),
    (6 * 60 + 30, "Copying lots"),
    (6 * 60 + 51, "Issues with joining your auction"),
    (8 * 60 + 58, "Bidding and proxy bidding"),
    (9 * 60 + 49, "Sniping and the end of the auction"),
    (11 * 60 + 11, "Invoices"),
    (12 * 60 + 9, "PayPal Batch Invoicing"),
    (13 * 60 + 12, "What happens if someone doesn't pay?"),
    (14 * 60 + 20, "Lot labels"),
    (15 * 60 + 23, "Stats"),
    (17 * 60 + 13, "Multi-location auctions"),
    (19 * 60 + 29, "Help and support"),
)
IN_PERSON_TUTORIAL_YOUTUBE_ID = "BXnoMMU_aCQ"
IN_PERSON_TUTORIAL_CHAPTERS = (
    (0, "Intro"),
    (36, "Account creation"),
    (1 * 60 + 17, "Auction creation"),
    (1 * 60 + 45, "Rules"),
    (2 * 60 + 54, "Location"),
    (3 * 60 + 22, "Joining the auction"),
    (3 * 60 + 44, "Adding users manually"),
    (4 * 60 + 31, "Users joining your auction"),
    (4 * 60 + 47, "Auction administrators"),
    (5 * 60 + 42, "Adding lots"),
    (6 * 60 + 10, "Editing lots"),
    (6 * 60 + 34, "Users adding lots"),
    (7 * 60 + 38, "Lot labels"),
    (8 * 60 + 37, "The auction itself: Set lot winners"),
    (9 * 60 + 50, "Some common issues with selling lots"),
    (11 * 60 + 1, "Invoices and payments"),
    (13 * 60 + 3, "Auction hall layout"),
    (14 * 60 + 5, "Images and lots"),
    (14 * 60 + 45, "Selling fees discounts for club members"),
    (15 * 60 + 52, "Changing bidder numbers"),
    (16 * 60 + 38, "Stats"),
    (18 * 60 + 54, "Attrition and Buy Now"),
    (22 * 60 + 50, "Reusing rules in your next auction"),
    (23 * 60 + 25, "Copying users between auctions"),
    (24 * 60 + 00, "Advertising"),
    (24 * 60 + 52, "Help and Support"),
)
HYBRID_TUTORIAL_YOUTUBE_ID = "tLR7l4Xsgtc"
HYBRID_TUTORIAL_CHAPTERS = (
    (0, "Intro"),
    (13, "Enable online bidding"),
    (33, "Bidding"),
    (59, "Seeing the max bid"),
    (1 * 60 + 27, "Setting winners"),
    (2 * 60 + 3, "Buy Now"),
    (2 * 60 + 45, "Payment"),
)
SUMMERNOTE_THEME = "bs5"

SUMMERNOTE_CONFIG = {
    "iframe": True,
    "summernote": {
        # Change editor size
        "width": "100%",
        "disableDragAndDrop": True,
        "toolbar": [
            ["style", ["style"]],
            ["font", ["bold", "italic", "clear"]],
            # ["color", ["color"]],
            [
                "para",
                [
                    "ul",
                    "ol",
                ],
            ],
            [
                "insert",
                [
                    "link",
                ],
            ],  # 'picture', 'video']],
            ["view", ["codeview"]],
            # ['view', ['fullscreen', 'codeview', 'help']],
        ],
    },
    "js": (
        ("/static/summernote/bs5-hack.js"),
        ("/static/summernote/clean-on-paste.js"),
    ),
}

X_FRAME_OPTIONS = "SAMEORIGIN"

PAYPAL_API_BASE = os.environ.get("PAYPAL_API_BASE", "")
if not PAYPAL_API_BASE:
    if DEBUG:
        PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com"
    else:
        PAYPAL_API_BASE = "https://api-m.paypal.com"
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
# Only used for making payments on behalf of others.
PARTNER_MERCHANT_ID = os.environ.get("PARTNER_MERCHANT_ID", "")
PAYPAL_BN_CODE = os.environ.get("PAYPAL_BN_CODE", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_PLATFORM_FEE = Decimal(str(os.environ.get("PAYPAL_PLATFORM_FEE", "0") or "0"))

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://:"
        + os.environ.get("REDIS_PASSWORD", "unsecure")
        + "@"
        + os.environ.get("REDIS_HOST", "redis")
        + ":6379/3",
    }
}


# Celery Broker URL using Redis
CELERY_BROKER_URL = (
    "redis://:" + os.environ.get("REDIS_PASSWORD", "unsecure") + "@" + os.environ.get("REDIS_HOST", "redis") + ":6379/1"
)

# Celery Result Backend using Redis
CELERY_RESULT_BACKEND = (
    "redis://:" + os.environ.get("REDIS_PASSWORD", "unsecure") + "@" + os.environ.get("REDIS_HOST", "redis") + ":6379/2"
)

# Celery Settings
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

# Celery Beat Scheduler (for periodic tasks)
CELERY_BEAT_SCHEDULER = "fishauctions.custom_scheduler:FixedDatabaseScheduler"

# Task time limits (in seconds)
CELERY_TASK_SOFT_TIME_LIMIT = 300  # 5 minutes
CELERY_TASK_TIME_LIMIT = 600  # 10 minutes

# Worker settings
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 1000
# Square settings - OAuth only, no platform credentials
SQUARE_ENVIRONMENT = os.environ.get("SQUARE_ENVIRONMENT", "sandbox" if DEBUG else "production")
SQUARE_APPLICATION_ID = os.environ.get("SQUARE_APPLICATION_ID", "")
SQUARE_CLIENT_SECRET = os.environ.get("SQUARE_CLIENT_SECRET", "")  # For OAuth token exchange
# Webhook signature key for verifying Square webhook notifications
SQUARE_WEBHOOK_SIGNATURE_KEY = os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY", "")
# Email domains blocked by Square payment links (comma-separated)
_blocked_domains_raw = os.environ.get(
    "SQUARE_BLOCKED_EMAIL_DOMAINS", "example.com,example.org,example.net,test.com,invalid.com"
)
SQUARE_BLOCKED_EMAIL_DOMAINS = []
for domain in _blocked_domains_raw.split(","):
    stripped = domain.strip().lower()
    if stripped:
        SQUARE_BLOCKED_EMAIL_DOMAINS.append(stripped)

# Fernet key for encrypted model fields (SquareSeller OAuth tokens). Generate with
# Fernet.generate_key().decode().
_encryption_key = os.environ.get("FIELD_ENCRYPTION_KEY", "")
if not _encryption_key:
    from cryptography.fernet import Fernet
    from django.core.exceptions import ImproperlyConfigured

    # Generate a key and show the user how to add it to .env
    generated_key = Fernet.generate_key().decode()
    env_line = f"FIELD_ENCRYPTION_KEY={generated_key}"
    print("\n" + "=" * 80)  # noqa: T201
    print("FIELD_ENCRYPTION_KEY is required but not set!")  # noqa: T201
    print("Add this line to your .env file:")  # noqa: T201
    print(f"\n{env_line}\n")  # noqa: T201
    print("=" * 80 + "\n")  # noqa: T201
    msg = f"FIELD_ENCRYPTION_KEY environment variable is required. Add this to your .env file: {env_line}"
    raise ImproperlyConfigured(msg)
FIELD_ENCRYPTION_KEY = _encryption_key

# Mailchimp OAuth: one global app, authorized per club.
MAILCHIMP_CLIENT_ID = os.environ.get("MAILCHIMP_CLIENT_ID", "")
MAILCHIMP_CLIENT_SECRET = os.environ.get("MAILCHIMP_CLIENT_SECRET", "")

# Brevo: each club uses its own API key.

# Google Calendar: one global OAuth app; each club gets its own secondary calendar. Redirect URI:
# https://<your-domain>/clubs/google-calendar/callback/
#
# Scope is calendar.app.created ALONE. Pairing it with a sign-in scope makes Google show unticked
# consent checkboxes, and an admin who skipped them got a token with no calendar access. Adding a
# scope back brings that screen back (CALENDAR_SCOPE guards it).
GOOGLE_CALENDAR_CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")
GOOGLE_CALENDAR_CLIENT_SECRET = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", "")
GOOGLE_CALENDAR_SCOPE = (
    os.environ.get("GOOGLE_CALENDAR_SCOPE", "").strip() or "https://www.googleapis.com/auth/calendar.app.created"
)

# Discord bot integration settings
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_BOT_CLIENT_ID = os.environ.get("DISCORD_BOT_CLIENT_ID", "")

# Google Wallet (optional): GOOGLE_WALLET_KEYFILE is a service-account JSON filename next to .env.
# Missing or unreadable hides the button.
GOOGLE_WALLET_ISSUER_ID = os.environ.get("GOOGLE_WALLET_ISSUER_ID", "")
GOOGLE_WALLET_KEYFILE = os.environ.get("GOOGLE_WALLET_KEYFILE", "")
GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL = ""
GOOGLE_WALLET_SERVICE_ACCOUNT_KEY = ""
if GOOGLE_WALLET_KEYFILE:
    import json as _json

    _wallet_keyfile_path = BASE_DIR / GOOGLE_WALLET_KEYFILE
    try:
        with _wallet_keyfile_path.open() as _f:
            _wallet_key = _json.load(_f)
        GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL = _wallet_key.get("client_email", "")
        GOOGLE_WALLET_SERVICE_ACCOUNT_KEY = _wallet_key.get("private_key", "")
    except (OSError, ValueError) as _wallet_err:
        # Don't raise: a misconfigured box still boots, with no wallet button.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "GOOGLE_WALLET_KEYFILE=%s could not be loaded (%s: %s); Google Wallet disabled.",
            _wallet_keyfile_path,
            type(_wallet_err).__name__,
            _wallet_err,
        )

# Apple Wallet (optional): the .p12 and WWDR .pem sit next to .env. Any missing hides the button.
APPLE_WALLET_CERT_FILE = os.environ.get("APPLE_WALLET_CERT_FILE", "")
APPLE_WALLET_CERT_PASSWORD = os.environ.get("APPLE_WALLET_CERT_PASSWORD", "")
APPLE_WALLET_WWDR_FILE = os.environ.get("APPLE_WALLET_WWDR_FILE", "")
APPLE_WALLET_PASS_TYPE_IDENTIFIER = os.environ.get("APPLE_WALLET_PASS_TYPE_IDENTIFIER", "")
APPLE_WALLET_TEAM_IDENTIFIER = os.environ.get("APPLE_WALLET_TEAM_IDENTIFIER", "")
APPLE_WALLET_ORGANIZATION_NAME = os.environ.get("APPLE_WALLET_ORGANIZATION_NAME", "")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "api_key_default": "1000/hour",
        "mobile_auth": "10/min",
        # Sized for admin bulk work: checkout is 2 calls a buyer, labels 1 a lot; 200/hour blocked checkout.
        "mobile_api": "1000/hour",
        # Search-as-you-type peaks ~6 req/sec.
        "mobile_search": "120/min",
        # AR sessions send every few seconds.
        "mobile_ar": "240/min",
        # The app pings at mount, on resume and every 10 minutes.
        "mobile_checkin": "30/hour",
    },
    # JSON only: the browsable API rendered view docstrings and writable-field forms to anyone with a
    # browser. Re-enabled below under DEBUG.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

if DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": datetime.timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": datetime.timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_OBTAIN_SERIALIZER": "rest_framework_simplejwt.serializers.TokenObtainPairSerializer",
}


# --- OAuth 2.1 for the MCP endpoint ------------------------------------------
#
# How Claude's apps get permission to act as a person through /mcp/ (auctions/mcp/auth.py).
OAUTH2_PROVIDER = {
    # Scopes are a ceiling like UserAPIKey.allow_writes, never a grant. ``offline_access`` gets a
    # refresh token.
    "SCOPES": {
        "read": "Look things up: auctions, lots, people, invoices, club members",
        "write": "Add and change things you could change yourself on the website",
        "offline_access": "Stay connected without signing in again",
    },
    # All three by default: a connector that names no scopes silently lost its write tools, and
    # without offline_access it dies after an hour.
    "DEFAULT_SCOPES": ["read", "write", "offline_access"],
    "PKCE_REQUIRED": True,
    # Claude prefers CIMD (DCR registers a new client per connection), but only if the metadata
    # advertises it and "none" auth (below).
    "DCR_ENABLED": True,
    "CIMD_ENABLED": True,
    # Drops grants we don't advertise before mapping; claude.ai's document names a JWT-bearer grant
    # the toolkit would otherwise reject. See auctions/mcp/cimd.py.
    "CIMD_METADATA_FETCHER": "auctions.mcp.cimd.ClientMetadataFetcher",
    # Extends the RFC 8252 loopback port exemption to "localhost", which Claude Code uses.
    "ALLOW_LOCALHOST_LOOPBACK": True,
    # OAuth 2.1 requires rotating public clients' refresh tokens.
    "ROTATE_REFRESH_TOKEN": True,
    # Replaying a rotated token revokes the whole family.
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    # Two refreshes can be in flight at once; without grace the second looks like a replay.
    "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 10,
    # Short-lived; Claude refreshes on 401 or before expiry.
    "ACCESS_TOKEN_EXPIRE_SECONDS": 60 * 60,
    # Six months: clubs meet monthly and auction quarterly, and 30 days signed people out mid-setup.
    # Rotation and reuse protection carry the security.
    "REFRESH_TOKEN_EXPIRE_SECONDS": 60 * 60 * 24 * 180,
    "OAUTH2_PROTECTED_RESOURCE_NAME": "Auction site MCP endpoint",
    # DCR happens before anyone signs in, so registration must be open.
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.dcr.AllowAllDCRPermission",),
    # Only the grants an MCP client uses.
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
    # "none" must be listed or Claude silently falls back to DCR.
    "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": ["none", "client_secret_post", "client_secret_basic"],
    # RFC 9700 hardening, on now (default in toolkit 4.0).
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    # S256 only.
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    # No access tokens in query strings.
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
    # RFC 9207 `iss` in the authorization response (mix-up defence).
    "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
    # check --deploy's W008 (http redirect URIs) is expected: Claude Code's loopback callback is http.
    #
    # COMPLIANT_BCP_RFC9700_TOKEN_STORAGE is left off: token hashing breaks the refresh grace period.
}
