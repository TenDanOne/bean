import logging
import uuid
from typing import Any

from django.db import models

from apps.common.encryption import EncryptedTextField
from apps.common.managers import WorkspaceScopedManager
from apps.credentials.models import PlatformCredential

logger = logging.getLogger(__name__)

# How close to expiry a token has to be before a call refreshes it first.
# Wide enough to absorb clock skew and a slow request, narrow enough that a
# healthy token isn't renewed on every poll.
TOKEN_REFRESH_WINDOW_MINUTES = 10


class SocialAccount(models.Model):
    class ConnectionStatus(models.TextChoices):
        CONNECTED = "connected", "Connected"
        TOKEN_EXPIRING = "token_expiring", "Token Expiring"
        DISCONNECTED = "disconnected", "Disconnected"
        ERROR = "error", "Error"
        # Deliberately retired by an operator. Distinct from DISCONNECTED, which
        # means the platform dropped us: an archived channel stops syncing and
        # publishing but keeps its posts, comments and metrics as a readable
        # record. Removing a channel used to be the only action in the product
        # that destroyed work without asking.
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="social_accounts",
    )
    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
    )
    account_platform_id = models.CharField(
        max_length=255,
        help_text="The account's native ID on the platform.",
    )
    account_name = models.CharField(max_length=255)
    account_handle = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.URLField(max_length=2000, blank=True, default="")
    follower_count = models.IntegerField(default=0)

    # Encrypted OAuth tokens
    oauth_access_token = EncryptedTextField(blank=True, default="")
    oauth_refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(blank=True, null=True)

    # Instance URL for Mastodon and Bluesky PDS
    instance_url = models.URLField(max_length=500, blank=True, default="")

    # Connection health
    connection_status = models.CharField(
        max_length=20,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.CONNECTED,
    )
    last_health_check_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")

    connected_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Per-account override of the Agent-API platform daily-post quota
    # (see apps/api/limits.py::PLATFORM_DAILY_POST_LIMIT). Null = use the
    # platform default. Useful when one specific integration is on a higher
    # upstream tier (e.g. an X account on Pro vs the default Basic cap).
    daily_post_limit_override = models.PositiveIntegerField(blank=True, null=True)

    # Set by the analytics sync layer when the platform rejects an analytics
    # call as insufficient-scope. Surfaces a "Reconnect for analytics" CTA in
    # place of the metric region. Cleared on successful reconnect.
    analytics_needs_reconnect = models.BooleanField(default=False)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "social_accounts_social_account"
        unique_together = [("workspace", "platform", "account_platform_id")]

    def __str__(self):
        return f"{self.account_name} ({self.get_platform_display()})"

    @property
    def is_token_expiring_soon(self) -> bool:
        """Token expires within 7 days."""
        if not self.token_expires_at:
            return False
        from datetime import timedelta

        from django.utils import timezone

        return self.token_expires_at < timezone.now() + timedelta(days=7)

    @property
    def needs_token_refresh(self) -> bool:
        """Token is spent or about to be, so refresh before the next API call.

        Deliberately much tighter than ``is_token_expiring_soon``: Google's
        access tokens live one hour, so a 7-day window is permanently true and
        useless as a trigger. That property stays as the "warn the operator"
        signal; this one gates refresh-before-use.
        """
        if not self.token_expires_at:
            return False
        from datetime import timedelta

        from django.utils import timezone

        return self.token_expires_at < timezone.now() + timedelta(minutes=TOKEN_REFRESH_WINDOW_MINUTES)

    @property
    def needs_reconnect(self) -> bool:
        return self.connection_status in (
            self.ConnectionStatus.DISCONNECTED,
            self.ConnectionStatus.ERROR,
        )

    @property
    def is_archived(self) -> bool:
        """Retired on purpose: keep the history, stop the machinery.

        Archived channels must never be swept up by anything that acts on the
        account — syncing, publishing, health checks — but their posts and
        comments stay readable.
        """
        return self.connection_status == self.ConnectionStatus.ARCHIVED

    def archive(self):
        """Stop all activity on this channel without destroying anything.

        Credentials are cleared because an archived channel has no business
        holding a live token, and keeping one would let a later bug act on a
        channel the operator believes is retired.
        """
        self.connection_status = self.ConnectionStatus.ARCHIVED
        self.oauth_access_token = ""
        self.oauth_refresh_token = ""
        self.token_expires_at = None
        self.last_error = ""
        self.save(
            update_fields=[
                "connection_status",
                "oauth_access_token",
                "oauth_refresh_token",
                "token_expires_at",
                "last_error",
                "updated_at",
            ]
        )

    def get_fresh_access_token(self, provider) -> str:
        """Return a usable access token, renewing it first when it's nearly spent.

        Every caller that hands a token to a provider should go through here.
        The scheduled health check only runs every 6 hours, which is no help for
        tokens that expire in one — that gap is what made inbox sync and replies
        fail with 401 while publishing (which already refreshed inline) worked.

        Best-effort by design: if the refresh itself fails we return the old
        token so the real API error surfaces at the call site, instead of
        masking it with a refresh traceback.
        """
        if self.needs_token_refresh and self.oauth_refresh_token:
            try:
                return self.refresh_oauth_token(provider)
            except Exception:
                logger.exception("Token refresh failed for %s; using the existing token", self)
        return self.oauth_access_token

    def refresh_oauth_token(self, provider) -> str:
        """Refresh this account's OAuth access token via *provider* and persist it.

        Returns the new access token. Propagates whatever the provider's
        ``refresh_token`` raises so callers decide between degrading (publish
        engine keeps the old token) and aborting (composer endpoints 502).
        """
        from datetime import timedelta

        from django.utils import timezone

        new_tokens = provider.refresh_token(self.oauth_refresh_token)
        self.oauth_access_token = new_tokens.access_token
        if new_tokens.refresh_token:
            self.oauth_refresh_token = new_tokens.refresh_token
        if new_tokens.expires_in:
            self.token_expires_at = timezone.now() + timedelta(seconds=new_tokens.expires_in)
        self.connection_status = self.ConnectionStatus.CONNECTED
        self.save(
            update_fields=[
                "oauth_access_token",
                "oauth_refresh_token",
                "token_expires_at",
                "connection_status",
                "updated_at",
            ]
        )
        return new_tokens.access_token

    # Platform character limits
    PLATFORM_CHAR_LIMITS = {
        "facebook": 63206,
        "instagram": 2200,
        "instagram_login": 2200,
        "linkedin_personal": 3000,
        "linkedin_company": 3000,
        "tiktok": 2200,
        "youtube": 5000,
        "pinterest": 500,
        "threads": 500,
        "bluesky": 300,
        "google_business": 1500,
        "mastodon": 500,
        "devto": 25000,
    }

    @property
    def char_limit(self) -> int:
        return self.PLATFORM_CHAR_LIMITS.get(self.platform, 2200)

    # Platform-specific field configuration (which platforms need extra fields)
    PLATFORM_FIELD_CONFIG: dict[str, dict[str, Any]] = {
        "youtube": {
            "needs_title": True,
            "title_max_length": 100,
            "title_label": "Video Title",
            "caption_label": "Description",
            "advanced_fields": ["made_for_kids", "privacy_status", "tags", "thumbnail"],
        },
        "pinterest": {
            "needs_title": True,
            "title_max_length": 100,
            "title_label": "Pin Title",
            "caption_label": "Description",
            "supports_first_comment": False,
            "advanced_fields": ["allow_comments", "show_similar_products", "alt_text", "cover_image"],
        },
        "tiktok": {
            "supports_first_comment": False,
        },
        "bluesky": {
            "supports_first_comment": False,
        },
        "google_business": {
            "supports_first_comment": False,
        },
        "devto": {
            "needs_title": True,
            "title_max_length": 128,
            "title_label": "Article Title",
            "caption_label": "Body (Markdown)",
            "supports_first_comment": False,
        },
    }

    PLATFORM_FIELD_DEFAULTS = {
        "needs_title": False,
        "title_max_length": 0,
        "title_label": "Title",
        "caption_label": "Caption",
        "supports_first_comment": True,
        "advanced_fields": [],
    }

    @property
    def field_config(self) -> dict:
        """Return field configuration for this platform."""
        return {**self.PLATFORM_FIELD_DEFAULTS, **self.PLATFORM_FIELD_CONFIG.get(self.platform, {})}

    def supports_first_comment(self) -> bool:
        """Whether this account can have a first comment posted by the worker.

        Most platforms answer purely from PLATFORM_FIELD_CONFIG. LinkedIn Personal
        is the exception: in OIDC mode the socialActions.CREATE endpoint returns
        403 ACCESS_DENIED because that endpoint is gated on Community Management
        API approval. Resolve credentials and check ``_oauth_mode`` for it.
        """
        if not self.field_config.get("supports_first_comment", True):
            return False
        if self.platform == "linkedin_personal":
            from apps.publisher.engine import _resolve_publish_credentials

            creds = _resolve_publish_credentials(self)
            if creds.get("_oauth_mode", "oidc") == "oidc":
                return False
        return True

    @property
    def platform_icon(self) -> str:
        """Short icon label for platform badges."""
        icons = {
            "facebook": "f",
            "instagram": "ig",
            "instagram_login": "ig",
            "linkedin_personal": "in",
            "linkedin_company": "in",
            "tiktok": "tk",
            "youtube": "yt",
            "pinterest": "pi",
            "threads": "th",
            "bluesky": "bs",
            "google_business": "gb",
            "mastodon": "ma",
            "devto": "dv",
        }
        return icons.get(self.platform, self.platform[:2])


