"""Contract tests for the TikTok Business connection label and credentials."""

from apps.credentials.models import PlatformCredential, derive_is_configured


def test_tiktok_platform_is_presented_as_business_account():
    assert PlatformCredential.Platform.TIKTOK.label == "TikTok Business"


def test_tiktok_business_credentials_require_both_app_values():
    assert derive_is_configured("tiktok", {"client_key": "key", "client_secret": "secret"}) is True
    assert derive_is_configured("tiktok", {"client_key": "key"}) is False
    assert derive_is_configured("tiktok", {"client_secret": "secret"}) is False
