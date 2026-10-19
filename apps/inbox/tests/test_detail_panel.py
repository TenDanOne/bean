"""The detail panel: publication card above the thread, and a thread that survives deleted authors."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage, InboxReply, InternalNote
from apps.inbox.views import _detail_context
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand P")


@pytest.fixture
def channel(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_handle="chan",
        account_name="Chan",
        account_platform_id="chan",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def operator(db, django_user_model, workspace):
    user = django_user_model.objects.create_user(
        email="op@example.com", password="pw12345!", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


def make_message(workspace, channel, message_id="m1", message_type="comment", extra=None, **kwargs):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=channel,
        platform_message_id=message_id,
        message_type=message_type,
        sender_name="Someone",
        body="hola",
        extra=extra or {},
        received_at=timezone.now(),
        **kwargs,
    )


@pytest.mark.django_db
class TestPostCard:
    def test_card_from_extra(self, workspace, channel):
        msg = make_message(
            workspace,
            channel,
            extra={"video_id": "abc123", "post_title": "Mi video", "post_published_at": "2026-01-05T10:00:00Z"},
        )
        card = _detail_context(workspace, msg)["post_card"]
        assert card["title"] == "Mi video"
        assert card["url"] == "https://www.youtube.com/watch?v=abc123"
        assert card["published_at"].year == 2026

    def test_card_from_related_post(self, workspace, channel, operator):
        post = Post.objects.create(workspace=workspace, author=operator, caption="Sopa del día")
        pp = PlatformPost.objects.create(
            post=post, social_account=channel, platform_post_id="abc123", published_at=timezone.now()
        )
        msg = make_message(workspace, channel, extra={"video_id": "abc123"}, related_post=pp)
        card = _detail_context(workspace, msg)["post_card"]
        assert card["title"] == "Sopa del día"
        assert card["published_at"] == pp.published_at

    def test_dm_has_no_card(self, workspace, channel):
        msg = make_message(workspace, channel, message_type="dm", extra={"video_id": "abc123"})
        assert _detail_context(workspace, msg)["post_card"] is None

    def test_no_post_data_has_no_card(self, workspace, channel):
        assert _detail_context(workspace, make_message(workspace, channel))["post_card"] is None

    def test_panel_renders_card_and_escapes_title(self, client, workspace, channel, operator):
        msg = make_message(workspace, channel, extra={"video_id": "abc123", "post_title": "<b>x</b>"})
        client.force_login(operator)
        url = reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": msg.id})
        html = client.get(url).content.decode()
        assert 'data-testid="post-card"' in html
        assert "&lt;b&gt;x&lt;/b&gt;" in html
        assert 'rel="noopener noreferrer"' in html


@pytest.mark.django_db
class TestDeletedAuthor:
    def test_thread_survives_null_author(self, workspace, channel, operator):
        msg = make_message(workspace, channel)
        InboxReply.objects.create(inbox_message=msg, author=None, body="r")
        InternalNote.objects.create(inbox_message=msg, author=None, body="n")
        names = {t["name"] for t in _detail_context(workspace, msg)["thread"]}
        assert names == {"Usuario eliminado"}
