"""The inbox sync engine suppresses first-sync history but never the first real message."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.inbox.tasks import InboxSyncEngine
from apps.settings_manager.defaults import APP_DEFAULTS
from apps.settings_manager.models import WorkspaceSetting
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Sync WS", organization=organization)


@pytest.fixture
def connected_account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-sync-1",
        account_name="Sync Test",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _msg(message_id, *, minutes_ago=0, text="hello"):
    """A minimal stand-in for the message objects providers return."""
    return SimpleNamespace(
        platform_message_id=message_id,
        sender_name="Sender",
        sender_id="sender-1",
        text=text,
        message_type=InboxMessage.MessageType.DM,
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        extra={},
    )


@pytest.mark.django_db
def test_first_sync_suppresses_old_backlog_then_notifies_new(connected_account):
    with patch("apps.inbox.tasks.get_provider") as get_provider:
        provider = get_provider.return_value

        # First-ever sync pulls an OLD historical backlog -> seed it silently.
        provider.get_messages.return_value = [_msg("h1", minutes_ago=1440), _msg("h2", minutes_ago=2880)]
        with patch.object(InboxSyncEngine, "_notify_new_message") as notify_new:
            InboxSyncEngine().sync_all()
        assert InboxMessage.objects.filter(social_account=connected_account).count() == 2
        notify_new.assert_not_called()

        # Once the account has history, a genuinely new message notifies.
        provider.get_messages.return_value = [_msg("n1", minutes_ago=0)]
        with patch.object(InboxSyncEngine, "_notify_new_message") as notify_new:
            InboxSyncEngine().sync_all()
        notify_new.assert_called_once()


@pytest.mark.django_db
def test_first_message_on_quiet_account_still_notifies(connected_account):
    # Regression: a long-quiet account has no prior messages, so last_msg is None
    # and it's still the "first sync" — but its first genuinely-recent message must
    # alert, not be silently swallowed as if it were backlog.
    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = [_msg("first", minutes_ago=0)]
        with patch.object(InboxSyncEngine, "_notify_new_message") as notify_new:
            InboxSyncEngine().sync_all()
        notify_new.assert_called_once()


@pytest.mark.django_db
def test_mastodon_sync_passes_per_account_instance_url(workspace):
    # Regression: sync used to call get_provider(platform) with no credentials, so the
    # federated MastodonProvider got instance_url="" and built scheme-less URLs like
    # "/api/v1/notifications" -> httpx.UnsupportedProtocol. The account's instance_url
    # must reach the provider. is_safe_url is patched so the SSRF check stays hermetic
    # (it does a real DNS lookup otherwise).
    from apps.social_accounts.models import MastodonAppRegistration

    # Persisted so sync_all() picks it up; referenced only via the DB query.
    SocialAccount.objects.create(
        workspace=workspace,
        platform="mastodon",
        account_platform_id="masto-1",
        account_name="Masto Test",
        instance_url="https://mastodon.social",
        oauth_access_token="tok",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    MastodonAppRegistration.objects.create(
        instance_url="https://mastodon.social",
        client_id="cid",
        client_secret="csecret",
    )

    with (
        patch("apps.inbox.tasks.get_provider") as get_provider,
        patch("apps.common.validators.is_safe_url", return_value=True),
    ):
        get_provider.return_value.get_messages.return_value = []
        InboxSyncEngine().sync_all()

    get_provider.assert_called_once()
    platform, credentials = get_provider.call_args.args
    assert platform == "mastodon"
    assert credentials["instance_url"] == "https://mastodon.social"


@pytest.mark.django_db
def test_first_sync_bounds_the_window_instead_of_asking_for_everything(connected_account):
    """`since=None` on a first sync would import the account's whole history.

    On YouTube that means paginating every comment thread the channel ever
    received, one quota-billed request per page. The first poll must carry the
    configured cutoff instead.
    """
    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = []
        InboxSyncEngine().sync_all()

    since = get_provider.return_value.get_messages.call_args.kwargs["since"]
    assert since is not None
    expected = timezone.now() - timedelta(days=APP_DEFAULTS["inbox.first_sync_backfill_days"])
    assert abs((since - expected).total_seconds()) < 60


@pytest.mark.django_db
def test_first_sync_window_is_configurable_per_workspace(connected_account, workspace):
    WorkspaceSetting.objects.create(
        workspace=workspace,
        key="inbox.first_sync_backfill_days",
        value=7,
    )

    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = []
        InboxSyncEngine().sync_all()

    since = get_provider.return_value.get_messages.call_args.kwargs["since"]
    expected = timezone.now() - timedelta(days=7)
    assert abs((since - expected).total_seconds()) < 60


@pytest.mark.django_db
def test_zero_window_restores_unbounded_first_sync(connected_account, workspace):
    WorkspaceSetting.objects.create(
        workspace=workspace,
        key="inbox.first_sync_backfill_days",
        value=0,
    )

    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = []
        InboxSyncEngine().sync_all()

    assert get_provider.return_value.get_messages.call_args.kwargs["since"] is None


@pytest.mark.django_db
def test_later_syncs_resume_from_the_newest_stored_message(connected_account):
    """Once history exists the cutoff is that timestamp, not the backfill window."""
    existing = InboxMessage.objects.create(
        workspace=connected_account.workspace,
        social_account=connected_account,
        platform_message_id="seed-1",
        sender_name="Sender",
        body="hello",
        message_type=InboxMessage.MessageType.DM,
        received_at=timezone.now() - timedelta(hours=3),
    )

    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = []
        InboxSyncEngine().sync_all()

    assert get_provider.return_value.get_messages.call_args.kwargs["since"] == existing.received_at


@pytest.mark.django_db
def test_sync_refreshes_the_token_before_calling_the_provider(connected_account):
    """A stale token used to 401 the whole cycle: the scheduled health check runs
    every 6 hours, but Google's access tokens only live one."""
    from apps.social_accounts.models import SocialAccount

    with (
        patch("apps.inbox.tasks.get_provider") as get_provider,
        patch.object(SocialAccount, "get_fresh_access_token", return_value="fresh") as fresh,
    ):
        get_provider.return_value.get_messages.return_value = []
        InboxSyncEngine().sync_all()

    fresh.assert_called_once()
    assert get_provider.return_value.get_messages.call_args.kwargs["access_token"] == "fresh"
