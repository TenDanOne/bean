from pathlib import Path
from urllib.parse import urlparse

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    APP_URL=(str, "http://localhost:8000"),
    STORAGE_BACKEND=(str, "local"),
    EMAIL_BACKEND_TYPE=(str, "smtp"),
    SENTRY_DSN=(str, ""),
    REDIS_URL=(str, ""),
)

environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
APP_URL = env("APP_URL")
PUBLIC_CONTACT_EMAIL = env("BEAN_CONTACT_EMAIL", default="dario.curiel@unx.mx")

# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "django_htmx",
    "tailwind",
    "csp",
    # OAuth 2.1 Authorization Server backing the MCP connector flow.
    "oauth2_provider",
    "apps.background_task_config.BackgroundTaskConfig",
]

LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.organizations",
    "apps.workspaces",
    "apps.members",
    "apps.settings_manager",
    "apps.credentials",
    "apps.social_accounts",
    "apps.media_library",
    "apps.composer",
    "apps.calendar",
    "apps.publisher",
    "apps.notifications",
    "apps.inbox",
    "apps.approvals",
    "apps.client_portal",
    "apps.onboarding",
    "apps.api_keys",
    "apps.api",
    "apps.mcp",
    "apps.oauth_server",
    "apps.analytics",
    "theme",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.accounts.middleware.AuthRateLimitMiddleware",
    "apps.accounts.middleware.TosAcceptanceMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    # After HtmxMiddleware (needs request.htmx) and outside RBACMiddleware so it
    # also catches the PermissionDenied that RBAC itself raises.
    "apps.common.middleware.HtmxErrorMiddleware",
    "apps.members.middleware.RBACMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.notifications.context_processors.unread_notification_count",
                "apps.common.context_processors.sidebar_context",
                "apps.onboarding.context_processors.onboarding_checklist",
            ],
            # {% icon %} is used in well over a hundred templates, most of which
            # load no tag library at all. A builtin spares every one of them a
            # {% load %} line that would only ever be forgotten somewhere.
            "builtins": ["apps.common.templatetags.icons"],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Cache (used by rate limiting, session fallback)
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

# Database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/brightbean"),
}

# Custom user model
AUTH_USER_MODEL = "accounts.User"

# Agent API trusted-proxy list — IPs whose ``X-Forwarded-For`` header we
# will honour for client-IP derivation in the rate-limit throttle and
# audit log. Empty by default so the API uses ``REMOTE_ADDR`` directly,
# which is the only safe choice when the app is reached without a
# trusted proxy in front. Set ``BB_TRUSTED_PROXIES`` in the environment
# (comma-separated) when you run the app behind Cloudflare, an ALB,
# nginx, etc.
# Comma-separated platform keys that override DEFAULT_ENABLED_PLATFORMS (empty = defaults).
BBS_ENABLED_PLATFORMS = tuple(p.strip() for p in env.list("BBS_ENABLED_PLATFORMS", default=[]) if p.strip())

# Bearer secret for POST /cron/run (external scheduler). Empty disables the endpoint.
CRON_SECRET = env("CRON_SECRET", default="")

BB_TRUSTED_PROXIES = tuple(p.strip() for p in env.list("BB_TRUSTED_PROXIES", default=[]) if p.strip())

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

# Password hashing - bcrypt with cost factor 12
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# Internationalization
LANGUAGE_CODE = "es-mx"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Media files
STORAGE_BACKEND = env("STORAGE_BACKEND")
if STORAGE_BACKEND.lower() == "s3":
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    }
    AWS_S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", default="")
    AWS_ACCESS_KEY_ID = env("S3_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("S3_SECRET_ACCESS_KEY", default="")
    AWS_STORAGE_BUCKET_NAME = env("S3_BUCKET_NAME", default="")
    AWS_S3_CUSTOM_DOMAIN = env("S3_CUSTOM_DOMAIN", default="")
    AWS_S3_REGION_NAME = env("S3_REGION_NAME", default="auto")
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = "private"
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = 3600  # 1-hour expiry for presigned URLs
    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": "max-age=86400",
    }
else:
    # Local FS fallback so dev + test environments without S3 credentials
    # can still call default_storage / save uploaded files.
    STORAGES["default"] = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    }
    MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
    MEDIA_URL = "/media/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Sites framework
SITE_ID = 1

