"""C2a: YouTube (categoría, playlist, capítulos, IA), Instagram (portada, colaboradores) y Facebook (link, Reel)."""

from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import APIError, PublishError
from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider
from providers.types import PostType, PublishContent
from providers.youtube import YouTubeProvider, build_chapters_block, chapters_error

CH = [{"t": "00:00", "title": "Intro"}, {"t": "00:30", "title": "Tema"}, {"t": "01:10", "title": "Cierre"}]


def test_chapters_rules():
    assert chapters_error(CH) is None
    assert chapters_error([]) is None
    assert "3" in chapters_error(CH[:2])
    assert "00:00" in chapters_error([{"t": "00:05", "title": "a"}, *CH[1:]])
    assert "10 segundos" in chapters_error([CH[0], {"t": "00:05", "title": "x"}, CH[2]])
    assert "mm:ss" in chapters_error([CH[0], {"t": "xx", "title": "x"}, CH[2]])
    assert build_chapters_block(CH) == "00:00 Intro\n00:30 Tema\n01:10 Cierre"
    with pytest.raises(ValueError):
        build_chapters_block(CH[:2])


def _yt_publish(tmp_path, extra, request_side_effect=None):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    provider = YouTubeProvider({"client_id": "c", "client_secret": "s"})
    init = MagicMock()
    init.headers = {"Location": "https://up"}
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((url, kwargs))
        if request_side_effect and url.endswith("/playlistItems"):
            raise request_side_effect
        return init if url.endswith("/videos") else MagicMock(status_code=200)

    content = PublishContent(text="desc", title="T", media_files=[str(video)], post_type=PostType.VIDEO, extra=extra)
    with (
        patch.object(YouTubeProvider, "_request", side_effect=fake_request),
        patch.object(YouTubeProvider, "_upload_resumable", return_value={"id": "vid1"}),
    ):
        result = provider.publish_post("tok", content)
    return result, calls


def test_youtube_category_chapters_synthetic_and_kids(tmp_path):
    extra = {"category_id": "27", "chapters": CH, "contains_synthetic_media": True, "made_for_kids": False}
    _, calls = _yt_publish(tmp_path, extra)
    body = next(kw["json"] for url, kw in calls if url.endswith("/videos"))
    assert body["snippet"]["categoryId"] == "27"
    assert body["snippet"]["description"].endswith("00:00 Intro\n00:30 Tema\n01:10 Cierre")
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False


def test_youtube_defaults_keep_category_22(tmp_path):
    _, calls = _yt_publish(tmp_path, {})
    body = next(kw["json"] for url, kw in calls if url.endswith("/videos"))
    assert body["snippet"]["categoryId"] == "22"
    assert body["status"]["containsSyntheticMedia"] is False


def test_youtube_invalid_chapters_fail_before_upload(tmp_path):
    with pytest.raises(PublishError):
        _yt_publish(tmp_path, {"chapters": CH[:2]})


def test_youtube_playlist_insert_and_best_effort(tmp_path):
    result, calls = _yt_publish(tmp_path, {"playlist_id": "PL1"})
    item = next(kw for url, kw in calls if url.endswith("/playlistItems"))
    assert item["json"]["snippet"]["playlistId"] == "PL1"
    assert item["json"]["snippet"]["resourceId"]["videoId"] == "vid1"
    assert "thumbnail_error" not in result.extra

    result, _ = _yt_publish(tmp_path, {"playlist_id": "PL1"}, APIError("sin permiso", platform="youtube"))
    assert result.platform_post_id == "vid1"
    assert "lista de reproducción" in result.extra["thumbnail_error"]


def test_youtube_list_playlists_is_cached():
    from django.core.cache import cache

    cache.clear()
    provider = YouTubeProvider({"client_id": "c", "client_secret": "s", "channel_id": "UCx"})
    resp = MagicMock()
    resp.json.return_value = {"items": [{"id": "PL1", "snippet": {"title": "Mi lista"}}]}
    with patch.object(YouTubeProvider, "_request", return_value=resp) as req:
        assert provider.list_playlists("tok", cache_id="acc-1") == [{"id": "PL1", "title": "Mi lista"}]
        assert provider.list_playlists("tok", cache_id="acc-1") == [{"id": "PL1", "title": "Mi lista"}]
    assert req.call_count == 1
    cache.clear()


