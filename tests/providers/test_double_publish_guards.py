"""Un timeout o 5xx tras el paso que publica no se reintenta (doble publicación)."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from providers import get_provider
from providers.base import MAYBE_PUBLISHED_MSG
from providers.exceptions import APIError, PublishError
from providers.facebook import FacebookProvider
from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent


def _mock_client(*, error=None, status=200):
    client = MagicMock()
    client.__enter__.return_value = client
    if error:
        client.request.side_effect = error
    else:
        resp = MagicMock(status_code=status, text="boom")
        resp.json.return_value = {}
        client.request.return_value = resp
    return client


class TestMayHavePublished:
    def setup_method(self):
        self.p = get_provider("facebook", {"app_id": "a", "app_secret": "b"})

    def test_read_timeout_on_publishing_call_is_not_retryable(self):
        with (
            patch("providers.base.httpx.Client", return_value=_mock_client(error=httpx.ReadTimeout("t"))),
            pytest.raises(PublishError) as e,
        ):
            self.p._request("POST", "https://x", may_have_published=True)
        assert e.value.retryable is False
        assert str(e.value) == MAYBE_PUBLISHED_MSG

    def test_5xx_on_publishing_call_is_not_retryable(self):
        with (
            patch("providers.base.httpx.Client", return_value=_mock_client(status=502)),
            pytest.raises(PublishError) as e,
        ):
            self.p._request("POST", "https://x", may_have_published=True)
        assert e.value.retryable is False

    def test_4xx_stays_a_normal_api_error(self):
        with (
            patch("providers.base.httpx.Client", return_value=_mock_client(status=400)),
            pytest.raises(APIError),
        ):
            self.p._request("POST", "https://x", may_have_published=True)

    def test_default_request_keeps_old_behaviour(self):
        with (
            patch("providers.base.httpx.Client", return_value=_mock_client(error=httpx.ReadTimeout("t"))),
            pytest.raises(httpx.ReadTimeout),
        ):
            self.p._request("POST", "https://x")

    def test_connect_timeout_is_still_retryable(self):
        with (
            patch("providers.base.httpx.Client", return_value=_mock_client(error=httpx.ConnectTimeout("t"))),
            pytest.raises(httpx.ConnectTimeout),
        ):
            self.p._request("POST", "https://x", may_have_published=True)


def test_facebook_multi_photo_keeps_staged_photos_after_feed_timeout():
    p = FacebookProvider({"app_id": "a", "app_secret": "b"})
    content = PublishContent(text="t", media_urls=["https://i/1.jpg", "https://i/2.jpg"], post_type=PostType.IMAGE)
    ambiguous = PublishError(MAYBE_PUBLISHED_MSG, retryable=False)
    stage = MagicMock()
    stage.json.return_value = {"id": "ph"}
    with (
        patch.object(p, "_request", side_effect=[stage, stage, ambiguous]),
        patch.object(p, "_delete_staged_photos") as delete,
        pytest.raises(PublishError),
    ):
        p._publish_multi_photo("tok", "page", content)
    delete.assert_not_called()


def test_tiktok_chunk_failure_keeps_publish_id_and_stays_pending(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    p = TikTokProvider({"client_key": "k", "client_secret": "s"})
    content = PublishContent(text="t", media_files=[str(video)], post_type=PostType.VIDEO)
    init = {"data": {"publish_id": "pub1", "upload_url": "https://up"}}
    with (
        patch.object(p, "_build_post_info", return_value={}),
        patch.object(p, "_init_video_publish", return_value=init) as init_call,
        patch.object(p, "_request", side_effect=httpx.ReadTimeout("t")),
    ):
        result = p._publish_file_upload("tok", content, "SELF_ONLY")
    assert init_call.call_count == 1
    assert result.platform_post_id == "pub1"
    assert result.pending is True
    assert "upload_error" in result.extra


def _tiktok_upload(tmp_path, size, request_effects):
    import providers.tiktok as tt

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * size)
    p = TikTokProvider({"client_key": "k", "client_secret": "s"})
    content = PublishContent(text="t", media_files=[str(video)], post_type=PostType.VIDEO)
    init = {"data": {"publish_id": "pub1", "upload_url": "https://up"}}
    with (
        patch.object(tt, "MAX_SINGLE_CHUNK_SIZE", 4),
        patch.object(tt, "UPLOAD_CHUNK_SIZE", 4),
        patch.object(p, "_build_post_info", return_value={}),
        patch.object(p, "_init_video_publish", return_value=init),
        patch.object(p, "_request", side_effect=request_effects),
    ):
        return p._publish_file_upload("tok", content, "SELF_ONLY")


def test_tiktok_middle_chunk_failure_is_a_normal_error(tmp_path):
    with pytest.raises(httpx.ReadTimeout):
        _tiktok_upload(tmp_path, 8, [httpx.ReadTimeout("t")])


def test_tiktok_last_chunk_failure_stays_pending(tmp_path):
    result = _tiktok_upload(tmp_path, 8, [MagicMock(), httpx.ReadTimeout("t")])
    assert result.platform_post_id == "pub1"
    assert result.pending is True


def test_tiktok_init_flag_depends_on_source():
    p = TikTokProvider({"client_key": "k", "client_secret": "s"})
    resp = MagicMock()
    resp.json.return_value = {}
    for source, expected in (("PULL_FROM_URL", True), ("FILE_UPLOAD", False)):
        with patch.object(p, "_request", return_value=resp) as req:
            p._init_video_publish("tok", {"source_info": {"source": source}})
        assert req.call_args.kwargs["may_have_published"] is expected
