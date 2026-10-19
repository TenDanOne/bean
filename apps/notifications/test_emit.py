"""POST_PUBLISHED / POST_FAILED / SOCIAL_ACCOUNT_DISCONNECTED se emiten; el digest está registrado."""

from unittest.mock import MagicMock, patch

import pytest

from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.notifications.models import Channel, EventType, Notification, QuietHours
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def ws(db, organization):
    return Workspace.objects.create(name="Notif WS", organization=organization)


@pytest.fixture
def account(ws):
    return SocialAccount.objects.create(
        workspace=ws,
        platform="youtube",
        account_platform_id="UC-n",
        account_name="Canal N",
        oauth_access_token="t",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def pp(ws, user, account):
    post = Post.objects.create(workspace=ws, author=user, title="Mi video", caption="c")
    return PlatformPost.objects.create(post=post, social_account=account, status="publishing")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,event",
    [("published", EventType.POST_PUBLISHED), ("failed", EventType.POST_FAILED)],
)
def test_publish_outcome_notifies_author_once(pp, user, status, event):
    pp.status = status
    pp.save()
    pp.save()  # un segundo guardado no duplica

    assert Notification.objects.filter(user=user, event_type=event).count() == 1


@pytest.mark.django_db
def test_health_check_error_notifies_workspace_owner(ws, user, account):
    from apps.social_accounts.tasks import check_social_account_health

    WorkspaceMembership.objects.create(user=user, workspace=ws, workspace_role="owner")
    provider = MagicMock()
    provider.get_profile.side_effect = RuntimeError("401")
    with (
        patch("providers.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        check_social_account_health.now(str(account.id))

    assert Notification.objects.filter(user=user, event_type=EventType.SOCIAL_ACCOUNT_DISCONNECTED).count() == 1


@pytest.mark.django_db
def test_daily_digest_is_registered_as_recurring_task():
    from background_task.models import Task

    from apps.notifications.apps import NotificationsConfig

    NotificationsConfig._register_tasks(None)
    assert Task.objects.filter(verbose_name="run_daily_digests").exists()


@pytest.mark.django_db
def test_digest_mode_suppresses_immediate_email(user):
    from apps.notifications.engine import notify

    QuietHours.objects.create(user=user, digest_mode=True)
    n = notify(user=user, event_type=EventType.POST_FAILED, title="x")

    assert not n.deliveries.filter(channel=Channel.EMAIL).exists()
