"""TikTok modo fotos (Content Posting API /post/publish/content/init/), con HTTP simulado."""

from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import PublishError
from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent

URLS = ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


def _content(**kw):
    extra = {"privacy_level": "SELF_ONLY", **kw.pop("extra", {})}
    return PublishContent(
        text="descripción",
        title="Título",
        media_urls=kw.pop("urls", URLS),
        post_type=kw.pop("post_type", PostType.IMAGE),
        extra=extra,
    )


@patch.object(TikTokProvider, "_request")
def test_photo_post_sends_photo_payload(mock_request):
    mock_request.side_effect = [
        _resp({"data": {"privacy_level_options": ["SELF_ONLY"]}}),  # creator_info
        _resp({"data": {"publish_id": "p1"}}),
    ]
    result = TikTokProvider().publish_post("tok", _content(extra={"disable_comment": True, "photo_cover_index": 1}))
    url, payload = mock_request.call_args_list[1].args[1], mock_request.call_args_list[1].kwargs["json"]
    assert url.endswith("/post/publish/content/init/")
    assert payload["media_type"] == "PHOTO" and payload["post_mode"] == "DIRECT_POST"
    assert payload["source_info"] == {"source": "PULL_FROM_URL", "photo_cover_index": 1, "photo_images": URLS}
    assert payload["post_info"]["title"] == "Título" and payload["post_info"]["description"] == "descripción"
    assert payload["post_info"]["privacy_level"] == "SELF_ONLY" and payload["post_info"]["disable_comment"] is True
    assert result.platform_post_id == "p1" and result.pending


@patch.object(TikTokProvider, "_request")
def test_photo_title_is_cut_to_90_and_cover_index_is_clamped(mock_request):
    mock_request.side_effect = [_resp({"data": {}}), _resp({"data": {"publish_id": "p2"}})]
    c = _content(extra={"photo_cover_index": 9})
    c.title = "x" * 120
    TikTokProvider().publish_post("tok", c)
    payload = mock_request.call_args_list[1].kwargs["json"]
    assert len(payload["post_info"]["title"]) == 90
    assert payload["source_info"]["photo_cover_index"] == 1


def test_photo_post_without_urls_fails_without_retry():
    with pytest.raises(PublishError) as exc:
        TikTokProvider().publish_post("tok", _content(urls=[]))
    assert exc.value.retryable is False


@patch.object(TikTokProvider, "_request")
def test_photo_post_info_only_sends_supported_fields(mock_request):
    mock_request.side_effect = [_resp({"data": {}}), _resp({"data": {"publish_id": "p3"}})]
    extra = {
        "disable_comment": True,
        "disable_duet": True,
        "disable_stitch": True,
        "brand_content_toggle": False,
        "is_aigc": True,
    }
    TikTokProvider().publish_post("tok", _content(extra=extra))
    payload = mock_request.call_args_list[1].kwargs["json"]
    assert "disable_duet" not in payload["post_info"] and "disable_stitch" not in payload["post_info"]
    assert payload["post_info"]["disable_comment"] is True and payload["post_info"]["brand_content_toggle"] is False
    assert payload["is_aigc"] is True and "is_aigc" not in payload["post_info"]


@patch.object(TikTokProvider, "_request")
def test_url_ownership_unverified_has_actionable_spanish_message(mock_request):
    from providers.exceptions import APIError

    mock_request.side_effect = [
        _resp({"data": {}}),
        APIError("x", platform="TikTok", status_code=400, raw_response={"error": {"code": "url_ownership_unverified"}}),
    ]
    with pytest.raises(PublishError) as exc:
        TikTokProvider().publish_post("tok", _content())
    assert "S3_CUSTOM_DOMAIN" in str(exc.value) and exc.value.retryable is False


def test_photo_post_rejects_more_than_35_images():
    with pytest.raises(PublishError):
        TikTokProvider().publish_post("tok", _content(urls=[f"https://x/{i}.jpg" for i in range(36)]))