# django-allauth
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Google OAuth for user login/signup (separate from PLATFORM_GOOGLE_* used for publishing)
GOOGLE_AUTH_CLIENT_ID = env("GOOGLE_AUTH_CLIENT_ID", default="")
GOOGLE_AUTH_CLIENT_SECRET = env("GOOGLE_AUTH_CLIENT_SECRET", default="")

# Unsplash stock-photo search in the composer (optional; empty disables the feature)
UNSPLASH_ACCESS_KEY = env("UNSPLASH_ACCESS_KEY", default="")

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": GOOGLE_AUTH_CLIENT_ID,
            "secret": GOOGLE_AUTH_CLIENT_SECRET,
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "VERIFIED_EMAIL": True,
    },
}

SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapters.SocialAccountAdapter"

# Sessions
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 14 * 24 * 60 * 60  # 14 days
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_SAVE_EVERY_REQUEST = True  # Sliding window

# Email
EMAIL_BACKEND_TYPE = env("EMAIL_BACKEND_TYPE")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@localhost")

if EMAIL_BACKEND_TYPE == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST", default="localhost")
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Tailwind
TAILWIND_APP_NAME = "theme"

# CSP - Alpine.js standard build requires unsafe-eval for inline expression
# evaluation. Styles use unsafe-inline because Tailwind utility classes are inline.
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = ("'self'", "'unsafe-eval'", "https://cdn.jsdelivr.net")
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net")
CSP_IMG_SRC = ("'self'", "data:", "blob:", "https:")
CSP_FONT_SRC = ("'self'",)
CSP_CONNECT_SRC = ("'self'", "https://cdn.jsdelivr.net")
CSP_MEDIA_SRC = ("'self'", "blob:")
CSP_FORM_ACTION = (
    "'self'",
    "https://accounts.google.com",
    "https://checkout.stripe.com",
    "https://billing.stripe.com",
    "https://www.facebook.com",
    "https://api.instagram.com",
    "https://www.instagram.com",
    "https://www.threads.com",
    "https://threads.net",
    "https://www.linkedin.com",
    "https://www.pinterest.com",
    "https://www.tiktok.com",
)
CSP_INCLUDE_NONCE_IN = ["script-src"]

# Allow media/images from the storage domain in CSP
if STORAGE_BACKEND.lower() == "s3":
    _storage_origin = AWS_S3_CUSTOM_DOMAIN or AWS_S3_ENDPOINT_URL
    if _storage_origin:
        if not _storage_origin.startswith("https://"):
            _storage_origin = f"https://{_storage_origin}"
        _parsed = urlparse(_storage_origin)
        _storage_origin = f"{_parsed.scheme}://{_parsed.hostname}"
        CSP_MEDIA_SRC = (*CSP_MEDIA_SRC, _storage_origin)  # type: ignore[assignment]
        CSP_IMG_SRC = (*CSP_IMG_SRC, _storage_origin)  # type: ignore[assignment]

# Media Library
MEDIA_LIBRARY_MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB
MEDIA_LIBRARY_MAX_VIDEO_SIZE = 1024 * 1024 * 1024  # 1GB
MEDIA_LIBRARY_MAX_BULK_UPLOAD = 50
MEDIA_LIBRARY_THUMBNAIL_SIZE = (400, 400)
MEDIA_LIBRARY_FFMPEG_TIMEOUT = 300  # 5 minutes
MEDIA_LIBRARY_MAX_CONCURRENT_TRANSCODES = 2
MEDIA_LIBRARY_PRESIGN_EXPIRE = 900  # seconds a presigned direct-upload URL stays valid

# Storage quota — bounds the total bytes a single Organization can
# accumulate in the media library. Without this, an Agent API key with
# ``upload_media`` permission could fill the bucket unbounded.
#
# Resolution order (high → low precedence):
#   1. OrgSetting ``media.storage_quota_bytes_override`` (manual support
#      exception or enterprise contract).
#   2. ``STORAGE_QUOTA_DEFAULT``.
# See ``apps.media_library.quotas.resolve_storage_quota``.
STORAGE_QUOTA_ENABLED = env.bool("STORAGE_QUOTA_ENABLED", default=True)
STORAGE_QUOTA_DEFAULT = 5 * 1024**3  # 5 GB

