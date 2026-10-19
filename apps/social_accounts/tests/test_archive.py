"""Removing a channel used to destroy its work without asking.

The old `disconnect` deleted the account row outright, which took every post
targeting only that channel with it and — through the cascade on
`SocialAccount` — the channel's entire inbox history. No confirmation, no
indication of how much was lost, and a popover that claimed "historical data
will be preserved".

Retiring a channel is routine; destroying its record is not. These are now two
actions, and the safe one is the default.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Archive WS", organization=organization)


@pytest.fixture
def owner(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


@pytest.fixture
def account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_platform_id="UC-archive",
        account_name="Premed UNX",
        oauth_access_token="token",
        oauth_refresh_token="refresh",
        token_expires_at=timezone.now(),
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def content(db, workspace, account, owner):
    """A post targeting only this channel, plus a comment on it."""
    post = Post.objects.create(workspace=workspace, author=owner, title="Solo suyo", caption="c")
    PlatformPost.objects.create(post=post, social_account=account, status="scheduled")
    message = InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="c1",
        sender_name="Viewer",
        body="great",
        message_type="comment",
        received_at=timezone.now(),
    )
    return post, message


def _archive_url(workspace, account):
    return reverse("social_accounts:disconnect", kwargs={"workspace_id": workspace.id, "account_id": account.id})


def _delete_url(workspace, account):
    return reverse("social_accounts:delete_account", kwargs={"workspace_id": workspace.id, "account_id": account.id})


@pytest.mark.django_db
class TestArchive:
    def test_archiving_keeps_the_channel_and_its_work(self, client, owner, workspace, account, content):
        post, message = content
        client.force_login(owner)

        client.post(_archive_url(workspace, account))

        account.refresh_from_db()
        assert account.connection_status == SocialAccount.ConnectionStatus.ARCHIVED
        assert account.is_archived
        # The whole point: nothing was destroyed.
        assert Post.objects.filter(id=post.id).exists()
        assert InboxMessage.objects.filter(id=message.id).exists()

    def test_archiving_clears_the_credentials(self, client, owner, workspace, account):
        """A retired channel has no business holding a live token."""
        client.force_login(owner)

        client.post(_archive_url(workspace, account))

        account.refresh_from_db()
        assert account.oauth_access_token == ""
        assert account.oauth_refresh_token == ""
        assert account.token_expires_at is None

    def test_reconnecting_revives_an_archived_channel(self, account):
        account.archive()

        # The OAuth callback path sets the status back on update_or_create.
        account.connection_status = SocialAccount.ConnectionStatus.CONNECTED
        account.save(update_fields=["connection_status"])

        account.refresh_from_db()
        assert not account.is_archived


@pytest.mark.django_db
class TestArchivedChannelsAreInert:
    def test_archived_accounts_are_excluded_from_inbox_sync(self, account):
        from unittest.mock import patch

        from apps.inbox.tasks import InboxSyncEngine

        account.archive()

        with patch("apps.inbox.tasks.get_provider") as get_provider:
            InboxSyncEngine().sync_all()

        get_provider.assert_not_called()

    def test_archived_accounts_do_not_publish_scheduled_posts(self, workspace, account, content):
        """Every other surface filters on connection_status, but the publisher
        selects by post status — so this needed its own exclusion."""
        from apps.publisher.engine import PublishEngine

        post, _ = content
        pp = PlatformPost.objects.get(post=post)
        pp.scheduled_at = timezone.now() - timezone.timedelta(minutes=5)
        pp.save(update_fields=["scheduled_at"])

        assert PublishEngine()._get_due_platform_posts()  # due before archiving

        account.archive()

        assert PublishEngine()._get_due_platform_posts() == []


@pytest.mark.django_db
class TestPermanentDelete:
    def test_confirmation_reports_what_will_be_destroyed(self, client, owner, workspace, account, content):
        client.force_login(owner)

        response = client.get(_delete_url(workspace, account))

        assert response.status_code == 200
        assert response.context["impact"] == {"posts": 1, "messages": 1}
        # The confirm replaces the card, so it must keep the id its own form targets.
        assert f'id="account-{account.id}"' in response.content.decode()

    def test_wrong_name_deletes_nothing(self, client, owner, workspace, account, content):
        client.force_login(owner)

        response = client.post(_delete_url(workspace, account), {"confirm_name": "not the name"})

        assert response.status_code == 200
        assert "no coincide" in response.content.decode()
        assert SocialAccount.objects.filter(id=account.id).exists()
        assert Post.objects.count() == 1

    def test_matching_name_destroys_the_channel_and_its_work(self, client, owner, workspace, account, content):
        client.force_login(owner)

        client.post(_delete_url(workspace, account), {"confirm_name": "Premed UNX"})

        assert not SocialAccount.objects.filter(id=account.id).exists()
        assert Post.objects.count() == 0
        assert InboxMessage.objects.count() == 0

    def test_posts_shared_with_another_channel_survive(self, client, owner, workspace, account, content):
        """Only posts left targeting nothing are removed."""
        post, _ = content
        other = SocialAccount.objects.create(
            workspace=workspace, platform="tiktok", account_platform_id="tt-1", account_name="Other"
        )
        PlatformPost.objects.create(post=post, social_account=other, status="draft")
        client.force_login(owner)

        client.post(_delete_url(workspace, account), {"confirm_name": "Premed UNX"})

        assert Post.objects.filter(id=post.id).exists()


@pytest.mark.django_db
class TestArchivedListing:
    def test_archived_shows_state_reconnect_and_is_not_counted(self, client, owner, workspace, account):
        account.archive()
        client.force_login(owner)

        resp = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))

        html = resp.content.decode()
        assert "Archivada" in html
        assert "Reconectar" in html
        assert "0 cuentas conectadas y 1 archivada" in html

    def test_htmx_archive_returns_the_card_with_archived_state(self, client, owner, workspace, account):
        client.force_login(owner)

        resp = client.post(_archive_url(workspace, account), HTTP_HX_REQUEST="true")

        assert "Archivada" in resp.content.decode()

    def test_delete_confirm_has_cancel_and_spanish_conjunction(self, client, owner, workspace, account):
        client.force_login(owner)

        html = client.get(_delete_url(workspace, account)).content.decode()

        assert "Cancelar" in html
        assert "</strong> and" not in html
