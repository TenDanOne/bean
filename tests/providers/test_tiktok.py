"""Tests for TikTokProvider analytics methods (video.list)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from providers.exceptions import APIError, PublishError
from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _date_range() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 6, 4, 23, 59, 59, tzinfo=UTC),
    )


class TestGetAuthUrl:
    def test_web_provider_does_not_declare_pkce(self):
        assert TikTokProvider({"client_key": "k", "client_secret": "s"}).uses_pkce is False

    def test_scopes_are_publish_plus_video_analytics_only(self):
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        url = provider.get_auth_url("https://app.example/cb", "state-123")

        query = parse_qs(urlsplit(url).query)
        assert query["client_key"] == ["k"]
        assert query["response_type"] == ["code"]
        assert query["redirect_uri"] == ["https://app.example/cb"]
        assert query["scope"] == ["user.info.basic,video.publish,video.list"]

    def test_web_authorization_does_not_include_pkce(self):
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        url = provider.get_auth_url("https://app.example/cb", "state-123", code_verifier="verifier-xyz")

        query = parse_qs(urlsplit(url).query)
        assert "code_challenge" not in query
        assert "code_challenge_method" not in query


class TestExchangeCode:
    @patch.object(TikTokProvider, "_request")
    def test_web_exchange_omits_code_verifier(self, mock_request):
        mock_request.return_value = _make_response({"access_token": "tok", "expires_in": 3600})

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider.exchange_code("auth-code", "https://app.example/cb", code_verifier="verifier-xyz")

        _, kwargs = mock_request.call_args
        assert "code_verifier" not in kwargs["data"]
        assert kwargs["data"]["grant_type"] == "authorization_code"

    @patch.object(TikTokProvider, "_request")
    def test_web_exchange_omits_code_verifier_when_absent(self, mock_request):
        mock_request.return_value = _make_response({"access_token": "tok", "expires_in": 3600})

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider.exchange_code("auth-code", "https://app.example/cb")

        _, kwargs = mock_request.call_args
        assert "code_verifier" not in kwargs["data"]


class TestGetPostMetrics:
    @patch.object(TikTokProvider, "_request")
    def test_request_shape_with_video_id(self, mock_request):
        # A bare numeric ID is treated as a TikTok video_id — no publish-status
        # round trip, single POST to /v2/video/query/.
        mock_request.return_value = _make_response({"data": {"videos": []}})

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider.get_post_metrics("token-xyz", "7234567890")

        assert mock_request.call_count == 1
        args, kwargs = mock_request.call_args
        assert args[0] == "POST"
        assert args[1] == "https://open.tiktokapis.com/v2/video/query/"
        assert kwargs["access_token"] == "token-xyz"
        assert kwargs["params"] == {"fields": "id,view_count,like_count,comment_count,share_count"}
        assert kwargs["json"] == {"filters": {"video_ids": ["7234567890"]}}

    @patch.object(TikTokProvider, "_request")
    def test_parses_counts_into_post_metrics(self, mock_request):
        mock_request.return_value = _make_response(
            {
                "data": {
                    "videos": [
                        {
                            "id": "7234567890",
                            "view_count": 1500,
                            "like_count": 80,
                            "comment_count": 12,
                            "share_count": 5,
                        }
                    ]
                }
            }
        )

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "7234567890")

        # video_views is the field the catalog mapper turns into "views".
        assert metrics.video_views == 1500
        assert metrics.likes == 80
        assert metrics.comments == 12
        assert metrics.shares == 5
        # ``engagements`` is intentionally NOT populated — the catalog's
        # ``engagement`` rate is derived from raw parts by
        # ``apps.analytics.derive.engagement_rate``; populating the
        # dataclass field would be dead computation (no snapshot mapping).
        assert metrics.engagements == 0

    @patch.object(TikTokProvider, "_request")
    def test_missing_video_returns_empty_metrics(self, mock_request):
        # TikTok returns an empty videos list if the ID is gone (deleted,
        # privacy-changed, or not yet visible to the API). The sync layer
        # treats empty PostMetrics as "no data" rather than "all zeros".
        mock_request.return_value = _make_response({"data": {"videos": []}})

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "doesnt-exist")

        assert metrics.video_views == 0
        assert metrics.likes == 0
        assert metrics.comments == 0
        assert metrics.shares == 0

    @patch.object(TikTokProvider, "_request")
    def test_missing_fields_default_to_zero(self, mock_request):
        # TikTok occasionally omits counters for sparse videos (e.g. brand-new
        # uploads where share_count hasn't been computed yet). Treat absent
        # keys as zero so the parser doesn't raise.
        mock_request.return_value = _make_response({"data": {"videos": [{"id": "7234567890", "view_count": 100}]}})

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "7234567890")

        assert metrics.video_views == 100
        assert metrics.likes == 0
        assert metrics.comments == 0
        assert metrics.shares == 0

    @patch.object(TikTokProvider, "_request")
    def test_publish_id_resolves_to_video_id_before_query(self, mock_request):
        # ``platform_post_id`` stored by ``publish_post`` is a publish_id
        # (``v_pub_…``), not a video_id. The provider must resolve via
        # /v2/post/publish/status/fetch/ before the analytics call —
        # ``/v2/video/query/`` only accepts video_ids.
        responses = [
            _make_response(
                {
                    "data": {
                        "status": "PUBLISH_COMPLETE",
                        "publicaly_available_post_id": ["7234567890"],
                    }
                }
            ),
            _make_response(
                {
                    "data": {
                        "videos": [
                            {
                                "id": "7234567890",
                                "view_count": 100,
                                "like_count": 10,
                                "comment_count": 2,
                                "share_count": 1,
                            }
                        ]
                    }
                }
            ),
        ]
        mock_request.side_effect = responses

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "v_pub_url~xxxx")

        # Two calls: status fetch then video query.
        assert mock_request.call_count == 2
        assert mock_request.call_args_list[0].args[0] == "POST"
        assert mock_request.call_args_list[0].args[1] == ("https://open.tiktokapis.com/v2/post/publish/status/fetch/")
        assert mock_request.call_args_list[0].kwargs["json"] == {"publish_id": "v_pub_url~xxxx"}
        # Resolved video_id is the one passed to the analytics call.
        assert mock_request.call_args_list[1].kwargs["json"] == {"filters": {"video_ids": ["7234567890"]}}
        assert metrics.video_views == 100

    @patch.object(TikTokProvider, "_request")
    def test_publish_id_in_progress_returns_empty(self, mock_request):
        # While the publish is still processing, there's no video_id yet.
        # Return empty metrics so the sync layer treats it as "no data" and
        # tries again on the next cycle — no /v2/video/query/ call.
        mock_request.return_value = _make_response({"data": {"status": "PROCESSING_UPLOAD"}})

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "v_pub_url~pending")

        # Only the status fetch — never reaches the analytics endpoint.
        assert mock_request.call_count == 1
        assert mock_request.call_args.args[1] == ("https://open.tiktokapis.com/v2/post/publish/status/fetch/")
        assert metrics.video_views == 0

    @patch.object(TikTokProvider, "_request")
    def test_inbox_publish_handle_resolves_via_status_fetch(self, mock_request):
        # When direct-post audit downgrades a publish to the inbox flow,
        # TikTok returns a ``v_inbox_url~`` / ``v_inbox_file~`` handle
        # instead of ``v_pub_…``. The resolver uses a positive numeric
        # check (video IDs are 19-digit numerics) so any non-numeric
        # publish handle goes through status resolution.
        mock_request.side_effect = [
            _make_response(
                {
                    "data": {
                        "status": "PUBLISH_COMPLETE",
                        "publicaly_available_post_id": ["7234567890"],
                    }
                }
            ),
            _make_response({"data": {"videos": [{"id": "7234567890", "view_count": 5}]}}),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "v_inbox_url~v2.xxxx")

        assert mock_request.call_count == 2
        # Status fetch first, with the raw inbox handle.
        assert mock_request.call_args_list[0].kwargs["json"] == {"publish_id": "v_inbox_url~v2.xxxx"}
        # Then video query with the resolved numeric video_id.
        assert mock_request.call_args_list[1].kwargs["json"] == {"filters": {"video_ids": ["7234567890"]}}
        assert metrics.video_views == 5

    @patch.object(TikTokProvider, "_request")
    def test_none_post_id_returns_empty_without_request(self, mock_request):
        # Defensive: a stored NULL/empty platform_post_id (legacy data or
        # publish that crashed pre-store) would AttributeError on
        # ``post_id.startswith(…)``; the resolver bails early instead.
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "")

        assert metrics.video_views == 0
        mock_request.assert_not_called()

    @patch.object(TikTokProvider, "_request")
    def test_string_publicaly_available_post_id_defended(self, mock_request):
        # Defensive: some TikTok response variants have returned the
        # publicaly_available_post_id as a bare string rather than a list.
        # The resolver coerces it without indexing into a single character.
        mock_request.side_effect = [
            _make_response(
                {
                    "data": {
                        "status": "PUBLISH_COMPLETE",
                        "publicaly_available_post_id": "7234567890",
                    }
                }
            ),
            _make_response({"data": {"videos": [{"id": "7234567890", "view_count": 1}]}}),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "v_pub_url~xxx")

        assert metrics.video_views == 1
        assert mock_request.call_args_list[1].kwargs["json"] == {"filters": {"video_ids": ["7234567890"]}}

    @patch.object(TikTokProvider, "_request")
    def test_null_counts_coerce_to_zero(self, mock_request):
        # Defensive: a null in JSON shouldn't blow up int() conversion.
        mock_request.return_value = _make_response(
            {
                "data": {
                    "videos": [
                        {
                            "id": "7234567890",
                            "view_count": None,
                            "like_count": None,
                            "comment_count": None,
                            "share_count": None,
                        }
                    ]
                }
            }
        )

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_post_metrics("token", "7234567890")

        assert metrics.video_views == 0
        assert metrics.likes == 0


class TestGetAccountMetrics:
    @patch.object(TikTokProvider, "_request")
    def test_returns_empty_without_user_info_request(self, mock_request):
        # TikTok analytics is video-only. Account follower totals require
        # user.info.stats, which OAuth no longer requests.
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        metrics = provider.get_account_metrics("token-xyz", _date_range())

        assert metrics.followers is None
        assert metrics.extra == {}
        mock_request.assert_not_called()

    def test_does_not_supports_date_range(self):
        # This remains false because account metrics are not date-ranged and
        # currently not fetched at all.
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        assert provider.account_metrics_supports_date_range is False


class TestAccountMetricsPersistence:
    """Integration checks for TikTok's video-only analytics catalog."""

    def test_tiktok_followers_are_not_persisted(self):
        from apps.analytics.tasks import _account_metrics_to_dict
        from providers.types import AccountMetrics

        metrics = AccountMetrics(followers=4200)

        out = _account_metrics_to_dict(metrics, "tiktok")

        assert "followers" not in out

    def test_followers_not_persisted_for_platforms_without_catalog_entry(self):
        from apps.analytics.tasks import _account_metrics_to_dict
        from providers.types import AccountMetrics

        metrics = AccountMetrics(followers=42)

        for platform in ("facebook", "linkedin_company", "tiktok"):
            out = _account_metrics_to_dict(metrics, platform)
            assert "followers" not in out, f"unexpected followers leak for {platform}"

    def test_tiktok_catalog_is_video_only(self):
        from apps.analytics.metrics import PLATFORM_METRICS, post_metrics_for

        assert PLATFORM_METRICS["tiktok"] == ["views", "likes", "comments", "shares", "engagement"]
        assert post_metrics_for("tiktok") == ["views", "likes", "comments", "shares", "engagement"]


CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
VIDEO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"


def _video_content(**extra) -> PublishContent:
    return PublishContent(
        text="Hello TikTok",
        media_urls=["https://cdn.example.com/video.mp4"],
        post_type=PostType.VIDEO,
        extra=extra,
    )


def _creator_info_response(options: list[str]) -> MagicMock:
    return _make_response(
        {
            "data": {
                "creator_nickname": "janschmitz51",
                "privacy_level_options": options,
                "comment_disabled": False,
                "duet_disabled": False,
                "stitch_disabled": False,
                "max_video_post_duration_sec": 600,
            }
        }
    )


def _init_response() -> MagicMock:
    return _make_response({"data": {"publish_id": "v_pub_url~123"}})


class TestPublishPost:
    @patch.object(TikTokProvider, "_request")
    def test_creator_info_queried_before_video_init(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE", "SELF_ONLY"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        result = provider.publish_post("tok", _video_content())

        urls = [call.args[1] for call in mock_request.call_args_list]
        assert urls == [CREATOR_INFO_URL, VIDEO_INIT_URL]
        assert result.platform_post_id == "v_pub_url~123"
        assert result.pending is True

    @patch.object(TikTokProvider, "_request")
    def test_unaudited_options_block_explicit_public_post_before_init(self, mock_request):
        # Unaudited apps only get SELF_ONLY back — an explicitly public post
        # must fail fast with retryable=False, without ever hitting video/init.
        mock_request.return_value = _creator_info_response(["SELF_ONLY"])

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        with pytest.raises(PublishError) as excinfo:
            provider.publish_post("tok", _video_content(privacy_level="PUBLIC_TO_EVERYONE"))

        assert excinfo.value.retryable is False
        assert "audit" in str(excinfo.value)
        assert "CUENTA de TikTok debe ser privada" in str(excinfo.value)
        assert "«Solo yo»" in str(excinfo.value)
        urls = [call.args[1] for call in mock_request.call_args_list]
        assert VIDEO_INIT_URL not in urls

    @patch.object(TikTokProvider, "_request")
    def test_unaudited_options_use_self_only_for_implicit_default(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["SELF_ONLY"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        result = provider.publish_post("tok", _video_content())

        assert result.platform_post_id == "v_pub_url~123"
        init_call = mock_request.call_args_list[1]
        assert init_call.kwargs["json"]["post_info"]["privacy_level"] == "SELF_ONLY"

    @patch.object(TikTokProvider, "_request")
    def test_self_only_post_allowed_when_unaudited(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["SELF_ONLY"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        result = provider.publish_post("tok", _video_content(privacy_level="SELF_ONLY"))

        assert result.platform_post_id == "v_pub_url~123"
        init_call = mock_request.call_args_list[1]
        assert init_call.kwargs["json"]["post_info"]["privacy_level"] == "SELF_ONLY"

    @patch.object(TikTokProvider, "_request")
    def test_video_over_max_duration_blocked_before_init(self, mock_request):
        # creator_info caps duration at 600s; a 900s video must fail fast,
        # non-retryable, without ever calling video/init.
        mock_request.return_value = _creator_info_response(["PUBLIC_TO_EVERYONE", "SELF_ONLY"])

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        content = PublishContent(
            text="Hello TikTok",
            media_urls=["https://cdn.example.com/video.mp4"],
            post_type=PostType.VIDEO,
            extra={"privacy_level": "PUBLIC_TO_EVERYONE"},
            video_duration_sec=900,
        )
        with pytest.raises(PublishError) as excinfo:
            provider.publish_post("tok", content)

        assert excinfo.value.retryable is False
        assert "600" in str(excinfo.value)
        urls = [call.args[1] for call in mock_request.call_args_list]
        assert VIDEO_INIT_URL not in urls

    @patch.object(TikTokProvider, "_request")
    def test_video_within_max_duration_proceeds(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE", "SELF_ONLY"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        content = PublishContent(
            text="Hello TikTok",
            media_urls=["https://cdn.example.com/video.mp4"],
            post_type=PostType.VIDEO,
            extra={"privacy_level": "PUBLIC_TO_EVERYONE"},
            video_duration_sec=120,
        )
        result = provider.publish_post("tok", content)

        assert result.platform_post_id == "v_pub_url~123"

    @patch.object(TikTokProvider, "_request")
    def test_creator_info_failure_aborts_publish(self, mock_request):
        mock_request.side_effect = [
            APIError("creator_info down", platform="TikTok", status_code=500),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        with pytest.raises(PublishError) as excinfo:
            provider.publish_post("tok", _video_content())

        assert "creator_info" in str(excinfo.value)
        assert excinfo.value.retryable is True
        urls = [call.args[1] for call in mock_request.call_args_list]
        assert urls == [CREATOR_INFO_URL]

    @patch.object(TikTokProvider, "_request")
    def test_creator_info_auth_failure_is_not_retryable(self, mock_request):
        mock_request.side_effect = [APIError("unauthorized", platform="TikTok", status_code=401)]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        with pytest.raises(PublishError) as excinfo:
            provider.publish_post("tok", _video_content())

        assert excinfo.value.retryable is False
        assert "Reconecta" in str(excinfo.value)

    @patch.object(TikTokProvider, "_request")
    def test_init_unaudited_error_becomes_non_retryable(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE", "SELF_ONLY"]),
            APIError(
                "TikTok API error 403",
                platform="TikTok",
                status_code=403,
                raw_response={
                    "error": {
                        "code": "unaudited_client_can_only_post_to_private_accounts",
                        "message": "Please review our integration guidelines",
                    }
                },
            ),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        with pytest.raises(PublishError) as excinfo:
            provider.publish_post("tok", _video_content())

        assert excinfo.value.retryable is False
        assert "audit" in str(excinfo.value)

    @patch.object(TikTokProvider, "_request")
    def test_init_unknown_error_stays_retryable(self, mock_request):
        original = APIError(
            "TikTok API error 500",
            platform="TikTok",
            status_code=500,
            raw_response={"error": {"code": "internal_error"}},
        )
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE"]),
            original,
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        with pytest.raises(APIError) as excinfo:
            provider.publish_post("tok", _video_content())

        assert excinfo.value is original
        assert excinfo.value.retryable is True

    @patch.object(TikTokProvider, "_request")
    def test_optional_post_info_fields_forwarded(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider.publish_post(
            "tok",
            _video_content(
                disable_comment=True,
                brand_content_toggle=True,
                is_aigc=True,
            ),
        )

        post_info = mock_request.call_args_list[1].kwargs["json"]["post_info"]
        assert post_info["disable_comment"] is True
        assert post_info["brand_content_toggle"] is True
        assert post_info["is_aigc"] is True
        # Fields the composer didn't set must not be sent at all.
        assert "disable_duet" not in post_info
        assert "brand_organic_toggle" not in post_info

    @patch.object(TikTokProvider, "_request")
    def test_video_cover_timestamp_forwarded_as_int(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider.publish_post("tok", _video_content(video_cover_timestamp_ms="12500"))

        post_info = mock_request.call_args_list[1].kwargs["json"]["post_info"]
        assert post_info["video_cover_timestamp_ms"] == 12500

    @patch.object(TikTokProvider, "_request")
    def test_video_cover_timestamp_invalid_or_absent_omitted(self, mock_request):
        mock_request.side_effect = [
            _creator_info_response(["PUBLIC_TO_EVERYONE"]),
            _init_response(),
            _creator_info_response(["PUBLIC_TO_EVERYONE"]),
            _init_response(),
        ]

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider.publish_post("tok", _video_content(video_cover_timestamp_ms="not-a-number"))
        provider.publish_post("tok", _video_content())

        first = mock_request.call_args_list[1].kwargs["json"]["post_info"]
        second = mock_request.call_args_list[3].kwargs["json"]["post_info"]
        assert "video_cover_timestamp_ms" not in first
        assert "video_cover_timestamp_ms" not in second

    @patch.object(TikTokProvider, "_request")
    def test_invalid_privacy_level_rejected_without_requests(self, mock_request):
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        with pytest.raises(PublishError) as excinfo:
            provider.publish_post("tok", _video_content(privacy_level="BOGUS"))

        assert excinfo.value.retryable is False
        mock_request.assert_not_called()


class TestCheckPublishStatus:
    @pytest.mark.parametrize(
        ("data", "state", "post_id"),
        [
            (
                {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["7300000000000000001"]},
                "published",
                "7300000000000000001",
            ),
            ({"status": "PROCESSING_UPLOAD"}, "pending", None),
            ({"status": "FAILED", "fail_reason": "file_format_check_failed"}, "failed", None),
        ],
    )
    @patch.object(TikTokProvider, "_request")
    def test_maps_tiktok_status(self, mock_request, data, state, post_id):
        mock_request.return_value = _make_response({"data": data})
        st = TikTokProvider({"client_key": "k", "client_secret": "s"}).check_publish_status("tok", "v_pub_url~1")
        assert st.state == state
        assert st.platform_post_id == post_id
        if state == "failed":
            assert "file_format_check_failed" in st.error


class TestChunkedUpload:
    @patch.object(TikTokProvider, "_request")
    def test_large_file_is_sent_in_chunks(self, mock_request, tmp_path, monkeypatch):
        monkeypatch.setattr("providers.tiktok.MAX_SINGLE_CHUNK_SIZE", 10)
        monkeypatch.setattr("providers.tiktok.UPLOAD_CHUNK_SIZE", 8)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x" * 20)  # 20 // 8 = 2 chunks: 8 + 12
        mock_request.return_value = _make_response({"data": {"upload_url": "https://up", "publish_id": "p1"}})
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        content = PublishContent(text="t", media_files=[str(video)], post_type=PostType.VIDEO)

        provider._publish_file_upload("tok", content, "SELF_ONLY")

        init = mock_request.call_args_list[0].kwargs["json"]["source_info"]
        assert (init["chunk_size"], init["total_chunk_count"]) == (8, 2)
        puts = [c.kwargs["headers"]["Content-Range"] for c in mock_request.call_args_list[1:]]
        assert puts == ["bytes 0-7/20", "bytes 8-19/20"]
