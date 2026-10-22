"""«Subir como borrador»: Facebook (video / Reel sin publicar) y YouTube (privado), con HTTP simulado."""

import contextlib
from unittest.mock import MagicMock

import pytest

from providers.exceptions import PublishError
from providers.facebook import FacebookProvider
from providers.types import PostType, PublishContent
from providers.youtube import YouTubeProvider

URL = "https://cdn.example.com/clip.mp4"


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def _fb():
    p = FacebookProvider({"client_id": "id", "client_secret": "secret"})
    p._request = MagicMock(return_value=_resp({"id": "video-1", "video_id": "video-1"}))
    return p


def _content(post_type, **extra):
    return PublishContent(
        text="t", media_urls=[URL], post_type=post_type, extra={"page_id": "page-1", "entrega": "borrador", **extra}
    )


def test_facebook_video_borrador_sube_sin_publicar():
    p = _fb()
    p.publish_post("tok", _content(PostType.VIDEO))
    sent = p._request.call_args_list[0].kwargs["json"]
    assert sent["published"] is False and sent["unpublished_content_type"] == "DRAFT"


def test_facebook_video_directo_no_manda_published():
    p = _fb()
    content = _content(PostType.VIDEO)
    content.extra.pop("entrega")
    p.publish_post("tok", content)
    assert "published" not in p._request.call_args_list[0].kwargs["json"]


def test_facebook_reel_borrador_termina_en_draft():
    p = _fb()
    p._request.side_effect = [
        _resp({"video_id": "v1", "upload_url": "https://up"}),
        _resp({}),
        _resp({"success": True}),
    ]
    p.publish_post("tok", _content(PostType.REEL))
    assert p._request.call_args_list[2].kwargs["json"]["video_state"] == "DRAFT"


@pytest.mark.parametrize("post_type", [PostType.TEXT, PostType.IMAGE])
def test_facebook_texto_o_foto_con_borrador_nunca_publica_en_vivo(post_type):
    p = _fb()
    with pytest.raises(PublishError) as exc:
        p.publish_post("tok", _content(post_type))
    assert exc.value.retryable is False
    p._request.assert_not_called()


@pytest.mark.parametrize(("entrega", "esperado"), [("borrador", "private"), (None, "unlisted")])
def test_youtube_borrador_sube_privado(entrega, esperado, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    p = YouTubeProvider({"client_id": "id", "client_secret": "secret"})
    p._request = MagicMock(side_effect=[MagicMock(headers={"Location": "https://up"}), _resp({"id": "yt1"})])
    extra = {"privacy_status": "unlisted", **({"entrega": entrega} if entrega else {})}
    content = PublishContent(text="t", title="t", media_files=[str(video)], post_type=PostType.VIDEO, extra=extra)
    with contextlib.suppress(Exception):  # lo posterior a la subida no importa: se revisa solo el metadato inicial
        p.publish_post("tok", content)
    assert p._request.call_args_list[0].kwargs["json"]["status"]["privacyStatus"] == esperado