def _ig(content):
    provider = InstagramProvider({"client_id": "c", "client_secret": "s"})
    created = []

    def create(token, user, payload):
        created.append(payload)
        return "cid"

    with (
        patch.object(InstagramProvider, "_create_container", side_effect=create),
        patch.object(InstagramProvider, "_wait_for_container"),
        patch.object(InstagramProvider, "_publish_container", return_value=MagicMock()),
    ):
        provider.publish_post("tok", content)
    return created


def test_instagram_reel_cover_collaborators_share_to_feed():
    created = _ig(
        PublishContent(
            text="c",
            media_urls=["https://x/v.mp4"],
            post_type=PostType.REEL,
            extra={
                "ig_user_id": "1",
                "cover_url": "https://x/c.jpg",
                "thumb_offset": 500,
                "collaborators": ["@a", "b", "c", "d"],
                "share_to_feed": False,
            },
        )
    )
    payload = created[0]
    assert payload["cover_url"] == "https://x/c.jpg"
    assert "thumb_offset" not in payload  # la imagen manda
    assert payload["collaborators"] == ["a", "b", "c"]
    assert payload["share_to_feed"] is False


def test_instagram_reel_thumb_offset_only_and_story_has_no_collaborators():
    reel = _ig(
        PublishContent(
            text="c",
            media_urls=["https://x/v.mp4"],
            post_type=PostType.REEL,
            extra={"ig_user_id": "1", "thumb_offset": 1500},
        )
    )[0]
    assert reel["thumb_offset"] == 1500 and "cover_url" not in reel and "share_to_feed" not in reel
    story = _ig(
        PublishContent(
            text="",
            media_urls=["https://x/i.jpg"],
            post_type=PostType.STORY,
            extra={"ig_user_id": "1", "collaborators": ["a"]},
        )
    )[0]
    assert "collaborators" not in story


def test_facebook_text_with_link_param():
    provider = FacebookProvider({"client_id": "c", "client_secret": "s"})
    resp = MagicMock()
    resp.json.return_value = {"id": "1_2"}
    with patch.object(FacebookProvider, "_request", return_value=resp) as req:
        provider.publish_post(
            "tok", PublishContent(text="hola", post_type=PostType.TEXT, extra={"page_id": "1", "link": "https://a.mx"})
        )
    assert req.call_args.kwargs["json"] == {"message": "hola", "link": "https://a.mx"}


def test_facebook_reel_start_upload_finish():
    provider = FacebookProvider({"client_id": "c", "client_secret": "s"})
    calls = []

    def fake(method, url, **kwargs):
        calls.append((url, kwargs))
        r = MagicMock()
        r.json.return_value = (
            {"video_id": "V9", "upload_url": "https://rupload/V9"} if len(calls) == 1 else {"success": True}
        )
        return r

    with patch.object(FacebookProvider, "_request", side_effect=fake):
        result = provider.publish_post(
            "tok",
            PublishContent(
                text="desc", media_urls=["https://x/v.mp4"], post_type=PostType.REEL, extra={"page_id": "1"}
            ),
        )
    assert [c[0].rsplit("/", 1)[-1] for c in calls] == ["video_reels", "V9", "video_reels"]
    assert calls[0][1]["json"] == {"upload_phase": "start"}
    assert calls[1][1]["headers"]["file_url"] == "https://x/v.mp4"
    assert calls[2][1]["json"]["upload_phase"] == "finish" and calls[2][1]["json"]["video_state"] == "PUBLISHED"
    assert result.platform_post_id == "V9"


def test_facebook_reel_without_video_id_fails():
    provider = FacebookProvider({"client_id": "c", "client_secret": "s"})
    resp = MagicMock()
    resp.json.return_value = {}
    with patch.object(FacebookProvider, "_request", return_value=resp), pytest.raises(PublishError):
        provider.publish_post(
            "tok",
            PublishContent(text="d", media_urls=["https://x/v.mp4"], post_type=PostType.REEL, extra={"page_id": "1"}),
        )
