"""awaiting_reply, the sidebar badge, DM reply context and the Meta webhook events of the new inbox."""

import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.common.context_processors import sidebar_context
from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.tasks import InboxSyncEngine
from apps.members.models import BUILTIN_ROLE_PERMISSIONS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Await WS", organization=organization)


@pytest.fixture
def operator(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


@pytest.fixture
def fb(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="page-1",
        account_name="Mi Pagina",
        oauth_access_token="tok",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def ig(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_handle="mimarca",
        account_name="Mi marca",
        oauth_access_token="tok",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _msg(mid, *, sender="u1", minutes_ago=10, kind="comment", extra=None, text="hola"):
    return SimpleNamespace(
        platform_message_id=mid,
        sender_id=sender,
        sender_name=sender,
        text=text,
        message_type=kind,
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        extra=extra or {},
    )


def _upsert(account, msg):
    InboxSyncEngine()._upsert_message(account, msg, notify=False)
    return InboxMessage.objects.get(social_account=account, platform_message_id=msg.platform_message_id)


@pytest.mark.django_db
class TestAwaitingReplyOnSync:
    def test_inbound_comment_awaits_a_reply_and_is_unread(self, fb):
        row = _upsert(fb, _msg("c1"))
        assert row.awaiting_reply is True
        assert row.status == InboxMessage.Status.UNREAD

    def test_outbound_row_is_never_unread_nor_awaiting(self, fb):
        row = _upsert(fb, _msg("c2", sender="page-1"))
        assert row.is_outbound is True
        assert row.status != InboxMessage.Status.UNREAD
        assert row.awaiting_reply is False

    def test_thread_replies_are_not_created_unread(self, fb):
        parent = _upsert(fb, _msg("c1", minutes_ago=30))
        child = _upsert(fb, _msg("c1r", minutes_ago=20, extra={"parent_id": "c1"}))
        assert child.parent_message_id == parent.id
        assert child.status == InboxMessage.Status.OPEN

    def test_an_outbound_reply_in_sync_clears_the_comment(self, fb):
        parent = _upsert(fb, _msg("c1", minutes_ago=30))
        _upsert(fb, _msg("c1r", sender="page-1", minutes_ago=20, extra={"parent_id": "c1"}))
        parent.refresh_from_db()
        assert parent.awaiting_reply is False

    def test_a_later_inbound_message_in_the_thread_still_waits(self, fb):
        parent = _upsert(fb, _msg("c1", minutes_ago=30))
        _upsert(fb, _msg("c1r", sender="page-1", minutes_ago=20, extra={"parent_id": "c1"}))
        later = _upsert(fb, _msg("c1x", minutes_ago=5, extra={"parent_id": "c1"}))
        parent.refresh_from_db()
        assert parent.awaiting_reply is False
        assert later.awaiting_reply is True

    def test_dm_conversation_is_cleared_by_our_message(self, fb):
        inbound = _upsert(fb, _msg("m1", kind="dm", minutes_ago=30, extra={"conversation_id": "t_1"}))
        assert inbound.conversation_id == "t_1"
        assert inbound.awaiting_reply is True
        _upsert(fb, _msg("m2", kind="dm", sender="page-1", minutes_ago=5, extra={"conversation_id": "t_1"}))
        inbound.refresh_from_db()
        assert inbound.awaiting_reply is False

    def test_instagram_own_comment_is_recognised_by_handle(self, ig):
        row = _upsert(ig, _msg("ic1", sender="MiMarca"))
        assert row.is_outbound is True

    def test_youtube_video_announcements_do_not_wait_for_a_reply(self, workspace):
        yt = SocialAccount.objects.create(
            workspace=workspace, platform="youtube", account_platform_id="UC1", account_name="Canal"
        )
        row = _upsert(yt, _msg("yt:video:abc", kind="mention"))
        assert row.awaiting_reply is False


@pytest.mark.django_db
class TestSidebarBadge:
    def _count(self, user, workspace):
        request = RequestFactory().get("/")
        request.user = user
        request.workspace = workspace
        request.workspace_membership = WorkspaceMembership.objects.get(user=user, workspace=workspace)
        request.org_membership = None
        return sidebar_context(request)["sidebar_unread_inbox_count"]

    def test_counts_only_pending_top_level_inbound(self, operator, workspace, fb):
        _upsert(fb, _msg("c1", minutes_ago=30))
        _upsert(fb, _msg("c2", minutes_ago=29))
        _upsert(fb, _msg("c2r", sender="page-1", minutes_ago=20, extra={"parent_id": "c2"}))  # answers c2
        _upsert(fb, _msg("c3", minutes_ago=25, extra={"parent_id": "c1"}))  # a child, not counted
        assert self._count(operator, workspace) == 1

    def test_resolved_messages_are_not_counted(self, operator, workspace, fb):
        row = _upsert(fb, _msg("c1"))
        InboxMessage.objects.filter(id=row.id).update(status=InboxMessage.Status.RESOLVED)
        assert self._count(operator, workspace) == 0

    def test_channels_the_member_cannot_see_are_not_counted(self, user, organization, workspace, fb):
        OrgMembership.objects.create(user=user, organization=organization, org_role="member")
        WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="editor")
        _upsert(fb, _msg("c1"))
        assert self._count(user, workspace) == 0  # an editor sees only assigned channels, none assigned


@pytest.mark.django_db
class TestSendReply:
    def _url(self, workspace, message):
        return reverse("inbox:send_reply", kwargs={"workspace_id": workspace.id, "message_id": message.id})

    def test_reply_clears_awaiting_reply_for_the_comment_thread(self, client, operator, workspace, fb):
        row = _upsert(fb, _msg("c1"))
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.return_value.platform_message_id = "r1"
            response = client.post(self._url(workspace, row), {"body": "gracias"})
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.awaiting_reply is False
        assert InboxReply.objects.filter(inbox_message=row).exists()

    def test_failed_reply_keeps_it_awaiting(self, client, operator, workspace, fb):
        row = _upsert(fb, _msg("c1"))
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = RuntimeError("nope")
            client.post(self._url(workspace, row), {"body": "gracias"})
        row.refresh_from_db()
        assert row.awaiting_reply is True

    def test_dm_reply_passes_recipient_and_last_inbound(self, client, operator, workspace, fb):
        _upsert(fb, _msg("m1", kind="dm", sender="psid-7", minutes_ago=90, extra={"conversation_id": "t_1"}))
        latest = _upsert(fb, _msg("m2", kind="dm", sender="psid-7", minutes_ago=30, extra={"conversation_id": "t_1"}))
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.return_value.platform_message_id = "r1"
            client.post(self._url(workspace, latest), {"body": "hola"})
        extra = get_provider.return_value.reply_to_message.call_args.kwargs["extra"]
        assert extra["recipient_id"] == "psid-7"  # derived for rows stored before recipient_id existed
        assert extra["last_inbound_at"] == latest.received_at.isoformat()
        assert InboxMessage.objects.filter(conversation_id="t_1", awaiting_reply=True).count() == 0

    def test_dm_without_recipient_is_refused_before_calling_the_provider(self, client, operator, workspace, fb):
        row = _upsert(
            fb, _msg("m1", kind="dm", sender="page-1", extra={"conversation_id": "t_1"})
        )  # outbound: no partner known
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            response = client.post(self._url(workspace, row), {"body": "hola"})
        assert response.status_code == 422
        assert "No se pudo identificar a quién responder" in response.content.decode()
        get_provider.return_value.reply_to_message.assert_not_called()

    def test_refused_window_shows_the_reason(self, client, operator, workspace, fb):
        from providers.exceptions import APIError

        row = _upsert(fb, _msg("m1", kind="dm", sender="psid-7", extra={"conversation_id": "t_1"}))
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = APIError("Pasaron más de 7 días.", retryable=False)
            response = client.post(self._url(workspace, row), {"body": "hola"})
        assert response.status_code == 422
        assert "7 días" in response.content.decode()


@pytest.mark.django_db
class TestModerationOnMeta:
    @pytest.mark.parametrize("account_fixture", ["fb", "ig"])
    def test_hide_and_delete_work_for_meta_comments(self, request, client, operator, workspace, account_fixture):
        account = request.getfixturevalue(account_fixture)
        row = _upsert(account, _msg("c1"))
        client.force_login(operator)
        kwargs = {"workspace_id": workspace.id, "message_id": row.id}
        with patch("apps.inbox.views.get_provider") as get_provider:
            client.post(reverse("inbox:toggle_hidden", kwargs=kwargs))
            row.refresh_from_db()
            assert row.is_hidden is True
            get_provider.return_value.set_comment_visibility.assert_called_once()
            client.post(reverse("inbox:delete_comment", kwargs=kwargs))
        row.refresh_from_db()
        assert row.deleted_on_platform is not None
        get_provider.return_value.delete_comment.assert_called_once()


def _post_webhook(client, payload, secret="meta-secret", url_name="webhook_facebook"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        reverse(f"inbox_webhooks:{url_name}"),
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
    )


@pytest.mark.django_db
class TestMetaWebhooks:
    @pytest.fixture(autouse=True)
    def _app_secret(self, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {p: {"app_secret": "meta-secret"} for p in ("facebook", "instagram")}

    def _feed(self, **value):
        return {"entry": [{"id": "page-1", "time": 1760868000, "changes": [{"field": "feed", "value": value}]}]}

    def test_facebook_comment_add_is_stored_with_platform_time_and_thread(self, client, fb):
        response = _post_webhook(
            client,
            self._feed(
                item="comment",
                verb="add",
                comment_id="c9",
                post_id="page-1_post-4",
                parent_id="c5",
                created_time=1760868123,
                message="hola!",
                **{"from": {"id": "u1", "name": "Ana"}},
            ),
        )
        assert response.status_code == 200
        row = InboxMessage.objects.get(platform_message_id="c9")
        assert row.received_at.timestamp() == 1760868123
        assert row.extra["post_id"] == "post-4"
        assert row.extra["parent_id"] == "c5"
        assert row.awaiting_reply is True

    def test_top_level_comment_has_no_parent_when_meta_reports_the_post(self, client, fb):
        _post_webhook(
            client,
            self._feed(
                item="comment",
                verb="add",
                comment_id="c10",
                post_id="page-1_post-4",
                parent_id="page-1_post-4",
                message="x",
                **{"from": {"id": "u1", "name": "Ana"}},
            ),
        )
        assert "parent_id" not in InboxMessage.objects.get(platform_message_id="c10").extra

    @pytest.mark.parametrize(
        "value",
        [
            {"item": "reaction", "verb": "add", "comment_id": "r1"},
            {"item": "comment", "verb": "edited", "comment_id": "r2"},
            {"item": "comment", "verb": "remove", "comment_id": "r3"},
            {"comment_id": "r4"},
        ],
    )
    def test_other_feed_events_are_ignored(self, client, fb, value):
        _post_webhook(client, self._feed(**value))
        assert InboxMessage.objects.count() == 0

    def test_page_own_comment_is_outbound_and_answers_the_thread(self, client, fb):
        parent = _upsert(fb, _msg("c5", minutes_ago=60))
        _post_webhook(
            client,
            self._feed(
                item="comment",
                verb="add",
                comment_id="c11",
                post_id="page-1_post-4",
                parent_id="c5",
                message="gracias",
                **{"from": {"id": "page-1", "name": "Mi Pagina"}},
            ),
        )
        row = InboxMessage.objects.get(platform_message_id="c11")
        assert row.is_outbound is True
        assert row.parent_message_id == parent.id
        parent.refresh_from_db()
        assert parent.awaiting_reply is False

    def test_instagram_comment_and_mention_fields(self, client, ig):
        payload = {
            "entry": [
                {
                    "id": "ig-1",
                    "time": 1760868000,
                    "changes": [
                        {
                            "field": "comments",
                            "value": {
                                "id": "ic7",
                                "text": "linda",
                                "from": {"id": "igsid-1", "username": "carla"},
                                "media": {"id": "media-3", "media_product_type": "FEED"},
                            },
                        },
                        {"field": "mentions", "value": {"media_id": "media-8", "comment_id": "ic8"}},
                    ],
                }
            ]
        }
        assert _post_webhook(client, payload).status_code == 200
        comment = InboxMessage.objects.get(platform_message_id="ic7")
        assert comment.message_type == "comment"
        assert comment.extra["media_id"] == "media-3"
        assert comment.sender_name == "carla"
        assert comment.received_at.timestamp() == 1760868000
        mention = InboxMessage.objects.get(platform_message_id="ic8")
        assert mention.message_type == "mention"

    def test_dm_gets_a_conversation_and_recipient_and_echo_answers_it(self, client, fb):
        inbound = {
            "entry": [
                {
                    "id": "page-1",
                    "messaging": [
                        {
                            "sender": {"id": "psid-7"},
                            "recipient": {"id": "page-1"},
                            "timestamp": 1760868000000,
                            "message": {"mid": "m1", "text": "hola"},
                        }
                    ],
                }
            ]
        }
        _post_webhook(client, inbound)
        row = InboxMessage.objects.get(platform_message_id="m1")
        assert row.extra["recipient_id"] == "psid-7"
        assert row.conversation_id == "user:psid-7"
        assert row.awaiting_reply is True

        echo = {
            "entry": [
                {
                    "id": "page-1",
                    "messaging": [
                        {
                            "sender": {"id": "page-1"},
                            "recipient": {"id": "psid-7"},
                            "timestamp": 1760868500000,
                            "message": {"mid": "m2", "text": "te ayudo", "is_echo": True},
                        }
                    ],
                }
            ]
        }
        _post_webhook(client, echo)
        echo_row = InboxMessage.objects.get(platform_message_id="m2")
        assert echo_row.is_outbound is True
        assert echo_row.conversation_id == "user:psid-7"
        row.refresh_from_db()
        assert row.awaiting_reply is False

    def test_dm_reuses_the_conversation_id_found_by_polling(self, client, fb):
        _upsert(fb, _msg("m0", kind="dm", sender="psid-7", extra={"conversation_id": "t_5", "recipient_id": "psid-7"}))
        _post_webhook(
            client,
            {
                "entry": [
                    {
                        "id": "page-1",
                        "messaging": [
                            {"sender": {"id": "psid-7"}, "message": {"mid": "m1", "text": "otra vez"}},
                        ],
                    }
                ]
            },
        )
        assert InboxMessage.objects.get(platform_message_id="m1").conversation_id == "t_5"

    def test_polling_unifies_user_rows_into_the_real_conversation(self, client, fb):
        _upsert(
            fb,
            _msg("m0", kind="dm", sender="psid-7", extra={"conversation_id": "user:psid-7", "recipient_id": "psid-7"}),
        )
        _upsert(fb, _msg("m1", kind="dm", sender="psid-7", extra={"conversation_id": "t_5", "recipient_id": "psid-7"}))
        assert InboxMessage.objects.get(platform_message_id="m0").conversation_id == "t_5"

    def test_dm_without_partner_keeps_an_empty_conversation_id(self, client, fb):
        _post_webhook(
            client,
            {
                "entry": [
                    {"id": "page-1", "messaging": [{"sender": {"id": "page-1"}, "message": {"mid": "m9", "text": "x"}}]}
                ]
            },
        )
        assert InboxMessage.objects.get(platform_message_id="m9").conversation_id == ""

    def test_duplicate_delivery_does_not_duplicate_or_reset(self, client, fb):
        payload = self._feed(
            item="comment",
            verb="add",
            comment_id="c9",
            post_id="p_1",
            message="x",
            **{"from": {"id": "u", "name": "U"}},
        )
        _post_webhook(client, payload)
        InboxMessage.objects.filter(platform_message_id="c9").update(awaiting_reply=False)
        _post_webhook(client, payload)
        assert InboxMessage.objects.filter(platform_message_id="c9").count() == 1
        assert InboxMessage.objects.get(platform_message_id="c9").awaiting_reply is False


@pytest.mark.django_db
def test_backfill_survives_a_null_conversation_id_in_extra(fb):
    import importlib

    from django.apps import apps as django_apps

    backfill = importlib.import_module("apps.inbox.migrations.0008_backfill_conversation_awaiting_reply").backfill
    kwargs = {"social_account": fb, "workspace": fb.workspace, "received_at": timezone.now(), "message_type": "dm"}
    nulled = InboxMessage.objects.create(platform_message_id="n1", extra={"conversation_id": None}, **kwargs)
    filled = InboxMessage.objects.create(platform_message_id="n2", extra={"conversation_id": "t_9"}, **kwargs)
    backfill(django_apps, None)
    assert InboxMessage.objects.get(id=nulled.id).conversation_id == ""
    assert InboxMessage.objects.get(id=filled.id).conversation_id == "t_9"


class TestDeletePublishedPostsPermission:
    def test_only_the_owner_holds_it_by_default(self):
        assert BUILTIN_ROLE_PERMISSIONS["owner"]["delete_published_posts"] is True
        for role, permissions in BUILTIN_ROLE_PERMISSIONS.items():
            if role != "owner":
                assert permissions["delete_published_posts"] is False, role
