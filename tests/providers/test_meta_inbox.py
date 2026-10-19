"""Facebook / Instagram inbox: comment polling, replies, moderation, DMs and deleting posts (HTTP mocked)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from providers.exceptions import APIError
from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider
from providers.meta_inbox import send_dm
from providers.youtube import YouTubeProvider

GRAPH = "https://graph.facebook.com/v25.0"


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def _fb(responses=None, **creds):
    provider = FacebookProvider({"client_id": "id", "client_secret": "s", "page_id": "page-1", **creds})
    provider._request = MagicMock(side_effect=responses) if isinstance(responses, list) else MagicMock()
    return provider


def _ig(responses=None, **creds):
    provider = InstagramProvider({"client_id": "id", "client_secret": "s", "ig_user_id": "ig-1", **creds})
    provider._request = MagicMock(side_effect=responses) if isinstance(responses, list) else MagicMock()
    return provider


def _iso(delta):
    return (datetime.now(UTC) - delta).isoformat()


class TestFacebookComments:
    FEED = {
        "data": [
            {
                "id": "page-1_post-9",
                "comments": {
                    "data": [
                        {
                            "id": "c1",
                            "message": "Hola",
                            "created_time": "2026-10-19T10:00:00+0000",
                            "from": {"id": "u1", "name": "Ana", "picture": {"data": {"url": "http://pic/ana"}}},
                            "comments": {
                                "data": [
                                    {
                                        "id": "c1r1",
                                        "message": "Gracias",
                                        "created_time": "2026-10-19T11:00:00+0000",
                                        "from": {"id": "page-1", "name": "Mi Pagina"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        ]
    }

    def test_polls_feed_with_nested_replies(self):
        provider = _fb([_resp(self.FEED), _resp({"data": []})])
        messages = provider.get_messages("tok")

        feed_call = provider._request.call_args_list[0]
        assert feed_call.args[:2] == ("GET", f"{GRAPH}/page-1/feed")
        assert "comments.limit(50){" in feed_call.kwargs["params"]["fields"]
        by_id = {m.platform_message_id: m for m in messages}
        assert set(by_id) == {"c1", "c1r1"}
        top, reply = by_id["c1"], by_id["c1r1"]
        assert top.message_type == "comment"
        assert top.extra["post_id"] == "post-9"  # the form the publisher stores
        assert top.extra["sender_avatar_url"] == "http://pic/ana"
        assert "parent_id" not in top.extra
        assert reply.extra["parent_id"] == "c1"
        assert reply.extra["sender_handle"] == "page-1"

    def test_since_drops_older_comments_but_keeps_new_replies(self):
        provider = _fb([_resp(self.FEED), _resp({"data": []})])
        messages = provider.get_messages("tok", since=datetime(2026, 10, 19, 10, 30, tzinfo=UTC))
        assert [m.platform_message_id for m in messages] == ["c1r1"]

    def test_comment_failure_does_not_hide_dms(self):
        provider = _fb([APIError("boom"), _resp({"data": []})])
        assert provider.get_messages("tok") == []

    def test_both_sources_failing_raises(self):
        provider = _fb([APIError("a"), APIError("b")])
        with pytest.raises(APIError):
            provider.get_messages("tok")


class TestFacebookDMs:
    def test_dm_carries_partner_and_photo(self):
        provider = _fb(
            [
                _resp({"data": []}),  # feed
                _resp({"data": [{"id": "t_1"}]}),  # conversations
                _resp(
                    {
                        "data": [
                            {
                                "id": "m1",
                                "message": "hola",
                                "created_time": "2026-10-19T10:00:00+0000",
                                "from": {"id": "psid-7", "name": "Beto"},
                                "to": {"data": [{"id": "page-1"}]},
                            },
                            {
                                "id": "m2",
                                "message": "te ayudo",
                                "created_time": "2026-10-19T10:05:00+0000",
                                "from": {"id": "page-1", "name": "Mi Pagina"},
                                "to": {"data": [{"id": "psid-7"}]},
                            },
                        ]
                    }
                ),
                _resp({"name": "Beto", "profile_pic": "http://pic/beto"}),  # profile, cached for both
            ]
        )
        inbound, outbound = provider.get_messages("tok")
        assert inbound.extra["conversation_id"] == "t_1"
        assert inbound.extra["recipient_id"] == "psid-7"
        assert inbound.extra["sender_avatar_url"] == "http://pic/beto"
        assert outbound.extra["recipient_id"] == "psid-7"  # still the other party
        assert outbound.extra["sender_avatar_url"] == ""
        assert provider._request.call_count == 4  # profile fetched once


class TestMetaReplies:
    def test_facebook_comment_reply_posts_to_comment_comments(self):
        provider = _fb()
        provider._request.return_value = _resp({"id": "c-new"})
        result = provider.reply_to_message("tok", "c1", "gracias", {"post_id": "9"})
        provider._request.assert_called_once_with(
            "POST", f"{GRAPH}/c1/comments", access_token="tok", json={"message": "gracias"}
        )
        assert result.platform_message_id == "c-new"

    def test_instagram_comment_reply_posts_to_replies(self):
        provider = _ig()
        provider._request.return_value = _resp({"id": "r-new"})
        result = provider.reply_to_message("tok", "ic1", "gracias", {})
        provider._request.assert_called_once_with(
            "POST", f"{GRAPH}/ic1/replies", access_token="tok", json={"message": "gracias"}
        )
        assert result.platform_message_id == "r-new"

    @pytest.mark.parametrize("make", [_fb, _ig])
    def test_dm_inside_24h_is_a_plain_response(self, make):
        provider = make()
        provider._request.return_value = _resp({"message_id": "mid-1", "recipient_id": "psid-7"})
        result = provider.reply_to_message(
            "tok", "m1", "hola", {"recipient_id": "psid-7", "last_inbound_at": _iso(timedelta(hours=2))}
        )
        args, kwargs = provider._request.call_args
        assert args == ("POST", f"{GRAPH}/me/messages")
        assert kwargs["json"] == {
            "recipient": {"id": "psid-7"},
            "message": {"text": "hola"},
            "messaging_type": "RESPONSE",
        }
        assert result.platform_message_id == "mid-1"

    @pytest.mark.parametrize("make", [_fb, _ig])
    def test_dm_after_24h_uses_the_human_agent_tag(self, make):
        provider = make()
        provider._request.return_value = _resp({"message_id": "mid-1"})
        provider.reply_to_message(
            "tok", "m1", "hola", {"recipient_id": "psid-7", "last_inbound_at": _iso(timedelta(days=3))}
        )
        payload = provider._request.call_args.kwargs["json"]
        assert payload["messaging_type"] == "MESSAGE_TAG"
        assert payload["tag"] == "HUMAN_AGENT"

    @pytest.mark.parametrize("make", [_fb, _ig])
    def test_dm_after_7_days_is_refused_without_calling_meta(self, make):
        provider = make()
        with pytest.raises(APIError) as exc:
            provider.reply_to_message(
                "tok", "m1", "hola", {"recipient_id": "psid-7", "last_inbound_at": _iso(timedelta(days=8))}
            )
        assert exc.value.retryable is False
        provider._request.assert_not_called()

    def test_dm_without_participant_is_refused(self):
        with pytest.raises(APIError):
            send_dm(_fb(), GRAPH, "tok", "", "hola", None)


class TestInstagramInbox:
    MEDIA = {
        "data": [
            {
                "id": "media-5",
                "comments": {
                    "data": [
                        {
                            "id": "ic1",
                            "text": "linda foto",
                            "timestamp": "2026-10-19T10:00:00+0000",
                            "username": "carla",
                            "replies": {
                                "data": [
                                    {
                                        "id": "ic1r",
                                        "text": "gracias",
                                        "timestamp": "2026-10-19T10:30:00+0000",
                                        "username": "mimarca",
                                        "from": {"id": "ig-1"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        ]
    }

    def test_polls_media_comments_with_replies(self):
        provider = _ig([_resp(self.MEDIA), _resp({"data": []})])
        top, reply = provider.get_messages("tok")
        assert provider._request.call_args_list[0].args[:2] == ("GET", f"{GRAPH}/ig-1/media")
        assert top.extra["media_id"] == "media-5"
        assert top.extra["sender_handle"] == "carla"
        assert reply.extra["parent_id"] == "ic1"
        assert reply.extra["sender_handle"] == "ig-1"  # our own id when `from` is present

    def test_dms_use_conversations_with_platform_instagram(self):
        provider = _ig(
            [
                _resp({"data": []}),  # media
                _resp(
                    {
                        "data": [
                            {
                                "id": "t_9",
                                "messages": {
                                    "data": [
                                        {
                                            "id": "im1",
                                            "message": "hola",
                                            "created_time": "2026-10-19T10:00:00+0000",
                                            "from": {"id": "igsid-3", "username": "dani"},
                                            "to": {"data": [{"id": "ig-1"}]},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                _resp({"name": "Dani", "username": "dani", "profile_pic": "http://pic/dani"}),
            ]
        )
        (msg,) = provider.get_messages("tok")
        conv_call = provider._request.call_args_list[1]
        assert conv_call.args[:2] == ("GET", f"{GRAPH}/me/conversations")
        assert conv_call.kwargs["params"]["platform"] == "instagram"
        assert msg.extra["conversation_id"] == "t_9"
        assert msg.extra["recipient_id"] == "igsid-3"
        assert msg.extra["sender_avatar_url"] == "http://pic/dani"
        assert msg.sender_name == "dani"

    def test_scopes_cover_dms_and_deleting_posts(self):
        scopes = InstagramProvider({}).required_scopes
        assert {"instagram_manage_messages", "instagram_manage_contents", "pages_messaging"} <= set(scopes)


class TestModeration:
    def test_facebook_hide_and_unhide(self):
        provider = _fb()
        provider.set_comment_visibility("tok", "c1", visible=False)
        provider._request.assert_called_with("POST", f"{GRAPH}/c1", access_token="tok", json={"is_hidden": True})
        provider.set_comment_visibility("tok", "c1", visible=True)
        provider._request.assert_called_with("POST", f"{GRAPH}/c1", access_token="tok", json={"is_hidden": False})

    def test_instagram_hide(self):
        provider = _ig()
        provider.set_comment_visibility("tok", "ic1", visible=False)
        provider._request.assert_called_with("POST", f"{GRAPH}/ic1", access_token="tok", json={"hide": True})

    @pytest.mark.parametrize("make", [_fb, _ig])
    def test_delete_comment(self, make):
        provider = make()
        provider.delete_comment("tok", "c1")
        provider._request.assert_called_once_with("DELETE", f"{GRAPH}/c1", access_token="tok")


class TestDeletePost:
    def test_base_default_is_not_implemented(self):
        # Mastodon does not override delete_post, so it exercises the base default.
        from providers.mastodon import MastodonProvider

        with pytest.raises(NotImplementedError):
            MastodonProvider({}).delete_post("tok", "1")

    def test_facebook_deletes_the_page_scoped_id(self):
        provider = _fb()
        provider.delete_post("tok", "post-9")
        provider._request.assert_called_once_with("DELETE", f"{GRAPH}/page-1_post-9", access_token="tok")

    def test_facebook_falls_back_to_the_bare_id_for_videos(self):
        provider = _fb([APIError("not found"), _resp({"success": True})])
        provider.delete_post("tok", "video-3")
        assert provider._request.call_args_list[1].args == ("DELETE", f"{GRAPH}/video-3")

    def test_facebook_failure_raises(self):
        provider = _fb([APIError("a"), APIError("b")])
        with pytest.raises(APIError):
            provider.delete_post("tok", "post-9")

    def test_instagram_deletes_the_media(self):
        provider = _ig()
        provider.delete_post("tok", "media-5")
        provider._request.assert_called_once_with("DELETE", f"{GRAPH}/media-5", access_token="tok")

    def test_youtube_deletes_the_video(self):
        provider = YouTubeProvider({})
        provider._request = MagicMock()
        provider.delete_post("tok", "vid-1")
        provider._request.assert_called_once_with(
            "DELETE", "https://www.googleapis.com/youtube/v3/videos", access_token="tok", params={"id": "vid-1"}
        )


class TestSubscribeWebhooks:
    def test_facebook_subscribes_feed_and_messages(self):
        provider = _fb()
        provider.subscribe_webhooks("page-token", "page-1")
        args, kwargs = provider._request.call_args
        assert args == ("POST", f"{GRAPH}/page-1/subscribed_apps")
        assert kwargs["access_token"] == "page-token"
        assert "feed" in kwargs["params"]["subscribed_fields"]
        assert "messages" in kwargs["params"]["subscribed_fields"]

    def test_instagram_subscribes_the_linked_page(self):
        provider = _ig()
        provider.subscribe_webhooks("page-token", "fb-page-9")
        args, kwargs = provider._request.call_args
        assert args == ("POST", f"{GRAPH}/fb-page-9/subscribed_apps")
        assert kwargs["params"]["subscribed_fields"] == "messages,comments,mentions"