class MastodonAppRegistration(models.Model):
    """Stores per-instance OAuth app registrations for Mastodon federation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instance_url = models.URLField(max_length=500, unique=True)
    client_id = EncryptedTextField()
    client_secret = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "social_accounts_mastodon_app_registration"

    def __str__(self):
        return self.instance_url


class PlatformVisibility(models.Model):
    """Site-wide toggle controlling which platforms appear on the connect page."""

    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
        unique=True,
    )
    is_visible = models.BooleanField(
        default=True,
        help_text="If unchecked, this platform is hidden from the connect page.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "social_accounts_platform_visibility"
        verbose_name = "Connect page platform"
        verbose_name_plural = "Connect page platforms"
        ordering = ["platform"]

    def __str__(self):
        return f"{self.get_platform_display()} ({'visible' if self.is_visible else 'hidden'})"


class AnalyticsPlatformConfig(models.Model):
    """Site-wide toggle controlling which platforms are enabled for the
    Analytics feature.

    App-review timelines for the new analytics scopes (Meta, TikTok) are
    unpredictable, so admins flip platforms on as their approvals land. If
    no rows have ``is_enabled=True`` the Analytics sidebar item is hidden
    entirely (see ``apps.common.context_processors.sidebar_context``).
    """

    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
        unique=True,
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text="If unchecked, this platform is excluded from the Analytics feature.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "social_accounts_analytics_platform_config"
        verbose_name = "Analytics platform"
        verbose_name_plural = "Analytics platforms"
        ordering = ["platform"]

    def __str__(self):
        return f"{self.get_platform_display()} ({'enabled' if self.is_enabled else 'disabled'})"

    @classmethod
    def enabled_platforms(cls) -> list[str]:
        """Return the list of platform slugs with analytics enabled.

        Falls back to "all platforms" if no rows exist yet (fresh DB before
        the seed migration has run). Otherwise honors only ``is_enabled=True``.
        """
        rows = list(cls.objects.values_list("platform", "is_enabled"))
        if not rows:
            return [value for value, _label in PlatformCredential.Platform.choices]
        return [platform for platform, enabled in rows if enabled]
