import os as _os

from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# CSRF trust for HTTPS tunnels (ngrok, cloudflared, etc.). Django requires the
# request Origin / Referer host to be explicitly trusted for any POST arriving
# through a non-localhost hostname, even with DEBUG=True.
#
# APP_URL is trusted automatically when it's https, since that's already the
# public origin you're serving from — it saves setting the same tunnel URL in
# two places. This used to key off STUDIO_BASE_URL, a variable that only existed
# for the removed Intelligence integration.
_csrf_trusted = []
_app_url = str(globals().get("APP_URL", "")).strip().rstrip("/")
if _app_url.startswith("https://"):
    _csrf_trusted.append(_app_url)
# Extra hosts (comma-separated) e.g. "https://foo.ngrok-free.app,https://bar"
_extra = _os.environ.get("CSRF_TRUSTED_ORIGINS", "").strip()
if _extra:
    _csrf_trusted.extend(o.strip() for o in _extra.split(",") if o.strip())
if _csrf_trusted:
    CSRF_TRUSTED_ORIGINS = _csrf_trusted

# Tunnel-aware redirect handling. ngrok terminates TLS and forwards plain
# HTTP to runserver; without this Django treats requests as http:// and
# OAuth callbacks would otherwise be built with the wrong scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Plain storage in dev — no manifest needed, runserver uses finders directly.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

# Console email backend in development — but only when no SMTP host is set.
# This used to be unconditional, which silently overrode base.py's
# EMAIL_BACKEND_TYPE and made a dev .env with real credentials a no-op: invite
# emails were printed to the runserver terminal and never sent, with nothing to
# indicate why. Configure EMAIL_HOST to actually deliver.
if not env("EMAIL_HOST", default=""):  # noqa: F405
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Disable CSP in development
CSP_REPORT_ONLY = True

# Django debug toolbar (optional)
try:
    import debug_toolbar  # noqa: F401

    INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405
    INTERNAL_IPS = ["127.0.0.1"]
except ImportError:
    pass

SESSION_COOKIE_SECURE = False
