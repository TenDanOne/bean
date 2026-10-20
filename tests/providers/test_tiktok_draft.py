"""TikTok «Enviar como borrador»: Content Posting API de bandeja (inbox), con HTTP simulado."""

from unittest.mock import MagicMock, patch

from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


def _provider():
    return TikTokProvider({"client_key": "k", "client_secret": "s"})


def test_video_scope_upload_is_requested():
    assert "video.upload" in _provider().required_scopes


@patch.object(TikTokProvider, "_request")
def test_draft_file_upload_uses_inbox_without_post_info_nor_creator_info(mock_request, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    mock_request.side_effect = [
        _resp({"data": {"publish_id": "v_inbox_file~1", "upload_url": "https://up"}}),
        _resp({}),
    ]
    content = PublishContent(
        text="t",
        media_files=[str(video)],
        post_type=PostType.VIDEO,
        extra={"tiktok_mode": "draft", "privacy_level": "X"},
    )
    result = _provider().publish_post("tok", content)
    init = mock_request.call_args_list[0]
    assert init.args[1].endswith("/post/publish/inbox/video/init/")
    assert init.kwargs["json"] == {
        "source_info": {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 10, "total_chunk_count": 1}
    }
    assert mock_request.call_count == 2  # init + PUT: sin creator_info
    assert result.platform_post_id == "v_inbox_file~1" and result.pending


@patch.object(TikTokProvider, "_request")
def test_draft_pull_from_url(mock_request):
    mock_request.return_value = _resp({"data": {"publish_id": "v_inbox_url~1"}})
    content = PublishContent(
        text="t", media_urls=["https://cdn/x.mp4"], post_type=PostType.VIDEO, extra={"tiktok_mode": "draft"}
    )
    result = _provider().publish_post("tok", content)
    call = mock_request.call_args
    assert call.args[1].endswith("/post/publish/inbox/video/init/")
    assert call.kwargs["json"] == {"source_info": {"source": "PULL_FROM_URL", "video_url": "https://cdn/x.mp4"}}
    assert call.kwargs["may_have_published"] is True
    assert result.platform_post_id == "v_inbox_url~1"


@patch.object(TikTokProvider, "_request")
def test_direct_mode_still_uses_direct_post_endpoint(mock_request):
    mock_request.side_effect = [_resp({"data": {}}), _resp({"data": {"publish_id": "p"}})]
    content = PublishContent(
        text="t", media_urls=["https://cdn/x.mp4"], post_type=PostType.VIDEO, extra={"tiktok_mode": "direct"}
    )
    _provider().publish_post("tok", content)
    assert mock_request.call_args.args[1].endswith("/post/publish/video/init/")
    assert "post_info" in mock_request.call_args.kwargs["json"]


@patch.object(TikTokProvider, "_request")
def test_draft_photos_use_media_upload_without_privacy(mock_request):
    mock_request.return_value = _resp({"data": {"publish_id": "p9"}})
    content = PublishContent(
        text="desc",
        title="T",
        media_urls=["https://cdn/a.jpg"],
        post_type=PostType.IMAGE,
        extra={"tiktok_mode": "draft", "disable_comment": True, "is_aigc": True},
    )
    _provider().publish_post("tok", content)
    assert mock_request.call_count == 1  # sin creator_info
    payload = mock_request.call_args.kwargs["json"]
    assert mock_request.call_args.args[1].endswith("/post/publish/content/init/")
    assert payload["post_mode"] == "MEDIA_UPLOAD" and payload["media_type"] == "PHOTO"
    assert payload["post_info"] == {"title": "T", "description": "desc"}
    assert "is_aigc" not in payload


@patch.object(TikTokProvider, "_fetch_publish_status")
def test_send_to_user_inbox_is_final_draft(mock_status):
    mock_status.return_value = {"status": "SEND_TO_USER_INBOX"}
    st = _provider().check_publish_status("tok", "v_inbox_file~1")
    assert st.state == "published" and st.platform_post_id is None
    assert st.extra == {"tiktok_draft": True}


@patch.object(TikTokProvider, "_fetch_publish_status")
def test_processing_stays_pending(mock_status):
    mock_status.return_value = {"status": "PROCESSING_UPLOAD"}
    assert _provider().check_publish_status("tok", "x").state == "pending"
