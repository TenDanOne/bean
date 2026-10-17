"""Which of the implemented platforms BBS actually offers.

All 13 providers stay in ``PROVIDER_REGISTRY`` and keep working: this only
controls what appears in the connect flow. Narrowing it means fewer credentials
to configure, less surface to test, and a connect screen that reflects what the
team really uses — without deleting integrations that may be wanted later.

Turning one back on is a one-line change here, or set BBS_ENABLED_PLATFORMS in
the environment (comma-separated) to override without touching code.
"""

from django.conf import settings

DEFAULT_ENABLED_PLATFORMS = (
    "youtube",
    "tiktok",
    "instagram",
    "instagram_login",
    "facebook",
)


def enabled_platform_keys():
    """Platform keys offered in the connect flow."""
    override = getattr(settings, "BBS_ENABLED_PLATFORMS", None)
    if override:
        return tuple(override)
    return DEFAULT_ENABLED_PLATFORMS
