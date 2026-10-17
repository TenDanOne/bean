"""The inbox used to answer itself.

Every reply sent from Brightbean came back through sync as a comment by the
connected account and was filed as a fresh inbound message — half a real
workspace's inbox turned out to be its own voice. The same reply also existed
twice, as an `InboxReply` and as an `InboxMessage`, with nothing linking them,
and replies rendered detached from the comment they answered.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.tasks import InboxSyncEngine
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

CHANNEL_ID = "UCq0GhY5GZFYPD67fiyT1whw"


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Thread WS", organization=organization)


@pytest.fixture
def operator(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


@pytest.fixture
def account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_platform_id=CHANNEL_ID,
        account_name="Premed UNX",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _msg(message_id, *, sender=CHANNEL_ID, parent=None, text="hi", minutes_ago=0):
    extra = {"sender_handle": sender}
    if parent:
        extra["parent_id"] = parent
    return SimpleNamespace(
        platform_message_id=message_id,
        sender_name="Someone",
        sender_id=sender,
        text=text,
        message_type="comment",
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        extra=extra,
    )


@pytest.mark.django_db
class TestOutboundDetection:
    def test_own_comment_is_marked_outbound(self, account):
        InboxSyncEngine()._upsert_message(account, _msg("c1", sender=CHANNEL_ID))

        assert InboxMessage.objects.get(platform_message_id="c1").is_outbound is True

    def test_third_party_comment_is_inbound(self, account):
        InboxSyncEngine()._upsert_message(account, _msg("c1", sender="UC-someone-else"))

        assert InboxMessage.objects.get(platform_message_id="c1").is_outbound is False

    def test_outbound_messages_never_notify(self, account):
        from unittest.mock import patch

        with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
            InboxSyncEngine()._upsert_message(account, _msg("c1", sender=CHANNEL_ID), notify=True)
        notify.assert_not_called()

    def test_account_without_platform_id_marks_nothing_outbound(self, workspace):
        """A blank account id must not make every blank sender_handle "ours"."""
        account = SocialAccount.objects.create(
            workspace=workspace, platform="youtube", account_platform_id="", account_name="No id"
        )
        InboxSyncEngine()._upsert_message(account, _msg("c1", sender=""))

        assert InboxMessage.objects.get(platform_message_id="c1").is_outbound is False


@pytest.mark.django_db
class TestThreading:
    def test_reply_links_to_its_parent(self, account):
        engine = InboxSyncEngine()
        engine._upsert_message(account, _msg("root", sender="UC-viewer", minutes_ago=10))
        engine._upsert_message(account, _msg("child", sender=CHANNEL_ID, parent="root"))

        root = InboxMessage.objects.get(platform_message_id="root")
        child = InboxMessage.objects.get(platform_message_id="child")
        assert child.parent_message == root

    def test_reply_with_missing_parent_stays_top_level(self, account):
        """A `since` window can start mid-thread; drop the link, not the message."""
        InboxSyncEngine()._upsert_message(account, _msg("child", sender="UC-viewer", parent="never-synced"))

        child = InboxMessage.objects.get(platform_message_id="child")
        assert child.parent_message is None

    def test_parent_lookup_is_scoped_to_the_account(self, account, workspace):
        other = SocialAccount.objects.create(
            workspace=workspace, platform="youtube", account_platform_id="UC-other", account_name="Other"
        )
        engine = InboxSyncEngine()
        engine._upsert_message(other, _msg("root", sender="UC-viewer"))
        engine._upsert_message(account, _msg("child", sender="UC-viewer", parent="root"))

        assert InboxMessage.objects.get(platform_message_id="child").parent_message is None


@pytest.mark.django_db
class TestReplyReconciliation:
    def _seed_root(self, account):
        InboxSyncEngine()._upsert_message(account, _msg("root", sender="UC-viewer", minutes_ago=10))
        return InboxMessage.objects.get(platform_message_id="root")

    def test_api_copy_absorbs_the_local_reply(self, account, operator):
        root = self._seed_root(account)
        InboxReply.objects.create(inbox_message=root, author=operator, body="gracias", platform_reply_id="reply-1")

        InboxSyncEngine()._upsert_message(account, _msg("reply-1", sender=CHANNEL_ID, parent="root"))

        message = InboxMessage.objects.get(platform_message_id="reply-1")
        assert message.sent_by == operator
        # The duplicate representation is gone: one reply, one row.
        assert not InboxReply.objects.filter(platform_reply_id="reply-1").exists()

    def test_reply_written_outside_brightbean_has_no_author(self, account):
        self._seed_root(account)

        InboxSyncEngine()._upsert_message(account, _msg("reply-2", sender=CHANNEL_ID, parent="root"))

        assert InboxMessage.objects.get(platform_message_id="reply-2").sent_by is None

    def test_unconfirmed_reply_survives_until_its_id_arrives(self, account, operator):
        """A send is recorded immediately; only the matching id reconciles it."""
        root = self._seed_root(account)
        InboxReply.objects.create(inbox_message=root, author=operator, body="gracias", platform_reply_id="reply-1")

        InboxSyncEngine()._upsert_message(account, _msg("unrelated", sender=CHANNEL_ID, parent="root"))

        assert InboxReply.objects.filter(platform_reply_id="reply-1").exists()


@pytest.mark.django_db
class TestFeedShowsOnlyActionable:
    def test_outbound_and_replies_are_excluded(self, client, operator, workspace, account):
        engine = InboxSyncEngine()
        engine._upsert_message(account, _msg("root", sender="UC-viewer", minutes_ago=10))
        engine._upsert_message(account, _msg("ours", sender=CHANNEL_ID, parent="root"))
        engine._upsert_message(account, _msg("their-reply", sender="UC-viewer", parent="root"))
        engine._upsert_message(account, _msg("standalone-ours", sender=CHANNEL_ID, minutes_ago=5))

        client.force_login(operator)
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": workspace.id}))

        # A plain browse lists publications now, so the assertion moves down a
        # level. What it checks is unchanged: our own words and replies stay out
        # of the list of things needing an answer.
        listed = [m.platform_message_id for folder in response.context["folders"] for m in folder["messages"]]
        assert listed == ["root"]

    def test_thread_carries_the_children(self, client, operator, workspace, account):
        engine = InboxSyncEngine()
        engine._upsert_message(account, _msg("root", sender="UC-viewer", minutes_ago=10))
        engine._upsert_message(account, _msg("ours", sender=CHANNEL_ID, parent="root", text="gracias"))
        root = InboxMessage.objects.get(platform_message_id="root")

        client.force_login(operator)
        response = client.get(
            reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": root.id})
        )

        kinds = [turn["kind"] for turn in response.context["thread"]]
        assert "message" in kinds
