"""Per-organization storage-quota resolution + enforcement.

The single chokepoint is ``apps.media_library.services.create_asset``,
which calls ``enforce_storage_quota`` before persisting a new asset.
Both the cookie-authenticated UI and the Agent API flow through that
service, so quota policy is consistent across surfaces by construction.

Resolution order (high → low precedence):

1. ``OrgSetting`` value at key ``media.storage_quota_bytes_override``
   — manual support exception or enterprise contract.
2. ``settings.STORAGE_QUOTA_DEFAULT`` — everyone else.

The middle tier used to come from an Intelligence subscription plan. That
integration is gone, so ``StorageQuota.plan_slug`` is now always empty — the
field stays because the Agent API's ``/me`` exposes it and already documents
an empty value as the no-subscription case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Sum

from .models import MediaAsset

logger = logging.getLogger(__name__)

OVERRIDE_SETTING_KEY = "media.storage_quota_bytes_override"


@dataclass(frozen=True)
class StorageQuota:
    limit_bytes: int
    plan_slug: str  # Empty string when no IntelligenceSubscription exists.
    source: str  # "override" | "plan" | "default" — for debugging.


class StorageQuotaExceededError(Exception):
    """Raised by ``enforce_storage_quota`` when an upload would push usage
    above the org's resolved limit.

    The API layer maps this to HTTP 413 with the documented body shape
    and ``X-Storage-*`` headers. See ``apps.api.api._http_error_handler``.
    """

    def __init__(self, *, used: int, limit: int, attempted: int):
        self.used = used
        self.limit = limit
        self.attempted = attempted
        super().__init__(f"Storage quota exceeded: used={used} limit={limit} attempted={attempted}")


def _override_bytes(organization) -> int | None:
    """Return an OrgSetting override in bytes, or ``None``.

    Accepts either an integer JSON value or a stringified integer. Anything
    else logs and is ignored — we never want a malformed setting to crash
    uploads.
    """
    try:
        from apps.settings_manager.models import OrgSetting
    except ImportError:
        return None
    try:
        row = OrgSetting.objects.filter(organization=organization, key=OVERRIDE_SETTING_KEY).only("value").first()
    except Exception:  # pragma: no cover
        logger.exception("Failed to read storage quota override for org %s", organization.id)
        return None
    if row is None:
        return None
    raw = row.value
    if isinstance(raw, bool):  # bool is a subclass of int — exclude explicitly
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    logger.warning(
        "Storage quota override for org %s is not a non-negative integer: %r",
        organization.id,
        raw,
    )
    return None


def resolve_storage_quota(organization) -> StorageQuota:
    """Resolve the org's storage cap in bytes, with provenance.

    Always returns a StorageQuota — never raises. When
    ``STORAGE_QUOTA_ENABLED`` is False the limit is still computed (so
    ``/me`` can render it for visibility) but enforcement is skipped.
    """
    override = _override_bytes(organization)
    if override is not None:
        return StorageQuota(limit_bytes=override, plan_slug="", source="override")

    return StorageQuota(
        limit_bytes=int(getattr(settings, "STORAGE_QUOTA_DEFAULT", 5 * 1024**3)),
        plan_slug="",
        source="default",
    )


def used_storage_bytes(organization) -> int:
    """Sum of ``MediaAsset.file_size`` for an organization's assets.

    Live aggregate — fine at realistic scale (a few thousand assets per
    org). Don't denormalize until profiling proves it's needed.
    """
    total = MediaAsset.objects.filter(organization=organization).aggregate(total=Sum("file_size"))["total"]
    return int(total or 0)


def enforce_storage_quota(organization, incoming_bytes: int) -> None:
    """Raise ``StorageQuotaExceededError`` if accepting ``incoming_bytes`` would
    push the org over its resolved limit. No-op when
    ``STORAGE_QUOTA_ENABLED`` is False.
    """
    if not getattr(settings, "STORAGE_QUOTA_ENABLED", True):
        return
    quota = resolve_storage_quota(organization)
    used = used_storage_bytes(organization)
    if used + max(incoming_bytes, 0) > quota.limit_bytes:
        raise StorageQuotaExceededError(used=used, limit=quota.limit_bytes, attempted=int(incoming_bytes))