# Advanced scheduling — posting slots, queues and recurrence rules.
#
# The code exists (19 routes, 4 models) but has never been verified end to end.
# Six separate cases of "the screen is there, the backend is half-wired" turned
# up while auditing this codebase, so leaving an untested feature reachable is
# how someone on the team ends up hitting a broken one. Flip this to True after
# it has been exercised and covered by tests.
BBS_SCHEDULING_ENABLED = env.bool("BBS_SCHEDULING_ENABLED", default=False)

# Encryption key derivation salt - MUST be set per-deployment via environment
ENCRYPTION_KEY_SALT = env("ENCRYPTION_KEY_SALT", default="").encode("utf-8") or None

# Sentry
SENTRY_DSN = env("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
    )

# Platform credentials env vars (cloud version)
_META_CREDENTIALS = {
    "app_id": env("PLATFORM_FACEBOOK_APP_ID", default=""),
    "app_secret": env("PLATFORM_FACEBOOK_APP_SECRET", default=""),
}
_GOOGLE_CREDENTIALS = {
    "client_id": env("PLATFORM_GOOGLE_CLIENT_ID", default=""),
    "client_secret": env("PLATFORM_GOOGLE_CLIENT_SECRET", default=""),
}
_INSTAGRAM_LOGIN_CREDENTIALS = {
    "app_id": env("PLATFORM_INSTAGRAM_APP_ID", default=""),
    "app_secret": env("PLATFORM_INSTAGRAM_APP_SECRET", default=""),
}
_LINKEDIN_LEGACY_CLIENT_ID = env("PLATFORM_LINKEDIN_CLIENT_ID", default="")
_LINKEDIN_LEGACY_CLIENT_SECRET = env("PLATFORM_LINKEDIN_CLIENT_SECRET", default="")

# LinkedIn Company always uses Community Management API scopes (the only path that
# works for Company Pages). Falls back to legacy shared creds for backward compat.
_LINKEDIN_COMPANY_CREDENTIALS = {
    "client_id": env("PLATFORM_LINKEDIN_COMPANY_CLIENT_ID", default="") or _LINKEDIN_LEGACY_CLIENT_ID,
    "client_secret": env("PLATFORM_LINKEDIN_COMPANY_CLIENT_SECRET", default="") or _LINKEDIN_LEGACY_CLIENT_SECRET,
}

# LinkedIn Personal credential resolution + auto-derived OAuth mode:
#   1. PLATFORM_LINKEDIN_PERSONAL_* set -> dedicated personal app -> OIDC + Share scopes
#      (the only personal-posting tier obtainable without CM approval)
#   2. Else, reuse the company app -> CM scopes (refresh tokens + inbox supported)
#   3. Else, empty placeholder
# `_oauth_mode` is computed here, never user-set; it lives in the credentials dict
# so the provider can branch on it without importing settings.
_LINKEDIN_PERSONAL_CLIENT_ID = env("PLATFORM_LINKEDIN_PERSONAL_CLIENT_ID", default="")
if _LINKEDIN_PERSONAL_CLIENT_ID:
    _LINKEDIN_PERSONAL_CREDENTIALS = {
        "client_id": _LINKEDIN_PERSONAL_CLIENT_ID,
        "client_secret": env("PLATFORM_LINKEDIN_PERSONAL_CLIENT_SECRET", default=""),
        "_oauth_mode": "oidc",
    }
elif _LINKEDIN_COMPANY_CREDENTIALS["client_id"]:
    _LINKEDIN_PERSONAL_CREDENTIALS = {
        **_LINKEDIN_COMPANY_CREDENTIALS,
        "_oauth_mode": "community_management",
    }
else:
    # No LinkedIn env vars set. Keep `_oauth_mode` out so the dict's values are all
    # falsy and `_get_configured_platforms()` doesn't false-positive (it treats any
    # truthy credential value as "configured"). The provider defaults to OIDC mode
    # via `_is_oidc_mode` if it ever sees an empty credentials dict.
    _LINKEDIN_PERSONAL_CREDENTIALS = {"client_id": "", "client_secret": ""}

