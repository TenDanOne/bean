"""Tests for YouTubeProvider.get_messages pagination and `since` handling."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from providers.youtube import _SINCE_GRACE_PAGES, YouTubeProvider

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)

FRESH = NOW - timedelta(days=1)
STALE = NOW - timedelta(days=365)


def _ts(value: datetime) -> str:
    """Render a datetime the way the YouTube API does (RFC-3339 with a Z)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _thread(comment_id: str, published: datetime, replies: list[tuple[str, datetime]] | None = None) -> dict:
    thread: dict = {
        "snippet": {
            "topLevelComment": {
                "id": comment_id,
                "snippet": {
                    "videoId": "vid-1",
                    "publishedAt": _ts(published),
                    "authorDisplayName": f"Author {comment_id}",
                    "authorChannelId": {"value": f"chan-{comment_id}"},
                    "textDisplay": f"body of {comment_id}",
                    "authorProfileImageUrl": "",
                },
            }
        }
    }
    if replies:
        thread["replies"] = {
            "comments": [
                {
                    "id": reply_id,
                    "snippet": {
                        "publishedAt": _ts(reply_at),
                        "authorDisplayName": f"Author {reply_id}",
                        "authorChannelId": {"value": f"chan-{reply_id}"},
                        "textDisplay": f"body of {reply_id}",
                        "authorProfileImageUrl": "",
                    },
                }
                for reply_id, reply_at in replies
            ]
        }
    return thread


def _response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _paginated(pages: list[list[dict]], urls: list[str] | None = None):
    """Build a ``_request`` side effect serving ``pages`` of comment threads.

    Page N hands out ``page-N+1`` as its ``nextPageToken`` (the last page hands
    out none), so the provider only ever sees a page it actually asked for.
    Returns ``(side_effect, requested)`` where ``requested`` accumulates the
    page tokens the provider requested — a page absent from it was never
    fetched, which is what the early-exit assertions check. Pass ``urls`` to
    also capture every endpoint hit.
    """
    requested: list[str | None] = []

    def side_effect(method, url, **kwargs):
        if urls is not None:
            urls.append(url)
        if url.endswith("/channels"):
            return _response({"items": [{"id": "channel-1"}]})

        token = kwargs["params"].get("pageToken")
        requested.append(token)
        index = 0 if token is None else int(token.split("-")[1]) - 1
        payload: dict = {"items": pages[index]}
        if index + 1 < len(pages):
            payload["nextPageToken"] = f"page-{index + 2}"
        return _response(payload)

    return side_effect, requested


class TestGetMessagesPagination:
    @patch.object(YouTubeProvider, "_request")
    def test_stops_after_grace_pages_of_stale_threads(self, mock_request):
        """A bounded `since` must not walk the channel's whole comment history."""
        pages = [
            [_thread("fresh-1", FRESH)],
            [_thread("old-1", STALE)],
            [_thread("old-2", STALE)],
            # Never requested: the two preceding pages exhaust the grace budget.
            [_thread("old-3", STALE)],
            [_thread("old-4", STALE)],
        ]
        side_effect, requested = _paginated(pages)
        mock_request.side_effect = side_effect

        messages = YouTubeProvider().get_messages("token", since=CUTOFF)

        assert _SINCE_GRACE_PAGES == 2
        # Page 1 (None) + pages 2 and 3, then the loop gives up.
        assert requested == [None, "page-2", "page-3"]
        assert [m.platform_message_id for m in messages] == ["fresh-1"]

    @patch.object(YouTubeProvider, "_request")
    def test_grace_budget_resets_on_a_fresh_page(self, mock_request):
        """One stale page between fresh ones must not end pagination."""
        pages = [
            [_thread("fresh-1", FRESH)],
            [_thread("old-1", STALE)],
            [_thread("fresh-2", FRESH)],
            [_thread("old-2", STALE)],
        ]
        side_effect, requested = _paginated(pages)
        mock_request.side_effect = side_effect

        messages = YouTubeProvider().get_messages("token", since=CUTOFF)

        assert requested == [None, "page-2", "page-3", "page-4"]
        assert [m.platform_message_id for m in messages] == ["fresh-1", "fresh-2"]

    @patch.object(YouTubeProvider, "_request")
    def test_without_since_walks_every_page(self, mock_request):
        """`since=None` keeps the previous exhaustive behaviour."""
        pages = [
            [_thread("old-1", STALE)],
            [_thread("old-2", STALE)],
            [_thread("old-3", STALE)],
        ]
        side_effect, requested = _paginated(pages)
        mock_request.side_effect = side_effect

        messages = YouTubeProvider().get_messages("token", since=None)

        assert requested == [None, "page-2", "page-3"]
        assert [m.platform_message_id for m in messages] == ["old-1", "old-2", "old-3"]

    @patch.object(YouTubeProvider, "_request")
    def test_fresh_reply_on_stale_thread_is_returned(self, mock_request):
        """A recent reply counts even when its top-level comment is out of window."""
        pages = [[_thread("old-root", STALE, replies=[("new-reply", FRESH), ("old-reply", STALE)])]]
        side_effect, _requested = _paginated(pages)
        mock_request.side_effect = side_effect

        messages = YouTubeProvider().get_messages("token", since=CUTOFF)

        # The stale root is filtered out; the fresh reply survives and keeps its
        # parent link so the thread still reconstructs.
        assert [m.platform_message_id for m in messages] == ["new-reply"]
        assert messages[0].extra["parent_id"] == "old-root"

    @patch.object(YouTubeProvider, "_request")
    def test_no_channel_returns_empty(self, mock_request):
        mock_request.return_value = _response({"items": []})

        assert YouTubeProvider().get_messages("token", since=CUTOFF) == []


class TestChannelIdResolution:
    """The channel ID is already stored on the account — don't pay a quota unit re-deriving it."""

    @patch.object(YouTubeProvider, "_request")
    def test_injected_channel_id_skips_the_channels_call(self, mock_request):
        urls: list[str] = []
        side_effect, _requested = _paginated([[_thread("fresh-1", FRESH)]], urls=urls)
        mock_request.side_effect = side_effect

        provider = YouTubeProvider(credentials={"channel_id": "UC-injected"})
        provider.get_messages("token", since=CUTOFF)

        assert not any(u.endswith("/channels") for u in urls)
        assert len(urls) == 1
        # And the injected ID is what actually gets filtered on.
        params = mock_request.call_args_list[0].kwargs["params"]
        assert params["allThreadsRelatedToChannelId"] == "UC-injected"

    @patch.object(YouTubeProvider, "_request")
    def test_falls_back_to_channels_list_without_credentials(self, mock_request):
        urls: list[str] = []
        side_effect, _requested = _paginated([[_thread("fresh-1", FRESH)]], urls=urls)
        mock_request.side_effect = side_effect

        YouTubeProvider().get_messages("token", since=CUTOFF)

        assert urls[0].endswith("/channels")
        params = mock_request.call_args_list[1].kwargs["params"]
        assert params["allThreadsRelatedToChannelId"] == "channel-1"

    @patch.object(YouTubeProvider, "_request")
    def test_blank_channel_id_falls_back(self, mock_request):
        """An account with an empty account_platform_id must not filter on ""."""
        urls: list[str] = []
        side_effect, _requested = _paginated([[_thread("fresh-1", FRESH)]], urls=urls)
        mock_request.side_effect = side_effect

        YouTubeProvider(credentials={"channel_id": ""}).get_messages("token", since=CUTOFF)

        assert urls[0].endswith("/channels")
