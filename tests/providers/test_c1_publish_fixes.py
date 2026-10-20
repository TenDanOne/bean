"""C1: miniatura de YouTube (error visible, tipo real, Shorts) y detección de video en Instagram."""

from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import APIError
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.types import PostType, PublishContent, is_video_url
from providers.youtube import YouTubeProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff\xe0" + b"0" * 20


def _publish(tmp_path, thumb_bytes, post_type=PostType.VIDEO, thumb_side_effect=None):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    thumb = tmp_path / "t.jpg"  # extension deliberately wrong for the PNG case
    thumb.write_bytes(thumb_bytes)
    provider = YouTubeProvider({"client_id": "c", "client_secret": "s"})
    init = MagicMock()
    init.headers = {"Location": "https://up"}
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/videos"):
            return init
        if url.endswith("/thumbnails/set") and thumb_side_effect:
            raise thumb_side_effect
        return MagicMock(status_code=200)

    content = PublishContent(
        text="t",
        title="Mi video",
        media_files=[str(video)],
        post_type=post_type,
        extra={"thumbnail_file": str(thumb)},
    )
    with (
        patch.object(YouTubeProvider, "_request", side_effect=fake_request),
        patch.object(YouTubeProvider, "_upload_resumable", return_value={"id": "vid1"}),
    ):
        result = provider.publish_post("tok", content)
    return result, calls


def test_thumbnail_content_type_comes_from_magic_bytes(tmp_path):
    result, calls = _publish(tmp_path, PNG)
    thumb_call = next(kw for url, kw in calls if url.endswith("/thumbnails/set"))
    assert thumb_call["headers"]["Content-Type"] == "image/png"
    assert "thumbnail_error" not in result.extra


def test_thumbnail_rejection_is_reported_not_swallowed(tmp_path):
    result, _ = _publish(tmp_path, JPG, thumb_side_effect=APIError("quota", platform="youtube"))
    assert result.platform_post_id == "vid1"
    assert "quota" in result.extra["thumbnail_error"]


def test_unknown_thumbnail_format_is_reported(tmp_path):
    result, calls = _publish(tmp_path, b"GIF89a" + b"0" * 10)
    assert "JPG ni PNG" in result.extra["thumbnail_error"]
    assert not any(url.endswith("/thumbnails/set") for url, _ in calls)


def test_shorts_do_not_call_thumbnails_set(tmp_path):
    result, calls = _publish(tmp_path, JPG, post_type=PostType.SHORT)
    assert not any(url.endswith("/thumbnails/set") for url, _ in calls)
    assert "Shorts" in result.extra["thumbnail_error"]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://r2.example/a/b.mp4?X-Amz-Signature=abc", True),
        ("https://r2.example/a/b.MOV", True),
        ("https://r2.example/a/b.jpg?name=clip.mp4", False),
    ],
)
def test_is_video_url_ignores_query_string(url, expected):
    assert is_video_url(url) is expected


@pytest.mark.parametrize("cls,args", [(InstagramProvider, ("ig",)), (InstagramLoginProvider, ())])
def test_instagram_story_with_presigned_video_url_sends_video(cls, args):
    provider = cls({"app_id": "a", "app_secret": "s"}) if cls is InstagramProvider else cls({})
    url = "https://r2.example/x.mp4?X-Amz-Signature=abc"
    content = PublishContent(text="", media_urls=[url], post_type=PostType.STORY)
    with (
        patch.object(cls, "_create_container", return_value="c1") as create,
        patch.object(cls, "_wait_for_container"),
        patch.object(cls, "_publish_container", return_value=MagicMock()),
    ):
        provider._publish_single("tok", *args, content)
    payload = create.call_args.args[-1]
    assert payload["video_url"] == url and "image_url" not in payload


def test_instagram_login_single_video_in_feed_is_a_reel():
    provider = InstagramLoginProvider({})
    content = PublishContent(text="", media_urls=["https://r2.example/x.mp4"], post_type=PostType.VIDEO)
    with (
        patch.object(InstagramLoginProvider, "_create_container", return_value="c1") as create,
        patch.object(InstagramLoginProvider, "_wait_for_container"),
        patch.object(InstagramLoginProvider, "_publish_container", return_value=MagicMock()),
    ):
        provider._publish_single("tok", content)
    assert create.call_args.args[-1]["media_type"] == "REELS"