PLATFORM_CREDENTIALS_FROM_ENV = {
    # Meta platforms - Facebook, Instagram, and Threads share the same app
    "facebook": _META_CREDENTIALS,
    "instagram": _META_CREDENTIALS,
    "threads": _META_CREDENTIALS,
    # Instagram (Direct) - uses Instagram Login with separate Instagram App credentials.
    # Despite the platform key, this targets Professional (Business/Creator) IG accounts
    # without requiring a linked Facebook Page. See providers/instagram_login.py.
    "instagram_login": _INSTAGRAM_LOGIN_CREDENTIALS,
    # LinkedIn - personal can run on its own OIDC + Share app (Path A) or reuse the
    # company app's Community Management API credentials (Path B). See README.
    "linkedin_personal": _LINKEDIN_PERSONAL_CREDENTIALS,
    "linkedin_company": _LINKEDIN_COMPANY_CREDENTIALS,
    "tiktok": {
        "client_key": env("PLATFORM_TIKTOK_CLIENT_KEY", default=""),
        "client_secret": env("PLATFORM_TIKTOK_CLIENT_SECRET", default=""),
    },
    # Google platforms - YouTube and Google Business Profile share the same OAuth client
    "youtube": _GOOGLE_CREDENTIALS,
    "google_business": _GOOGLE_CREDENTIALS,
    "pinterest": {
        "app_id": env("PLATFORM_PINTEREST_APP_ID", default=""),
        "app_secret": env("PLATFORM_PINTEREST_APP_SECRET", default=""),
    },
    # Bluesky - session-based auth (app passwords), no app-level credentials needed
    "bluesky": {},
    # Mastodon - instance-specific OAuth; credentials are registered per-instance
    # on first connect and persisted in MastodonAppRegistration. No repo-wide
    # credentials apply.
    "mastodon": {},
    # DEV.to - per-account API key (no OAuth). The key is supplied by the user
    # at connect time, so no app-level credentials apply (same as Bluesky).
    "devto": {},
}

# Webhook verification
FACEBOOK_WEBHOOK_VERIFY_TOKEN = env("FACEBOOK_WEBHOOK_VERIFY_TOKEN", default="")
INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN = env("INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN", default="")
YOUTUBE_WEBHOOK_SECRET = env("YOUTUBE_WEBHOOK_SECRET", default="")
YOUTUBE_WEBHOOK_VERIFY_TOKEN = env("YOUTUBE_WEBHOOK_VERIFY_TOKEN", default="")

# Rate limiting
RATELIMIT_ENABLE = not DEBUG
RATELIMIT_USE_CACHE = "default"


# ---------------------------------------------------------------------------
# OAuth 2.1 Authorization Server (django-oauth-toolkit) — MCP connector flow
# ---------------------------------------------------------------------------
# Powers the /api/v1/mcp endpoint's native connector flow: an MCP client
# (e.g. Claude Desktop) registers itself via Dynamic Client Registration,
# then runs an authorization-code + PKCE flow against /oauth/authorize/ and
# /oauth/token/. Issued access tokens resolve to a User and act with that
# user's current workspace permissions. Existing bb_studio_ API keys keep
# working unchanged (Claude Code injects them as a static header).
#
# Studio serves the app, the API, and the OAuth server on ONE host, so both
# URLs default to APP_URL (unlike a split app/api deployment).
#   MCP_PUBLIC_BASE_URL  — origin advertised as the /mcp resource server.
#   MCP_OAUTH_ISSUER_URL — origin of the OAuth Authorization Server (where
#     /oauth/authorize/ sends unauthenticated users to log in via allauth).
MCP_PUBLIC_BASE_URL = env("MCP_PUBLIC_BASE_URL", default=APP_URL).rstrip("/")
MCP_OAUTH_ISSUER_URL = env("MCP_OAUTH_ISSUER_URL", default=APP_URL).rstrip("/")

OAUTH2_PROVIDER = {
    "SCOPES": {"mcp": "Usar las herramientas MCP de BEAN en tu nombre"},
    "DEFAULT_SCOPES": ["mcp"],
    "PKCE_REQUIRED": True,
    # Restrict ``code_challenge_method`` to ``S256``. django-oauth-toolkit
    # (and oauthlib under it) accept ``plain`` by default — RFC 7636 §4.2
    # marks ``plain`` as insecure. ``S256OnlyOAuth2Validator`` rejects any
    # authorize request whose method is missing or ``plain`` before a Grant
    # row is written.
    "OAUTH2_VALIDATOR_CLASS": "apps.oauth_server.validator.S256OnlyOAuth2Validator",
    "ACCESS_TOKEN_EXPIRE_SECONDS": 60 * 60,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 60 * 60,
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 600,
    "ROTATE_REFRESH_TOKEN": True,
    "REQUEST_APPROVAL_PROMPT": "auto",
    # Claude's OAuth callback is always https; reject non-TLS redirect URIs.
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https"],
}
